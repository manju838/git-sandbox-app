"""Guided practice scenarios and the session object that owns the sandbox.

The curriculum is ordered so that finishing every scenario in a group builds on the last:

  1. Moving changes between the working directory, staging area and repository
  2. reset  - --soft / --mixed / --hard, on all three layers, plus paths and the reflog
  3. revert - last commit, middle commit, ranges, conflicts, merge commits
  4. merge  - fast-forward, --no-ff, 3-way, conflicts, abort, undoing a merge
  5. The same operation from main vs from a feature branch (watch which branch moves)
  6. The remote - rejected pushes, pull conflicts, publishing, force-pushing safely

Each scenario is *state based*: the goal describes the end result, never the command, and the
check inspects the sandbox, so any correct route to that state counts as solved.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable

from .engine import CONFLICT_RE, Commit, Sandbox


# --------------------------------------------------------------------------- #
# Helpers for setups and checks
# --------------------------------------------------------------------------- #
def C(path: str, content: str, message: str) -> list[str]:
    """Setup commands: write ``content`` to ``path`` and commit it."""
    return [f'echo "{content}" > {path}', "git add .", f'git commit -m "{message}"']


def A(path: str, line: str, message: str) -> list[str]:
    """Setup commands: append ``line`` to ``path`` and commit it."""
    return [f'echo "{line}" >> {path}', "git add .", f'git commit -m "{message}"']


def find(sb: Sandbox, message: str) -> Commit | None:
    """Newest commit whose message equals ``message``."""
    matches = [c for c in sb.commits.values() if c.message == message]
    return max(matches, key=lambda c: c.seq) if matches else None


def tip(sb: Sandbox, branch: str) -> Commit:
    return sb.commits[sb.branches[branch]]


def in_history(sb: Sandbox, message: str, start: str | None = None) -> bool:
    commit = find(sb, message)
    return bool(commit and commit.id in sb.ancestors(start or sb.head_id))


def clean(sb: Sandbox) -> bool:
    return sb.index == sb.head_tree and sb.wd == sb.head_tree and not sb.pending and not sb.conflicts


def synced(sb: Sandbox, branch: str) -> bool:
    return sb.remote_branches.get(branch) == sb.branches.get(branch)


def no_markers(text: str) -> bool:
    return not CONFLICT_RE.search(text)


@dataclass
class Exercise:
    id: str
    group: str
    title: str
    area: str
    description: str
    hint: str
    takeaway: str = ""
    setup: list[str] = field(default_factory=list)
    check: Callable[[Sandbox], bool] | None = None


G_BASICS = "1. Moving changes between areas"
G_RESET = "2. Reset: soft, mixed, hard"
G_REVERT = "3. Revert"
G_MERGE = "4. Merge"
G_BRANCH = "5. Same operation, different branch"
G_REMOTE = "6. Working with the remote"

APP = [*C("app.py", "print(1)", "Add app.py"), "git push"]

# --------------------------------------------------------------------------- #
# Shared scenario setups
# --------------------------------------------------------------------------- #
# Three layers of work at once: an unpushed commit, a staged new file, an unstaged edit.
TRIO = [
    *APP,
    *C("app.py", "print(2)", "Add print(2)"),
    'echo "notes v1" > notes.txt',
    "git add notes.txt",
    'echo "unstaged edit" >> README.md',
]

NAV = [
    "git switch -c feature/nav",
    *C("nav.py", "nav", "Add nav"),
    *A("nav.py", "links", "Add nav links"),
    "git switch main",
]

THEME = [
    *C("config.txt", "color=red\\nsize=small", "Add config"),
    "git switch -c feature/theme",
    *C("config.txt", "color=blue\\nsize=small", "Blue theme"),
    "git switch main",
    *C("config.txt", "color=green\\nsize=small", "Green theme"),
]

UI_BRANCH = ["git switch -c feature/ui", *C("ui.py", "ui ok", "Add ui")]


# --------------------------------------------------------------------------- #
# Checks (each returns True only when the learner reached the goal state)
# --------------------------------------------------------------------------- #
def _unstaged_undo(sb):
    return sb.head_commit.message == "Add app.py" and sb.wd.get("app.py") == "print(1)" and clean(sb)


def _staged_undo(sb):
    return (
        sb.head_commit.message == "Add app.py"
        and sb.index == sb.head_tree
        and sb.wd.get("app.py") == "print(2)  # work in progress"
    )


def _discard_one(sb):
    return (
        sb.head_commit.message == "Add util.py"
        and sb.index == sb.head_tree
        and sb.wd.get("app.py") == "print(1)"
        and sb.wd.get("util.py") == "def f(): return 1"
    )


def _amend(sb):
    parent = sb.commits[sb.head_commit.parents[0]]
    return (
        sb.head_commit.message == "Add main.py"
        and "helper.py" in sb.head_tree
        and parent.message == "Initial commit"
        and clean(sb)
    )


def _reset_soft(sb):
    return (
        sb.head_commit.message == "Add app.py"
        and sb.index.get("app.py") == "print(2)"
        and sb.wd.get("app.py") == "print(2)"
    )


def _reset_mixed(sb):
    return (
        sb.head_commit.message == "Add app.py"
        and sb.index == sb.head_tree
        and sb.wd.get("app.py") == "print(2)"
    )


def _reset_hard(sb):
    bad = find(sb, "Oops: debug code")
    return (
        sb.head_commit.message == "Add app.py"
        and clean(sb)
        and bad is not None
        and bad.id not in sb.ancestors(sb.head_id)
    )


def _trio_soft(sb):
    expected = {**sb.head_tree, "app.py": "print(2)", "notes.txt": "notes v1"}
    return (
        sb.head_commit.message == "Add app.py"
        and sb.index == expected
        and sb.wd.get("README.md", "").endswith("unstaged edit")
    )


def _trio_mixed(sb):
    return (
        sb.head_commit.message == "Add app.py"
        and sb.index == sb.head_tree
        and sb.wd.get("app.py") == "print(2)"
        and sb.wd.get("notes.txt") == "notes v1"
        and sb.wd.get("README.md", "").endswith("unstaged edit")
    )


def _trio_hard(sb):
    return sb.head_commit.message == "Add app.py" and clean(sb) and sb.wd.get("app.py") == "print(1)"


def _reset_to_hash(sb):
    return sb.head_commit.message == "Step 1" and clean(sb)


def _reset_path(sb):
    return (
        sb.head_commit.message == "Change color"
        and sb.index.get("config.txt") == "color=red"
        and sb.wd.get("config.txt") == "color=green"
    )


def _recover(sb):
    return sb.head_commit.message == "Add feature B" and clean(sb)


def _squash(sb):
    return (
        sb.head_branch == "feature/search"
        and sb.head_commit.message == "Add search"
        and sb.head_commit.parents == [sb.branches["main"]]
        and sb.head_tree.get("search.py") == "a\nb\nc"
        and clean(sb)
    )


def _revert_pushed(sb):
    good, bad = find(sb, "Add feature A"), find(sb, "Add buggy feature B")
    if not (good and bad):
        return False
    return (
        sb.head_tree == good.tree
        and bad.id in sb.ancestors(sb.head_id)  # history was not rewritten
        and synced(sb, "main")
        and sb.head_id not in (good.id, bad.id)
        and clean(sb)
    )


def _revert_middle(sb):
    return (
        "b.txt" not in sb.head_tree
        and "a.txt" in sb.head_tree
        and "c.txt" in sb.head_tree
        and in_history(sb, "Add b (bad)")
        and in_history(sb, "Add c")
        and synced(sb, "main")
        and clean(sb)
    )


def _revert_range(sb):
    bad2 = find(sb, "Bad 2")
    return bool(
        bad2
        and "x.txt" not in sb.head_tree
        and "y.txt" not in sb.head_tree
        and "a.txt" in sb.head_tree
        and sb.head_commit.parents == [bad2.id]  # exactly one new commit on top
        and synced(sb, "main")
        and clean(sb)
    )


def _revert_conflict(sb):
    return (
        sb.head_commit.message.startswith("Revert")
        and sb.head_tree.get("settings.txt") == "mode=staging\ndebug=false"
        and in_history(sb, "Enable debug")
        and clean(sb)
    )


def _revert_merge(sb):
    return (
        "pay.py" not in sb.head_tree
        and "docs.md" in sb.head_tree
        and in_history(sb, "Merge branch 'feature/pay'")
        and len(sb.head_commit.parents) == 1
        and synced(sb, "main")
        and clean(sb)
    )


def _merge_ff(sb):
    return (
        sb.head_branch == "main"
        and sb.head_id == sb.branches["feature/nav"]
        and len(sb.head_commit.parents) == 1
        and clean(sb)
    )


def _merge_no_ff(sb):
    return (
        sb.head_branch == "main"
        and len(sb.head_commit.parents) == 2
        and sb.commits[sb.head_commit.parents[0]].message == "Initial commit"
        and "nav.py" in sb.head_tree
        and clean(sb)
    )


def _merge_clean(sb):
    return (
        sb.head_branch == "main"
        and len(sb.head_commit.parents) == 2
        and "login.py" in sb.head_tree
        and "docs.md" in sb.head_tree
        and clean(sb)
    )


def _merge_conflict(sb):
    config = sb.head_tree.get("config.txt", "")
    return (
        sb.head_branch == "main"
        and len(sb.head_commit.parents) == 2
        and no_markers(config)
        and sum(line.startswith("color=") for line in config.split("\n")) == 1
        and clean(sb)
    )


def _merge_abort(sb):
    return sb.head_branch == "main" and sb.head_commit.message == "Green theme" and clean(sb)


def _merge_two(sb):
    return (
        sb.head_branch == "main"
        and len(sb.head_commit.parents) == 2
        and sb.head_tree.get("a.txt") == "A=main"
        and sb.head_tree.get("b.txt") == "B=feature"
        and clean(sb)
    )


def _merge_dirty(sb):
    return (
        sb.head_branch == "main"
        and len(sb.head_commit.parents) == 2
        and "x.txt" in sb.head_tree
        and sb.head_tree.get("README.md", "").endswith("draft")
        and clean(sb)
    )


def _merge_undo_local(sb):
    return (
        sb.head_branch == "main"
        and sb.head_commit.message == "Add docs"
        and tip(sb, "feature/x").message == "Add x"
        and clean(sb)
    )


def _into_feature(sb):
    return (
        sb.head_branch == "feature/profile"
        and len(sb.head_commit.parents) == 2
        and "docs.md" in sb.head_tree
        and "profile.py" not in tip(sb, "main").tree
        and clean(sb)
    )


def _ff_from_feature(sb):
    return sb.head_branch == "feature/old" and sb.head_id == sb.branches["main"] and clean(sb)


def _reset_on_feature(sb):
    return (
        sb.head_branch == "feature/ui"
        and sb.head_commit.message == "Add ui"
        and tip(sb, "main").message == "Add app.py"
        and clean(sb)
    )


def _reset_on_main_shared(sb):
    return (
        sb.head_branch == "main"
        and sb.head_commit.message == "Add app.py"
        and tip(sb, "feature/ui").message == "Add ui"
        and in_history(sb, "Oops: debug code", sb.branches["feature/ui"])
        and clean(sb)
    )


def _revert_on_feature(sb):
    return (
        sb.head_branch == "main"
        and "ui.py" in sb.head_tree
        and "sidebar.py" not in sb.head_tree
        and in_history(sb, "Add broken sidebar")
        and clean(sb)
    )


def _revert_on_main(sb):
    feature = tip(sb, "feature/ui")
    return (
        sb.head_branch == "main"
        and "ui.py" in sb.head_tree
        and "sidebar.py" not in sb.head_tree
        and feature.message == "Add broken sidebar"
        and "sidebar.py" in feature.tree
        and synced(sb, "main")
        and clean(sb)
    )


def _move_edit(sb):
    main = tip(sb, "main")
    feature = tip(sb, "feature/search")
    return (
        sb.head_branch == "feature/search"
        and "search.py" in feature.tree
        and main.message == "Initial commit"
        and "search.py" not in main.tree
        and clean(sb)
    )


def _push_rejected(sb):
    mate, mine = find(sb, "Teammate updates notes.txt"), find(sb, "Add mine.txt")
    ids = sb.ancestors(sb.head_id)
    return bool(mate and mine and mate.id in ids and mine.id in ids and synced(sb, "main") and clean(sb))


def _pull_conflict(sb):
    notes = sb.head_tree.get("notes.txt", "")
    return (
        synced(sb, "main")
        and len(sb.head_commit.parents) == 2
        and no_markers(notes)
        and "mine" in notes
        and "theirs" in notes
        and clean(sb)
    )


def _publish(sb):
    return (
        sb.remote_branches.get("feature/cart") == sb.branches["feature/cart"]
        and sb.upstream.get("feature/cart") == "origin/feature/cart"
        and sb.commits[sb.remote_branches["main"]].message == "Initial commit"
    )


def _fetch_merge(sb):
    return (
        sb.head_branch == "feature/cart"
        and in_history(sb, "Teammate updates notes.txt")
        and tip(sb, "main").message == "Initial commit"  # local main was left alone
        and clean(sb)
    )


def _force_private(sb):
    oops = find(sb, "Oops: API key")
    return bool(
        oops
        and sb.head_branch == "feature/draft"
        and sb.head_commit.message == "Add draft"
        and synced(sb, "feature/draft")
        and oops.id not in sb._remote_reachable()
        and clean(sb)
    )


# --------------------------------------------------------------------------- #
# The curriculum
# --------------------------------------------------------------------------- #
EXERCISES: list[Exercise] = [
    Exercise(
        id="sandbox", group="Free play", title="Free play", area="everything",
        description="No goal - experiment freely. Edit files with echo/touch, then try branches, merges, resets and reverts.",
        hint="Try: echo \"hi\" > a.txt, git add ., git commit -m \"x\", git push. Type 'help' for everything.",
    ),

    # ---------------------------------------------------------------- 1. basics
    Exercise(
        id="unstaged_undo", group=G_BASICS, title="Discard a working-directory edit", area="working directory",
        description="You edited app.py and broke it. Nothing is staged. Put app.py back to its committed content (print(1)) without creating a commit.",
        hint="git status shows the command: git restore app.py",
        takeaway="git restore <file> copies the staged (or committed) version over the working directory. It only touches the working directory - and the edit is gone for good.",
        setup=[*APP, 'echo "print(BROKEN" > app.py'], check=_unstaged_undo,
    ),
    Exercise(
        id="staged_undo", group=G_BASICS, title="Unstage a file (keep the edit)", area="staging area",
        description="You staged app.py by mistake. Take it out of the staging area but keep your edit (print(2)  # work in progress) in the working directory.",
        hint="git restore --staged app.py   (or: git reset app.py)",
        takeaway="git restore --staged <file> (same as git reset <file>) copies HEAD's version into the staging area only. Your working directory edit is untouched.",
        setup=[*APP, 'echo "print(2)  # work in progress" > app.py', "git add app.py"], check=_staged_undo,
    ),
    Exercise(
        id="discard_one_of_two", group=G_BASICS, title="Discard staged + unstaged edits of one file", area="working directory + staging",
        description="app.py has a staged edit AND a different unstaged edit - both are junk. util.py has a good unstaged edit (return 1). Reset app.py completely (staging area and working directory) and leave util.py exactly as it is.",
        hint="git restore --staged --worktree app.py  (never use --hard here: it would wipe util.py too)",
        takeaway="A file can differ in two places at once: HEAD vs staging, and staging vs working directory. --staged and --worktree choose which of the two copies get overwritten.",
        setup=[
            *APP, *C("util.py", "def f(): pass", "Add util.py"), "git push",
            'echo "print(BROKEN" > app.py', "git add app.py", 'echo "print(WORSE" > app.py',
            'echo "def f(): return 1" > util.py',
        ],
        check=_discard_one,
    ),
    Exercise(
        id="amend_forgotten_file", group=G_BASICS, title="Add a forgotten file to the last commit", area="local repository",
        description='You committed "Add main.py" but forgot helper.py (untracked). Fix the commit so it contains helper.py - there must still be only ONE commit on top of "Initial commit". Nothing is pushed.',
        hint="git add helper.py   then   git commit --amend --no-edit",
        takeaway="--amend replaces the tip commit with a new one (new hash). Same result as git reset --soft HEAD~1 + commit. Only do it on commits nobody else has.",
        setup=[*C("main.py", "v1", "Add main.py"), 'echo "helper" > helper.py'], check=_amend,
    ),

    # ----------------------------------------------------------------- 2. reset
    Exercise(
        id="reset_soft", group=G_RESET, title="reset --soft: redo a commit", area="local repository",
        description='The last local commit ("Add print(2)") is too small. Remove the commit but keep its changes STAGED so you can recommit them differently. Nothing is pushed.',
        hint="git reset --soft HEAD~1  - moves the branch only; staging area and working directory stay.",
        takeaway="--soft moves the branch pointer and nothing else: the undone commit's changes are left staged, ready to commit again.",
        setup=[*APP, *C("app.py", "print(2)", "Add print(2)")], check=_reset_soft,
    ),
    Exercise(
        id="reset_mixed", group=G_RESET, title="reset --mixed: unstage a whole commit", area="local repository",
        description='Remove the last local commit ("Add print(2)") but keep the change as a plain EDIT in the working directory: not committed and not staged.',
        hint="git reset HEAD~1   (--mixed is the default mode)",
        takeaway="--mixed (the default) moves the branch AND resets the staging area to match, but leaves the working directory alone. The changes become unstaged edits.",
        setup=[*APP, *C("app.py", "print(2)", "Add print(2)")], check=_reset_mixed,
    ),
    Exercise(
        id="reset_hard", group=G_RESET, title="reset --hard: throw a commit away", area="local repository",
        description='The unpushed commit "Oops: debug code" must vanish completely - from history, the staging area AND the working directory.',
        hint="git reset --hard HEAD~1   (careful: it also destroys uncommitted work)",
        takeaway="--hard moves the branch and overwrites BOTH the staging area and the working directory. It is the only mode that can destroy uncommitted work.",
        setup=[*APP, 'echo "print(1); debug()" > app.py', "git add .", 'git commit -m "Oops: debug code"'], check=_reset_hard,
    ),
    Exercise(
        id="trio_soft", group=G_RESET, title="Same start A: keep everything staged", area="all three layers",
        description='State: unpushed commit "Add print(2)", a STAGED new file notes.txt, and an UNSTAGED edit to README.md. Goal: drop the commit so that its changes AND notes.txt are staged together, while the README.md edit stays unstaged.',
        hint="Which reset mode leaves the staging area and working directory untouched? Check the result with git status.",
        takeaway="With --soft the three layers keep their content: the commit's changes join what was already staged, and unstaged edits stay unstaged.",
        setup=TRIO, check=_trio_soft,
    ),
    Exercise(
        id="trio_mixed", group=G_RESET, title="Same start B: unstage everything", area="all three layers",
        description='Same starting state as "Same start A". Goal: drop the commit so that NOTHING is staged any more, yet every edit survives in the working directory (the old app.py change, notes.txt and the README.md edit).',
        hint="git reset HEAD~1  - then run git status and see notes.txt turn untracked.",
        takeaway="--mixed empties the staging area: the commit's changes and the previously staged notes.txt all become working-directory changes (notes.txt is untracked again).",
        setup=TRIO, check=_trio_mixed,
    ),
    Exercise(
        id="trio_hard", group=G_RESET, title="Same start C: wipe it all", area="all three layers",
        description='Same starting state again. Goal: go back to "Add app.py" so the working directory, staging area and HEAD all match it exactly - the commit, the staged notes.txt and the README.md edit are all gone.',
        hint="git reset --hard HEAD~1",
        takeaway="Soft keeps staged + unstaged, mixed keeps only the working directory, hard keeps nothing. Remember it as: soft = HEAD, mixed = HEAD + index, hard = HEAD + index + working directory are reset.",
        setup=TRIO, check=_trio_hard,
    ),
    Exercise(
        id="reset_to_hash", group=G_RESET, title="Rewind several commits", area="local repository",
        description='main has three unpushed commits "Step 1", "Step 2", "Step 3". Rewind main so "Step 1" is the tip and the working directory matches it. Find "Step 1" with git log.',
        hint="git log --oneline, then git reset --hard <hash>   (or HEAD~2)",
        takeaway="reset accepts any commit: a hash, HEAD~N, a branch. The commits you skip are not deleted - they are just unreachable, which the reflog still remembers.",
        setup=[*APP, *C("steps.txt", "1", "Step 1"), *A("steps.txt", "2", "Step 2"), *A("steps.txt", "3", "Step 3")],
        check=_reset_to_hash,
    ),
    Exercise(
        id="reset_path", group=G_RESET, title="reset <commit> -- <file>: stage an old version", area="staging area",
        description='HEAD is "Change color". config.txt used to be color=red one commit ago, and you now have a messy edit (color=green) in your working directory. Stage the OLD version (color=red) of config.txt without moving HEAD and without touching your working-directory edit.',
        hint="git reset HEAD~1 -- config.txt   (or: git restore --source=HEAD~1 --staged config.txt)",
        takeaway="With a path, reset never moves HEAD - it only copies that file from the given commit into the staging area. That is also why git reset <file> unstages.",
        setup=[*APP, *C("config.txt", "color=red", "Add config"), *C("config.txt", "color=blue", "Change color"), 'echo "color=green" > config.txt'],
        check=_reset_path,
    ),
    Exercise(
        id="reflog_recover", group=G_RESET, title="Recover from a bad reset", area="local repository",
        description='You ran reset --hard and lost the commit "Add feature B". Get it back.',
        hint="git reflog lists where HEAD has been. Then: git reset --hard <hash>",
        takeaway="reset --hard is rarely final: the reflog keeps every position HEAD had, so a lost commit is recoverable until it is garbage collected.",
        setup=[
            *C("feature.txt", "A", "Add feature A"), *C("feature.txt", "A+B", "Add feature B"),
            "git reset --hard HEAD~1",
        ],
        check=_recover,
    ),
    Exercise(
        id="squash_soft", group=G_RESET, title="Squash 3 WIP commits with reset --soft", area="local repository",
        description='On feature/search you made three commits ("WIP 1/2/3"). Turn them into ONE commit "Add search" sitting directly on top of main. main must not move.',
        hint="git reset --soft main   (or HEAD~3), then git commit -m \"Add search\"",
        takeaway="Squashing = reset --soft to the fork point + one new commit. All the work stays staged during the reset, so nothing is lost.",
        setup=[
            "git switch -c feature/search", *C("search.py", "a", "WIP 1"),
            *A("search.py", "b", "WIP 2"), *A("search.py", "c", "WIP 3"),
        ],
        check=_squash,
    ),

    # ---------------------------------------------------------------- 3. revert
    Exercise(
        id="revert_pushed", group=G_REVERT, title="Undo the last pushed commit", area="remote repository",
        description='"Add buggy feature B" is already on origin/main, so history must not be rewritten. Undo its changes with a new commit and push the result.',
        hint="git revert HEAD   then   git push",
        takeaway="revert adds a NEW commit that is the inverse of an old one, so it is safe on shared history. reset would rewrite history and force everyone else to repair their clones.",
        setup=[
            *C("feature.txt", "A", "Add feature A"), *C("feature.txt", "A + bug", "Add buggy feature B"), "git push",
        ],
        check=_revert_pushed,
    ),
    Exercise(
        id="revert_middle", group=G_REVERT, title="Revert a commit from the middle", area="remote repository",
        description='History (all pushed): "Add a", "Add b (bad)", "Add c". Undo ONLY "Add b (bad)" - a.txt and c.txt must stay - and push.',
        hint="git log --oneline  - the bad commit is HEAD~1. git revert HEAD~1, then git push.",
        takeaway="revert can target any commit, not just the latest. Later commits stay; only that commit's changes are inverted.",
        setup=[*C("a.txt", "A", "Add a"), *C("b.txt", "B", "Add b (bad)"), *C("c.txt", "C", "Add c"), "git push"],
        check=_revert_middle,
    ),
    Exercise(
        id="revert_range", group=G_REVERT, title="Revert two commits as one", area="staging area + remote",
        description='"Bad 1" and "Bad 2" are pushed and both wrong. Undo both with exactly ONE new commit on top of "Bad 2" (not two revert commits), then push.',
        hint="git revert -n HEAD HEAD~1  (-n stages the result without committing), then git commit -m \"...\" and git push.",
        takeaway="revert --no-commit (-n) leaves the inverse changes staged. That lets you combine several reverts into a single commit, or tweak the result before committing.",
        setup=[*C("a.txt", "A", "Add a"), *C("x.txt", "X", "Bad 1"), *C("y.txt", "Y", "Bad 2"), "git push"],
        check=_revert_range,
    ),
    Exercise(
        id="revert_conflict", group=G_REVERT, title="Resolve a conflict while reverting", area="working directory + staging + repo",
        description='settings.txt: "Enable debug" (HEAD~1) turned on debug, then "Switch to staging" changed both lines again. Revert "Enable debug" and resolve the conflict so the file ends as mode=staging / debug=false, then finish the revert.',
        hint="git revert HEAD~1 -> edit: echo \"mode=staging\\ndebug=false\" > settings.txt -> git add settings.txt -> git commit  (or git revert --abort)",
        takeaway="Reverting an old commit can conflict with later edits to the same lines. You resolve it exactly like a merge conflict: edit, git add, git commit.",
        setup=[
            *C("settings.txt", "mode=prod\\ndebug=false", "Add settings"),
            *C("settings.txt", "mode=prod\\ndebug=true", "Enable debug"),
            *C("settings.txt", "mode=staging\\ndebug=verbose", "Switch to staging"),
            "git push",
        ],
        check=_revert_conflict,
    ),
    Exercise(
        id="revert_merge", group=G_REVERT, title="Revert a pushed merge commit", area="remote repository",
        description='feature/pay was merged into main and pushed, but it must come out (pay.py gone, docs.md stays). Undo the merge without rewriting history and push.',
        hint="Plain git revert HEAD fails: a merge has two parents. Use git revert -m 1 HEAD (keep the main side), then git push.",
        takeaway="For a merge commit, -m 1 says 'go back to the first parent' (the branch you merged into). Note: re-merging that branch later will bring nothing back until you revert the revert.",
        setup=[
            "git switch -c feature/pay", *C("pay.py", "pay", "Add pay"), "git switch main",
            *C("docs.md", "# Docs", "Add docs"), "git merge feature/pay", "git push",
        ],
        check=_revert_merge,
    ),

    # ----------------------------------------------------------------- 4. merge
    Exercise(
        id="merge_ff", group=G_MERGE, title="Fast-forward merge", area="local repository",
        description="feature/nav has two commits and main has nothing new. Merge feature/nav into main so that main simply catches up - no merge commit.",
        hint="You are on main: git merge feature/nav. Compare the graph before and after.",
        takeaway="When the current branch is a direct ancestor of the other, git just moves the pointer forward (fast-forward). No new commit is created.",
        setup=NAV, check=_merge_ff,
    ),
    Exercise(
        id="merge_no_ff", group=G_MERGE, title="Force a merge commit (--no-ff)", area="local repository",
        description="Same situation as the fast-forward scenario, but this time the team wants an explicit merge commit that records that feature/nav existed as a branch.",
        hint="git merge --no-ff feature/nav",
        takeaway="--no-ff always creates a merge commit with two parents, keeping the branch visible in history (and making the whole feature revertable with revert -m 1).",
        setup=NAV, check=_merge_no_ff,
    ),
    Exercise(
        id="merge_clean", group=G_MERGE, title="Three-way merge of diverged branches", area="local repository",
        description="feature/login has two commits and main moved on too (docs.md). Merge feature/login into main.",
        hint="You are on main. git merge feature/login - then look at the graph.",
        takeaway="When both branches have new commits, git builds a merge commit from the common ancestor plus both sides. Non-overlapping changes merge automatically.",
        setup=[
            "git switch -c feature/login", *C("login.py", "def login(): pass", "Add login"),
            *A("login.py", "def logout(): pass", "Add logout"), "git switch main", *C("docs.md", "# Docs", "Add docs"),
        ],
        check=_merge_clean,
    ),
    Exercise(
        id="merge_conflict", group=G_MERGE, title="Resolve a merge conflict", area="working directory + staging + repo",
        description="Both branches changed the color line in config.txt. Merge feature/theme into main, resolve the conflict (keep exactly one color= line) and finish the merge.",
        hint="git merge feature/theme -> edit config.txt (echo \"color=blue\\nsize=small\" > config.txt) -> git add config.txt -> git commit",
        takeaway="A conflict puts markers in the working directory. You choose the final text, git add marks it resolved (staging area), git commit completes the merge (repository).",
        setup=THEME, check=_merge_conflict,
    ),
    Exercise(
        id="merge_abort", group=G_MERGE, title="Abort a conflicted merge", area="working directory + staging",
        description="You started merging feature/theme into main and hit a conflict. You are not ready to resolve it. Get back to exactly where main was before the merge, with nothing left behind.",
        hint="git merge --abort",
        takeaway="merge --abort restores HEAD, the staging area and the working directory to the pre-merge state. Conflicts are never a point of no return.",
        setup=[*THEME, "git merge feature/theme"], check=_merge_abort,
    ),
    Exercise(
        id="merge_two_conflicts", group=G_MERGE, title="Resolve two conflicted files differently", area="working directory + staging + repo",
        description='a.txt and b.txt both conflict. Resolve them so a.txt ends as "A=main" (keep main\'s value) and b.txt as "B=feature" (take the feature\'s value), then complete the merge of feature/x.',
        hint="git merge feature/x, then echo \"A=main\" > a.txt and echo \"B=feature\" > b.txt, git add ., git commit",
        takeaway="Each conflicted file is resolved independently - you can pick ours, theirs, or a blend per file. git status lists what is still unmerged.",
        setup=[
            'echo "A=1" > a.txt', 'echo "B=1" > b.txt', "git add .", 'git commit -m "Add a and b"',
            "git switch -c feature/x", 'echo "A=feature" > a.txt', 'echo "B=feature" > b.txt', "git add .", 'git commit -m "Feature values"',
            "git switch main", 'echo "A=main" > a.txt', 'echo "B=main" > b.txt', "git add .", 'git commit -m "Main values"',
        ],
        check=_merge_two,
    ),
    Exercise(
        id="merge_dirty_tree", group=G_MERGE, title="Merge with uncommitted work", area="working directory",
        description="You have an uncommitted README.md edit (draft) on main and want to merge feature/x. Keep your README edit and get feature/x merged into main.",
        hint="The sandbox refuses to merge on a dirty tree (it is the safe habit). Commit first: git commit -am \"Update README\", then git merge feature/x.",
        takeaway="Start merges from a clean working tree: commit (or stash) first, so you can always tell merge changes from your own and abort cleanly.",
        setup=["git switch -c feature/x", *C("x.txt", "x", "Add x"), "git switch main", 'echo "draft" >> README.md'],
        check=_merge_dirty,
    ),
    Exercise(
        id="merge_undo_local", group=G_MERGE, title="Undo a local merge (reset)", area="local repository",
        description='You merged feature/x into main locally by mistake (not pushed). Move main back to "Add docs" so the merge never happened. feature/x must keep its commit.',
        hint="git reset --hard HEAD~1  - HEAD~1 of a merge is its FIRST parent (the main side).",
        takeaway="An unpushed merge is just a commit: reset --hard to the first parent undoes it. If it HAD been pushed you would use revert -m 1 instead.",
        setup=[
            "git switch -c feature/x", *C("x.txt", "x", "Add x"), "git switch main", *C("docs.md", "# Docs", "Add docs"),
            "git merge feature/x",
        ],
        check=_merge_undo_local,
    ),

    # ------------------------------------------------- 5. different branches
    Exercise(
        id="branch_into_feature", group=G_BRANCH, title="Merge main INTO the feature branch", area="local repository",
        description="You are on feature/profile. main got new work (docs.md). Bring main's changes into feature/profile - and keep main itself untouched (it must not get profile.py).",
        hint="Stay on feature/profile: git merge main.  Compare with 'merge feature into main'.",
        takeaway="Direction matters: the branch you are ON moves. 'git merge feature' on main moves main; 'git merge main' on feature moves feature and leaves main alone.",
        setup=[
            "git switch -c feature/profile", *C("profile.py", "def profile(): pass", "Add profile"),
            "git switch main", *C("docs.md", "# Docs", "Add docs"), "git push", "git switch feature/profile",
        ],
        check=_into_feature,
    ),
    Exercise(
        id="branch_ff_from_feature", group=G_BRANCH, title="Fast-forward a feature branch from main", area="local repository",
        description="feature/old was branched long ago and main has two new commits. Make feature/old catch up with main (no merge commit). You are on feature/old.",
        hint="git merge main  - feature/old has nothing of its own, so it fast-forwards.",
        takeaway="The same merge that was a three-way merge before is a fast-forward here because feature/old has no commits of its own. The graph shape - not the command - decides.",
        setup=["git branch feature/old", *C("api.py", "api", "Add api"), *C("tests.py", "tests", "Add tests"), "git push", "git switch feature/old"],
        check=_ff_from_feature,
    ),
    Exercise(
        id="branch_reset_on_feature", group=G_BRANCH, title="reset --hard on a feature branch", area="local repository",
        description='You are on feature/ui; its last commit "Oops: broken ui" is bad. Remove it from feature/ui. main must stay exactly as it is.',
        hint="git reset --hard HEAD~1  - then check that main did not move (git log --oneline --all).",
        takeaway="reset moves only the branch you are on. On a feature branch it is a safe private undo: main and the remote are not affected.",
        setup=[*APP, "git switch -c feature/ui", *C("ui.py", "ui ok", "Add ui"), *C("ui.py", "ui BROKEN", "Oops: broken ui")],
        check=_reset_on_feature,
    ),
    Exercise(
        id="branch_reset_on_main_shared", group=G_BRANCH, title="reset --hard on main when a feature shares the commit", area="local repository",
        description='main has an unpushed commit "Oops: debug code". feature/ui was branched after it, so it contains that commit too. Remove the commit from main (you are on main) - and notice that feature/ui keeps it.',
        hint="git reset --hard HEAD~1, then git log --oneline --all to see feature/ui still has the Oops commit.",
        takeaway="reset only moves the current branch pointer. A commit stays alive as long as ANY branch reaches it, so you may need to fix every branch that contains a bad commit.",
        setup=[*APP, *C("debug.txt", "x", "Oops: debug code"), "git switch -c feature/ui", *C("ui.py", "ui", "Add ui"), "git switch main"],
        check=_reset_on_main_shared,
    ),
    Exercise(
        id="branch_revert_on_feature", group=G_BRANCH, title="Revert on the feature branch, then merge", area="local repository",
        description='feature/ui has "Add ui" (good) and "Add broken sidebar" (bad). Fix it BEFORE it reaches main: undo the sidebar on the feature branch, then merge feature/ui into main. End on main with ui.py but no sidebar.py.',
        hint="git revert HEAD (on feature/ui), git switch main, git merge feature/ui",
        takeaway="Reverting early, on the feature branch, keeps main clean: the bad change and its undo arrive together and main never has a broken state.",
        setup=[*APP, *UI_BRANCH, *C("sidebar.py", "broken", "Add broken sidebar")],
        check=_revert_on_feature,
    ),
    Exercise(
        id="branch_revert_on_main", group=G_BRANCH, title="Revert on main after the merge", area="remote repository",
        description='feature/ui (with "Add ui" and "Add broken sidebar") was already merged into main with --no-ff and pushed. Undo only the broken sidebar ON MAIN and push; feature/ui itself must stay untouched.',
        hint="The merge's second parent is the feature tip: git revert HEAD^2, then git push. (git log --oneline --all helps.)",
        takeaway="Reverting on main undoes the change for everyone on main, while the feature branch keeps its own history. Re-merging that branch later will NOT re-apply the reverted change.",
        setup=[
            *APP, *UI_BRANCH, *C("sidebar.py", "broken", "Add broken sidebar"),
            "git switch main", "git merge --no-ff feature/ui", "git push",
        ],
        check=_revert_on_main,
    ),
    Exercise(
        id="branch_move_edit", group=G_BRANCH, title="Move an edit from main to a feature branch", area="working directory + staging",
        description="You started search.py (already staged) while on main, but it belongs on feature/search. Commit it on feature/search instead; main must not get the commit.",
        hint="git switch feature/search  - uncommitted changes travel with you when the branches point at the same commit. Then git commit.",
        takeaway="Staged and unstaged changes are not owned by a branch - they follow you when you switch (as long as nothing conflicts). Switching first and committing second puts work on the right branch.",
        setup=["git branch feature/search", 'echo "def search(): pass" > search.py', "git add search.py"],
        check=_move_edit,
    ),

    # ------------------------------------------------------------ 6. remote
    Exercise(
        id="push_rejected", group=G_REMOTE, title="Sync with a teammate (pull, then push)", area="remote repository",
        description="You committed mine.txt locally while a teammate pushed notes.txt to origin/main. Get both changes onto origin/main without losing the teammate's work.",
        hint="git push is rejected (non-fast-forward). Run git pull, then git push. Never use --force here!",
        takeaway="git pull = git fetch + git merge origin/main. A rejected push means the remote has commits you lack: integrate them, then push.",
        setup=[*C("mine.txt", "mine", "Add mine.txt"), 'teammate notes.txt "from teammate"'],
        check=_push_rejected,
    ),
    Exercise(
        id="pull_conflict", group=G_REMOTE, title="Pull that conflicts, then push", area="working directory + remote",
        description='You edited notes.txt locally to "mine"; a teammate pushed "theirs" to the same file. Pull, resolve so the file keeps BOTH lines (mine, theirs), finish the merge and push.',
        hint="git pull -> echo \"mine\\ntheirs\" > notes.txt -> git add notes.txt -> git commit -> git push",
        takeaway="A conflicting pull stops mid-merge exactly like git merge does. Resolve, add, commit - and only then push.",
        setup=[*C("notes.txt", "shared", "Add notes"), "git push", *C("notes.txt", "mine", "Edit notes locally"), 'teammate notes.txt "theirs"'],
        check=_pull_conflict,
    ),
    Exercise(
        id="publish_feature", group=G_REMOTE, title="Publish a feature branch", area="remote repository",
        description="feature/cart exists only on your machine. Publish it to origin and set it as the branch's upstream so a plain git push/pull works later. origin/main must not change.",
        hint="git push -u origin feature/cart",
        takeaway="Pushing a new branch creates it on the remote; -u remembers the pairing (upstream). Pushing from a feature branch never moves main.",
        setup=["git switch -c feature/cart", *C("cart.py", "cart", "Add cart")], check=_publish,
    ),
    Exercise(
        id="fetch_merge_feature", group=G_REMOTE, title="Update a feature branch from origin/main", area="local + remote",
        description="A teammate pushed to origin/main. You are on feature/cart and want their work in your branch - but your local main branch must stay as it is.",
        hint="git fetch  (downloads origin/main) then git merge origin/main",
        takeaway="fetch only updates the origin/* pointers; merging origin/main into your feature branch brings the work in without ever touching local main. (git pull would try origin/feature/cart instead.)",
        setup=["git switch -c feature/cart", *C("cart.py", "cart", "Add cart"), 'teammate notes.txt "news"'],
        check=_fetch_merge,
    ),
    Exercise(
        id="force_push_private", group=G_REMOTE, title="Rewrite a private pushed branch", area="local + remote",
        description='You pushed feature/draft with a commit "Oops: API key" that leaks a secret. Nobody else uses this branch. Remove that commit from feature/draft locally AND on origin.',
        hint="git reset --hard HEAD~1, then git push --force  (a normal push is rejected)",
        takeaway="History rewrites on pushed branches need a force push - fine on a branch only you use, harmful on shared ones like main (use revert there). Real leaked secrets must also be rotated!",
        setup=[
            "git switch -c feature/draft", *C("draft.txt", "draft", "Add draft"),
            *C("secrets.txt", "KEY=123", "Oops: API key"), "git push -u origin feature/draft",
        ],
        check=_force_private,
    ),
]

EXERCISE_BY_ID = {e.id: e for e in EXERCISES}


class Lab:
    """Single-user session: one sandbox plus the currently selected exercise."""

    def __init__(self) -> None:
        self.sandbox = Sandbox()
        self.exercise = EXERCISE_BY_ID["sandbox"]
        self._lock = threading.Lock()

    def load(self, exercise_id: str) -> None:
        exercise = EXERCISE_BY_ID[exercise_id]
        with self._lock:
            self.sandbox = Sandbox()
            for command in exercise.setup:
                self.sandbox.run(command)
            self.exercise = exercise

    def run(self, command: str) -> str:
        with self._lock:
            return self.sandbox.run(command)

    def payload(self) -> dict:
        with self._lock:
            ex = self.exercise
            solved = bool(ex.check(self.sandbox)) if ex.check else None
            return {
                "state": self.sandbox.snapshot(),
                "exercise": {
                    "id": ex.id,
                    "group": ex.group,
                    "title": ex.title,
                    "area": ex.area,
                    "description": ex.description,
                    "hint": ex.hint,
                    "takeaway": ex.takeaway,
                    "solved": solved,
                },
            }
