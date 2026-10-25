"""팀 편성 규칙: 역할 상수, 학생, 필수 포함 역할 자리 계산.

DB와 Streamlit에 의존하지 않는 순수 파이썬 모듈이다.
판단은 역할과 규칙으로만 하고, 학생의 이름·학번·순서는 보지 않는다.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

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
