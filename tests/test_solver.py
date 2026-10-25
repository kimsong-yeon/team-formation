"""배정안 다섯 가지 검사와 모든 경우를 따지는 배정(teamapp/solver.py) 테스트."""
import itertools
import random
import time
import unittest

from teamapp.rules import PLANNER, ROLES, WEB, State, Student
from teamapp.solver import explain_no_assignment, solve, validate_plan

FALLBACK = "역할 조합상 모든 규칙을 함께 지킬 수 없음"


def S(i, main, *subs):  # 테스트용 학생
    return Student(i, f"2026{i:03d}", f"학생{i}", main, tuple(subs))


def make_state(students, members=None, teams=3):
    """테스트용 마감 뒤 상태. members={팀 번호: [학생 id]}."""
    members = members or {}
    return State(
        students={s.id: s for s in students},
        team_ids=tuple(range(1, teams + 1)),
        members={t: tuple(members.get(t, ())) for t in range(1, teams + 1)},
        applications=(),
        votes={},
        closed=True,
    )


# 역할 약자 -> 역할 이름 (테스트 데이터를 짧게 쓰려고)
PL, WP, FD, TT, RW, PT = ROLES


def appendix1_after_close():
    """부록 1 마감 뒤: 팀1 {1,4}, 팀2 {2,5}, 팀3 {3,6}, 팀 없음 {7,8}."""
    students = [
        S(1, PL, RW), S(2, PL, PT), S(3, PL, TT),
        S(4, WP, FD), S(5, WP), S(6, WP, TT),
        S(7, FD, PT), S(8, TT, RW, PT),
    ]
    return make_state(students, {1: [1, 4], 2: [2, 5], 3: [3, 6]}, teams=3)


def appendix3_after_close():
    """부록 3 마감 뒤: 팀1 {1,4,7}, 팀2 {2,8,5}, 팀3 {11,15}, 팀4 빈 팀, 팀 없음 8명."""
    students = [
        S(1, PL, RW), S(2, PL, PT), S(3, PL, TT),
        S(4, WP, PL), S(5, WP, FD), S(6, WP, RW),
        S(7, FD, PT, TT), S(8, FD, TT), S(9, FD, WP),
        S(10, TT, RW), S(11, TT, PT), S(12, TT, PL),
        S(13, RW, PT, FD), S(14, RW, TT), S(15, PT, FD), S(16, PT),
    ]
    members = {1: [1, 4, 7], 2: [2, 8, 5], 3: [11, 15]}
    return make_state(students, members, teams=4)


def plan_of(result):
    return sorted(result.items())


class ValidatePlanTest(unittest.TestCase):
    def test_valid_plan_has_no_problems(self):
        state = appendix1_after_close()
        self.assertEqual(validate_plan(state, [(7, 1), (8, 2)]), [])

    def test_validate_plan_reports_each_check(self):
        state = appendix1_after_close()
        # 1) 배정되지 않은 학생, 팀이 없는 학생이 아닌 학생, 없는 팀
        self.assertIn("학생8이(가) 배정되지 않음", validate_plan(state, [(7, 1)]))
        self.assertIn("학생1은(는) 팀이 없는 학생이 아님", validate_plan(state, [(7, 1), (8, 2), (1, 3)]))
        self.assertIn("없는 팀 9", validate_plan(state, [(7, 1), (8, 9)]))
        # 2) 한 학생이 두 팀 이상에 배정됨
        self.assertIn("학생7이(가) 두 팀 이상에 배정됨", validate_plan(state, [(7, 1), (7, 2), (8, 2)]))

        # 3) 팀 인원이 2~4명이 아님 (빈 팀 / 5명)
        hard = appendix3_after_close()
        good = [(3, 3), (6, 3), (9, 4), (12, 4), (13, 4), (16, 4), (10, 1), (14, 2)]
        self.assertEqual(validate_plan(hard, good), [])  # 바른 배정
        empty_team4 = [(3, 3), (6, 3), (9, 3), (12, 3), (13, 3), (16, 3), (10, 1), (14, 2)]
        self.assertIn("팀4의 인원이 0명임(2~4명이어야 함)", validate_plan(hard, empty_team4))
        five_in_team1 = [(3, 1), (6, 1), (9, 4), (12, 4), (13, 4), (16, 4), (10, 2), (14, 3)]
        self.assertIn("팀1의 인원이 5명임(2~4명이어야 함)", validate_plan(hard, five_in_team1))

        # 4) 팀 안에서 주 역할 중복
        team_state = make_state(
            [S(1, PL), S(2, WP), S(3, PL, RW)], {1: [1, 2]}, teams=1
        )
        self.assertEqual(
            validate_plan(team_state, [(3, 1)]),
            ["팀1에 주 역할이 같은 학생이 있음(기획자)"],
        )

        # 5) 필수 포함 역할: 공고 예
        no_web = make_state(
            [S(1, PL), S(2, WP), S(3, PL), S(4, TT), S(5, FD)],
            {1: [1, 2], 2: [3, 4]},
            teams=2,
        )
        self.assertEqual(
            validate_plan(no_web, [(5, 2)]),
            ["팀2에 웹 프로그래머를 맡을 학생이 없음"],
        )

    def test_check5_messages_for_each_missing_role(self):
        no_planner = make_state([S(1, TT), S(2, WP)], {1: [1, 2]}, teams=1)
        self.assertEqual(validate_plan(no_planner, []), ["팀1에 기획자를 맡을 학생이 없음"])

        neither = make_state([S(1, TT), S(2, FD)], {1: [1, 2]}, teams=1)
        self.assertEqual(
            validate_plan(neither, []),
            ["팀1에 기획자를 맡을 학생이 없음", "팀1에 웹 프로그래머를 맡을 학생이 없음"],
        )

        one_flexible = make_state([S(1, PL, WP), S(2, TT)], {1: [1, 2]}, teams=1)
        self.assertEqual(
            validate_plan(one_flexible, []),
            ["팀1에 기획자와 웹 프로그래머를 따로 맡을 학생이 없음"],
        )

        # 둘 다 가능한 학생이 있어도 다른 학생이 하나라도 맡을 수 있으면 통과
        two_people = make_state([S(1, PL, WP), S(2, TT, WP)], {1: [1, 2]}, teams=1)
        self.assertEqual(validate_plan(two_people, []), [])

    def test_problems_are_ordered_by_check_then_team(self):
        state = make_state(
            [S(1, PL), S(2, TT), S(3, TT, PL), S(4, FD), S(5, RW), S(6, PT, WP)],
            {1: [1, 2], 2: [3, 4]},
            teams=3,
        )
        # 학생5, 6은 팀이 없다. 학생5는 팀2에, 학생6은 팀 9에 배정(없는 팀).
        problems = validate_plan(state, [(5, 2), (6, 9), (1, 3)])
        self.assertEqual(
            problems,
            [
                "학생6이(가) 배정되지 않음",               # 1) 배정되지 않음 (없는 팀은 배정으로 안 침)
                "학생1은(는) 팀이 없는 학생이 아님",       # 1) 팀 없는 학생 아님
                "없는 팀 9",                              # 1) 없는 팀
                "팀3의 인원이 0명임(2~4명이어야 함)",       # 3)
                "팀1에 웹 프로그래머를 맡을 학생이 없음",   # 5) 팀 번호 순서
                "팀2에 웹 프로그래머를 맡을 학생이 없음",
                "팀3에 기획자를 맡을 학생이 없음",
                "팀3에 웹 프로그래머를 맡을 학생이 없음",
            ],
        )

    def test_check1_order_missing_then_not_free_then_unknown(self):
        state = appendix1_after_close()
        problems = validate_plan(state, [(2, 7), (5, 1), (99, 1)])
        self.assertEqual(
            problems,
            [
                "학생7이(가) 배정되지 않음",
                "학생8이(가) 배정되지 않음",
                "학생5은(는) 팀이 없는 학생이 아님",   # 팀 번호가 작은 줄이 먼저
                "학생2은(는) 팀이 없는 학생이 아님",
                "없는 학생 99",
                "없는 팀 7",
            ],
        )

    def test_pairs_for_students_with_teams_are_ignored_after_reporting(self):
        state = appendix1_after_close()
        # 학생1을 팀3에 또 넣는 줄은 보고만 하고 팀 구성에는 반영하지 않는다.
        problems = validate_plan(state, [(7, 1), (8, 2), (1, 3)])
        self.assertEqual(problems, ["학생1은(는) 팀이 없는 학생이 아님"])

    def test_same_pair_twice_is_not_two_teams(self):
        state = appendix1_after_close()
        self.assertEqual(validate_plan(state, [(7, 1), (7, 1), (8, 2)]), [])

    def test_duplicated_main_role_reported_once_per_role(self):
        state = make_state(
            [S(1, PL), S(2, WP), S(3, PL), S(4, WP, PL), S(5, TT)],
            {1: [1, 2]},
            teams=1,
        )
        problems = validate_plan(state, [(3, 1), (4, 1), (5, 1)])
        self.assertEqual(
            problems,
            [
                "팀1의 인원이 5명임(2~4명이어야 함)",
                "팀1에 주 역할이 같은 학생이 있음(기획자)",
                "팀1에 주 역할이 같은 학생이 있음(웹 프로그래머)",
            ],
        )


class SolveTest(unittest.TestCase):
    def test_solve_easy_after_close(self):
        state = appendix1_after_close()
        result = solve(state)
        self.assertIsNotNone(result)
        self.assertEqual(sorted(result), [7, 8])
        self.assertEqual(validate_plan(state, plan_of(result)), [])

    def test_solve_hard_needs_sub_roles(self):
        state = appendix3_after_close()
        result = solve(state)
        self.assertIsNotNone(result)
        self.assertEqual(sorted(result), [3, 6, 9, 10, 12, 13, 14, 16])
        self.assertEqual(validate_plan(state, plan_of(result)), [])
        # 팀3(학생11 TT, 학생15 PT)과 빈 팀4는 보조 역할로 기획자·웹 프로그래머를 채웠다.
        for team_id in (3, 4):
            members = state.team_members(team_id) + [
                state.students[i] for i, t in result.items() if t == team_id
            ]
            self.assertTrue(any(m.can(PLANNER) for m in members))
            self.assertTrue(any(m.can(WEB) for m in members))

    def test_solve_is_deterministic(self):
        state = appendix3_after_close()
        self.assertEqual(solve(state), solve(state))

    def test_solve_does_not_move_existing_members(self):
        state = appendix3_after_close()
        result = solve(state)
        placed_ids = {i for ids in state.members.values() for i in ids}
        self.assertTrue(placed_ids.isdisjoint(result))

    def test_solve_without_unassigned_students(self):
        ok = make_state([S(1, PL), S(2, WP)], {1: [1, 2]}, teams=1)
        self.assertEqual(solve(ok), {})
        # 팀 인원이 모자라면(빈 팀) 아무도 더 배정할 수 없으므로 없음
        with_empty_team = make_state([S(1, PL), S(2, WP)], {1: [1, 2]}, teams=2)
        self.assertIsNone(solve(with_empty_team))
        # 이미 필수 역할을 못 채운 팀이 있어도 없음
        no_web = make_state([S(1, PL), S(2, TT)], {1: [1, 2]}, teams=1)
        self.assertIsNone(solve(no_web))

    def test_solve_gives_up_when_an_existing_team_is_already_hopeless(self):
        # 3명이 있는데 기획자도 웹 프로그래머도 못 맡음: 남은 자리 1개로 둘을 채울 수 없다.
        state = make_state(
            [S(1, TT), S(2, FD), S(3, RW), S(4, PL, WP), S(5, PT)],
            {1: [1, 2, 3]},
            teams=1,
        )
        self.assertIsNone(solve(state))

    def test_solve_none_and_explain(self):
        # 팀3이 4명이 다 찼는데 웹 프로그래머를 맡을 학생이 없음
        full_without_web = make_state(
            [
                S(1, PL), S(2, WP), S(3, PL), S(4, WP),
                S(5, PL), S(6, FD), S(7, TT), S(8, RW),
                S(9, PT, WP), S(10, FD),
            ],
            {1: [1, 2], 2: [3, 4], 3: [5, 6, 7, 8]},
            teams=3,
        )
        self.assertIsNone(solve(full_without_web))
        self.assertIn("팀3에 웹 프로그래머를 맡을 학생이 없음", explain_no_assignment(full_without_web))

        # 남은 자리 3개, 팀 없는 학생 4명
        too_many = make_state(
            [
                S(1, PL), S(2, WP), S(3, FD),
                S(4, PL), S(5, WP),
                S(6, PL), S(7, WP), S(8, FD), S(9, TT),
                S(10, RW), S(11, PT), S(12, TT), S(13, FD),
            ],
            {1: [1, 2, 3], 2: [4, 5], 3: [6, 7, 8, 9]},
            teams=3,
        )
        self.assertIsNone(solve(too_many))
        self.assertEqual(
            explain_no_assignment(too_many),
            ["남은 자리보다 팀이 없는 학생이 많음 (학생 4명, 자리 3개)"],
        )

        # 빈 팀 2개를 채울 웹 프로그래머가 1명뿐
        one_web = make_state(
            [S(1, PL), S(2, PL, RW), S(3, WP), S(4, FD)],
            teams=2,
        )
        self.assertIsNone(solve(one_web))
        self.assertEqual(
            explain_no_assignment(one_web),
            ["웹 프로그래머를 맡을 학생이 모자람 (필요 2명, 가능 1명)"],
        )

        # 위 어느 것에도 안 걸리지만 풀리지 않음: 둘 다 가능한 학생 1명과 아무것도 못 맡는 학생 1명
        combo = make_state([S(1, PL, WP), S(2, FD)], teams=1)
        self.assertIsNone(solve(combo))
        self.assertEqual(explain_no_assignment(combo), [FALLBACK])

    def test_explain_not_enough_students_to_reach_two_per_team(self):
        state = make_state(
            [S(1, PL, WP), S(2, PL, WP), S(3, PL, WP), S(4, FD), S(5, TT)],
            teams=3,
        )
        self.assertIsNone(solve(state))
        self.assertEqual(
            explain_no_assignment(state),
            ["팀을 2명 이상으로 채울 학생이 모자람 (필요 6명, 있음 5명)"],
        )

    def test_explain_single_flexible_member_message(self):
        state = make_state([S(1, PL, WP), S(2, TT), S(3, FD), S(4, RW)], {1: [1, 2]}, teams=1)
        self.assertIsNone(solve(state))
        self.assertEqual(
            explain_no_assignment(state),
            ["팀1에 기획자와 웹 프로그래머를 따로 맡을 학생이 없음"],
        )

    def test_explain_candidate_blocked_by_same_main_role(self):
        # 팀1에 기획자가 없다. 기획자를 맡을 수 있는 학생3은 주 역할 테스터가 팀1의 테스터와 겹쳐 넣을 수 없다.
        state = make_state(
            [S(1, WP), S(2, TT), S(3, TT, PL), S(4, FD)],
            {1: [1, 2]},
            teams=1,
        )
        self.assertIsNone(solve(state))
        self.assertEqual(explain_no_assignment(state), ["팀1에 기획자를 맡을 학생이 없음"])

    def test_explain_lists_every_reason_in_fixed_order(self):
        # 팀1: 4명 가득 + 웹 프로그래머 없음 / 팀2: 빈 팀. 팀 없는 학생 5명, 자리 4개.
        state = make_state(
            [
                S(1, PL), S(2, TT), S(3, FD), S(4, RW),
                S(5, PT), S(6, PT), S(7, PT), S(8, PT), S(9, PT),
            ],
            {1: [1, 2, 3, 4]},
            teams=2,
        )
        self.assertIsNone(solve(state))
        self.assertEqual(
            explain_no_assignment(state),
            [
                "팀1에 웹 프로그래머를 맡을 학생이 없음",
                "팀2에 기획자를 맡을 학생이 없음",
                "팀2에 웹 프로그래머를 맡을 학생이 없음",
                "남은 자리보다 팀이 없는 학생이 많음 (학생 5명, 자리 4개)",
                "기획자를 맡을 학생이 모자람 (필요 1명, 가능 0명)",
                "웹 프로그래머를 맡을 학생이 모자람 (필요 2명, 가능 0명)",
            ],
        )

    def test_solve_is_fast_for_eight_students_and_four_teams(self):
        start = time.perf_counter()
        self.assertIsNotNone(solve(appendix3_after_close()))
        self.assertLess(time.perf_counter() - start, 1.0)

        # 모든 경우를 따져야 "없음"을 알 수 있는 경우: 학생 7명은 기획자·웹 프로그래머를 모두 맡을 수 있고
        # 1명은 둘 다 못 맡는다. 팀 4개가 2명씩 8명이 필요해 그 1명이 들어갈 곳이 없다.
        students = [
            S(1, PL, WP), S(2, WP, PL), S(3, FD, PL, WP), S(4, TT, PL, WP),
            S(5, RW, PL, WP), S(6, PT, PL, WP), S(7, FD, WP, PL), S(8, FD),
        ]
        hopeless = make_state(students, teams=4)
        start = time.perf_counter()
        self.assertIsNone(solve(hopeless))
        self.assertLess(time.perf_counter() - start, 1.0)

    def test_solve_quickly_rejects_role_shortage_no_single_role_count_shows(self):
        # 팀 6개가 모두 비어 있고 학생 16명. 기획자(학생 3·8·9·12·14·16)도 웹 프로그래머(학생 2·4·7·10·11·14)도
        # 6명씩 맡을 수 있어 역할별로 세면 모자라지 않다. 하지만 학생 14가 둘 다 맡을 수 있어
        # 서로 다른 학생으로 채울 수 있는 자리는 11개뿐이라 12개 자리(팀마다 둘)를 채울 수 없다.
        students = [
            S(1, PT, FD), S(2, RW, FD, WP), S(3, PL), S(4, RW, FD, WP),
            S(5, RW), S(6, FD, TT), S(7, RW, WP), S(8, PT, TT, PL),
            S(9, PL, PT, RW), S(10, WP), S(11, FD, WP), S(12, RW, PL, TT),
            S(13, PT, FD), S(14, TT, WP, PL), S(15, RW), S(16, FD, PL, TT),
        ]
        state = make_state(students, teams=6)
        start = time.perf_counter()
        self.assertIsNone(solve(state))
        self.assertLess(time.perf_counter() - start, 1.0)
        self.assertEqual(explain_no_assignment(state), [FALLBACK])


def random_state(rng):
    """무작위 작은 상태: 팀 2~4개, 팀마다 기존 팀원 0~3명(주 역할 중복 없음), 팀 없는 학생 0~6명.

    절반은 규칙을 지키는 팀을 먼저 만든 뒤 일부 팀원을 "팀이 없는 학생"으로 빼서 만든다(배정이 있는 상태).
    그중 절반은 빠진 학생 한 명의 역할을 새로 뽑는다(배정이 있을 수도 없을 수도 있음).
    나머지 절반은 처음부터 마구 만든다(대부분 배정이 없음).
    """

    def student(student_id, main, must_do=None):
        others = [r for r in ROLES if r != main]
        subs = rng.sample(others, rng.choice([0, 1, 1, 2]))
        if must_do and must_do != main and must_do not in subs:
            subs = subs[:1] + [must_do]  # 보조 역할은 최대 2개
        return S(student_id, main, *subs)

    teams = rng.randint(2, 4)
    students, members = [], {}
    if rng.random() < 0.5:
        # 팀마다 2~4명을 규칙대로 만든다. 팀에서 빼는 수는 남는 팀원이 3명 이하, 전체 6명 이하가 되게 정한다.
        while True:
            sizes = [rng.randint(2, 4) for _ in range(teams)]
            leaving = [rng.randint(max(0, size - 3), size) for size in sizes]
            if sum(leaving) <= 6:
                break
        free_ids = []
        for team_id, (size, out) in enumerate(zip(sizes, leaving), start=1):
            planner, web = rng.sample(range(size), 2)
            team_ids = []
            for i, main in enumerate(rng.sample(ROLES, size)):
                must_do = PLANNER if i == planner else WEB if i == web else None
                students.append(student(len(students) + 1, main, must_do))
                team_ids.append(students[-1].id)
            rng.shuffle(team_ids)
            free_ids += team_ids[:out]
            members[team_id] = team_ids[out:]
        if free_ids and rng.random() < 0.5:
            swapped = rng.choice(free_ids)
            students[swapped - 1] = student(swapped, rng.choice(ROLES))
    else:
        for team_id in range(1, teams + 1):
            members[team_id] = []
            for main in rng.sample(ROLES, rng.randint(0, 3)):
                students.append(student(len(students) + 1, main))
                members[team_id].append(students[-1].id)
        for _ in range(rng.randint(0, 6)):
            students.append(student(len(students) + 1, rng.choice(ROLES)))
    return make_state(students, members, teams=teams)


def independent_plan_ok(state, free, assignment):
    """validate_plan과 따로 쓴 단순 검사: assignment[i]는 free[i]의 팀 번호."""
    for team_id in state.team_ids:
        team = state.team_members(team_id) + [s for s, t in zip(free, assignment) if t == team_id]
        if not 2 <= len(team) <= 4:
            return False
        mains = [m.main_role for m in team]
        if len(set(mains)) != len(mains):
            return False
        if not any(a.id != b.id and a.can(PLANNER) and b.can(WEB) for a in team for b in team):
            return False
    return True


class BruteForceTest(unittest.TestCase):
    def test_solve_matches_brute_force(self):
        rng = random.Random(20261025)
        solvable = unsolvable = 0
        for _ in range(200):
            state = random_state(rng)
            free = state.unassigned()
            exists = any(
                independent_plan_ok(state, free, assignment)
                for assignment in itertools.product(state.team_ids, repeat=len(free))
            )
            result = solve(state)
            if exists:
                solvable += 1
                self.assertIsNotNone(result, "배정이 있는데 solve가 None")
                self.assertEqual(sorted(result), [s.id for s in free])
                self.assertTrue(set(result.values()) <= set(state.team_ids))
                self.assertEqual(validate_plan(state, plan_of(result)), [])
                self.assertTrue(independent_plan_ok(state, free, [result[s.id] for s in free]))
            else:
                unsolvable += 1
                self.assertIsNone(result, "배정이 없는데 solve가 배정을 냄")
        # 두 경우가 모두 충분히 나와야 비교가 의미 있다.
        self.assertGreaterEqual(solvable, 30)
        self.assertGreaterEqual(unsolvable, 30)

    def test_validate_plan_agrees_with_independent_check(self):
        rng = random.Random(7)
        for _ in range(60):
            state = random_state(rng)
            free = state.unassigned()
            for assignment in itertools.islice(
                itertools.product(state.team_ids, repeat=len(free)), 300
            ):
                pairs = [(s.id, t) for s, t in zip(free, assignment)]
                self.assertEqual(
                    validate_plan(state, pairs) == [],
                    independent_plan_ok(state, free, assignment),
                    (pairs, validate_plan(state, pairs)),
                )

    def test_explain_reasons_are_sound_and_always_given_when_unsolvable(self):
        rng = random.Random(99)
        for _ in range(200):
            state = random_state(rng)
            reasons = explain_no_assignment(state)
            self.assertTrue(reasons)
            if solve(state) is not None:
                # 배정이 있으면 "불가능" 이유는 하나도 걸리면 안 된다(마지막 문구만 나옴).
                self.assertEqual(reasons, [FALLBACK], state)


if __name__ == "__main__":
    unittest.main()
