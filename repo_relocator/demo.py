"""Build a fake home with Claude Code / Codex / Pi / VS Code state and run the full flow in it."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from .core import claude_dir_name, pi_dir_name


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def build_fixture(root: Path) -> tuple[Path, str, str]:
    """Return (home, old, new). Contains a decoy project whose encoded name collides by prefix."""
    home = root / "home"
    old = str(home / "code" / "old-app")
    new = str(home / "code" / "new-app")
    decoy = str(home / "code" / "old-app-extra")
    for p in (old, decoy):
        Path(p).mkdir(parents=True)
    Path(old, "main.py").write_text("print('hi')\n")
    Path(old, "sub").mkdir()
    subprocess.run(["git", "init", "-q", old], check=True)
    subprocess.run(["git", "-C", old, "remote", "add", "origin", "https://example.com/me/old-app.git"], check=True)

    cp = home / ".claude/projects"
    _jsonl(cp / claude_dir_name(old) / "s1.jsonl", [
        {"type": "user", "cwd": old, "message": {"content": f"edit {old}/main.py"}},
        {"type": "assistant", "cwd": old, "message": {"content": "done"}}])
    _jsonl(cp / claude_dir_name(old + "/sub") / "s2.jsonl", [{"type": "user", "cwd": old + "/sub"}])
    _jsonl(cp / claude_dir_name(decoy) / "s3.jsonl", [{"type": "user", "cwd": decoy}])
    (home / ".claude.json").write_text(json.dumps(
        {"projects": {old: {"allowedTools": []}, decoy: {"allowedTools": []}}}, indent=2))
    _jsonl(home / ".claude/history.jsonl", [{"display": "hi", "project": old}, {"display": "x", "project": decoy}])

    _jsonl(home / ".codex/sessions/2026/11/13/rollout-a.jsonl", [
        {"type": "session_meta", "payload": {"id": "a", "cwd": old}},
        {"type": "event", "payload": {"text": f"ls {old}"}}])
    _jsonl(home / ".codex/sessions/2026/11/13/rollout-b.jsonl", [
        {"type": "session_meta", "payload": {"id": "b", "cwd": decoy}}])
    con = sqlite3.connect(home / ".codex/state_5.sqlite")
    con.execute("CREATE TABLE threads (id TEXT, cwd TEXT, title TEXT)")
    con.executemany("INSERT INTO threads VALUES (?,?,?)", [("a", old, "t"), ("b", decoy, "t")])
    con.commit()
    con.close()
    (home / ".codex/config.toml").write_text(f'[projects."{old}"]\ntrust_level = "trusted"\n')

    _jsonl(home / ".pi/agent/sessions" / pi_dir_name(old) / "p1.jsonl", [
        {"type": "session", "version": 3, "id": "p1", "cwd": old}])

    vs = home / "vscode/User"
    (vs / "globalStorage").mkdir(parents=True)
    (vs / "globalStorage/storage.json").write_text(json.dumps(
        {"backupWorkspaces": {"folders": [{"folderUri": "file://" + old}]}}))
    con = sqlite3.connect(vs / "globalStorage/state.vscdb")
    con.execute("CREATE TABLE ItemTable (key TEXT UNIQUE, value BLOB)")
    con.execute("INSERT INTO ItemTable VALUES (?,?)", ("history.recentlyOpenedPathsList", json.dumps(
        {"entries": [{"folderUri": "file://" + old}, {"folderUri": "file://" + decoy}]})))
    con.commit()
    con.close()
    (vs / "workspaceStorage/abc").mkdir(parents=True)
    (vs / "workspaceStorage/abc/workspace.json").write_text(json.dumps({"folder": "file://" + old}))
    Path(home / "code" / "suite.code-workspace").write_text(json.dumps(
        {"folders": [{"path": old}, {"path": decoy}]}, indent=2))
    return home, old, new


def run_demo(directory: str | None = None) -> int:
    from .cli import main
    root = Path(directory or tempfile.mkdtemp(prefix="rr-demo-"))
    root.mkdir(parents=True, exist_ok=True)
    home, old, new = build_fixture(root)
    os.environ["REPO_RELOCATOR_HOME"] = str(root / "state")
    common = ["--home", str(home), "--vscode-dir", str(home / "vscode/User")]
    print(f"沙盒: {root}\n\n=== 1. 扫描计划 ===")
    main(["plan", old, new, "--move", "--remote-url", "https://example.com/me/new-app.git", *common])
    print("\n=== 2. 备份 + 应用 + 验证 ===")
    rc = main(["apply", old, new, "--move", "--remote-url", "https://example.com/me/new-app.git", "-y", *common])
    print("\n=== 3. 一键回滚 ===")
    main(["rollback", "-y", *common])
    print(f"\n回滚后旧目录存在: {os.path.isdir(old)}, 新目录存在: {os.path.isdir(new)}")
    return rc
