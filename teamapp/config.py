"""비밀 값 읽기: Streamlit Secrets(배포한 앱)에 있으면 그 값, 없으면 .env를 읽은 환경 변수(로컬).

값은 돌려주기만 하고 화면이나 로그에 내보내지 않는다. 있는지 확인할 때는 secret_status()의 "있음"/"없음"만 쓴다.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

SECRET_NAMES = ("DATABASE_URL", "OPENROUTER_API_KEY", "ADMIN_PASSWORD")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

_dotenv_lock = threading.Lock()
_dotenv_loaded = False


def _clean(value: object) -> str | None:
    """문자열(TOML에 따옴표 없이 쓴 정수도)만 받고, 앞뒤 공백을 뺀 뒤 비어 있으면 없는 것으로 본다."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text or None


def _from_streamlit_secrets(name: str) -> str | None:
    """Streamlit으로 실행 중일 때만 st.secrets에서 읽는다. 없거나 어떤 오류가 나도 None(아무것도 출력하지 않음)."""
    st = sys.modules.get("streamlit")
    if st is None:  # streamlit run으로 실행 중이면 이미 불러와 있다
        return None
    try:
        from streamlit import runtime

        if not runtime.exists():
            return None
        secrets = st.secrets
        # secrets.toml이 없을 때 st.secrets[...]가 화면에 오류를 띄우지 않도록 먼저 조용히 읽어 본다.
        if hasattr(type(secrets), "load_if_toml_exists") and not secrets.load_if_toml_exists():
            return None
        return _clean(secrets[name])
    except Exception:
        return None


def _load_dotenv_once() -> None:
    """저장소 폴더의 .env를 한 번만 읽어 환경 변수에 넣는다(이미 있는 환경 변수는 덮어쓰지 않음)."""
    global _dotenv_loaded
    with _dotenv_lock:
        if _dotenv_loaded:
            return
        _dotenv_loaded = True
        try:
            from dotenv import load_dotenv
        except ImportError:  # python-dotenv가 없으면 환경 변수만 본다
            return
        # utf-8-sig: 메모장이 붙이는 BOM이 있어도 첫 이름이 깨지지 않는다. 값은 글자 그대로(${...} 치환 없음).
        load_dotenv(ENV_FILE, override=False, interpolate=False, encoding="utf-8-sig")


def get_secret(name: str) -> str | None:
    """비밀 값. Streamlit Secrets -> .env/환경 변수 순서로 찾고, 없거나 비어 있으면 None."""
    value = _from_streamlit_secrets(name)
    if value is not None:
        return value
    _load_dotenv_once()
    return _clean(os.environ.get(name))


def secret_status() -> dict[str, str]:
    """비밀 값마다 "있음"/"없음"(값은 넣지 않음)."""
    return {name: ("있음" if get_secret(name) else "없음") for name in SECRET_NAMES}
