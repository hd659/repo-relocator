# repo-relocator

One-shot project rename/move. Sweeps every stale absolute path that IDEs and AI coding agents
store about a project, shows you a plan, backs everything up, applies it in one go, verifies that
old sessions still resolve, and rolls back with one command if anything is off.

No dependencies beyond the Python 3.9+ standard library.

## Why

Renaming `~/code/old-app` to `~/code/new-app` silently orphans:

| Where | What is stored |
|---|---|
| Claude Code | `~/.claude/projects/<cwd with non-alnum -> "-">/*.jsonl` (+ `cwd` inside), `~/.claude.json` projects, `history.jsonl` |
| Codex | `~/.codex/sessions/**/rollout-*.jsonl` `session_meta.cwd`, `state_*.sqlite` `threads.cwd`, `config.toml` trusted projects |
| Pi | `~/.pi/agent/sessions/--<cwd>--/*.jsonl` session header `cwd` |
| VS Code / Cursor | `state.vscdb` recently opened, `storage.json`, `workspaceStorage/*/workspace.json`, `*.code-workspace` |
| Git | `.git/config`, worktree `gitdir`, optionally `origin` URL when the remote repo was renamed |

Usually you end up with half of these updated and half not. This tool does all of them or none.

## Usage

```bash
pip install -e .            # or: python3 -m repo_relocator ...

# 1. read-only plan (nothing is written)
repo-relocator plan ~/code/old-app ~/code/new-app --move --detail

# 2. confirm -> backup -> apply -> verify (asks again before rolling back)
repo-relocator apply ~/code/old-app ~/code/new-app --move \
    --remote-url https://example.com/me/new-app.git

# 3. later
repo-relocator list
repo-relocator verify            # re-check the latest migration
repo-relocator rollback [ID]     # restore exactly what was there before

# try the whole flow in a throwaway sandbox
python3 -m repo_relocator demo
```

Options: `--move` also moves the project directory (otherwise `mv` it yourself first);
`--deep` also rewrites mentions of the old path inside sessions that belong to other projects;
`--home` / `--vscode-dir` / `--search-dir` point at alternative locations;
`-y` skips prompts (a failed verification then auto-rolls back).

Close VS Code and any running agent sessions in that project before `apply`, otherwise they may
write the old path back from memory.

## How it stays safe

- **Path-boundary matching**: `/x/app` matches `/x/app` and `/x/app/src`, never `/x/app-extra`
  or `/x/apple`. `file://` percent-encoded forms are handled too.
- **Ownership check**: Claude/Pi session dir names are lossy (`/a/b-c` and `/a/b/c` encode the
  same), so a dir is only renamed when the `cwd` stored inside it is under the old path.
- **Journaled apply**: each step is written to `~/.repo-relocator/backups/<id>/manifest.json`
  (original file copies, original sqlite row values, rename/merge lists) before it runs. Any
  exception mid-way triggers an automatic reverse-order rollback.
- **Atomic writes**: files are rewritten via temp file + `os.replace`.

## What "verify" means

It does not launch the GUIs. It checks the exact data each tool uses to find a session:
no planned file/db still contains the old path, JSON/JSONL still parses, every Claude/Pi session
dir name equals the encoding of the `cwd` stored inside it and that `cwd` exists, Codex rollouts
point at the new path, and the old directory is gone (no half-migrated state).

## Limits

- Claude Code hashes encoded names longer than 200 chars; such dirs are reported, not renamed.
- Paths containing quotes, backslashes or control characters are refused.
- macOS/Linux only.

## Tests

```bash
python3 -m pytest -q
```
