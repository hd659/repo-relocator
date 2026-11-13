import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from repo_relocator.cli import main
from repo_relocator.core import RelocateError, Rewriter, check_pair, claude_dir_name, pi_dir_name, rewrite_json
from repo_relocator.demo import build_fixture
from repo_relocator.ops import apply, list_backups, rollback
from repo_relocator.scanners import Env, scan
from repo_relocator.verify import verify

URL = "https://example.com/me/new-app.git"


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_RELOCATOR_HOME", str(tmp_path / "state"))
    home, old, new = build_fixture(tmp_path)
    env = Env.build(home=home, vscode_dirs=[home / "vscode/User"], move=True, remote_url=URL)
    return home, old, new, env


def snapshot(home: Path) -> dict:
    out = {}
    for p in sorted(home.rglob("*")):
        if p.is_file() and ".git/objects" not in str(p) and p.suffix != ".sqlite" and p.suffix != ".vscdb":
            out[str(p.relative_to(home))] = p.read_bytes()
    for db in list(home.rglob("*.sqlite")) + list(home.rglob("*.vscdb")):
        con = sqlite3.connect(db)
        out[str(db)] = sorted(map(str, con.execute("SELECT * FROM " + (
            "threads" if db.suffix == ".sqlite" else "ItemTable")).fetchall()))
        con.close()
    return out


def test_encoders_match_tools():
    assert claude_dir_name("/Users/me/my_app.v2") == "-Users-me-my-app-v2"
    assert claude_dir_name("/" + "a" * 300) is None
    assert pi_dir_name("/Users/me/app") == "--Users-me-app--"


def test_rewriter_respects_path_boundaries():
    rw = Rewriter("/x/app", "/y/app2")
    text, n = rw.sub('"/x/app" /x/app/src /x/app-extra /x/apple file:///x/app')
    assert n == 3
    assert "/x/app-extra" in text and "/x/apple" in text and "file:///y/app2" in text


def test_rewriter_handles_percent_encoded_uri():
    rw = Rewriter("/x/my app", "/x/new app")
    text, n = rw.sub("file:///x/my%20app/a.py")
    assert n == 1 and text == "file:///x/new%20app/a.py"


def test_rewrite_json_renames_keys():
    obj, n = rewrite_json({"projects": {"/x/app": 1, "/x/app-2": 2}}, Rewriter("/x/app", "/y/app"), [])
    assert n == 1 and obj == {"projects": {"/x/app-2": 2, "/y/app": 1}}


def test_check_pair_guards():
    for old, new in (("/x/a", "/x/a"), ("/", "/x"), ("/x/a", "/x/a/b"), ('/x/"a', "/x/b")):
        with pytest.raises(RelocateError):
            check_pair(old, new)


def test_plan_finds_everything_and_skips_decoy(box):
    home, old, new, env = box
    plan = scan(old, new, env)
    sources = {c.source for c in plan.changes}
    assert {"Claude Code", "Codex", "Pi", "Git", "项目目录", "VS Code 最近项目",
            "VS Code 工作区缓存", "VS Code 工作区文件"} <= sources
    renames = [c for c in plan.changes if c.kind == "rename"]
    assert len(renames) == 3 and all("extra" not in c.path for c in renames)
    codex = [c.path for c in plan.changes if c.source == "Codex"]
    assert not any("rollout-b" in p for p in codex)
    assert "Claude Code" in plan.render()


def test_plan_is_read_only(box):
    home, old, new, env = box
    before = snapshot(home)
    scan(old, new, env)
    assert snapshot(home) == before


def test_apply_then_verify_passes(box):
    home, old, new, env = box
    plan = scan(old, new, env)
    j = apply(plan, home)
    assert j.data["status"] == "applied"
    assert verify(plan, env).ok
    assert not os.path.exists(old) and os.path.isdir(new)
    cp = home / ".claude/projects"
    assert (cp / claude_dir_name(new) / "s1.jsonl").is_file()
    assert (cp / claude_dir_name(new + "/sub")).is_dir()
    assert (home / ".pi/agent/sessions" / pi_dir_name(new) / "p1.jsonl").is_file()
    cfg = json.loads((home / ".claude.json").read_text())
    assert new in cfg["projects"] and old + "-extra" in cfg["projects"]
    con = sqlite3.connect(home / ".codex/state_5.sqlite")
    assert dict(con.execute("SELECT id, cwd FROM threads")) == {"a": new, "b": old + "-extra"}
    url = subprocess.run(["git", "-C", new, "remote", "get-url", "origin"], capture_output=True, text=True)
    assert url.stdout.strip() == URL
    assert "old-app-extra" in (home / ".codex/sessions/2026/11/13/rollout-b.jsonl").read_text()


def test_rollback_restores_exact_state(box):
    home, old, new, env = box
    before = snapshot(home)
    j = apply(scan(old, new, env), home)
    errors = rollback(j)
    assert errors == [] and snapshot(home) == before
    assert list_backups(home)[-1]["status"] == "rolled_back"


def test_failure_midway_auto_rolls_back(box, monkeypatch):
    home, old, new, env = box
    before = snapshot(home)
    plan = scan(old, new, env)
    import repo_relocator.ops as ops
    real = ops._rename
    calls = {"n": 0}

    def flaky(c, j):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full (simulated)")
        real(c, j)

    monkeypatch.setattr(ops, "_rename", flaky)
    with pytest.raises(RelocateError):
        apply(plan, home)
    assert snapshot(home) == before
    assert list_backups(home)[-1]["status"] == "rolled_back"


def test_verify_detects_half_migration(box):
    home, old, new, env = box
    plan = scan(old, new, env)
    apply(plan, home)
    pi = home / ".pi/agent/sessions" / pi_dir_name(new) / "p1.jsonl"
    pi.write_text(pi.read_text().replace(new, old))  # simulate a tool writing the old path back
    rep = verify(plan, env)
    assert not rep.ok and "仍含旧路径" in rep.render()


def test_cli_end_to_end(box, capsys):
    home, old, new, _ = box
    common = ["--home", str(home), "--vscode-dir", str(home / "vscode/User")]
    assert main(["plan", old, new, "--move", *common]) == 0
    assert "迁移计划" in capsys.readouterr().out
    assert main(["apply", old, new, "--move", "-y", *common]) == 0
    out = capsys.readouterr().out
    assert "全部通过" in out
    assert main(["verify", *common]) == 0
    assert main(["list", *common]) == 0 and "verified" in capsys.readouterr().out
    assert main(["rollback", "-y", *common]) == 0
    assert os.path.isdir(old) and not os.path.exists(new)


def test_cli_declined_confirmation_changes_nothing(box, monkeypatch):
    home, old, new, _ = box
    before = snapshot(home)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    rc = main(["apply", old, new, "--move", "--home", str(home), "--vscode-dir", str(home / "vscode/User")])
    assert rc == 1 and snapshot(home) == before
