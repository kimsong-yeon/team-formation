"""팀 편성 규칙: 역할 상수, 학생, 필수 포함 역할 자리 계산, 지원·투표·마감 판단.

DB와 Streamlit에 의존하지 않는 순수 파이썬 모듈이다.
결과는 역할과 규칙으로만 정한다. 학생의 이름·학번은 화면과 문구에 보여 주거나 순서를 정할 때만 쓴다.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

PLANNER = "기획자"
WEB = "웹 프로그래머"

# 역할 6개 (이 순서). 약자는 ABBR.
ROLES: tuple[str, ...] = (
    PLANNER,
    WEB,
    "프론트엔드 디자이너",
    "테스터",
    "보고서 작성자",
    "발표자",
)
ABBR: dict[str, str] = {
    PLANNER: "PL",
    WEB: "WP",
    "프론트엔드 디자이너": "FD",
    "테스터": "TT",
    "보고서 작성자": "RW",
    "발표자": "PT",
}

# 팀 인원 범위 (2~4명)
TEAM_MIN = 2
TEAM_MAX = 4

# 필수 포함 역할: 팀마다 서로 다른 학생이 1명씩 맡아야 한다.
REQUIRED_ROLES: tuple[str, ...] = (PLANNER, WEB)

# 지원 상태
PENDING = "대기"
APPROVED = "승인"
REJECTED = "거절"
BLOCKED = "막힘"

# 투표
YES = "찬성"
NO = "거부"


@dataclass(frozen=True)
class Student:
    id: int
    student_no: str
    name: str
    main_role: str
    sub_roles: tuple[str, ...] = ()

    def can(self, role: str) -> bool:
        """그 역할이 주 역할이거나 보조 역할이면 맡을 수 있다."""
        return role == self.main_role or role in self.sub_roles


def required_filled(members: Sequence[Student]) -> int:
    """기획자와 웹 프로그래머를 서로 다른 학생에게 맡길 때 채울 수 있는 최대 수(0~2).

    한 학생이 둘 다 맡을 수 있어도 하나로만 센다.
    """
    planners = [m for m in members if m.can(PLANNER)]
    webs = [m for m in members if m.can(WEB)]
    # 기획자 담당 a와 웹 프로그래머 담당 b를 서로 다른 학생으로 고를 수 있으면 2
    if any(a.id != b.id for a in planners for b in webs):
        return 2
    if planners or webs:
        return 1
    return 0


def unmet_roles(members: Sequence[Student]) -> list[str]:
    """아직 못 채운 필수 역할의 설명.

    - 기획자를 맡을 학생이 없으면 "기획자"
    - 웹 프로그래머를 맡을 학생이 없으면 "웹 프로그래머"
    - 둘 다 맡을 수 있는 학생이 한 명뿐이라 하나만 채우면 "기획자나 웹 프로그래머 중 하나"
    """
    if required_filled(members) == 2:
        return []
    has_planner = any(m.can(PLANNER) for m in members)
    has_web = any(m.can(WEB) for m in members)
    if has_planner and has_web:
        # 둘 다 가능한 학생이 한 명뿐이고 다른 후보가 없는 경우
        return ["기획자나 웹 프로그래머 중 하나"]
    unmet = []
    if not has_planner:
        unmet.append(PLANNER)
    if not has_web:
        unmet.append(WEB)
    return unmet


def seat_problem(members_after: Sequence[Student]) -> str | None:
    """지원자를 넣은 뒤의 팀원 목록이 어기는 규칙의 이유. 어기는 것이 없으면 None.

    검사 순서: 정원 -> 주 역할 중복 -> 필수 포함 역할 자리.
    """
    if len(members_after) > TEAM_MAX:
        return "팀이 꽉 참"
    main_roles = [m.main_role for m in members_after]
    if len(set(main_roles)) < len(main_roles):
        return "같은 주 역할의 팀원이 있음"
    seats_left = TEAM_MAX - len(members_after)
    missing = len(REQUIRED_ROLES) - required_filled(members_after)
    if seats_left < missing:
        return (
            f"필수 포함 역할 자리가 모자람 "
            f"(남는 자리 {seats_left}개, 채워야 할 역할: {', '.join(unmet_roles(members_after))})"
        )
    return None


@dataclass(frozen=True)
class Application:
    """팀에 낸 지원. status는 대기·승인·거절 가운데 하나."""

    id: int
    team_id: int
    student_id: int
    status: str
    reason: str = ""
    created_at: datetime | None = None


@dataclass(frozen=True)
class State:
    """판단에 필요한 지금 상태(읽기 전용).

    members: 팀 번호 -> 팀원 학생 id들, votes: 지원 id -> {투표자 id: 찬성/거부}.
    """

    students: dict[int, Student]
    team_ids: tuple[int, ...]
    members: dict[int, tuple[int, ...]]
    applications: tuple[Application, ...]
    votes: dict[int, dict[int, str]]
    closed: bool

    def team_name(self, team_id: int) -> str:
        return f"팀{team_id}"

    def team_of(self, student_id: int) -> int | None:
        for team_id, ids in self.members.items():
            if student_id in ids:
                return team_id
        return None

    def team_members(self, team_id: int) -> list[Student]:
        return [self.students[i] for i in self.members.get(team_id, ())]

    def pending_of(self, student_id: int) -> Application | None:
        for a in self.applications:
            if a.student_id == student_id and a.status == PENDING:
                return a
        return None

    def application(self, app_id: int) -> Application | None:
        for a in self.applications:
            if a.id == app_id:
                return a
        return None

    def pending_for_team(self, team_id: int) -> list[Application]:
        pending = [a for a in self.applications if a.team_id == team_id and a.status == PENDING]
        return sorted(pending, key=lambda a: a.id)

    def unassigned(self) -> list[Student]:
        """팀이 없는 학생, 학번 순서."""
        assigned = {i for ids in self.members.values() for i in ids}
        free = [s for s in self.students.values() if s.id not in assigned]
        return sorted(free, key=lambda s: s.student_no)


@dataclass(frozen=True)
class Outcome:
    """결과 안내. kind는 승인·대기·거절·막힘 가운데 하나."""

    kind: str
    message: str

    @property
    def text(self) -> str:
        return f"{self.kind}: {self.message}"


def apply_lock_reason(state: State, student_id: int) -> str | None:
    """지원 단추를 모두 비활성화할 이유. 지원할 수 있으면 None.

    검사 순서는 decide_apply와 같다: 마감 -> 팀 있음 -> 대기 지원 있음.
    """
    if state.closed:
        return "모집이 마감되었습니다"
    team_id = state.team_of(student_id)
    if team_id is not None:
        return f"이미 {state.team_name(team_id)}에 속해 있습니다"
    pending = state.pending_of(student_id)
    if pending is not None:
        return (
            f"{state.team_name(pending.team_id)}에 낸 지원이 대기 중입니다. "
            f"결과가 나온 뒤 다시 지원할 수 있습니다"
        )
    return None


def decide_apply(state: State, student_id: int, team_id: int) -> Outcome:
    """지원 단추를 눌렀을 때의 결과. 막힘이면 아무것도 기록하지 않는다.

    PRD 4.2절 순서: 마감 -> 이미 팀 있음 -> 대기 지원 있음 -> 정원 -> 주 역할 중복 -> 필수 역할 자리.
    뒤의 셋은 seat_problem 하나가 그 순서로 판단한다.
    """
    if team_id not in state.team_ids:
        return Outcome(BLOCKED, "없는 팀입니다")
    if state.closed:
        return Outcome(BLOCKED, "모집이 마감됨")
    if state.team_of(student_id) is not None:
        return Outcome(BLOCKED, "이미 팀이 있음")
    if state.pending_of(student_id) is not None:
        return Outcome(BLOCKED, "결과를 기다리는 지원이 있음")
    members = state.team_members(team_id)
    problem = seat_problem(members + [state.students[student_id]])
    if problem is not None:
        return Outcome(BLOCKED, problem)
    if not members:
        return Outcome(APPROVED, "빈 팀이라 바로 승인되었습니다")
    return Outcome(PENDING, "지원했습니다. 팀원 모두의 투표를 기다립니다")


@dataclass(frozen=True)
class VoteDecision:
    """투표 판단. record_vote가 True일 때만 표를 기록하고,
    final_status가 있으면 지원을 그 상태(승인/거절)로 확정한다."""

    outcome: Outcome
    record_vote: bool
    final_status: str | None


def decide_vote(state: State, application_id: int, voter_id: int, vote: str) -> VoteDecision:
    """투표 한 표의 결과. state는 이번 표를 넣기 전 상태이다."""

    def no_record(kind: str, message: str) -> VoteDecision:
        return VoteDecision(Outcome(kind, message), False, None)

    app = state.application(application_id)
    if app is None:
        return no_record(BLOCKED, "없는 지원입니다")
    # 이미 결과가 나온 지원은 지금 상태를 알려 준다.
    if app.status == APPROVED:
        return no_record(APPROVED, "이미 승인된 지원입니다")
    if app.status == REJECTED:
        return no_record(REJECTED, "이미 거절된 지원입니다")
    if state.closed:
        return no_record(BLOCKED, "모집이 마감됨")
    if voter_id not in state.members.get(app.team_id, ()):
        return no_record(BLOCKED, "이 팀의 팀원만 투표할 수 있습니다")
    if voter_id in state.votes.get(app.id, {}):
        return no_record(PENDING, "이미 투표했습니다. 다른 팀원의 투표를 기다립니다")
    if vote not in (YES, NO):
        return no_record(BLOCKED, "투표는 찬성이나 거부만 할 수 있습니다")

    if vote == NO:
        return VoteDecision(Outcome(REJECTED, "거부하여 지원이 거절되었습니다"), True, REJECTED)

    # 지금 팀원이 아닌 사람의 표는 세지 않는다. 이번 찬성을 더해 지금 팀원 전원이 찬성했는지 본다.
    yes_voters = {v for v, value in state.votes.get(app.id, {}).items() if value == YES}
    yes_voters.add(voter_id)
    if all(member_id in yes_voters for member_id in state.members[app.team_id]):
        # 승인하기 직전에 지금 팀 구성으로 규칙을 다시 검사한다.
        problem = seat_problem(state.team_members(app.team_id) + [state.students[app.student_id]])
        if problem is None:
            return VoteDecision(Outcome(APPROVED, "팀원 모두 찬성하여 승인되었습니다"), True, APPROVED)
        message = f"승인 직전 검사에서 규칙에 맞지 않습니다 ({problem})"
        return VoteDecision(Outcome(REJECTED, message), True, REJECTED)
    return VoteDecision(Outcome(PENDING, "찬성했습니다. 다른 팀원의 투표를 기다립니다"), True, None)


def decide_close(state: State) -> tuple[Outcome, tuple[int, ...], tuple[int, ...]]:
    """모집 마감 결과: (안내, 거절할 대기 지원 id들, 팀에서 뺄 학생 id들).

    대기 지원은 모두 거절하고(지원 id 순서), 1명뿐인 팀의 학생은 팀에서 뺀다(팀 번호 순서).
    """
    if state.closed:
        return Outcome(BLOCKED, "이미 마감되었습니다"), (), ()
    pending = sorted((a for a in state.applications if a.status == PENDING), key=lambda a: a.id)
    rejected = tuple(a.id for a in pending)
    removed = tuple(
        state.members[team_id][0]
        for team_id in sorted(state.members)
        if len(state.members[team_id]) == 1
    )
    applicants = ", ".join(state.students[a.student_id].name for a in pending) or "없음"
    removed_names = ", ".join(state.students[i].name for i in removed) or "없음"
    message = f"모집을 마감했습니다. 거절한 지원: {applicants} · 팀에서 뺀 학생: {removed_names}"
    return Outcome(APPROVED, message), rejected, removed
