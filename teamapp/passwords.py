"""비밀번호 해시: bcrypt(rounds=10).

bcrypt는 함수 안에서 불러온다. 설치되어 있지 않은 환경에서도 이 모듈을 쓰는 다른 코드와 테스트가 돌도록 하기 위해서이다.
"""
from __future__ import annotations

BCRYPT_ROUNDS = 10


def hash_password(password: str) -> str:
    """비밀번호를 bcrypt로 해시한다. bcrypt가 거부하는 비밀번호(72바이트 초과 등)는 ValueError가 그대로 난다."""
    import bcrypt

    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """비밀번호가 해시와 맞는지. 해시 모양이 틀리거나 bcrypt가 거부하는 비밀번호면 오류 대신 False."""
    import bcrypt

    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError, AttributeError):
        return False
