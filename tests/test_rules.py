"""역할 상수, 필수 역할 자리 계산, 지원·투표·마감 판단(teamapp/rules.py) 테스트."""
import unittest
from datetime import datetime

from teamapp.rules import (
    ABBR,
    ROLES,
    Application,
    Outcome,
    State,
    Student,
    VoteDecision,
    apply_lock_reason,
    decide_apply,
    decide_close,
    decide_vote,
    required_filled,
    seat_problem,
    unmet_roles,
)


def S(i, main, *subs):  # 테스트용 학생
    return Student(i, f"2026{i:03d}", f"학생{i}", main, tuple(subs))


def App(app_id, team_id, student_id, status="대기"):  # 테스트용 지원
    return Application(app_id, team_id, student_id, status)


def make_state(students, members=None, apps=(), votes=None, closed=False, teams=3):
    """테스트용 상태. members={팀 번호: [학생 id]}, votes={지원 id: {투표자 id: 투표}}."""
    members = members or {}
    return State(
        students={s.id: s for s in students},
        team_ids=tuple(range(1, teams + 1)),
        members={t: tuple(members.get(t, ())) for t in range(1, teams + 1)},
        applications=tuple(apps),
        votes={a: dict(v) for a, v in (votes or {}).items()},
        closed=closed,
    )


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


class StateTest(unittest.TestCase):
    def setUp(self):
        self.students = [S(3, "테스터"), S(1, "기획자"), S(2, "웹 프로그래머"), S(7, "발표자")]
        self.state = make_state(
            self.students,
            members={2: [2, 1]},
            apps=[App(9, 1, 7), App(4, 3, 3), App(2, 1, 3, "거절"), App(5, 1, 1, "대기")],
        )

    def test_team_name_and_team_of(self):
        self.assertEqual(self.state.team_name(3), "팀3")
        self.assertEqual(self.state.team_of(1), 2)
        self.assertIsNone(self.state.team_of(7))

    def test_team_members_returns_students(self):
        self.assertEqual([s.id for s in self.state.team_members(2)], [2, 1])
        self.assertEqual(self.state.team_members(1), [])

    def test_pending_of_ignores_decided_applications(self):
        self.assertEqual(self.state.pending_of(3).id, 4)
        self.assertEqual(self.state.pending_of(7).id, 9)
        self.assertIsNone(make_state(self.students, apps=[App(2, 1, 3, "거절")]).pending_of(3))

    def test_application_lookup(self):
        self.assertEqual(self.state.application(9).student_id, 7)
        self.assertIsNone(self.state.application(99))

    def test_pending_for_team_is_sorted_by_id_and_only_pending(self):
        self.assertEqual([a.id for a in self.state.pending_for_team(1)], [5, 9])
        self.assertEqual(self.state.pending_for_team(2), [])

    def test_unassigned_sorted_by_student_no(self):
        # 팀 2에 학생 1, 2가 있으므로 학생 3, 7이 남는다. 학번 순서로 돌려준다.
        self.assertEqual([s.id for s in self.state.unassigned()], [3, 7])
        state = make_state([S(7, "테스터"), S(2, "발표자"), S(5, "기획자")])
        self.assertEqual([s.id for s in state.unassigned()], [2, 5, 7])
        # id 순서와 학번 순서가 다르면 학번을 따른다.
        late = Student(1, "2026900", "나중", "테스터")
        early = Student(2, "2026100", "먼저", "발표자")
        self.assertEqual([s.id for s in make_state([late, early]).unassigned()], [2, 1])

    def test_application_created_at_is_optional(self):
        self.assertIsNone(App(1, 1, 1).created_at)
        when = datetime(2026, 10, 25, 9, 0)
        self.assertEqual(Application(1, 1, 1, "대기", "", when).created_at, when)

    def test_outcome_text(self):
        self.assertEqual(Outcome("막힘", "모집이 마감됨").text, "막힘: 모집이 마감됨")


class ApplyTest(unittest.TestCase):
    def test_apply_empty_team_is_approved(self):
        state = make_state([S(5, "테스터")])
        out = decide_apply(state, 5, 1)
        self.assertEqual(out.text, "승인: 빈 팀이라 바로 승인되었습니다")

    def test_apply_team_with_members_waits(self):
        state = make_state([S(1, "기획자"), S(5, "테스터")], members={1: [1]})
        out = decide_apply(state, 5, 1)
        self.assertEqual(out.text, "대기: 지원했습니다. 팀원 모두의 투표를 기다립니다")

    def test_apply_blocked_order(self):
        four = [S(1, "기획자"), S(2, "웹 프로그래머"), S(3, "테스터"), S(4, "발표자")]
        cases = [
            (
                "마감",
                make_state([S(5, "보고서 작성자")], closed=True),
                "막힘: 모집이 마감됨",
            ),
            (
                "이미 팀 있음",
                make_state([S(1, "기획자"), S(5, "테스터")], members={2: [5]}),
                "막힘: 이미 팀이 있음",
            ),
            (
                "대기 지원 있음",
                make_state([S(1, "기획자"), S(5, "테스터")], apps=[App(1, 2, 5)]),
                "막힘: 결과를 기다리는 지원이 있음",
            ),
            (
                "꽉 참",
                make_state(four + [S(5, "보고서 작성자")], members={1: [1, 2, 3, 4]}),
                "막힘: 팀이 꽉 참",
            ),
            (
                "주 역할 중복",
                make_state([S(1, "테스터"), S(5, "테스터", "기획자")], members={1: [1]}),
                "막힘: 같은 주 역할의 팀원이 있음",
            ),
            (
                "자리 모자람",
                make_state(
                    [S(1, "테스터"), S(2, "발표자"), S(5, "보고서 작성자")],
                    members={1: [1, 2]},
                ),
                "막힘: 필수 포함 역할 자리가 모자람 (남는 자리 1개, 채워야 할 역할: 기획자, 웹 프로그래머)",
            ),
        ]
        for name, state, expected in cases:
            with self.subTest(name):
                self.assertEqual(decide_apply(state, 5, 1).text, expected)

    def test_apply_blocked_checks_run_in_prd_order(self):
        four = [S(1, "기획자"), S(2, "웹 프로그래머"), S(3, "테스터"), S(4, "발표자")]
        # 마감 + 이미 팀 있음 -> 마감이 먼저
        state = make_state(four + [S(5, "테스터")], members={2: [5]}, closed=True)
        self.assertEqual(decide_apply(state, 5, 1).text, "막힘: 모집이 마감됨")
        # 이미 팀 있음 + 대기 지원 있음 -> 이미 팀 있음이 먼저
        state = make_state(four + [S(5, "테스터")], members={2: [5]}, apps=[App(1, 3, 5)])
        self.assertEqual(decide_apply(state, 5, 1).text, "막힘: 이미 팀이 있음")
        # 대기 지원 있음 + 꽉 참 -> 대기 지원 있음이 먼저
        state = make_state(
            four + [S(5, "보고서 작성자")], members={1: [1, 2, 3, 4]}, apps=[App(1, 2, 5)]
        )
        self.assertEqual(decide_apply(state, 5, 1).text, "막힘: 결과를 기다리는 지원이 있음")
        # 꽉 참 + 주 역할 중복 -> 꽉 참이 먼저
        state = make_state(four + [S(5, "테스터")], members={1: [1, 2, 3, 4]})
        self.assertEqual(decide_apply(state, 5, 1).text, "막힘: 팀이 꽉 참")
        # 주 역할 중복 + 자리 모자람 -> 주 역할 중복이 먼저
        state = make_state(
            [S(1, "테스터"), S(2, "발표자"), S(5, "발표자")], members={1: [1, 2]}
        )
        self.assertEqual(decide_apply(state, 5, 1).text, "막힘: 같은 주 역할의 팀원이 있음")

    def test_apply_to_unknown_team_is_blocked(self):
        state = make_state([S(5, "테스터")], teams=3)
        self.assertEqual(decide_apply(state, 5, 4).text, "막힘: 없는 팀입니다")

    def test_apply_decision_uses_sub_roles_for_required_seats(self):
        # 테스터 + 발표자 팀에 기획자/웹 프로그래머를 둘 다 할 수 있는 학생이 지원하면
        # 필수 역할 하나만 채우지만, 남는 자리 1개로 나머지 1개를 채울 수 있다.
        state = make_state(
            [S(1, "테스터"), S(2, "발표자"), S(5, "보고서 작성자", "기획자", "웹 프로그래머")],
            members={1: [1, 2]},
        )
        self.assertEqual(
            decide_apply(state, 5, 1).text,
            "대기: 지원했습니다. 팀원 모두의 투표를 기다립니다",
        )

    def test_apply_lock_reason(self):
        students = [S(1, "기획자"), S(5, "테스터")]
        pending = make_state(students, apps=[App(1, 1, 5)])
        self.assertEqual(
            apply_lock_reason(pending, 5),
            "팀1에 낸 지원이 대기 중입니다. 결과가 나온 뒤 다시 지원할 수 있습니다",
        )
        on_team = make_state(students, members={2: [5]})
        self.assertEqual(apply_lock_reason(on_team, 5), "이미 팀2에 속해 있습니다")
        closed = make_state(students, closed=True)
        self.assertEqual(apply_lock_reason(closed, 5), "모집이 마감되었습니다")
        self.assertIsNone(apply_lock_reason(make_state(students), 5))

    def test_apply_lock_reason_order_matches_apply(self):
        students = [S(1, "기획자"), S(5, "테스터")]
        # 마감 + 팀 있음 + 대기 지원 -> 마감 > 팀 있음 > 대기 지원
        state = make_state(students, members={2: [5]}, apps=[App(1, 1, 5)], closed=True)
        self.assertEqual(apply_lock_reason(state, 5), "모집이 마감되었습니다")
        state = make_state(students, members={2: [5]}, apps=[App(1, 1, 5)])
        self.assertEqual(apply_lock_reason(state, 5), "이미 팀2에 속해 있습니다")

    def test_apply_lock_reason_ignores_decided_applications(self):
        students = [S(1, "기획자"), S(5, "테스터")]
        state = make_state(students, apps=[App(1, 1, 5, "거절")])
        self.assertIsNone(apply_lock_reason(state, 5))


class VoteTest(unittest.TestCase):
    # 팀1: 학생1(기획자), 학생2(웹 프로그래머). 학생5(테스터)가 팀1에 지원(지원 id 1).
    def setUp(self):
        self.students = [
            S(1, "기획자"),
            S(2, "웹 프로그래머"),
            S(5, "테스터"),
            S(6, "테스터"),
            S(8, "발표자"),
        ]

    def state(self, **kw):
        kw.setdefault("members", {1: [1, 2]})
        kw.setdefault("apps", [App(1, 1, 5)])
        return make_state(self.students, **kw)

    def test_vote_yes_waits_for_others(self):
        d = decide_vote(self.state(), 1, 1, "찬성")
        self.assertIsInstance(d, VoteDecision)
        self.assertEqual(d.outcome.text, "대기: 찬성했습니다. 다른 팀원의 투표를 기다립니다")
        self.assertTrue(d.record_vote)
        self.assertIsNone(d.final_status)

    def test_vote_no_rejects_immediately(self):
        d = decide_vote(self.state(), 1, 1, "거부")
        self.assertEqual(d.outcome.text, "거절: 거부하여 지원이 거절되었습니다")
        self.assertTrue(d.record_vote)
        self.assertEqual(d.final_status, "거절")

    def test_vote_all_yes_approves(self):
        d = decide_vote(self.state(votes={1: {2: "찬성"}}), 1, 1, "찬성")
        self.assertEqual(d.outcome.text, "승인: 팀원 모두 찬성하여 승인되었습니다")
        self.assertTrue(d.record_vote)
        self.assertEqual(d.final_status, "승인")

    def test_vote_recheck_rejects(self):
        # 같은 주 역할(테스터)의 학생6이 먼저 승인되어 팀원이 된 뒤, 학생5의 지원에 마지막 찬성이 온다.
        state = self.state(
            members={1: [1, 2, 6]},
            apps=[App(1, 1, 5), App(2, 1, 6, "승인")],
            votes={1: {1: "찬성", 2: "찬성"}},
        )
        d = decide_vote(state, 1, 6, "찬성")
        self.assertEqual(
            d.outcome.text,
            "거절: 승인 직전 검사에서 규칙에 맞지 않습니다 (같은 주 역할의 팀원이 있음)",
        )
        self.assertTrue(d.record_vote)
        self.assertEqual(d.final_status, "거절")

    def test_vote_recheck_rejects_when_team_became_full(self):
        # 팀이 그 사이 4명이 되었으면 승인 직전 검사에서 정원 초과로 거절된다.
        students = self.students + [S(9, "보고서 작성자")]
        state = make_state(
            students,
            members={1: [1, 2, 8, 9]},
            apps=[App(1, 1, 5)],
            votes={1: {1: "찬성", 2: "찬성", 8: "찬성"}},
        )
        d = decide_vote(state, 1, 9, "찬성")
        self.assertEqual(
            d.outcome.text,
            "거절: 승인 직전 검사에서 규칙에 맞지 않습니다 (팀이 꽉 참)",
        )
        self.assertEqual(d.final_status, "거절")

    def test_new_member_must_also_vote(self):
        # 지원 뒤에 학생8이 팀원이 되었다. 기존 팀원 1, 2만 찬성했으므로 아직 승인되지 않는다.
        state = self.state(members={1: [1, 2, 8]}, votes={1: {1: "찬성"}})
        d = decide_vote(state, 1, 2, "찬성")
        self.assertEqual(d.outcome.text, "대기: 찬성했습니다. 다른 팀원의 투표를 기다립니다")
        self.assertTrue(d.record_vote)
        self.assertIsNone(d.final_status)

    def test_votes_from_non_members_are_ignored(self):
        # 학생9의 찬성 기록은 지금 팀원이 아니므로 세지 않는다.
        state = self.state(votes={1: {9: "찬성"}})
        d = decide_vote(state, 1, 1, "찬성")
        self.assertEqual(d.outcome.text, "대기: 찬성했습니다. 다른 팀원의 투표를 기다립니다")
        self.assertIsNone(d.final_status)

    def test_vote_on_decided_application(self):
        for status, text in [
            ("승인", "승인: 이미 승인된 지원입니다"),
            ("거절", "거절: 이미 거절된 지원입니다"),
        ]:
            with self.subTest(status):
                state = self.state(apps=[App(1, 1, 5, status)])
                d = decide_vote(state, 1, 1, "찬성")
                self.assertEqual(d.outcome.text, text)
                self.assertFalse(d.record_vote)
                self.assertIsNone(d.final_status)

    def test_decided_application_is_reported_even_after_close_or_for_outsiders(self):
        state = self.state(apps=[App(1, 1, 5, "거절")], closed=True)
        self.assertEqual(decide_vote(state, 1, 8, "거부").outcome.text, "거절: 이미 거절된 지원입니다")

    def test_vote_twice(self):
        d = decide_vote(self.state(votes={1: {1: "찬성"}}), 1, 1, "찬성")
        self.assertEqual(
            d.outcome.text, "대기: 이미 투표했습니다. 다른 팀원의 투표를 기다립니다"
        )
        self.assertFalse(d.record_vote)
        self.assertIsNone(d.final_status)

    def test_vote_by_non_member(self):
        # 학생8은 다른 팀(팀2) 팀원, 학생6은 팀이 없다.
        for voter in (8, 6):
            with self.subTest(voter):
                state = self.state(members={1: [1, 2], 2: [8]})
                d = decide_vote(state, 1, voter, "찬성")
                self.assertEqual(d.outcome.text, "막힘: 이 팀의 팀원만 투표할 수 있습니다")
                self.assertFalse(d.record_vote)
                self.assertIsNone(d.final_status)

    def test_vote_after_close_is_blocked(self):
        d = decide_vote(self.state(closed=True), 1, 1, "찬성")
        self.assertEqual(d.outcome.text, "막힘: 모집이 마감됨")
        self.assertFalse(d.record_vote)
        self.assertIsNone(d.final_status)

    def test_close_check_comes_before_member_check(self):
        d = decide_vote(self.state(closed=True), 1, 8, "찬성")
        self.assertEqual(d.outcome.text, "막힘: 모집이 마감됨")

    def test_vote_on_unknown_application(self):
        d = decide_vote(self.state(), 99, 1, "찬성")
        self.assertEqual(d.outcome.text, "막힘: 없는 지원입니다")
        self.assertFalse(d.record_vote)
        self.assertIsNone(d.final_status)

    def test_vote_value_must_be_yes_or_no(self):
        d = decide_vote(self.state(), 1, 1, "기권")
        self.assertEqual(d.outcome.text, "막힘: 투표는 찬성이나 거부만 할 수 있습니다")
        self.assertFalse(d.record_vote)
        self.assertIsNone(d.final_status)

    def test_no_vote_after_a_yes_vote_by_someone_else_still_rejects(self):
        d = decide_vote(self.state(votes={1: {2: "찬성"}}), 1, 1, "거부")
        self.assertEqual(d.outcome.text, "거절: 거부하여 지원이 거절되었습니다")
        self.assertEqual(d.final_status, "거절")


class CloseTest(unittest.TestCase):
    def test_close(self):
        # 팀1: 학생1, 2 (2명, 그대로). 팀2: 학생3 혼자. 팀3: 학생11 혼자. 학생12의 지원이 대기 중.
        students = [S(1, "기획자"), S(2, "웹 프로그래머"), S(3, "테스터"), S(11, "발표자"), S(12, "테스터")]
        state = make_state(
            students,
            members={1: [1, 2], 2: [3], 3: [11]},
            apps=[App(7, 1, 12)],
        )
        outcome, rejected, removed = decide_close(state)
        self.assertEqual(rejected, (7,))
        self.assertEqual(removed, (3, 11))
        self.assertEqual(
            outcome.text,
            "승인: 모집을 마감했습니다. 거절한 지원: 학생12 · 팀에서 뺀 학생: 학생3, 학생11",
        )

    def test_close_with_nothing_to_do(self):
        state = make_state([S(1, "기획자"), S(2, "웹 프로그래머")], members={1: [1, 2]})
        outcome, rejected, removed = decide_close(state)
        self.assertEqual(rejected, ())
        self.assertEqual(removed, ())
        self.assertEqual(
            outcome.text,
            "승인: 모집을 마감했습니다. 거절한 지원: 없음 · 팀에서 뺀 학생: 없음",
        )

    def test_close_lists_names_in_application_id_and_team_order(self):
        students = [S(3, "테스터"), S(11, "발표자"), S(12, "테스터"), S(13, "기획자"), S(14, "발표자")]
        state = make_state(
            students,
            members={1: [11], 2: [3]},
            # 지원 id 순서가 튜플 순서와 다르다. 결과는 지원 id 순서.
            apps=[App(9, 3, 14), App(4, 3, 13), App(6, 3, 12, "거절"), App(5, 3, 12)],
        )
        outcome, rejected, removed = decide_close(state)
        self.assertEqual(rejected, (4, 5, 9))
        # 학번 순서가 아니라 팀 번호 순서(팀1의 학생11, 팀2의 학생3)
        self.assertEqual(removed, (11, 3))
        self.assertEqual(
            outcome.text,
            "승인: 모집을 마감했습니다. 거절한 지원: 학생13, 학생12, 학생14 · 팀에서 뺀 학생: 학생11, 학생3",
        )

    def test_close_keeps_empty_and_larger_teams(self):
        students = [S(1, "기획자"), S(2, "웹 프로그래머"), S(3, "테스터"), S(4, "발표자")]
        state = make_state(students, members={1: [1, 2, 3], 3: [4]})
        _, rejected, removed = decide_close(state)
        self.assertEqual(rejected, ())
        self.assertEqual(removed, (4,))

    def test_close_twice(self):
        state = make_state([S(1, "기획자")], members={1: [1]}, apps=[App(1, 2, 1)], closed=True)
        outcome, rejected, removed = decide_close(state)
        self.assertEqual(outcome.text, "막힘: 이미 마감되었습니다")
        self.assertEqual(rejected, ())
        self.assertEqual(removed, ())


if __name__ == "__main__":
    unittest.main()
