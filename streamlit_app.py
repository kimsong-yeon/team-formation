"""팀 편성 앱 화면(Streamlit): 로그인, 학생 화면, 관리자 화면.

- 화면 요소의 key는 PRD 5.4절 표와 글자 하나까지 같다(채점 프로그램이 key로만 찾는다).
- 규칙 판단은 여기서 하지 않는다. 쓰기는 teamapp.actions, 배정은 teamapp.ai, 트랜잭션은 teamapp.db가 맡는다.
- 결과 안내(key result)는 승인·대기·거절·막힘 가운데 하나로 시작하고, 다음 단추를 누를 때까지 남는다.
- 다른 사람의 변경은 st.fragment(run_every=3)로 3초마다 다시 그린다(학생 화면 전체, 관리자 화면의 현황).
- 오류 원문(DB 주소와 비밀번호가 섞일 수 있음)은 화면에도 로그에도 내보내지 않고 일반 안내만 보여 준다.
- 명단 CSV에서 온 글(이름 등)은 HTML을 해석하지 않는 방법으로만 보여 주고, Markdown 특수 문자는 escape한다.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone

import streamlit as st

from teamapp import actions, ai, config, db
from teamapp.rules import (
    APPROVED,
    BLOCKED,
    NO,
    PENDING,
    REJECTED,
    TEAM_MAX,
    YES,
    Outcome,
    State,
    Student,
    apply_lock_reason,
)

REFRESH_SECONDS = 3  # 다른 사람의 변경을 다시 그리는 간격(초)
KST = timezone(timedelta(hours=9))  # 한국 시간(zoneinfo 없이)

DB_FAILED = f"{BLOCKED}: DB에 연결하지 못했습니다. 잠시 뒤 다시 시도해 주세요"
SETTINGS_MISSING = f"{BLOCKED}: 앱 설정이 없습니다. 관리자에게 알려 주세요"
ASSIGN_FAILED = f"{BLOCKED}: 배정하는 중에 문제가 생겼습니다. 다시 시도해 주세요"
NOT_CLOSED = f"{BLOCKED}: 모집을 먼저 마감해야 합니다"
NOT_ADMIN = f"{BLOCKED}: {actions.NOT_ADMIN}"
NO_STUDENTS = "배정할 학생이 없습니다"  # ai.run_assignment가 돌려주는 이유와 같은 글
NO_VALID_ASSIGNMENT = "규칙을 지키는 배정이 없음"
NO_RESULT_YET = "단추를 누르면 결과가 여기에 나옴"
RESULT_KINDS = (APPROVED, PENDING, REJECTED, BLOCKED)

# 세션 값 이름. 결과 문구는 화면 key "result"와 겹치지 않는 이름으로 둔다.
USER_ID = "user_id"
STUDENT_NO = "student_no"
RESULT = "result_text"
PLAN = "plan"  # 검사를 통과한 배정안(학생 id -> 팀 id)
PLAN_SOURCE = "plan_source"
ASSIGN_LINES = "assign_lines"
ASSIGN_PROBLEMS = "assign_problems"
PLAN_KEYS = (PLAN, PLAN_SOURCE, ASSIGN_LINES, ASSIGN_PROBLEMS)
SESSION_KEYS = (USER_ID, STUDENT_NO, RESULT, *PLAN_KEYS)


# ---- 글 만들기(Streamlit 없이 쓰는 작은 함수) ----

_MARKDOWN_SPECIAL = frozenset("\\`*_[]<>#$|~")  # 강조, 코드, 링크, HTML, 제목, 수식, 표, 취소선
_LIST_MARKER = re.compile(r"^(\s*)([-+]|\d+[.)])")  # 글 맨 앞의 "-", "+", "1." 같은 목록 표시


def md(text: object) -> str:
    """명단 CSV나 AI 응답에서 온 글이 Markdown으로 해석되지 않게 한다(줄바꿈은 빈칸, 특수 문자 앞에 \\)."""
    flat = str(text).replace("\r", " ").replace("\n", " ")
    escaped = "".join("\\" + ch if ch in _MARKDOWN_SPECIAL else ch for ch in flat)
    return _LIST_MARKER.sub(lambda m: f"{m.group(1)}{m.group(2)[:-1]}\\{m.group(2)[-1]}", escaped)


def split_result(text: str) -> tuple[str | None, str]:
    """결과 문구를 (승인·대기·거절·막힘, 나머지)로 나눈다. 그 넷으로 시작하지 않으면 (None, 문구 전체)."""
    kind, sep, message = text.partition(": ")
    if sep and kind in RESULT_KINDS:
        return kind, message
    return None, text


def kst_text(moment: datetime | None) -> str:
    """시각을 한국 시간 "MM-DD HH:MM:SS KST"로. 시간대가 없는 값은 UTC로 본다."""
    if moment is None:
        return "시각 모름"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(KST).strftime("%m-%d %H:%M:%S") + " KST"


def roles_text(roles: Sequence[str]) -> str:
    return ", ".join(md(role) for role in roles) if roles else "없음"


def member_line(student: Student) -> str:
    return f"{md(student.name)} · 주 역할 {md(student.main_role)} · 보조 역할 {roles_text(student.sub_roles)}"


def application_line(student: Student, created_at: datetime | None) -> str:
    return (
        f"{md(student.name)} · 학번 {md(student.student_no)} · 주 역할 {md(student.main_role)} · "
        f"보조 역할 {roles_text(student.sub_roles)} · {kst_text(created_at)}"
    )


def team_title(team_name: str, size: int, mine: bool) -> str:
    title = f"{team_name} ({size}/{TEAM_MAX}명)"
    return f"{title} · 내 팀" if mine else title


def student_status_line(closed: bool, my_team_name: str | None) -> str:
    status = "마감" if closed else "모집 중"
    return f"모집 상태: {status} · 내 팀: {my_team_name or '아직 팀이 없음'} · 화면은 {REFRESH_SECONDS}초마다 저절로 바뀜"


def overview_line(student_count: int, unassigned_count: int) -> str:
    return f"학생 {student_count}명 · 팀이 없는 학생 {unassigned_count}명 · 화면은 {REFRESH_SECONDS}초마다 저절로 바뀜"


def unassigned_line(students: Sequence[Student]) -> str:
    names = ", ".join(md(s.name) for s in students) or "없음"
    return f"팀이 없는 학생 {len(students)}명: {names}"


def wait_text(order: int, model: str, elapsed: float) -> str:
    return f"{order}차 모델({md(model)})의 응답을 기다리는 중... (최대 {ai.TIMEOUT_SECONDS}초, {int(elapsed)}초 지남)"


def assign_result_text(result: ai.AssignResult) -> str:
    """AI 배정 단추의 결과 문구."""
    if result.plan is not None:
        return f"{APPROVED}: 검사를 통과한 배정안을 만들었습니다 (출처: {result.source})"
    if result.problems == [NO_STUDENTS]:
        return f"{BLOCKED}: {NO_STUDENTS}"
    return f"{BLOCKED}: {NO_VALID_ASSIGNMENT}"


def plan_rows(state: State, plan: dict[int, int]) -> list[dict[str, str]]:
    """배정안 표의 줄(이름 · 주 역할 · 보조 역할 · 배정된 팀), 팀 번호와 학번 순서. 표 칸은 Markdown이 아니다."""

    def order(item: tuple[int, int]) -> tuple[int, str]:
        student = state.students.get(item[0])
        return item[1], student.student_no if student else ""

    rows = []
    for student_id, team_id in sorted(plan.items(), key=order):
        student = state.students.get(student_id)
        rows.append(
            {
                "이름": student.name if student else "(명단에 없는 학생)",
                "주 역할": student.main_role if student else "",
                "보조 역할": ", ".join(student.sub_roles) if student and student.sub_roles else "없음",
                "배정된 팀": state.team_name(team_id),
            }
        )
    return rows


def team_count_value(value: object) -> int | None:
    """팀 수 입력칸의 값을 정수로. 정수가 아니면 None(reset_roster가 막힘으로 알린다)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def highlight_css(team_id: int) -> str:
    """내 팀 카드를 진하게 보이는 CSS. 팀 번호(정수)만 들어가고 사용자 글은 넣지 않는다."""
    return (
        f"<style>.st-key-team_{int(team_id)} "
        "{border: 2px solid rgba(128,128,128,.9) !important; background: rgba(128,128,128,.12) !important;}</style>"
    )


# ---- 연결 ----


class MissingSettings(Exception):
    """DATABASE_URL이나 ADMIN_PASSWORD가 없음(값은 담지 않는다)."""


@st.cache_resource(show_spinner="DB에 연결하는 중...")
def get_pool() -> db.ReusingConnectionPool:
    """앱 프로세스에서 한 번만 만드는 연결 풀. 테이블이 없으면 만들고 관리자 계정을 맞춘다.

    예외가 나면 cache_resource는 결과를 저장하지 않으므로 다음 실행에서 다시 해 본다.
    """
    dsn = config.get_secret("DATABASE_URL")
    admin_password = config.get_secret("ADMIN_PASSWORD")
    if not dsn or not admin_password:
        raise MissingSettings()
    pool = db.make_pool(dsn)
    try:
        db.ensure_schema(pool, db.read_schema_sql())
        db.ensure_admin(pool, admin_password)
    except BaseException:
        pool.closeall()
        raise
    return pool


def connect_or_stop() -> db.ReusingConnectionPool:
    """연결 풀을 받는다. 안 되면 일반 안내만 보이고 멈춘다(오류 원문은 보이지 않음)."""
    try:
        return get_pool()
    except MissingSettings:
        message = SETTINGS_MISSING
    except Exception:
        message = DB_FAILED
    st.error(message)
    st.stop()


# ---- 세션 ----


def set_result(text: str) -> None:
    st.session_state[RESULT] = text


def forget(keys: Sequence[str]) -> None:
    for key in keys:
        if key in st.session_state:
            del st.session_state[key]


def clear_plan() -> None:
    forget(PLAN_KEYS)


def clear_session() -> None:
    """로그아웃: 로그인 정보, 결과 문구, 배정안을 모두 지운다."""
    forget(SESSION_KEYS)


def is_session_account(account: actions.Account | None) -> bool:
    """DB의 계정이 세션에 저장한 학번과 같은 사람인지(명단을 다시 올려 다른 학생이 되는 일을 막는다)."""
    return account is not None and account.student_no == st.session_state.get(STUDENT_NO)


def load_view(pool: db.ReusingConnectionPool) -> tuple[actions.Account | None, State | None]:
    """세션의 계정과 지금 상태를 읽기 트랜잭션 하나로 읽는다. 로그인하지 않았거나 계정이 맞지 않으면 (None, None)."""
    user_id = st.session_state.get(USER_ID)
    if user_id is None:
        return None, None
    with db.read_repo(pool) as repo:
        account = repo.get_account(user_id)
        if not is_session_account(account):
            return None, None
        return account, repo.load_state()


def restart_logged_out() -> None:
    """계정이 없어졌거나 다른 사람이 되었으면 세션을 비우고 앱 전체를 다시 실행한다(로그인 화면으로)."""
    clear_session()
    st.rerun()


# ---- 단추 콜백(화면을 다시 그리기 전에 실행) ----


def run_write(pool: db.ReusingConnectionPool, act: Callable[[actions.Repo, int], Outcome]) -> Outcome | None:
    """쓰기 하나(잠긴 트랜잭션 하나)를 하고 결과 문구를 세션에 넣는다.

    세션 계정이 DB와 맞지 않으면 아무것도 하지 않는다(화면을 그릴 때 로그인 화면으로 돌아간다).
    """
    user_id = st.session_state.get(USER_ID)
    if user_id is None:
        return None
    try:
        with db.write_repo(pool) as repo:
            if not is_session_account(repo.get_account(user_id)):
                return None
            outcome = act(repo, user_id)
    except Exception:
        set_result(DB_FAILED)
        return None
    set_result(outcome.text)
    return outcome


def approved(outcome: Outcome | None) -> bool:
    return outcome is not None and outcome.kind == APPROVED


def on_apply(pool: db.ReusingConnectionPool, team_id: int) -> None:
    run_write(pool, lambda repo, user_id: actions.apply(repo, user_id, team_id))


def on_vote(pool: db.ReusingConnectionPool, application_id: int, vote: str) -> None:
    run_write(pool, lambda repo, user_id: actions.vote(repo, user_id, application_id, vote))


def on_start(pool: db.ReusingConnectionPool) -> None:
    def act(repo: actions.Repo, user_id: int) -> Outcome:
        uploaded = st.session_state.get("admin_roster_file")
        data = uploaded.getvalue() if uploaded is not None else None
        team_count = team_count_value(st.session_state.get("admin_team_count"))
        return actions.reset_roster(repo, user_id, data, team_count)

    if approved(run_write(pool, act)):
        clear_plan()


def on_close(pool: db.ReusingConnectionPool) -> None:
    run_write(pool, actions.close)


def on_confirm(pool: db.ReusingConnectionPool) -> None:
    plan = dict(st.session_state.get(PLAN) or {})
    if approved(run_write(pool, lambda repo, user_id: actions.confirm(repo, user_id, plan))):
        clear_plan()


# ---- 공통 화면 조각 ----


def show_whoami(text: str) -> None:
    with st.container(key="whoami"):
        st.markdown(text)


def result_placeholder():
    """결과 안내 자리(key result). 안에 요소 하나만 두려고 st.empty를 쓴다. 채우는 것은 fill_result."""
    return st.container(key="result").empty()


def fill_result(slot) -> None:
    """세션의 마지막 결과 문구를 색 상자로(승인 초록, 대기 파랑, 거절 노랑, 막힘 빨강)."""
    text = st.session_state.get(RESULT)
    if not text:
        slot.caption(NO_RESULT_YET)
        return
    kind, message = split_result(text)
    if kind is None:
        slot.error(md(text))
        return
    show = {APPROVED: slot.success, PENDING: slot.info, REJECTED: slot.warning, BLOCKED: slot.error}[kind]
    show(f"{kind}: {md(message)}")


def team_cards(
    state: State,
    my_team: int | None = None,
    apply_pool: db.ReusingConnectionPool | None = None,
    lock_reason: str | None = None,
) -> None:
    """팀 카드(key team_N)를 옆으로 나란히 그린다. apply_pool이 있으면(학생 화면) 카드마다 지원 단추(key apply_N)."""
    team_ids = sorted(state.team_ids)
    if not team_ids:
        st.caption("아직 팀이 없음")
        return
    for column, team_id in zip(st.columns(len(team_ids)), team_ids):
        members = state.team_members(team_id)
        card = column.container(key=f"team_{team_id}", border=True)
        card.markdown(f"**{team_title(state.team_name(team_id), len(members), team_id == my_team)}**")
        for member in members:
            card.markdown(member_line(member))
        if not members:
            card.markdown("빈 팀")
        if apply_pool is not None:
            card.button(
                f"{state.team_name(team_id)}에 지원",
                key=f"apply_{team_id}",
                disabled=lock_reason is not None,
                on_click=on_apply,
                args=(apply_pool, team_id),
            )


# ---- 로그인 화면 ----


def login_page(pool: db.ReusingConnectionPool) -> None:
    st.title("팀 편성")
    with st.form("login"):
        student_no = st.text_input("학번", key="login_student_no")
        password = st.text_input("비밀번호", type="password", key="login_password")
        submitted = st.form_submit_button("로그인", key="login_submit")
    slot = result_placeholder()
    if submitted:
        log_in(pool, student_no, password)
    fill_result(slot)


def log_in(pool: db.ReusingConnectionPool, student_no: str, password: str) -> None:
    """맞으면 세션에 계정을 넣고 앱을 다시 실행한다. 틀리면 결과 문구만 남긴다."""
    try:
        with db.read_repo(pool) as repo:
            outcome, account = actions.login(repo, student_no or "", password or "")
    except Exception:
        set_result(DB_FAILED)
        return
    if account is None:
        set_result(outcome.text)
        return
    clear_session()
    st.session_state[USER_ID] = account.id
    st.session_state[STUDENT_NO] = account.student_no
    st.rerun()


# ---- 학생 화면 ----


def student_page(pool: db.ReusingConnectionPool, account: actions.Account) -> None:
    show_whoami(f"로그인: {md(account.name)} ({md(account.student_no)})")
    student_body(pool)


@st.fragment(run_every=REFRESH_SECONDS)
def student_body(pool: db.ReusingConnectionPool) -> None:
    """학생 화면 전체. 3초마다(그리고 이 안의 단추를 누를 때마다) 이 부분만 다시 그린다."""
    slot = result_placeholder()
    try:
        account, state = load_view(pool)
    except Exception:
        st.error(DB_FAILED)
        fill_result(slot)
        return
    if account is None:
        restart_logged_out()
        return
    if account.is_admin:
        st.rerun()  # 앱 전체를 다시 실행해 관리자 화면으로
        return

    my_team = state.team_of(account.id)
    st.markdown(student_status_line(state.closed, state.team_name(my_team) if my_team is not None else None))
    if my_team is not None:
        incoming_applications(pool, state, account.id, my_team)

    st.subheader("팀 목록")
    lock_reason = apply_lock_reason(state, account.id)
    if lock_reason is not None:
        st.markdown(f"지원 단추를 쓸 수 없음: {lock_reason}")
    if my_team is not None:
        st.markdown(highlight_css(my_team), unsafe_allow_html=True)
    team_cards(state, my_team=my_team, apply_pool=pool, lock_reason=lock_reason)
    fill_result(slot)


def incoming_applications(pool: db.ReusingConnectionPool, state: State, my_id: int, team_id: int) -> None:
    """내 팀에 들어온 대기 지원마다 한 줄과 찬성·거부 단추(이미 투표했으면 내 투표)."""
    st.subheader("내 팀에 들어온 지원")
    pending = state.pending_for_team(team_id)
    if not pending:
        st.markdown("대기 중인 지원 없음")
        return
    for application in pending:
        applicant = state.students.get(application.student_id)
        if applicant is None:
            continue
        line_column, yes_column, no_column = st.columns([6, 1, 1])
        line_column.markdown(application_line(applicant, application.created_at))
        my_vote = state.votes.get(application.id, {}).get(my_id)
        if my_vote is not None:
            yes_column.markdown(f"내 투표: {my_vote}")
            continue
        yes_column.button(
            "찬성",
            key=f"vote_yes_{applicant.student_no}",
            on_click=on_vote,
            args=(pool, application.id, YES),
        )
        no_column.button(
            "거부",
            key=f"vote_no_{applicant.student_no}",
            on_click=on_vote,
            args=(pool, application.id, NO),
        )


# ---- 관리자 화면 ----


def admin_page(pool: db.ReusingConnectionPool, state: State) -> None:
    show_whoami("로그인: 관리자 (admin)")
    slot = result_placeholder()
    roster_section(pool)
    assign_section(pool, state, slot)
    fill_result(slot)
    admin_overview(pool)


def roster_section(pool: db.ReusingConnectionPool) -> None:
    st.subheader("명단 올리기")
    st.caption(
        "명단 CSV(UTF-8, 머리글 `student_no,name,main_role,sub_roles`)와 팀 수를 넣고 단추를 누르면 "
        "지원·투표·팀원·마감 표시를 모두 지우고 처음 상태를 만듭니다."
    )
    st.file_uploader("명단 CSV", type=["csv"], key="admin_roster_file")
    st.number_input(
        "팀 수",
        min_value=actions.MIN_TEAM_COUNT,
        max_value=actions.MAX_TEAM_COUNT,
        value=3,
        step=1,
        key="admin_team_count",
    )
    st.button("명단으로 처음 상태 만들기", key="admin_start", on_click=on_start, args=(pool,))


def assign_section(pool: db.ReusingConnectionPool, state: State, result_slot) -> None:
    st.subheader("마감과 배정")
    st.markdown(f"모집 상태: {'마감' if state.closed else '모집 중'}")
    st.button("모집 마감", key="admin_close", on_click=on_close, args=(pool,))
    clicked = st.button("AI 배정", key="admin_assign")
    status_box = st.container(key="assign_status")
    fresh = run_ai_assignment(pool, status_box, result_slot) if clicked else None
    if fresh is None:
        show_assign_status(status_box)
    else:
        state = fresh
    show_plan(pool, state)


def run_ai_assignment(pool: db.ReusingConnectionPool, status_box, result_slot) -> State | None:
    """AI 배정 단추. 콜백이 아니라 본문에서 바로 실행하며 진행 상황을 assign_status에 그린다(몇 분 걸릴 수 있음).

    배정을 시작했으면 그때 읽은 상태를, 시작하지 못했으면(관리자 아님, 마감 전, DB 오류) None을 돌려준다.
    AI를 기다리는 동안에는 DB 연결이나 트랜잭션을 쥐고 있지 않는다.
    """
    user_id = st.session_state.get(USER_ID)
    try:
        with db.read_repo(pool) as repo:
            account = repo.get_account(user_id) if user_id is not None else None
            state = repo.load_state()
    except Exception:
        set_result(DB_FAILED)
        return None
    if not is_session_account(account) or not account.is_admin:
        set_result(NOT_ADMIN)
        return None
    if not state.closed:
        set_result(NOT_CLOSED)
        return None

    clear_plan()
    result_slot.caption("AI 배정 중... 끝나면 결과가 여기에 나옴")
    wait_slot = status_box.empty()

    def on_wait(order: int, model: str, elapsed: float) -> None:
        wait_slot.info(wait_text(order, model, elapsed))

    def on_line(line: str) -> None:
        status_box.markdown(md(line))

    try:
        result = ai.run_assignment(
            state,
            config.get_secret("OPENROUTER_API_KEY") or "",
            on_line=on_line,
            on_wait=on_wait,
        )
    except Exception:
        wait_slot.empty()
        status_box.markdown("배정하는 중에 문제가 생겨 멈췄습니다")
        set_result(ASSIGN_FAILED)
        return state
    wait_slot.empty()

    st.session_state[ASSIGN_LINES] = list(result.lines)
    st.session_state[ASSIGN_PROBLEMS] = list(result.problems)
    if result.plan is not None:
        st.session_state[PLAN] = dict(result.plan)
        st.session_state[PLAN_SOURCE] = result.source
    with status_box:
        show_problems(result.problems)
    set_result(assign_result_text(result))
    return state


def show_assign_status(status_box) -> None:
    """단추를 누르지 않은 실행: 지난 AI 배정의 줄과 이유를 다시 그린다."""
    lines = st.session_state.get(ASSIGN_LINES) or []
    problems = st.session_state.get(ASSIGN_PROBLEMS) or []
    with status_box:
        if not lines and not problems:
            st.caption("AI 배정 단추를 누르면 진행 상황이 여기에 나옴")
            return
        for line in lines:
            st.markdown(md(line))
        show_problems(problems)


def show_problems(problems: Sequence[str]) -> None:
    """배정안을 만들지 못한 이유(지금 컨테이너 안에 그린다)."""
    if not problems:
        return
    if list(problems) == [NO_STUDENTS]:
        st.markdown(NO_STUDENTS)
        return
    st.markdown(NO_VALID_ASSIGNMENT)
    for problem in problems:
        st.markdown(f"- {md(problem)}")


def show_plan(pool: db.ReusingConnectionPool, state: State) -> None:
    """검사를 통과한 배정안이 있으면 표, 출처, 확정 단추."""
    plan = st.session_state.get(PLAN)
    if not plan:
        return
    st.markdown("검사를 통과한 배정안")
    st.dataframe(plan_rows(state, plan), hide_index=True)
    st.markdown(f"출처: {md(st.session_state.get(PLAN_SOURCE, ''))}")
    st.button("배정안 확정", key="admin_confirm", on_click=on_confirm, args=(pool,))


@st.fragment(run_every=REFRESH_SECONDS)
def admin_overview(pool: db.ReusingConnectionPool) -> None:
    """현황: 팀이 없는 학생과 팀 카드. 3초마다 이 부분만 다시 그린다."""
    st.subheader("현황")
    try:
        account, state = load_view(pool)
    except Exception:
        st.error(DB_FAILED)
        return
    if account is None:
        restart_logged_out()
        return
    if not account.is_admin:
        st.rerun()  # 앱 전체를 다시 실행해 학생 화면으로
        return
    free = state.unassigned()
    st.markdown(overview_line(len(state.students), len(free)))
    with st.container(key="unassigned"):
        st.markdown(unassigned_line(free))
    team_cards(state)


# ---- 시작 ----


def read_view_or_stop(pool: db.ReusingConnectionPool) -> tuple[actions.Account | None, State | None]:
    try:
        return load_view(pool)
    except Exception:
        pass
    st.error(DB_FAILED)
    st.stop()


def main() -> None:
    st.set_page_config(page_title="팀 편성", layout="wide")
    pool = connect_or_stop()
    account, state = read_view_or_stop(pool)
    if account is None:
        if st.session_state.get(USER_ID) is not None:
            clear_session()  # 명단을 다시 올려 계정이 없어졌거나 다른 사람이 됨
        login_page(pool)
        return
    st.sidebar.button("로그아웃", key="logout", on_click=clear_session)
    if account.is_admin:
        admin_page(pool, state)
    else:
        student_page(pool, account)


if __name__ == "__main__":  # streamlit run과 AppTest는 이 파일을 __main__으로 실행한다
    main()
