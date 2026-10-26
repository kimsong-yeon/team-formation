"""PostgreSQL(Supabase) 저장소: 연결 풀, 테이블 만들기, 관리자 계정, PgRepo, 쓰기 잠금.

- 연결 풀(make_pool)은 앱 프로세스에서 한 번만 만들어 다시 쓴다. 질의마다 연결을 새로 만들지 않는다.
- connection(pool): 풀에서 연결을 꺼내 `select 1`로 살아 있는지 확인한다. 끊겼으면 버리고 새 연결을 한 번 받는다.
  다 쓰면 끝나지 않은 트랜잭션을 되돌리고 풀에 돌려준다.
- write_repo(pool): 쓰기(명단 올리기, 지원, 투표, 마감, 확정) 한 번이 트랜잭션 하나이다. 시작하자마자
  pg_advisory_xact_lock(LOCK_KEY)로 잠가 한 번에 하나씩 처리하고, 성공하면 commit, 예외면 rollback한다.
- read_repo(pool): 읽기 전용 트랜잭션. 화면을 그릴 상태는 load_state 질의 한 번으로 읽는다.
- 비밀 값(DATABASE_URL, 비밀번호)은 출력하지도, 오류 문구에 넣지도 않는다.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

import psycopg2
import psycopg2.extensions
import psycopg2.pool

from teamapp.actions import Account, Repo
from teamapp.passwords import hash_password, verify_password
from teamapp.roster import ADMIN_NO, RosterRow
from teamapp.rules import PENDING, ROLES, TEAM_MAX, Application, State, Student

LOCK_KEY = 7240001  # 쓰기를 한 번에 하나씩 처리하는 잠금 번호(pg_advisory_xact_lock)
LOCK_TIMEOUT = "15s"  # 잠금을 이보다 오래 기다리면 그 쓰기는 오류로 끝난다(멈춘 쓰기가 모두를 막지 않도록)
POOL_MIN = 1  # 시작할 때 미리 여는 연결 수
POOL_MAX = 5  # Supabase 무료 플랜 Session pooler의 서버 연결(약 15개)을 PC 테스트, GitHub Actions와 나눠 씀
POOL_WAIT_SECONDS = 5.0  # 풀의 연결이 모두 쓰이는 중이면 이만큼 기다린다
POOL_RETRY_SLEEP = 0.05
ADMIN_NAME = "관리자"
CLOSED_KEY = "closed"  # tf_settings에 (closed, true)가 있으면 모집 마감
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"

T = TypeVar("T")

# 연결 실패 원문에는 DATABASE_URL 조각(비밀번호가 섞일 수 있음)이 들어갈 수 있어, 종류만 골라 일반 문구로 바꾼다.
# 앞에서부터 처음 맞는 것을 쓴다. 맞는 것이 없으면 "연결 실패".
_CONNECT_FAILURES = (
    (
        "주소/포트 오류",
        (
            "could not translate host name",
            "name or service not known",
            "nodename nor servname",
            "no such host",
            "unknown host",
            "could not parse network address",
            "invalid port",
            "invalid integer value",
        ),
    ),
    ("시간 초과", ("timeout expired", "timed out")),
    (
        "인증 실패",
        ("authentication failed", "no password supplied", "tenant or user not found", "sasl", "password"),
    ),
)


def read_schema_sql() -> str:
    """저장소의 schema.sql을 읽는다(Windows에서도 UTF-8로, BOM이 있어도 됨)."""
    return SCHEMA_PATH.read_text(encoding="utf-8-sig")


# ---- 연결 풀과 연결 ----


class ReusingConnectionPool(psycopg2.pool.ThreadedConnectionPool):
    """돌려받은 연결을 maxconn개까지 쉬게 두었다가 다시 쓰는 연결 풀.

    psycopg2의 기본 풀은 쉬는 연결이 minconn개가 되면 그 뒤로 돌려받는 연결을 닫는다. 그러면 화면 여러 개가 겹칠 때
    거의 매번 새로 접속(TCP, TLS, 인증)하게 된다. 시작할 때 미리 여는 연결은 minconn개 그대로이다.
    close=True로 돌려받거나, 이미 닫혔거나, 서버 연결이 끊긴 연결은 닫아서 버린다.
    """

    def _putconn(self, conn, key=None, close=False):
        # ThreadedConnectionPool.putconn이 잠금을 잡은 채로 부른다.
        if self.closed:
            raise psycopg2.pool.PoolError("connection pool is closed")
        if key is None:
            key = self._rused.get(id(conn))
            if key is None:
                raise psycopg2.pool.PoolError("trying to put unkeyed connection")
        keep = not close and not conn.closed and len(self._pool) < self.maxconn
        if keep:
            status = conn.info.transaction_status
            if status == psycopg2.extensions.TRANSACTION_STATUS_UNKNOWN:  # 서버 연결이 끊김
                keep = False
            elif status != psycopg2.extensions.TRANSACTION_STATUS_IDLE:  # 트랜잭션 중이거나 오류 상태
                try:
                    conn.rollback()
                except psycopg2.Error:
                    keep = False
        if keep:
            self._pool.append(conn)
        else:
            conn.close()
        del self._used[key]
        del self._rused[id(conn)]

    def close_idle(self) -> None:
        """쉬고 있는 연결을 모두 닫는다(하나가 끊겨 있으면 같이 쉬던 연결도 끊겼을 수 있음)."""
        with self._lock:
            idle, self._pool = self._pool, []
        for conn in idle:
            conn.close()


def _connect_failure(error: Exception) -> str:
    text = str(error).lower()
    for reason, needles in _CONNECT_FAILURES:
        if any(needle in text for needle in needles):
            return reason
    return "연결 실패"


def _opening(open_connection: Callable[[], T]) -> T:
    """새 연결을 여는 일(풀 만들기, 풀에서 꺼내기)을 하고, 실패하면 원문 없이 종류만 담은 오류로 바꾼다.

    새 오류는 except 블록 밖에서 내므로 원래 오류(문구)가 __context__로도 따라가지 않는다.
    """
    try:
        return open_connection()
    except psycopg2.ProgrammingError:  # invalid dsn: libpq가 주소 전체를 문구에 넣기도 한다
        reason = None
    except psycopg2.OperationalError as error:
        reason = _connect_failure(error)
    if reason is None:
        raise ValueError("DATABASE_URL 형식이 맞지 않습니다") from None
    raise psycopg2.OperationalError(f"DB에 연결하지 못했습니다 ({reason})") from None


def make_pool(dsn: str) -> ReusingConnectionPool:
    """연결 풀을 만든다. 연결 1개를 바로 열므로 접속 정보가 틀리면 여기서 (원문 없는) 오류가 난다."""
    return _opening(
        lambda: ReusingConnectionPool(
            minconn=POOL_MIN,
            maxconn=POOL_MAX,
            dsn=dsn,
            connect_timeout=10,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
            application_name="team-formation",
        )
    )


def _getconn(pool: ReusingConnectionPool) -> psycopg2.extensions.connection:
    """풀에서 연결을 꺼낸다. 모두 쓰이는 중이면(PoolError) 잠깐씩 쉬며 POOL_WAIT_SECONDS까지 다시 해 본다."""
    deadline = time.monotonic() + POOL_WAIT_SECONDS
    while True:
        try:
            return _opening(pool.getconn)
        except psycopg2.pool.PoolError:
            if pool.closed or time.monotonic() >= deadline:
                raise
            time.sleep(POOL_RETRY_SLEEP)


def _is_alive(conn: psycopg2.extensions.connection) -> bool:
    """닫히지 않았고 `select 1`에 답하면 살아 있다.

    확인 질의는 autocommit으로 보내 트랜잭션 없이 한 번만 오가게 하고, 끝나면 autocommit을 끈다.
    끊긴 연결은 여기서 오류가 나며(psycopg2가 closed를 0이 아닌 값으로 바꿈) False가 된다.
    """
    if conn.closed:
        return False
    try:
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute("select 1")
                cur.fetchone()
        finally:
            conn.autocommit = False
    except psycopg2.Error:
        return False
    return True


def _discard(pool: ReusingConnectionPool, conn: psycopg2.extensions.connection) -> None:
    """연결을 닫고 풀에서 뺀다."""
    try:
        pool.putconn(conn, close=True)
    except psycopg2.pool.PoolError:  # 풀이 이미 닫힘
        conn.close()


def _checkin(pool: ReusingConnectionPool, conn: psycopg2.extensions.connection) -> None:
    """끝나지 않은 트랜잭션을 되돌리고 연결을 풀에 돌려준다(풀이 쉬게 두었다가 다시 씀). 끊긴 연결은 닫아서 뺀다."""
    broken = bool(conn.closed)
    if not broken:
        try:
            conn.rollback()  # 트랜잭션이 없으면 아무것도 보내지 않는다
        except psycopg2.Error:
            broken = True
    if broken:
        _discard(pool, conn)
        return
    try:
        pool.putconn(conn)
    except psycopg2.pool.PoolError:
        conn.close()


def _checkout(pool: ReusingConnectionPool) -> psycopg2.extensions.connection:
    """살아 있는 연결을 꺼낸다.

    꺼낸 연결이 끊겼으면 버리고, 같이 쉬던 연결도 모두 닫은 뒤(함께 끊겼을 수 있음) 새 연결을 한 번만 더 받는다.
    """
    for attempt in range(2):
        conn = _getconn(pool)
        try:
            alive = _is_alive(conn)
        except BaseException:
            _discard(pool, conn)
            raise
        if alive:
            return conn
        _discard(pool, conn)
        if attempt == 0:
            pool.close_idle()
    raise psycopg2.OperationalError("DB에 다시 연결했지만 응답이 없습니다")


@contextmanager
def connection(pool: ReusingConnectionPool) -> Iterator[psycopg2.extensions.connection]:
    """살아 있는 연결을 빌려 주고, 다 쓰면(오류가 나도) 되돌리고 풀에 돌려준다."""
    conn = _checkout(pool)
    try:
        yield conn
    finally:
        _checkin(pool, conn)


def _rollback_quietly(conn: psycopg2.extensions.connection) -> None:
    try:
        conn.rollback()
    except psycopg2.Error:  # 끊긴 연결: 서버가 트랜잭션을 이미 버렸다
        pass


@contextmanager
def _locked_cursor(pool: ReusingConnectionPool) -> Iterator[psycopg2.extensions.cursor]:
    """쓰기 트랜잭션 하나. 잠금부터 걸고, 끝까지 성공하면 commit, 예외면 rollback하고 예외를 그대로 낸다.

    블록 안에서 DB 오류를 잡고 넘어가 트랜잭션이 오류 상태로 끝나면, commit(실제로는 조용한 rollback)하지 않고
    rollback한 뒤 오류를 낸다. 성공처럼 보이면 안 되기 때문이다.
    """
    with connection(pool) as conn:
        try:
            with conn.cursor() as cur:
                # psycopg2는 첫 질의 앞에 BEGIN을 보낸다. 잠금 시간 제한(이 트랜잭션에만)과 잠금을 한 번에 보낸다.
                # 잠금은 commit/rollback 때 저절로 풀린다.
                cur.execute(
                    "set local lock_timeout = %s; select pg_advisory_xact_lock(%s)", (LOCK_TIMEOUT, LOCK_KEY)
                )
                yield cur
            if conn.info.transaction_status == psycopg2.extensions.TRANSACTION_STATUS_INERROR:
                raise psycopg2.InternalError("쓰기 도중 DB 오류가 있어 트랜잭션을 되돌렸습니다")
            conn.commit()
        except BaseException:
            _rollback_quietly(conn)
            raise


@contextmanager
def write_repo(pool: ReusingConnectionPool) -> Iterator[PgRepo]:
    """쓰기용 저장소. 블록 하나가 잠긴 트랜잭션 하나이다."""
    with _locked_cursor(pool) as cur:
        yield PgRepo(cur)


@contextmanager
def read_repo(pool: ReusingConnectionPool) -> Iterator[PgRepo]:
    """읽기 전용 저장소. 쓰기 함수를 부르면 DB가 오류를 낸다. 끝나면 되돌린다."""
    with connection(pool) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("set transaction read only")
                yield PgRepo(cur)
        finally:
            _rollback_quietly(conn)


# ---- 테이블과 관리자 계정 ----


def ensure_schema(pool: ReusingConnectionPool, sql: str) -> None:
    """schema.sql을 실행해 없는 테이블을 만든다. 앱 프로세스 여러 개가 동시에 시작해도 되도록 잠그고 한다."""
    with _locked_cursor(pool) as cur:
        cur.execute(sql)


def ensure_admin(pool: ReusingConnectionPool, admin_password: str) -> None:
    """관리자 계정(학번 admin)이 없으면 만들고, 비밀번호 해시가 admin_password와 다르면 갱신한다."""
    if not admin_password:
        raise ValueError("ADMIN_PASSWORD가 비어 있어 관리자 계정을 만들 수 없습니다")
    with _locked_cursor(pool) as cur:
        cur.execute("select id, password_hash, is_admin from tf_students where student_no = %s", (ADMIN_NO,))
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "insert into tf_students (student_no, name, main_role, password_hash, is_admin) "
                "values (%s, %s, null, %s, true)",
                (ADMIN_NO, ADMIN_NAME, hash_password(admin_password)),
            )
        elif not row[2] or not verify_password(admin_password, row[1]):
            cur.execute(
                "update tf_students set password_hash = %s, is_admin = true where id = %s",
                (hash_password(admin_password), row[0]),
            )


# ---- 저장소 ----

# 화면을 그릴 상태 전체를 질의 한 번으로 읽는다. 빈 집계(null)는 빈 배열로 바꾼다.
_STATE_SQL = """
select json_build_object(
    'students', coalesce((
        select json_agg(json_build_object(
            'id', s.id,
            'student_no', s.student_no,
            'name', s.name,
            'main_role', s.main_role,
            'sub_roles', coalesce((
                select json_agg(r.role) from tf_student_sub_roles r where r.student_id = s.id
            ), '[]'::json)
        ) order by s.id)
        from tf_students s
        where not s.is_admin
    ), '[]'::json),
    'team_ids', coalesce((select json_agg(t.id order by t.id) from tf_teams t), '[]'::json),
    'members', coalesce((
        select json_agg(json_build_array(m.team_id, m.student_id) order by m.team_id, m.slot_no)
        from tf_members m
    ), '[]'::json),
    'applications', coalesce((
        select json_agg(json_build_object(
            'id', a.id,
            'team_id', a.team_id,
            'student_id', a.student_id,
            'status', a.status,
            'reason', a.reason,
            'created_at', a.created_at
        ) order by a.id)
        from tf_applications a
    ), '[]'::json),
    'votes', coalesce((
        select json_agg(json_build_array(v.application_id, v.voter_id, v.vote) order by v.application_id, v.voter_id)
        from tf_votes v
    ), '[]'::json),
    'closed', exists (select 1 from tf_settings where key = %(closed_key)s and value = 'true')
)
"""

# 명단 올리기: 관리자를 뺀 학생, 지원, 투표, 팀원, 팀, 마감 표시를 지우고 명단의 학생과 빈 팀을 만든다.
# 학생 id(identity)는 되돌리지 않으므로 예전 id를 다시 쓰지 않는다. 새 id는 명단 순서로 커진다.
_RESET_SQL = """
delete from tf_votes;
delete from tf_applications;
delete from tf_members;
delete from tf_teams;
delete from tf_students where not is_admin;
delete from tf_settings where key = %(closed_key)s;
insert into tf_students (student_no, name, main_role, password_hash, is_admin)
select r.student_no, r.name, r.main_role, r.password_hash, false
from unnest(%(student_nos)s::text[], %(names)s::text[], %(main_roles)s::text[], %(hashes)s::text[])
    with ordinality as r(student_no, name, main_role, password_hash, ord)
order by r.ord;
insert into tf_student_sub_roles (student_id, role)
select s.id, r.role
from unnest(%(sub_student_nos)s::text[], %(sub_roles)s::text[]) as r(student_no, role)
join tf_students s on s.student_no = r.student_no;
insert into tf_teams (id, name)
select n, '팀' || n from generate_series(1, %(team_count)s) as n;
"""

_ACCOUNT_SQL = "select id, student_no, name, password_hash, is_admin from tf_students where "


def _role_rank(role: str) -> int:
    return ROLES.index(role) if role in ROLES else len(ROLES)


def _parse_time(text: str | None) -> datetime | None:
    """JSON의 timestamptz 글자(예: 2026-11-01T09:00:00.12+00:00)를 시간대가 있는 datetime으로."""
    return datetime.fromisoformat(text) if text else None


def state_from_json(data: Any) -> State:
    """_STATE_SQL의 결과(JSON)를 State로 바꾼다.

    members에는 모든 팀 번호가 키로 있고 학생 id는 slot_no 순서이다. 보조 역할은 ROLES 순서이다.
    """
    if isinstance(data, (str, bytes)):  # psycopg2의 json 형 변환이 꺼져 있을 때
        data = json.loads(data)
    students = {}
    for item in data["students"]:
        student_id = int(item["id"])
        students[student_id] = Student(
            student_id,
            item["student_no"],
            item["name"],
            item["main_role"],
            tuple(sorted(item["sub_roles"], key=_role_rank)),
        )
    team_ids = tuple(int(team_id) for team_id in data["team_ids"])
    slots: dict[int, list[int]] = {team_id: [] for team_id in team_ids}
    for team_id, student_id in data["members"]:
        slots[int(team_id)].append(int(student_id))
    applications = tuple(
        Application(
            int(item["id"]),
            int(item["team_id"]),
            int(item["student_id"]),
            item["status"],
            item["reason"],
            _parse_time(item["created_at"]),
        )
        for item in data["applications"]
    )
    votes: dict[int, dict[int, str]] = {}
    for application_id, voter_id, value in data["votes"]:
        votes.setdefault(int(application_id), {})[int(voter_id)] = value
    return State(
        students=students,
        team_ids=team_ids,
        members={team_id: tuple(ids) for team_id, ids in slots.items()},
        applications=applications,
        votes=votes,
        closed=bool(data["closed"]),
    )


def _account(row: Sequence[Any] | None) -> Account | None:
    if row is None:
        return None
    account_id, student_no, name, password_hash, is_admin = row
    return Account(int(account_id), student_no, name, password_hash, bool(is_admin))


class PgRepo(Repo):
    """커서 하나로 Repo를 구현한다. 트랜잭션과 잠금은 write_repo/read_repo가 맡는다.

    DB 제약을 어기면 psycopg2.IntegrityError가 그대로 나간다(트랜잭션은 write_repo가 되돌림).
    MemoryRepo와 같이, 꽉 찬 팀에 넣거나 없는 지원의 상태를 바꾸면 ValueError이다.
    """

    def __init__(self, cursor: psycopg2.extensions.cursor):
        self._cur = cursor

    # ---- 읽기 ----
    def load_state(self) -> State:
        self._cur.execute(_STATE_SQL, {"closed_key": CLOSED_KEY})
        return state_from_json(self._cur.fetchone()[0])

    def find_account(self, student_no: str) -> Account | None:
        if "\x00" in student_no:  # DB 문자열에는 NUL이 없다
            return None
        self._cur.execute(_ACCOUNT_SQL + "student_no = %s", (student_no,))
        return _account(self._cur.fetchone())

    def get_account(self, user_id: int) -> Account | None:
        self._cur.execute(_ACCOUNT_SQL + "id = %s", (user_id,))
        return _account(self._cur.fetchone())

    # ---- 쓰기 ----
    def insert_application(self, team_id: int, student_id: int, status: str, reason: str) -> int:
        self._cur.execute(
            "insert into tf_applications (team_id, student_id, status, reason, decided_at) "
            "values (%(team_id)s, %(student_id)s, %(status)s, %(reason)s, "
            "case when %(decided)s then now() end) "
            "returning id",
            {
                "team_id": team_id,
                "student_id": student_id,
                "status": status,
                "reason": reason,
                "decided": status != PENDING,  # 빈 팀에 지원해 바로 승인된 경우
            },
        )
        return int(self._cur.fetchone()[0])

    def add_member(self, team_id: int, student_id: int) -> None:
        """비어 있는 가장 작은 slot_no(1~4)에 넣는다. 꽉 찼으면 ValueError."""
        self._cur.execute(
            "insert into tf_members (team_id, slot_no, student_id) "
            "select %(team_id)s, slot.n, %(student_id)s "
            "from generate_series(1, %(team_max)s) as slot(n) "
            "where not exists ("
            "    select 1 from tf_members m where m.team_id = %(team_id)s and m.slot_no = slot.n"
            ") "
            "order by slot.n "
            "limit 1 "
            "returning slot_no",
            {"team_id": team_id, "student_id": student_id, "team_max": TEAM_MAX},
        )
        if self._cur.fetchone() is None:
            raise ValueError(f"팀{team_id}가 꽉 참")

    def remove_member(self, student_id: int) -> None:
        self._cur.execute("delete from tf_members where student_id = %s", (student_id,))

    def record_vote(self, application_id: int, voter_id: int, vote: str) -> None:
        self._cur.execute(
            "insert into tf_votes (application_id, voter_id, vote) values (%s, %s, %s)",
            (application_id, voter_id, vote),
        )

    def set_application_status(self, application_id: int, status: str, reason: str) -> None:
        self._cur.execute(
            "update tf_applications set status = %s, reason = %s, decided_at = now() where id = %s",
            (status, reason, application_id),
        )
        if self._cur.rowcount == 0:
            raise ValueError(f"없는 지원 {application_id}")

    def set_closed(self) -> None:
        self._cur.execute(
            "insert into tf_settings (key, value) values (%s, 'true') "
            "on conflict (key) do update set value = excluded.value",
            (CLOSED_KEY,),
        )

    def reset(self, rows: list[RosterRow], password_hashes: list[str], team_count: int) -> None:
        if len(rows) != len(password_hashes):
            raise ValueError("학생 수와 비밀번호 해시 수가 다름")
        self._cur.execute(
            _RESET_SQL,
            {
                "closed_key": CLOSED_KEY,
                "student_nos": [row.student_no for row in rows],
                "names": [row.name for row in rows],
                "main_roles": [row.main_role for row in rows],
                "hashes": list(password_hashes),
                "sub_student_nos": [row.student_no for row in rows for _ in row.sub_roles],
                "sub_roles": [role for row in rows for role in row.sub_roles],
                "team_count": team_count,
            },
        )
