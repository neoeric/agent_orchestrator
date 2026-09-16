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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402
AGY = os.environ.get("AGY_EXE") or paths.resolve_agy() or "agy"
NO_TOOLS = "不要使用任何工具：不要執行指令、不要讀檔、不要搜尋，只根據對話內容作答。"


def run_agy(prompt: str, conv: str | None, timeout: int) -> tuple[dict | None, str, int]:
    """conv=None ⇒ 開新對話；conv="__continue__" ⇒ `--continue`（舊行為，備援）；其他 ⇒ `--conversation <id>`。

    2026-09-16 踩到：`--continue` 接的是「最近一個對話」，使用者同時在終端用 agy（登入／互動）時，
    第 2 塊起會接到別人的對話 ⇒ 審查者只看到最後一兩塊、前幾塊在別的對話裡，回「diff 缺 README／測試本體」
    的假退回（gw-politics-exempt 手動補審）。改用第 1 塊回傳的 conversation_id 釘住同一個對話。"""
    cmd = [AGY]
    if conv == "__continue__":
        cmd.append("--continue")
    elif conv:
        cmd += ["--conversation", conv]
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


def _split_long_line(line: str, size: int) -> list[str]:
    """單行就超過一塊上限時硬切成多段（UTF-8 位元組計、字元邊界），每段尾加續行標記。

    2026-09-14 實際踩到：gateway_reviewer 的 engine.py 第 76 行 RULE_VERSION 註解是一條 ~75KB 的
    單行，改版本號＝diff 帶新舊兩份 ⇒ 一塊 150KB ⇒ CreateProcess WinError 206「檔名或副檔名太長」，
    review 工具崩潰、relay 把崩潰當成 changes_requested 送實作者空轉一輪。不截斷（審查者要看到全文），只切。"""
    if len(line.encode("utf-8")) <= size:
        return [line]
    pieces, buf, n = [], [], 0
    body = line.rstrip("\n")
    for ch in body:
        b = len(ch.encode("utf-8"))
        if n + b > size and buf:
            pieces.append("".join(buf) + "⤶(此行未完，下段續)\n")
            buf, n = [], 0
        buf.append(ch)
        n += b
    pieces.append("".join(buf) + "\n")
    return pieces


def chunks(text: str, size: int) -> list[str]:
    out, buf = [], []
    n = 0
    for raw in text.splitlines(keepends=True):
        for line in _split_long_line(raw, size - 200):  # 留 200 bytes 給續行標記與換行
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
    conv_id: str | None = None      # 第 1 塊開新對話；之後用它回傳的 conversation_id 釘住（見 run_agy）
    for i, part in enumerate(parts, 1):
        p = (f"{NO_TOOLS}\n這是待審 diff 的第 {i}/{len(parts)} 段，請完整記住內容，只回覆「OK {i}」，不要分析。\n"
             f"=== DIFF PART {i}/{len(parts)} BEGIN ===\n{part}\n=== DIFF PART {i}/{len(parts)} END ===")
        obj, err, rc = run_agy(p, conv=(conv_id if i > 1 else None), timeout=a.timeout)
        resp = (obj or {}).get("response", "")
        print(f"chunk {i}/{len(parts)}: rc={rc} status={(obj or {}).get('status')} resp={resp.strip()[:20]!r} "
              f"input={(obj or {}).get('usage', {}).get('input_tokens')}")
        if not obj or obj.get("status") != "SUCCESS" or not resp.strip():
            print("chunk 送入失敗；stderr：", err.strip()[:300], file=sys.stderr)
            return 1
        if i == 1:
            conv_id = obj.get("conversation_id") or "__continue__"
            print(f"conversation={conv_id}")
    final = (f"{NO_TOOLS}\n以上 {len(parts)} 段就是完整 diff。現在請照下面的審查指令作答：\n\n{instr}")
    obj, err, rc = run_agy(final, conv=(conv_id if parts else None), timeout=a.timeout)
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
