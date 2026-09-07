"""closure_map.py — 純 AST 的「依賴閉包圖」產生器（重構棒 1 用）。

不 import 受測專案的任何模組（生產機／GPU 安全），只讀原始碼：
- 每個核心檔的頂層 def／class：行數、裝飾器（route？）、直接碰到的外部類別（db／net／model／subproc／fs）、
  同模組內呼叫了哪些頂層函式。
- 沿同模組呼叫圖做傳遞閉包：一個函式的「閉包類別」＝它自己＋所有可達 callee 碰到的類別聯集。
  閉包裡沒有 db／net／model／subproc／route 的＝**純邏輯**，可以往外抽。
- 另掃全 repo：哪些頂層名稱被其他檔 `from X import` 或 `X.name` 使用（消費者），抽出去時要 re-export。

用法：PYTHONUTF8=1 python closure_map.py <repo_dir> <out.md> [--json out.json] [--modules server,worker,...]
判準是啟發式（名字對照），供人審，不是真理。
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# 外部類別的偵測：模組別名 → 類別
EXT_CATEGORY = {
    "db": {"db", "sqlite3"},
    "net": {"requests", "httpx", "urllib", "urllib.request", "aiohttp", "yt_dlp", "socket"},
    "model": {"pipeline", "models", "torch", "transformers", "easyocr", "faster_whisper", "whisper"},
    "subproc": {"subprocess"},
    "fs": {"shutil", "tempfile"},
}
ROUTE_DECORATOR_RE = re.compile(r"^(app|router)\.(get|post|put|delete|patch|api_route|middleware|on_event|exception_handler|websocket)$")
DEFAULT_MODULES = ["server", "worker", "pipeline", "db", "ingest", "verdict_map", "config", "domain_intel"]


def _dec_name(d: ast.expr) -> str:
    if isinstance(d, ast.Call):
        d = d.func
    parts = []
    while isinstance(d, ast.Attribute):
        parts.append(d.attr)
        d = d.value
    if isinstance(d, ast.Name):
        parts.append(d.id)
    return ".".join(reversed(parts))


def _root_of_attr(node: ast.Attribute) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def analyze_module(path: Path) -> dict:
    src = path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    lines = src.splitlines()

    # 模組層 import：alias → 模組名；from-import：name → 模組名
    alias_to_mod: dict[str, str] = {}
    fromname_to_mod: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                alias_to_mod[a.asname or a.name.split(".")[0]] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                fromname_to_mod[a.asname or a.name] = node.module

    top: dict[str, dict] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            end = getattr(node, "end_lineno", node.lineno)
            decos = [_dec_name(d) for d in getattr(node, "decorator_list", [])]
            top[node.name] = {
                "kind": type(node).__name__.replace("Def", "").lower(),
                "lineno": node.lineno, "end": end, "loc": end - node.lineno + 1,
                "decorators": decos,
                "route": any(ROUTE_DECORATOR_RE.match(d) for d in decos),
                "public": not node.name.startswith("_"),
                "node": node,
            }
    top_names = set(top)

    def scan(fn: dict) -> None:
        cats: set[str] = set()
        callees: set[str] = set()
        ext_mods: set[str] = set()
        for sub in ast.walk(fn["node"]):
            # 函式內部的 import（延遲載入）也算它碰到的外部模組
            if isinstance(sub, ast.Import):
                for a in sub.names:
                    ext_mods.add(a.name)
                continue
            if isinstance(sub, ast.ImportFrom) and sub.module:
                ext_mods.add(sub.module)
                continue
            if isinstance(sub, ast.Name):
                nid = sub.id
                if nid in top_names and nid != fn["node"].name:
                    callees.add(nid)
                mod = alias_to_mod.get(nid) or fromname_to_mod.get(nid)
                if mod:
                    ext_mods.add(mod)
            elif isinstance(sub, ast.Attribute):
                root = _root_of_attr(sub)
                if root:
                    mod = alias_to_mod.get(root)
                    if mod:
                        ext_mods.add(mod)
        for m in ext_mods:
            base = m.split(".")[0]
            for cat, mods in EXT_CATEGORY.items():
                if m in mods or base in mods:
                    cats.add(cat)
        if fn["route"]:
            cats.add("route")
        fn["direct_cats"] = sorted(cats)
        fn["callees"] = sorted(callees)
        fn["ext_mods"] = sorted(ext_mods)

    for fn in top.values():
        scan(fn)

    # 傳遞閉包
    def closure_cats(name: str, seen: set[str]) -> set[str]:
        if name in seen:
            return set()
        seen.add(name)
        fn = top[name]
        cats = set(fn["direct_cats"])
        for c in fn["callees"]:
            cats |= closure_cats(c, seen)
        return cats

    for name, fn in top.items():
        cc = closure_cats(name, set())
        fn["closure_cats"] = sorted(cc)
        fn["pure_closure"] = not (cc & {"db", "net", "model", "subproc", "route"})
        fn.pop("node", None)

    callers: dict[str, set[str]] = defaultdict(set)
    for name, fn in top.items():
        for c in fn["callees"]:
            callers[c].add(name)
    for name, fn in top.items():
        fn["callers"] = sorted(callers.get(name, ()))

    return {"module": path.stem, "path": str(path), "loc": len(lines), "top": top,
            "imports": {"alias": alias_to_mod, "from": fromname_to_mod}}


def scan_consumers(repo: Path, modules: list[str]) -> dict[str, dict[str, set[str]]]:
    """回傳 {module: {name: {消費檔...}}}：其他檔用 `from M import name` 或 `M.name` 的地方。"""
    out: dict[str, dict[str, set[str]]] = {m: defaultdict(set) for m in modules}
    for py in repo.rglob("*.py"):
        if ".git" in py.parts or py.stem in modules:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        rel = str(py.relative_to(repo))
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in modules:
                for a in node.names:
                    out[node.module][a.name].add(rel)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name in modules:
                        aliases[a.asname or a.name] = a.name
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases:
                out[aliases[node.value.id]][node.attr].add(rel)
    return out


def render_md(results: list[dict], consumers: dict) -> str:
    L: list[str] = []
    L.append("# 依賴閉包圖（AST 產生，啟發式，供人審）\n")
    L.append("類別：`route`＝FastAPI 路由｜`db`＝碰 db 模組／sqlite3｜`net`＝requests 等｜`model`＝pipeline／models／torch｜"
             "`subproc`｜`fs`（tempfile／shutil，不算外部）。**閉包**＝自己＋同模組可達 callee 的類別聯集；"
             "閉包不含 route/db/net/model/subproc 的標 ✅ 純邏輯，可往外抽。\n")
    for r in results:
        top = r["top"]
        pure = [n for n, f in top.items() if f["pure_closure"]]
        L.append(f"\n## {r['module']}.py — {r['loc']} 行，頂層 {len(top)} 個；✅ 純閉包 {len(pure)} 個"
                 f"（{sum(top[n]['loc'] for n in pure)} 行）\n")
        L.append("| 名稱 | 行 | LOC | 直接類別 | 閉包類別 | 同模組 callee | 被誰呼叫 | 外部消費者 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for n, f in sorted(top.items(), key=lambda kv: kv[1]["lineno"]):
            mark = "✅ " if f["pure_closure"] else ""
            cons = consumers.get(r["module"], {}).get(n, set())
            L.append(f"| {mark}`{n}`{' (class)' if f['kind']=='class' else ''} | {f['lineno']} | {f['loc']} | "
                     f"{', '.join(f['direct_cats']) or '—'} | {', '.join(f['closure_cats']) or '—'} | "
                     f"{', '.join(f['callees']) or '—'} | {', '.join(f['callers']) or '—'} | "
                     f"{', '.join(sorted(cons)) if cons else '—'} |")
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("out_md")
    ap.add_argument("--json")
    ap.add_argument("--modules", default=",".join(DEFAULT_MODULES))
    a = ap.parse_args(argv)
    repo = Path(a.repo)
    modules = [m.strip() for m in a.modules.split(",") if m.strip()]
    results = []
    for m in modules:
        p = repo / f"{m}.py"
        if p.exists():
            results.append(analyze_module(p))
    consumers = scan_consumers(repo, modules)
    Path(a.out_md).write_text(render_md(results, consumers), encoding="utf-8", newline="\n")
    if a.json:
        ser = {r["module"]: {"loc": r["loc"], "top": r["top"],
                             "consumers": {k: sorted(v) for k, v in consumers.get(r["module"], {}).items()}}
               for r in results}
        Path(a.json).write_text(json.dumps(ser, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    for r in results:
        pure = [n for n, f in r["top"].items() if f["pure_closure"]]
        print(f"{r['module']:12s} loc={r['loc']:5d} top={len(r['top']):3d} pure={len(pure):3d} "
              f"pure_loc={sum(r['top'][n]['loc'] for n in pure)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
