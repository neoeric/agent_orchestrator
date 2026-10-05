"""notify_telegram.py — relay 推播通道轉發腳本（Telegram；只用標準函式庫）。

2026-10-05：relay 不內建任何通道，只在 notify.json 的 cmd 呼叫外部指令並經 stdin 交訊息；這支就是其中一種。
用法（stdin 是訊息本文，UTF-8）：
    python tools/notify_telegram.py [--env-file 路徑]

憑證只讀兩個環境變數（不經命令列參數，避免被程序列表、shell 歷史、權限設定檔記錄）：
    TELEGRAM_ALERT_TOKEN   bot token
    TELEGRAM_ALERT_TO      聊天室 id
環境變數優先；沒有時才讀 --env-file（`KEY=VALUE` 逐行，`#` 開頭為註解，值可用單／雙引號包住）。
exit 0 只有「Telegram 回 HTTP 200」；缺憑證、網路錯誤、非 200 都回非 0（讓 relay 記帳本，但不影響那一棒的結果）。
🔴 任何輸出、錯誤訊息都不得出現 token（含 URL 裡的 bot<token>）：一律經 mask() 處理。
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

TOKEN_VAR = "TELEGRAM_ALERT_TOKEN"
CHAT_VAR = "TELEGRAM_ALERT_TO"
MAX_LEN = 4096                 # Telegram sendMessage 單則文字上限（字元）
TRUNC_NOTE = "\n…（已截斷）"
API = "https://api.telegram.org/bot{token}/sendMessage"
HTTP_TIMEOUT = 20


def load_env_file(path: str) -> dict[str, str]:
    """解析 KEY=VALUE 逐行檔；空行與 # 註解略過；值兩側成對引號去掉。讀不到檔就丟 OSError（呼叫端處理）。"""
    out: dict[str, str] = {}
    with open(path, encoding="utf-8-sig") as f:  # utf-8-sig：記事本存的檔開頭可能有 BOM
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[k] = v
    return out


def get_credentials(environ, env_file: str | None) -> tuple[str, str, list[str]]:
    """回 (token, chat, 缺少的變數名)。只回變數名，不把值放進任何訊息。"""
    src = dict(environ)
    if env_file:
        file_vals = load_env_file(env_file)
        for k in (TOKEN_VAR, CHAT_VAR):
            if not src.get(k) and file_vals.get(k):
                src[k] = file_vals[k]
    token, chat = src.get(TOKEN_VAR, ""), src.get(CHAT_VAR, "")
    missing = [n for n, v in ((TOKEN_VAR, token), (CHAT_VAR, chat)) if not v]
    return token, chat, missing


def truncate(text: str) -> str:
    if len(text) <= MAX_LEN:
        return text
    return text[: MAX_LEN - len(TRUNC_NOTE)] + TRUNC_NOTE


def mask(s: str, token: str) -> str:
    """把 token 遮掉（含 URL 裡的 bot<token>）。"""
    return s.replace(token, "***") if token else s


def _post(url: str, data: bytes, timeout: float) -> tuple[int, str]:
    """送出 POST，回 (HTTP 狀態碼, 回應本文)；非 2xx 的 HTTPError 也轉成正常回傳。測試時整個換掉，不連網。"""
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main(argv=None, environ=None, stdin=None) -> int:
    ap = argparse.ArgumentParser(description="從 stdin 讀訊息，經 Telegram bot 發出（憑證讀環境變數或 --env-file）")
    ap.add_argument("--env-file", help=f"KEY=VALUE 逐行檔，提供 {TOKEN_VAR}／{CHAT_VAR}（環境變數優先）")
    a = ap.parse_args(argv)
    try:
        token, chat, missing = get_credentials(os.environ if environ is None else environ, a.env_file)
    except OSError as e:
        print(f"NOTIFY telegram error: 讀不了 --env-file（{type(e).__name__}）", file=sys.stderr)
        return 2
    if missing:
        print("NOTIFY telegram error: 缺少 " + "、".join(missing) + "（環境變數或 --env-file 擇一提供）", file=sys.stderr)
        return 2
    if stdin is None:
        text = sys.stdin.buffer.read().decode("utf-8", "replace")
    else:
        text = stdin.read()
    text = truncate(text.strip("\r\n"))
    if not text.strip():
        print("NOTIFY telegram error: stdin 沒有訊息內容", file=sys.stderr)
        return 2
    url = API.format(token=token)
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode("utf-8")
    try:
        code, body = _post(url, data, HTTP_TIMEOUT)
    except Exception as e:  # 網路錯誤、DNS、逾時…：例外文字可能夾帶含 token 的 URL，遮掉再印
        print(f"NOTIFY telegram error: {type(e).__name__}: {mask(str(e), token)[:200]}", file=sys.stderr)
        return 1
    print(f"NOTIFY telegram http={code}")
    if code != 200:
        print("NOTIFY telegram error: " + mask(body, token).replace("\n", " ")[:200], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
