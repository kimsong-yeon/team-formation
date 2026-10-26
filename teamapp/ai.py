"""AI 배정: 오픈라우터의 무료 모델 1차 -> 2차 -> 3차에게 배정안을 받고, 모두 실패하면 앱이 직접 배정한다.

- 모델마다 요청을 시작한 때부터 전체 대기 시간을 재서 TIMEOUT_SECONDS(120초)가 넘으면 기다리지 않고 끊는다.
- 시간 초과, HTTP 오류(429 포함), 빈 응답, 읽을 수 없는 JSON, 검사를 통과하지 못한 배정안은 모두 "실패"이며
  다음 모델로 넘어간다. 모델마다 결과를 줄 하나로 남긴다(화면의 assign_status에 보여 줄 글).
- 검사는 앱의 다섯 가지 검사(solver.validate_plan)로 한다. 3차 모델까지 실패하면 solver.solve가 모든 경우를 따져 배정한다.
- 가장 오래 걸려도 120초 x 3 + 앱의 직접 배정이라 10분 안에 끝난다.

DB와 Streamlit에 의존하지 않는다(teamapp.rules, teamapp.solver, requests와 표준 라이브러리만 쓴다).
테스트에서는 call에 가짜 함수를 넘겨 네트워크를 쓰지 않는다.
API 키는 요청 머리글에만 쓰이고 프롬프트, 줄 문구, 오류 문구 어디에도 들어가지 않는다.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import requests

from teamapp.rules import PLANNER, TEAM_MAX, TEAM_MIN, WEB, State, Student
from teamapp.solver import explain_no_assignment, solve, validate_plan

# 1차, 2차, 3차 모델 (모두 :free 모델이며, 무료 모델로 자동 연결하는 오픈라우터의 free 라우터는 쓰지 않는다)
MODELS: tuple[str, str, str] = (
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "poolside/laguna-s-2.1:free",
)
TIMEOUT_SECONDS = 120
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# 팀이 없는 학생이 없고 지금 팀이 모두 규칙을 지킬 때의 빈 배정안(지금 팀 구성 그대로 확정)
NOTHING_TO_ASSIGN_SOURCE = "앱(배정할 학생 없음)"
NOTHING_TO_ASSIGN_LINE = "배정할 학생이 없어 지금 팀 구성을 그대로 확정할 수 있음"

# requests의 (연결, 읽기) 제한. 읽기 제한은 한 번 기다리는 시간이라 전체 120초 제한은 run_with_deadline이 맡는다.
_REQUEST_TIMEOUT = (10, 130)
_TICK_SECONDS = 1.0
_ERROR_TEXT_LIMIT = 80

_TIMEOUT = "시간 초과"
_UNREADABLE = "JSON을 읽을 수 없음"
_EMPTY = "빈 응답"
_CONNECTION = "연결 오류"
_FENCE = re.compile(r"```[A-Za-z0-9_-]*")


class ModelFailure(Exception):
    """모델 하나가 실패한 이유. reason이 줄 문구에 그대로 들어간다."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class AssignResult:
    """배정 결과.

    plan: 학생 id -> 팀 id (배정할 수 없으면 None, 배정할 학생이 없고 팀이 모두 규칙을 지키면 {}),
    source: 어디서 나온 배정인지,
    lines: 모델마다 남긴 줄, problems: plan이 None일 때의 이유 목록.
    """

    plan: dict[int, int] | None
    source: str
    lines: list[str]
    problems: list[str]


# ---- 요청 만들기 ----
def _describe(student: Student) -> str:
    subs = ", ".join(student.sub_roles) if student.sub_roles else "없음"
    return f"학번 {student.student_no} · 주 역할 {student.main_role} · 보조 역할 {subs}"


def build_messages(state: State) -> list[dict]:
    """모델에게 보낼 메시지. 규칙(system), 지금 팀 구성과 팀이 없는 학생, 답 형식(user)."""
    system = "\n".join(
        [
            "너는 팀 편성 도우미이다. 팀이 없는 학생을 팀에 배정하는 배정안을 만든다.",
            "규칙:",
            f"1. 모든 팀은 {TEAM_MIN}~{TEAM_MAX}명이어야 한다(빈 팀 없음).",
            "2. 주 역할이 같은 학생이 한 팀에 2명 이상이면 안 된다.",
            f"3. 팀마다 {PLANNER}를 맡을 학생과 {WEB}를 맡을 학생이 서로 다른 사람으로 1명씩 있어야 한다. "
            "주 역할이나 보조 역할에 그 역할이 있으면 맡을 수 있다.",
            "4. 이미 팀에 있는 학생은 옮기지 않음. 팀이 없는 학생만 배정한다.",
            "5. 팀이 없는 학생을 빠짐없이 한 팀씩 배정. 한 학생을 두 팀에 넣지 않는다.",
            "설명, 머리말, 코드 블록 없이 JSON만 답함.",
        ]
    )

    lines = ["지금 팀 구성이다."]
    for team_id in sorted(state.team_ids):
        members = state.team_members(team_id)
        free_seats = max(0, TEAM_MAX - len(members))
        lines.append(f"{state.team_name(team_id)} (현재 {len(members)}명, 남은 자리 {free_seats}개)")
        if members:
            lines.extend(f"- {_describe(m)}" for m in members)
        else:
            lines.append("- 팀원 없음")
    free = state.unassigned()
    lines.append("")
    lines.append(f"팀이 없는 학생 {len(free)}명이다.")
    lines.extend(f"- {_describe(s)}" for s in free)
    lines.append("")
    lines.append("팀이 없는 학생 모두를 팀에 배정하고, 아래 형식의 JSON만 답하라.")
    lines.append('{"assignments": [{"student_no": "...", "team": 1}]}')
    lines.append("student_no는 위 목록에 있는 학번 문자열이고, team은 팀 번호(정수)이다.")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(lines)},
    ]


# ---- 응답 읽기 ----
def _student_no_text(value) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value.strip() or None
    return None


def _team_number(value) -> int | None:
    """정수, 숫자 문자열, "팀2" 같은 글에서 팀 번호를 꺼낸다. 숫자가 없으면 None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        digits = re.search(r"\d+", value)
        if digits is None:
            return None
        try:
            return int(digits.group())
        except ValueError:  # 자릿수가 터무니없이 많은 숫자
            return None
    return None


def parse_assignments(text: str | None, state: State) -> list[tuple[int, int]]:
    """모델의 응답 글에서 (학생 id, 팀 id) 쌍들을 답한 순서대로 꺼낸다.

    중복과 없는 팀 번호는 그대로 두고 validate_plan이 알리게 한다.
    """
    if text is None or (isinstance(text, str) and not text.strip()):
        raise ModelFailure(_EMPTY)
    if not isinstance(text, str):
        raise ModelFailure(_UNREADABLE)

    body = _FENCE.sub("", text)  # ```json 같은 코드 블록 표시를 없앤다
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end < start:
        raise ModelFailure(_UNREADABLE)
    try:
        data = json.loads(body[start : end + 1])
    except (ValueError, RecursionError):
        raise ModelFailure(_UNREADABLE) from None
    items = data.get("assignments") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ModelFailure(_UNREADABLE)

    answers: list[tuple[str, int]] = []
    for item in items:
        if not isinstance(item, dict) or "student_no" not in item or "team" not in item:
            raise ModelFailure(_UNREADABLE)
        student_no = _student_no_text(item["student_no"])
        team = _team_number(item["team"])
        if student_no is None or team is None:
            raise ModelFailure(_UNREADABLE)
        answers.append((student_no, team))

    id_of = {s.student_no.strip(): s.id for s in state.students.values()}
    pairs = []
    for student_no, team in answers:
        if student_no not in id_of:
            raise ModelFailure(f"모르는 학번 {student_no}")
        pairs.append((id_of[student_no], team))
    return pairs


# ---- 기다리기와 호출 ----
def run_with_deadline(
    fn: Callable[[], str],
    seconds: float,
    on_tick: Callable[[float], None] | None = None,
) -> str:
    """fn을 별도 스레드에서 실행해 결과를 돌려준다(fn이 던진 예외는 그대로 다시 던진다).

    기다리는 동안 1초마다(seconds가 1보다 작으면 그만큼마다) on_tick(지난 초)를 부른다.
    seconds가 지나도 fn이 끝나지 않으면 fn을 기다리지 않고 바로 ModelFailure("시간 초과(N초)")를 던진다.
    fn의 스레드는 데몬이라 남아 있어도 프로그램이 끝나는 것을 막지 않는다.
    """
    done = threading.Event()
    box: dict[str, object] = {}

    def worker() -> None:
        try:
            box["result"] = fn()
        except BaseException as error:  # 호출한 쪽에서 다시 던진다
            box["error"] = error
        finally:
            done.set()

    threading.Thread(target=worker, name="ai-call", daemon=True).start()
    started = time.monotonic()
    while True:
        remaining = seconds - (time.monotonic() - started)
        if remaining <= 0:
            if done.is_set():
                break
            raise ModelFailure(f"{_TIMEOUT}({seconds:g}초)")
        if done.wait(min(_TICK_SECONDS, remaining)):
            break
        if on_tick is not None:
            on_tick(time.monotonic() - started)

    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["result"]  # type: ignore[return-value]


def _json_body(response):
    try:
        return response.json()
    except ValueError:
        return None


def _error_text(payload, api_key: str) -> str:
    """응답 본문의 error.message를 한 줄로 줄여 앞 80자만. 없으면 빈 글. 키가 섞여 있으면 지운다."""
    error = payload.get("error") if isinstance(payload, dict) else None
    message = error.get("message") if isinstance(error, dict) else error
    if not isinstance(message, str):
        return ""
    text = " ".join(message.split()).replace(api_key, "***")
    return text[:_ERROR_TEXT_LIMIT]


def call_openrouter(model: str, messages: list[dict], api_key: str | None) -> str:
    """오픈라우터에 한 번 요청해 답 글을 돌려준다. 실패하면 ModelFailure(이유).

    키는 요청 머리글에만 쓰고 어떤 이유 문구에도 넣지 않는다.
    """
    key = (api_key or "").strip()
    if not key:
        raise ModelFailure("API 키 없음")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-Title": "team-formation",
    }
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": 4000,
        "reasoning": {"effort": "low"},
        "temperature": 0,
    }
    try:
        response = requests.post(OPENROUTER_URL, headers=headers, json=body, timeout=_REQUEST_TIMEOUT)
    except requests.exceptions.RequestException:
        # 오류 원문은 버린다(접속 정보가 섞일 수 있음).
        raise ModelFailure(_CONNECTION) from None

    if response.status_code == 429:
        raise ModelFailure("HTTP 429(사용량 제한)")
    payload = _json_body(response)
    if response.status_code != 200:
        message = _error_text(payload, key)
        raise ModelFailure(f"HTTP {response.status_code} ({message})" if message else f"HTTP {response.status_code}")
    if isinstance(payload, dict) and payload.get("error"):
        message = _error_text(payload, key)
        raise ModelFailure(f"오류 ({message})" if message else "오류")

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = None
    if not isinstance(content, str) or not content.strip():
        raise ModelFailure(_EMPTY)
    return content


# ---- 배정 ----
def _ask_model(
    state: State,
    messages: list[dict],
    api_key: str,
    model: str,
    call: Callable[[str, list[dict], str], str],
    timeout: float,
    on_tick: Callable[[float], None],
) -> dict[int, int]:
    """모델 하나에게 배정안을 받아 검사한다. 통과하면 배정(학생 id -> 팀 id), 아니면 ModelFailure."""
    try:
        text = run_with_deadline(lambda: call(model, messages, api_key), timeout, on_tick)
    except ModelFailure:
        raise
    except Exception:
        # 어떤 예외든 원문은 버린다(접속 정보나 키가 섞일 수 있음).
        raise ModelFailure(_CONNECTION) from None
    pairs = parse_assignments(text, state)
    problems = validate_plan(state, pairs)
    if problems:
        raise ModelFailure(f"검사 실패: {problems[0]}")
    return dict(pairs)


def run_assignment(
    state: State,
    api_key: str,
    *,
    on_line: Callable[[str], None] = lambda line: None,
    on_wait: Callable[[int, str, float], None] = lambda order, model, elapsed: None,
    call: Callable[[str, list[dict], str], str] = call_openrouter,
    models: tuple[str, ...] = MODELS,
    timeout: float = TIMEOUT_SECONDS,
) -> AssignResult:
    """팀이 없는 학생의 배정을 만든다: 규칙을 지키는 배정이 있는지 보고, AI 1차 -> 2차 -> 3차, 마지막에 앱.

    팀이 없는 학생이 없으면 모델을 부르지 않는다: 지금 팀이 모두 규칙을 지키면 빈 배정안({}),
    아니면 plan=None과 이유(explain_no_assignment).
    on_line(줄): 줄이 만들어지는 대로 부른다. on_wait(몇 차, 모델, 기다린 초): 모델마다 요청 전에 0초로,
    기다리는 동안은 1초마다 부른다.
    """
    lines: list[str] = []

    def log(line: str) -> None:
        lines.append(line)
        on_line(line)

    # 팀이 없는 학생이 없으면 AI를 부르지 않는다. 지금 팀이 모두 규칙을 지키면 빈 배정안(지금 팀 구성 그대로)을
    # 확정할 수 있게 돌려주고, 아니면 "규칙을 지키는 배정이 없음"의 이유를 돌려준다.
    if not state.unassigned():
        if validate_plan(state, []):
            return AssignResult(None, "", [], explain_no_assignment(state))
        log(NOTHING_TO_ASSIGN_LINE)
        return AssignResult({}, NOTHING_TO_ASSIGN_SOURCE, lines, [])
    # 규칙을 지키는 배정이 없으면 AI를 부르지 않는다(호출 횟수를 아낌).
    if solve(state) is None:
        return AssignResult(None, "", [], explain_no_assignment(state))

    messages = build_messages(state)

    for order, model in enumerate(models, start=1):
        label = f"{order}차 모델({model})"
        on_wait(order, model, 0)
        started = time.monotonic()
        try:
            plan = _ask_model(
                state, messages, api_key, model, call, timeout,
                lambda elapsed, order=order, model=model: on_wait(order, model, elapsed),
            )
        except ModelFailure as failure:
            following = f"{order + 1}차 모델로 넘어감" if order < len(models) else "앱이 직접 배정함"
            reason = failure.reason
            if not reason.startswith(_TIMEOUT):  # 시간 초과 문구에는 이미 초가 들어 있다
                reason = f"{reason} ({int(round(time.monotonic() - started))}초)"
            log(f"{label}: {reason} → {following}")
            continue
        seconds = int(round(time.monotonic() - started))
        log(f"{label}: 검사 통과 ({seconds}초)")
        return AssignResult(plan, label, lines, [])

    # 모든 모델이 실패했다: 앱이 모든 경우를 따져 배정한다(같은 상태에서는 항상 같은 결과).
    plan = solve(state)
    if plan is None:
        log("3차 모델까지 실패했고 앱도 규칙을 지키는 배정을 찾지 못함")
        return AssignResult(None, "", lines, explain_no_assignment(state))
    log("3차 모델까지 실패하여 앱이 모든 경우를 따져 배정함")
    return AssignResult(plan, "앱(모든 경우를 따진 배정)", lines, [])
