"""Guided practice scenarios and the session object that owns the sandbox."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable

from .engine import CONFLICT_RE, Sandbox


def find(sb: Sandbox, message: str):
    """Newest commit whose message equals ``message``."""
    matches = [c for c in sb.commits.values() if c.message == message]
    return max(matches, key=lambda c: c.seq) if matches else None


def reachable(sb: Sandbox, message: str) -> bool:
    commit = find(sb, message)
    return bool(commit and commit.id in sb.ancestors(sb.head_id))


def clean(sb: Sandbox) -> bool:
    return sb.index == sb.head_tree and sb.wd == sb.head_tree and not sb.pending


@dataclass
class Exercise:
    id: str
    title: str
    area: str
    description: str
    hint: str
    setup: list[str] = field(default_factory=list)
    check: Callable[[Sandbox], bool] | None = None


APP_SETUP = [
    'echo "print(1)" > app.py',
    "git add .",
    'git commit -m "Add app.py"',
    "git push",
]


def _unstaged(sb: Sandbox) -> bool:
    return (
        sb.head_commit.message == "Add app.py"
        and sb.wd.get("app.py") == "print(1)"
        and clean(sb)
    )


def _staged(sb: Sandbox) -> bool:
    return (
        sb.head_commit.message == "Add app.py"
        and sb.index == sb.head_tree
        and sb.wd.get("app.py") == "print(2)  # work in progress"
    )


def _soft(sb: Sandbox) -> bool:
    return (
        sb.head_commit.message == "Add app.py"
        and sb.index.get("app.py") == "print(2)"
        and sb.wd.get("app.py") == "print(2)"
    )


def _hard(sb: Sandbox) -> bool:
    bad = find(sb, "Oops: debug code")
    return (
        sb.head_commit.message == "Add app.py"
        and clean(sb)
        and bad is not None
        and bad.id not in sb.ancestors(sb.head_id)
    )


def _revert_pushed(sb: Sandbox) -> bool:
    good, bad = find(sb, "Add feature A"), find(sb, "Add buggy feature B")
    if not (good and bad):
        return False
    return (
        sb.head_tree == good.tree
        and bad.id in sb.ancestors(sb.head_id)  # history was not rewritten
        and sb.remote_branches["main"] == sb.head_id
        and sb.head_id not in (good.id, bad.id)
        and clean(sb)
    )


def _recover(sb: Sandbox) -> bool:
    return sb.head_commit.message == "Add feature B" and clean(sb)


def _merge_clean(sb: Sandbox) -> bool:
    return (
        sb.head_branch == "main"
        and len(sb.head_commit.parents) == 2
        and "login.py" in sb.head_tree
        and "docs.md" in sb.head_tree
        and clean(sb)
    )


def _merge_conflict(sb: Sandbox) -> bool:
    config = sb.head_tree.get("config.txt", "")
    return (
        sb.head_branch == "main"
        and len(sb.head_commit.parents) == 2
        and not CONFLICT_RE.search(config)
        and sum(line.startswith("color=") for line in config.split("\n")) == 1
        and clean(sb)
    )


def _push_rejected(sb: Sandbox) -> bool:
    mate = find(sb, "Teammate updates notes.txt")
    mine = find(sb, "Add mine.txt")
    ids = sb.ancestors(sb.head_id)
    return bool(
        mate and mine
        and mate.id in ids and mine.id in ids
        and sb.remote_branches["main"] == sb.head_id
        and clean(sb)
    )


EXERCISES: list[Exercise] = [
    Exercise(
        id="sandbox",
        title="Free play",
        area="everything",
        description="No goal - experiment freely. Edit files with echo/touch, then try branches, merges, resets and reverts.",
        hint="Try: echo \"hi\" > a.txt, git add ., git commit -m \"x\", git push. Type 'help' for everything.",
    ),
    Exercise(
        id="unstaged_undo",
        title="Discard a working-directory edit",
        area="working directory",
        description="You edited app.py and broke it. It is NOT staged. Restore app.py to its committed content (print(1)) without creating any commit.",
        hint="git status tells you which command to use: git restore app.py",
        setup=[*APP_SETUP, 'echo "print(BROKEN" > app.py'],
        check=_unstaged,
    ),
    Exercise(
        id="staged_undo",
        title="Unstage a file (keep the edit)",
        area="staging area",
        description="You staged app.py by mistake. Take it out of the staging area, but keep your edit (print(2)  # work in progress) in the working directory.",
        hint="git restore --staged app.py   (or: git reset app.py)",
        setup=[*APP_SETUP, 'echo "print(2)  # work in progress" > app.py', "git add app.py"],
        check=_staged,
    ),
    Exercise(
        id="reset_soft",
        title="Redo a local commit (reset --soft)",
        area="local repository",
        description='The last local commit is too small ("Add print(2)"). Remove the commit but keep its changes STAGED so you can recommit them differently. Nothing is pushed yet.',
        hint="git reset --soft HEAD~1  - moves the branch only; index and working directory stay.",
        setup=[*APP_SETUP, 'echo "print(2)" > app.py', "git add .", 'git commit -m "Add print(2)"'],
        check=_soft,
    ),
    Exercise(
        id="reset_hard",
        title="Throw away a local commit (reset --hard)",
        area="local repository",
        description='The unpushed commit "Oops: debug code" should vanish completely - from history, the staging area AND the working directory.',
        hint="git reset --hard HEAD~1   (careful: this destroys uncommitted work too!)",
        setup=[*APP_SETUP, 'echo "print(1); debug()" > app.py', "git add .", 'git commit -m "Oops: debug code"'],
        check=_hard,
    ),
    Exercise(
        id="reflog_recover",
        title="Recover from a bad reset",
        area="local repository",
        description='You ran reset --hard and lost the commit "Add feature B". Get it back.',
        hint="git reflog lists where HEAD has been. Then: git reset --hard <hash>",
        setup=[
            'echo "A" > feature.txt', "git add .", 'git commit -m "Add feature A"',
            'echo "A+B" > feature.txt', "git add .", 'git commit -m "Add feature B"',
            "git reset --hard HEAD~1",
        ],
        check=_recover,
    ),
    Exercise(
        id="revert_pushed",
        title="Undo a pushed commit (revert)",
        area="remote repository",
        description='"Add buggy feature B" is already on origin/main, so history must not be rewritten. Undo its changes with a new commit and push the result.',
        hint="git revert HEAD   then   git push",
        setup=[
            'echo "A" > feature.txt', "git add .", 'git commit -m "Add feature A"',
            'echo "A + bug" > feature.txt', "git add .", 'git commit -m "Add buggy feature B"',
            "git push",
        ],
        check=_revert_pushed,
    ),
    Exercise(
        id="merge_clean",
        title="Merge a feature branch",
        area="local repository",
        description="Branch feature/login has two commits and main moved on too (docs.md). Merge feature/login into main.",
        hint="You are on main. git merge feature/login  - then look at the graph.",
        setup=[
            "git switch -c feature/login",
            'echo "def login(): pass" > login.py', "git add .", 'git commit -m "Add login"',
            'echo "def logout(): pass" >> login.py', "git add .", 'git commit -m "Add logout"',
            "git switch main",
            'echo "# Docs" > docs.md', "git add .", 'git commit -m "Add docs"',
        ],
        check=_merge_clean,
    ),
    Exercise(
        id="merge_conflict",
        title="Resolve a merge conflict",
        area="working directory + staging + repo",
        description="Both branches changed the color line in config.txt. Merge feature/theme into main, resolve the conflict (keep exactly one color= line) and finish the merge.",
        hint="git merge feature/theme -> edit config.txt (echo \"color=blue\\nsize=small\" > config.txt) -> git add config.txt -> git commit -m \"Merge theme\". Or give up with git merge --abort.",
        setup=[
            'echo "color=red\\nsize=small" > config.txt', "git add .", 'git commit -m "Add config"',
            "git switch -c feature/theme",
            'echo "color=blue\\nsize=small" > config.txt', "git add .", 'git commit -m "Blue theme"',
            "git switch main",
            'echo "color=green\\nsize=small" > config.txt', "git add .", 'git commit -m "Green theme"',
        ],
        check=_merge_conflict,
    ),
    Exercise(
        id="push_rejected",
        title="Sync with a teammate (pull, then push)",
        area="remote repository",
        description="You committed mine.txt locally while a teammate pushed notes.txt to origin/main. Get both changes onto origin/main without losing the teammate's work.",
        hint="git push is rejected (non-fast-forward). Run git pull, then git push. Never use --force here!",
        setup=[
            'echo "mine" > mine.txt', "git add .", 'git commit -m "Add mine.txt"',
            'teammate notes.txt "from teammate"',
        ],
        check=_push_rejected,
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
                    "title": ex.title,
                    "area": ex.area,
                    "description": ex.description,
                    "hint": ex.hint,
                    "solved": solved,
                },
            }
