"""Behavioural tests for the git simulation. Run with: python -m pytest  (or python tests/test_engine.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from git_sandbox_app.engine import Sandbox, merge3_text  # noqa: E402
from git_sandbox_app.exercises import EXERCISES, Lab  # noqa: E402


def run(sb: Sandbox, *commands: str) -> str:
    out = ""
    for command in commands:
        out = sb.run(command)
    return out


def commit_file(sb: Sandbox, name: str, content: str, message: str) -> None:
    run(sb, f'echo "{content}" > {name}', "git add .", f'git commit -m "{message}"')


def test_add_commit_flow_moves_changes_through_areas():
    sb = Sandbox()
    run(sb, 'echo "hi" > a.txt')
    assert {f["name"]: f["status"] for f in sb.snapshot()["working"]}["a.txt"] == "untracked"
    run(sb, "git add a.txt")
    assert sb.index["a.txt"] == "hi" and "a.txt" not in sb.head_tree
    run(sb, 'git commit -m "add a"')
    assert sb.head_tree["a.txt"] == "hi"
    assert sb.head_commit.message == "add a"


def test_reset_modes():
    # mode -> (file still staged?, file still in working directory?)
    expected = {"--soft": (True, True), "--mixed": (False, True), "--hard": (False, False)}
    for mode, (in_index, in_wd) in expected.items():
        sb = Sandbox()
        commit_file(sb, "a.txt", "one", "c1")
        run(sb, f"git reset {mode} HEAD~1")
        assert sb.head_commit.message == "Initial commit"
        assert ("a.txt" in sb.index) is in_index, mode
        assert ("a.txt" in sb.wd) is in_wd, mode


def test_hard_reset_keeps_untracked_files():
    sb = Sandbox()
    commit_file(sb, "a.txt", "one", "c1")
    run(sb, 'echo "scratch" > notes.txt', "git reset --hard HEAD~1")
    assert "a.txt" not in sb.wd and sb.wd["notes.txt"] == "scratch"


def test_revert_adds_commit_and_undoes_changes():
    sb = Sandbox()
    commit_file(sb, "a.txt", "one", "c1")
    bad = sb.head_id
    run(sb, "git revert HEAD")
    assert sb.head_commit.message == 'Revert "c1"'
    assert "a.txt" not in sb.head_tree and bad in sb.ancestors(sb.head_id)


def test_revert_conflict_and_abort():
    sb = Sandbox()
    commit_file(sb, "a.txt", "one", "c1")
    first = sb.head_id
    commit_file(sb, "a.txt", "two", "c2")
    out = run(sb, f"git revert {first}")
    assert "CONFLICT" in out and sb.conflicts == {"a.txt"}
    run(sb, "git revert --abort")
    assert not sb.conflicts and sb.wd["a.txt"] == "two"


def test_fast_forward_and_three_way_merge():
    sb = Sandbox()
    run(sb, "git switch -c feature")
    commit_file(sb, "f.txt", "f", "feature work")
    run(sb, "git switch main")
    assert "Fast-forward" in run(sb, "git merge feature")
    assert sb.head_commit.message == "feature work"

    run(sb, "git switch -c other")
    commit_file(sb, "o.txt", "o", "other work")
    run(sb, "git switch main")
    commit_file(sb, "m.txt", "m", "main work")
    run(sb, "git merge other")
    assert len(sb.head_commit.parents) == 2
    assert {"f.txt", "o.txt", "m.txt"} <= set(sb.head_tree)


def test_merge_conflict_resolution():
    sb = Sandbox()
    commit_file(sb, "c.txt", "base", "base")
    run(sb, "git switch -c feature")
    commit_file(sb, "c.txt", "feature", "feature")
    run(sb, "git switch main")
    commit_file(sb, "c.txt", "main", "main")
    out = run(sb, "git merge feature")
    assert "CONFLICT" in out
    assert sb.wd["c.txt"].startswith("<<<<<<< HEAD\nmain\n=======\nfeature\n>>>>>>> feature")
    assert "nothing" not in run(sb, "git status") and "Unmerged" in run(sb, "git status")
    assert "unmerged" in run(sb, 'git commit -m "x"')
    run(sb, 'echo "resolved" > c.txt', "git add c.txt", 'git commit -m "merged"')
    assert len(sb.head_commit.parents) == 2 and sb.pending is None and not sb.conflicts


def test_merge_abort():
    sb = Sandbox()
    commit_file(sb, "c.txt", "base", "base")
    run(sb, "git switch -c feature")
    commit_file(sb, "c.txt", "feature", "feature")
    run(sb, "git switch main")
    commit_file(sb, "c.txt", "main", "main")
    run(sb, "git merge feature", "git merge --abort")
    assert sb.wd["c.txt"] == "main" and not sb.conflicts and sb.pending is None


def test_line_level_merge_is_clean_when_changes_do_not_touch():
    text, conflict = merge3_text("a\nb\nc\nd\ne", "A\nb\nc\nd\ne", "a\nb\nc\nd\nE", "x")
    assert not conflict and text == "A\nb\nc\nd\nE"


def test_push_rejected_then_pull_and_push():
    sb = Sandbox()
    commit_file(sb, "mine.txt", "m", "mine")
    run(sb, 'teammate notes.txt "n"')
    assert "rejected" in run(sb, "git push")
    run(sb, "git pull")
    assert "Successfully" not in run(sb, "git push") and sb.remote_branches["main"] == sb.head_id


def test_force_push_overwrites_remote():
    sb = Sandbox()
    commit_file(sb, "a.txt", "a", "c1")
    run(sb, "git push", "git reset --hard HEAD~1")
    commit_file(sb, "b.txt", "b", "c2")
    assert "rejected" in run(sb, "git push")
    assert "forced" in run(sb, "git push --force")


def test_checkout_blocks_on_conflicting_local_changes_but_carries_others():
    sb = Sandbox()
    run(sb, "git branch other")
    run(sb, 'echo "dirty" >> README.md')
    run(sb, "git switch other")
    assert sb.head_branch == "other" and sb.wd["README.md"].endswith("dirty")

    commit_file(sb, "README.md", "changed on other", "other change")
    run(sb, "git switch main")
    run(sb, 'echo "dirty" > README.md')
    assert "overwritten" in run(sb, "git switch other")


def test_reflog_recovery():
    sb = Sandbox()
    commit_file(sb, "a.txt", "a", "c1")
    lost = sb.head_id
    run(sb, "git reset --hard HEAD~1")
    assert lost in run(sb, "git reflog")
    run(sb, f"git reset --hard {lost}")
    assert sb.head_commit.message == "c1"


def _hash_of(lab: Lab, message: str) -> str:
    return next(c.id for c in lab.sandbox.commits.values() if c.message == message)


# One known-good solution per scenario. A callable receives the Lab to look up commit hashes.
SOLUTIONS = {
    "unstaged_undo": ["git restore app.py"],
    "staged_undo": ["git restore --staged app.py"],
    "discard_one_of_two": ["git restore --staged --worktree app.py"],
    "amend_forgotten_file": ["git add helper.py", "git commit --amend --no-edit"],
    "reset_soft": ["git reset --soft HEAD~1"],
    "reset_mixed": ["git reset HEAD~1"],
    "reset_hard": ["git reset --hard HEAD~1"],
    "trio_soft": ["git reset --soft HEAD~1"],
    "trio_mixed": ["git reset --mixed HEAD~1"],
    "trio_hard": ["git reset --hard HEAD~1"],
    "reset_to_hash": lambda lab: [f"git reset --hard {_hash_of(lab, 'Step 1')}"],
    "reset_path": ["git reset HEAD~1 -- config.txt"],
    "reflog_recover": lambda lab: [f"git reset --hard {_hash_of(lab, 'Add feature B')}"],
    "squash_soft": ["git reset --soft main", 'git commit -m "Add search"'],
    "revert_pushed": ["git revert HEAD", "git push"],
    "revert_middle": ["git revert HEAD~1", "git push"],
    "revert_range": ["git revert -n HEAD HEAD~1", 'git commit -m "Revert bad work"', "git push"],
    "revert_conflict": [
        "git revert HEAD~1",
        'echo "mode=staging\\ndebug=false" > settings.txt',
        "git add settings.txt",
        "git commit",
    ],
    "revert_merge": ["git revert -m 1 HEAD", "git push"],
    "merge_ff": ["git merge feature/nav"],
    "merge_no_ff": ["git merge --no-ff feature/nav"],
    "merge_clean": ["git merge feature/login"],
    "merge_conflict": [
        "git merge feature/theme",
        'echo "color=blue\\nsize=small" > config.txt',
        "git add config.txt",
        'git commit -m "Merge theme"',
    ],
    "merge_abort": ["git merge --abort"],
    "merge_two_conflicts": [
        "git merge feature/x",
        'echo "A=main" > a.txt',
        'echo "B=feature" > b.txt',
        "git add .",
        'git commit -m "Merge feature/x"',
    ],
    "merge_dirty_tree": ['git commit -am "Update README"', "git merge feature/x"],
    "merge_undo_local": ["git reset --hard HEAD~1"],
    "branch_into_feature": ["git merge main"],
    "branch_ff_from_feature": ["git merge main"],
    "branch_reset_on_feature": ["git reset --hard HEAD~1"],
    "branch_reset_on_main_shared": ["git reset --hard HEAD~1"],
    "branch_revert_on_feature": ["git revert HEAD", "git switch main", "git merge feature/ui"],
    "branch_revert_on_main": ["git revert HEAD^2", "git push"],
    "branch_move_edit": ["git switch feature/search", 'git commit -m "Add search"'],
    "push_rejected": ["git pull", "git push"],
    "pull_conflict": [
        "git pull",
        'echo "mine\\ntheirs" > notes.txt',
        "git add notes.txt",
        "git commit",
        "git push",
    ],
    "publish_feature": ["git push -u origin feature/cart"],
    "fetch_merge_feature": ["git fetch", "git merge origin/main"],
    "force_push_private": ["git reset --hard HEAD~1", "git push --force"],
}


def _solve(lab: Lab, exercise_id: str) -> None:
    lab.load(exercise_id)
    commands = SOLUTIONS[exercise_id]
    for command in commands(lab) if callable(commands) else commands:
        lab.run(command)


def test_every_exercise_has_a_solution_and_starts_unsolved():
    assert {e.id for e in EXERCISES if e.check} == set(SOLUTIONS)
    lab = Lab()
    for exercise in EXERCISES:
        lab.load(exercise.id)
        solved = lab.payload()["exercise"]["solved"]
        if exercise.check is None:
            assert solved is None
        else:
            assert solved is False, f"{exercise.id} is already solved when loaded"
            _solve(lab, exercise.id)
            assert lab.payload()["exercise"]["solved"] is True, f"{exercise.id} not solved by its solution"


def test_exercise_setups_run_without_errors():
    import re

    bad = re.compile(r"^(error|fatal|\S+: command not found)", re.IGNORECASE)
    for exercise in EXERCISES:
        sb = Sandbox()
        for command in exercise.setup:
            out = sb.run(command)
            assert not bad.match(out), f"{exercise.id}: '{command}' -> {out}"


def test_reset_trio_goals_are_mutually_exclusive():
    """Each --soft/--mixed/--hard result must satisfy only its own scenario."""
    modes = {"trio_soft": "--soft", "trio_mixed": "--mixed", "trio_hard": "--hard"}
    for solved_id, flag in modes.items():
        lab = Lab()
        lab.load(solved_id)
        lab.run(f"git reset {flag} HEAD~1")
        for other_id in modes:
            lab.exercise = next(e for e in EXERCISES if e.id == other_id)
            assert lab.payload()["exercise"]["solved"] is (other_id == solved_id), (flag, other_id)


def test_merge_direction_changes_which_branch_moves():
    lab = Lab()
    lab.load("branch_into_feature")  # on feature/profile
    main_before = lab.sandbox.branches["main"]
    lab.run("git merge main")
    assert lab.sandbox.branches["main"] == main_before

    lab.load("merge_clean")  # on main
    feature_before = lab.sandbox.branches["feature/login"]
    lab.run("git merge feature/login")
    assert lab.sandbox.branches["feature/login"] == feature_before
    assert lab.sandbox.branches["main"] == lab.sandbox.head_id


def test_reset_on_main_leaves_shared_commit_reachable_from_feature():
    lab = Lab()
    lab.load("branch_reset_on_main_shared")
    oops = _hash_of(lab, "Oops: debug code")
    lab.run("git reset --hard HEAD~1")
    assert oops not in lab.sandbox.ancestors(lab.sandbox.branches["main"])
    assert oops in lab.sandbox.ancestors(lab.sandbox.branches["feature/ui"])


def test_revert_multiple_commits_in_one_command_makes_one_commit_each():
    sb = Sandbox()
    commit_file(sb, "a.txt", "a", "A")
    commit_file(sb, "b.txt", "b", "B")
    before = sb.head_id
    out = run(sb, "git revert HEAD HEAD~1")
    assert "error" not in out
    assert "a.txt" not in sb.head_tree and "b.txt" not in sb.head_tree
    assert sb.commits[sb.commits[sb.head_id].parents[0]].parents == [before]


def test_push_head_publishes_current_branch():
    sb = Sandbox()
    run(sb, "git switch -c feature/x")
    commit_file(sb, "x.txt", "x", "x")
    run(sb, "git push -u origin HEAD")
    assert sb.remote_branches["feature/x"] == sb.head_id
    assert sb.upstream["feature/x"] == "origin/feature/x"


def test_snapshot_is_json_serialisable():
    import json

    sb = Sandbox()
    commit_file(sb, "a.txt", "a", "c1")
    run(sb, "git switch -c feature")
    json.dumps(sb.snapshot())


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("ok  ", name)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print("FAIL", name, "->", type(exc).__name__, exc)
    sys.exit(1 if failures else 0)
