"""AI 배정(teamapp/ai.py) 테스트. 네트워크는 쓰지 않는다: 모델 호출은 가짜 call로, requests.post는 가짜로 바꾼다."""
import json
import threading
import time
import unittest
from unittest import mock

import requests

from teamapp import ai
from teamapp.ai import (
    MODELS,
    OPENROUTER_URL,
    TIMEOUT_SECONDS,
    AssignResult,
    ModelFailure,
    build_messages,
    call_openrouter,
    parse_assignments,
    run_assignment,
    run_with_deadline,
)
from teamapp.rules import ROLES, State, Student
from teamapp.solver import validate_plan

PL, WP, FD, TT, RW, PT = ROLES
API_KEY = "sk-or-test-SECRET-1234567890"
APP_SOURCE = "앱(모든 경우를 따진 배정)"
APP_LINE = "3차 모델까지 실패하여 앱이 모든 경우를 따져 배정함"


def S(i, main, *subs):  # 테스트용 학생: 학번 2026101부터
    return Student(i, f"{2026100 + i}", f"학생{i}", main, tuple(subs))


def make_state(students, members, teams):
    return State(
        students={s.id: s for s in students},
        team_ids=tuple(range(1, teams + 1)),
        members={t: tuple(members.get(t, ())) for t in range(1, teams + 1)},
        applications=(),
        votes={},
        closed=True,
    )


def easy_state():
    """팀1 {1,4}, 팀2 {2,5}, 팀3 {3,6}, 팀 없음 {7,8}. 예: 7->1, 8->2 가 바른 배정."""
    students = [
        S(1, PL, RW), S(2, PL, PT), S(3, PL, TT),
        S(4, WP, FD), S(5, WP), S(6, WP, TT),
        S(7, FD, PT), S(8, TT, RW, PT),
    ]
    return make_state(students, {1: [1, 4], 2: [2, 5], 3: [3, 6]}, teams=3)


def web_gap_state():
    """팀1 {PL 1, WP 2}, 팀2 {PL 3, TT 4}, 팀 없음 {5(FD, 보조 WP), 6(RW)}.

    바른 배정은 5->팀2(웹 프로그래머를 맡음). 5->팀1, 6->팀2 로 하면 팀2에 웹 프로그래머가 없다.
    """
    students = [S(1, PL), S(2, WP), S(3, PL), S(4, TT), S(5, FD, WP), S(6, RW)]
    return make_state(students, {1: [1, 2], 2: [3, 4]}, teams=2)


def impossible_state():
    """빈 팀 2개에 팀이 없는 학생이 1명뿐이라 어떤 배정도 규칙을 지킬 수 없다."""
    return make_state([S(1, PL, WP)], {}, teams=2)


def answer(*pairs):
    """(학번, 팀) 쌍들을 모델의 JSON 응답으로 만든다."""
    return json.dumps({"assignments": [{"student_no": no, "team": team} for no, team in pairs]})


GOOD_EASY = answer(("2026107", 1), ("2026108", 2))
GOOD_WEB_GAP = answer(("2026105", 2), ("2026106", 1))
BAD_WEB_GAP = answer(("2026105", 1), ("2026106", 2))


class FakeCall:
    """call 대신 쓰는 가짜. 부를 때마다 answers의 다음 값을 돌려주고, 예외 객체면 던진다."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, model, messages, api_key):
        self.calls.append((model, messages, api_key))
        result = self.answers[len(self.calls) - 1]
        if isinstance(result, BaseException):
            raise result
        return result


class ModelsTest(unittest.TestCase):
    def test_three_distinct_free_models_without_router(self):
        self.assertEqual(len(MODELS), 3)
        self.assertEqual(len(set(MODELS)), 3)
        self.assertEqual(
            MODELS,
            (
                "google/gemma-4-31b-it:free",
                "nvidia/nemotron-3-super-120b-a12b:free",
                "poolside/laguna-s-2.1:free",
            ),
        )
        for model in MODELS:
            self.assertTrue(model.endswith(":free"))
            self.assertNotEqual(model, "openrouter/free")
        self.assertEqual(TIMEOUT_SECONDS, 120)
        self.assertEqual(OPENROUTER_URL, "https://openrouter.ai/api/v1/chat/completions")

    def test_model_failure_keeps_reason(self):
        failure = ModelFailure("빈 응답")
        self.assertEqual(failure.reason, "빈 응답")
        self.assertEqual(str(failure), "빈 응답")


class BuildMessagesTest(unittest.TestCase):
    def test_system_and_user_messages(self):
        messages = build_messages(easy_state())
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        system, user = messages[0]["content"], messages[1]["content"]
        for phrase in (
            "이미 팀에 있는 학생은 옮기지 않음",
            "팀이 없는 학생을 빠짐없이 한 팀씩 배정",
            "JSON만 답함",
            "2~4명",
            "주 역할",
            "기획자",
            "웹 프로그래머",
        ):
            self.assertIn(phrase, system)
        # 팀마다 지금 팀원(학번·주 역할·보조 역할)과 남은 자리
        self.assertIn("팀1", user)
        self.assertIn("팀3", user)
        self.assertIn("2026101", user)  # 팀1의 팀원 학생1
        self.assertIn("남은 자리 2개", user)
        # 팀이 없는 학생 목록
        self.assertIn("2026107", user)
        self.assertIn("2026108", user)
        self.assertIn("발표자", user)
        # 답 형식
        self.assertIn('{"assignments": [{"student_no": "...", "team": 1}]}', user)

    def test_empty_team_is_listed_with_all_seats_free(self):
        user = build_messages(impossible_state())[1]["content"]
        self.assertIn("팀1", user)
        self.assertIn("팀2", user)
        self.assertIn("남은 자리 4개", user)
        self.assertIn("2026101", user)

    def test_messages_contain_no_secret(self):
        for state in (easy_state(), web_gap_state(), impossible_state()):
            for message in build_messages(state):
                for value in message.values():
                    self.assertNotIn(API_KEY, value)
                    self.assertNotIn("sk-or", value)


class ParseAssignmentsTest(unittest.TestCase):
    def setUp(self):
        self.state = easy_state()

    def test_parse_accepts_code_fence_and_team_text(self):
        text = '```json\n{"assignments":[{"student_no":"2026107","team":"팀2"}]}\n```'
        self.assertEqual(parse_assignments(text, self.state), [(7, 2)])

    def test_parse_plain_json_keeps_answer_order(self):
        self.assertEqual(parse_assignments(GOOD_EASY, self.state), [(7, 1), (8, 2)])
        self.assertEqual(
            parse_assignments(answer(("2026108", 2), ("2026107", 1)), self.state), [(8, 2), (7, 1)]
        )

    def test_parse_takes_json_out_of_surrounding_text(self):
        text = '배정안입니다.\n{"assignments": [{"student_no": "2026107", "team": 3}]}\n이상입니다.'
        self.assertEqual(parse_assignments(text, self.state), [(7, 3)])

    def test_parse_accepts_number_and_string_forms(self):
        text = '{"assignments": [{"student_no": 2026107, "team": 1}, {"student_no": " 2026108 ", "team": "2"}]}'
        self.assertEqual(parse_assignments(text, self.state), [(7, 1), (8, 2)])
        text = '{"assignments": [{"student_no": "2026107", "team": "team 3"}]}'
        self.assertEqual(parse_assignments(text, self.state), [(7, 3)])

    def test_parse_does_not_deduplicate_or_check_teams(self):
        text = answer(("2026107", 1), ("2026107", 2), ("2026108", 9))
        pairs = parse_assignments(text, self.state)
        self.assertEqual(pairs, [(7, 1), (7, 2), (8, 9)])
        # 중복과 없는 팀은 validate_plan이 알려 준다.
        problems = validate_plan(self.state, pairs)
        self.assertIn("학생7이(가) 두 팀 이상에 배정됨", problems)
        self.assertIn("없는 팀 9", problems)

    def test_parse_empty_response(self):
        for text in (None, "", "   ", "\n\t "):
            with self.assertRaises(ModelFailure) as caught:
                parse_assignments(text, self.state)
            self.assertEqual(caught.exception.reason, "빈 응답")

    def test_parse_unreadable_json(self):
        bad_texts = [
            "배정할 수 없습니다",
            '{"assignments": [',
            "{}",
            '{"assignments": "없음"}',
            '{"assignments": {"student_no": "2026107", "team": 1}}',
            '{"assignments": [{"team": 1}]}',
            '{"assignments": [{"student_no": "2026107"}]}',
            '{"assignments": [{"student_no": "2026107", "team": "없음"}]}',
            '{"assignments": [{"student_no": null, "team": 1}]}',
            '{"assignments": [{"student_no": "2026107", "team": [1]}]}',
            '{"assignments": ["2026107"]}',
        ]
        for text in bad_texts:
            with self.subTest(text=text):
                with self.assertRaises(ModelFailure) as caught:
                    parse_assignments(text, self.state)
                self.assertEqual(caught.exception.reason, "JSON을 읽을 수 없음")

    def test_parse_unknown_student(self):
        with self.assertRaises(ModelFailure) as caught:
            parse_assignments(answer(("2026107", 1), ("2026999", 2)), self.state)
        self.assertEqual(caught.exception.reason, "모르는 학번 2026999")

    def test_parse_student_already_in_team_is_left_to_validate_plan(self):
        pairs = parse_assignments(answer(("2026101", 2), ("2026107", 1), ("2026108", 2)), self.state)
        self.assertEqual(pairs[0], (1, 2))
        self.assertIn("학생1은(는) 팀이 없는 학생이 아님", validate_plan(self.state, pairs))


class RunWithDeadlineTest(unittest.TestCase):
    def test_returns_result(self):
        self.assertEqual(run_with_deadline(lambda: "답", 5), "답")

    def test_reraises_exception_from_fn(self):
        def fail():
            raise ModelFailure("HTTP 429(사용량 제한)")

        with self.assertRaises(ModelFailure) as caught:
            run_with_deadline(fail, 5)
        self.assertEqual(caught.exception.reason, "HTTP 429(사용량 제한)")

        def boom():
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            run_with_deadline(boom, 5)

    def test_run_with_deadline_cuts_off(self):
        started = time.monotonic()
        with self.assertRaises(ModelFailure) as caught:
            run_with_deadline(lambda: time.sleep(2) or "늦은 답", 0.3)
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.reason, "시간 초과(0.3초)")
        self.assertGreaterEqual(elapsed, 0.3)
        self.assertLess(elapsed, 1.0)  # fn이 끝나기를(2초) 기다리지 않는다

    def test_short_deadline_ticks_once_with_elapsed(self):
        ticks = []
        with self.assertRaises(ModelFailure):
            run_with_deadline(lambda: time.sleep(1.5) or "", 0.3, ticks.append)
        self.assertEqual(len(ticks), 1)
        self.assertGreaterEqual(ticks[0], 0.29)
        self.assertLess(ticks[0], 0.9)

    def test_ticks_every_second_and_deadline_text_uses_plain_number(self):
        ticks = []
        started = time.monotonic()
        with self.assertRaises(ModelFailure) as caught:
            run_with_deadline(lambda: time.sleep(3) or "", 1.0, ticks.append)
        self.assertEqual(caught.exception.reason, "시간 초과(1초)")
        self.assertLess(time.monotonic() - started, 1.8)
        self.assertEqual(len(ticks), 1)
        self.assertGreaterEqual(ticks[0], 0.99)

    def test_ticks_while_waiting_for_a_slower_fn(self):
        ticks = []
        result = run_with_deadline(lambda: time.sleep(1.3) or "느린 답", 5, ticks.append)
        self.assertEqual(result, "느린 답")
        self.assertEqual(len(ticks), 1)  # 1초 지점에서 한 번, 그 뒤 fn이 끝남
        self.assertGreaterEqual(ticks[0], 0.99)

    def test_no_tick_when_fn_finishes_at_once(self):
        ticks = []
        self.assertEqual(run_with_deadline(lambda: "바로", 5, ticks.append), "바로")
        self.assertEqual(ticks, [])

    def test_runs_fn_in_daemon_thread(self):
        seen = []

        def fn():
            seen.append(threading.current_thread())
            return "ok"

        run_with_deadline(fn, 5)
        self.assertIsNot(seen[0], threading.main_thread())
        self.assertTrue(seen[0].daemon)


def fake_response(status=200, body=None):
    """requests.post가 돌려줄 가짜 응답. body를 주지 않으면 본문이 JSON이 아닌 응답이다."""
    response = mock.Mock()
    response.status_code = status
    if body is None:
        response.json.side_effect = ValueError("본문이 JSON이 아님")
    else:
        response.json.return_value = body
    return response


def ok_body(content):
    return {"choices": [{"message": {"content": content}}]}


class CallOpenRouterTest(unittest.TestCase):
    MESSAGES = [{"role": "system", "content": "규칙"}, {"role": "user", "content": "학생들"}]

    def call(self, response):
        with mock.patch.object(ai.requests, "post", return_value=response) as post:
            try:
                return call_openrouter(MODELS[0], self.MESSAGES, API_KEY), post
            except ModelFailure as failure:
                return failure, post

    def reason_of(self, response):
        result, _ = self.call(response)
        self.assertIsInstance(result, ModelFailure)
        self.assertNotIn(API_KEY, result.reason)
        return result.reason

    def test_success_sends_documented_request(self):
        result, post = self.call(fake_response(200, ok_body('{"assignments": []}')))
        self.assertEqual(result, '{"assignments": []}')
        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(args, (OPENROUTER_URL,))
        self.assertEqual(
            kwargs["headers"],
            {
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
                "X-Title": "team-formation",
            },
        )
        self.assertEqual(
            kwargs["json"],
            {
                "model": MODELS[0],
                "messages": self.MESSAGES,
                "max_tokens": 4000,
                "reasoning": {"effort": "low"},
                "temperature": 0,
            },
        )
        self.assertEqual(kwargs["timeout"], (10, 130))

    def test_missing_key_does_not_call_the_network(self):
        for key in ("", None, "   "):
            with mock.patch.object(ai.requests, "post") as post:
                with self.assertRaises(ModelFailure) as caught:
                    call_openrouter(MODELS[0], self.MESSAGES, key)
            self.assertEqual(caught.exception.reason, "API 키 없음")
            post.assert_not_called()

    def test_http_429(self):
        self.assertEqual(self.reason_of(fake_response(429, {"error": {"message": "rate limited"}})), "HTTP 429(사용량 제한)")
        self.assertEqual(self.reason_of(fake_response(429)), "HTTP 429(사용량 제한)")

    def test_other_http_errors_add_message_cut_to_80_chars(self):
        long_message = "가" * 100
        reason = self.reason_of(fake_response(503, {"error": {"message": long_message}}))
        self.assertEqual(reason, f"HTTP 503 ({'가' * 80})")
        self.assertEqual(self.reason_of(fake_response(401, {"error": {"message": "No auth"}})), "HTTP 401 (No auth)")
        self.assertEqual(self.reason_of(fake_response(500)), "HTTP 500")  # JSON이 아닌 본문
        self.assertEqual(self.reason_of(fake_response(502, {"detail": "x"})), "HTTP 502")  # error.message 없음

    def test_error_inside_200_body(self):
        body = {"error": {"message": "Provider returned error", "code": 502}}
        self.assertEqual(self.reason_of(fake_response(200, body)), "오류 (Provider returned error)")
        self.assertEqual(self.reason_of(fake_response(200, {"error": {"message": "나" * 120}})), f"오류 ({'나' * 80})")

    def test_empty_content_is_empty_response(self):
        for body in (
            ok_body(None),
            ok_body(""),
            ok_body("  \n "),
            {"choices": [{"message": {}}]},
            {"choices": [{"message": {"content": None, "reasoning": "생각만 하고 끝"}}]},
            {"choices": []},
            {"choices": None},
            {},
            [],
        ):
            with self.subTest(body=body):
                self.assertEqual(self.reason_of(fake_response(200, body)), "빈 응답")
        self.assertEqual(self.reason_of(fake_response(200)), "빈 응답")  # JSON이 아닌 본문

    def test_connection_problems_are_connection_error(self):
        for error in (
            requests.exceptions.ConnectionError(f"HTTPSConnectionPool: Bearer {API_KEY}"),
            requests.exceptions.ReadTimeout("read timed out"),
            requests.exceptions.ConnectTimeout("connect timed out"),
            requests.exceptions.SSLError("ssl"),
        ):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(ai.requests, "post", side_effect=error):
                    with self.assertRaises(ModelFailure) as caught:
                        call_openrouter(MODELS[0], self.MESSAGES, API_KEY)
                self.assertEqual(caught.exception.reason, "연결 오류")
                self.assertNotIn(API_KEY, str(caught.exception))

    def test_key_echoed_by_server_is_not_repeated(self):
        reason = self.reason_of(fake_response(401, {"error": {"message": f"Invalid key {API_KEY}"}}))
        self.assertNotIn(API_KEY, reason)
        self.assertTrue(reason.startswith("HTTP 401"))

    def test_messages_are_not_changed(self):
        before = json.dumps(self.MESSAGES)
        self.call(fake_response(200, ok_body("답")))
        self.assertEqual(json.dumps(self.MESSAGES), before)


class RunAssignmentTest(unittest.TestCase):
    def run_it(self, state, fake, **kwargs):
        lines = []
        waits = []
        result = run_assignment(
            state,
            API_KEY,
            on_line=lines.append,
            on_wait=lambda order, model, elapsed: waits.append((order, model, elapsed)),
            call=fake,
            **kwargs,
        )
        self.assertEqual(lines, result.lines)  # on_line에 준 줄과 결과의 줄이 같다
        return result, waits

    def test_first_model_success(self):
        state = easy_state()
        fake = FakeCall(GOOD_EASY)
        result, waits = self.run_it(state, fake)
        self.assertIsInstance(result, AssignResult)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(result.lines, [f"1차 모델({MODELS[0]}): 검사 통과 (0초)"])
        self.assertEqual(result.source, f"1차 모델({MODELS[0]})")
        self.assertEqual(result.plan, {7: 1, 8: 2})
        self.assertEqual(result.problems, [])
        self.assertEqual(validate_plan(state, list(result.plan.items())), [])
        self.assertEqual(waits[0], (1, MODELS[0], 0))

    def test_call_gets_model_messages_and_key(self):
        state = easy_state()
        fake = FakeCall(GOOD_EASY)
        self.run_it(state, fake)
        model, messages, api_key = fake.calls[0]
        self.assertEqual(model, MODELS[0])
        self.assertEqual(messages, build_messages(state))
        self.assertEqual(api_key, API_KEY)

    def test_falls_through_timeout_429_empty_then_app(self):
        state = easy_state()
        fake = FakeCall(
            ModelFailure("시간 초과(120초)"),
            ModelFailure("HTTP 429(사용량 제한)"),
            ModelFailure("빈 응답"),
        )
        result, waits = self.run_it(state, fake)
        self.assertEqual([c[0] for c in fake.calls], list(MODELS))
        self.assertEqual(
            result.lines,
            [
                f"1차 모델({MODELS[0]}): 시간 초과(120초) → 2차 모델로 넘어감",
                f"2차 모델({MODELS[1]}): HTTP 429(사용량 제한) → 3차 모델로 넘어감",
                f"3차 모델({MODELS[2]}): 빈 응답 → 앱이 직접 배정함",
                APP_LINE,
            ],
        )
        self.assertEqual(result.source, APP_SOURCE)
        self.assertIsNotNone(result.plan)
        self.assertEqual(validate_plan(state, list(result.plan.items())), [])
        self.assertEqual(result.problems, [])
        self.assertEqual([w[:3] for w in waits if w[2] == 0], [(1, MODELS[0], 0), (2, MODELS[1], 0), (3, MODELS[2], 0)])

    def test_invalid_plan_goes_to_next_model(self):
        state = web_gap_state()
        fake = FakeCall(BAD_WEB_GAP, GOOD_WEB_GAP)
        result, _ = self.run_it(state, fake)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(
            result.lines[0],
            f"1차 모델({MODELS[0]}): 검사 실패: 팀2에 웹 프로그래머를 맡을 학생이 없음 → 2차 모델로 넘어감",
        )
        self.assertEqual(result.lines[1], f"2차 모델({MODELS[1]}): 검사 통과 (0초)")
        self.assertEqual(len(result.lines), 2)
        self.assertEqual(result.source, f"2차 모델({MODELS[1]})")
        self.assertEqual(result.plan, {5: 2, 6: 1})

    def test_failure_reasons_cover_parse_and_validation_problems(self):
        state = easy_state()
        fake = FakeCall("죄송합니다. 배정할 수 없습니다.", answer(("2026107", 1)), "   ")
        result, _ = self.run_it(state, fake)
        self.assertEqual(
            result.lines[:3],
            [
                f"1차 모델({MODELS[0]}): JSON을 읽을 수 없음 → 2차 모델로 넘어감",
                f"2차 모델({MODELS[1]}): 검사 실패: 학생8이(가) 배정되지 않음 → 3차 모델로 넘어감",
                f"3차 모델({MODELS[2]}): 빈 응답 → 앱이 직접 배정함",
            ],
        )
        self.assertEqual(result.source, APP_SOURCE)
        fake = FakeCall(answer(("2026777", 1)), GOOD_EASY)
        result, _ = self.run_it(state, fake)
        self.assertEqual(result.lines[0], f"1차 모델({MODELS[0]}): 모르는 학번 2026777 → 2차 모델로 넘어감")
        self.assertEqual(result.source, f"2차 모델({MODELS[1]})")

    def test_failure_line_reports_only_the_first_problem(self):
        state = easy_state()
        fake = FakeCall('{"assignments": []}', GOOD_EASY)  # 학생7, 학생8 둘 다 배정되지 않음
        result, _ = self.run_it(state, fake)
        self.assertEqual(
            result.lines[0],
            f"1차 모델({MODELS[0]}): 검사 실패: 학생7이(가) 배정되지 않음 → 2차 모델로 넘어감",
        )

    def test_unexpected_exception_from_call_is_connection_error(self):
        state = easy_state()
        fake = FakeCall(RuntimeError(f"boom {API_KEY}"), GOOD_EASY)
        result, _ = self.run_it(state, fake)
        self.assertEqual(result.lines[0], f"1차 모델({MODELS[0]}): 연결 오류 → 2차 모델로 넘어감")
        self.assertEqual(result.source, f"2차 모델({MODELS[1]})")
        self.assertNotIn(API_KEY, "\n".join(result.lines))

    def test_no_assignment_skips_ai(self):
        state = impossible_state()
        fake = FakeCall()
        result, waits = self.run_it(state, fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(waits, [])
        self.assertIsNone(result.plan)
        self.assertTrue(result.problems)
        self.assertIn("팀을 2명 이상으로 채울 학생이 모자람", result.problems[0])
        self.assertEqual(result.lines, [])

    def test_nobody_to_assign_skips_ai(self):
        students = [S(1, PL), S(2, WP)]
        state = make_state(students, {1: [1, 2]}, teams=1)
        fake = FakeCall()
        result, _ = self.run_it(state, fake)
        self.assertEqual(fake.calls, [])
        self.assertIsNone(result.plan)
        self.assertEqual(result.problems, ["배정할 학생이 없습니다"])

    def test_timeout_cuts_off_slow_model_and_moves_on(self):
        state = easy_state()
        slow_release = threading.Event()

        def slow_then_good(model, messages, api_key):
            if model == MODELS[0]:
                slow_release.wait(3)  # 응답이 늦는 1차 모델
            return GOOD_EASY

        started = time.monotonic()
        result, waits = self.run_it(state, slow_then_good, timeout=0.3)
        slow_release.set()
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(result.lines[0], f"1차 모델({MODELS[0]}): 시간 초과(0.3초) → 2차 모델로 넘어감")
        self.assertEqual(result.source, f"2차 모델({MODELS[1]})")
        # 1차 모델을 기다리는 동안 틱이 on_wait로 이어진다
        ticks = [w for w in waits if w[0] == 1 and w[2] > 0]
        self.assertTrue(ticks)
        self.assertGreaterEqual(ticks[0][2], 0.29)

    def test_lines_are_reported_as_soon_as_produced(self):
        state = easy_state()
        events = []

        def fake(model, messages, api_key):
            events.append(("call", model))
            if model == MODELS[0]:
                raise ModelFailure("빈 응답")
            return GOOD_EASY

        run_assignment(
            state,
            API_KEY,
            on_line=lambda line: events.append(("line", line)),
            on_wait=lambda order, model, elapsed: events.append(("wait", order)),
            call=fake,
        )
        self.assertEqual(
            events,
            [
                ("wait", 1),
                ("call", MODELS[0]),
                ("line", f"1차 모델({MODELS[0]}): 빈 응답 → 2차 모델로 넘어감"),
                ("wait", 2),
                ("call", MODELS[1]),
                ("line", f"2차 모델({MODELS[1]}): 검사 통과 (0초)"),
            ],
        )

    def test_success_line_shows_rounded_seconds(self):
        def slow_but_good(model, messages, api_key):
            time.sleep(0.7)  # 0.7초 -> 반올림해 1초
            return GOOD_EASY

        result, _ = self.run_it(easy_state(), slow_but_good)
        self.assertEqual(result.lines, [f"1차 모델({MODELS[0]}): 검사 통과 (1초)"])

    def test_custom_models_and_last_model_goes_to_app(self):
        state = easy_state()
        fake = FakeCall(ModelFailure("빈 응답"))
        result, _ = self.run_it(state, fake, models=("m1",))
        self.assertEqual(
            result.lines,
            ["1차 모델(m1): 빈 응답 → 앱이 직접 배정함", APP_LINE],
        )
        self.assertEqual(result.source, APP_SOURCE)

    def test_app_plan_is_none_when_solve_unexpectedly_fails_at_the_end(self):
        state = easy_state()
        real_solve = ai.solve
        results = iter([real_solve(state), None])  # 사전 검사는 통과, 마지막 앱 배정은 None
        fake = FakeCall(ModelFailure("빈 응답"), ModelFailure("빈 응답"), ModelFailure("빈 응답"))
        with mock.patch.object(ai, "solve", side_effect=lambda s: next(results)):
            result, _ = self.run_it(state, fake)
        self.assertIsNone(result.plan)
        self.assertTrue(result.problems)
        self.assertNotIn(APP_LINE, result.lines)
        self.assertEqual(len(result.lines), 4)

    def test_worst_case_stays_under_ten_minutes(self):
        self.assertEqual(TIMEOUT_SECONDS, 120)
        self.assertLess(len(MODELS) * TIMEOUT_SECONDS, 10 * 60)


if __name__ == "__main__":
    unittest.main()
