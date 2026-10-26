"""DB(Supabase 무료 프로젝트)가 일시정지되지 않도록 질의를 한 번 보낸다.

깃허브 Actions(.github/workflows/keep-alive.yml)가 매일 실행한다. 실행기에는 psycopg2-binary만 설치되므로
이 파일은 teamapp 등 앱 코드를 불러오지 않고 표준 라이브러리와 psycopg2만 쓴다.

실행: python scripts/keep_alive.py   (환경 변수 DATABASE_URL 필요)
출력은 다음 두 가지뿐이고, 주소, 비밀번호, 학생 수, 오류 원문은 어떤 경우에도 출력하지 않는다.
  성공: "DB 요청 성공" (종료 코드 0)
  실패: "DB 요청 실패" (종료 코드 1)
"""
import os
import sys
import time

QUERY = "select count(*) from tf_students"
CONNECT_TIMEOUT_SECONDS = 15
APPLICATION_NAME = "team-formation-keepalive"
ATTEMPTS = 2  # 처음 한 번 + 다시 한 번
RETRY_WAIT_SECONDS = 10  # Supavisor가 잠깐 바쁠 수 있어 잠시 쉬었다가 다시 한다

SUCCESS_MESSAGE = "DB 요청 성공"
FAILURE_MESSAGE = "DB 요청 실패"


def _query_once(connect, dsn):
    """연결해서 질의를 한 번 보내고 연결을 닫는다. 결과 값은 쓰지 않고 버린다."""
    conn = connect(dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS, application_name=APPLICATION_NAME)
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(QUERY)
            cursor.fetchone()
        finally:
            cursor.close()
    finally:
        conn.close()


def main(connect=None, sleep=time.sleep) -> int:
    """질의를 보내고 종료 코드를 돌려준다. connect와 sleep은 테스트에서 가짜를 넣기 위한 자리다."""
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print(FAILURE_MESSAGE)
        return 1

    if connect is None:
        try:
            import psycopg2
        except Exception:  # 설치가 안 된 경우: 다시 해도 소용없으므로 바로 실패
            print(FAILURE_MESSAGE)
            return 1
        connect = psycopg2.connect

    for attempt in range(ATTEMPTS):
        if attempt:
            sleep(RETRY_WAIT_SECONDS)
        try:
            _query_once(connect, dsn)
        except Exception:  # psycopg2 오류 문구에는 접속 주소가 섞일 수 있어 내용을 보지 않고 버린다
            continue
        print(SUCCESS_MESSAGE)
        return 0

    print(FAILURE_MESSAGE)
    return 1


if __name__ == "__main__":
    sys.exit(main())
