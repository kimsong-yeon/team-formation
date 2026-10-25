"""명단 CSV 읽기: 관리자가 올린 CSV 바이트를 검사해 학생 줄 목록으로 바꾼다.

DB와 Streamlit에 의존하지 않는 순수 파이썬 모듈이다.
머리글은 student_no,name,main_role,sub_roles 이고, 보조 역할은 ';'로 나눈다(없으면 빈 칸).
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from teamapp.rules import ROLES

HEADER = ["student_no", "name", "main_role", "sub_roles"]
ADMIN_NO = "admin"  # 관리자 로그인에 쓰는 학번이라 학생 학번으로 못 쓴다
MAX_SUB_ROLES = 2


@dataclass(frozen=True)
class RosterRow:
    """명단의 학생 한 줄."""

    student_no: str
    name: str
    main_role: str
    sub_roles: tuple[str, ...]


class RosterError(ValueError):
    """명단 CSV 형식 오류. str(error)가 화면에 보여 줄 문구 그대로이다."""


def _is_blank(row: list[str]) -> bool:
    return all(not cell.strip() for cell in row)


def _check_role(role: str, line_no: int) -> None:
    if role not in ROLES:
        raise RosterError(f"{line_no}번째 줄: 모르는 역할 '{role}'")


def _parse_sub_roles(cell: str, main_role: str, line_no: int) -> tuple[str, ...]:
    """보조 역할 칸을 읽는다. 주 역할과 같은 것은 빼고, 겹친 것은 하나로 줄인다."""
    subs: list[str] = []
    for item in cell.split(";"):
        role = item.strip()
        if not role:
            continue
        _check_role(role, line_no)
        if role != main_role and role not in subs:
            subs.append(role)
    if len(subs) > MAX_SUB_ROLES:
        raise RosterError(f"{line_no}번째 줄: 보조 역할은 {MAX_SUB_ROLES}개까지입니다")
    return tuple(subs)


def parse_roster(data: bytes) -> list[RosterRow]:
    """명단 CSV 바이트(UTF-8, BOM 있어도 됨)를 읽어 파일 순서대로 돌려 준다.

    줄 번호는 csv.reader가 내주는 줄을 세며 머리글이 1번째 줄이다.
    완전히 빈 줄은 건너뛰지만 번호에는 센다. 첫 오류에서 RosterError를 던진다.
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise RosterError("UTF-8 CSV 파일이 아닙니다") from None

    rows = [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(text, newline=""))]
    if all(_is_blank(row) for row in rows):  # 빈 파일 포함: 머리글도 없다
        raise RosterError("명단에 학생이 없습니다")
    if rows[0] != HEADER:
        raise RosterError(f"머리글은 {','.join(HEADER)} 이어야 합니다")

    result: list[RosterRow] = []
    seen: set[str] = set()
    for line_no, row in enumerate(rows[1:], start=2):
        if _is_blank(row):
            continue
        if len(row) != len(HEADER):
            raise RosterError(f"{line_no}번째 줄: 열이 {len(HEADER)}개가 아닙니다")
        student_no, name, main_role, sub_cell = row
        if not student_no:
            raise RosterError(f"{line_no}번째 줄: 학번이 비어 있습니다")
        if student_no == ADMIN_NO:
            raise RosterError(f"{line_no}번째 줄: {ADMIN_NO}은 학번으로 쓸 수 없습니다")
        if student_no in seen:
            raise RosterError(f"{line_no}번째 줄: 학번 {student_no}이 겹칩니다")
        seen.add(student_no)
        if not name:
            raise RosterError(f"{line_no}번째 줄: 이름이 비어 있습니다")
        if not main_role:
            raise RosterError(f"{line_no}번째 줄: 주 역할이 비어 있습니다")
        _check_role(main_role, line_no)
        result.append(
            RosterRow(student_no, name, main_role, _parse_sub_roles(sub_cell, main_role, line_no))
        )

    if not result:
        raise RosterError("명단에 학생이 없습니다")
    return result
