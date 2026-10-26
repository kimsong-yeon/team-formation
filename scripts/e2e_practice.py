"""배포한 앱에서 부록 연습용 시나리오를 채점 프로그램처럼 화면 key로 눌러 보는 도구(수강생 컴퓨터에서 실행).

- 화면 요소는 Streamlit key가 붙은 CSS 클래스(st-key-<key>)로만 찾는다(PRD 5.4절 표).
- 관리자와 학생마다 브라우저 창(context)을 따로 연다. 학생 비밀번호는 명단 공통 1234.
- 단계마다 결과 안내(result)의 첫 단어를 기대 결과와 비교해 표로 보여 주고, 화면을 practice_runs/ 아래에 저장한다.
- 확정 뒤에는 .env의 DATABASE_URL로 DB를 읽어 모든 팀이 규칙을 지키는지 검사한다.
- 관리자 비밀번호와 DB 주소는 .env에서 읽어 쓰기만 하고 출력하지 않는다.
- 주의: 배포한 앱의 DB를 바꾼다(명단을 다시 올림). AI 배정은 시나리오마다 1번(무료 호출 1~3회).

준비(한 번만):
  .venv\\Scripts\\python -m pip install playwright
  .venv\\Scripts\\python -m playwright install chromium
실행(맨 위 폴더에서):
  .venv\\Scripts\\python scripts\\e2e_practice.py https://<앱 주소>.streamlit.app
  .venv\\Scripts\\python scripts\\e2e_practice.py https://<앱 주소>.streamlit.app 쉬움      (하나만)
  .venv\\Scripts\\python scripts\\e2e_practice.py --dry-run                                  (단계만 보기)
"""
from __future__ import annotations

import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from teamapp import config  # noqa: E402
from teamapp.roster import ADMIN_NO, parse_roster  # noqa: E402
from tests.scenarios import DATA_DIR, EASY, HARD, MEDIUM, STUDENT_PASSWORD  # noqa: E402

SCENARIOS = {"쉬움": EASY, "중간": MEDIUM, "어려움": HARD}
WORDS = ("승인", "대기", "거절", "막힘")
ADMIN = "관리자"
BUTTON_WAIT = 12  # 다른 사람의 변경이 화면에 나타나기를 기다리는 시간(초). 앱은 3초마다 다시 그림
SAME_TEXT_WAIT = 8  # 결과 문구가 앞 결과와 같을 수 있을 때 기다리는 시간(초)
AI_WAIT = 600  # AI 배정 전체를 기다리는 시간(초)


def first_word(text: str) -> str:
    return text.split(":", 1)[0].strip() if text else ""


def one_line(text: str, limit: int = 70) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Actor:
    """브라우저 창 하나(가상 학생이나 관리자 한 명)."""

    def __init__(self, browser, url: str, label: str, shots: Path):
        self.label = label
        self.shots = shots
        self.context = browser.new_context(viewport={"width": 1400, "height": 2000}, locale="ko-KR")
        self.page = self.context.new_page()
        self.page.set_default_timeout(30_000)
        self.page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        self.frame = self._find_app_frame(timeout=240)

    def _find_app_frame(self, timeout: float):
        """앱 화면이 들어 있는 frame을 찾는다(Community Cloud는 앱을 iframe 안에 보여 줄 수 있음). 잠든 앱은 깨운다."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            wake = self.page.get_by_role("button", name=re.compile("back up", re.I))
            try:
                if wake.count():
                    wake.first.click()
            except Exception:
                pass
            for frame in self.page.frames:
                try:
                    if frame.locator(".st-key-login_student_no, .st-key-whoami").count():
                        return frame
                except Exception:
                    continue
            time.sleep(1)
        raise RuntimeError(f"{self.label}: 앱 화면(로그인 칸)을 찾지 못했습니다")

    def key(self, key: str):
        return self.frame.locator(f".st-key-{key}")

    def result_text(self) -> str:
        box = self.key("result")
        try:
            return box.first.inner_text(timeout=3_000).strip() if box.count() else ""
        except Exception:
            return ""

    def wait_result(self, before: str, timeout: float = 20, same_ok: bool = True) -> str:
        """단추를 누른 뒤 결과 안내가 네 단어 가운데 하나로 시작하고 바뀔 때까지 기다린다.

        same_ok이면 앞 결과와 문구가 같을 수 있으므로, SAME_TEXT_WAIT초가 지나면 바뀌지 않았어도 그대로 읽는다.
        AI 배정은 기다리는 동안 앞 결과(모집 마감)가 그대로 보이므로 same_ok=False로 문구가 바뀔 때까지 기다린다.
        """
        started = time.monotonic()
        last = None
        while time.monotonic() - started < timeout:
            text = self.result_text()
            settled = text == last  # 0.5초 사이에 그대로면 다 그려진 것으로 봄
            last = text
            if settled and first_word(text) in WORDS:
                if text != before or (same_ok and time.monotonic() - started >= SAME_TEXT_WAIT):
                    return text
            time.sleep(0.5)
        return self.result_text()

    def login(self, student_no: str, password: str) -> None:
        self.key("login_student_no").locator("input").first.fill(student_no)
        self.key("login_password").locator("input").first.fill(password)
        self.key("login_submit").locator("button").first.click()
        try:
            self.key("whoami").first.wait_for(state="visible", timeout=40_000)
        except Exception:
            raise RuntimeError(f"{self.label}: 로그인하지 못했습니다 ({one_line(self.result_text())})") from None

    def button(self, key: str, timeout: float = BUTTON_WAIT):
        """key의 단추가 나타나기를 기다려 돌려 준다. 없으면 None."""
        locator = self.key(key).locator("button").first
        try:
            locator.wait_for(state="visible", timeout=timeout * 1000)
            return locator
        except Exception:
            return None

    def click_for_result(self, key: str, timeout: float = 20, same_ok: bool = True) -> str:
        """단추를 눌러 결과 안내를 읽는다. 단추가 없거나 비활성이면 채점 프로그램처럼 막힘으로 본다."""
        button = self.button(key)
        if button is None:
            return "막힘: (단추가 화면에 없음)"
        if button.is_disabled():
            return "막힘: (단추가 비활성)"
        before = self.result_text()
        button.click()
        return self.wait_result(before, timeout, same_ok)

    def shot(self, name: str) -> None:
        try:
            self.page.screenshot(path=str(self.shots / f"{name}.png"), full_page=True)
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.context.close()
        except Exception:
            pass


def check_rules() -> str:
    """확정 뒤 DB 상태로 팀이 없는 학생과 팀 규칙(인원, 주 역할 중복, 필수 역할)을 검사한다."""
    try:
        from teamapp import db
        from teamapp.solver import validate_plan
    except Exception:
        return "검사하지 못함(psycopg2 없음)"
    dsn = config.get_secret("DATABASE_URL")
    if not dsn:
        return "검사하지 못함(.env에 DATABASE_URL 없음)"
    pool = None
    try:
        pool = db.make_pool(dsn)
        with db.read_repo(pool) as repo:
            state = repo.load_state()
        problems = validate_plan(state, [])
        left = [s.name for s in state.unassigned()]
        if left:
            problems = [f"팀이 없는 학생이 남음: {', '.join(left)}"] + problems
        return "통과(모든 학생이 팀에 있고 모든 팀이 규칙을 지킴)" if not problems else "실패: " + " / ".join(problems)
    except Exception:
        return "검사하지 못함(DB 연결 실패)"
    finally:
        if pool is not None:
            pool.closeall()


def describe(step: tuple) -> tuple[str, str]:
    kind = step[0]
    if kind == "apply":
        return step[1], f"팀{step[2]}에 지원"
    if kind == "vote":
        return step[1], f"{step[2]}에 {step[3]}"
    if kind == "close":
        return ADMIN, "모집 마감"
    return ADMIN, "AI 배정 후 확정"


def run_scenario(browser, url: str, title: str, scenario, out: Path, admin_password: str, report: list[str]) -> bool:
    shots = out / title
    shots.mkdir(parents=True, exist_ok=True)
    rows = parse_roster((DATA_DIR / scenario.csv_name).read_bytes())
    number = {row.name: row.student_no for row in rows}
    actors: dict[str, Actor] = {}
    all_ok = True

    def actor(name: str) -> Actor:
        if name not in actors:
            label = ADMIN if name == ADMIN else name
            created = Actor(browser, url, label, shots)
            if name == ADMIN:
                created.login(ADMIN_NO, admin_password)
            else:
                created.login(number[name], STUDENT_PASSWORD)
            actors[name] = created
        return actors[name]

    def line(text: str) -> None:
        print(text, flush=True)
        report.append(text)

    line(f"\n## {title} ({scenario.csv_name}, 팀 {scenario.team_count}개)\n")
    try:
        admin = actor(ADMIN)
        upload = admin.key("admin_roster_file").locator('input[type="file"]').first
        upload.set_input_files(str(DATA_DIR / scenario.csv_name))
        admin.key("admin_roster_file").get_by_text(scenario.csv_name).first.wait_for(timeout=30_000)
        count = admin.key("admin_team_count").locator("input").first
        count.fill(str(scenario.team_count))
        count.press("Enter")
        time.sleep(2)
        started = admin.click_for_result("admin_start")
        admin.shot("00-명단")
        line(f"- 명단 올리기: {one_line(started)}")
        if first_word(started) != "승인":
            line("- 명단을 올리지 못해 이 시나리오를 멈춥니다")
            return False

        line("\n| 번호 | 누가 | 하는 일 | 기대 | 실제 | 결과 안내 |")
        line("|---|---|---|---|---|---|")
        for index, step in enumerate(scenario.steps, start=1):
            who, what = describe(step)
            expected = step[-1]
            extra = ""
            if step[0] == "apply":
                result = actor(who).click_for_result(f"apply_{step[2]}")
            elif step[0] == "vote":
                key = ("vote_yes_" if step[3] == "찬성" else "vote_no_") + number[step[2]]
                result = actor(who).click_for_result(key)
            elif step[0] == "close":
                result = admin.click_for_result("admin_close")
                time.sleep(4)
                extra = " / 팀이 없는 학생 표시: " + one_line(admin.key("unassigned").first.inner_text(), 60)
            else:
                line(f"| {index} | {ADMIN} | AI 배정 | 승인 | (기다리는 중, 최대 {AI_WAIT // 60}분) | |")
                result = admin.click_for_result("admin_assign", timeout=AI_WAIT, same_ok=False)
                admin.shot(f"{index:02d}-AI배정")
                status = admin.key("assign_status").first.inner_text().strip()
                line(f"| {index} | {ADMIN} | AI 배정 결과 | 승인 | {first_word(result)} | {one_line(result)} |")
                for status_line in status.splitlines():
                    if status_line.strip():
                        line(f"|  |  | (assign_status) |  |  | {one_line(status_line, 90)} |")
                if first_word(result) == "승인":
                    result = admin.click_for_result("admin_confirm")
                what = "배정안 확정"
            actual = first_word(result)
            ok = actual == expected
            all_ok = all_ok and ok
            mark = "" if ok else " ← 다름"
            line(f"| {index} | {who} | {what} | {expected} | {actual}{mark} | {one_line(result)}{extra} |")
            actors[who].shot(f"{index:02d}-{who}")
        time.sleep(4)
        rule = check_rules()
        line(f"\n- 확정 뒤 규칙 검사: {rule}")
        all_ok = all_ok and rule.startswith("통과")
    except Exception as error:  # 화면을 찾지 못하는 등 도구가 멈춘 경우
        line(f"\n- 멈춤: {one_line(str(error), 150)}")
        for who, opened in actors.items():
            opened.shot(f"zz-멈춤-{who}")
        all_ok = False
    finally:
        for opened in actors.values():
            opened.close()
    return all_ok


def main(argv: list[str]) -> int:
    if "--dry-run" in argv:
        for title, scenario in SCENARIOS.items():
            print(f"\n{title} ({scenario.csv_name}, 팀 {scenario.team_count}개)")
            rows = {row.name: row.student_no for row in parse_roster((DATA_DIR / scenario.csv_name).read_bytes())}
            for index, step in enumerate(scenario.steps, start=1):
                who, what = describe(step)
                if who != ADMIN and who not in rows:
                    print(f"  명단에 없는 학생: {who}")
                    return 1
                print(f"  {index:2d}. {who} — {what} → 기대 {step[-1]}")
        return 0
    urls = [arg for arg in argv if arg.startswith("http")]
    if not urls:
        print("사용법: .venv\\Scripts\\python scripts\\e2e_practice.py https://<앱 주소>.streamlit.app [쉬움 중간 어려움]")
        return 2
    url = urls[0].rstrip("/") + "/"
    chosen = [name for name in SCENARIOS if name in argv] or list(SCENARIOS)
    admin_password = config.get_secret("ADMIN_PASSWORD")
    if not admin_password:
        print(".env에 ADMIN_PASSWORD가 없습니다")
        return 2
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright가 없습니다. 먼저 설치하세요:")
        print("  .venv\\Scripts\\python -m pip install playwright")
        print("  .venv\\Scripts\\python -m playwright install chromium")
        return 2

    out = ROOT / "practice_runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    report = [f"# 연습 시나리오 실행 기록 ({datetime.now():%Y-%m-%d %H:%M})", "", f"앱: {url}"]
    results = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for title in chosen:
                results[title] = run_scenario(browser, url, title, SCENARIOS[title], out, admin_password, report)
        finally:
            browser.close()
    summary = " · ".join(f"{title} {'모두 일치' if ok else '다른 곳 있음'}" for title, ok in results.items())
    report += ["", f"요약: {summary}"]
    (out / "결과.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"\n요약: {summary}")
    print(f"화면과 기록: {out.relative_to(ROOT)}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
