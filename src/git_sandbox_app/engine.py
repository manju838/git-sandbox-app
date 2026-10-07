"""A small, deterministic, in-memory simulation of git.

The sandbox models the four places a change can live:

    working directory  ->  staging area (index)  ->  local repository  ->  remote repository

Everything is plain Python data (dicts of ``path -> text``), so a "tree" is just a
snapshot dict.  Commits are immutable nodes in one shared object store; the local
and remote repositories are simply different sets of refs pointing into that store.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import shlex
from dataclasses import dataclass

Tree = dict[str, str]

REMOTE = "origin"
CONFLICT_RE = re.compile(r"^(<{7}|={7}|>{7})", re.MULTILINE)

HELP_TEXT = """\
Git commands simulated in this sandbox
  git status | log [--oneline] [--all] | reflog | diff [--staged] | show <rev>
  git add <file>|. | restore [--staged] <file> | rm [--cached] <file> | clean -f
  git commit -m "msg" [-a] [--amend]
  git reset [--soft|--mixed|--hard] [<rev>]      git reset <file>
  git revert [-n] [-m 1] <rev>                    git revert --abort
  git branch [name] [-d name] | checkout [-b] <branch|rev> | switch [-c] <branch>
  git merge [--no-ff] [--abort] <branch>
  git fetch | pull | push [-u] [--force] [origin] [branch] | remote -v

Revisions: HEAD, HEAD~2, HEAD^, a branch, origin/main, or a commit hash.

Helpers that stand in for your editor and for a teammate
  touch <file>                    create an empty file
  echo "text" > <file>            overwrite a file   (use >> to append, \\n for a newline)
  cat <file> | ls | rm <file>
  teammate <file> "content"       a teammate pushes a commit straight to origin/main
  clear                           clear the terminal
"""


class GitError(Exception):
    """Raised for anything git itself would refuse to do."""


@dataclass
class Commit:
    id: str
    message: str
    parents: list[str]
    tree: Tree
    seq: int
    author: str = "you"


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def split_args(line: str) -> list[str]:
    lex = shlex.shlex(line, posix=True)
    lex.whitespace_split = True
    lex.escape = ""
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError as exc:
        raise GitError(f"error: {exc}") from exc


def find_redirect(body: str) -> tuple[int, str] | None:
    """Locate the first unquoted ``>`` or ``>>`` in ``body``."""
    quote = ""
    for i, ch in enumerate(body):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == ">":
            return i, ">>" if body[i:i + 2] == ">>" else ">"
    return None


def unescape(text: str) -> str:
    return text.replace("\\n", "\n")


def norm_path(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def valid_file_name(path: str) -> str:
    path = norm_path(path)
    if not re.fullmatch(r"[\w][\w.\-]*(/[\w][\w.\-]*)*", path):
        raise GitError(f"error: invalid file name '{path}'")
    return path


def diff_trees(a: Tree, b: Tree) -> dict[str, str]:
    """Return ``path -> added|modified|deleted`` going from tree ``a`` to tree ``b``."""
    changes: dict[str, str] = {}
    for path in sorted(set(a) | set(b)):
        if path not in a:
            changes[path] = "added"
        elif path not in b:
            changes[path] = "deleted"
        elif a[path] != b[path]:
            changes[path] = "modified"
    return changes


def _lines(text: str) -> list[str]:
    return text.split("\n") if text else []


def _hunks(base: list[str], other: list[str]):
    matcher = difflib.SequenceMatcher(None, base, other, autojunk=False)
    return [
        (i1, i2, other[j1:j2])
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    ]


def merge3_text(base: str, ours: str, theirs: str, label: str) -> tuple[str, bool]:
    """Line based three-way merge. Returns ``(text, had_conflict)``."""
    b, o, t = _lines(base), _lines(ours), _lines(theirs)
    hunks = [(i1, i2, rep, 0) for i1, i2, rep in _hunks(b, o)]
    hunks += [(i1, i2, rep, 1) for i1, i2, rep in _hunks(b, t)]
    hunks.sort(key=lambda h: (h[0], h[1]))

    out: list[str] = []
    pos = 0
    conflict = False
    k = 0
    while k < len(hunks):
        start, end = hunks[k][0], hunks[k][1]
        group = [hunks[k]]
        k += 1
        # Overlapping *or adjacent* changes form one group (git conflicts on those too).
        while k < len(hunks) and hunks[k][0] <= end:
            end = max(end, hunks[k][1])
            group.append(hunks[k])
            k += 1

        out += b[pos:start]
        sides: list[list[str]] = []
        for side in (0, 1):
            res: list[str] = []
            p = start
            for i1, i2, rep, owner in group:
                if owner != side:
                    continue
                res += b[p:i1]
                res += rep
                p = i2
            res += b[p:end]
            sides.append(res)

        original = b[start:end]
        ours_res, theirs_res = sides
        if ours_res == theirs_res:
            out += ours_res
        elif ours_res == original:
            out += theirs_res
        elif theirs_res == original:
            out += ours_res
        else:
            conflict = True
            out += ["<<<<<<< HEAD", *ours_res, "=======", *theirs_res, f">>>>>>> {label}"]
        pos = end

    out += b[pos:]
    return "\n".join(out), conflict


@dataclass
class MergeResult:
    worktree: Tree
    index: Tree
    conflicts: list[str]
    notes: list[str]


def merge_trees(base: Tree, ours: Tree, theirs: Tree, label: str) -> MergeResult:
    """Three-way merge of whole trees (used by merge, pull and revert)."""
    worktree: Tree = {}
    index: Tree = {}
    conflicts: list[str] = []
    notes: list[str] = []

    for path in sorted(set(base) | set(ours) | set(theirs)):
        b, o, t = base.get(path), ours.get(path), theirs.get(path)

        if o == t:
            result = o
        elif b == o:
            result = t
        elif b == t:
            result = o
        elif o is None or t is None:
            kept = o if o is not None else t
            gone, changed = ("HEAD", label) if o is None else (label, "HEAD")
            notes.append(f"CONFLICT (modify/delete): {path} deleted in {gone} and modified in {changed}.")
            conflicts.append(path)
            worktree[path] = kept  # type: ignore[assignment]
            if o is not None:
                index[path] = o
            continue
        else:
            merged, bad = merge3_text(b or "", o, t, label)
            if bad:
                notes.append(f"Auto-merging {path}")
                notes.append(f"CONFLICT (content): Merge conflict in {path}")
                conflicts.append(path)
                worktree[path] = merged
                index[path] = o
                continue
            notes.append(f"Auto-merging {path}")
            result = merged

        if result is not None:
            worktree[path] = index[path] = result

    return MergeResult(worktree, index, conflicts, notes)


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# --------------------------------------------------------------------------- #
# The sandbox
# --------------------------------------------------------------------------- #
class Sandbox:
    def __init__(self) -> None:
        self.reset()

    # ----------------------------------------------------------------- setup
    def reset(self) -> None:
        self.commits: dict[str, Commit] = {}
        self.seq = 0
        self.reflog: list[tuple[str, str]] = []

        root = self._new_commit("Initial commit", [], {"README.md": "# Demo project"})
        self.branches: dict[str, str] = {"main": root.id}
        self.head_branch: str | None = "main"
        self.detached_id: str | None = None

        self.remote_branches: dict[str, str] = {"main": root.id}
        self.tracking: dict[str, str] = {f"{REMOTE}/main": root.id}
        self.upstream: dict[str, str] = {"main": f"{REMOTE}/main"}

        self.wd: Tree = dict(root.tree)
        self.index: Tree = dict(root.tree)

        self.conflicts: set[str] = set()
        self.pending: dict | None = None  # {"kind": "merge"|"revert", "other": id|None, "message": str}
        self._log_ref(root.id, "commit (initial): Initial commit")

    # ------------------------------------------------------------ properties
    @property
    def head_id(self) -> str:
        return self.branches[self.head_branch] if self.head_branch else self.detached_id  # type: ignore[return-value]

    @property
    def head_commit(self) -> Commit:
        return self.commits[self.head_id]

    @property
    def head_tree(self) -> Tree:
        return self.head_commit.tree

    # ---------------------------------------------------------- graph basics
    def _new_commit(self, message: str, parents: list[str], tree: Tree, author: str = "you") -> Commit:
        self.seq += 1
        digest = hashlib.sha1(
            f"{self.seq}|{message}|{parents}|{sorted(tree.items())}".encode()
        ).hexdigest()[:7]
        commit = Commit(digest, message, list(parents), dict(tree), self.seq, author)
        self.commits[digest] = commit
        return commit

    def ancestors(self, cid: str) -> set[str]:
        seen: set[str] = set()
        stack = [cid]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(self.commits[current].parents)
        return seen

    def merge_base(self, a: str, b: str) -> str | None:
        common = self.ancestors(a) & self.ancestors(b)
        return max(common, key=lambda c: self.commits[c].seq) if common else None

    def _log_ref(self, cid: str, reason: str) -> None:
        self.reflog.append((cid, reason))

    def _set_head(self, cid: str, reason: str) -> None:
        if self.head_branch:
            self.branches[self.head_branch] = cid
        else:
            self.detached_id = cid
        self._log_ref(cid, reason)

    def local_refs(self) -> list[tuple[str, str]]:
        refs = [(name, cid) for name, cid in self.branches.items()]
        refs += list(self.tracking.items())
        refs.append(("HEAD", self.head_id))
        return refs

    # ------------------------------------------------------------ revisions
    def resolve(self, spec: str) -> str:
        match = re.fullmatch(r"([^~^]+)((?:[~^]\d*)*)", spec)
        if not match:
            raise GitError(f"fatal: ambiguous argument '{spec}': unknown revision or path not in the working tree.")
        base, suffix = match.groups()
        cid = self._resolve_base(base, spec)
        for op, num in re.findall(r"([~^])(\d*)", suffix):
            commit = self.commits[cid]
            if op == "~":
                for _ in range(int(num) if num else 1):
                    if not commit.parents:
                        raise GitError(f"fatal: ambiguous argument '{spec}': unknown revision (not enough history).")
                    commit = self.commits[commit.parents[0]]
            else:
                n = int(num) if num else 1
                if n:
                    if len(commit.parents) < n:
                        raise GitError(f"fatal: ambiguous argument '{spec}': unknown revision (no such parent).")
                    commit = self.commits[commit.parents[n - 1]]
            cid = commit.id
        return cid

    def _resolve_base(self, base: str, spec: str) -> str:
        if base in ("HEAD", "@"):
            return self.head_id
        if base in self.branches:
            return self.branches[base]
        if base in self.tracking:
            return self.tracking[base]
        if re.fullmatch(r"[0-9a-f]{4,40}", base):
            matches = [c for c in self.commits if c.startswith(base)]
            if len(matches) == 1:
                return matches[0]
        raise GitError(f"fatal: ambiguous argument '{spec}': unknown revision or path not in the working tree.")

    def _try_resolve(self, spec: str) -> str | None:
        try:
            return self.resolve(spec)
        except GitError:
            return None

    def describe(self, cid: str) -> str:
        return f"{cid} {self.commits[cid].message}"

    def decorations(self, cid: str) -> str:
        names: list[str] = []
        if self.head_id == cid:
            names.append(f"HEAD -> {self.head_branch}" if self.head_branch else "HEAD")
        names += [b for b, i in self.branches.items() if i == cid and b != self.head_branch]
        names += [r for r, i in self.tracking.items() if i == cid]
        return f" ({', '.join(names)})" if names else ""

    # ----------------------------------------------------- status computing
    def staged_changes(self) -> dict[str, str]:
        return {p: s for p, s in diff_trees(self.head_tree, self.index).items() if p not in self.conflicts}

    def unstaged_changes(self) -> dict[str, str]:
        changes: dict[str, str] = {}
        for path, content in self.index.items():
            if path in self.conflicts:
                continue
            if path not in self.wd:
                changes[path] = "deleted"
            elif self.wd[path] != content:
                changes[path] = "modified"
        return changes

    def untracked(self) -> list[str]:
        return sorted(set(self.wd) - set(self.index))

    def _require_clean(self, action: str) -> None:
        if self.pending or self.conflicts:
            raise GitError(f"error: You have not concluded your {self.pending['kind'] if self.pending else 'merge'} (resolve it, or use --abort).")
        if self.staged_changes() or self.unstaged_changes():
            raise GitError(
                f"error: Your local changes would be overwritten by {action}.\n"
                f"Please commit your changes (or discard them) before you {action}.\nAborting"
            )

    def _guard_untracked(self, new_paths) -> None:
        clash = sorted(p for p in new_paths if p in self.wd and p not in self.index)
        if clash:
            raise GitError(
                "error: The following untracked working tree files would be overwritten:\n"
                + "\n".join(f"\t{p}" for p in clash)
                + "\nPlease move or remove them before you proceed.\nAborting"
            )

    # --------------------------------------------------------------- entry
    def run(self, line: str) -> str:
        """Run one command line and return its terminal output."""
        line = line.strip()
        if line.startswith("$"):
            line = line[1:].strip()
        if not line:
            return ""
        try:
            return self._dispatch(line)
        except GitError as exc:
            return str(exc)

    def _dispatch(self, line: str) -> str:
        word = line.split(None, 1)[0]
        if word == "echo":
            return self._echo(line)
        args = split_args(line)
        cmd, rest = args[0], args[1:]
        if cmd == "git":
            return self._git(rest)
        simple = {
            "touch": self._touch,
            "rm": self._shell_rm,
            "cat": self._cat,
            "ls": self._ls,
            "teammate": self._teammate,
        }
        if cmd in simple:
            return simple[cmd](rest)
        if cmd == "help":
            return HELP_TEXT
        return f"{cmd}: command not found. Type 'help' to see what this sandbox understands."

    # ----------------------------------------------------- shell helpers
    def _echo(self, line: str) -> str:
        body = line[len("echo"):].strip()
        redirect = find_redirect(body)
        if redirect is None:
            return unescape(" ".join(split_args(body)))
        start, op = redirect
        text = unescape(" ".join(split_args(body[:start])))
        targets = split_args(body[start + len(op):])
        if len(targets) != 1:
            raise GitError(f"error: expected exactly one file name after '{op}'")
        path = valid_file_name(targets[0])
        if op == ">>" and self.wd.get(path):
            self.wd[path] = self.wd[path] + "\n" + text
        else:
            self.wd[path] = text
        return ""

    def _touch(self, args: list[str]) -> str:
        if not args:
            raise GitError("touch: missing file operand")
        for name in args:
            self.wd.setdefault(valid_file_name(name), "")
        return ""

    def _shell_rm(self, args: list[str]) -> str:
        files = [a for a in args if not a.startswith("-")]
        if not files:
            raise GitError("rm: missing operand")
        for name in files:
            path = norm_path(name)
            if path not in self.wd:
                raise GitError(f"rm: cannot remove '{name}': No such file")
            del self.wd[path]
        return ""

    def _cat(self, args: list[str]) -> str:
        out = []
        for name in args:
            path = norm_path(name)
            if path not in self.wd:
                raise GitError(f"cat: {name}: No such file")
            out.append(self.wd[path])
        return "\n".join(out)

    def _ls(self, _args: list[str]) -> str:
        return "\n".join(sorted(self.wd)) or "(empty directory)"

    def _teammate(self, args: list[str]) -> str:
        if len(args) < 2:
            raise GitError('usage: teammate <file> "new content"')
        path = valid_file_name(args[0])
        content = unescape(" ".join(args[1:]))
        tip = self.commits[self.remote_branches["main"]]
        tree = dict(tip.tree)
        tree[path] = content
        commit = self._new_commit(f"Teammate updates {path}", [tip.id], tree, author="teammate")
        self.remote_branches["main"] = commit.id
        return (
            f"Teammate pushed {commit.id} to {REMOTE}/main.\n"
            "Your local repo does not know yet - run 'git fetch' or 'git pull'."
        )

    # ------------------------------------------------------------- git cmds
    def _git(self, args: list[str]) -> str:
        if not args:
            return "usage: git <command> [<args>]   (type 'help' to see supported commands)"
        sub, rest = args[0], args[1:]
        table = {
            "status": self._status,
            "add": self._add,
            "commit": self._commit,
            "log": self._log,
            "reflog": self._reflog,
            "diff": self._diff,
            "show": self._show,
            "branch": self._branch,
            "checkout": self._checkout,
            "switch": self._switch_cmd,
            "restore": self._restore,
            "reset": self._reset,
            "revert": self._revert,
            "merge": self._merge_cmd,
            "push": self._push,
            "fetch": self._fetch_cmd,
            "pull": self._pull,
            "remote": self._remote,
            "rm": self._git_rm,
            "clean": self._clean,
        }
        if sub in ("help", "--help"):
            return HELP_TEXT
        fn = table.get(sub)
        if fn is None:
            raise GitError(f"git: '{sub}' is not simulated in this sandbox. Type 'help' to see supported commands.")
        return fn(rest)

    # ---- status
    def _status(self, _args: list[str]) -> str:
        out: list[str] = []
        if self.head_branch:
            out.append(f"On branch {self.head_branch}")
            up = self.upstream.get(self.head_branch)
            if up and up in self.tracking:
                local, remote = self.head_id, self.tracking[up]
                ahead = len(self.ancestors(local) - self.ancestors(remote))
                behind = len(self.ancestors(remote) - self.ancestors(local))
                if ahead and behind:
                    out.append(
                        f"Your branch and '{up}' have diverged,\n"
                        f"and have {plural(ahead, 'commit')} and {plural(behind, 'commit')} different each, respectively."
                    )
                elif ahead:
                    out.append(f"Your branch is ahead of '{up}' by {plural(ahead, 'commit')}.\n  (use \"git push\" to publish your local commits)")
                elif behind:
                    out.append(f"Your branch is behind '{up}' by {plural(behind, 'commit')}, and can be fast-forwarded.\n  (use \"git pull\" to update your local branch)")
                else:
                    out.append(f"Your branch is up to date with '{up}'.")
        else:
            out.append(f"HEAD detached at {self.head_id}")

        verb = {"added": "new file:   ", "modified": "modified:   ", "deleted": "deleted:    "}

        if self.conflicts:
            out.append("")
            kind = self.pending["kind"] if self.pending else "merge"
            out.append(f'You have unmerged paths.\n  (fix conflicts and run "git commit")\n  (use "git {kind} --abort" to abort the {kind})')
            out.append("\nUnmerged paths:\n  (use \"git add <file>...\" to mark resolution)")
            out += [f"\tboth modified:   {p}" for p in sorted(self.conflicts)]
        elif self.pending:
            out.append("\nAll conflicts fixed but you are still merging.\n  (use \"git commit\" to conclude)")

        staged = self.staged_changes()
        if staged:
            out.append('\nChanges to be committed:\n  (use "git restore --staged <file>..." to unstage)')
            out += [f"\t{verb[s]}{p}" for p, s in staged.items()]

        unstaged = self.unstaged_changes()
        if unstaged:
            out.append('\nChanges not staged for commit:\n  (use "git add <file>..." to update what will be committed)\n  (use "git restore <file>..." to discard changes in working directory)')
            out += [f"\t{verb[s]}{p}" for p, s in unstaged.items()]

        untracked = self.untracked()
        if untracked:
            out.append('\nUntracked files:\n  (use "git add <file>..." to include in what will be committed)')
            out += [f"\t{p}" for p in untracked]

        if not (staged or unstaged or untracked or self.conflicts or self.pending):
            out.append("\nnothing to commit, working tree clean")
        elif not staged and not self.conflicts:
            out.append(
                '\nno changes added to commit (use "git add" and/or "git commit -a")'
                if unstaged
                else '\nnothing added to commit but untracked files present (use "git add" to track)'
            )
        return "\n".join(out)

    # ---- add / rm / clean
    def _expand(self, paths: list[str], universe) -> list[str]:
        result: list[str] = []
        for raw in paths:
            path = norm_path(raw)
            if path == ".":
                result += sorted(universe)
            elif path in universe:
                result.append(path)
            else:
                raise GitError(f"fatal: pathspec '{raw}' did not match any files")
        return result

    def _add(self, args: list[str]) -> str:
        flags = [a for a in args if a.startswith("-")]
        paths = [a for a in args if not a.startswith("-")]
        every = "-A" in flags or "--all" in flags
        tracked_only = "-u" in flags or "--update" in flags
        if not paths and not (every or tracked_only):
            raise GitError("Nothing specified, nothing added.\nhint: Maybe you wanted to say 'git add .'?")

        if every or (tracked_only and not paths):
            universe = set(self.index) if tracked_only else set(self.wd) | set(self.index)
            targets = sorted(universe)
        else:
            targets = self._expand(paths, set(self.wd) | set(self.index))
            if tracked_only:
                targets = [t for t in targets if t in self.index]

        out: list[str] = []
        for path in targets:
            if path in self.wd:
                if path in self.conflicts and CONFLICT_RE.search(self.wd[path]):
                    out.append(f"warning: '{path}' still contains conflict markers (<<<<<<<, =======, >>>>>>>).")
                self.index[path] = self.wd[path]
            else:
                self.index.pop(path, None)
            self.conflicts.discard(path)
        return "\n".join(out)

    def _git_rm(self, args: list[str]) -> str:
        cached = "--cached" in args
        paths = [a for a in args if not a.startswith("-")]
        if not paths:
            raise GitError("fatal: No pathspec was given. Which files should I remove?")
        for path in self._expand(paths, set(self.index)):
            self.index.pop(path, None)
            if not cached:
                self.wd.pop(path, None)
            self.conflicts.discard(path)
        return "\n".join(f"rm '{norm_path(p)}'" for p in paths)

    def _clean(self, args: list[str]) -> str:
        dry = "-n" in args or "--dry-run" in args
        if not dry and not any(a in ("-f", "-fd", "-df", "--force") for a in args):
            raise GitError("fatal: clean.requireForce is true and neither -f nor -n was given; refusing to clean")
        files = self.untracked()
        if not dry:
            for path in files:
                del self.wd[path]
        prefix = "Would remove" if dry else "Removing"
        return "\n".join(f"{prefix} {p}" for p in files) or "Nothing to clean."

    # ---- commit
    def _commit(self, args: list[str]) -> str:
        messages: list[str] = []
        all_ = amend = allow_empty = False
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("-m", "--message", "-am", "-ma"):
                if a in ("-am", "-ma"):
                    all_ = True
                if i + 1 >= len(args):
                    raise GitError(f"error: switch `{a.lstrip('-')[0]}' requires a value")
                messages.append(args[i + 1])
                i += 1
            elif a.startswith("-m") and len(a) > 2 and not a.startswith("--"):
                messages.append(a[2:])
            elif a in ("-a", "--all"):
                all_ = True
            elif a == "--amend":
                amend = True
            elif a == "--allow-empty":
                allow_empty = True
            elif a == "--no-edit":
                pass
            else:
                raise GitError(f"error: unknown option '{a}' for 'git commit'")
            i += 1
        message = "\n\n".join(messages) if messages else None

        if self.conflicts:
            raise GitError(
                "error: Committing is not possible because you have unmerged files.\n"
                "hint: Fix them up in the work tree, and then use 'git add <file>'\n"
                "hint: as appropriate to mark resolution and make a commit."
            )
        if all_:
            for path in list(self.index):
                if path in self.wd:
                    self.index[path] = self.wd[path]
                else:
                    del self.index[path]

        if amend:
            if self.pending:
                raise GitError("fatal: You are in the middle of a merge -- cannot amend.")
            old = self.head_commit
            new = self._new_commit(message or old.message, old.parents, self.index)
            pushed = old.id in self._remote_reachable()
            self._set_head(new.id, f"commit (amend): {new.message}")
            note = (
                f"\nNote: the old commit {old.id} was already pushed - 'git push' will be rejected, you'd need --force."
                if pushed else ""
            )
            return f"[{self.head_branch or 'detached HEAD'} {new.id}] {new.message}{note}"

        if self.index == self.head_tree and not self.pending and not allow_empty:
            return self._status([])  # git prints the status plus a hint
        if message is None:
            if self.pending:
                message = self.pending["message"]
            else:
                raise GitError('error: no commit message given. In this sandbox use: git commit -m "message"')

        parents = [self.head_id]
        kind = "commit"
        if self.pending and self.pending["kind"] == "merge":
            parents.append(self.pending["other"])
            kind = "commit (merge)"
        before = self.head_tree
        new = self._new_commit(message, parents, self.index)
        self._set_head(new.id, f"{kind}: {message}")
        self.pending = None
        changed = len(diff_trees(before, new.tree))
        return f"[{self.head_branch or 'detached HEAD'} {new.id}] {message}\n {plural(changed, 'file')} changed"

    def _remote_reachable(self) -> set[str]:
        reach: set[str] = set()
        for cid in self.remote_branches.values():
            reach |= self.ancestors(cid)
        return reach

    # ---- log / reflog / diff / show
    def _log(self, args: list[str]) -> str:
        oneline = "--oneline" in args
        all_ = "--all" in args
        limit = None
        rev = None
        i = 0
        while i < len(args):
            a = args[i]
            if a == "-n" and i + 1 < len(args):
                limit = int(args[i + 1])
                i += 1
            elif re.fullmatch(r"-\d+", a):
                limit = int(a[1:])
            elif a.startswith("--max-count="):
                limit = int(a.split("=", 1)[1])
            elif not a.startswith("-"):
                rev = a
            i += 1

        if all_:
            tips = [cid for _, cid in self.local_refs()]
        else:
            tips = [self.resolve(rev) if rev else self.head_id]
        ids: set[str] = set()
        for tip in tips:
            ids |= self.ancestors(tip)
        ordered = sorted(ids, key=lambda c: self.commits[c].seq, reverse=True)
        if limit is not None:
            ordered = ordered[:limit]

        out: list[str] = []
        for cid in ordered:
            commit = self.commits[cid]
            if oneline:
                out.append(f"{cid}{self.decorations(cid)} {commit.message}")
            else:
                out.append(f"commit {cid}{self.decorations(cid)}")
                if len(commit.parents) > 1:
                    out.append("Merge: " + " ".join(commit.parents))
                out.append(f"Author: {commit.author}\n\n    {commit.message}\n")
        return "\n".join(out).rstrip()

    def _reflog(self, _args: list[str]) -> str:
        total = len(self.reflog)
        return "\n".join(
            f"{cid} HEAD@{{{n}}}: {reason}"
            for n, (cid, reason) in enumerate(reversed(self.reflog))
        ) if total else ""

    @staticmethod
    def _unified(a: Tree, b: Tree, paths=None) -> str:
        out: list[str] = []
        for path in sorted(set(a) | set(b)):
            if paths is not None and path not in paths:
                continue
            if a.get(path) == b.get(path):
                continue
            out.append(f"diff --git a/{path} b/{path}")
            old = _lines(a[path]) if path in a else []
            new = _lines(b[path]) if path in b else []
            out.append(f"--- {'a/' + path if path in a else '/dev/null'}")
            out.append(f"+++ {'b/' + path if path in b else '/dev/null'}")
            out += list(difflib.unified_diff(old, new, lineterm="", n=2))[2:]
        return "\n".join(out)

    def _diff(self, args: list[str]) -> str:
        flags = [a for a in args if a.startswith("-")]
        revs = [a for a in args if not a.startswith("-")]
        if "--staged" in flags or "--cached" in flags:
            base = self.commits[self.resolve(revs[0])].tree if revs else self.head_tree
            return self._unified(base, self.index)
        if len(revs) == 2:
            return self._unified(self.commits[self.resolve(revs[0])].tree, self.commits[self.resolve(revs[1])].tree)
        if len(revs) == 1:
            tree = self.commits[self.resolve(revs[0])].tree
            return self._unified(tree, self.wd, paths=set(self.index))
        return self._unified(self.index, self.wd, paths=set(self.index))

    def _show(self, args: list[str]) -> str:
        cid = self.resolve(args[0] if args else "HEAD")
        commit = self.commits[cid]
        parent_tree = self.commits[commit.parents[0]].tree if commit.parents else {}
        header = f"commit {cid}{self.decorations(cid)}\nAuthor: {commit.author}\n\n    {commit.message}\n"
        return header + "\n" + self._unified(parent_tree, commit.tree)

    # ---- branches / checkout / switch
    @staticmethod
    def _valid_branch(name: str) -> str:
        if not re.fullmatch(r"[\w][\w./\-]*", name) or name.endswith("/") or ".." in name:
            raise GitError(f"fatal: '{name}' is not a valid branch name")
        return name

    def _branch(self, args: list[str]) -> str:
        flags = [a for a in args if a.startswith("-")]
        names = [a for a in args if not a.startswith("-")]
        if "-d" in flags or "-D" in flags or "--delete" in flags:
            if not names:
                raise GitError("fatal: branch name required")
            out = []
            for name in names:
                if name not in self.branches:
                    raise GitError(f"error: branch '{name}' not found.")
                if name == self.head_branch:
                    raise GitError(f"error: Cannot delete branch '{name}' checked out in the sandbox")
                tip = self.branches[name]
                if "-D" not in flags and tip not in self.ancestors(self.head_id):
                    raise GitError(
                        f"error: The branch '{name}' is not fully merged.\n"
                        f"If you are sure you want to delete it, run 'git branch -D {name}'."
                    )
                del self.branches[name]
                self.upstream.pop(name, None)
                out.append(f"Deleted branch {name} (was {tip}).")
            return "\n".join(out)

        if names:
            name = self._valid_branch(names[0])
            if name in self.branches:
                raise GitError(f"fatal: a branch named '{name}' already exists")
            start = self.resolve(names[1]) if len(names) > 1 else self.head_id
            self.branches[name] = start
            return ""

        verbose = "-v" in flags or "-vv" in flags
        lines = []
        for name in sorted(self.branches):
            marker = "*" if name == self.head_branch else " "
            extra = f" {self.describe(self.branches[name])}" if verbose else ""
            lines.append(f"{marker} {name}{extra}")
        if not self.head_branch:
            lines.insert(0, f"* (HEAD detached at {self.head_id})")
        if "-a" in flags or "-r" in flags:
            remote_lines = [f"  remotes/{r}" for r in sorted(self.tracking)]
            lines = remote_lines if "-r" in flags else lines + remote_lines
        return "\n".join(lines)

    def _move_worktree(self, target_id: str) -> None:
        """Move index + working tree to ``target_id``'s tree, keeping unrelated local edits."""
        if self.pending or self.conflicts:
            raise GitError("error: you need to resolve your current index first")
        old, new = self.head_tree, self.commits[target_id].tree
        changed = [p for p in set(old) | set(new) if old.get(p) != new.get(p)]
        untracked = sorted(p for p in changed if p not in old and p in self.wd and p not in self.index)
        dirty = sorted(
            p for p in changed
            if p not in untracked and (self.index.get(p) != old.get(p) or self.wd.get(p) != self.index.get(p))
        )
        if dirty:
            raise GitError(
                "error: Your local changes to the following files would be overwritten by checkout:\n"
                + "\n".join(f"\t{p}" for p in dirty)
                + "\nPlease commit your changes or discard them (git restore / git reset) before you switch.\nAborting"
            )
        if untracked:
            raise GitError(
                "error: The following untracked working tree files would be overwritten by checkout:\n"
                + "\n".join(f"\t{p}" for p in untracked) + "\nAborting"
            )
        for path in changed:
            if path in new:
                self.index[path] = self.wd[path] = new[path]
            else:
                self.index.pop(path, None)
                self.wd.pop(path, None)

    def _switch_to_branch(self, name: str) -> str:
        if name == self.head_branch:
            return f"Already on '{name}'"
        old = self.head_branch or self.head_id
        self._move_worktree(self.branches[name])
        self.head_branch, self.detached_id = name, None
        self._log_ref(self.head_id, f"checkout: moving from {old} to {name}")
        return f"Switched to branch '{name}'"

    def _switch_detached(self, cid: str) -> str:
        old = self.head_branch or self.head_id
        self._move_worktree(cid)
        self.head_branch, self.detached_id = None, cid
        self._log_ref(cid, f"checkout: moving from {old} to {cid}")
        return (
            f"Note: switching to '{cid}'.\n\n"
            "You are in 'detached HEAD' state. Commits you make here belong to no branch -\n"
            "create one with 'git switch -c <name>' to keep them.\n\n"
            f"HEAD is now at {self.describe(cid)}"
        )

    def _create_branch_and_switch(self, name: str, start: str | None) -> str:
        name = self._valid_branch(name)
        if name in self.branches:
            raise GitError(f"fatal: a branch named '{name}' already exists")
        start_id = self.resolve(start) if start else self.head_id
        old = self.head_branch or self.head_id
        if start_id != self.head_id:
            self._move_worktree(start_id)
        self.branches[name] = start_id
        if start and start in self.tracking:
            self.upstream[name] = start
        self.head_branch, self.detached_id = name, None
        self._log_ref(start_id, f"checkout: moving from {old} to {name}")
        return f"Switched to a new branch '{name}'"

    def _switch_target(self, target: str) -> str:
        if target in self.branches:
            return self._switch_to_branch(target)
        remote_name = f"{REMOTE}/{target}"
        if remote_name in self.tracking:
            out = self._create_branch_and_switch(target, remote_name)
            return out + f"\nbranch '{target}' set up to track '{remote_name}'."
        cid = self._try_resolve(target)
        if cid is None:
            raise GitError(f"error: pathspec '{target}' did not match any file(s) known to git")
        return self._switch_detached(cid)

    def _switch_cmd(self, args: list[str]) -> str:
        if args and args[0] in ("-c", "-C", "--create"):
            if len(args) < 2:
                raise GitError("fatal: missing branch name")
            return self._create_branch_and_switch(args[1], args[2] if len(args) > 2 else None)
        if args and args[0] in ("--detach", "-d"):
            return self._switch_detached(self.resolve(args[1] if len(args) > 1 else "HEAD"))
        if len(args) != 1:
            raise GitError("usage: git switch [-c] <branch>")
        if args[0] not in self.branches and f"{REMOTE}/{args[0]}" not in self.tracking:
            raise GitError(f"fatal: invalid reference: {args[0]}")
        return self._switch_target(args[0])

    def _checkout(self, args: list[str]) -> str:
        if args and args[0] in ("-b", "-B"):
            if len(args) < 2:
                raise GitError("fatal: missing branch name")
            return self._create_branch_and_switch(args[1], args[2] if len(args) > 2 else None)
        if "--" in args:
            k = args.index("--")
            before, paths = args[:k], args[k + 1:]
            if not paths:
                raise GitError("fatal: you must specify path(s) after '--'")
            if before:
                return self._restore(["--staged", "--worktree", f"--source={before[0]}", *paths])
            return self._restore(paths)
        if not args:
            raise GitError("usage: git checkout [-b] <branch|commit> | -- <file>")
        if len(args) == 1:
            target = args[0]
            known = target in self.branches or f"{REMOTE}/{target}" in self.tracking or self._try_resolve(target)
            if not known and norm_path(target) in set(self.index) | set(self.wd):
                return self._restore([target])
            return self._switch_target(target)
        raise GitError("usage: git checkout [-b] <branch|commit> | -- <file>")

    # ---- restore
    def _restore(self, args: list[str]) -> str:
        staged = worktree = False
        source: str | None = None
        paths: list[str] = []
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("--staged", "-S"):
                staged = True
            elif a in ("--worktree", "-W"):
                worktree = True
            elif a in ("--source", "-s"):
                if i + 1 >= len(args):
                    raise GitError("error: option 'source' requires a value")
                source = args[i + 1]
                i += 1
            elif a.startswith("--source="):
                source = a.split("=", 1)[1]
            elif a == "--":
                pass
            elif a.startswith("-"):
                raise GitError(f"error: unknown option '{a}' for 'git restore'")
            else:
                paths.append(a)
            i += 1
        if not paths:
            raise GitError("fatal: you must specify path(s) to restore")
        if not staged:
            worktree = True

        src_tree = self.commits[self.resolve(source)].tree if source else None
        index_source = src_tree if src_tree is not None else self.head_tree
        wd_source = src_tree if src_tree is not None else (self.head_tree if staged else self.index)

        whole = any(norm_path(p) == "." for p in paths)
        universe = set(self.index) | set(index_source if staged else wd_source)
        targets = self._expand(paths, universe) if not whole else sorted(universe)
        for path in targets:
            if path in self.conflicts:
                if whole:
                    continue
                raise GitError(f"error: path '{path}' is unmerged")
            if staged:
                if path in index_source:
                    self.index[path] = index_source[path]
                else:
                    self.index.pop(path, None)
            if worktree:
                if path in wd_source:
                    self.wd[path] = wd_source[path]
                else:
                    self.wd.pop(path, None)
        return ""

    # ---- reset
    def _reset(self, args: list[str]) -> str:
        mode = "mixed"
        positional: list[str] = []
        explicit_paths: list[str] | None = None
        for k, a in enumerate(args):
            if a == "--":
                explicit_paths = args[k + 1:]
                break
            if a in ("--soft", "--mixed", "--hard"):
                mode = a[2:]
            elif a in ("-q", "--quiet"):
                pass
            elif a.startswith("-"):
                raise GitError(f"error: unknown option '{a}' for 'git reset'")
            else:
                positional.append(a)

        target = self.head_id
        paths: list[str] = list(explicit_paths or [])
        if positional:
            resolved = self._try_resolve(positional[0])
            if resolved is not None:
                target = resolved
                paths += positional[1:]
            elif explicit_paths is None:
                paths += positional
                bad = [p for p in paths if norm_path(p) != "." and norm_path(p) not in set(self.index) | set(self.head_tree)]
                if bad:
                    raise GitError(
                        f"fatal: ambiguous argument '{bad[0]}': unknown revision or path not in the working tree."
                    )
            else:
                self.resolve(positional[0])  # raises a proper error

        if paths:
            if mode != "mixed":
                raise GitError(f"fatal: Cannot do {mode} reset with paths.")
            tree = self.commits[target].tree
            names = self._expand(paths, set(self.index) | set(tree))
            for path in names:
                if path in tree:
                    self.index[path] = tree[path]
                else:
                    self.index.pop(path, None)
                self.conflicts.discard(path)
            return self._unstaged_report()

        if mode == "soft" and (self.pending or self.conflicts):
            raise GitError("fatal: Cannot do a soft reset in the middle of a merge.")

        tree = self.commits[target].tree
        previous_tracked = set(self.index)
        self._set_head(target, f"reset: moving to {positional[0] if positional else 'HEAD'}")
        out = ""
        if mode in ("mixed", "hard"):
            self.index = dict(tree)
            self.conflicts.clear()
            self.pending = None
        if mode == "hard":
            for path in previous_tracked - set(tree):
                self.wd.pop(path, None)
            self.wd.update(tree)
            out = f"HEAD is now at {self.describe(target)}"
        elif mode == "mixed":
            out = self._unstaged_report()
        return out

    def _unstaged_report(self) -> str:
        changes = self.unstaged_changes()
        if not changes:
            return ""
        letters = {"modified": "M", "deleted": "D"}
        return "Unstaged changes after reset:\n" + "\n".join(f"{letters[s]}\t{p}" for p, s in changes.items())

    # ---- revert
    def _revert(self, args: list[str]) -> str:
        if "--abort" in args:
            if not (self.pending and self.pending["kind"] == "revert"):
                raise GitError("error: no revert in progress")
            self._abort_pending()
            return "Revert aborted - everything is back to HEAD."
        parent_number = 1
        no_commit = False
        revs: list[str] = []
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("-m", "--mainline"):
                parent_number = int(args[i + 1])
                i += 1
            elif a in ("-n", "--no-commit"):
                no_commit = True
            elif a in ("--no-edit", "-e", "--edit"):
                pass
            elif a.startswith("-"):
                raise GitError(f"error: unknown option '{a}' for 'git revert'")
            else:
                revs.append(a)
            i += 1
        if not revs:
            raise GitError("usage: git revert [-n] [-m <parent>] <commit>...")

        self._require_clean("revert")
        # Like git, resolve every revision up front: HEAD moves while we commit.
        targets = [self.commits[self.resolve(r)] for r in revs]
        for target in targets:
            if len(target.parents) > 1 and "-m" not in args and "--mainline" not in args:
                raise GitError(
                    f"error: commit {target.id} is a merge but no -m option was given.\n"
                    "hint: use 'git revert -m 1 <commit>' to undo the merge relative to its first parent."
                )
        out = [self._revert_one(t, parent_number, no_commit) for t in targets]
        return "\n".join(part for part in out if part)

    def _revert_one(self, target: Commit, parent_number: int, no_commit: bool) -> str:
        if target.parents and not 1 <= parent_number <= len(target.parents):
            raise GitError(f"error: commit {target.id} does not have parent {parent_number}")
        parent_tree = self.commits[target.parents[parent_number - 1]].tree if target.parents else {}
        # "ours" is the index: it equals HEAD, or HEAD plus earlier `revert -n` steps.
        ours = dict(self.index)
        result = merge_trees(target.tree, ours, parent_tree, f"parent of {target.id}")
        message = f'Revert "{target.message}"'

        if result.worktree == ours and not result.conflicts:
            raise GitError(f"error: nothing to revert - the changes of {target.id} are already undone.")
        self._guard_untracked(p for p in result.worktree if p not in self.index)
        self._apply_merge_result(result)

        if result.conflicts:
            self.pending = {"kind": "revert", "other": None, "message": message}
            self.conflicts = set(result.conflicts)
            return (
                "\n".join(result.notes)
                + f"\nerror: could not revert {target.id}... {target.message}\n"
                "hint: Resolve the conflicts, mark them with 'git add <file>', then run 'git commit'.\n"
                "hint: Or run 'git revert --abort' to give up."
            ).strip()
        if no_commit:
            return f"Reverted {target.id} in the index and working directory (not committed). Run 'git commit' to finish."
        new = self._new_commit(message, [self.head_id], self.index)
        self._set_head(new.id, f"revert: {message}")
        return f"[{self.head_branch or 'detached HEAD'} {new.id}] {message}"

    def _apply_merge_result(self, result: MergeResult) -> None:
        touched = set(self.head_tree) | set(self.index) | set(result.worktree)
        for path in touched:
            if path in result.worktree:
                self.wd[path] = result.worktree[path]
            else:
                self.wd.pop(path, None)
            if path in result.index:
                self.index[path] = result.index[path]
            else:
                self.index.pop(path, None)

    def _abort_pending(self) -> None:
        tree = self.head_tree
        for path in set(self.index) - set(tree):
            self.wd.pop(path, None)
        self.index = dict(tree)
        self.wd.update(tree)
        self.conflicts.clear()
        self.pending = None

    # ---- merge
    def _merge_cmd(self, args: list[str]) -> str:
        if "--abort" in args:
            if not (self.pending and self.pending["kind"] == "merge"):
                raise GitError("fatal: There is no merge to abort (MERGE_HEAD missing).")
            self._abort_pending()
            return "Merge aborted - everything is back to HEAD."
        no_ff = "--no-ff" in args
        ff_only = "--ff-only" in args
        message = None
        names: list[str] = []
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("-m", "--message"):
                message = args[i + 1]
                i += 1
            elif a.startswith("-"):
                if a not in ("--no-ff", "--ff-only", "--no-edit", "--ff"):
                    raise GitError(f"error: unknown option '{a}' for 'git merge'")
            else:
                names.append(a)
            i += 1
        if len(names) != 1:
            raise GitError("usage: git merge [--no-ff] <branch>")
        other = self.resolve(names[0])
        return self._merge(other, names[0], no_ff, ff_only, message)

    def _merge(self, other: str, label: str, no_ff: bool, ff_only: bool, message: str | None) -> str:
        self._require_clean("merge")
        head = self.head_id
        if other in self.ancestors(head):
            return "Already up to date."

        if head in self.ancestors(other) and not no_ff:
            new_tree = self.commits[other].tree
            self._guard_untracked(p for p in new_tree if p not in self.head_tree and p not in self.index)
            old_tree = self.head_tree
            for path in set(old_tree) | set(new_tree):
                if path in new_tree:
                    self.index[path] = self.wd[path] = new_tree[path]
                else:
                    self.index.pop(path, None)
                    self.wd.pop(path, None)
            self._set_head(other, f"merge {label}: Fast-forward")
            changed = len(diff_trees(old_tree, new_tree))
            return f"Updating {head}..{other}\nFast-forward\n {plural(changed, 'file')} changed"

        if ff_only:
            raise GitError("fatal: Not possible to fast-forward, aborting.")

        base = self.merge_base(head, other)
        base_tree = self.commits[base].tree if base else {}
        result = merge_trees(base_tree, self.head_tree, self.commits[other].tree, label)
        self._guard_untracked(p for p in result.worktree if p not in self.index)

        is_remote = label in self.tracking
        text = message or (
            f"Merge remote-tracking branch '{label}'" if is_remote else f"Merge branch '{label}'"
        )
        if not message and self.head_branch and self.head_branch != "main":
            text += f" into {self.head_branch}"

        self._apply_merge_result(result)
        notes = "\n".join(result.notes)
        if result.conflicts:
            self.pending = {"kind": "merge", "other": other, "message": text}
            self.conflicts = set(result.conflicts)
            return (
                notes + "\nAutomatic merge failed; fix conflicts and then commit the result.\n"
                "hint: edit the file(s), 'git add' them, then 'git commit' (or 'git merge --abort')."
            ).strip()

        commit = self._new_commit(text, [head, other], self.index)
        self._set_head(commit.id, f"merge {label}: Merge made by the 'ort' strategy.")
        changed = len(diff_trees(self.commits[head].tree, commit.tree))
        return (notes + f"\nMerge made by the 'ort' strategy. [{commit.id}]\n {plural(changed, 'file')} changed").strip()

    # ---- remote: push / fetch / pull / remote
    def _remote(self, args: list[str]) -> str:
        if "-v" in args:
            return f"{REMOTE}\t<sandbox remote> (fetch)\n{REMOTE}\t<sandbox remote> (push)"
        return REMOTE

    def _push(self, args: list[str]) -> str:
        force = any(a in ("-f", "--force", "--force-with-lease") for a in args)
        set_up = any(a in ("-u", "--set-upstream") for a in args)
        positional = [a for a in args if not a.startswith("-")]
        if positional and positional[0] != REMOTE:
            raise GitError(f"fatal: '{positional[0]}' does not appear to be a git repository")
        if len(positional) > 1:
            branch = positional[1].split(":")[-1]
            source_name = positional[1].split(":")[0]
            if source_name == "HEAD":  # `git push -u origin HEAD`
                if not self.head_branch:
                    raise GitError("fatal: You are not currently on a branch.")
                source_name = self.head_branch
                if branch == "HEAD":
                    branch = self.head_branch
        else:
            if not self.head_branch:
                raise GitError("fatal: You are not currently on a branch.")
            branch = source_name = self.head_branch
        if source_name not in self.branches:
            raise GitError(f"error: src refspec {source_name} does not match any")
        local = self.branches[source_name]
        remote = self.remote_branches.get(branch)

        if set_up:
            self.upstream[source_name] = f"{REMOTE}/{branch}"
        if remote == local:
            self.tracking[f"{REMOTE}/{branch}"] = local
            return "Everything up-to-date"
        if remote and remote not in self.ancestors(local) and not force:
            return (
                f" ! [rejected]        {source_name} -> {branch} (non-fast-forward)\n"
                f"error: failed to push some refs to '{REMOTE}'\n"
                "hint: Updates were rejected because the remote contains work you do not have locally.\n"
                "hint: Integrate it first with 'git pull', or overwrite it with 'git push --force'."
            )
        forced = bool(remote and remote not in self.ancestors(local))
        self.remote_branches[branch] = local
        self.tracking[f"{REMOTE}/{branch}"] = local
        if remote is None:
            return f"To {REMOTE}\n * [new branch]      {source_name} -> {branch}"
        arrow = "..." if forced else ".."
        suffix = " (forced update)" if forced else ""
        sign = "+" if forced else " "
        return f"To {REMOTE}\n {sign} {remote}{arrow}{local}  {source_name} -> {branch}{suffix}"

    def _fetch(self) -> str:
        out: list[str] = []
        for branch, tip in self.remote_branches.items():
            name = f"{REMOTE}/{branch}"
            old = self.tracking.get(name)
            if old == tip:
                continue
            self.tracking[name] = tip
            out.append(
                f" * [new branch]      {branch} -> {name}" if old is None
                else f"   {old}..{tip}  {branch} -> {name}"
            )
        return f"From {REMOTE}\n" + "\n".join(out) if out else ""

    def _fetch_cmd(self, args: list[str]) -> str:
        positional = [a for a in args if not a.startswith("-")]
        if positional and positional[0] != REMOTE:
            raise GitError(f"fatal: '{positional[0]}' does not appear to be a git repository")
        return self._fetch()

    def _pull(self, args: list[str]) -> str:
        if "--rebase" in args or "-r" in args:
            raise GitError("error: 'git pull --rebase' is not simulated here - use plain 'git pull' (fetch + merge).")
        positional = [a for a in args if not a.startswith("-")]
        if positional and positional[0] != REMOTE:
            raise GitError(f"fatal: '{positional[0]}' does not appear to be a git repository")
        if not self.head_branch:
            raise GitError("fatal: You are not currently on a branch.")
        branch = positional[1] if len(positional) > 1 else self.head_branch
        if branch not in self.remote_branches:
            raise GitError(f"fatal: couldn't find remote ref {branch}")
        self._require_clean("pull")
        fetched = self._fetch()
        merged = self._merge(
            self.tracking[f"{REMOTE}/{branch}"],
            f"{REMOTE}/{branch}",
            "--no-ff" in args,
            "--ff-only" in args,
            None,
        )
        return "\n".join(part for part in (fetched, merged) if part)

    # ---------------------------------------------------------------- views
    def snapshot(self) -> dict:
        """Everything the UI needs to draw all four areas."""
        head_tree = self.head_tree
        working = []
        for path in sorted(set(self.wd) | set(self.index)):
            if path in self.conflicts:
                status = "conflict"
            elif path not in self.index:
                status = "untracked"
            elif path not in self.wd:
                status = "deleted"
            elif self.wd[path] != self.index[path]:
                status = "modified"
            else:
                status = "clean"
            working.append({"name": path, "content": self.wd.get(path, ""), "status": status})

        staging = []
        for path in sorted(set(self.index) | set(head_tree)):
            if path not in self.index:
                status = "deleted"
            elif path not in head_tree:
                status = "added"
            elif self.index[path] != head_tree[path]:
                status = "modified"
            else:
                status = "clean"
            if path in self.conflicts:
                status = "conflict"
            staging.append({"name": path, "content": self.index.get(path, ""), "status": status})

        local_refs = [(name, "branch", cid) for name, cid in self.branches.items()]
        local_refs += [(name, "remote", cid) for name, cid in self.tracking.items()]
        local_refs.append(("HEAD", "head", self.head_id))
        remote_refs = [(name, "branch", cid) for name, cid in self.remote_branches.items()]

        return {
            "head": {"id": self.head_id, "branch": self.head_branch, "detached": self.head_branch is None},
            "working": working,
            "staging": staging,
            "local": self._graph(local_refs, self.branches.get("main"), local=True),
            "remote": self._graph(remote_refs, self.remote_branches.get("main"), local=False),
            "conflicts": sorted(self.conflicts),
            "pending": self.pending["kind"] if self.pending else None,
            "reflog": [
                {"id": cid, "reason": reason}
                for cid, reason in reversed(self.reflog[-12:])
            ],
        }

    def _graph(self, refs: list[tuple[str, str, str]], prefer: str | None, local: bool) -> list[dict]:
        ids: set[str] = set()
        for _, _, cid in refs:
            ids |= self.ancestors(cid)
        ordered = sorted(ids, key=lambda c: self.commits[c].seq)

        lane_of: dict[str, int] = {}
        taken: set[str] = set()
        if prefer:
            cursor: str | None = prefer
            while cursor:
                lane_of[cursor] = 0
                parents = self.commits[cursor].parents
                if parents:
                    taken.add(parents[0])
                cursor = parents[0] if parents else None
        next_lane = 1
        for cid in ordered:
            if cid in lane_of:
                continue
            parents = self.commits[cid].parents
            if parents and parents[0] not in taken:
                lane_of[cid] = lane_of[parents[0]]
                taken.add(parents[0])
            else:
                lane_of[cid] = next_lane
                next_lane += 1

        labels: dict[str, list[dict]] = {}
        for name, kind, cid in refs:
            entry = {"name": name, "type": kind}
            if local and kind == "branch" and name == self.head_branch:
                entry["current"] = True
            labels.setdefault(cid, []).append(entry)

        nodes = []
        for column, cid in enumerate(ordered):
            commit = self.commits[cid]
            nodes.append(
                {
                    "id": cid,
                    "message": commit.message,
                    "author": commit.author,
                    "parents": commit.parents,
                    "lane": lane_of[cid],
                    "col": column,
                    "refs": labels.get(cid, []),
                    "tree": commit.tree,
                }
            )
        return nodes
