"""scripts/keep_alive.py 와 .github/workflows/keep-alive.yml 테스트(DB, 네트워크, 깃허브 없이).

연결 함수와 sleep을 가짜로 넣어, 성공/실패 때 출력이 정해진 한 줄뿐인지(주소, 비밀번호, 학생 수, 오류 원문이 없는지),
종료 코드와 재시도 횟수가 맞는지 본다. 실제로 쉬지 않는다.
"""
import ast
import contextlib
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "keep_alive.py"
WORKFLOW = ROOT / ".github" / "workflows" / "keep-alive.yml"

QUERY = "select count(*) from tf_students"
FAKE_PASSWORD = "Sup3rS3cretPw"
FAKE_URL = f"postgresql://postgres.abc:{FAKE_PASSWORD}@db.example.invalid:5432/postgres"
STUDENT_COUNT = 4217  # 출력에 나오면 안 되는 숫자


def load_module():
    """teamapp 없이 스크립트 파일만 불러온다(실행기에서와 같은 조건)."""
    spec = importlib.util.spec_from_file_location("keep_alive_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.closed = False

    def execute(self, sql, *args):
        self.conn.executed.append(sql)
        if self.conn.query_error is not None:
            raise self.conn.query_error

    def fetchone(self):
        return (STUDENT_COUNT,)

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, query_error=None):
        self.executed = []
        self.query_error = query_error
        self.closed = False
        self.cursors = []

    def cursor(self):
        cursor = FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def close(self):
        self.closed = True


class FakeConnect:
    """psycopg2.connect 흉내. 호출마다 동작을 하나씩 꺼내 쓴다(예외 객체면 던지고, 아니면 연결을 돌려줌)."""

    def __init__(self, *behaviours):
        self.behaviours = list(behaviours)
        self.calls = []
        self.connections = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        behaviour = self.behaviours.pop(0) if self.behaviours else FakeConnection()
        if isinstance(behaviour, BaseException):
            raise behaviour
        self.connections.append(behaviour)
        return behaviour


class FakeSleep:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


def run_main(module, connect, env, sleep=None):
    """main을 돌리고 (종료 코드, 표준 출력, 표준 오류, sleep 기록)을 돌려준다."""
    sleep = sleep or FakeSleep()
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env, clear=True):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = module.main(connect=connect, sleep=sleep)
    return code, out.getvalue(), err.getvalue(), sleep.calls


class KeepAliveMainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_success_prints_only_success_line(self):
        connect = FakeConnect()
        code, out, err, sleeps = run_main(self.module, connect, {"DATABASE_URL": FAKE_URL})
        self.assertEqual(code, 0)
        self.assertEqual(out, "DB 요청 성공\n")
        self.assertEqual(err, "")
        self.assertEqual(sleeps, [])
        self.assertEqual(len(connect.calls), 1)
        conn = connect.connections[0]
        self.assertEqual(conn.executed, [QUERY])
        self.assertTrue(conn.closed)
        self.assertNotIn(str(STUDENT_COUNT), out)
        self.assertNotIn(FAKE_PASSWORD, out + err)

    def test_connects_with_timeout_and_application_name(self):
        connect = FakeConnect()
        run_main(self.module, connect, {"DATABASE_URL": FAKE_URL})
        args, kwargs = connect.calls[0]
        self.assertEqual(args, (FAKE_URL,))
        self.assertEqual(kwargs, {"connect_timeout": 15, "application_name": "team-formation-keepalive"})

    def test_surrounding_whitespace_in_secret_is_ignored(self):
        connect = FakeConnect()
        code, out, _err, _sleeps = run_main(self.module, connect, {"DATABASE_URL": f"  {FAKE_URL}\n"})
        self.assertEqual((code, out), (0, "DB 요청 성공\n"))
        self.assertEqual(connect.calls[0][0], (FAKE_URL,))

    def test_missing_url_fails_without_connecting(self):
        for env in ({}, {"DATABASE_URL": ""}, {"DATABASE_URL": "   "}):
            with self.subTest(env=env):
                connect = FakeConnect()
                code, out, err, sleeps = run_main(self.module, connect, env)
                self.assertEqual(code, 1)
                self.assertEqual(out, "DB 요청 실패\n")
                self.assertEqual(err, "")
                self.assertEqual(connect.calls, [])
                self.assertEqual(sleeps, [])

    def test_connect_error_leaks_nothing_and_retries_once(self):
        error_text = f"could not connect to {FAKE_URL}: password authentication failed for {FAKE_PASSWORD}"
        connect = FakeConnect(RuntimeError(error_text), RuntimeError(error_text))
        code, out, err, sleeps = run_main(self.module, connect, {"DATABASE_URL": FAKE_URL})
        self.assertEqual(code, 1)
        self.assertEqual(out, "DB 요청 실패\n")
        self.assertEqual(err, "")
        self.assertNotIn(FAKE_PASSWORD, out + err)
        self.assertNotIn("example.invalid", out + err)
        self.assertEqual(len(connect.calls), 2)
        self.assertEqual(sleeps, [10])

    def test_first_failure_then_success(self):
        connect = FakeConnect(OSError(f"timeout {FAKE_URL}"), FakeConnection())
        code, out, err, sleeps = run_main(self.module, connect, {"DATABASE_URL": FAKE_URL})
        self.assertEqual(code, 0)
        self.assertEqual(out, "DB 요청 성공\n")
        self.assertEqual(err, "")
        self.assertEqual(len(connect.calls), 2)
        self.assertEqual(sleeps, [10])
        self.assertEqual(connect.connections[0].executed, [QUERY])

    def test_query_error_closes_connection_and_retries_once(self):
        first = FakeConnection(query_error=RuntimeError(f"relation of {FAKE_URL} missing"))
        second = FakeConnection(query_error=RuntimeError(f"again {FAKE_PASSWORD}"))
        connect = FakeConnect(first, second)
        code, out, err, sleeps = run_main(self.module, connect, {"DATABASE_URL": FAKE_URL})
        self.assertEqual((code, out, err), (1, "DB 요청 실패\n", ""))
        self.assertEqual(len(connect.calls), 2)
        self.assertEqual(sleeps, [10])
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)

    def test_does_not_retry_more_than_once(self):
        connect = FakeConnect(*[RuntimeError("x")] * 5)
        run_main(self.module, connect, {"DATABASE_URL": FAKE_URL})
        self.assertEqual(len(connect.calls), 2)

    def test_default_connect_without_psycopg2_still_fails_cleanly(self):
        # 실행기에 psycopg2가 없거나 불러오지 못해도 추적 메시지 없이 같은 실패 한 줄만 낸다.
        out, err = io.StringIO(), io.StringIO()
        sleep = FakeSleep()
        with mock.patch.dict(os.environ, {"DATABASE_URL": FAKE_URL}, clear=True):
            with mock.patch.dict(sys.modules, {"psycopg2": None}):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = self.module.main(sleep=sleep)
        self.assertEqual((code, out.getvalue(), err.getvalue()), (1, "DB 요청 실패\n", ""))
        self.assertEqual(sleep.calls, [])  # 설치 문제는 다시 해도 소용없으므로 쉬지 않는다

    def test_default_connect_is_psycopg2_connect(self):
        fake_psycopg2 = mock.Mock()
        fake_psycopg2.connect.return_value = FakeConnection()
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"DATABASE_URL": FAKE_URL}, clear=True):
            with mock.patch.dict(sys.modules, {"psycopg2": fake_psycopg2}):
                with contextlib.redirect_stdout(out):
                    code = self.module.main(sleep=FakeSleep())
        self.assertEqual((code, out.getvalue()), (0, "DB 요청 성공\n"))
        fake_psycopg2.connect.assert_called_once_with(
            FAKE_URL, connect_timeout=15, application_name="team-formation-keepalive"
        )


class KeepAliveScriptTest(unittest.TestCase):
    def test_script_does_not_import_teamapp_or_read_dotenv(self):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("teamapp", imported)
        self.assertNotIn("dotenv", imported)
        self.assertNotIn("streamlit", imported)

    def test_module_imports_without_psycopg2(self):
        with mock.patch.dict(sys.modules, {"psycopg2": None}):
            module = load_module()
        self.assertTrue(callable(module.main))

    def test_running_script_without_url_exits_1_with_one_line(self):
        env = {key: value for key, value in os.environ.items() if key != "DATABASE_URL"}
        # Windows(cp949 콘솔)에서도 한글을 UTF-8로 쓰고 읽도록 고정한다. -I는 PYTHON* 환경 변수를 무시하므로
        # 실제로 효과가 있는 것은 -X utf8이고, 환경 변수는 -I를 빼더라도 같은 결과가 나오게 하는 이중 안전장치다.
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        with tempfile.TemporaryDirectory() as other_dir:  # 저장소 밖에서도(실행기와 비슷하게) 돌아야 한다
            result = subprocess.run(
                [sys.executable, "-I", "-X", "utf8", str(SCRIPT)],
                cwd=other_dir,
                env=env,
                capture_output=True,
                timeout=60,
            )
        # Windows는 파이프로 나가는 줄바꿈을 \r\n으로 바꾸므로 \r\n만 \n으로 맞춘 뒤 정확히 비교한다
        # (줄 수나 빈 줄이 더 있으면 여전히 실패한다).
        stdout = result.stdout.decode("utf-8").replace("\r\n", "\n")
        stderr = result.stderr.decode("utf-8").replace("\r\n", "\n")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(stdout, "DB 요청 실패\n")
        self.assertEqual(stderr, "")


class KeepAliveWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.lines = [line.rstrip() for line in cls.text.splitlines()]

    def assertHasLine(self, line):
        self.assertIn(line, self.lines)

    def test_name_and_triggers(self):
        self.assertHasLine("name: keep-alive")
        self.assertHasLine("on:")
        self.assertHasLine("  schedule:")
        self.assertHasLine('    - cron: "17 0 * * *"')
        self.assertHasLine("  workflow_dispatch:")

    def test_only_one_cron(self):
        self.assertEqual(len(re.findall(r"^\s*-\s*cron:", self.text, flags=re.MULTILINE)), 1)

    def test_read_only_permissions_and_timeout(self):
        self.assertHasLine("permissions:")
        self.assertHasLine("  contents: read")
        self.assertHasLine("    runs-on: ubuntu-latest")
        self.assertHasLine("    timeout-minutes: 5")

    def test_steps(self):
        self.assertHasLine("      - uses: actions/checkout@v4")
        self.assertHasLine("      - uses: actions/setup-python@v5")
        self.assertHasLine('          python-version: "3.13"')
        self.assertHasLine("      - run: pip install psycopg2-binary")
        self.assertHasLine("      - run: python scripts/keep_alive.py")

    def test_only_database_url_secret_is_used(self):
        self.assertHasLine("          DATABASE_URL: ${{ secrets.DATABASE_URL }}")
        self.assertEqual(set(re.findall(r"secrets\.(\w+)", self.text)), {"DATABASE_URL"})
        self.assertEqual(self.text.count("secrets."), 1)

    def test_secret_is_given_only_to_the_script_step(self):
        # env는 스크립트를 부르는 단계에만 붙어 있어야 한다(다른 단계나 job 전체에 두지 않음).
        run_index = self.lines.index("      - run: python scripts/keep_alive.py")
        env_lines = [i for i, line in enumerate(self.lines) if line.strip() == "env:"]
        self.assertEqual(env_lines, [run_index + 1])
        self.assertEqual(self.lines[run_index + 2], "          DATABASE_URL: ${{ secrets.DATABASE_URL }}")

    def test_does_not_echo_or_dump_environment(self):
        for word in ("echo", "printenv", "set -x", "env |", "toJSON", "::debug"):
            self.assertNotIn(word, self.text)
        self.assertNotRegex(self.text, r"(?m)^\s*-?\s*run:\s*env\s*$")


if __name__ == "__main__":
    unittest.main()
