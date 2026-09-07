"""deps_of.py — 列出模組裡指定頂層函式用到的「同模組頂層名稱」（函式、常數）與 from-import 名稱。

閉包圖只追蹤 def 之間的呼叫；常數（頂層 Assign）與 from X import name 的名稱看不到，
搬函式前要靠這支確認「還需要帶什麼」。純 AST，不 import 受測專案。

用法：python deps_of.py <module.py> name1,name2,...
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    path, names = Path(argv[0]), [n for n in argv[1].split(",") if n]
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    top_defs, top_consts, from_imports, mod_imports = {}, {}, {}, {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top_defs[node.name] = node
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    top_consts[t.id] = node.lineno
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            top_consts[node.target.id] = node.lineno
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                from_imports[a.asname or a.name] = node.module
        elif isinstance(node, ast.Import):
            for a in node.names:
                mod_imports[a.asname or a.name.split(".")[0]] = a.name
    for n in names:
        fn = top_defs.get(n)
        if fn is None:
            print(f"{n}: 不是頂層 def/class")
            continue
        used = {s.id for s in ast.walk(fn) if isinstance(s, ast.Name)}
        used.discard(n)
        defs = sorted(u for u in used if u in top_defs)
        consts = sorted(u for u in used if u in top_consts)
        froms = sorted(f"{u} (from {from_imports[u]})" for u in used if u in from_imports)
        mods = sorted(f"{u}" for u in used if u in mod_imports)
        print(f"{n}:")
        print(f"  同模組 def   : {defs or '—'}")
        print(f"  同模組常數   : {[f'{c}@{top_consts[c]}' for c in consts] or '—'}")
        print(f"  from-import  : {froms or '—'}")
        print(f"  模組 import  : {mods or '—'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
