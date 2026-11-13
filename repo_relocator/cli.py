"""repo-relocator CLI: plan -> confirm -> backup+apply -> verify -> (rollback)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .core import Plan, RelocateError
from .ops import Journal, apply, backup_root, list_backups, rollback
from .scanners import Env, scan
from .verify import verify

CLOSE_HINT = "提示: 应用前请先退出 VS Code 以及正在该项目里运行的 Claude Code / Codex / Pi 会话, 否则它们可能把旧值写回。"


def _env(a) -> Env:
    return Env.build(home=a.home, vscode_dirs=a.vscode_dir, search_dirs=[Path(p) for p in a.search_dir],
                     move=getattr(a, "move", False), deep=getattr(a, "deep", False),
                     remote_url=getattr(a, "remote_url", None))


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--home", help="把这个目录当作 home (测试/演示用), 默认 ~")
    p.add_argument("--vscode-dir", action="append", help="VS Code User 目录, 可多次指定")
    p.add_argument("--search-dir", action="append", default=[], help="额外搜索 *.code-workspace 的目录")


def _pair(p: argparse.ArgumentParser) -> None:
    p.add_argument("old", help="旧项目路径")
    p.add_argument("new", help="新项目路径")
    p.add_argument("--move", action="store_true", help="顺带把项目目录从 old 移到 new")
    p.add_argument("--deep", action="store_true", help="连其他项目会话里顺口提到的旧路径也改")
    p.add_argument("--remote-url", help="同时把 git origin 改成这个 URL (仓库改名时用)")
    _common(p)


def _confirm(msg: str) -> bool:
    try:
        return input(f"{msg} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_plan(a) -> int:
    plan = scan(a.old, a.new, _env(a))
    if a.json:
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(plan.render(detail=a.detail))
    if a.out:
        Path(a.out).write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n计划已保存到 {a.out}")
    return 0


def cmd_apply(a) -> int:
    env = _env(a)
    plan = scan(a.old, a.new, env)
    print(plan.render(detail=a.detail))
    if not plan.changes:
        return 0
    print("\n" + CLOSE_HINT)
    if not a.yes and not _confirm("确认按上面的计划备份并执行?"):
        print("已取消, 没有改动任何东西。")
        return 1
    j = apply(plan, env.home)
    print(f"\n已应用。备份 ID: {j.data['id']}  ({j.dir})")
    for w in j.data.get("warnings", []):
        print(f"  注意: {w}")
    print("\n开始验证:")
    rep = verify(plan, env)
    print(rep.render())
    j.status("verified" if rep.ok else "verify_failed")
    if rep.ok:
        return 0
    if a.yes or _confirm("验证没过, 现在一键回滚?"):
        errs = rollback(j)
        print("已回滚到迁移前状态。" if not errs else "回滚有错误:\n  " + "\n  ".join(errs))
    else:
        print(f"未回滚。之后可执行: repo-relocator rollback {j.data['id']}")
    return 2


def _find(env: Env, bid: str | None) -> Journal:
    items = list_backups(env.home)
    if not items:
        raise RelocateError("没有任何备份记录")
    if bid is None:
        return Journal.load(backup_root(env.home) / items[-1]["id"])
    if not (backup_root(env.home) / bid / "manifest.json").is_file():
        raise RelocateError(f"找不到备份 {bid}")
    return Journal.load(backup_root(env.home) / bid)


def cmd_verify(a) -> int:
    env = _env(a)
    j = _find(env, a.id)
    rep = verify(Plan.from_dict(j.data["plan"]), env)
    print(f"验证备份 {j.data['id']}: {j.data['old']} -> {j.data['new']}\n" + rep.render())
    return 0 if rep.ok else 2


def cmd_rollback(a) -> int:
    env = _env(a)
    j = _find(env, a.id)
    if j.data["status"] == "rolled_back":
        print(f"{j.data['id']} 已经回滚过了。")
        return 0
    print(f"将回滚 {j.data['id']}: {j.data['new']} -> {j.data['old']} ({len(j.data['steps'])} 步)")
    if not a.yes and not _confirm("确认回滚?"):
        return 1
    errs = rollback(j)
    print("回滚完成。" if not errs else "回滚有错误:\n  " + "\n  ".join(errs))
    return 0 if not errs else 2


def cmd_list(a) -> int:
    items = list_backups(_env(a).home)
    if not items:
        print("还没有备份记录。")
    for d in items:
        print(f"{d['id']}  {d['status']:<14} {d['old']} -> {d['new']}  ({len(d['steps'])} 步)")
    return 0


def cmd_demo(a) -> int:
    from .demo import run_demo
    return run_demo(a.dir)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="repo-relocator", description="项目改名/搬家: 扫描 -> 确认 -> 备份 -> 应用 -> 验证 -> 回滚")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="只读扫描, 列出所有要改的地方")
    _pair(p)
    p.add_argument("--detail", action="store_true", help="逐个文件列出")
    p.add_argument("--json", action="store_true")
    p.add_argument("--out", help="把计划存成 JSON")
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("apply", help="确认后备份并执行, 自动验证")
    _pair(p)
    p.add_argument("--detail", action="store_true")
    p.add_argument("-y", "--yes", action="store_true", help="不询问 (验证失败时自动回滚)")
    p.set_defaults(fn=cmd_apply)
    for name, fn, hlp in (("verify", cmd_verify, "重新验证某次迁移"), ("rollback", cmd_rollback, "回滚某次迁移")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("id", nargs="?", help="备份 ID, 默认最近一次")
        p.add_argument("-y", "--yes", action="store_true")
        _common(p)
        p.set_defaults(fn=fn)
    p = sub.add_parser("list", help="列出历史迁移/备份")
    _common(p)
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("demo", help="在临时沙盒里演示完整流程, 不碰真实数据")
    p.add_argument("--dir", help="沙盒目录, 默认临时目录")
    p.set_defaults(fn=cmd_demo)
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except RelocateError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
