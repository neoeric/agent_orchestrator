"""iface_gate.py — 簽章閘門（v3 第 4 節第一層）：比對 .py 檔改動前後的公開介面，純 AST，不 import 受測程式碼。

公開介面＝頂層不以底線開頭的 def／async def／class，以及 class 內不以底線開頭的方法。
判定：
  breaking ＝ 移除公開名稱、必填參數增加、參數改名／移除、回傳型別註記改變
  additive ＝ 新增公開名稱、新增選填參數
兩者都沒有 ＝ 純內部改動（重構、私有函式）。

用法（程式）：compare_sources(old_src, new_src) -> {"breaking": [...], "additive": [...]}
用法（CLI）：python iface_gate.py <repo_worktree> <base_ref>   → 對 git 內所有改動的 .py 逐檔比對
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


def _sig(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict:
    a = fn.args
    pos = [x.arg for x in a.posonlyargs + a.args]
    n_default = len(a.defaults)
    required = pos[: len(pos) - n_default] if n_default else pos
    optional = pos[len(pos) - n_default:] if n_default else []
    kwonly = [x.arg for x in a.kwonlyargs]
    kw_required = [x.arg for x, d in zip(a.kwonlyargs, a.kw_defaults) if d is None]
    return {
        "required": [p for p in required if p not in ("self", "cls")],
        "optional": optional,
        "kwonly": kwonly, "kw_required": kw_required,
        "vararg": a.vararg.arg if a.vararg else None,
        "kwarg": a.kwarg.arg if a.kwarg else None,
        "returns": ast.unparse(fn.returns) if fn.returns is not None else None,
    }


def public_api(src: str) -> dict[str, dict]:
    tree = ast.parse(src)
    out: dict[str, dict] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            out[node.name] = _sig(node)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            out[node.name] = {"class": True}
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and not sub.name.startswith("_"):
                    out[f"{node.name}.{sub.name}"] = _sig(sub)
    return out


def compare_api(old: dict[str, dict], new: dict[str, dict]) -> dict[str, list[str]]:
    breaking, additive = [], []
    for name in old:
        if name not in new:
            breaking.append(f"removed: {name}")
            continue
        o, n = old[name], new[name]
        if o.get("class") or n.get("class"):
            continue
        for p in n["required"]:
            if p not in o["required"] and p not in o["optional"]:
                breaking.append(f"{name}: required param added -> {p}")
        for p in o["required"] + o["optional"]:
            if p not in n["required"] and p not in n["optional"]:
                breaking.append(f"{name}: param removed -> {p}")
        for p in o["optional"]:
            if p in n["required"]:
                breaking.append(f"{name}: optional param became required -> {p}")
        for p in n["optional"]:
            if p not in o["required"] and p not in o["optional"]:
                additive.append(f"{name}: optional param added -> {p}")
        if o["returns"] != n["returns"]:
            breaking.append(f"{name}: return type changed -> {o['returns']} => {n['returns']}")
        for p in n["kw_required"]:
            if p not in o["kwonly"]:
                breaking.append(f"{name}: required kw-only param added -> {p}")
    for name in new:
        if name not in old:
            additive.append(f"added: {name}")
    return {"breaking": breaking, "additive": additive}


def compare_sources(old_src: str, new_src: str) -> dict[str, list[str]]:
    try:
        return compare_api(public_api(old_src), public_api(new_src))
    except SyntaxError as e:
        return {"breaking": [f"syntax error, cannot compare: {e}"], "additive": []}


def gate_worktree(worktree: str, base_ref: str) -> dict[str, dict[str, list[str]]]:
    """對 worktree 裡相對 base_ref 有改動的 .py 逐檔比對（含新增檔＝全部 additive）。"""
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", worktree, *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace").stdout
    changed = [l for l in git("diff", "--name-only", base_ref).splitlines() if l.endswith(".py")]
    untracked = [l for l in git("ls-files", "--others", "--exclude-standard").splitlines() if l.endswith(".py")]
    result: dict[str, dict[str, list[str]]] = {}
    for path in changed:
        old = git("show", f"{base_ref}:{path}")
        p = Path(worktree) / path
        new = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        if not new:
            result[path] = {"breaking": ["file removed"], "additive": []}
            continue
        result[path] = compare_sources(old, new)
    for path in untracked:
        new = (Path(worktree) / path).read_text(encoding="utf-8", errors="replace")
        result[path] = compare_sources("", new)
    return result


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if len(argv) != 2:
        print(__doc__)
        return 2
    res = gate_worktree(argv[0], argv[1])
    n_break = sum(len(v["breaking"]) for v in res.values())
    for path, r in res.items():
        print(f"{path}: breaking={len(r['breaking'])} additive={len(r['additive'])}")
        for b in r["breaking"]:
            print("   BREAKING", b)
        for a in r["additive"]:
            print("   additive", a)
    print(f"total breaking={n_break}")
    return 1 if n_break else 0


if __name__ == "__main__":
    sys.exit(main())
