"""DB 없이 확인하는 DB 저장소(teamapp/db.py)의 연결 풀·연결·트랜잭션 처리와 상태 변환.

가짜 psycopg2(연결, 커서)를 넣어 teamapp/db.py를 다른 이름으로 따로 불러온다(진짜 teamapp.db는 건드리지 않음).
가짜의 연결 풀은 psycopg2 2.9의 lib/pool.py와 같은 방식(쉬는 연결 목록 _pool, 쓰는 연결 _used/_rused,
minconn개까지만 쉬게 두는 _putconn)으로 옮겨 적었다. 실제 SQL은 tests/test_db.py가 진짜 DB로 확인한다.
"""
import importlib.util
import sys
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import teamapp.actions  # noqa: F401  db.py가 쓰는 모듈은 가짜를 넣기 전에 먼저 불러 둔다
from teamapp.roster import RosterRow
from teamapp.rules import Application, State, Student

DB_PATH = Path(__file__).resolve().parent.parent / "teamapp" / "db.py"

IDLE, ACTIVE, INTRANS, INERROR, UNKNOWN = range(5)  # psycopg2.extensions.TRANSACTION_STATUS_*


def make_fake_psycopg2():
    pg = types.ModuleType("psycopg2")
    pg.Error = type("Error", (Exception,), {})
    pg.InterfaceError = type("InterfaceError", (pg.Error,), {})
    pg.DatabaseError = type("DatabaseError", (pg.Error,), {})
    pg.OperationalError = type("OperationalError", (pg.DatabaseError,), {})
    pg.ProgrammingError = type("ProgrammingError", (pg.DatabaseError,), {})
    pg.IntegrityError = type("IntegrityError", (pg.DatabaseError,), {})
    pg.InternalError = type("InternalError", (pg.DatabaseError,), {})
    pg.connect = lambda *args, **kwargs: pg.connect_factory(*args, **kwargs)  # 테스트마다 connect_factory를 바꾼다
    pg.connect_factory = None

    ext = types.ModuleType("psycopg2.extensions")
    ext.TRANSACTION_STATUS_IDLE = IDLE
    ext.TRANSACTION_STATUS_ACTIVE = ACTIVE
    ext.TRANSACTION_STATUS_INTRANS = INTRANS
    ext.TRANSACTION_STATUS_INERROR = INERROR
    ext.TRANSACTION_STATUS_UNKNOWN = UNKNOWN

    pool = types.ModuleType("psycopg2.pool")

    class PoolError(pg.Error):
        pass

    class AbstractConnectionPool:
        def __init__(self, minconn, maxconn, *args, **kwargs):
            self.minconn = int(minconn)
            self.maxconn = int(maxconn)
            self.closed = False
            self._args = args
            self._kwargs = kwargs
            self._pool = []
            self._used = {}
            self._rused = {}
            self._keys = 0
            for _ in range(self.minconn):
                self._connect()

        def _connect(self, key=None):
            conn = pg.connect(*self._args, **self._kwargs)
            if key is not None:
                self._used[key] = conn
                self._rused[id(conn)] = key
            else:
                self._pool.append(conn)
            return conn

        def _getkey(self):
            self._keys += 1
            return self._keys

        def _getconn(self, key=None):
            if self.closed:
                raise PoolError("connection pool is closed")
            if key is None:
                key = self._getkey()
            if key in self._used:
                return self._used[key]
            if self._pool:
                self._used[key] = conn = self._pool.pop()
                self._rused[id(conn)] = key
                return conn
            if len(self._used) == self.maxconn:
                raise PoolError("connection pool exhausted")
            return self._connect(key)

        def _putconn(self, conn, key=None, close=False):
            if self.closed:
                raise PoolError("connection pool is closed")
            if key is None:
                key = self._rused.get(id(conn))
                if key is None:
                    raise PoolError("trying to put unkeyed connection")
            if len(self._pool) < self.minconn and not close:
                if not conn.closed:
                    status = conn.info.transaction_status
                    if status == UNKNOWN:
                        conn.close()
                    elif status != IDLE:
                        conn.rollback()
                        self._pool.append(conn)
                    else:
                        self._pool.append(conn)
            else:
                conn.close()
            if not self.closed or key in self._used:
                del self._used[key]
                del self._rused[id(conn)]

        def _closeall(self):
            if self.closed:
                raise PoolError("connection pool is closed")
            for conn in self._pool + list(self._used.values()):
                conn.close()
            self.closed = True

    class ThreadedConnectionPool(AbstractConnectionPool):
        def __init__(self, minconn, maxconn, *args, **kwargs):
            AbstractConnectionPool.__init__(self, minconn, maxconn, *args, **kwargs)
            self._lock = threading.Lock()

        def getconn(self, key=None):
            with self._lock:
                return self._getconn(key)

        def putconn(self, conn=None, key=None, close=False):
            with self._lock:
                self._putconn(conn, key, close)

        def closeall(self):
            with self._lock:
                self._closeall()

    pool.PoolError = PoolError
    pool.AbstractConnectionPool = AbstractConnectionPool
    pool.ThreadedConnectionPool = ThreadedConnectionPool
    pg.pool, pg.extensions = pool, ext
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
    """psycopg2 연결 흉내: autocommit이 꺼져 있으면 첫 질의 앞에 BEGIN, 트랜잭션이 없으면 commit/rollback은 아무것도 안 보냄.

    "fail"로 시작하는 질의는 IntegrityError를 내고 트랜잭션을 오류 상태(INERROR)로 만든다.
    """

    def __init__(self, name, log, dead=False, closed=0, results=()):
        self.name, self.log, self.dead, self.closed = name, log, dead, closed
        self.results = list(results)  # fetchone이 차례로 돌려줄 값
        self.rowcount = 1
        self._autocommit = False
        self.in_transaction = False
        self.failed = False
        self.lost = False  # 서버 연결이 끊긴 것을 libpq가 알고 있음(closed는 아직 0)

    @property
    def info(self):
        if self.closed or self.lost:
            status = UNKNOWN
        elif self.failed:
            status = INERROR
        else:
            status = INTRANS if self.in_transaction else IDLE
        return types.SimpleNamespace(transaction_status=status)

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
        self.log.append((self.name, " ".join(sql.split()), params))
        if sql.startswith("fail"):
            self.failed = True
            raise PG.IntegrityError("duplicate key value violates unique constraint")

    def _end(self, word):
        if self.closed:
            raise PG.InterfaceError("connection already closed")
        if self.in_transaction:
            self.in_transaction = False
            self.failed = False
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
    """connection()의 꺼내기·돌려주기 순서만 보는 단순한 풀. conns를 차례로 내준다."""

    def __init__(self, conns, exhausted=0, closed=False, error=None):
        self.conns = list(conns)
        self.exhausted = exhausted  # 이만큼 PoolError("connection pool exhausted")를 낸다
        self.closed = closed
        self.error = error  # 새 연결을 열다 실패한 경우
        self.put = []
        self.idle_closed = 0

    def getconn(self):
        if self.closed:
            raise PG.pool.PoolError("connection pool is closed")
        if self.exhausted:
            self.exhausted -= 1
            raise PG.pool.PoolError("connection pool exhausted")
        if self.error is not None:
            raise self.error
        return self.conns.pop(0)

    def putconn(self, conn, key=None, close=False):
        self.put.append((conn.name, close))
        if close:
            conn.close()

    def close_idle(self):
        self.idle_closed += 1


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
LOCK = "set local lock_timeout = %s; select pg_advisory_xact_lock(%s)"
POOL_ARGS = dict(
    dsn="postgresql://example",
    connect_timeout=10,
    keepalives=1,
    keepalives_idle=30,
    keepalives_interval=10,
    keepalives_count=3,
    application_name="team-formation",
)


class DbLogicTestCase(unittest.TestCase):
    def setUp(self):
        self.log = []
        self.clock = FakeClock()
        patcher = mock.patch.object(db, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.opened = []  # 가짜 connect가 연 연결(순서대로)
        patcher = mock.patch.object(PG, "connect_factory", self.fake_connect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def conn(self, name, **kwargs):
        return FakeConn(name, self.log, **kwargs)

    def fake_connect(self, *args, **kwargs):
        self.connect_args = (args, kwargs)
        conn = self.conn(f"C{len(self.opened) + 1}")
        self.opened.append(conn)
        return conn

    def sqls(self):
        return [entry[1] for entry in self.log]


class PoolTests(DbLogicTestCase):
    def test_make_pool_opens_one_connection(self):
        pool = db.make_pool("postgresql://example")
        self.assertIsInstance(pool, db.ReusingConnectionPool)
        self.assertEqual((pool.minconn, pool.maxconn), (1, 5))
        self.assertEqual(len(self.opened), 1)  # 시작할 때 미리 여는 것은 1개뿐
        self.assertEqual(self.connect_args, ((), POOL_ARGS))

    def test_returned_connections_are_reused(self):
        pool = db.make_pool("postgresql://example")
        # 화면 세 개가 겹쳐 연결 세 개를 쓴 뒤 돌려준다
        with db.connection(pool) as a, db.connection(pool) as b, db.connection(pool) as c:
            first = {a, b, c}
        self.assertEqual(len(self.opened), 3)
        self.assertFalse(any(conn.closed for conn in first))  # minconn(1)개를 넘어도 닫지 않고 쉬게 둔다
        self.assertEqual(len(pool._pool), 3)
        # 다시 세 개가 겹쳐도 새로 접속하지 않고 쉬던 연결을 쓴다
        with db.connection(pool) as a, db.connection(pool) as b, db.connection(pool) as c:
            self.assertEqual({a, b, c}, first)
        with db.connection(pool) as again:
            self.assertIn(again, first)
        self.assertEqual(len(self.opened), 3)

    def test_closed_broken_or_close_true_connections_are_not_kept(self):
        pool = db.make_pool("postgresql://example")
        conns = [pool.getconn() for _ in range(5)]
        a, b, c, d, e = conns
        pool.putconn(a, close=True)
        b.close()
        pool.putconn(b)
        c.lost = True  # 서버 연결이 끊김(TRANSACTION_STATUS_UNKNOWN)
        pool.putconn(c)
        d.cursor().execute("update x", None)  # 트랜잭션 중
        pool.putconn(d)
        pool.putconn(e)
        self.assertEqual([conn.closed for conn in conns], [1, 1, 1, 0, 0])
        self.assertEqual(pool._pool, [d, e])
        self.assertEqual(self.log[-1], ("C4", "ROLLBACK"))  # 트랜잭션을 되돌리고 쉬게 둔다
        self.assertEqual((pool._used, pool._rused), ({}, {}))
        with self.assertRaises(PG.pool.PoolError):
            pool.putconn(self.conn("X"))  # 이 풀의 연결이 아님

    def test_dead_idle_connections_are_all_replaced(self):
        pool = db.make_pool("postgresql://example")
        with db.connection(pool) as a, db.connection(pool) as b:
            pass
        a.dead = b.dead = True  # 쉬는 동안 둘 다 서버 쪽에서 끊김
        with db.connection(pool) as conn:
            self.assertNotIn(conn, (a, b))
        self.assertTrue(a.closed and b.closed)
        self.assertEqual(len(self.opened), 3)
        self.assertEqual(pool._pool, [conn])

    def test_close_idle_and_closeall(self):
        pool = db.make_pool("postgresql://example")
        with db.connection(pool) as a, db.connection(pool) as b:
            pass
        pool.close_idle()
        self.assertEqual(pool._pool, [])
        self.assertTrue(a.closed and b.closed)
        pool.closeall()
        self.assertTrue(pool.closed)


class ConnectFailureTests(DbLogicTestCase):
    CASES = {
        'could not translate host name "ss@aws-0.pooler.supabase.com" to address: Name or service not known':
            "주소/포트 오류",
        'invalid integer value "ss@5432" for connection option "port"': "주소/포트 오류",
        'connection to server at "h" (1.2.3.4), port 5432 failed: timeout expired': "시간 초과",
        'connection to server at "h", port 5432 failed: FATAL:  password authentication failed for user "pw-ss"':
            "인증 실패",
        "connection to server at \"h\", port 5432 failed: FATAL:  Tenant or user not found": "인증 실패",
        'connection to server at "h", port 5432 failed: Connection refused': "연결 실패",
    }

    def assert_generic(self, caught, reason):
        error = caught.exception
        self.assertIsInstance(error, PG.OperationalError)
        self.assertEqual(str(error), f"DB에 연결하지 못했습니다 ({reason})")
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)  # 원래 오류(문구)가 따라가지 않는다
        self.assertTrue(error.__suppress_context__)

    def test_make_pool_failures_become_generic_messages(self):
        for text, reason in self.CASES.items():
            with self.subTest(reason=reason, text=text[:30]):
                with mock.patch.object(PG, "connect_factory", side_effect=PG.OperationalError(text)):
                    with self.assertRaises(PG.OperationalError) as caught:
                        db.make_pool("postgresql://u:pw-ss@aws-0.pooler.supabase.com:5432/postgres")
                self.assert_generic(caught, reason)
                self.assertNotIn("ss", str(caught.exception))

    def test_new_connection_failure_in_connection_is_generic(self):
        text = 'could not translate host name "ss@h" to address'
        pool = FakePool([], error=PG.OperationalError(text))
        with self.assertRaises(PG.OperationalError) as caught:
            with db.connection(pool):
                self.fail("연결을 빌려 주면 안 됨")
        self.assert_generic(caught, "주소/포트 오류")

    def test_malformed_url_error_does_not_show_the_url(self):
        secret_error = PG.ProgrammingError('invalid dsn: unexpected character in URI: "postgresql://u:pw-secret@h"')
        with mock.patch.object(PG, "connect_factory", side_effect=secret_error):
            with self.assertRaises(ValueError) as caught:
                db.make_pool("postgresql://u:pw-secret@h")
        self.assertEqual(str(caught.exception), "DATABASE_URL 형식이 맞지 않습니다")
        self.assertIsNone(caught.exception.__context__)


class ConnectionTests(DbLogicTestCase):
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
                self.assertEqual(pool.idle_closed, 1)  # 같이 쉬던 연결도 닫고 새로 받는다
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
    def test_write_repo_locks_first_with_timeout_and_commits(self):
        pool = FakePool([self.conn("A")])
        with db.write_repo(pool) as repo:
            repo.set_closed()
        sqls = self.sqls()
        self.assertEqual(sqls[:3], [PING, "BEGIN", LOCK])
        self.assertTrue(sqls[3].startswith("insert into tf_settings"))
        self.assertEqual(sqls[4:], ["COMMIT"])
        self.assertEqual(self.log[2], ("A", LOCK, ("15s", 7240001)))  # 잠금 시간 제한과 잠금을 한 번에
        self.assertEqual((db.LOCK_KEY, db.LOCK_TIMEOUT), (7240001, "15s"))
        self.assertEqual(pool.put, [("A", False)])

    def test_write_repo_rolls_back_and_reraises(self):
        pool = FakePool([self.conn("A")])
        with self.assertRaises(RuntimeError):
            with db.write_repo(pool) as repo:
                repo.set_closed()
                raise RuntimeError("일부러")
        self.assertEqual(self.sqls()[-1], "ROLLBACK")
        self.assertNotIn("COMMIT", self.sqls())
        self.assertEqual(pool.put, [("A", False)])

    def test_swallowed_db_error_is_not_committed(self):
        pool = FakePool([self.conn("A")])
        with self.assertRaises(PG.InternalError):
            with db.write_repo(pool) as repo:
                repo.set_closed()
                try:
                    repo._cur.execute("fail: 유일성 제약 위반", None)
                except PG.IntegrityError:
                    pass  # 잡고 넘어가도 트랜잭션은 오류 상태
        self.assertEqual(self.sqls()[-1], "ROLLBACK")
        self.assertNotIn("COMMIT", self.sqls())
        self.assertEqual(pool.put, [("A", False)])

    def test_read_repo_is_read_only_and_rolls_back(self):
        pool = FakePool([self.conn("A", results=[(STATE_JSON,)])])
        with db.read_repo(pool) as repo:
            repo.load_state()
        sqls = self.sqls()
        self.assertEqual(sqls[:3] + sqls[-1:], [PING, "BEGIN", "set transaction read only", "ROLLBACK"])

    def test_ensure_schema_runs_sql_under_lock(self):
        pool = FakePool([self.conn("A")])
        db.ensure_schema(pool, "create table if not exists tf_x (id int)")
        self.assertEqual(self.sqls(), [PING, "BEGIN", LOCK, "create table if not exists tf_x (id int)", "COMMIT"])
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
