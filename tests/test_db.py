"""실제 PostgreSQL(Supabase)로 DB 저장소(teamapp/db.py)를 확인하는 테스트.

- psycopg2가 없거나, .env(또는 환경 변수)에 DATABASE_URL이나 ADMIN_PASSWORD가 없으면 모듈 전체를 건너뛴다.
- 주의: DATABASE_URL이 가리키는 DB의 tf_ 데이터(관리자를 뺀 학생, 팀, 팀원, 지원, 투표, 마감 표시)를 지우고
  연습용 명단을 다시 올린다. 관리자 계정은 남고, 비밀번호 해시는 ADMIN_PASSWORD에 맞춘다.
  끝나면 쉬움 명단(팀 3개, 학생 비밀번호 1234를 bcrypt로 해시)으로 처음 상태를 만들어 둔다.
  그래서 배포한 앱으로 시험하는 도중에는 돌리지 않는다. 로컬 .env와 Streamlit Secrets의 ADMIN_PASSWORD는 같아야 한다.
- 실행(수강생 컴퓨터): .venv\\Scripts\\python -m unittest tests.test_db -v
"""
import threading
import time
import unittest

from teamapp import actions, config
from teamapp.passwords import hash_password, verify_password
from teamapp.roster import ADMIN_NO, parse_roster
from teamapp.rules import APPROVED, BLOCKED, PENDING, REJECTED, ROLES, YES, State
from tests.memory_repo import MemoryRepo, fake_hash, fake_verify
from tests.scenarios import DATA_DIR, EASY, HARD, MEDIUM, play_full

try:
    import psycopg2
except ImportError:
    psycopg2 = None

try:
    import bcrypt
except ImportError:
    bcrypt = None

if psycopg2 is not None:
    from teamapp import db
else:
    db = None

TABLES = (
    "tf_students",
    "tf_student_sub_roles",
    "tf_teams",
    "tf_members",
    "tf_applications",
    "tf_votes",
    "tf_settings",
)

POOL = None
ADMIN_PASSWORD = None


def setUpModule():
    global POOL, ADMIN_PASSWORD
    if psycopg2 is None:
        raise unittest.SkipTest("psycopg2가 설치되어 있지 않음")
    dsn = config.get_secret("DATABASE_URL")
    if not dsn:
        raise unittest.SkipTest("DATABASE_URL이 없음(.env)")
    ADMIN_PASSWORD = config.get_secret("ADMIN_PASSWORD")
    if not ADMIN_PASSWORD:
        raise unittest.SkipTest("ADMIN_PASSWORD가 없음(.env)")
    POOL = db.make_pool(dsn)
    try:
        db.ensure_schema(POOL, db.read_schema_sql())
        db.ensure_admin(POOL, ADMIN_PASSWORD)
    except BaseException:
        POOL.closeall()
        POOL = None
        raise


def tearDownModule():
    global POOL
    if POOL is None:
        return
    try:
        # 빠르게 하려고 가짜 해시(h:1234)로 올린 명단이 DB에 남지 않도록, 끝에 쉬움 명단을 진짜 bcrypt 해시로 다시 올린다.
        if bcrypt is not None:
            reset(EASY.csv_name, EASY.team_count, hasher=hash_password)
    finally:
        POOL.closeall()
        POOL = None


def reset(csv_name="쉬움.csv", team_count=3, hasher=fake_hash):
    """명단을 올려 처음 상태를 만들고 (상태, 이름 -> 학생 id)를 돌려 준다."""
    data = (DATA_DIR / csv_name).read_bytes()
    with db.write_repo(POOL) as repo:
        outcome = actions.reset_roster(repo, repo.find_account(ADMIN_NO).id, data, team_count, hasher)
        if outcome.kind != APPROVED:
            raise AssertionError(outcome.text)
        state = repo.load_state()
    return state, {s.name: s.id for s in state.students.values()}


def read_state():
    with db.read_repo(POOL) as repo:
        return repo.load_state()


def query(sql, params=None):
    """질의 하나를 실행해 모든 행을 돌려 주고 트랜잭션은 되돌린다."""
    with db.connection(POOL) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        finally:
            conn.rollback()


def summary(state):
    """학생 id 대신 학번으로 바꾼 상태 요약(MemoryRepo와 PgRepo는 id가 다를 수 있음)."""
    no = {s.id: s.student_no for s in state.students.values()}
    applicant = {a.id: no[a.student_id] for a in state.applications}
    return {
        "students": sorted(
            (s.student_no, s.name, s.main_role, tuple(sorted(s.sub_roles))) for s in state.students.values()
        ),
        "team_ids": state.team_ids,
        "members": {team_id: tuple(no[i] for i in ids) for team_id, ids in state.members.items()},
        "applications": [(no[a.student_id], a.team_id, a.status, a.reason) for a in state.applications],
        "votes": sorted(
            (applicant[app_id], no[voter_id], value)
            for app_id, votes in state.votes.items()
            for voter_id, value in votes.items()
        ),
        "closed": state.closed,
    }


class SchemaTests(unittest.TestCase):
    def assert_rejected(self, *statements):
        """문장들을 트랜잭션 하나에서 차례로 실행한다. 마지막 문장만 IntegrityError여야 하고, 끝나면 모두 되돌린다."""
        with db.connection(POOL) as conn:
            try:
                with conn.cursor() as cur:
                    for sql, params in statements[:-1]:
                        cur.execute(sql, params)
                    with self.assertRaises(psycopg2.IntegrityError):
                        cur.execute(*statements[-1])
            finally:
                conn.rollback()

    def test_schema_has_constraints(self):
        db.ensure_schema(POOL, db.read_schema_sql())  # 두 번 실행해도 된다
        rows = query(
            "select table_name from information_schema.tables "
            "where table_schema = current_schema() and table_name = any(%s)",
            (list(TABLES),),
        )
        self.assertEqual(sorted(r[0] for r in rows), sorted(TABLES))
        rows = query(
            "select c.relname, c.relrowsecurity from pg_class c "
            "where c.relnamespace = current_schema()::regnamespace and c.relname = any(%s)",
            (list(TABLES),),
        )
        self.assertEqual(dict(rows), {name: True for name in TABLES})  # RLS가 모두 켜져 있다
        rows = query("select indexdef from pg_indexes where indexname = 'tf_one_pending_per_student'")
        self.assertEqual(len(rows), 1)
        self.assertIn("UNIQUE", rows[0][0])

        _, ids = reset()
        s1, s2, s3 = ids["학생1(PL;RW)"], ids["학생4(WP;FD)"], ids["학생7(FD;PT)"]
        with db.write_repo(POOL) as repo:
            repo.add_member(1, s1)
        member = "insert into tf_members (team_id, slot_no, student_id) values (%s, %s, %s)"
        application = "insert into tf_applications (team_id, student_id, status) values (%s, %s, %s)"
        student = (
            "insert into tf_students (student_no, name, main_role, password_hash, is_admin) "
            "values (%s, %s, %s, 'h', %s)"
        )
        cases = {
            "같은 학생을 두 팀에": [(member, (2, 1, s1))],
            "같은 자리에 두 명": [(member, (1, 1, s2))],
            "slot_no 5": [(member, (1, 5, s2))],
            "slot_no 0": [(member, (2, 0, s2))],
            "없는 팀": [(member, (9, 1, s2))],
            "대기 지원 두 개": [(application, (2, s2, PENDING)), (application, (3, s2, PENDING))],
            "모르는 지원 상태": [(application, (2, s2, "보류"))],
            "모르는 주 역할": [(student, ("t-1", "테스트", "디자이너", False))],
            "주 역할 없는 학생": [(student, ("t-2", "테스트", None, False))],
            "겹치는 학번": [(student, ("2026101", "테스트", ROLES[0], False))],
            "모르는 보조 역할": [("insert into tf_student_sub_roles (student_id, role) values (%s, %s)", (s1, "PM"))],
            "겹치는 팀 이름": [("insert into tf_teams (id, name) values (%s, %s)", (9, "팀1"))],
        }
        vote = "insert into tf_votes (application_id, voter_id, vote) values (%s, %s, %s)"
        with db.write_repo(POOL) as repo:
            app_id = repo.insert_application(1, s3, PENDING, "")
        cases["같은 투표 두 번"] = [(vote, (app_id, s1, YES)), (vote, (app_id, s1, YES))]
        cases["모르는 투표 값"] = [(vote, (app_id, s1, "기권"))]
        for name, statements in cases.items():
            with self.subTest(name):
                self.assert_rejected(*statements)

        # 막힌 문장은 모두 되돌려져 처음 넣은 것만 남는다
        state = read_state()
        self.assertEqual(state.members, {1: (s1,), 2: (), 3: ()})
        self.assertEqual([(a.id, a.status) for a in state.applications], [(app_id, PENDING)])
        self.assertEqual(state.votes, {})
        self.assertEqual(len(state.students), 8)
        self.assertEqual(query("select count(*) from tf_teams")[0][0], 3)

        # 관리자는 주 역할이 없어도 된다
        rows = query("select main_role, is_admin from tf_students where student_no = %s", (ADMIN_NO,))
        self.assertEqual(rows, [(None, True)])


class ScenarioTests(unittest.TestCase):
    def test_scenarios_on_real_db(self):
        """과제 5의 세 시나리오를 PgRepo로 다시 돌려 MemoryRepo와 같은 결과인지 본다."""
        for scenario in (EASY, MEDIUM, HARD):
            with self.subTest(csv=scenario.csv_name):
                memory = MemoryRepo()
                expected = play_full(memory, scenario, fake_hash, fake_verify)
                with db.write_repo(POOL) as repo:  # 연결 하나, 트랜잭션 하나: 자기가 쓴 것을 바로 읽는다
                    results = play_full(repo, scenario, fake_hash, fake_verify)
                    inside = repo.load_state()
                self.assertEqual([o.kind for _, o in results], [word for word, _ in results])
                self.assertEqual([o.text for _, o in results], [o.text for _, o in expected])
                self.assertEqual(summary(inside), summary(memory.load_state()))
                self.assertEqual(summary(read_state()), summary(inside))  # commit되어 다른 연결에서도 보인다
                self.assertEqual(read_state(), inside)


class PasswordTests(unittest.TestCase):
    def setUp(self):
        if bcrypt is None:
            self.skipTest("bcrypt가 설치되어 있지 않음")

    def test_password_is_hashed(self):
        reset(hasher=hash_password)  # 실제 bcrypt
        rows = query("select student_no, password_hash from tf_students where not is_admin order by id")
        self.assertEqual(len(rows), 8)
        for student_no, password_hash in rows:
            with self.subTest(student_no=student_no):
                self.assertNotEqual(password_hash, "1234")
                self.assertTrue(password_hash.startswith("$2b$"), "bcrypt 해시가 아님")
        self.assertEqual(len({h for _, h in rows}), 8)  # 소금이 학생마다 다르다
        self.assertTrue(verify_password("1234", rows[0][1]))
        with db.read_repo(POOL) as repo:
            outcome, account = actions.login(repo, rows[0][0], "1234")
            self.assertEqual(outcome.kind, APPROVED)
            self.assertEqual(account.student_no, rows[0][0])
            self.assertEqual(actions.login(repo, rows[0][0], "0000")[0].kind, BLOCKED)

    def test_admin_kept_after_reset(self):
        self.addCleanup(db.ensure_admin, POOL, ADMIN_PASSWORD)  # 실패해도 관리자 비밀번호를 되돌린다
        with db.read_repo(POOL) as repo:
            before = repo.find_account(ADMIN_NO)
        reset(EASY.csv_name, EASY.team_count)
        state, _ = reset(MEDIUM.csv_name, MEDIUM.team_count)
        self.assertEqual(len(state.students), 12)
        self.assertNotIn(ADMIN_NO, {s.student_no for s in state.students.values()})
        with db.read_repo(POOL) as repo:
            after = repo.find_account(ADMIN_NO)
            self.assertEqual(after, before)
            self.assertEqual((after.name, after.is_admin), ("관리자", True))
            outcome, account = actions.login(repo, ADMIN_NO, ADMIN_PASSWORD)
            self.assertEqual(outcome.kind, APPROVED)
            self.assertTrue(account.is_admin)
            self.assertEqual(actions.login(repo, ADMIN_NO, ADMIN_PASSWORD + "x")[0].kind, BLOCKED)

        # 해시가 ADMIN_PASSWORD와 맞으면 ensure_admin은 해시를 바꾸지 않는다
        db.ensure_admin(POOL, ADMIN_PASSWORD)
        with db.read_repo(POOL) as repo:
            self.assertEqual(repo.find_account(ADMIN_NO).password_hash, before.password_hash)

        # 해시가 다르면 ADMIN_PASSWORD로 다시 맞춘다
        with db.connection(POOL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "update tf_students set password_hash = %s where student_no = %s", (fake_hash("x"), ADMIN_NO)
                )
            conn.commit()
        db.ensure_admin(POOL, ADMIN_PASSWORD)
        with db.read_repo(POOL) as repo:
            fixed = repo.find_account(ADMIN_NO)
            self.assertEqual(fixed.id, before.id)
            self.assertTrue(fixed.password_hash.startswith("$2b$"))
            self.assertEqual(actions.login(repo, ADMIN_NO, ADMIN_PASSWORD)[0].kind, APPROVED)

        for empty in ("", None):
            with self.subTest(password=empty):
                with self.assertRaises(ValueError):
                    db.ensure_admin(POOL, empty)


class ConnectionTests(unittest.TestCase):
    def select_one(self):
        with db.connection(POOL) as conn:
            with conn.cursor() as cur:
                cur.execute("select 1")
                return cur.fetchone()[0]

    def test_broken_connection_is_replaced(self):
        # 꺼낸 연결을 일부러 닫고 돌려준다
        conn = POOL.getconn()
        conn.close()
        POOL.putconn(conn)
        self.assertEqual(self.select_one(), 1)

        # 서버 쪽에서 끊긴 연결(클라이언트는 아직 모름)이 풀에 들어 있어도 새 연결로 바꾼다
        victim = POOL.getconn()
        killer = POOL.getconn()
        try:
            with victim.cursor() as cur:
                cur.execute("select pg_backend_pid()")
                victim_pid = cur.fetchone()[0]
            victim.rollback()
        finally:
            POOL.putconn(victim)  # 풀에 남는다(최소 1개)
        try:
            with killer.cursor() as cur:
                cur.execute("select pg_terminate_backend(%s, 5000)", (victim_pid,))  # 끝날 때까지 최대 5초 기다림
                self.assertIs(cur.fetchone()[0], True)
            killer.commit()
        finally:
            POOL.putconn(killer, close=True)
        with db.connection(POOL) as conn:
            with conn.cursor() as cur:
                cur.execute("select pg_backend_pid()")
                self.assertNotEqual(cur.fetchone()[0], victim_pid)
        self.assertEqual(self.select_one(), 1)


class RepoTests(unittest.TestCase):
    def test_load_state_shape(self):
        _, ids = reset()
        s1, s4 = ids["학생1(PL;RW)"], ids["학생4(WP;FD)"]
        with db.write_repo(POOL) as repo:
            self.assertEqual(actions.apply(repo, s1, 1).kind, APPROVED)
            self.assertEqual(actions.apply(repo, s4, 1).kind, PENDING)
            app_id = max(a.id for a in repo.load_state().applications)
            repo.record_vote(app_id, s1, YES)
        state = read_state()
        self.assertIsInstance(state, State)
        self.assertEqual(state.team_ids, (1, 2, 3))
        self.assertEqual(state.members, {1: (s1,), 2: (), 3: ()})
        self.assertEqual([(a.student_id, a.team_id, a.status) for a in state.applications],
                         [(s1, 1, APPROVED), (s4, 1, PENDING)])
        self.assertEqual([a.reason for a in state.applications], ["빈 팀에 지원", ""])
        for app in state.applications:
            self.assertIsNotNone(app.created_at.tzinfo)
        self.assertEqual(state.votes, {app_id: {s1: YES}})
        self.assertFalse(state.closed)
        first = state.students[s1]
        self.assertEqual((first.student_no, first.main_role, first.sub_roles), ("2026101", ROLES[0], ("보고서 작성자",)))
        self.assertIsInstance(first.sub_roles, tuple)
        self.assertEqual(len(state.students), 8)

        # decided_at: 대기는 비어 있고, 결과가 나면 채워진다
        decided = dict(query("select student_id, decided_at is not null from tf_applications"))
        self.assertEqual(decided, {s1: True, s4: False})
        with db.write_repo(POOL) as repo:
            repo.set_application_status(app_id, REJECTED, "팀원이 거부함")
            repo.set_closed()
            repo.set_closed()  # 두 번 해도 된다
        self.assertEqual(query("select decided_at is not null from tf_applications where id = %s", (app_id,)), [(True,)])
        state = read_state()
        self.assertTrue(state.closed)
        self.assertEqual(state.application(app_id).reason, "팀원이 거부함")

        # 명단을 다시 올리면 마감 표시와 지원, 투표가 지워지고 학생 id와 지원 id는 다시 쓰지 않는다
        state2, ids2 = reset()
        self.assertFalse(state2.closed)
        self.assertEqual(state2.applications, ())
        self.assertEqual(state2.votes, {})
        self.assertGreater(min(ids2.values()), max(ids.values()))
        roster = parse_roster((DATA_DIR / EASY.csv_name).read_bytes())
        by_id = sorted(state2.students.values(), key=lambda s: s.id)
        self.assertEqual([s.student_no for s in by_id], [row.student_no for row in roster])  # id는 명단 순서
        with db.write_repo(POOL) as repo:
            self.assertGreater(repo.insert_application(2, ids2["학생2(PL;PT)"], APPROVED, "빈 팀에 지원"), app_id)
            self.assertIsNone(repo.get_account(s1))  # 예전 학생 id는 계정이 아니다
            self.assertIsNone(repo.find_account("9999999"))

    def test_add_member_uses_smallest_free_slot(self):
        _, ids = reset(HARD.csv_name, HARD.team_count)
        a, b, c, d, e = (ids[n] for n in ("학생1(PL;RW)", "학생4(WP;PL)", "학생7(FD;PT;TT)", "학생13(RW;PT;FD)", "학생2(PL;PT)"))
        with db.write_repo(POOL) as repo:
            for student_id in (a, b, c, d):
                repo.add_member(1, student_id)
        slots = "select slot_no, student_id from tf_members where team_id = 1 order by slot_no"
        self.assertEqual(query(slots), [(1, a), (2, b), (3, c), (4, d)])

        # 꽉 찬 팀에 넣으면 ValueError이고 아무것도 바뀌지 않는다(트랜잭션 되돌림)
        with self.assertRaises(ValueError):
            with db.write_repo(POOL) as repo:
                repo.remove_member(ids["학생5(WP;FD)"])  # 팀이 없는 학생: 오류 없이 지나간다
                repo.set_closed()
                repo.add_member(1, e)
        self.assertFalse(read_state().closed)

        # 이미 팀이 있는 학생은 유일성 제약에 걸린다
        with self.assertRaises(psycopg2.IntegrityError):
            with db.write_repo(POOL) as repo:
                repo.add_member(2, a)

        with db.write_repo(POOL) as repo:
            repo.remove_member(b)
            repo.add_member(1, e)
        self.assertEqual(query(slots), [(1, a), (2, e), (3, c), (4, d)])
        self.assertEqual(read_state().members[1], (a, e, c, d))

    def test_write_repo_rolls_back_on_error_and_read_repo_is_read_only(self):
        reset()
        with self.assertRaises(RuntimeError):
            with db.write_repo(POOL) as repo:
                repo.set_closed()
                raise RuntimeError("일부러 낸 오류")
        self.assertFalse(read_state().closed)

        with self.assertRaises(psycopg2.Error) as caught:
            with db.read_repo(POOL) as repo:
                repo.set_closed()
        self.assertEqual(caught.exception.pgcode, "25006")  # read_only_sql_transaction
        self.assertFalse(read_state().closed)

        with self.assertRaises(ValueError):
            with db.write_repo(POOL) as repo:
                repo.set_application_status(10**12, REJECTED, "없는 지원")

    def test_write_repo_serializes_writers(self):
        """두 쓰기가 겹치면 뒤의 쓰기는 앞의 쓰기가 commit될 때까지 잠금에서 기다리고, 그 뒤의 상태를 읽는다."""
        reset()
        first_inside = threading.Event()
        events = []
        errors = []

        def first():
            try:
                with db.write_repo(POOL) as repo:
                    events.append("first in")
                    first_inside.set()
                    time.sleep(1.0)
                    repo.set_closed()
                    events.append("first out")
            except BaseException as error:  # 스레드의 오류를 테스트로 넘긴다
                errors.append(error)
                first_inside.set()

        def second():
            try:
                first_inside.wait(30)
                with db.write_repo(POOL) as repo:
                    events.append("second in")
                    events.append(("second sees closed", repo.load_state().closed))
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=first), threading.Thread(target=second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(events, ["first in", "first out", "second in", ("second sees closed", True)])


if __name__ == "__main__":
    unittest.main()
