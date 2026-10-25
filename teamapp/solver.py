"""배정안 검사와 모든 경우를 따지는 배정.

- validate_plan: 배정안(학생 id, 팀 id 쌍들)을 다섯 가지로 검사해 문제 문구 목록을 돌려준다.
- solve: 팀이 없는 학생을 팀에 넣는 모든 경우를 백트래킹으로 따져, 다섯 검사를 모두 통과하는 배정 하나를 찾는다.
- explain_no_assignment: 배정이 없을 때 이유를 찾아 준다.

DB와 Streamlit에 의존하지 않는 순수 파이썬 모듈이다(teamapp.rules와 표준 라이브러리만 쓴다).
판단은 역할과 규칙으로만 하고, 학생의 이름·학번은 판단에 쓰지 않는다
(학번 순서는 문구와 탐색 순서를 일정하게 하려고 State.unassigned()의 순서를 따를 뿐이다).
"""
from __future__ import annotations

from collections.abc import Sequence

from teamapp.rules import (
    PLANNER,
    TEAM_MAX,
    TEAM_MIN,
    WEB,
    State,
    Student,
    required_filled,
    seat_problem,
)

NO_REASON = "역할 조합상 모든 규칙을 함께 지킬 수 없음"


def _role_gaps(members: Sequence[Student]) -> list[tuple[str, ...]]:
    """못 채운 필수 역할 자리들. 자리마다 그 자리를 채울 수 있는 역할 묶음을 돌려준다.

    - (기획자,) / (웹 프로그래머,): 그 역할을 맡을 팀원이 아무도 없음
    - (기획자, 웹 프로그래머): 둘 다 맡을 수 있는 팀원 한 명뿐이고 다른 팀원은 둘 다 못 맡음.
      팀원 밖의 학생 한 명이 둘 중 하나를 맡아야 한다.
    """
    if required_filled(members) == 2:
        return []
    has_planner = any(m.can(PLANNER) for m in members)
    has_web = any(m.can(WEB) for m in members)
    if has_planner and has_web:
        return [(PLANNER, WEB)]
    gaps: list[tuple[str, ...]] = []
    if not has_planner:
        gaps.append((PLANNER,))
    if not has_web:
        gaps.append((WEB,))
    return gaps


def _gap_text(team_name: str, gap: tuple[str, ...]) -> str:
    # 조사는 "를"로 고정한다("기획자를", "웹 프로그래머를").
    if len(gap) == 2:
        return f"{team_name}에 {PLANNER}와 {WEB}를 따로 맡을 학생이 없음"
    return f"{team_name}에 {gap[0]}를 맡을 학생이 없음"


def _matching_size(candidates: list[list[int]]) -> int:
    """자리마다 채울 수 있는 학생 id 목록이 주어질 때, 서로 다른 학생으로 채울 수 있는 자리 수의 최댓값."""
    taker: dict[int, int] = {}  # 학생 id -> 그 학생이 채우기로 한 자리 번호

    def assign(slot: int, visited: set[int]) -> bool:
        for student_id in candidates[slot]:
            if student_id in visited:
                continue
            visited.add(student_id)
            if student_id not in taker or assign(taker[student_id], visited):
                taker[student_id] = slot
                return True
        return False

    return sum(1 for slot in range(len(candidates)) if assign(slot, set()))


def validate_plan(state: State, pairs: Sequence[tuple[int, int]]) -> list[str]:
    """배정안의 문제 문구 목록. 문제가 없으면 빈 목록.

    pairs는 (학생 id, 팀 id). 문구는 검사 1~5 순서로, 같은 검사 안에서는 팀 번호,
    학생 순서(State.unassigned()의 순서)대로 나온다.
    팀이 이미 있는 학생이나 없는 팀을 가리키는 쌍은 1번 검사에서 알리고, 팀 구성에는 반영하지 않는다.
    나머지 검사는 이 배정안을 적용한 뒤의 모든 팀을 본다(이미 팀에 있는 학생은 옮기지 않음).
    """
    free = state.unassigned()
    free_ids = {s.id for s in free}
    team_order = sorted(state.team_ids)
    team_set = set(team_order)
    rank = {
        s.id: i
        for i, s in enumerate(sorted(state.students.values(), key=lambda s: s.student_no))
    }

    # 쌍을 가른다: 학생 id -> 배정된 팀들(중복 쌍은 하나로), 그리고 잘못된 쌍의 기록.
    assigned: dict[int, list[int]] = {}
    not_free: dict[int, int] = {}  # 이미 팀이 있는 학생 -> 가장 작은 팀 번호
    unknown_students: dict[int, int] = {}  # 없는 학생 id -> 가장 작은 팀 번호
    unknown_teams: set[int] = set()
    for student_id, team_id in pairs:
        student_ok = True
        if student_id not in state.students:
            student_ok = False
            unknown_students[student_id] = min(team_id, unknown_students.get(student_id, team_id))
        elif student_id not in free_ids:
            student_ok = False
            not_free[student_id] = min(team_id, not_free.get(student_id, team_id))
        team_ok = team_id in team_set
        if not team_ok:
            unknown_teams.add(team_id)
        if student_ok and team_ok and team_id not in assigned.setdefault(student_id, []):
            assigned[student_id].append(team_id)

    problems: list[str] = []

    # 1) 팀이 없는 학생이 빠짐없이 배정됨 (없는 학생·없는 팀을 가리키는 쌍도 여기서 알린다)
    for s in free:
        if s.id not in assigned:
            problems.append(f"{s.name}이(가) 배정되지 않음")
    for student_id in sorted(not_free, key=lambda i: (not_free[i], rank[i])):
        problems.append(f"{state.students[student_id].name}은(는) 팀이 없는 학생이 아님")
    for student_id in sorted(unknown_students, key=lambda i: (unknown_students[i], i)):
        problems.append(f"없는 학생 {student_id}")
    for team_id in sorted(unknown_teams):
        problems.append(f"없는 팀 {team_id}")

    # 2) 한 학생은 한 팀에만 배정됨
    for s in free:
        if len(assigned.get(s.id, ())) > 1:
            problems.append(f"{s.name}이(가) 두 팀 이상에 배정됨")

    # 배정안을 적용한 뒤의 팀 구성 (기존 팀원 + 새로 들어온 학생, 학생 순서대로)
    teams: dict[int, list[Student]] = {}
    for team_id in team_order:
        added = [s for s in free if team_id in assigned.get(s.id, ())]
        teams[team_id] = state.team_members(team_id) + added

    # 3) 모든 팀이 2~4명
    for team_id in team_order:
        size = len(teams[team_id])
        if not TEAM_MIN <= size <= TEAM_MAX:
            problems.append(
                f"{state.team_name(team_id)}의 인원이 {size}명임({TEAM_MIN}~{TEAM_MAX}명이어야 함)"
            )

    # 4) 팀 안에서 주 역할 중복 없음 (겹치는 역할마다 한 번)
    for team_id in team_order:
        counts: dict[str, int] = {}
        for m in teams[team_id]:
            counts[m.main_role] = counts.get(m.main_role, 0) + 1
        for role, count in counts.items():
            if count > 1:
                problems.append(f"{state.team_name(team_id)}에 주 역할이 같은 학생이 있음({role})")

    # 5) 필수 포함 역할 충족
    for team_id in team_order:
        for gap in _role_gaps(teams[team_id]):
            problems.append(_gap_text(state.team_name(team_id), gap))

    return problems


def solve(state: State) -> dict[int, int] | None:
    """다섯 검사를 모두 통과하는 배정 하나(학생 id -> 팀 id). 없으면 None.

    팀이 없는 학생만 배정한다. 무작위 없이 항상 같은 상태에서 같은 결과를 낸다.
    들어갈 수 있는 팀이 가장 적은 학생부터 하나씩 놓는 백트래킹이며, 가망 없는 경우는 일찍 버린다:
    정원·주 역할 중복·필수 역할 자리(seat_problem), 2명이 안 되는 팀을 채울 학생 수와 남은 자리 수,
    못 채운 필수 역할 자리를 서로 다른 학생으로 채울 수 있는지(짝짓기).
    """
    pending = state.unassigned()
    if not pending:
        return {} if not validate_plan(state, []) else None

    team_order = sorted(state.team_ids)
    current: dict[int, list[Student]] = {t: list(state.team_members(t)) for t in team_order}
    # 기존 팀원은 옮기지 않으므로, 이미 규칙을 어긴 팀이 있으면 학생을 더 넣어도 소용없다.
    if any(seat_problem(members) is not None for members in current.values()):
        return None

    placed: dict[int, int] = {}

    def search(remaining: list[Student]) -> bool:
        # (b) 남은 학생 수와 팀 자리 수: 2명이 안 되는 팀을 채우기에 모자라거나, 자리보다 학생이 많으면 버림
        sizes = [len(current[t]) for t in team_order]
        if len(remaining) < sum(max(0, TEAM_MIN - n) for n in sizes):
            return False
        if len(remaining) > sum(max(0, TEAM_MAX - n) for n in sizes):
            return False

        # (a) 학생을 팀에 넣을 때 정원, 주 역할 중복, 필수 역할 자리(seat_problem)를 검사한다.
        options = {
            s.id: [t for t in team_order if seat_problem(current[t] + [s]) is None]
            for s in remaining
        }

        # 팀마다 못 채운 필수 역할 자리는 서로 다른 학생이 채워야 한다.
        # 자리마다 채울 수 있는 학생(그 팀에 들어갈 수 있고 역할을 맡을 수 있음)을 이어서,
        # 모든 자리에 서로 다른 학생을 짝지을 수 없으면 버린다.
        slots = [
            [
                s.id
                for s in remaining
                if t in options[s.id] and any(s.can(role) for role in gap)
            ]
            for t in team_order
            for gap in _role_gaps(current[t])
        ]
        if _matching_size(slots) < len(slots):
            return False

        if not remaining:
            # 마지막에 다섯 검사로 한 번 더 확인한다.
            return not validate_plan(state, list(placed.items()))

        # 비어 있는 팀끼리는 바꿔도 같으므로 그중 첫 번째에만 넣어 본다.
        def candidates(student: Student) -> list[int]:
            seen_empty = False
            result = []
            for t in options[student.id]:
                if not current[t]:
                    if seen_empty:
                        continue
                    seen_empty = True
                result.append(t)
            return result

        # 들어갈 수 있는 팀이 가장 적은 학생부터 놓는다(같으면 학생 순서).
        choices = [(student, candidates(student)) for student in remaining]
        student, teams_to_try = min(choices, key=lambda pair: len(pair[1]))
        if not teams_to_try:
            return False

        rest = [s for s in remaining if s.id != student.id]
        for t in teams_to_try:
            current[t].append(student)
            placed[student.id] = t
            if search(rest):
                return True
            current[t].pop()
            del placed[student.id]
        return False

    if search(pending):
        return dict(placed)
    return None


def explain_no_assignment(state: State) -> list[str]:
    """solve가 None일 때의 이유 목록(하나 이상). 해당하는 이유를 아래 순서로 모두 담는다.

    1. 팀마다 못 채운 필수 역할을 맡을 수 있는 학생이 없음(팀 번호 순서)
    2. 남은 자리보다 팀이 없는 학생이 많음
    3. 팀을 2명 이상으로 채울 학생이 모자람
    4. 역할(기획자, 웹 프로그래머)마다 그 역할이 필요한 팀 수보다 맡을 수 있는 학생이 적음
    어느 것에도 안 걸리면 마지막 문구 하나.
    """
    pending = state.unassigned()
    team_order = sorted(state.team_ids)
    members_of = {t: state.team_members(t) for t in team_order}
    reasons: list[str] = []

    # 1) 못 채운 역할을 맡을 수 있고, 주 역할이 팀 안에서 겹치지 않으며, 팀에 자리가 있는 학생이 있는가
    for t in team_order:
        members = members_of[t]
        has_seat = len(members) < TEAM_MAX
        taken_mains = {m.main_role for m in members}
        candidates = [s for s in pending if has_seat and s.main_role not in taken_mains]
        for gap in _role_gaps(members):
            if not any(s.can(role) for s in candidates for role in gap):
                reasons.append(_gap_text(state.team_name(t), gap))

    # 2) 자리 수
    seats = sum(max(0, TEAM_MAX - len(m)) for m in members_of.values())
    if len(pending) > seats:
        reasons.append(f"남은 자리보다 팀이 없는 학생이 많음 (학생 {len(pending)}명, 자리 {seats}개)")

    # 3) 팀마다 2명 이상 만들 학생 수
    needed = sum(max(0, TEAM_MIN - len(m)) for m in members_of.values())
    if needed > len(pending):
        reasons.append(f"팀을 2명 이상으로 채울 학생이 모자람 (필요 {needed}명, 있음 {len(pending)}명)")

    # 4) 역할마다 필요한 학생 수(그 역할을 맡을 팀원이 아직 없는 팀 수)와 맡을 수 있는 학생 수
    for role in (PLANNER, WEB):
        teams_needing = sum(1 for m in members_of.values() if not any(x.can(role) for x in m))
        able = sum(1 for s in pending if s.can(role))
        if teams_needing > able:
            reasons.append(f"{role}를 맡을 학생이 모자람 (필요 {teams_needing}명, 가능 {able}명)")

    return reasons or [NO_REASON]
