"""쓰기 처리(teamapp/actions.py)가 결과마다 무엇을 쓰는지, 문구는 무엇인지, MemoryRepo가 DB 제약을 흉내 내는지 확인한다."""
import unittest

from teamapp import actions
from teamapp.roster import ADMIN_NO
from tests.memory_repo import MemoryRepo, fake_hash, fake_verify
from tests.scenarios import DATA_DIR, EASY, HARD, MEDIUM, Scenario, play

NOT_ADMIN = "막힘: 관리자만 할 수 있습니다"
CSV_HEADER = "student_no,name,main_role,sub_roles\n"
EASY_CSV = (DATA_DIR / EASY.csv_name).read_bytes()


def replay_until(scenario, last_step):
    """시나리오를 last_step까지만 재생한다. (저장소, 이름 -> 학생 id, 관리자 id)."""
    repo = MemoryRepo()
    steps = scenario.steps[: scenario.steps.index(last_step) + 1]
    play(repo, Scenario(scenario.csv_name, scenario.team_count, steps), fake_hash, fake_verify)
    ids = {s.name: s.id for s in repo.load_state().students.values()}
    return repo, ids, repo.find_account(ADMIN_NO).id


class RepoTestCase(unittest.TestCase):
    def setUp(self):
        self.repo = MemoryRepo()
        self.admin_id = self.repo.find_account(ADMIN_NO).id

    def reset(self, data=EASY_CSV, team_count=3, hasher=fake_hash):
        return actions.reset_roster(self.repo, self.admin_id, data, team_count, hasher)

    def application_of(self, repo, student_id):
        mine = [a for a in repo.load_state().applications if a.student_id == student_id]
        return mine[-1]


class ResetRosterTests(RepoTestCase):
    def test_success(self):
        outcome = self.reset()
        self.assertEqual(outcome.text, "승인: 학생 8명과 빈 팀 3개로 처음 상태를 만들었습니다")
        state = self.repo.load_state()
        self.assertEqual(len(state.students), 8)
        self.assertEqual(state.team_ids, (1, 2, 3))
        self.assertEqual(state.members, {1: (), 2: (), 3: ()})
        self.assertFalse(state.closed)
        first = state.students[1]
        self.assertEqual((first.student_no, first.name, first.main_role, first.sub_roles),
                         ("2026101", "학생1(PL;RW)", "기획자", ("보고서 작성자",)))

    def test_every_student_password_is_1234_hashed_with_hasher(self):
        calls = []

        def hasher(password):
            calls.append(password)
            return f"hash{len(calls)}:{password}"

        self.reset(hasher=hasher)
        self.assertEqual(calls, ["1234"] * 8)
        hashes = [self.repo.find_account(f"2026{100 + i}").password_hash for i in range(1, 9)]
        self.assertEqual(hashes, [f"hash{i}:1234" for i in range(1, 9)])
        # 관리자 계정은 그대로이다
        self.assertTrue(self.repo.find_account("admin").is_admin)

    def test_missing_file(self):
        for data in (None, b""):
            with self.subTest(data=data):
                self.assertEqual(self.reset(data).text, "막힘: 명단 CSV 파일을 올려 주세요")
        self.assertEqual(len(self.repo.load_state().students), 0)

    def test_team_count_must_be_int_from_1_to_8(self):
        for bad in (0, 9, -1, 100, "3", 3.0, True, None):
            with self.subTest(team_count=bad):
                self.assertEqual(self.reset(team_count=bad).text, "막힘: 팀 수는 1~8 사이여야 합니다")
        self.assertEqual(len(self.repo.load_state().students), 0)
        self.assertEqual(self.reset(team_count=1).text, "승인: 학생 8명과 빈 팀 1개로 처음 상태를 만들었습니다")
        self.assertEqual(self.reset(team_count=8).text, "승인: 학생 8명과 빈 팀 8개로 처음 상태를 만들었습니다")

    def test_roster_errors_show_the_message(self):
        cases = {
            b"a,b,c\n1,2,3\n": "머리글은 student_no,name,main_role,sub_roles 이어야 합니다",
            b"\n\n": "명단에 학생이 없습니다",
            (CSV_HEADER + "1,가,마법사,\n").encode(): "2번째 줄: 모르는 역할 '마법사'",
            b"\xff\xfe\x00": "UTF-8 CSV 파일이 아닙니다",
        }
        for data, message in cases.items():
            with self.subTest(message=message):
                self.assertEqual(self.reset(data).text, f"막힘: CSV 형식이 맞지 않습니다 ({message})")
        self.assertEqual(len(self.repo.load_state().students), 0)

    def test_unreadable_csv(self):
        text = "막힘: CSV 형식이 맞지 않습니다 (읽을 수 없는 CSV 파일입니다)"
        too_long_field = (CSV_HEADER + "x" * 200_000 + ",가,기획자,\n").encode()  # csv.Error
        nul_in_name = (CSV_HEADER + "1,가\x00나,기획자,\n").encode()
        nul_in_student_no = (CSV_HEADER + "1\x00,가,기획자,\n").encode()
        for data in (too_long_field, nul_in_name, nul_in_student_no):
            with self.subTest(size=len(data)):
                self.assertEqual(self.reset(data).text, text)
        self.assertEqual(len(self.repo.load_state().students), 0)

    def test_admin_is_checked_before_anything_else(self):
        self.reset()
        student_id = self.repo.find_account("2026101").id
        for data, count in [(EASY_CSV, 3), (None, 3), (b"", 0), (b"bad", 99)]:
            with self.subTest(count=count):
                self.assertEqual(actions.reset_roster(self.repo, student_id, data, count, fake_hash).text, NOT_ADMIN)
        self.assertEqual(len(self.repo.load_state().students), 8)

    def test_reset_clears_the_old_state(self):
        repo, ids, admin_id = replay_until(EASY, ("close", "승인"))
        self.assertTrue(repo.load_state().closed)
        outcome = actions.reset_roster(repo, admin_id, EASY_CSV, 2, fake_hash)
        self.assertEqual(outcome.kind, "승인")
        state = repo.load_state()
        self.assertEqual((state.applications, state.votes, state.closed), ((), {}, False))
        self.assertEqual(state.team_ids, (1, 2))
        self.assertEqual(state.members, {1: (), 2: ()})
        self.assertEqual(state.unassigned(), sorted(state.students.values(), key=lambda s: s.student_no))


class ApplyAndVoteWriteTests(RepoTestCase):
    def test_apply_to_empty_team_writes_approved_application_and_member(self):
        repo, ids, _ = replay_until(EASY, ("apply", "학생1(PL;RW)", 1, "승인"))
        app = self.application_of(repo, ids["학생1(PL;RW)"])
        self.assertEqual((app.team_id, app.status, app.reason), (1, "승인", "빈 팀에 지원"))
        self.assertEqual(repo.load_state().members[1], (ids["학생1(PL;RW)"],))

    def test_apply_to_team_with_members_writes_pending_application_only(self):
        repo, ids, _ = replay_until(EASY, ("apply", "학생4(WP;FD)", 1, "대기"))
        app = self.application_of(repo, ids["학생4(WP;FD)"])
        self.assertEqual((app.team_id, app.status, app.reason), (1, "대기", ""))
        self.assertEqual(repo.load_state().members[1], (ids["학생1(PL;RW)"],))

    def test_blocked_apply_writes_nothing(self):
        repo, ids, _ = replay_until(EASY, ("vote", "학생1(PL;RW)", "학생4(WP;FD)", "찬성", "승인"))
        before = repo.load_state()
        # 같은 주 역할(웹 프로그래머), 이미 팀 있음, 없는 팀
        self.assertEqual(actions.apply(repo, ids["학생5(WP)"], 1).text, "막힘: 같은 주 역할의 팀원이 있음")
        self.assertEqual(actions.apply(repo, ids["학생1(PL;RW)"], 2).text, "막힘: 이미 팀이 있음")
        self.assertEqual(actions.apply(repo, ids["학생5(WP)"], 9).text, "막힘: 없는 팀입니다")
        self.assertEqual(repo.load_state(), before)

    def test_all_yes_approves_and_adds_member(self):
        repo, ids, _ = replay_until(EASY, ("vote", "학생1(PL;RW)", "학생4(WP;FD)", "찬성", "승인"))
        state = repo.load_state()
        app = self.application_of(repo, ids["학생4(WP;FD)"])
        self.assertEqual((app.status, app.reason), ("승인", "팀원 모두 찬성"))
        self.assertEqual(state.members[1], (ids["학생1(PL;RW)"], ids["학생4(WP;FD)"]))
        self.assertEqual(state.votes[app.id], {ids["학생1(PL;RW)"]: "찬성"})

    def test_no_vote_rejects_with_reason(self):
        repo, ids, _ = replay_until(EASY, ("vote", "학생4(WP;FD)", "학생7(FD;PT)", "거부", "거절"))
        state = repo.load_state()
        app = self.application_of(repo, ids["학생7(FD;PT)"])
        self.assertEqual((app.status, app.reason), ("거절", "팀원이 거부함"))
        self.assertEqual(state.votes[app.id], {ids["학생1(PL;RW)"]: "찬성", ids["학생4(WP;FD)"]: "거부"})
        self.assertNotIn(ids["학생7(FD;PT)"], state.members[1])

    def test_partial_yes_only_records_the_vote(self):
        repo, ids, _ = replay_until(EASY, ("vote", "학생1(PL;RW)", "학생7(FD;PT)", "찬성", "대기"))
        state = repo.load_state()
        app = self.application_of(repo, ids["학생7(FD;PT)"])
        self.assertEqual((app.status, app.reason), ("대기", ""))
        self.assertEqual(state.votes[app.id], {ids["학생1(PL;RW)"]: "찬성"})

    def test_recheck_failure_rejects_with_reason(self):
        step = ("vote", "학생5(WP;FD)", "학생6(WP;RW)", "찬성", "거절")
        repo, ids, _ = replay_until(HARD, step)
        state = repo.load_state()
        app = self.application_of(repo, ids["학생6(WP;RW)"])
        self.assertEqual((app.status, app.reason), ("거절", "승인 직전 검사: 같은 주 역할의 팀원이 있음"))
        self.assertEqual(state.votes[app.id][ids["학생5(WP;FD)"]], "찬성")  # 마지막 찬성 표도 기록됨
        self.assertNotIn(ids["학생6(WP;RW)"], state.members[2])

    def test_votes_that_record_nothing(self):
        repo, ids, _ = replay_until(EASY, ("vote", "학생1(PL;RW)", "학생7(FD;PT)", "찬성", "대기"))
        app = self.application_of(repo, ids["학생7(FD;PT)"])
        before = repo.load_state()
        cases = [
            (ids["학생2(PL;PT)"], app.id, "찬성", "막힘: 이 팀의 팀원만 투표할 수 있습니다"),
            (ids["학생1(PL;RW)"], app.id, "거부", "대기: 이미 투표했습니다. 다른 팀원의 투표를 기다립니다"),
            (ids["학생4(WP;FD)"], 999, "찬성", "막힘: 없는 지원입니다"),
            (ids["학생4(WP;FD)"], app.id, "모름", "막힘: 투표는 찬성이나 거부만 할 수 있습니다"),
            # 이미 승인된 지원(학생4의 지원은 1번 팀 승인)
            (ids["학생1(PL;RW)"], self.application_of(repo, ids["학생4(WP;FD)"]).id, "찬성", "승인: 이미 승인된 지원입니다"),
        ]
        for voter, app_id, value, text in cases:
            with self.subTest(text=text):
                self.assertEqual(actions.vote(repo, voter, app_id, value).text, text)
        self.assertEqual(repo.load_state(), before)

    def test_vote_on_rejected_application_reports_it(self):
        repo, ids, _ = replay_until(EASY, ("vote", "학생4(WP;FD)", "학생7(FD;PT)", "거부", "거절"))
        app = self.application_of(repo, ids["학생7(FD;PT)"])
        before = repo.load_state()
        outcome = actions.vote(repo, ids["학생1(PL;RW)"], app.id, "찬성")
        self.assertEqual(outcome.text, "거절: 이미 거절된 지원입니다")
        self.assertEqual(repo.load_state(), before)


class CloseAndConfirmTests(RepoTestCase):
    def test_close_without_roster(self):
        self.assertEqual(actions.close(self.repo, self.admin_id).text, "막힘: 명단을 먼저 올려 주세요")
        self.assertFalse(self.repo.load_state().closed)

    def test_close_twice(self):
        self.reset()
        self.assertEqual(actions.close(self.repo, self.admin_id).kind, "승인")
        before = self.repo.load_state()
        self.assertEqual(actions.close(self.repo, self.admin_id).text, "막힘: 이미 마감되었습니다")
        self.assertEqual(self.repo.load_state(), before)

    def test_close_rejects_pending_removes_single_members_and_closes(self):
        repo, ids, admin_id = replay_until(MEDIUM, ("close", "승인"))
        state = repo.load_state()
        self.assertTrue(state.closed)
        self.assertEqual(state.members[1], (ids["학생1(PL;PT;TT)"], ids["학생5(WP;TT)"]))
        self.assertEqual(state.members[3], ())  # 학생3 빠짐
        self.assertEqual(state.members[4], ())  # 학생11 빠짐
        self.assertEqual([a for a in state.applications if a.status == "대기"], [])
        app = self.application_of(repo, ids["학생12(PT;RW;FD)"])
        self.assertEqual((app.status, app.reason), ("거절", "모집 마감"))
        # 투표로 이미 거절된 지원의 사유는 그대로이다
        self.assertEqual(self.application_of(repo, ids["학생10(TT;PL)"]).reason, "팀원이 거부함")

    def test_confirm_blocked_cases_write_nothing(self):
        repo, ids, admin_id = replay_until(EASY, ("apply", "학생1(PL;RW)", 1, "승인"))
        before = repo.load_state()
        self.assertEqual(actions.confirm(repo, admin_id, {2: 1}).text, "막힘: 모집을 먼저 마감해야 합니다")
        self.assertEqual(repo.load_state(), before)
        actions.close(repo, admin_id)
        before = repo.load_state()
        self.assertEqual(actions.confirm(repo, admin_id, {}).text, "막힘: 확정할 배정안이 없습니다")
        stale = "막힘: 배정안을 만든 뒤 상태가 바뀌었습니다. AI 배정을 다시 해 주세요"
        self.assertEqual(actions.confirm(repo, admin_id, {ids["학생2(PL;PT)"]: 9}).text, stale)  # 없는 팀
        self.assertEqual(repo.load_state(), before)

    def test_confirm_success_adds_members_and_counts_them(self):
        repo, ids, admin_id = replay_until(EASY, ("close", "승인"))
        plan = {ids["학생7(FD;PT)"]: 1, ids["학생8(TT;RW;PT)"]: 2}
        self.assertEqual(actions.confirm(repo, admin_id, plan).text, "승인: 배정안을 확정했습니다 (2명)")
        members = repo.load_state().members
        self.assertEqual(members[1][-1], ids["학생7(FD;PT)"])
        self.assertEqual(members[2][-1], ids["학생8(TT;RW;PT)"])
        self.assertEqual([len(members[t]) for t in (1, 2, 3)], [3, 3, 2])


class MemoryRepoTests(RepoTestCase):
    def test_admin_account(self):
        admin = self.repo.find_account("admin")
        self.assertEqual((admin.id, admin.name, admin.is_admin, admin.password_hash), (0, "관리자", True, "h:adminpw"))
        self.assertEqual(self.repo.get_account(0), admin)
        self.assertIsNone(self.repo.get_account(1))
        self.assertIsNone(self.repo.find_account("2026101"))
        custom = MemoryRepo(admin_password="비밀", hasher=lambda p: p[::-1])
        self.assertEqual(custom.find_account("admin").password_hash, "밀비")

    def test_student_ids_follow_roster_order_and_are_never_reused_after_reset(self):
        self.reset()
        first_ids = [s.id for s in self.repo.load_state().students.values()]
        self.assertEqual(first_ids, list(range(1, 9)))  # 처음에는 명단 순서대로 1부터
        account = self.repo.get_account(3)
        self.assertEqual((account.student_no, account.is_admin), ("2026103", False))
        self.assertEqual(self.repo.find_account("2026103"), account)
        self.reset(team_count=2)
        # 다시 올리면 예전 id는 모두 없어지고, 새 학생의 id는 예전 id보다 모두 크다
        second_ids = [s.id for s in self.repo.load_state().students.values()]
        self.assertEqual(len(second_ids), 8)
        self.assertGreater(min(second_ids), max(first_ids))
        self.assertEqual(second_ids, sorted(second_ids))  # 명단 순서
        for old_id in first_ids:
            self.assertIsNone(self.repo.get_account(old_id))
        self.assertEqual(self.repo.get_account(second_ids[0]).student_no, "2026101")
        self.assertEqual(self.repo.get_account(0).is_admin, True)  # 관리자는 id 0 그대로

    def test_add_member_takes_smallest_free_slot(self):
        self.reset()
        for student_id in (1, 2, 3):
            self.repo.add_member(1, student_id)
        self.repo.remove_member(1)
        self.repo.add_member(1, 4)  # 비어 있는 1번 자리에 들어가므로 가장 앞에 온다
        self.assertEqual(self.repo.load_state().members[1], (4, 2, 3))
        self.repo.add_member(1, 5)
        self.assertEqual(self.repo.load_state().members[1], (4, 2, 3, 5))

    def test_add_member_constraints(self):
        self.reset()
        for student_id in (1, 2, 3, 4):
            self.repo.add_member(1, student_id)
        with self.assertRaises(ValueError):
            self.repo.add_member(1, 5)  # 팀이 꽉 참
        with self.assertRaises(ValueError):
            self.repo.add_member(2, 1)  # 이미 팀이 있음
        with self.assertRaises(ValueError):
            self.repo.add_member(9, 5)  # 없는 팀
        with self.assertRaises(ValueError):
            self.repo.add_member(2, 99)  # 없는 학생
        self.assertEqual(self.repo.load_state().members[2], ())

    def test_one_pending_application_per_student(self):
        self.reset()
        first = self.repo.insert_application(1, 1, "대기", "")
        with self.assertRaises(ValueError):
            self.repo.insert_application(2, 1, "대기", "")
        self.repo.insert_application(2, 2, "승인", "빈 팀에 지원")
        self.repo.insert_application(3, 2, "승인", "다시")  # 승인은 대기가 아니므로 겹쳐도 된다
        self.repo.set_application_status(first, "거절", "사유")
        second = self.repo.insert_application(2, 1, "대기", "")  # 결과가 나온 뒤에는 다시 낼 수 있다
        self.assertGreater(second, first)

    def test_application_ids_and_times_increase_and_are_not_reused_after_reset(self):
        self.reset()
        a = self.repo.insert_application(1, 1, "대기", "")
        b = self.repo.insert_application(1, 2, "대기", "")
        self.reset()
        student = min(self.repo.load_state().students)  # 학생 id도 다시 쓰지 않으므로 처음 학생의 id가 1이 아니다
        c = self.repo.insert_application(1, student, "대기", "")
        self.assertEqual((a, b, c), (1, 2, 3))
        apps = {x.id: x for x in self.repo.load_state().applications}
        self.assertEqual(list(apps), [3])
        self.repo.insert_application(1, student + 1, "대기", "")
        times = [x.created_at for x in self.repo.load_state().applications]
        self.assertEqual(times, sorted(set(times)))

    def test_record_vote_is_unique_per_voter(self):
        self.reset()
        app_id = self.repo.insert_application(1, 1, "대기", "")
        self.repo.record_vote(app_id, 2, "찬성")
        with self.assertRaises(ValueError):
            self.repo.record_vote(app_id, 2, "거부")
        self.assertEqual(self.repo.load_state().votes, {app_id: {2: "찬성"}})

    def test_load_state_is_a_snapshot(self):
        self.reset()
        before = self.repo.load_state()
        self.repo.add_member(1, 1)
        self.repo.set_closed()
        self.assertEqual(before.members[1], ())
        self.assertFalse(before.closed)
        self.assertTrue(self.repo.load_state().closed)


if __name__ == "__main__":
    unittest.main()
