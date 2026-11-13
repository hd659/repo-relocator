"""Core model: path normalisation, tool-specific dir encoders, safe path replacement, plan."""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import quote

CLAUDE_DIR_MAX = 200  # Claude Code truncates + hashes longer encoded names
_FORBIDDEN = set('"\\\n\r\t')
_EDGE = r"[\w.~-]"  # chars that would mean "this is a longer, different path"


class RelocateError(Exception):
    """Raised for invalid input or an unsafe operation."""


def norm(path: str) -> str:
    """Absolute path without trailing slash; symlinks are NOT resolved on purpose."""
    p = os.path.abspath(os.path.expanduser(str(path)))
    return p.rstrip("/") or "/"


def check_pair(old: str, new: str) -> tuple[str, str]:
    old, new = norm(old), norm(new)
    if old == new:
        raise RelocateError("old and new path are identical")
    if old in ("/", os.path.expanduser("~")):
        raise RelocateError(f"refusing to relocate a root/home directory: {old}")
    if any(c in _FORBIDDEN for c in old + new):
        raise RelocateError("paths containing quotes, backslashes or control chars are not supported")
    if new.startswith(old + "/"):
        raise RelocateError("new path must not live inside the old path")
    return old, new


def claude_dir_name(path: str) -> str | None:
    """~/.claude/projects/<name>; None when Claude would truncate+hash it."""
    enc = re.sub(r"[^a-zA-Z0-9]", "-", path)
    return enc if len(enc) <= CLAUDE_DIR_MAX else None


def pi_dir_name(path: str) -> str:
    """~/.pi/agent/sessions/<name>, same encoding as pi's session-manager."""
    return "--" + re.sub(r"[/\\:]", "-", re.sub(r"^[/\\]", "", path)) + "--"


def is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


class Rewriter:
    """Replaces whole-path occurrences of old (plain and file:// percent-encoded)."""

    def __init__(self, old: str, new: str):
        self.old, self.new = old, new
        pairs = {old: new}
        q_old, q_new = quote(old, safe="/"), quote(new, safe="/")
        if q_old != old:
            pairs[q_old] = q_new
        self.map = pairs
        alt = "|".join(re.escape(k) for k in sorted(pairs, key=len, reverse=True))
        self.rx = re.compile(rf"(?<!{_EDGE})({alt})(?!{_EDGE})")
        self.needles = [k.encode() for k in pairs]

    def quick(self, data: bytes) -> bool:
        return any(n in data for n in self.needles)

    def count(self, text: str) -> int:
        return sum(1 for _ in self.rx.finditer(text))

    def sub(self, text: str) -> tuple[str, int]:
        return self.rx.subn(lambda m: self.map[m.group(1)], text)

    def path(self, p: str) -> str | None:
        """Map a path under old to its new location, else None."""
        if is_under(p, self.old):
            return self.new + p[len(self.old):]
        return None


def rewrite_json(obj, rw: Rewriter, warnings: list[str]):
    """Recursively rewrite keys and string values. Returns (new_obj, count)."""
    if isinstance(obj, str):
        return rw.sub(obj)
    if isinstance(obj, list):
        out, total = [], 0
        for item in obj:
            v, c = rewrite_json(item, rw, warnings)
            out.append(v)
            total += c
        return out, total
    if isinstance(obj, dict):
        kept, moved, total = {}, {}, 0
        for k, v in obj.items():
            nk, ck = rw.sub(k)
            nv, cv = rewrite_json(v, rw, warnings)
            total += ck + cv
            (moved if ck else kept)[nk] = nv
        for nk, nv in moved.items():
            if nk in kept:
                warnings.append(f"key collision on {nk!r}: migrated entry replaces the existing one")
            kept[nk] = nv
        return kept, total
    return obj, 0


@dataclass
class Change:
    kind: str  # text | json | sqlite | rename | git | move
    source: str  # human label, e.g. "Claude Code"
    path: str
    count: int = 0
    dest: str | None = None
    detail: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class Plan:
    old: str
    new: str
    changes: list[Change] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"old": self.old, "new": self.new, "warnings": self.warnings,
                "changes": [asdict(c) for c in self.changes]}

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        return cls(d["old"], d["new"], [Change(**c) for c in d["changes"]], list(d.get("warnings", [])))

    def render(self, detail: bool = False) -> str:
        lines = [f"迁移计划: {self.old}  ->  {self.new}", ""]
        if not self.changes:
            lines.append("  没有发现任何引用旧路径的地方。")
        groups: dict[str, list[Change]] = {}
        for c in self.changes:
            groups.setdefault(c.source, []).append(c)
        for source, items in groups.items():
            files = [c for c in items if c.kind in ("text", "json", "sqlite")]
            renames = [c for c in items if c.kind == "rename"]
            parts = []
            if files:
                parts.append(f"改写 {len(files)} 个文件, {sum(c.count for c in files)} 处引用")
            if renames:
                parts.append(f"重命名 {len(renames)} 个会话目录")
            for c in items:
                if c.kind in ("git", "move"):
                    parts.append(c.detail)
            lines.append(f"  [{source}] " + "; ".join(parts))
            if detail:
                for c in items:
                    arrow = f" -> {c.dest}" if c.dest else ""
                    lines.append(f"      {c.kind:6} {c.path}{arrow}  {c.detail or ''}".rstrip())
        total = sum(c.count for c in self.changes)
        lines += ["", f"合计 {len(self.changes)} 项改动, {total} 处路径引用。"]
        for w in self.warnings:
            lines.append(f"  注意: {w}")
        return "\n".join(lines)


def dump_json(path, data) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
