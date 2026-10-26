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
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extensions
import psycopg2.pool

from teamapp.actions import Account, Repo
from teamapp.passwords import hash_password, verify_password
from teamapp.roster import ADMIN_NO, RosterRow
from teamapp.rules import PENDING, ROLES, TEAM_MAX, Application, State, Student

LOCK_KEY = 7240001  # 쓰기를 한 번에 하나씩 처리하는 잠금 번호(pg_advisory_xact_lock)
POOL_MIN = 1
POOL_MAX = 8
POOL_WAIT_SECONDS = 5.0  # 풀의 연결이 모두 쓰이는 중이면 이만큼 기다린다
POOL_RETRY_SLEEP = 0.05
ADMIN_NAME = "관리자"
CLOSED_KEY = "closed"  # tf_settings에 (closed, true)가 있으면 모집 마감
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


def read_schema_sql() -> str:
    """저장소의 schema.sql을 읽는다(Windows에서도 UTF-8로, BOM이 있어도 됨)."""
    return SCHEMA_PATH.read_text(encoding="utf-8-sig")


# ---- 연결 풀과 연결 ----


def make_pool(dsn: str) -> psycopg2.pool.ThreadedConnectionPool:
    """연결 풀을 만든다. 최소 1개 연결을 바로 만들므로 접속 정보가 틀리면 여기서 오류가 난다.

    DATABASE_URL 형식이 틀리면 psycopg2 오류 문구에 주소(비밀번호 포함)가 섞일 수 있어, 값 없는 문구로 바꾼다.
    """
    try:
        return psycopg2.pool.ThreadedConnectionPool(
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
    except psycopg2.ProgrammingError:  # invalid dsn
        raise ValueError("DATABASE_URL 형식이 맞지 않습니다") from None


def _getconn(pool: psycopg2.pool.AbstractConnectionPool) -> psycopg2.extensions.connection:
    """풀에서 연결을 꺼낸다. 모두 쓰이는 중이면(PoolError) 잠깐씩 쉬며 POOL_WAIT_SECONDS까지 다시 해 본다."""
    deadline = time.monotonic() + POOL_WAIT_SECONDS
    while True:
        try:
            return pool.getconn()
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


def _discard(pool: psycopg2.pool.AbstractConnectionPool, conn: psycopg2.extensions.connection) -> None:
    """연결을 닫고 풀에서 뺀다."""
    try:
        pool.putconn(conn, close=True)
    except psycopg2.pool.PoolError:  # 풀이 이미 닫힘
        conn.close()


def _checkin(pool: psycopg2.pool.AbstractConnectionPool, conn: psycopg2.extensions.connection) -> None:
    """끝나지 않은 트랜잭션을 되돌리고 연결을 풀에 돌려준다. 끊긴 연결은 닫아서 뺀다."""
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


def _checkout(pool: psycopg2.pool.AbstractConnectionPool) -> psycopg2.extensions.connection:
    """살아 있는 연결을 꺼낸다. 끊긴 연결은 버리고 새 연결을 한 번만 더 받는다."""
    for _ in range(2):
        conn = _getconn(pool)
        try:
            alive = _is_alive(conn)
        except BaseException:
            _discard(pool, conn)
            raise
        if alive:
            return conn
        _discard(pool, conn)
    raise psycopg2.OperationalError("DB에 다시 연결했지만 응답이 없습니다")


@contextmanager
def connection(pool: psycopg2.pool.AbstractConnectionPool) -> Iterator[psycopg2.extensions.connection]:
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
def _locked_cursor(pool: psycopg2.pool.AbstractConnectionPool) -> Iterator[psycopg2.extensions.cursor]:
    """쓰기 트랜잭션 하나. 잠금부터 걸고, 끝까지 성공하면 commit, 예외면 rollback하고 예외를 그대로 낸다."""
    with connection(pool) as conn:
        try:
            with conn.cursor() as cur:
                # psycopg2는 첫 질의 앞에 BEGIN을 보낸다. 잠금은 commit/rollback 때 저절로 풀린다.
                cur.execute("select pg_advisory_xact_lock(%s)", (LOCK_KEY,))
                yield cur
            conn.commit()
        except BaseException:
            _rollback_quietly(conn)
            raise


@contextmanager
def write_repo(pool: psycopg2.pool.AbstractConnectionPool) -> Iterator[PgRepo]:
    """쓰기용 저장소. 블록 하나가 잠긴 트랜잭션 하나이다."""
    with _locked_cursor(pool) as cur:
        yield PgRepo(cur)


@contextmanager
def read_repo(pool: psycopg2.pool.AbstractConnectionPool) -> Iterator[PgRepo]:
    """읽기 전용 저장소. 쓰기 함수를 부르면 DB가 오류를 낸다. 끝나면 되돌린다."""
    with connection(pool) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("set transaction read only")
                yield PgRepo(cur)
        finally:
            _rollback_quietly(conn)


# ---- 테이블과 관리자 계정 ----


def ensure_schema(pool: psycopg2.pool.AbstractConnectionPool, sql: str) -> None:
    """schema.sql을 실행해 없는 테이블을 만든다. 앱 프로세스 여러 개가 동시에 시작해도 되도록 잠그고 한다."""
    with _locked_cursor(pool) as cur:
        cur.execute(sql)


def ensure_admin(pool: psycopg2.pool.AbstractConnectionPool, admin_password: str) -> None:
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
