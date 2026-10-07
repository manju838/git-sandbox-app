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


def test_every_exercise_loads_unsolved_and_has_a_solution():
    solutions = {
        "unstaged_undo": ["git restore app.py"],
        "staged_undo": ["git restore --staged app.py"],
        "reset_soft": ["git reset --soft HEAD~1"],
        "reset_hard": ["git reset --hard HEAD~1"],
        "reflog_recover": [],  # needs the lost hash, handled below
        "revert_pushed": ["git revert HEAD", "git push"],
        "merge_clean": ["git merge feature/login"],
        "merge_conflict": [
            "git merge feature/theme",
            'echo "color=blue\\nsize=small" > config.txt',
            "git add config.txt",
            'git commit -m "Merge theme"',
        ],
        "push_rejected": ["git pull", "git push"],
    }
    lab = Lab()
    for exercise in EXERCISES:
        lab.load(exercise.id)
        payload = lab.payload()
        if exercise.check is None:
            assert payload["exercise"]["solved"] is None
            continue
        assert payload["exercise"]["solved"] is False, exercise.id
        if exercise.id == "reflog_recover":
            lost = next(c.id for c in lab.sandbox.commits.values() if c.message == "Add feature B")
            commands = [f"git reset --hard {lost}"]
        else:
            commands = solutions[exercise.id]
        for command in commands:
            lab.run(command)
        assert lab.payload()["exercise"]["solved"] is True, exercise.id


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
