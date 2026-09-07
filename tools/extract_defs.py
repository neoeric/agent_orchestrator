"""extract_defs.py — 把模組裡指定的頂層定義「往外抽」成新模組，原檔原名 re-export（重構棒 2 用）。

做的事（純文字＋AST 定位，不 import 受測專案）：
1. 用 AST 找出 <source> 裡指定名稱的頂層 def／async def／class／單一名稱的 Assign，取其原始碼片段
   （含裝飾器），依原順序寫進 <target>，前面加上 --header（docstring）與 --imports（逐行）。
2. 從 <source> 刪掉那些片段，把連續 3 個以上空行壓回 2 個。
3. 在 <source> 的 --anchor（正規表示式，取第一個命中行）**之後**插入
   `from <target_module> import (name1, name2, ...)`  ← 原名 re-export，消費者一行不改。
4. 印出摘要：搬了幾個、幾行；並檢查 <target> 沒有 import <source 模組名>。

用法：
  python extract_defs.py <source.py> <target.py> --module applib.render \
      --names _TZ_TAIPEI,_tpe,_duration --imports "import html" --imports "from datetime import datetime" \
      --header "docstring text" --anchor "^from verdict_map import"
不做的事：不判斷搬得對不對（那是閉包圖與審查的事）；不處理巢狀／條件式定義。
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path


def top_level_nodes(tree: ast.Module) -> dict[str, ast.AST]:
    out: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[node.name] = node
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            out[node.targets[0].id] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out[node.target.id] = node
    return out


def node_span(node: ast.AST, lines: list[str] | None = None) -> tuple[int, int]:
    start = node.lineno
    decos = getattr(node, "decorator_list", None)
    if decos:
        start = min(d.lineno for d in decos)
    # 緊貼在定義上方、中間沒有空行的 `#` 註解區也一起帶走（2026-09-07 第二組審查抓到：
    # 只搬 AST 節點會讓「說明 token shim」的註解孤兒留在原檔、張冠李戴到下一個常數頭上）。
    # 有空行隔開的區段標頭（# ── xxx ──）不帶，那是描述整個區段的。
    if lines is not None:
        i = start - 2  # 0-based index of the line above `start`
        while i >= 0 and lines[i].lstrip().startswith("#"):
            i -= 1
        start = i + 2
    return start, node.end_lineno  # 1-based inclusive


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("target")
    ap.add_argument("--module", required=True, help="target 的 import 路徑，例如 applib.render")
    ap.add_argument("--names", required=True, help="逗號分隔的頂層名稱，依此順序寫入 target")
    ap.add_argument("--imports", action="append", default=[], help="target 需要的 import 行（可重複）")
    ap.add_argument("--header", default="", help="target 的模組 docstring")
    ap.add_argument("--anchor", required=True, help="source 裡插入 re-export 行的錨點（regex，取第一個命中行之後）")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    src_path, tgt_path = Path(a.source), Path(a.target)
    # newline="" 才保得住 CRLF（否則偵測永遠是 LF）；Path.read_text 的 newline 參數 3.13 才有，用 open()
    with open(src_path, encoding="utf-8", newline="") as fh:
        src = fh.read()
    newline = "\r\n" if "\r\n" in src else "\n"
    lines = src.replace("\r\n", "\n").split("\n")
    tree = ast.parse("\n".join(lines))
    nodes = top_level_nodes(tree)
    names = [n.strip() for n in a.names.split(",") if n.strip()]
    missing = [n for n in names if n not in nodes]
    if missing:
        print("找不到頂層定義：", missing, file=sys.stderr)
        return 2

    # 依原始碼順序排列片段，避免 target 裡引用順序倒置
    spans = sorted(((node_span(nodes[n], lines), n) for n in names), key=lambda x: x[0][0])
    segments: list[str] = []
    removed = set()
    for (s, e), n in spans:
        segments.append("\n".join(lines[s - 1:e]))
        removed.update(range(s - 1, e))
    moved_lines = len(removed)

    # target
    parts = []
    if a.header:
        parts.append('"""' + a.header.strip() + '\n"""')
    if a.imports:
        parts.append("\n".join(a.imports))
    parts.extend(segments)
    target_text = "\n\n\n".join(parts) + "\n"
    src_mod = src_path.stem
    if re.search(rf"^\s*(import {src_mod}\b|from {src_mod}\b)", target_text, re.M):
        print(f"target 竟然 import 了 {src_mod}，違反單向依賴，中止", file=sys.stderr)
        return 2

    # source：刪片段
    kept = [l for i, l in enumerate(lines) if i not in removed]
    text = "\n".join(kept)
    text = re.sub(r"\n{4,}", "\n\n\n", text)  # 頂層之間最多兩個空行

    # 插入 re-export
    anchor_re = re.compile(a.anchor)
    out_lines = text.split("\n")
    idx = next((i for i, l in enumerate(out_lines) if anchor_re.search(l)), None)
    if idx is None:
        print("anchor 沒命中：", a.anchor, file=sys.stderr)
        return 2
    # 若 anchor 是多行括號 import，跳到括號結束
    j = idx
    if "(" in out_lines[idx] and ")" not in out_lines[idx]:
        while j < len(out_lines) and ")" not in out_lines[j]:
            j += 1
    export = f"from {a.module} import ({', '.join(names)})  # noqa: F401  # 2026-09-07 重構：原名 re-export"
    if len(export) > 110:
        body = ",\n    ".join(names)
        export = f"from {a.module} import (  # noqa: F401  # 2026-09-07 重構：原名 re-export，消費者不動\n    {body},\n)"
    out_lines.insert(j + 1, export)
    new_src = "\n".join(out_lines)

    print(f"搬移 {len(names)} 個定義、{moved_lines} 行：{src_path.name} → {tgt_path}")
    print(f"re-export 插在 {src_path.name} 第 {j + 2} 行")
    if a.dry_run:
        return 0
    tgt_path.parent.mkdir(parents=True, exist_ok=True)
    tgt_path.write_text(target_text.replace("\n", newline), encoding="utf-8", newline="")
    src_path.write_text(new_src.replace("\n", newline), encoding="utf-8", newline="")
    # 語法驗證
    for p in (src_path, tgt_path):
        ast.parse(p.read_text(encoding="utf-8"))
    print("兩檔 AST 解析 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
