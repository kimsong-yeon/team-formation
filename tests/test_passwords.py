"""비밀번호 해시(teamapp/passwords.py) 테스트.

bcrypt가 없는 작업 공간에서도 돌도록, 가짜 bcrypt 모듈로 호출 방식(rounds=10, UTF-8, 오류 처리)을 확인하고
진짜 bcrypt가 있을 때만 실제 해시도 확인한다.
"""
import sys
import types
import unittest
from unittest import mock

from teamapp import passwords

try:
    import bcrypt
except ImportError:
    bcrypt = None


def fake_bcrypt():
    """호출 기록을 남기는 가짜 bcrypt 모듈."""
    calls = {"gensalt": [], "hashpw": [], "checkpw": []}

    def gensalt(rounds=12, prefix=b"2b"):
        calls["gensalt"].append(rounds)
        return b"$2b$%02d$salt" % rounds

    def hashpw(password, salt):
        calls["hashpw"].append((password, salt))
        return salt + b"|" + password.hex().encode("ascii")  # 진짜 bcrypt처럼 ASCII 해시

    def checkpw(password, hashed):
        calls["checkpw"].append((password, hashed))
        if not hashed.startswith(b"$2b$"):
            raise ValueError("Invalid salt")
        return hashed.endswith(b"|" + password.hex().encode("ascii"))

    module = types.SimpleNamespace(gensalt=gensalt, hashpw=hashpw, checkpw=checkpw)
    return module, calls


class FakeBcryptTests(unittest.TestCase):
    def setUp(self):
        self.fake, self.calls = fake_bcrypt()
        patcher = mock.patch.dict(sys.modules, {"bcrypt": self.fake})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_bcrypt_is_imported_inside_the_functions(self):
        self.assertFalse(hasattr(passwords, "bcrypt"))

    def test_hash_uses_rounds_10_and_utf8_and_returns_str(self):
        hashed = passwords.hash_password("비번1234")
        self.assertEqual(self.calls["gensalt"], [10])
        self.assertEqual(self.calls["hashpw"][0][0], "비번1234".encode("utf-8"))
        self.assertIsInstance(hashed, str)
        self.assertTrue(hashed.startswith("$2b$10$"))

    def test_verify_matches_and_mismatches(self):
        hashed = passwords.hash_password("1234")
        self.assertIs(passwords.verify_password("1234", hashed), True)
        self.assertIs(passwords.verify_password("0000", hashed), False)
        self.assertEqual(self.calls["checkpw"][0], (b"1234", hashed.encode("utf-8")))

    def test_verify_returns_false_for_malformed_hash(self):
        for bad in ("", "h:1234", "not a hash"):
            with self.subTest(bad=bad):
                self.assertIs(passwords.verify_password("1234", bad), False)

    def test_verify_returns_false_when_bcrypt_rejects_the_password(self):
        def too_long(password, hashed):  # bcrypt 5.0.0: 72바이트가 넘는 비밀번호는 ValueError
            raise ValueError("password cannot be longer than 72 bytes")

        self.fake.checkpw = too_long
        self.assertIs(passwords.verify_password("a" * 73, "$2b$10$x"), False)

    def test_hash_lets_errors_through(self):
        def too_long(password, salt):
            raise ValueError("password cannot be longer than 72 bytes")

        self.fake.hashpw = too_long
        with self.assertRaises(ValueError):
            passwords.hash_password("a" * 73)


@unittest.skipIf(bcrypt is None, "bcrypt가 설치되어 있지 않음")
class RealBcryptTests(unittest.TestCase):
    def test_round_trip(self):
        hashed = passwords.hash_password("1234")
        self.assertTrue(hashed.startswith("$2b$10$"))
        self.assertIs(passwords.verify_password("1234", hashed), True)
        self.assertIs(passwords.verify_password("1235", hashed), False)
        self.assertIs(passwords.verify_password("", hashed), False)
        self.assertNotEqual(hashed, passwords.hash_password("1234"))  # 해시마다 소금이 다르다

    def test_malformed_hash_and_long_password_do_not_raise(self):
        for bad in ("", "h:1234", "$2b$10$짧음"):
            with self.subTest(bad=bad):
                self.assertIs(passwords.verify_password("1234", bad), False)
        self.assertIs(passwords.verify_password("a" * 73, passwords.hash_password("1234")), False)


if __name__ == "__main__":
    unittest.main()
