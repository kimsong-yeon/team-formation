"""부록 연습 시나리오 3개(쉬움·중간·어려움)를 옮긴 데이터와, 쓰기 처리로 재생하는 도구.

단계는 이 모양의 튜플이다(학생은 CSV의 이름 전체로 부른다).
- ("apply", 학생 이름, 팀 번호, 기대 첫 단어)
- ("vote", 투표자 이름, 지원자 이름, "찬성" | "거부", 기대 첫 단어)
- ("close", 기대 첫 단어)
- ("assign_confirm", 기대 첫 단어): 지금 상태로 solve를 해서 나온 배정안을 확정한다.

부록이 "막힌 지원에는 투표하지 않는다"고 한 투표는 목록에 넣지 않았다.
play는 DB가 없는 MemoryRepo에도, 과제 7의 실제 DB 저장소에도 쓸 수 있도록 Repo 인터페이스만 쓴다.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from teamapp import actions
from teamapp.roster import ADMIN_NO
from teamapp.rules import APPROVED, Outcome, Student
from teamapp.solver import solve

DATA_DIR = Path(__file__).parent / "data"
STUDENT_PASSWORD = "1234"


class Scenario(NamedTuple):
    csv_name: str  # tests/data 안의 명단 CSV 파일 이름
    team_count: int
    steps: tuple[tuple, ...]


EASY = Scenario(
    "쉬움.csv",
    3,
    (
        ("apply", "학생1(PL;RW)", 1, "승인"),
        ("apply", "학생2(PL;PT)", 2, "승인"),
        ("apply", "학생3(PL;TT)", 3, "승인"),
        ("apply", "학생4(WP;FD)", 1, "대기"),
        ("vote", "학생1(PL;RW)", "학생4(WP;FD)", "찬성", "승인"),
        ("apply", "학생5(WP)", 2, "대기"),
        ("vote", "학생2(PL;PT)", "학생5(WP)", "찬성", "승인"),
        ("apply", "학생7(FD;PT)", 1, "대기"),
        ("vote", "학생1(PL;RW)", "학생7(FD;PT)", "찬성", "대기"),
        ("vote", "학생4(WP;FD)", "학생7(FD;PT)", "거부", "거절"),
        ("apply", "학생6(WP;TT)", 3, "대기"),
        ("vote", "학생3(PL;TT)", "학생6(WP;TT)", "찬성", "승인"),
        ("close", "승인"),
        ("assign_confirm", "승인"),
    ),
)

MEDIUM = Scenario(
    "중간.csv",
    4,
    (
        ("apply", "학생1(PL;PT;TT)", 1, "승인"),
        ("apply", "학생5(WP;TT)", 1, "대기"),
        ("vote", "학생1(PL;PT;TT)", "학생5(WP;TT)", "찬성", "승인"),
        ("apply", "학생2(PL;TT)", 1, "막힘"),
        ("apply", "학생6(WP;FD)", 2, "승인"),
        ("apply", "학생9(FD;WP)", 2, "대기"),
        ("vote", "학생6(WP;FD)", "학생9(FD;WP)", "찬성", "승인"),
        ("apply", "학생10(TT;PL)", 2, "대기"),
        ("vote", "학생6(WP;FD)", "학생10(TT;PL)", "찬성", "대기"),
        ("vote", "학생9(FD;WP)", "학생10(TT;PL)", "거부", "거절"),
        ("apply", "학생3(PL;RW)", 3, "승인"),
        ("apply", "학생11(RW)", 4, "승인"),
        ("apply", "학생12(PT;RW;FD)", 4, "대기"),
        ("apply", "학생12(PT;RW;FD)", 3, "막힘"),
        ("close", "승인"),
        ("apply", "학생2(PL;TT)", 3, "막힘"),
        ("assign_confirm", "승인"),
    ),
)

HARD = Scenario(
    "어려움.csv",
    4,
    (
        ("apply", "학생1(PL;RW)", 1, "승인"),
        ("apply", "학생4(WP;PL)", 1, "대기"),
        ("vote", "학생1(PL;RW)", "학생4(WP;PL)", "찬성", "승인"),
        ("apply", "학생7(FD;PT;TT)", 1, "대기"),
        ("vote", "학생1(PL;RW)", "학생7(FD;PT;TT)", "찬성", "대기"),
        ("vote", "학생4(WP;PL)", "학생7(FD;PT;TT)", "찬성", "승인"),
        ("apply", "학생2(PL;PT)", 2, "승인"),
        ("apply", "학생8(FD;TT)", 2, "대기"),
        ("vote", "학생2(PL;PT)", "학생8(FD;TT)", "찬성", "승인"),
        ("apply", "학생5(WP;FD)", 2, "대기"),
        ("apply", "학생6(WP;RW)", 2, "대기"),
        ("vote", "학생2(PL;PT)", "학생5(WP;FD)", "찬성", "대기"),
        ("vote", "학생8(FD;TT)", "학생5(WP;FD)", "찬성", "승인"),
        ("vote", "학생2(PL;PT)", "학생6(WP;RW)", "찬성", "대기"),
        ("vote", "학생8(FD;TT)", "학생6(WP;RW)", "찬성", "대기"),
        ("vote", "학생5(WP;FD)", "학생6(WP;RW)", "찬성", "거절"),
        ("apply", "학생11(TT;PT)", 3, "승인"),
        ("apply", "학생15(PT;FD)", 3, "대기"),
        ("vote", "학생11(TT;PT)", "학생15(PT;FD)", "찬성", "승인"),
        ("apply", "학생14(RW;TT)", 3, "막힘"),
        ("apply", "학생10(TT;RW)", 4, "승인"),
        ("apply", "학생13(RW;PT;FD)", 1, "대기"),
        ("vote", "학생1(PL;RW)", "학생13(RW;PT;FD)", "찬성", "대기"),
        ("close", "승인"),
        ("assign_confirm", "승인"),
    ),
)


def play_full(
    repo: actions.Repo,
    scenario: Scenario,
    hasher: Callable[[str], str],
    verify: Callable[[str, str], bool],
) -> list[tuple[str, Outcome]]:
    """명단을 올리고 단계를 차례로 실행해, 단계(행동)마다 (기대 첫 단어, 실제 결과 Outcome)을 돌려 준다.

    관리자는 저장소에서 학번 admin으로 찾는다(비밀번호는 모른다).
    학생은 처음 행동할 때 비밀번호 1234로 로그인해 계정을 얻는다.
    """
    admin = repo.find_account(ADMIN_NO)
    if admin is None:
        raise RuntimeError("관리자 계정이 없습니다")
    data = (DATA_DIR / scenario.csv_name).read_bytes()
    started = actions.reset_roster(repo, admin.id, data, scenario.team_count, hasher)
    if started.kind != APPROVED:
        raise RuntimeError(f"명단을 올리지 못했습니다: {started.text}")

    by_name = {s.name: s for s in repo.load_state().students.values()}
    actor_ids: dict[str, int] = {}

    def student(name: str) -> Student:
        if name not in by_name:
            raise ValueError(f"명단에 없는 학생: {name}")
        return by_name[name]

    def actor(name: str) -> int:
        if name not in actor_ids:
            outcome, account = actions.login(repo, student(name).student_no, STUDENT_PASSWORD, verify)
            if account is None:
                raise RuntimeError(f"{name} 로그인 실패: {outcome.text}")
            actor_ids[name] = account.id
        return actor_ids[name]

    def latest_application_id(applicant: str) -> int:
        applicant_id = student(applicant).id
        mine = [a.id for a in repo.load_state().applications if a.student_id == applicant_id]
        if not mine:
            raise ValueError(f"{applicant}의 지원이 아직 없음")
        return max(mine)

    results: list[tuple[str, Outcome]] = []
    for step in scenario.steps:
        kind = step[0]
        if kind == "apply":
            _, name, team_id, expected = step
            outcome = actions.apply(repo, actor(name), team_id)
        elif kind == "vote":
            _, voter, applicant, value, expected = step
            outcome = actions.vote(repo, actor(voter), latest_application_id(applicant), value)
        elif kind == "close":
            _, expected = step
            outcome = actions.close(repo, admin.id)
        elif kind == "assign_confirm":
            _, expected = step
            plan = solve(repo.load_state()) or {}
            outcome = actions.confirm(repo, admin.id, plan)
        else:
            raise ValueError(f"알 수 없는 단계: {kind}")
        results.append((expected, outcome))
    return results


def play(
    repo: actions.Repo,
    scenario: Scenario,
    hasher: Callable[[str], str],
    verify: Callable[[str, str], bool],
) -> list[tuple[str, str]]:
    """단계(행동)마다 (기대 첫 단어, 실제 첫 단어)를 돌려 준다."""
    return [(expected, outcome.kind) for expected, outcome in play_full(repo, scenario, hasher, verify)]
