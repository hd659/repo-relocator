"""Read-only scanners: find every stored reference to the old path. Nothing is modified here."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .core import Change, Plan, Rewriter, check_pair, claude_dir_name, is_under, pi_dir_name

_CWD_RX = re.compile(rb'"cwd"\s*:\s*"([^"]+)"')


def default_vscode_dirs(home: Path) -> list[Path]:
    base = home / "Library/Application Support" if sys.platform == "darwin" else home / ".config"
    return [base / name / "User" for name in ("Code", "Code - Insiders", "Cursor")]


@dataclass
class Env:
    home: Path
    vscode_dirs: list[Path] = field(default_factory=list)
    search_dirs: list[Path] = field(default_factory=list)
    move: bool = False
    deep: bool = False  # also rewrite mentions in sessions that belong to other projects
    remote_url: str | None = None

    @classmethod
    def build(cls, home=None, vscode_dirs=None, **kw) -> "Env":
        h = Path(home or os.path.expanduser("~"))
        dirs = [Path(d) for d in vscode_dirs] if vscode_dirs else default_vscode_dirs(h)
        return cls(home=h, vscode_dirs=dirs, **kw)


def first_cwd(directory: Path, limit: int = 5) -> str | None:
    for f in sorted(directory.rglob("*.jsonl"))[:limit]:
        with open(f, "rb") as fh:
            m = _CWD_RX.search(fh.read(65536))
        if m:
            return m.group(1).decode("utf-8", "replace")
    return None


class Scanner:
    def __init__(self, old: str, new: str, env: Env):
        self.old, self.new = check_pair(old, new)
        self.env, self.rw = env, Rewriter(self.old, self.new)
        self.plan = Plan(self.old, self.new)
        self.seen: set[str] = set()

    # ---- generic helpers -------------------------------------------------
    def text(self, path: Path, source: str, kind: str = "text") -> None:
        key = str(path)
        if key in self.seen or not path.is_file():
            return
        try:
            data = path.read_bytes()
            if not self.rw.quick(data):
                return
            n = self.rw.count(data.decode("utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            self.plan.warnings.append(f"跳过无法读取的文件 {path}: {exc}")
            return
        if n:
            self.seen.add(key)
            self.plan.changes.append(Change(kind, source, key, n))

    def sqlite(self, db: Path, table: str, column: str, source: str) -> None:
        if not db.is_file():
            return
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = []
            for needle in self.rw.map:
                rows += con.execute(f"SELECT rowid, {column} FROM {table} WHERE instr({column}, ?) > 0",
                                    (needle,)).fetchall()
            con.close()
        except sqlite3.Error as exc:
            self.plan.warnings.append(f"跳过数据库 {db} ({table}): {exc}")
            return
        hits = {}
        for rowid, val in rows:
            text = val.decode("utf-8") if isinstance(val, bytes) else str(val)
            if self.rw.count(text):
                hits[rowid] = self.rw.count(text)
        if hits:
            self.plan.changes.append(Change("sqlite", source, str(db), sum(hits.values()),
                                            extra={"table": table, "column": column}))

    def rename(self, src: Path, dest_name: str | None, source: str) -> None:
        if dest_name is None:
            self.plan.warnings.append(f"{source}: 新路径编码后过长，无法推算目录名，未重命名 {src.name}")
        elif dest_name != src.name:
            dest = src.parent / dest_name
            note = "目标已存在, 将合并" if dest.exists() else ""
            self.plan.changes.append(Change("rename", source, str(src), dest=str(dest), detail=note))

    # ---- individual sources ----------------------------------------------
    def project(self) -> None:
        old_ok, new_ok = os.path.isdir(self.old), os.path.isdir(self.new)
        if self.env.move and old_ok and not new_ok:
            self.plan.changes.append(Change("move", "项目目录", self.old, dest=self.new,
                                            detail="移动项目目录"))
        elif old_ok and not new_ok:
            self.plan.warnings.append("旧目录还在、新目录不存在：加 --move 让工具顺带移动，或先自己 mv")
        elif not old_ok and not new_ok:
            self.plan.warnings.append("新旧目录都不存在，验证阶段会失败")
        root = Path(self.old if old_ok else self.new)
        git = root / ".git"
        if git.is_dir():
            self.text(git / "config", "Git")
            for f in (git / "worktrees").glob("*/gitdir"):
                self.text(f, "Git")
        if self.env.remote_url and git.is_dir():
            cur = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"],
                                 capture_output=True, text=True).stdout.strip()
            if cur != self.env.remote_url:
                self.plan.changes.append(Change("git", "Git", str(root), detail=f"origin: {cur or '(无)'} -> {self.env.remote_url}",
                                                extra={"old_url": cur, "new_url": self.env.remote_url}))
        elif git.is_dir():
            cur = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"],
                                 capture_output=True, text=True).stdout.strip()
            name = os.path.basename(self.old)
            if cur and os.path.basename(self.new) != name and re.search(rf"[/:]{re.escape(name)}(\.git)?$", cur):
                self.plan.warnings.append(f"origin 仍指向旧仓库名 ({cur})；远端改名后用 --remote-url 一起更新")

    def workspaces(self) -> None:
        roots = self.env.search_dirs or [Path(self.old).parent, Path(self.new).parent]
        for r in list(roots) + [Path(self.old), Path(self.new)]:
            if r.is_dir():
                for f in list(r.glob("*.code-workspace")) + list(r.glob("*/*.code-workspace")):
                    self.text(f, "VS Code 工作区文件")

    def vscode(self) -> None:
        for d in self.env.vscode_dirs:
            self.text(d / "globalStorage/storage.json", "VS Code 最近项目")
            self.sqlite(d / "globalStorage/state.vscdb", "ItemTable", "value", "VS Code 最近项目")
            for f in (d / "workspaceStorage").glob("*/workspace.json"):
                self.text(f, "VS Code 工作区缓存")

    def owned_dirs(self, base: Path, prefix: str, sep: str, encode) -> None:
        """Session dirs named after cwd (Claude Code / Pi). Ownership is confirmed via the stored cwd."""
        if not base.is_dir():
            return
        source = "Claude Code" if encode is claude_dir_name else "Pi"
        for d in sorted(p for p in base.iterdir() if p.is_dir()):
            if not (d.name == prefix or d.name.startswith(prefix.rstrip("-") + sep)):
                continue
            cwd = first_cwd(d)
            if cwd and not is_under(cwd, self.old):
                continue  # e.g. /a/b-c encodes like /a/b/c: not ours
            for f in sorted(d.rglob("*.jsonl")):
                self.text(f, source)
            target = self.rw.path(cwd) if cwd else (self.new if d.name == prefix else None)
            if target:
                self.rename(d, encode(target), source)

    def claude(self) -> None:
        h = self.env.home
        enc = re.sub(r"[^a-zA-Z0-9]", "-", self.old)[:200]
        self.owned_dirs(h / ".claude/projects", enc, "-", claude_dir_name)
        self.text(h / ".claude.json", "Claude Code", kind="json")
        self.text(h / ".claude/history.jsonl", "Claude Code")

    def codex(self) -> None:
        h = self.env.home / ".codex"
        for sub in ("sessions", "archived_sessions"):
            for f in sorted((h / sub).rglob("*.jsonl")) if (h / sub).is_dir() else []:
                with open(f, "rb") as fh:
                    m = _CWD_RX.search(fh.readline())
                if m and is_under(m.group(1).decode("utf-8", "replace"), self.old):
                    self.text(f, "Codex")
        for db in sorted(h.glob("state_*.sqlite")):
            self.sqlite(db, "threads", "cwd", "Codex")
        self.text(h / "config.toml", "Codex")

    def pi(self) -> None:
        self.owned_dirs(self.env.home / ".pi/agent/sessions", pi_dir_name(self.old), "-", pi_dir_name)

    def deep(self) -> None:
        h = self.env.home
        for base, src in ((h / ".claude/projects", "Claude Code"), (h / ".codex/sessions", "Codex"),
                          (h / ".pi/agent/sessions", "Pi")):
            for f in sorted(base.rglob("*.jsonl")) if base.is_dir() else []:
                self.text(f, src + " (其他会话中的提及)")

    def run(self) -> Plan:
        self.project()
        self.workspaces()
        self.vscode()
        self.claude()
        self.codex()
        self.pi()
        if self.env.deep:
            self.deep()
        return self.plan


def scan(old: str, new: str, env: Env) -> Plan:
    return Scanner(old, new, env).run()
