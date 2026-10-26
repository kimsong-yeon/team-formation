"""비밀 값 읽기(teamapp/config.py)와 scripts/check_env.py 테스트.

값은 돌려주기만 하고, 확인용 출력에는 "있음"/"없음"만 나오는지 본다. 실제 .env와 Streamlit은 쓰지 않는다.
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from teamapp import config

ROOT = Path(__file__).resolve().parent.parent


class FakeSecrets:
    """st.secrets 흉내. 읽은 key를 기록한다."""

    def __init__(self, values, toml_exists=True, error=None):
        self.values = values
        self.toml_exists = toml_exists
        self.error = error
        self.read = []

    def load_if_toml_exists(self):
        return self.toml_exists

    def __getitem__(self, name):
        self.read.append(name)
        if self.error is not None:
            raise self.error
        return self.values[name]


def fake_streamlit(secrets, running=True):
    st = types.ModuleType("streamlit")
    st.secrets = secrets
    st.runtime = types.SimpleNamespace(exists=lambda: running)
    return st


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env_file = Path(tmp.name) / ".env"
        for patcher in (
            mock.patch.object(config, "ENV_FILE", self.env_file),
            mock.patch.object(config, "_dotenv_loaded", False),
            mock.patch.dict(os.environ),
            mock.patch.dict(sys.modules, {"streamlit": None}),  # Streamlit으로 실행 중이 아님
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in config.SECRET_NAMES:
            os.environ.pop(name, None)

    def use_streamlit(self, secrets, running=True):
        patcher = mock.patch.dict(sys.modules, {"streamlit": fake_streamlit(secrets, running)})
        patcher.start()
        self.addCleanup(patcher.stop)


class GetSecretTests(ConfigTestCase):
    def test_environment_variable_is_read_and_blank_is_missing(self):
        os.environ["DATABASE_URL"] = "  postgresql://u:p@h:5432/db  "
        os.environ["OPENROUTER_API_KEY"] = "   "
        self.assertEqual(config.get_secret("DATABASE_URL"), "postgresql://u:p@h:5432/db")
        self.assertIsNone(config.get_secret("OPENROUTER_API_KEY"))
        self.assertIsNone(config.get_secret("ADMIN_PASSWORD"))

    def test_dotenv_with_bom_is_read_literally_and_environment_wins(self):
        self.env_file.write_text(
            "DATABASE_URL=postgresql://file\nOPENROUTER_API_KEY=sk-${HOME}\nADMIN_PASSWORD=FAKE-TEST-ONLY-FILE\n",
            encoding="utf-8-sig",  # 메모장의 BOM
        )
        os.environ["ADMIN_PASSWORD"] = "FAKE-TEST-ONLY-ENV"
        self.assertEqual(config.get_secret("DATABASE_URL"), "postgresql://file")
        self.assertEqual(config.get_secret("OPENROUTER_API_KEY"), "sk-${HOME}")
        self.assertEqual(config.get_secret("ADMIN_PASSWORD"), "FAKE-TEST-ONLY-ENV")

    def test_dotenv_is_loaded_once(self):
        with mock.patch("dotenv.load_dotenv") as load:
            for _ in range(3):
                config.get_secret("DATABASE_URL")
        load.assert_called_once_with(self.env_file, override=False, interpolate=False, encoding="utf-8-sig")

    def test_missing_dotenv_file_is_fine(self):
        self.assertFalse(self.env_file.exists())
        self.assertIsNone(config.get_secret("DATABASE_URL"))


class StreamlitSecretsTests(ConfigTestCase):
    def test_streamlit_secrets_come_first(self):
        os.environ["DATABASE_URL"] = "from-env"
        self.use_streamlit(FakeSecrets({"DATABASE_URL": "from-secrets", "ADMIN_PASSWORD": 1234}))
        self.assertEqual(config.get_secret("DATABASE_URL"), "from-secrets")
        self.assertEqual(config.get_secret("ADMIN_PASSWORD"), "1234")  # TOML에 따옴표 없이 쓴 숫자

    def test_falls_back_to_environment_without_printing(self):
        os.environ["DATABASE_URL"] = "from-env"
        cases = {
            "secrets에 없음": (FakeSecrets({}), True),
            "빈 값": (FakeSecrets({"DATABASE_URL": "  "}), True),
            "secrets.toml 없음": (FakeSecrets({"DATABASE_URL": "x"}, toml_exists=False), True),
            "읽기 오류": (FakeSecrets({}, error=RuntimeError("boom")), True),
            "Streamlit으로 실행 중이 아님": (FakeSecrets({"DATABASE_URL": "x"}), False),
        }
        for name, (secrets, running) in cases.items():
            with self.subTest(name):
                self.use_streamlit(secrets, running)
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    self.assertEqual(config.get_secret("DATABASE_URL"), "from-env")
                self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
                if name in ("secrets.toml 없음", "Streamlit으로 실행 중이 아님"):
                    self.assertEqual(secrets.read, [])  # st.secrets[...]를 건드리지 않는다


class SecretStatusTests(ConfigTestCase):
    def test_status_has_only_presence(self):
        os.environ["DATABASE_URL"] = "postgresql://user:FAKEPW@db.example.invalid/db"
        os.environ["ADMIN_PASSWORD"] = "FAKE-TEST-ONLY"
        status = config.secret_status()
        self.assertEqual(
            status, {"DATABASE_URL": "있음", "OPENROUTER_API_KEY": "없음", "ADMIN_PASSWORD": "있음"}
        )
        self.assertNotIn("FAKEPW", repr(status))
        self.assertNotIn("FAKE-TEST-ONLY", repr(status))

    def test_check_env_script_prints_one_line_per_name(self):
        # 저장소 밖 임시 폴더에 같은 구조로 복사해 실행한다(실제 .env를 읽지 않도록).
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "teamapp").mkdir()
        (tmp / "scripts").mkdir()
        shutil.copy(ROOT / "teamapp" / "__init__.py", tmp / "teamapp" / "__init__.py")
        shutil.copy(ROOT / "teamapp" / "config.py", tmp / "teamapp" / "config.py")
        shutil.copy(ROOT / "scripts" / "check_env.py", tmp / "scripts" / "check_env.py")
        (tmp / ".env").write_text(
            "DATABASE_URL=postgresql://user:FAKEPW@db.example.invalid/db\nADMIN_PASSWORD=FAKE-TEST-ONLY\n",
            encoding="utf-8",
        )
        env = {k: v for k, v in os.environ.items() if k not in config.SECRET_NAMES and k != "PYTHONPATH"}
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, str(tmp / "scripts" / "check_env.py")],
            cwd=tmp,
            env=env,
            capture_output=True,
            timeout=60,
        )
        stdout = result.stdout.decode("utf-8")
        stderr = result.stderr.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, stderr)
        self.assertEqual(
            stdout.splitlines(), ["DATABASE_URL: 있음", "OPENROUTER_API_KEY: 없음", "ADMIN_PASSWORD: 있음"]
        )
        self.assertNotIn("FAKEPW", stdout + stderr)
        self.assertNotIn("FAKE-TEST-ONLY", stdout + stderr)


if __name__ == "__main__":
    unittest.main()
