"""check_ps1_encoding.py — 驗 .ps1 改動後的編碼紀律（CLAUDE.md §4：BOM 剛好一個、不能雙 BOM、換行符不變）。

對 worktree 裡相對 <base_ref> 有改動的 .ps1（或指定檔）：
  1. BOM 數與 base 版相同（有 BOM 的維持 1、沒有的維持 0；絕不可變 2）
  2. 檔內換行符不混用（index 是 LF、工作樹是 CRLF，所以不跟 base 比）
  3. 無控制字元（BEL、退格那類跳脫事故）
  4. [PSParser]::Tokenize errors=0
用法：python check_ps1_encoding.py <worktree> <base_ref> [file ...]
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def bom_count(b: bytes) -> int:
    n = 0
    while b[n * 3:(n + 1) * 3] == b"\xef\xbb\xbf":
        n += 1
    return n


def eol(b: bytes) -> str:
    return "CRLF" if b"\r\n" in b else ("LF" if b"\n" in b else "none")


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    wt, base = argv[0], argv[1]
    files = argv[2:]
    if not files:
        out = subprocess.run(["git", "-C", wt, "diff", "--name-only", base], capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout
        files = [l for l in out.splitlines() if l.lower().endswith(".ps1")]
    bad = 0
    for f in files:
        new = (Path(wt) / f).read_bytes()
        old = subprocess.run(["git", "-C", wt, "show", f"{base}:{f}"], capture_output=True).stdout
        problems = []
        if old and bom_count(old) != bom_count(new):
            problems.append(f"BOM {bom_count(old)}→{bom_count(new)}")
        if bom_count(new) > 1:
            problems.append("雙 BOM")
        # 不拿 git show 比換行符：index 存 LF、工作樹因 autocrlf 是 CRLF，比了必假警報。改驗「檔內不混用」
        crlf = new.count(b"\r\n")
        bare_lf = new.count(b"\n") - crlf
        if crlf and bare_lf:
            problems.append(f"換行符混用（CRLF {crlf}／LF {bare_lf}）")
        ctrl = sum(1 for c in new if c < 9 or (10 < c < 32 and c != 13))
        if ctrl:
            problems.append(f"控制字元 {ctrl}")
        ps = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                             f"$e=$null;[System.Management.Automation.PSParser]::Tokenize((Get-Content '{Path(wt) / f}' -Raw),[ref]$e)|Out-Null;$e.Count"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        try:
            errs = int(ps.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            errs = -1
        if errs != 0:
            problems.append(f"PSParser errors={errs}")
        print(f"{'FAIL' if problems else 'PASS'} {f}: BOM={bom_count(new)} {eol(new)} " + ("; ".join(problems) if problems else "OK"))
        bad += bool(problems)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
