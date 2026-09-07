"""agy_review.py — 用 Antigravity CLI（agy）當唯讀審查者，diff 分塊餵進同一個對話（重構棒 3 用）。

為什麼要分塊：Windows 命令列上限約 32K 字元，`agy -p "<整包 diff>"` 超過就 exit 126 根本沒啟動；
而本機實測 agy 無頭模式**不讀 stdin**（redirect 與 pipe 都回 NO_STDIN），又不能給檔案路徑
（headless 讀檔工具會被軟拒 → status=SUCCESS 但 response 空）。`--continue` 能沿用上一輪對話，
所以：第 1..N 輪各送一塊 diff 要它「只回 OK」，第 N+1 輪送審查指令。

每一輪都用 -p 明說「不要使用任何工具」，避免它想跑指令被無頭模式自動拒絕後回空 response。

用法：
  python agy_review.py --instructions review_prompt.txt --diff diff.txt --out review.stdout.txt [--chunk 18000]
離開碼：0＝拿到非空審查；1＝審查失敗／回空；2＝參數／執行錯誤。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

AGY = os.environ.get("AGY_EXE", r"C:\Users\<user>\AppData\Local\agy\bin\agy.exe")
NO_TOOLS = "不要使用任何工具：不要執行指令、不要讀檔、不要搜尋，只根據對話內容作答。"


def run_agy(prompt: str, cont: bool, timeout: int) -> tuple[dict | None, str, int]:
    cmd = [AGY]
    if cont:
        cmd.append("--continue")
    cmd += ["-p", prompt, "--output-format", "json"]
    cp = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=timeout, stdin=subprocess.DEVNULL)
    obj = None
    for line in cp.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                pass
    return obj, cp.stderr, cp.returncode


def chunks(text: str, size: int) -> list[str]:
    out, buf = [], []
    n = 0
    for line in text.splitlines(keepends=True):
        b = len(line.encode("utf-8"))
        if n + b > size and buf:
            out.append("".join(buf))
            buf, n = [], 0
        buf.append(line)
        n += b
    if buf:
        out.append("".join(buf))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instructions", required=True)
    ap.add_argument("--diff", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--chunk", type=int, default=18000, help="每塊上限（UTF-8 bytes）")
    ap.add_argument("--timeout", type=int, default=600)
    a = ap.parse_args(argv)

    instr = Path(a.instructions).read_text(encoding="utf-8")
    diff = Path(a.diff).read_text(encoding="utf-8")
    parts = chunks(diff, a.chunk)
    t0 = time.monotonic()
    for i, part in enumerate(parts, 1):
        p = (f"{NO_TOOLS}\n這是待審 diff 的第 {i}/{len(parts)} 段，請完整記住內容，只回覆「OK {i}」，不要分析。\n"
             f"=== DIFF PART {i}/{len(parts)} BEGIN ===\n{part}\n=== DIFF PART {i}/{len(parts)} END ===")
        obj, err, rc = run_agy(p, cont=(i > 1), timeout=a.timeout)
        resp = (obj or {}).get("response", "")
        print(f"chunk {i}/{len(parts)}: rc={rc} status={(obj or {}).get('status')} resp={resp.strip()[:20]!r} "
              f"input={(obj or {}).get('usage', {}).get('input_tokens')}")
        if not obj or obj.get("status") != "SUCCESS" or not resp.strip():
            print("chunk 送入失敗；stderr：", err.strip()[:300], file=sys.stderr)
            return 1
    final = (f"{NO_TOOLS}\n以上 {len(parts)} 段就是完整 diff。現在請照下面的審查指令作答：\n\n{instr}")
    obj, err, rc = run_agy(final, cont=(len(parts) > 0), timeout=a.timeout)
    Path(a.out).write_text(json.dumps(obj, ensure_ascii=False, indent=1) if obj else "", encoding="utf-8")
    resp = (obj or {}).get("response", "")
    print(f"review: rc={rc} status={(obj or {}).get('status')} len={len(resp)} usage={(obj or {}).get('usage')} "
          f"elapsed={time.monotonic() - t0:.0f}s")
    if not obj or obj.get("status") != "SUCCESS" or not resp.strip():
        print("審查回空或失敗；stderr：", err.strip()[:300], file=sys.stderr)
        return 1
    print("=" * 70)
    print(resp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
