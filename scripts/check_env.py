"""비밀 값이 있는지만 확인한다(값은 출력하지 않음).

실행(저장소 폴더에서): .venv\\Scripts\\python scripts\\check_env.py
출력 예: DATABASE_URL: 있음
"""
import sys
from pathlib import Path

# scripts 폴더에서 바로 실행해도 teamapp을 불러올 수 있도록 저장소 폴더를 경로에 넣는다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teamapp.config import secret_status  # noqa: E402


def main() -> None:
    for name, status in secret_status().items():
        print(f"{name}: {status}")


if __name__ == "__main__":
    main()
