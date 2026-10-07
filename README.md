# Git Sandbox

A visual playground for practising `merge`, `revert` and `reset` and seeing where each change lives:
**working directory -> staging area -> local repository -> remote repository**.

## Run

```powershell
uv run git-sandbox-app        # or: .venv\Scripts\git-sandbox-app
```

Then open <http://127.0.0.1:8000>.

## How it works

- `engine.py` - a deterministic in-memory simulation of git (add, commit, reset, revert, merge with real
  three-way/conflict handling, branches, checkout/switch/restore, fetch/pull/push, reflog, diff, log).
- `exercises.py` - guided scenarios with goal checks, plus the single-user session.
- `main.py` - FastAPI app (`/api/command`, `/api/state`, `/api/exercise/{id}`, `/api/exercises`).
- `index.html` - UI: the four areas, a terminal, and SVG commit graphs for local and remote.

Type `help` in the terminal for the supported commands. `echo "text" > file` edits files and
`teammate <file> "text"` makes a teammate push to `origin/main`.

## Tests

```powershell
.venv\Scripts\python tests\test_engine.py
```
