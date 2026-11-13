"""Backup + apply + rollback. Every step is journaled to manifest.json before it runs."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from .core import Change, Plan, RelocateError, Rewriter, dump_json, rewrite_json

ORDER = {"text": 0, "json": 0, "sqlite": 1, "git": 2, "rename": 3, "move": 4}


def backup_root(home: Path) -> Path:
    return Path(os.environ.get("REPO_RELOCATOR_HOME", home / ".repo-relocator")) / "backups"


def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.rr-tmp")
    tmp.write_text(text, encoding="utf-8")
    shutil.copymode(path, tmp)
    os.replace(tmp, path)


class Journal:
    def __init__(self, directory: Path, plan: Plan):
        self.dir = directory
        (directory / "files").mkdir(parents=True)
        self.data = {"id": directory.name, "old": plan.old, "new": plan.new, "status": "applying",
                     "created": time.strftime("%Y-%m-%d %H:%M:%S"), "plan": plan.to_dict(), "steps": []}
        self.save()

    @classmethod
    def load(cls, directory: Path) -> "Journal":
        j = cls.__new__(cls)
        j.dir = directory
        j.data = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        return j

    def save(self) -> None:
        dump_json(self.dir / "manifest.json", self.data)

    def step(self, **info) -> dict:
        info["done"] = False
        self.data["steps"].append(info)
        self.save()
        return info

    def status(self, value: str, **extra) -> None:
        self.data["status"] = value
        self.data.update(extra)
        self.save()


def _rewrite_file(c: Change, rw: Rewriter, j: Journal) -> None:
    src = Path(c.path)
    saved = j.dir / "files" / f"{len(j.data['steps']):05d}_{src.name}"
    shutil.copy2(src, saved)
    st = j.step(kind="file", path=c.path, backup=str(saved))
    text = src.read_text(encoding="utf-8")
    if c.kind == "json":
        warnings: list[str] = []
        obj, _ = rewrite_json(json.loads(text), rw, warnings)
        new_text = json.dumps(obj, ensure_ascii=False, indent=2)
        j.data.setdefault("warnings", []).extend(warnings)
    else:
        new_text, _ = rw.sub(text)
    atomic_write(src, new_text)
    st["done"] = True


def _rewrite_sqlite(c: Change, rw: Rewriter, j: Journal) -> None:
    table, col = c.extra["table"], c.extra["column"]
    con = sqlite3.connect(c.path, timeout=5)
    try:
        rows = con.execute(f"SELECT rowid, {col} FROM {table}").fetchall()
        undo = []
        for rowid, val in rows:
            if val is None:
                continue
            is_bytes = isinstance(val, bytes)
            text = val.decode("utf-8") if is_bytes else str(val)
            new, n = rw.sub(text)
            if n:
                undo.append([rowid, text, new, is_bytes])
        st = j.step(kind="sqlite", path=c.path, table=table, column=col, rows=undo)
        with con:
            for rowid, _, new, is_bytes in undo:
                con.execute(f"UPDATE {table} SET {col}=? WHERE rowid=?",
                            (new.encode() if is_bytes else new, rowid))
        st["done"] = True
    finally:
        con.close()


def _git(c: Change, j: Journal, root: str) -> None:
    st = j.step(kind="git", path=root, old_url=c.extra["old_url"], new_url=c.extra["new_url"])
    verb = "set-url" if c.extra["old_url"] else "add"
    subprocess.run(["git", "-C", root, "remote", verb, "origin", c.extra["new_url"]], check=True,
                   capture_output=True)
    st["done"] = True


def _rename(c: Change, j: Journal) -> None:
    src, dest = Path(c.path), Path(c.dest)
    if not dest.exists():
        st = j.step(kind="rename", src=c.path, dest=c.dest, merged=[])
        os.rename(src, dest)
    else:  # merge: move children one by one so rollback knows exactly what came from where
        st = j.step(kind="rename", src=c.path, dest=c.dest, merged=[])
        for child in sorted(src.iterdir()):
            target = dest / child.name
            if target.exists():
                raise RelocateError(f"merge conflict: {target} already exists")
            os.rename(child, target)
            st["merged"].append(child.name)
            j.save()
        src.rmdir()
    st["done"] = True


def _move(c: Change, j: Journal) -> None:
    st = j.step(kind="move", src=c.path, dest=c.dest)
    Path(c.dest).parent.mkdir(parents=True, exist_ok=True)
    shutil.move(c.path, c.dest)
    st["done"] = True


def apply(plan: Plan, home: Path) -> Journal:
    """Back up and apply all changes. On any error, roll back what was done and re-raise."""
    rw = Rewriter(plan.old, plan.new)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    d = backup_root(home) / stamp
    n = 1
    while d.exists():
        d = backup_root(home) / f"{stamp}-{n}"
        n += 1
    j = Journal(d, plan)
    git_root = plan.old if os.path.isdir(plan.old) else plan.new
    try:
        for c in sorted(plan.changes, key=lambda c: ORDER[c.kind]):
            if c.kind in ("text", "json"):
                _rewrite_file(c, rw, j)
            elif c.kind == "sqlite":
                _rewrite_sqlite(c, rw, j)
            elif c.kind == "git":
                _git(c, j, git_root)
            elif c.kind == "rename":
                _rename(c, j)
            elif c.kind == "move":
                _move(c, j)
            j.save()
    except Exception as exc:
        j.status("failed", error=repr(exc))
        rollback(j)
        raise RelocateError(f"应用失败并已自动回滚: {exc}") from exc
    j.status("applied")
    return j


def _undo(st: dict) -> None:
    k = st["kind"]
    if k == "file":
        dst = Path(st["path"])
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(st["backup"], dst)
    elif k == "sqlite":
        con = sqlite3.connect(st["path"], timeout=5)
        with con:
            for rowid, old, _, is_bytes in st["rows"]:
                con.execute(f"UPDATE {st['table']} SET {st['column']}=? WHERE rowid=?",
                            (old.encode() if is_bytes else old, rowid))
        con.close()
    elif k == "git":
        root = st["path"] if os.path.isdir(st["path"]) else None
        if root and st["old_url"]:
            subprocess.run(["git", "-C", root, "remote", "set-url", "origin", st["old_url"]], check=True)
        elif root:
            subprocess.run(["git", "-C", root, "remote", "remove", "origin"], check=False)
    elif k == "rename":
        src, dest = Path(st["src"]), Path(st["dest"])
        if st["merged"] or src.exists():
            src.mkdir(exist_ok=True)
            for name in st["merged"]:
                if (dest / name).exists():
                    os.rename(dest / name, src / name)
        elif dest.exists():
            os.rename(dest, src)
    elif k == "move" and os.path.exists(st["dest"]) and not os.path.exists(st["src"]):
        shutil.move(st["dest"], st["src"])


def rollback(j: Journal) -> list[str]:
    """Undo journaled steps in reverse order (including a half-finished one). Returns errors."""
    errors = []
    for st in reversed(j.data["steps"]):
        try:
            _undo(st)
        except Exception as exc:  # keep going: restore as much as possible
            errors.append(f"{st['kind']} {st.get('path') or st.get('src')}: {exc}")
    j.status("rolled_back" if not errors else "rollback_incomplete", rollback_errors=errors)
    return errors


def list_backups(home: Path) -> list[dict]:
    root = backup_root(home)
    out = []
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        m = d / "manifest.json"
        if m.is_file():
            out.append(Journal.load(d).data)
    return out
