"""부록 연습 시나리오 3개 재현과 쓰기 처리(teamapp/actions.py) 핵심 동작 테스트."""
import unittest

from teamapp import actions
from teamapp.roster import ADMIN_NO
from teamapp.rules import TEAM_MAX, TEAM_MIN
from teamapp.solver import solve, validate_plan
from tests.memory_repo import MemoryRepo, fake_hash, fake_verify
from tests.scenarios import DATA_DIR, EASY, HARD, MEDIUM, Scenario, play, play_full

NOT_ADMIN = "막힘: 관리자만 할 수 있습니다"
LOGIN_FAIL = "막힘: 학번이나 비밀번호가 맞지 않습니다"


def replay(scenario):
    """빈 MemoryRepo에서 시나리오를 끝까지 재생한다. (저장소, [(기대 첫 단어, 결과)])."""
    repo = MemoryRepo()
    return repo, play_full(repo, scenario, fake_hash, fake_verify)


class ScenarioTestCase(unittest.TestCase):
    def assert_words(self, scenario, results, expected_words):
        """부록의 기대 첫 단어와 같게 옮겼는지, 실제 첫 단어가 기대와 같은지 확인한다."""
        self.assertEqual([e for e, _ in results], expected_words, "scenarios.py의 기대 단어가 부록과 다름")
        self.assertEqual([o.kind for _, o in results], expected_words)
        self.assertEqual(len(results), len(scenario.steps))
        for _, outcome in results:  # 문구는 첫 단어와 ": "로 시작한다
            self.assertTrue(outcome.text.startswith(f"{outcome.kind}: "), outcome.text)

    def assert_teams_valid(self, repo):
        """확정 뒤: 팀이 없는 학생이 없고, 모든 팀이 2~4명이며 배정안 검사를 통과한다."""
        state = repo.load_state()
        self.assertEqual(state.unassigned(), [])
        self.assertEqual(validate_plan(state, []), [])
        for team_id in state.team_ids:
            self.assertTrue(TEAM_MIN <= len(state.members[team_id]) <= TEAM_MAX, team_id)

    def close_message(self, scenario, results):
        kinds = [step[0] for step in scenario.steps]
        return results[kinds.index("close")][1].message


class AppendixScenarioTests(ScenarioTestCase):
    def test_easy_appendix_1(self):
        repo, results = replay(EASY)
        self.assert_words(
            EASY,
            results,
            # 1단계 승인x3, 2 승인(대기 후 찬성), 3 승인, 4 거절(대기, 대기, 거절), 5 승인, 6 마감, 확정
            ["승인", "승인", "승인", "대기", "승인", "대기", "승인", "대기", "대기", "거절", "대기", "승인", "승인", "승인"],
        )
        message = self.close_message(EASY, results)
        self.assertIn("거절한 지원: 없음", message)
        self.assertIn("팀에서 뺀 학생: 없음", message)
        self.assert_teams_valid(repo)

    def test_medium_appendix_2(self):
        repo, results = replay(MEDIUM)
        self.assert_words(
            MEDIUM,
            results,
            # 1 승인, 2 승인(대기, 승인), 3 막힘, 4 승인, 5 승인(대기, 승인), 6 거절(대기, 대기, 거절),
            # 7 승인x2, 8 대기와 막힘, 9 마감, 10 막힘, 11 배정과 확정
            ["승인", "대기", "승인", "막힘", "승인", "대기", "승인", "대기", "대기", "거절",
             "승인", "승인", "대기", "막힘", "승인", "막힘", "승인"],
        )
        message = self.close_message(MEDIUM, results)
        self.assertIn("거절한 지원: 학생12(PT;RW;FD)", message)
        self.assertIn("팀에서 뺀 학생: 학생3(PL;RW), 학생11(RW)", message)
        # 마감 때 거절된 지원과 팀에서 빠진 학생
        state = repo.load_state()
        by_name = {s.name: s for s in state.students.values()}
        rejected = [a for a in state.applications if a.student_id == by_name["학생12(PT;RW;FD)"].id]
        self.assertEqual([(a.status, a.reason) for a in rejected], [("거절", "모집 마감")])
        self.assert_teams_valid(repo)

    def test_hard_appendix_3(self):
        repo, results = replay(HARD)
        self.assert_words(
            HARD,
            results,
            # 1~13단계: 8단계 거절, 11단계 막힘, 13단계 대기, 14 마감, 15 배정과 확정
            ["승인", "대기", "승인", "대기", "대기", "승인", "승인", "대기", "승인", "대기", "대기", "대기", "승인",
             "대기", "대기", "거절", "승인", "대기", "승인", "막힘", "승인", "대기", "대기", "승인", "승인"],
        )
        message = self.close_message(HARD, results)
        self.assertIn("거절한 지원: 학생13(RW;PT;FD)", message)
        self.assertIn("팀에서 뺀 학생: 학생10(TT;RW)", message)
        self.assert_teams_valid(repo)

    def test_play_returns_expected_and_actual_first_words(self):
        for scenario in (EASY, MEDIUM, HARD):
            with self.subTest(csv=scenario.csv_name):
                pairs = play(MemoryRepo(), scenario, fake_hash, fake_verify)
                self.assertEqual(len(pairs), len(scenario.steps))
                for step, (expected, actual) in zip(scenario.steps, pairs):
                    self.assertEqual(expected, step[-1])  # 기대는 단계에 적힌 마지막 칸
                    self.assertEqual(actual, expected, step)


class ActionTests(ScenarioTestCase):
    def setUp(self):
        self.repo = MemoryRepo()
        self.admin_id = self.repo.find_account(ADMIN_NO).id
        self.data = (DATA_DIR / EASY.csv_name).read_bytes()

    def start(self):
        outcome = actions.reset_roster(self.repo, self.admin_id, self.data, 3, fake_hash)
        self.assertEqual(outcome.kind, "승인", outcome.text)
        return {s.name: s.id for s in self.repo.load_state().students.values()}

    def test_double_apply_is_blocked(self):
        ids = self.start()
        actions.apply(self.repo, ids["학생1(PL;RW)"], 1)  # 빈 팀: 승인
        first = actions.apply(self.repo, ids["학생4(WP;FD)"], 1)
        second = actions.apply(self.repo, ids["학생4(WP;FD)"], 1)
        self.assertEqual(first.text, "대기: 지원했습니다. 팀원 모두의 투표를 기다립니다")
        self.assertEqual(second.text, "막힘: 결과를 기다리는 지원이 있음")
        mine = [a for a in self.repo.load_state().applications if a.student_id == ids["학생4(WP;FD)"]]
        self.assertEqual([a.status for a in mine], ["대기"])

    def test_student_cannot_use_admin_actions(self):
        ids = self.start()
        student_id = ids["학생1(PL;RW)"]
        before = self.repo.load_state()
        for actor_id in (student_id, 999):  # 학생 계정, 없는 계정
            with self.subTest(actor_id=actor_id):
                self.assertEqual(
                    actions.reset_roster(self.repo, actor_id, self.data, 3, fake_hash).text, NOT_ADMIN
                )
                self.assertEqual(actions.close(self.repo, actor_id).text, NOT_ADMIN)
                self.assertEqual(actions.confirm(self.repo, actor_id, {1: 1}).text, NOT_ADMIN)
        self.assertEqual(self.repo.load_state(), before)

    def test_admin_cannot_use_student_actions(self):
        self.start()
        for actor_id in (self.admin_id, 999):
            with self.subTest(actor_id=actor_id):
                self.assertEqual(
                    actions.apply(self.repo, actor_id, 1).text, "막힘: 학생 계정으로만 할 수 있습니다"
                )
                self.assertEqual(
                    actions.vote(self.repo, actor_id, 1, "찬성").text, "막힘: 학생 계정으로만 할 수 있습니다"
                )
        self.assertEqual(self.repo.load_state().applications, ())

    def test_login(self):
        self.start()
        outcome, account = actions.login(self.repo, "2026101", "1234", fake_verify)
        self.assertEqual(outcome.text, "승인: 로그인했습니다")
        self.assertEqual((account.student_no, account.name, account.is_admin), ("2026101", "학생1(PL;RW)", False))
        # 앞뒤 공백은 학번에서 뺀다
        outcome, account = actions.login(self.repo, "  2026101 ", "1234", fake_verify)
        self.assertEqual(outcome.kind, "승인")
        self.assertEqual(account.student_no, "2026101")
        # 관리자도 같은 방법으로 로그인한다
        outcome, account = actions.login(self.repo, "admin", "adminpw", fake_verify)
        self.assertEqual(outcome.kind, "승인")
        self.assertTrue(account.is_admin)
        # 틀린 비밀번호, 없는 학번, 빈칸은 모두 같은 문구
        for student_no, password in [
            ("2026101", "0000"),
            ("9999999", "1234"),
            ("", "1234"),
            ("   ", "1234"),
            ("2026101", ""),
            ("", ""),
            ("admin", "1234"),
        ]:
            with self.subTest(student_no=student_no, password=password):
                outcome, account = actions.login(self.repo, student_no, password, fake_verify)
                self.assertEqual(outcome.text, LOGIN_FAIL)
                self.assertIsNone(account)

    def test_confirm_rejects_stale_plan(self):
        # 마감까지 하고 배정안을 만든다
        repo = self.repo
        play_until_close = Scenario(EASY.csv_name, EASY.team_count, EASY.steps[:-1])
        play(repo, play_until_close, fake_hash, fake_verify)
        plan = solve(repo.load_state())
        self.assertTrue(plan)

        # 명단을 다시 올리면 마감이 풀려서 확정할 수 없다
        again = actions.reset_roster(repo, self.admin_id, self.data, 3, fake_hash)
        self.assertEqual(again.kind, "승인")
        self.assertEqual(actions.confirm(repo, self.admin_id, plan).text, "막힘: 모집을 먼저 마감해야 합니다")

        # 다시 마감해도 팀이 없는 학생이 달라졌으므로 옛 배정안은 막힌다
        self.assertEqual(actions.close(repo, self.admin_id).kind, "승인")
        before = repo.load_state()
        self.assertEqual(
            actions.confirm(repo, self.admin_id, plan).text,
            "막힘: 배정안을 만든 뒤 상태가 바뀌었습니다. AI 배정을 다시 해 주세요",
        )
        self.assertEqual(repo.load_state(), before)  # 아무것도 쓰지 않음


if __name__ == "__main__":
    unittest.main()
