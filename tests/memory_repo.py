"""DB 없이 쓰는 메모리 저장소(MemoryRepo). 테스트용이다.

DB의 제약을 ValueError로 흉내 낸다(실제 DB 저장소가 같은 행동을 해야 한다).
- 팀원: 한 팀 4자리(slot_no 1~4, 비어 있는 가장 작은 자리), 한 학생은 한 팀에만
- 지원: 학생마다 대기 지원은 1개만
- 투표: (지원, 투표자)마다 한 번만
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta

from teamapp.actions import Account, Repo
from teamapp.roster import ADMIN_NO, RosterRow
from teamapp.rules import PENDING, TEAM_MAX, Application, State, Student

_CLOCK_START = datetime(2026, 11, 1, 9, 0, 0)


def fake_hash(password: str) -> str:
    """테스트용 해시기(bcrypt를 쓰지 않음)."""
    return "h:" + password


def fake_verify(password: str, password_hash: str) -> bool:
    """fake_hash와 짝이 되는 확인 함수."""
    return password_hash == "h:" + password


class MemoryRepo(Repo):
    def __init__(self, admin_password: str = "adminpw", hasher: Callable[[str], str] = fake_hash):
        # 관리자 계정은 id 0이고 명단을 다시 올려도 남는다.
        self._admin = Account(0, ADMIN_NO, "관리자", hasher(admin_password), True)
        self._accounts: dict[int, Account] = {0: self._admin}
        self._students: dict[int, Student] = {}
        self._team_ids: tuple[int, ...] = ()
        self._slots: dict[int, dict[int, int]] = {}  # 팀 -> {slot_no: 학생 id}
        self._applications: dict[int, Application] = {}
        self._votes: dict[int, dict[int, str]] = {}
        self._closed = False
        self._last_application_id = 0  # 명단을 다시 올려도 되돌리지 않는다

    # ---- 읽기 ----
    def load_state(self) -> State:
        return State(
            students=dict(self._students),
            team_ids=self._team_ids,
            members={t: tuple(self._slots[t][slot] for slot in sorted(self._slots[t])) for t in self._team_ids},
            applications=tuple(self._applications[i] for i in sorted(self._applications)),
            votes={app_id: dict(v) for app_id, v in self._votes.items()},
            closed=self._closed,
        )

    def find_account(self, student_no: str) -> Account | None:
        for account in self._accounts.values():
            if account.student_no == student_no:
                return account
        return None

    def get_account(self, user_id: int) -> Account | None:
        return self._accounts.get(user_id)

    # ---- 쓰기 ----
    def insert_application(self, team_id: int, student_id: int, status: str, reason: str) -> int:
        if team_id not in self._slots:
            raise ValueError(f"없는 팀 {team_id}")
        if student_id not in self._students:
            raise ValueError(f"없는 학생 {student_id}")
        if status == PENDING and any(
            a.student_id == student_id and a.status == PENDING for a in self._applications.values()
        ):
            raise ValueError(f"학생 {student_id}의 대기 지원이 이미 있음")
        self._last_application_id += 1
        app_id = self._last_application_id
        self._applications[app_id] = Application(
            app_id, team_id, student_id, status, reason, _CLOCK_START + timedelta(seconds=app_id)
        )
        return app_id

    def add_member(self, team_id: int, student_id: int) -> None:
        if team_id not in self._slots:
            raise ValueError(f"없는 팀 {team_id}")
        if student_id not in self._students:
            raise ValueError(f"없는 학생 {student_id}")
        if any(student_id in slots.values() for slots in self._slots.values()):
            raise ValueError(f"학생 {student_id}는 이미 팀이 있음")
        taken = self._slots[team_id]
        free = [slot for slot in range(1, TEAM_MAX + 1) if slot not in taken]
        if not free:
            raise ValueError(f"팀{team_id}가 꽉 참")
        taken[free[0]] = student_id

    def remove_member(self, student_id: int) -> None:
        for slots in self._slots.values():
            for slot, member_id in list(slots.items()):
                if member_id == student_id:
                    del slots[slot]

    def record_vote(self, application_id: int, voter_id: int, vote: str) -> None:
        if application_id not in self._applications:
            raise ValueError(f"없는 지원 {application_id}")
        votes = self._votes.setdefault(application_id, {})
        if voter_id in votes:
            raise ValueError(f"지원 {application_id}에 {voter_id}가 이미 투표함")
        votes[voter_id] = vote

    def set_application_status(self, application_id: int, status: str, reason: str) -> None:
        if application_id not in self._applications:
            raise ValueError(f"없는 지원 {application_id}")
        self._applications[application_id] = replace(
            self._applications[application_id], status=status, reason=reason
        )

    def set_closed(self) -> None:
        self._closed = True

    def reset(self, rows: list[RosterRow], password_hashes: list[str], team_count: int) -> None:
        if len(rows) != len(password_hashes):
            raise ValueError("학생 수와 비밀번호 해시 수가 다름")
        self._accounts = {0: self._admin}
        self._students = {}
        for student_id, (row, password_hash) in enumerate(zip(rows, password_hashes), start=1):
            self._students[student_id] = Student(
                student_id, row.student_no, row.name, row.main_role, tuple(row.sub_roles)
            )
            self._accounts[student_id] = Account(student_id, row.student_no, row.name, password_hash, False)
        self._team_ids = tuple(range(1, team_count + 1))
        self._slots = {team_id: {} for team_id in self._team_ids}
        self._applications = {}
        self._votes = {}
        self._closed = False
