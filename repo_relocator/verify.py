"""Post-apply verification: can each tool still find its old sessions at the new path?

This is a static check of exactly what each tool uses to locate a session:
  - Claude Code: ~/.claude/projects/<encode(cwd)>/*.jsonl, with "cwd" inside
  - Pi:          ~/.pi/agent/sessions/--<cwd>--/*.jsonl, header {"type":"session","cwd":...}
  - Codex:       sessions/**/rollout-*.jsonl session_meta.cwd + state_*.sqlite threads.cwd
We do not launch the GUIs/CLIs; we check the data they read.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .core import Plan, Rewriter, claude_dir_name, is_under, pi_dir_name
from .scanners import Env, first_cwd


class Report:
    def __init__(self):
        self.checks: list[tuple[bool, str]] = []

    def add(self, ok: bool, msg: str) -> None:
        self.checks.append((ok, msg))

    def note(self, msg: str) -> None:
        self.checks.append((None, msg))

    @property
    def ok(self) -> bool:
        return all(ok is not False for ok, _ in self.checks)

    def render(self) -> str:
        tag = {True: "  通过  ", False: "  失败  ", None: "  提示  "}
        lines = [tag[ok] + msg for ok, msg in self.checks]
        lines.append("验证结果: " + ("全部通过" if self.ok else "存在失败项"))
        return "\n".join(lines)


def _json_lines_ok(path: Path, limit: int = 3) -> bool:
    try:
        with open(path, encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i >= limit:
                    break
                if line.strip():
                    json.loads(line)
        return True
    except (OSError, ValueError):
        return False


def _session_dirs(base: Path, new: str, encode) -> list[Path]:
    if not base.is_dir():
        return []
    out = []
    prefix = (encode(new) or "").rstrip("-")
    for d in base.iterdir():
        if not prefix or not d.name.startswith(prefix):
            continue
        cwd = first_cwd(d) if d.is_dir() else None
        if cwd and is_under(cwd, new):
            out.append(d)
    return out


def verify(plan: Plan, env: Env) -> Report:
    r = Report()
    rw = Rewriter(plan.old, plan.new)
    r.add(os.path.isdir(plan.new), f"新项目目录存在: {plan.new}")
    if os.path.isdir(plan.old):
        r.add(False, f"旧目录仍然存在 (半迁移状态): {plan.old}")

    # 1) nothing we planned to touch still references the old path
    leftovers = 0
    for c in plan.changes:
        if c.kind in ("text", "json"):
            p = Path(c.path)
            for ch in plan.changes:  # file may now live inside a renamed dir
                if ch.kind == "rename" and c.path.startswith(ch.path + "/"):
                    p = Path(ch.dest + c.path[len(ch.path):])
            if p.is_file() and rw.count(p.read_text(encoding="utf-8")):
                leftovers += 1
                r.add(False, f"仍含旧路径: {p}")
            elif p.is_file() and p.suffix == ".jsonl" and not _json_lines_ok(p):
                r.add(False, f"JSONL 损坏: {p}")
            elif p.suffix == ".json" and p.is_file():
                try:
                    json.loads(p.read_text(encoding="utf-8"))
                except ValueError:
                    r.add(False, f"JSON 损坏: {p}")
        elif c.kind == "sqlite":
            con = sqlite3.connect(f"file:{c.path}?mode=ro", uri=True)
            col, tab = c.extra["column"], c.extra["table"]
            vals = [v for (v,) in con.execute(f"SELECT {col} FROM {tab}")]
            con.close()
            bad = sum(1 for v in vals if v is not None and rw.count(
                v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)))
            if bad:
                leftovers += 1
                r.add(False, f"{c.path} ({tab}.{col}) 还有 {bad} 行含旧路径")
    if leftovers == 0:
        r.add(True, "计划内所有文件/数据库已不含旧路径")

    # 2) each tool can resolve sessions for the new cwd
    h = env.home
    for name, base, encode in (("Claude Code", h / ".claude/projects", claude_dir_name),
                               ("Pi", h / ".pi/agent/sessions", pi_dir_name)):
        expected = [c for c in plan.changes if c.kind == "rename" and c.source == name]
        dirs = _session_dirs(base, plan.new, encode)
        if not expected and not dirs:
            continue
        for d in dirs:
            cwd = first_cwd(d)
            want = encode(cwd)
            if want and d.name != want:
                r.add(False, f"{name}: 会话目录 {d.name} 与 cwd {cwd} 的编码 {want} 不一致, 打开时找不到")
            elif not os.path.isdir(cwd):
                if cwd == plan.new:
                    r.add(False, f"{name}: 会话 cwd 指向不存在的目录 {cwd}")
                else:  # subdir may have been deleted long before the move
                    r.note(f"{name}: 子目录会话的 cwd 已不存在 (迁移前可能就已删除): {cwd}")
        n = sum(len(list(d.glob("*.jsonl"))) for d in dirs)
        r.add(bool(dirs) and n > 0, f"{name}: 在新路径下可定位 {n} 个会话文件 ({len(dirs)} 个目录)")
    codex_files = [c for c in plan.changes if c.source == "Codex" and c.path.endswith(".jsonl")]
    if codex_files:
        missing = [c.path for c in codex_files if not Path(c.path).is_file()]
        r.add(not missing, f"Codex: {len(codex_files)} 个会话文件 cwd 已指向新路径")
    return r
