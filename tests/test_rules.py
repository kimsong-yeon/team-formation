"""역할 상수와 필수 역할 자리 계산(teamapp/rules.py) 테스트."""
import unittest

from teamapp.rules import (
    ABBR,
    ROLES,
    Student,
    required_filled,
    seat_problem,
    unmet_roles,
)


def S(i, main, *subs):  # 테스트용 학생
    return Student(i, f"2026{i:03d}", f"학생{i}", main, tuple(subs))


class ConstantsTest(unittest.TestCase):
    def test_roles_order_and_abbr(self):
        self.assertEqual(
            ROLES,
            ("기획자", "웹 프로그래머", "프론트엔드 디자이너", "테스터", "보고서 작성자", "발표자"),
        )
        self.assertEqual([ABBR[r] for r in ROLES], ["PL", "WP", "FD", "TT", "RW", "PT"])

    def test_student_can_uses_main_and_sub_roles(self):
        s = S(1, "테스터", "기획자")
        self.assertTrue(s.can("테스터"))
        self.assertTrue(s.can("기획자"))
        self.assertFalse(s.can("웹 프로그래머"))


class RequiredFilledTest(unittest.TestCase):
    def test_one_student_who_can_do_both_fills_only_one(self):
        self.assertEqual(required_filled([S(1, "기획자", "웹 프로그래머")]), 1)
        self.assertEqual(
            unmet_roles([S(1, "기획자", "웹 프로그래머")]),
            ["기획자나 웹 프로그래머 중 하나"],
        )

    def test_sub_role_counts_for_required(self):
        team = [S(1, "테스터", "기획자"), S(2, "프론트엔드 디자이너", "웹 프로그래머")]
        self.assertEqual(required_filled(team), 2)
        self.assertEqual(unmet_roles(team), [])

    def test_nobody_can_do_either_role(self):
        team = [S(1, "테스터"), S(2, "발표자")]
        self.assertEqual(required_filled(team), 0)
        self.assertEqual(unmet_roles(team), ["기획자", "웹 프로그래머"])

    def test_only_planner_or_only_web_available(self):
        self.assertEqual(required_filled([S(1, "기획자"), S(2, "테스터")]), 1)
        self.assertEqual(unmet_roles([S(1, "기획자"), S(2, "테스터")]), ["웹 프로그래머"])
        self.assertEqual(required_filled([S(1, "웹 프로그래머"), S(2, "테스터")]), 1)
        self.assertEqual(unmet_roles([S(1, "웹 프로그래머"), S(2, "테스터")]), ["기획자"])

    def test_flexible_student_plus_one_specialist_fills_both(self):
        # 둘 다 할 수 있는 학생 + 기획자만 되는 학생: 기획자 / 웹 프로그래머를 따로 맡을 수 있음
        team = [S(1, "기획자", "웹 프로그래머"), S(2, "기획자")]
        self.assertEqual(required_filled(team), 2)
        self.assertEqual(unmet_roles(team), [])

    def test_two_flexible_students_fill_both(self):
        team = [S(1, "기획자", "웹 프로그래머"), S(2, "웹 프로그래머", "기획자")]
        self.assertEqual(required_filled(team), 2)

    def test_empty_team(self):
        self.assertEqual(required_filled([]), 0)
        self.assertEqual(unmet_roles([]), ["기획자", "웹 프로그래머"])


class SeatProblemTest(unittest.TestCase):
    def test_seat_problem_full_team(self):
        four = [S(1, "기획자"), S(2, "웹 프로그래머"), S(3, "테스터"), S(4, "발표자")]
        self.assertEqual(seat_problem(four + [S(5, "보고서 작성자")]), "팀이 꽉 참")

    def test_seat_problem_same_main_role(self):
        self.assertEqual(
            seat_problem([S(1, "기획자"), S(2, "기획자")]),
            "같은 주 역할의 팀원이 있음",
        )

    def test_seat_problem_not_enough_seats_for_required(self):
        # 공고 3장 예: 테스터와 발표자만 있는 팀(2명)에 보고서 작성자가 지원
        msg = seat_problem([S(1, "테스터"), S(2, "발표자"), S(3, "보고서 작성자")])
        self.assertEqual(
            msg,
            "필수 포함 역할 자리가 모자람 (남는 자리 1개, 채워야 할 역할: 기획자, 웹 프로그래머)",
        )

    def test_seat_problem_ok_when_seats_enough(self):
        self.assertIsNone(seat_problem([S(1, "테스터"), S(2, "기획자")]))

    def test_full_team_check_comes_before_duplicate_check(self):
        five = [S(1, "기획자"), S(2, "기획자"), S(3, "테스터"), S(4, "발표자"), S(5, "보고서 작성자")]
        self.assertEqual(seat_problem(five), "팀이 꽉 참")

    def test_four_members_with_both_required_roles_is_ok(self):
        four = [S(1, "기획자"), S(2, "웹 프로그래머"), S(3, "테스터"), S(4, "발표자")]
        self.assertIsNone(seat_problem(four))

    def test_single_flexible_student_names_either_role(self):
        # 3명 팀: 둘 다 되는 학생 1명이 필수 하나만 채움, 남는 자리 1개, 못 채운 필수 1개 -> 통과
        team = [S(1, "기획자", "웹 프로그래머"), S(2, "테스터"), S(3, "발표자")]
        self.assertIsNone(seat_problem(team))
        # 4명 팀이면 남는 자리 0개 < 못 채운 필수 1개 -> 막힘
        team4 = team + [S(4, "보고서 작성자")]
        self.assertEqual(
            seat_problem(team4),
            "필수 포함 역할 자리가 모자람 (남는 자리 0개, 채워야 할 역할: 기획자나 웹 프로그래머 중 하나)",
        )


if __name__ == "__main__":
    unittest.main()
