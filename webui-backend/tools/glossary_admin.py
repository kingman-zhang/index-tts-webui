#!/usr/bin/env python3
"""术语表超管 CLI：维护「全局库」data/glossary.json（无需后端运行）。

全局库对所有用户生效；用户在个人账号下的自定义词条同名时优先于全局库。

用法（在 webui-backend 目录下）：
    python tools/glossary_admin.py list
    python tools/glossary_admin.py add --original 说服 --replacement 说福
    python tools/glossary_admin.py remove --original 重头
    python tools/glossary_admin.py import --file <path.json> [--replace]
    python tools/glossary_admin.py export --file <path.json>

    # 数据目录非默认时（该参数由 app.config 消费）：
    python tools/glossary_admin.py --data-dir /path/to/data list

`import` 默认合并（同名覆盖、其余保留）；加 --replace 会先清空全局库。

加词前请先校验同音性：python tools/check_glossary_homophone.py
（本 CLI 只负责写库，不做读音判断。）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 让 `import app` 生效（tools/ 与 webui-backend/ 同级）
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.stores import load_global_glossary, save_global_glossary  # noqa: E402


def _read_json_terms(path_str: str) -> list:
    path = Path(path_str)
    if not path.exists():
        raise SystemExit(f"文件不存在: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit("文件内容必须是 [{\"original\":...,\"replacement\":...}] 数组")
    out = []
    for t in data:
        if not isinstance(t, dict):
            continue
        o = str(t.get("original") or "").strip()
        if not o:
            continue
        out.append({"original": o, "replacement": str(t.get("replacement") or "")})
    return out


def _dedupe(terms: list) -> list:
    """按 original 去重，后者覆盖前者并保持位置。"""
    out, index = [], {}
    for t in terms:
        o = t["original"]
        if o in index:
            out[index[o]] = t
        else:
            index[o] = len(out)
            out.append(t)
    return out


def _show(terms: list) -> None:
    if not terms:
        print("（全局库为空）")
        return
    width = max(len(t["original"]) for t in terms)
    for t in terms:
        rep = t["replacement"] or "（替换为空）"
        print(f"  {t['original']:<{width}}  ->  {rep}")
    print(f"\n共 {len(terms)} 条")


def main() -> None:
    parser = argparse.ArgumentParser(description="术语表超管 CLI（全局库）")
    # app.config 在导入时已用 parse_known_args 消费了 --data-dir（真正决定 DATA_DIR），
    # 但它不把该参数从 argv 移除；这里必须补一个同名参数接收，否则
    # `--data-dir X list` 会被当成「cmd=X」而报 invalid choice（2026-09-28 修）。
    parser.add_argument("--data-dir", default=None,
                        help="数据目录（生效方是 app.config，此处仅为接收）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出全局库")

    p = sub.add_parser("add", help="新增/覆盖一条全局条目")
    p.add_argument("--original", required=True, help="原词")
    p.add_argument("--replacement", default="", help="替换为（留空=替换为空串）")

    p = sub.add_parser("remove", help="删除一条全局条目")
    p.add_argument("--original", required=True)

    p = sub.add_parser("import", help="从 JSON 文件导入")
    p.add_argument("--file", required=True)
    p.add_argument("--replace", action="store_true", help="先清空全局库再导入")

    p = sub.add_parser("export", help="导出全局库到 JSON")
    p.add_argument("--file", required=True)

    args = parser.parse_args()

    if args.cmd == "list":
        _show(load_global_glossary())

    elif args.cmd == "add":
        terms = [t for t in load_global_glossary() if t.get("original") != args.original]
        terms.append({"original": args.original.strip(), "replacement": args.replacement})
        save_global_glossary(_dedupe(terms))
        print(f"已写入全局库：{args.original} -> {args.replacement or '（替换为空）'}")
        _show(load_global_glossary())

    elif args.cmd == "remove":
        before = load_global_glossary()
        after = [t for t in before if t.get("original") != args.original]
        if len(after) == len(before):
            raise SystemExit(f"全局库中没有 {args.original!r}")
        save_global_glossary(after)
        print(f"已删除：{args.original}")
        _show(after)

    elif args.cmd == "import":
        incoming = _read_json_terms(args.file)
        if args.replace:
            merged = _dedupe(incoming)
        else:
            merged = _dedupe(load_global_glossary() + incoming)
        save_global_glossary(merged)
        print(f"已导入 {len(incoming)} 条"
              f"（{'替换' if args.replace else '合并'}后共 {len(merged)} 条）")
        _show(merged)

    elif args.cmd == "export":
        terms = load_global_glossary()
        Path(args.file).write_text(
            json.dumps(terms, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"已导出 {len(terms)} 条到 {args.file}")


if __name__ == "__main__":
    main()
