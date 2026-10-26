"""무작위 지원·투표를 해도 규칙이 깨지지 않는지, 마감 뒤 배정이 규칙을 지키는지 확인한다.

seed 0~299마다 학생 8~16명(역할과 보조 역할 무작위), 팀 3~4개로 시작해
무작위 지원·투표를 60번 하고, 매 번 뒤에 불변 조건을 검사한다.
"""
import random
import unittest
from collections import Counter

from teamapp import actions
from teamapp.rules import APPROVED, NO, PENDING, ROLES, TEAM_MAX, TEAM_MIN, YES
from teamapp.solver import explain_no_assignment, solve, validate_plan
from tests.memory_repo import MemoryRepo, fake_hash

SEEDS = range(300)
ACTIONS_PER_SEED = 60


def random_roster(rng):
    """역할이 무작위인 명단 CSV 바이트와 학생 수. 학생 id는 1부터 명단 순서이다."""
    count = rng.randint(8, 16)
    lines = ["student_no,name,main_role,sub_roles"]
    for i in range(1, count + 1):
        main = rng.choice(ROLES)
        subs = rng.sample([r for r in ROLES if r != main], rng.randint(0, 2))
        lines.append(f"2026{i:03d},학생{i},{main},{';'.join(subs)}")
    return "\n".join(lines).encode("utf-8"), count


class InvariantTests(unittest.TestCase):
    def check_state(self, state):
        """(a) 팀 4명 이하 (b) 팀 안 주 역할 중복 없음 (c) 대기 지원 학생당 1개 이하 (d) 한 학생은 한 팀에만."""
        seen = set()
        for team_id, ids in state.members.items():
            self.assertLessEqual(len(ids), TEAM_MAX, f"팀{team_id}가 4명을 넘음")
            mains = [state.students[i].main_role for i in ids]
            self.assertEqual(len(mains), len(set(mains)), f"팀{team_id}에 같은 주 역할: {mains}")
            for student_id in ids:
                self.assertNotIn(student_id, seen, f"학생 {student_id}가 두 팀에 있음")
                seen.add(student_id)
        pending = Counter(a.student_id for a in state.applications if a.status == PENDING)
        self.assertTrue(all(n <= 1 for n in pending.values()), f"대기 지원이 둘 이상: {pending}")

    def check_step(self, before, after):
        """(e) 새로 승인된 지원은 그때 팀원 전원이 찬성했거나 빈 팀이었다. 팀원은 승인된 지원자만 늘어난다."""
        old_status = {a.id: a.status for a in before.applications}
        newly_approved = [
            a for a in after.applications if a.status == APPROVED and old_status.get(a.id) != APPROVED
        ]
        for app in newly_approved:
            members_then = before.members[app.team_id]
            votes = after.votes.get(app.id, {})
            self.assertTrue(
                all(votes.get(m) == YES for m in members_then),
                f"지원 {app.id}: 팀원 {members_then}의 투표 {votes}로 승인됨",
            )
            self.assertEqual(after.team_of(app.student_id), app.team_id)
        joined = {i for ids in after.members.values() for i in ids} - {
            i for ids in before.members.values() for i in ids
        }
        self.assertEqual(joined, {a.student_id for a in newly_approved})

    def random_action(self, repo, rng, state, count):
        """무작위 지원 또는 투표 한 번. 투표는 대부분 아직 투표하지 않은 팀원이 한다."""
        pending = [a for a in state.applications if a.status == PENDING]
        if pending and rng.random() < 0.6:
            app = rng.choice(pending)
            undecided = [m for m in state.members[app.team_id] if m not in state.votes.get(app.id, {})]
            voter = rng.choice(undecided) if undecided and rng.random() < 0.9 else rng.randint(1, count)
            value = YES if rng.random() < 0.6 else NO
            return "vote", actions.vote(repo, voter, app.id, value)
        student_id = rng.randint(1, count)
        team_id = rng.choice(state.team_ids)
        return "apply", actions.apply(repo, student_id, team_id)

    def run_seed(self, seed, tally):
        rng = random.Random(seed)
        repo = MemoryRepo()
        data, count = random_roster(rng)
        started = actions.reset_roster(repo, 0, data, rng.randint(3, 4), fake_hash)
        self.assertEqual(started.kind, APPROVED, started.text)

        for _ in range(ACTIONS_PER_SEED):
            before = repo.load_state()
            kind, outcome = self.random_action(repo, rng, before, count)
            after = repo.load_state()
            tally[(kind, outcome.kind)] += 1
            self.assertTrue(outcome.text.startswith(f"{outcome.kind}: "), outcome.text)
            self.check_state(after)
            self.check_step(before, after)

        # 마감: 대기 지원은 모두 없어지고 1명뿐인 팀도 없어진다. 마감 뒤 지원은 막힌다.
        self.assertEqual(actions.close(repo, 0).kind, APPROVED)
        state = repo.load_state()
        self.assertTrue(state.closed)
        self.assertFalse([a for a in state.applications if a.status == PENDING])
        self.assertNotIn(1, [len(ids) for ids in state.members.values()])
        self.check_state(state)
        self.assertEqual(actions.apply(repo, 1, 1).text, "막힘: 모집이 마감됨")

        plan = solve(state)
        if plan is None:
            self.assertTrue(explain_no_assignment(state))
            tally["배정 없음"] += 1
            return
        if plan:
            self.assertEqual(actions.confirm(repo, 0, plan).kind, APPROVED)
            tally["확정"] += 1
        else:  # 팀이 없는 학생이 없고 지금 팀이 모두 규칙을 지키면 빈 배정안을 그대로 확정한다
            self.assertEqual(actions.confirm(repo, 0, plan).text, "승인: 배정안을 확정했습니다 (0명)")
            tally["이미 모두 팀 있음"] += 1
        final = repo.load_state()
        self.assertEqual(final.unassigned(), [])
        self.assertEqual(validate_plan(final, []), [])
        self.check_state(final)
        for ids in final.members.values():
            self.assertTrue(TEAM_MIN <= len(ids) <= TEAM_MAX)

    def test_invariants_hold(self):
        tally = Counter()
        for seed in SEEDS:
            with self.subTest(seed=seed):
                self.run_seed(seed, tally)
        # 무작위 시나리오가 중요한 경우를 실제로 지나갔는지(빈 검사가 아닌지) 확인한다.
        for key in [
            ("apply", "승인"), ("apply", "대기"), ("apply", "막힘"),
            ("vote", "승인"), ("vote", "거절"), ("vote", "대기"),
            "확정", "배정 없음",
        ]:
            self.assertGreater(tally[key], 0, f"{key}가 한 번도 나오지 않음: {dict(tally)}")


if __name__ == "__main__":
    unittest.main()
