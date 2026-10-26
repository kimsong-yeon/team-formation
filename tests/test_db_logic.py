"""DB 없이 확인하는 DB 저장소(teamapp/db.py)의 연결·트랜잭션 처리와 상태 변환.

가짜 psycopg2(연결, 커서, 풀)를 넣어 teamapp/db.py를 다른 이름으로 따로 불러온다(진짜 teamapp.db는 건드리지 않음).
실제 SQL은 tests/test_db.py가 진짜 DB로 확인한다.
"""
import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import teamapp.actions  # noqa: F401  db.py가 쓰는 모듈은 가짜를 넣기 전에 먼저 불러 둔다
from teamapp.roster import RosterRow
from teamapp.rules import Application, State, Student

DB_PATH = Path(__file__).resolve().parent.parent / "teamapp" / "db.py"


def make_fake_psycopg2():
    pg = types.ModuleType("psycopg2")
    pg.Error = type("Error", (Exception,), {})
    pg.InterfaceError = type("InterfaceError", (pg.Error,), {})
    pg.DatabaseError = type("DatabaseError", (pg.Error,), {})
    pg.OperationalError = type("OperationalError", (pg.DatabaseError,), {})
    pg.ProgrammingError = type("ProgrammingError", (pg.DatabaseError,), {})
    pg.pool = types.ModuleType("psycopg2.pool")
    pg.pool.PoolError = type("PoolError", (pg.Error,), {})
    pg.pool.AbstractConnectionPool = object
    pg.pool.ThreadedConnectionPool = mock.Mock(name="ThreadedConnectionPool")
    pg.extensions = types.ModuleType("psycopg2.extensions")
    return pg


PG = make_fake_psycopg2()


def load_db_module():
    spec = importlib.util.spec_from_file_location("teamapp_db_with_fake_psycopg2", DB_PATH)
    module = importlib.util.module_from_spec(spec)
    fakes = {"psycopg2": PG, "psycopg2.pool": PG.pool, "psycopg2.extensions": PG.extensions}
    with mock.patch.dict(sys.modules, fakes):
        spec.loader.exec_module(module)
    return module


db = load_db_module()


class FakeConn:
    """psycopg2 연결 흉내: autocommit이 꺼져 있으면 첫 질의 앞에 BEGIN, 트랜잭션이 없으면 commit/rollback은 아무것도 안 보냄."""

    def __init__(self, name, log, dead=False, closed=0, results=()):
        self.name, self.log, self.dead, self.closed = name, log, dead, closed
        self.results = list(results)  # fetchone이 차례로 돌려줄 값
        self.rowcount = 1
        self._autocommit = False
        self.in_transaction = False

    @property
    def autocommit(self):
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value):
        if self.closed:
            raise PG.InterfaceError("connection already closed")
        if self.in_transaction:
            raise PG.ProgrammingError("set_session cannot be used inside a transaction")
        self._autocommit = value

    def cursor(self):
        if self.closed:
            raise PG.InterfaceError("connection already closed")
        return FakeCursor(self)

    def run(self, sql, params):
        if self.closed:
            raise PG.InterfaceError("connection already closed")
        if self.dead:
            self.closed = 2  # psycopg2는 끊긴 것을 알게 되면 closed를 2로 바꾼다
            raise PG.OperationalError("server closed the connection unexpectedly")
        if not self._autocommit and not self.in_transaction:
            self.in_transaction = True
            self.log.append((self.name, "BEGIN"))
        self.log.append((self.name, " ".join(sql.split())[:40], params))

    def _end(self, word):
        if self.closed:
            raise PG.InterfaceError("connection already closed")
        if self.in_transaction:
            self.in_transaction = False
            self.log.append((self.name, word))

    def commit(self):
        self._end("COMMIT")

    def rollback(self):
        self._end("ROLLBACK")

    def close(self):
        self.closed = 1


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.last_sql = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def rowcount(self):
        return self.conn.rowcount

    def execute(self, sql, params=None):
        self.conn.run(sql, params)
        self.last_sql = sql

    def fetchone(self):
        if self.last_sql == "select 1":  # 살아 있는지 확인하는 질의
            return (1,)
        return self.conn.results.pop(0)


class FakePool:
    def __init__(self, conns, exhausted=0, closed=False):
        self.conns = list(conns)
        self.exhausted = exhausted  # 이만큼 PoolError("connection pool exhausted")를 낸다
        self.closed = closed
        self.put = []

    def getconn(self):
        if self.closed:
            raise PG.pool.PoolError("connection pool is closed")
        if self.exhausted:
            self.exhausted -= 1
            raise PG.pool.PoolError("connection pool exhausted")
        return self.conns.pop(0)

    def putconn(self, conn, key=None, close=False):
        self.put.append((conn.name, close))
        if close:
            conn.close()


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


PING = "select 1"
LOCK = "select pg_advisory_xact_lock(%s)"


class DbLogicTestCase(unittest.TestCase):
    def setUp(self):
        self.log = []
        self.clock = FakeClock()
        patcher = mock.patch.object(db, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def conn(self, name, **kwargs):
        return FakeConn(name, self.log, **kwargs)


class ConnectionTests(DbLogicTestCase):
    def test_make_pool_arguments(self):
        PG.pool.ThreadedConnectionPool.reset_mock()
        db.make_pool("postgresql://example")
        PG.pool.ThreadedConnectionPool.assert_called_once_with(
            minconn=1,
            maxconn=8,
            dsn="postgresql://example",
            connect_timeout=10,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
            application_name="team-formation",
        )

    def test_malformed_url_error_does_not_show_the_url(self):
        secret_error = PG.ProgrammingError('invalid dsn: unexpected character in URI: "postgresql://u:pw-secret@h"')
        with mock.patch.object(PG.pool, "ThreadedConnectionPool", side_effect=secret_error):
            with self.assertRaises(ValueError) as caught:
                db.make_pool("postgresql://u:pw-secret@h")
        self.assertNotIn("pw-secret", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)  # 원래 오류는 traceback에 나오지 않는다

    def test_ping_without_transaction_then_return_to_pool(self):
        a = self.conn("A")
        pool = FakePool([a])
        with db.connection(pool) as conn:
            self.assertIs(conn, a)
            self.assertFalse(conn.autocommit)
            self.assertFalse(conn.in_transaction)
        self.assertEqual(self.log, [("A", PING, None)])  # BEGIN 없이 한 번
        self.assertEqual(pool.put, [("A", False)])

    def test_open_transaction_is_rolled_back_even_on_error(self):
        a = self.conn("A")
        pool = FakePool([a])
        with self.assertRaises(KeyError):
            with db.connection(pool) as conn:
                conn.cursor().execute("update x", None)
                raise KeyError("일부러")
        self.assertEqual(self.log, [("A", PING, None), ("A", "BEGIN"), ("A", "update x", None), ("A", "ROLLBACK")])
        self.assertEqual(pool.put, [("A", False)])

    def test_connection_lost_during_use_is_closed_not_reused(self):
        a = self.conn("A")
        pool = FakePool([a])
        with self.assertRaises(PG.OperationalError):
            with db.connection(pool) as conn:
                conn.dead = True
                conn.cursor().execute("select 2", None)
        self.assertEqual(pool.put, [("A", True)])

    def test_closed_or_dead_connection_is_replaced_once(self):
        for name, broken in {"닫힌 연결": {"closed": 1}, "서버에서 끊긴 연결": {"dead": True}}.items():
            with self.subTest(name):
                self.log.clear()
                a, b = self.conn("A", **broken), self.conn("B")
                pool = FakePool([a, b])
                with db.connection(pool) as conn:
                    self.assertIs(conn, b)
                self.assertEqual(pool.put, [("A", True), ("B", False)])
                self.assertTrue(a.closed)

    def test_gives_up_after_one_new_connection(self):
        a, b, c = self.conn("A", dead=True), self.conn("B", dead=True), self.conn("C")
        pool = FakePool([a, b, c])
        with self.assertRaises(PG.OperationalError) as caught:
            with db.connection(pool):
                self.fail("연결을 빌려 주면 안 됨")
        self.assertEqual(pool.put, [("A", True), ("B", True)])
        self.assertEqual(pool.conns, [c])
        self.assertNotIn("postgresql", str(caught.exception))

    def test_waits_while_pool_is_exhausted(self):
        a = self.conn("A")
        pool = FakePool([a], exhausted=3)
        with db.connection(pool) as conn:
            self.assertIs(conn, a)
        self.assertEqual(self.clock.sleeps, [db.POOL_RETRY_SLEEP] * 3)

    def test_gives_up_when_pool_stays_exhausted(self):
        pool = FakePool([self.conn("A")], exhausted=10**9)
        with self.assertRaises(PG.pool.PoolError):
            with db.connection(pool):
                self.fail("연결을 빌려 주면 안 됨")
        waited = sum(self.clock.sleeps)
        self.assertGreaterEqual(waited, db.POOL_WAIT_SECONDS)
        self.assertLess(waited, db.POOL_WAIT_SECONDS + 1)

    def test_closed_pool_fails_at_once(self):
        pool = FakePool([], closed=True)
        with self.assertRaises(PG.pool.PoolError):
            with db.connection(pool):
                pass
        self.assertEqual(self.clock.sleeps, [])


class TransactionTests(DbLogicTestCase):
    def test_write_repo_locks_first_and_commits(self):
        pool = FakePool([self.conn("A")])
        with db.write_repo(pool) as repo:
            repo.set_closed()
        self.assertEqual(
            [entry[1] for entry in self.log], [PING, "BEGIN", LOCK, "insert into tf_settings (key, value) val", "COMMIT"]
        )
        self.assertEqual(self.log[2], ("A", LOCK, (db.LOCK_KEY,)))
        self.assertEqual(db.LOCK_KEY, 7240001)
        self.assertEqual(pool.put, [("A", False)])

    def test_write_repo_rolls_back_and_reraises(self):
        pool = FakePool([self.conn("A")])
        with self.assertRaises(RuntimeError):
            with db.write_repo(pool) as repo:
                repo.set_closed()
                raise RuntimeError("일부러")
        self.assertEqual([entry[1] for entry in self.log][-1], "ROLLBACK")
        self.assertNotIn("COMMIT", [entry[1] for entry in self.log])
        self.assertEqual(pool.put, [("A", False)])

    def test_read_repo_is_read_only_and_rolls_back(self):
        pool = FakePool([self.conn("A", results=[(STATE_JSON,)])])
        with db.read_repo(pool) as repo:
            repo.load_state()
        self.assertEqual(
            [entry[1] for entry in self.log][:3] + [self.log[-1][1]],
            [PING, "BEGIN", "set transaction read only", "ROLLBACK"],
        )

    def test_ensure_schema_runs_sql_under_lock(self):
        pool = FakePool([self.conn("A")])
        db.ensure_schema(pool, "create table if not exists tf_x (id int)")
        self.assertEqual(
            [entry[1] for entry in self.log], [PING, "BEGIN", LOCK, "create table if not exists tf_x (id int)", "COMMIT"]
        )
        self.assertIsNone(self.log[3][2])  # 매개변수 없이 실행(% 글자를 바꾸지 않음)

    def test_schema_file_is_read_as_utf8(self):
        sql = db.read_schema_sql()
        self.assertIn("create table if not exists tf_students", sql)
        self.assertNotIn("%", sql)


class EnsureAdminTests(DbLogicTestCase):
    def setUp(self):
        super().setUp()
        for name, fake in (("hash_password", lambda pw: "h:" + pw), ("verify_password", lambda pw, h: h == "h:" + pw)):
            patcher = mock.patch.object(db, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_ensure_admin(self, existing):
        self.log.clear()
        db.ensure_admin(FakePool([self.conn("A", results=[existing])]), "pw")
        return [entry for entry in self.log if entry[1].startswith(("insert", "update"))]

    def test_empty_password_is_refused_before_connecting(self):
        for empty in ("", None):
            with self.subTest(password=empty):
                with self.assertRaises(ValueError):
                    db.ensure_admin(FakePool([]), empty)

    def test_create_keep_or_update(self):
        created = self.run_ensure_admin(None)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0][2], ("admin", "관리자", "h:pw"))
        self.assertEqual(self.run_ensure_admin((5, "h:pw", True)), [])
        for row in ((5, "h:old", True), (5, "h:pw", False)):
            with self.subTest(row=row):
                updated = self.run_ensure_admin(row)
                self.assertEqual([entry[2] for entry in updated], [("h:pw", 5)])


STATE_JSON = {
    "students": [
        {"id": 7, "student_no": "2026101", "name": "학생1(PL;RW)", "main_role": "기획자", "sub_roles": ["보고서 작성자"]},
        {"id": 8, "student_no": "2026113", "name": "학생13(RW;PT;FD)", "main_role": "보고서 작성자",
         "sub_roles": ["발표자", "프론트엔드 디자이너"]},
        {"id": 9, "student_no": "2026105", "name": "학생5(WP)", "main_role": "웹 프로그래머", "sub_roles": []},
    ],
    "team_ids": [1, 2, 3],
    "members": [[1, 8], [1, 7], [3, 9]],
    "applications": [
        {"id": 11, "team_id": 1, "student_id": 7, "status": "승인", "reason": "빈 팀에 지원",
         "created_at": "2026-11-01T00:00:01.5+00:00"},
        {"id": 12, "team_id": 1, "student_id": 8, "status": "대기", "reason": "",
         "created_at": "2026-11-01T09:00:02+09:00"},
    ],
    "votes": [[12, 7, "찬성"]],
    "closed": False,
}


class StateFromJsonTests(unittest.TestCase):
    def test_state_shape(self):
        utc = timezone.utc
        expected = State(
            students={
                7: Student(7, "2026101", "학생1(PL;RW)", "기획자", ("보고서 작성자",)),
                8: Student(8, "2026113", "학생13(RW;PT;FD)", "보고서 작성자", ("프론트엔드 디자이너", "발표자")),
                9: Student(9, "2026105", "학생5(WP)", "웹 프로그래머", ()),
            },
            team_ids=(1, 2, 3),
            members={1: (8, 7), 2: (), 3: (9,)},  # 빈 팀도 키로 있고, slot 순서 그대로
            applications=(
                Application(11, 1, 7, "승인", "빈 팀에 지원", datetime(2026, 11, 1, 0, 0, 1, 500000, tzinfo=utc)),
                Application(12, 1, 8, "대기", "", datetime(2026, 11, 1, 0, 0, 2, tzinfo=utc)),
            ),
            votes={12: {7: "찬성"}},
            closed=False,
        )
        state = db.state_from_json(STATE_JSON)
        self.assertEqual(state, expected)
        self.assertEqual(state.applications[1].created_at.utcoffset(), timedelta(hours=9))
        self.assertEqual(list(state.students), [7, 8, 9])

    def test_json_text_and_empty_database(self):
        import json

        self.assertEqual(db.state_from_json(json.dumps(STATE_JSON)), db.state_from_json(STATE_JSON))
        empty = {"students": [], "team_ids": [], "members": [], "applications": [], "votes": [], "closed": True}
        self.assertEqual(db.state_from_json(empty), State({}, (), {}, (), {}, True))


class PgRepoTests(DbLogicTestCase):
    def repo(self, **kwargs):
        self.connection = self.conn("A", **kwargs)
        return db.PgRepo(self.connection.cursor())

    def test_errors_like_memory_repo(self):
        with self.assertRaises(ValueError):  # 꽉 찬 팀: 넣을 자리가 없어 아무 행도 돌아오지 않음
            self.repo(results=[None]).add_member(1, 7)
        repo = self.repo()
        self.connection.rowcount = 0  # 바뀐 행이 없음
        with self.assertRaises(ValueError):
            repo.set_application_status(99, "거절", "없는 지원")
        self.log.clear()
        with self.assertRaises(ValueError):  # 학생 수와 해시 수가 다름
            repo.reset([RosterRow("1", "a", "기획자", ())], [], 3)
        self.assertIsNone(repo.find_account("adm\x00in"))
        self.assertEqual(self.log, [])  # 둘 다 DB에 아무것도 보내지 않는다

    def test_reset_parameters_follow_roster_order(self):
        rows = [
            RosterRow("2026101", "학생1", "기획자", ("보고서 작성자",)),
            RosterRow("2026102", "학생2", "테스터", ()),
            RosterRow("2026103", "학생3", "발표자", ("기획자", "테스터")),
        ]
        self.repo().reset(rows, ["h1", "h2", "h3"], 4)
        params = self.log[-1][2]
        self.assertEqual(params["student_nos"], ["2026101", "2026102", "2026103"])
        self.assertEqual(params["names"], ["학생1", "학생2", "학생3"])
        self.assertEqual(params["main_roles"], ["기획자", "테스터", "발표자"])
        self.assertEqual(params["hashes"], ["h1", "h2", "h3"])
        self.assertEqual(params["sub_student_nos"], ["2026101", "2026103", "2026103"])
        self.assertEqual(params["sub_roles"], ["보고서 작성자", "기획자", "테스터"])
        self.assertEqual(params["team_count"], 4)
        self.assertEqual(params["closed_key"], "closed")

    def test_insert_application_sets_decided_only_for_final_status(self):
        repo = self.repo(results=[(41,), (42,)])
        self.assertEqual(repo.insert_application(1, 7, "승인", "빈 팀에 지원"), 41)
        self.assertEqual(repo.insert_application(2, 8, "대기", ""), 42)
        decided = [entry[2]["decided"] for entry in self.log if entry[1].startswith("insert")]
        self.assertEqual(decided, [True, False])


if __name__ == "__main__":
    unittest.main()
