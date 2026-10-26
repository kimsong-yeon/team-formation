"""쓰기 처리: 규칙의 판단 결과(teamapp.rules)대로만 저장소(Repo)에 쓴다.

판단 로직은 여기서 다시 짜지 않는다. 각 함수는 계정 확인 -> repo.load_state() -> 판단 -> 쓰기 순서로 일하고
결과 안내(Outcome)를 돌려 준다. 막힘이면 아무것도 쓰지 않는다.
트랜잭션과 잠금은 Repo를 만드는 쪽(DB 저장소)이 맡는다.
DB와 Streamlit에 의존하지 않는다(bcrypt는 teamapp.passwords가 함수 안에서만 불러온다).
"""
from __future__ import annotations

import csv
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from teamapp.passwords import hash_password, verify_password
from teamapp.roster import RosterError, RosterRow, parse_roster
from teamapp.rules import (
    APPROVED,
    BLOCKED,
    PENDING,
    REJECTED,
    YES,
    Application,
    Outcome,
    State,
    decide_apply,
    decide_close,
    decide_vote,
    seat_problem,
)
from teamapp.solver import validate_plan

STUDENT_PASSWORD = "1234"  # 명단으로 만든 학생 계정의 처음 비밀번호
MIN_TEAM_COUNT = 1
MAX_TEAM_COUNT = 8

NOT_ADMIN = "관리자만 할 수 있습니다"
NOT_STUDENT = "학생 계정으로만 할 수 있습니다"
LOGIN_FAILED = "학번이나 비밀번호가 맞지 않습니다"
UNREADABLE_CSV = "CSV 형식이 맞지 않습니다 (읽을 수 없는 CSV 파일입니다)"


@dataclass(frozen=True)
class Account:
    id: int
    student_no: str
    name: str
    password_hash: str
    is_admin: bool


class Repo(Protocol):
    """저장소 인터페이스. 읽기는 load_state 한 번이고, 쓰기는 아래 함수들이다."""

    def load_state(self) -> State:
        """지금 상태(학생, 팀, 팀원, 지원, 투표, 마감 여부)를 읽는다."""
        ...

    def find_account(self, student_no: str) -> Account | None: ...

    def get_account(self, user_id: int) -> Account | None: ...

    def insert_application(self, team_id: int, student_id: int, status: str, reason: str) -> int:
        """지원을 기록하고 지원 id를 돌려 준다."""
        ...

    def add_member(self, team_id: int, student_id: int) -> None:
        """팀에 넣는다. 비어 있는 가장 작은 slot_no(1~4)에 들어간다."""
        ...

    def remove_member(self, student_id: int) -> None: ...

    def record_vote(self, application_id: int, voter_id: int, vote: str) -> None: ...

    def set_application_status(self, application_id: int, status: str, reason: str) -> None: ...

    def set_closed(self) -> None: ...

    def reset(self, rows: list[RosterRow], password_hashes: list[str], team_count: int) -> None:
        """관리자 계정을 뺀 학생, 지원, 투표, 팀원, 팀, 마감 표시를 지우고 명단의 학생과 빈 팀을 만든다."""
        ...


def _require_admin(repo: Repo, actor_id: int) -> Outcome | None:
    account = repo.get_account(actor_id)
    if account is None or not account.is_admin:
        return Outcome(BLOCKED, NOT_ADMIN)
    return None


def _require_student(repo: Repo, actor_id: int) -> Outcome | None:
    account = repo.get_account(actor_id)
    if account is None or account.is_admin:
        return Outcome(BLOCKED, NOT_STUDENT)
    return None


def login(
    repo: Repo,
    student_no: str,
    password: str,
    verify: Callable[[str, str], bool] = verify_password,
) -> tuple[Outcome, Account | None]:
    """학번과 비밀번호로 로그인한다. 실패 이유(빈칸, 없는 학번, 틀린 비밀번호)는 모두 같은 문구로 알린다."""
    failed = (Outcome(BLOCKED, LOGIN_FAILED), None)
    student_no = student_no.strip()
    if not student_no or not password:
        return failed
    account = repo.find_account(student_no)
    if account is None or not verify(password, account.password_hash):
        return failed
    return Outcome(APPROVED, "로그인했습니다"), account


def apply(repo: Repo, actor_id: int, team_id: int) -> Outcome:
    """학생이 팀에 지원한다."""
    blocked = _require_student(repo, actor_id)
    if blocked is not None:
        return blocked
    outcome = decide_apply(repo.load_state(), actor_id, team_id)
    if outcome.kind == APPROVED:  # 빈 팀: 지원이 바로 승인되어 팀원이 된다
        repo.insert_application(team_id, actor_id, APPROVED, "빈 팀에 지원")
        repo.add_member(team_id, actor_id)
    elif outcome.kind == PENDING:
        repo.insert_application(team_id, actor_id, PENDING, "")
    return outcome


def vote(repo: Repo, actor_id: int, application_id: int, vote: str) -> Outcome:
    """팀원이 지원에 투표한다."""
    blocked = _require_student(repo, actor_id)
    if blocked is not None:
        return blocked
    state = repo.load_state()
    decision = decide_vote(state, application_id, actor_id, vote)
    if decision.record_vote:
        repo.record_vote(application_id, actor_id, vote)
    app = state.application(application_id)
    if decision.final_status == APPROVED:
        repo.set_application_status(application_id, APPROVED, "팀원 모두 찬성")
        repo.add_member(app.team_id, app.student_id)
    elif decision.final_status == REJECTED:
        repo.set_application_status(application_id, REJECTED, _rejection_reason(state, app, vote, decision.outcome))
    return decision.outcome


def _rejection_reason(state: State, app: Application, vote: str, outcome: Outcome) -> str:
    """투표로 거절된 지원의 사유. 거부 표이거나, 모두 찬성했지만 승인 직전 검사에 걸린 경우이다."""
    if vote != YES:
        return "팀원이 거부함"
    problem = seat_problem(state.team_members(app.team_id) + [state.students[app.student_id]])
    return f"승인 직전 검사: {problem or outcome.message}"


def reset_roster(
    repo: Repo,
    actor_id: int,
    data: bytes | None,
    team_count: int,
    hasher: Callable[[str], str] = hash_password,
) -> Outcome:
    """관리자가 올린 명단 CSV로 처음 상태를 만든다. 학생의 처음 비밀번호는 모두 1234이다."""
    blocked = _require_admin(repo, actor_id)
    if blocked is not None:
        return blocked
    if not data:
        return Outcome(BLOCKED, "명단 CSV 파일을 올려 주세요")
    if (
        not isinstance(team_count, int)
        or isinstance(team_count, bool)
        or not MIN_TEAM_COUNT <= team_count <= MAX_TEAM_COUNT
    ):
        return Outcome(BLOCKED, f"팀 수는 {MIN_TEAM_COUNT}~{MAX_TEAM_COUNT} 사이여야 합니다")
    try:
        rows = parse_roster(data)
    except RosterError as error:
        return Outcome(BLOCKED, f"CSV 형식이 맞지 않습니다 ({error})")
    except (csv.Error, ValueError):
        return Outcome(BLOCKED, UNREADABLE_CSV)
    # DB 문자열에는 NUL 문자를 저장할 수 없다.
    if any("\x00" in field for row in rows for field in (row.student_no, row.name, row.main_role, *row.sub_roles)):
        return Outcome(BLOCKED, UNREADABLE_CSV)

    repo.reset(rows, [hasher(STUDENT_PASSWORD) for _ in rows], team_count)
    return Outcome(APPROVED, f"학생 {len(rows)}명과 빈 팀 {team_count}개로 처음 상태를 만들었습니다")


def close(repo: Repo, actor_id: int) -> Outcome:
    """모집을 마감한다: 대기 지원을 모두 거절하고, 1명뿐인 팀의 학생을 팀에서 뺀다."""
    blocked = _require_admin(repo, actor_id)
    if blocked is not None:
        return blocked
    state = repo.load_state()
    if not state.students:
        return Outcome(BLOCKED, "명단을 먼저 올려 주세요")
    outcome, rejected, removed = decide_close(state)
    if outcome.kind != APPROVED:
        return outcome
    for application_id in rejected:
        repo.set_application_status(application_id, REJECTED, "모집 마감")
    for student_id in removed:
        repo.remove_member(student_id)
    repo.set_closed()
    return outcome


def confirm(repo: Repo, actor_id: int, plan: dict[int, int]) -> Outcome:
    """관리자가 배정안(학생 id -> 팀 id)을 확정한다. 지금 상태로 한 번 더 검사해서 통과해야 한다."""
    blocked = _require_admin(repo, actor_id)
    if blocked is not None:
        return blocked
    state = repo.load_state()
    if not state.closed:
        return Outcome(BLOCKED, "모집을 먼저 마감해야 합니다")
    if not plan:
        return Outcome(BLOCKED, "확정할 배정안이 없습니다")
    pairs = sorted(plan.items())
    if validate_plan(state, pairs):
        return Outcome(BLOCKED, "배정안을 만든 뒤 상태가 바뀌었습니다. AI 배정을 다시 해 주세요")
    for student_id, team_id in pairs:
        repo.add_member(team_id, student_id)
    return Outcome(APPROVED, f"배정안을 확정했습니다 ({len(pairs)}명)")
