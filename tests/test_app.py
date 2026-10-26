"""Streamlit 화면 테스트(AppTest, 실제 DB).

- 화면 요소는 채점 프로그램처럼 Streamlit key로만 찾는다(PRD 5.4절 표).
- TextHelperTests(글 만들기 함수)는 streamlit과 psycopg2만 있으면 돌고, 없으면 건너뛴다.
- ScreenTests는 bcrypt와 .env(또는 환경 변수)의 DATABASE_URL, ADMIN_PASSWORD도 있어야 하고, 없으면 건너뛴다.
- 주의: test_db처럼 DATABASE_URL이 가리키는 DB의 tf_ 데이터(관리자를 뺀 학생, 팀, 팀원, 지원, 투표, 마감 표시)를
  지우고 명단을 다시 올린다. 테스트하는 동안 관리자 비밀번호 해시를 로컬 ADMIN_PASSWORD에 맞추고, 끝나면 테스트 전
  해시로 되돌린다(해시는 출력하지 않음). 끝나면 쉬움 명단(팀 3개, 학생 비밀번호 1234)으로 처음 상태를 만들어 둔다.
  그래서 배포한 앱으로 시험하는 도중에는 돌리지 않는다.
- 실행(수강생 컴퓨터): .venv\\Scripts\\python -m unittest tests.test_app -v
"""
import gc
import unittest
from datetime import datetime, timezone
from pathlib import Path

from teamapp import actions, config
from teamapp.passwords import hash_password
from teamapp.roster import ADMIN_NO
from teamapp.rules import APPROVED, State, Student
from tests.scenarios import DATA_DIR

try:
    import streamlit
    from streamlit.testing.v1 import AppTest
except ImportError:
    streamlit = None
    AppTest = None

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

APP_PATH = Path(__file__).resolve().parent.parent / "streamlit_app.py"
RUN_TIMEOUT = 30  # AppTest 실행 한 번의 제한(초). 처음 실행은 연결 풀과 테이블 확인까지 한다
STUDENT_PASSWORD = "1234"
HAS_APP_MODULES = AppTest is not None and psycopg2 is not None

ADMIN_KEYS = (
    "whoami",
    "result",
    "admin_roster_file",
    "admin_team_count",
    "admin_start",
    "admin_close",
    "unassigned",
    "admin_assign",
    "assign_status",
    "team_1",
    "team_2",
    "team_3",
)


# ---- key로 화면 요소 찾기 ----


def walk(node):
    """node와 그 안의 모든 요소를 화면 순서대로 내준다(컨테이너는 children을 가진다)."""
    yield node
    children = getattr(node, "children", None)
    if isinstance(children, dict):
        for index in sorted(children):
            yield from walk(children[index])


def user_key(node):
    """요소에 붙인 Streamlit key. 위젯은 .key, 컨테이너와 그 밖의 요소는 proto.id("$$ID-해시-key")에서 꺼낸다."""
    key = getattr(node, "key", None)
    if isinstance(key, str) and key:
        return key
    element_id = getattr(getattr(node, "proto", None), "id", None)
    if isinstance(element_id, str) and element_id:
        parts = element_id.split("-", 2)
        return parts[2] if len(parts) == 3 else element_id
    return None


def find_key(at, key):
    """key가 붙은 요소(위젯이나 컨테이너). 없으면 None."""
    for root in (at.main, at.sidebar):
        for node in walk(root):
            if user_key(node) == key:
                return node
    return None


def text_in(node):
    """요소 안에 보이는 글(마크다운, 안내 상자, 캡션 등)을 줄바꿈으로 이어 붙인다."""
    texts = []
    for item in walk(node):
        if isinstance(getattr(item, "children", None), dict):
            continue
        try:
            value = item.value
        except Exception:
            continue
        if isinstance(value, str):
            texts.append(value)
    return "\n".join(texts)


# ---- DB 준비 ----


def reset_roster(pool, csv_name, team_count):
    """관리자로 명단을 올려 처음 상태를 만든다(학생 비밀번호 1234를 bcrypt로 해시)."""
    data = (DATA_DIR / csv_name).read_bytes()
    with db.write_repo(pool) as repo:
        admin = repo.find_account(ADMIN_NO)
        outcome = actions.reset_roster(repo, admin.id, data, team_count, hash_password)
    if outcome.kind != APPROVED:
        raise AssertionError(outcome.text)


def admin_hash(pool):
    with db.read_repo(pool) as repo:
        account = repo.find_account(ADMIN_NO)
    return account.password_hash if account is not None else None


def restore_admin_hash(pool, password_hash):
    with db.connection(pool) as conn:
        with conn.cursor() as cur:
            cur.execute("update tf_students set password_hash = %s where student_no = %s", (password_hash, ADMIN_NO))
        conn.commit()


def release_app_pool():
    """AppTest로 돌린 앱이 st.cache_resource에 만든 연결 풀을 놓아 준다(연결은 쓰레기 수거 때 닫힌다)."""
    try:
        streamlit.cache_resource.clear()
    except Exception:
        pass
    gc.collect()


@unittest.skipUnless(HAS_APP_MODULES, "streamlit이나 psycopg2가 설치되어 있지 않음")
class TextHelperTests(unittest.TestCase):
    """streamlit_app.py의 글 만들기 함수(DB 없이)."""

    @classmethod
    def setUpClass(cls):
        import streamlit_app

        cls.app = streamlit_app

    def test_markdown_escape(self):
        self.assertEqual(self.app.md("학생1(PL;RW)"), "학생1(PL;RW)")
        self.assertEqual(self.app.md("a*b_c[d]<e>$f"), "a\\*b\\_c\\[d\\]\\<e\\>\\$f")
        self.assertEqual(self.app.md("줄\n바꿈"), "줄 바꿈")
        self.assertEqual(self.app.md("- 학생"), "\\- 학생")  # 글 맨 앞의 목록 표시
        self.assertEqual(self.app.md("1. 학생"), "1\\. 학생")
        self.assertEqual(self.app.md("학생 1. 끝"), "학생 1. 끝")

    def test_split_result(self):
        self.assertEqual(self.app.split_result("막힘: 팀이 꽉 참"), ("막힘", "팀이 꽉 참"))
        self.assertEqual(self.app.split_result("알 수 없음"), (None, "알 수 없음"))

    def test_korean_time(self):
        utc = datetime(2026, 11, 1, 0, 0, 5, tzinfo=timezone.utc)
        self.assertEqual(self.app.kst_text(utc), "11-01 09:00:05 KST")
        self.assertEqual(self.app.kst_text(utc.replace(tzinfo=None)), "11-01 09:00:05 KST")  # 시간대 없으면 UTC로 봄

    def test_lines(self):
        student = Student(1, "2026101", "학생1(PL;RW)", "기획자", ("보고서 작성자",))
        alone = Student(2, "2026105", "학생5(WP)", "웹 프로그래머")
        self.assertEqual(self.app.member_line(student), "학생1(PL;RW) · 주 역할 기획자 · 보조 역할 보고서 작성자")
        self.assertEqual(self.app.member_line(alone), "학생5(WP) · 주 역할 웹 프로그래머 · 보조 역할 없음")
        self.assertEqual(
            self.app.application_line(alone, datetime(2026, 11, 1, 3, 4, 5, tzinfo=timezone.utc)),
            "학생5(WP) · 학번 2026105 · 주 역할 웹 프로그래머 · 보조 역할 없음 · 11-01 12:04:05 KST",
        )
        self.assertEqual(self.app.team_title("팀1", 2, True), "팀1 (2/4명) · 내 팀")
        self.assertEqual(self.app.team_title("팀2", 0, False), "팀2 (0/4명)")
        self.assertEqual(self.app.unassigned_line([]), "팀이 없는 학생 0명: 없음")
        self.assertEqual(self.app.unassigned_line([student, alone]), "팀이 없는 학생 2명: 학생1(PL;RW), 학생5(WP)")
        self.assertEqual(
            self.app.student_status_line(False, None),
            "모집 상태: 모집 중 · 내 팀: 아직 팀이 없음 · 화면은 3초마다 저절로 바뀜",
        )

    def test_assign_result_text(self):
        result = self.app.ai.AssignResult
        self.assertEqual(
            self.app.assign_result_text(result({1: 2}, "2차 모델(m)", ["줄"], [])),
            "승인: 검사를 통과한 배정안을 만들었습니다 (출처: 2차 모델(m))",
        )
        self.assertEqual(
            self.app.assign_result_text(result(None, "", [], ["배정할 학생이 없습니다"])),
            "막힘: 배정할 학생이 없습니다",
        )
        self.assertEqual(
            self.app.assign_result_text(result(None, "", [], ["팀3에 웹 프로그래머를 맡을 학생이 없음"])),
            "막힘: 규칙을 지키는 배정이 없음",
        )

    def test_plan_rows(self):
        a = Student(1, "2026107", "학생7(FD;PT)", "프론트엔드 디자이너", ("발표자",))
        b = Student(2, "2026108", "학생8(TT;RW;PT)", "테스터", ("보고서 작성자", "발표자"))
        state = State({1: a, 2: b}, (1, 2), {1: (), 2: ()}, (), {}, True)
        self.assertEqual(
            self.app.plan_rows(state, {2: 1, 1: 2}),
            [
                {"이름": "학생8(TT;RW;PT)", "주 역할": "테스터", "보조 역할": "보고서 작성자, 발표자", "배정된 팀": "팀1"},
                {"이름": "학생7(FD;PT)", "주 역할": "프론트엔드 디자이너", "보조 역할": "발표자", "배정된 팀": "팀2"},
            ],
        )

    def test_team_count_value(self):
        self.assertEqual(self.app.team_count_value(3), 3)
        self.assertEqual(self.app.team_count_value(4.0), 4)
        self.assertIsNone(self.app.team_count_value(2.5))
        self.assertIsNone(self.app.team_count_value(True))
        self.assertIsNone(self.app.team_count_value(None))


class ScreenTests(unittest.TestCase):
    """AppTest로 앱 파일을 실제 DB에 붙여 돌린다. 학생·관리자마다 AppTest(브라우저 세션) 하나씩."""

    pool = None
    admin_password = None
    saved_admin_hash = None  # 테스트 전 관리자 비밀번호 해시(관리자가 없었으면 None). 출력하지 않는다

    @classmethod
    def setUpClass(cls):
        if AppTest is None:
            raise unittest.SkipTest("streamlit이 설치되어 있지 않음")
        if psycopg2 is None:
            raise unittest.SkipTest("psycopg2가 설치되어 있지 않음")
        if bcrypt is None:
            raise unittest.SkipTest("bcrypt가 설치되어 있지 않음")
        dsn = config.get_secret("DATABASE_URL")
        if not dsn:
            raise unittest.SkipTest("DATABASE_URL이 없음(.env)")
        cls.admin_password = config.get_secret("ADMIN_PASSWORD")
        if not cls.admin_password:
            raise unittest.SkipTest("ADMIN_PASSWORD가 없음(.env)")
        pool = db.make_pool(dsn)
        try:
            db.ensure_schema(pool, db.read_schema_sql())
            cls.saved_admin_hash = admin_hash(pool)  # ensure_admin이 바꾸기 전에 기억해 둔다
            db.ensure_admin(pool, cls.admin_password)
        except BaseException:
            pool.closeall()
            raise
        cls.pool = pool

    @classmethod
    def tearDownClass(cls):
        pool = cls.pool
        if pool is None:
            return
        try:
            reset_roster(pool, "쉬움.csv", 3)
        finally:
            try:
                if cls.saved_admin_hash is not None:  # 관리자 비밀번호 해시를 테스트 전으로 되돌린다
                    restore_admin_hash(pool, cls.saved_admin_hash)
            finally:
                cls.saved_admin_hash = None
                cls.pool = None
                pool.closeall()
                release_app_pool()

    def setUp(self):
        reset_roster(self.pool, "쉬움.csv", 3)

    # ---- 도우미 ----
    def assert_no_exception(self, at):
        self.assertEqual(len(at.exception), 0, [str(getattr(e, "value", "")) for e in at.exception])

    def open_app(self):
        at = AppTest.from_file(str(APP_PATH), default_timeout=RUN_TIMEOUT)
        at.run()
        self.assert_no_exception(at)
        return at

    def log_in(self, student_no, password):
        at = self.open_app()
        at.text_input(key="login_student_no").input(student_no)
        at.text_input(key="login_password").input(password)
        at.button(key="login_submit").click()
        at.run()
        self.assert_no_exception(at)
        return at

    def click(self, at, key):
        at.button(key=key).click()
        at.run()
        self.assert_no_exception(at)

    def text_of_key(self, at, key):
        node = find_key(at, key)
        self.assertIsNotNone(node, f"key {key}가 화면에 없음")
        return text_in(node)

    def assert_keys(self, at, keys):
        missing = [key for key in keys if find_key(at, key) is None]
        self.assertEqual(missing, [], "화면에 없는 key")

    def assert_no_keys(self, at, keys):
        present = [key for key in keys if find_key(at, key) is not None]
        self.assertEqual(present, [], "화면에 있으면 안 되는 key")

    # ---- 테스트 ----
    def test_login_page_keys(self):
        at = self.open_app()
        self.assert_keys(at, ["login_student_no", "login_password", "login_submit", "result"])
        self.assert_no_keys(at, ["whoami", "team_1", "admin_start"])
        self.assertEqual(self.text_of_key(at, "result"), "단추를 누르면 결과가 여기에 나옴")

    def test_wrong_password_is_blocked(self):
        at = self.log_in("2026101", "0000")
        self.assertTrue(self.text_of_key(at, "result").startswith("막힘"), self.text_of_key(at, "result"))
        self.assert_no_keys(at, ["whoami"])
        self.assert_keys(at, ["login_student_no"])

    def test_admin_keys(self):
        at = self.log_in(ADMIN_NO, self.admin_password)
        self.assert_keys(at, ADMIN_KEYS)
        self.assertEqual(self.text_of_key(at, "whoami"), "로그인: 관리자 (admin)")
        self.assertEqual(self.text_of_key(at, "unassigned").split(":")[0], "팀이 없는 학생 8명")
        self.assert_no_keys(at, ["apply_1", "login_student_no", "admin_confirm", "refresh"])

    def test_student_keys_and_no_admin(self):
        at = self.log_in("2026101", STUDENT_PASSWORD)
        self.assert_keys(at, ["whoami", "result", "team_1", "team_2", "team_3", "apply_1", "apply_2", "apply_3"])
        self.assertEqual(self.text_of_key(at, "whoami"), "로그인: 학생1(PL;RW) (2026101)")
        self.assert_no_keys(
            at,
            ["admin_roster_file", "admin_team_count", "admin_start", "admin_close", "admin_assign", "unassigned", "refresh"],
        )

    def test_vote_buttons_appear_for_member(self):
        student1 = self.log_in("2026101", STUDENT_PASSWORD)
        self.click(student1, "apply_1")
        self.assertTrue(self.text_of_key(student1, "result").startswith("승인"), self.text_of_key(student1, "result"))

        student4 = self.log_in("2026104", STUDENT_PASSWORD)
        self.click(student4, "apply_1")
        self.assertTrue(self.text_of_key(student4, "result").startswith("대기"), self.text_of_key(student4, "result"))
        self.assertTrue(student4.button(key="apply_2").disabled)  # 결과를 기다리는 지원이 있으면 지원 단추를 모두 막는다

        student1.run()  # 3초마다 다시 그리는 것과 같다
        self.assert_no_exception(student1)
        self.assert_keys(student1, ["vote_yes_2026104", "vote_no_2026104"])
        self.click(student1, "vote_yes_2026104")
        self.assertTrue(self.text_of_key(student1, "result").startswith("승인"), self.text_of_key(student1, "result"))
        self.assert_no_keys(student1, ["vote_yes_2026104", "vote_no_2026104"])
        self.assertIn("학생4(WP;FD)", self.text_of_key(student1, "team_1"))

    def test_removed_student_is_logged_out(self):
        student1 = self.log_in("2026101", STUDENT_PASSWORD)
        self.assert_keys(student1, ["whoami"])
        reset_roster(self.pool, "중간.csv", 4)  # 학번 2026101이 없는 명단
        student1.run()
        self.assert_no_exception(student1)
        self.assert_keys(student1, ["login_student_no", "login_password", "login_submit"])
        self.assert_no_keys(student1, ["whoami", "team_1"])


if __name__ == "__main__":
    unittest.main()
