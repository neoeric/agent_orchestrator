"""judge.py — 四支 CLI（claude / codex / gemini / agy）headless 回傳的白名單判定器。

設計原則（來自「多 Agent 協作方案 v3」審查第 5 節，以及 2026-09-07 本機補測）：

1. **白名單**：所有正面條件都成立才算成功，其餘一律失敗。不逐一修補失敗樣態（那是黑名單，
   新的失敗樣態一定會繞過去）。
2. **逐段找 JSON，不整包解析**：三支都會在 stdout 夾非 JSON 的診斷／警告行（成功樣本也有）。
   單行 JSON（Claude 的 result 物件、Codex 的 JSONL 事件）與多行 pretty JSON（Gemini）都要認得。
   **找不到任何 JSON 物件 ⇒ 失敗**（Gemini 的 GCA 被拒路徑就是這樣：只有 stack trace）。
3. **exit code 只當佐證，不當布林**：Gemini 的 exit code＝HTTP 狀態碼 mod 256（400→144、
   429→173、404→148），Claude／Codex 出錯回 1。只用它來偵測「判定結果與 exit code 矛盾」。
4. **矛盾要吵出來**：同一物件 `is_error=true` 卻 `subtype=success`（Claude 實測）這種 CLI 自己
   前後不一致的情況要記進 `contradictions`，不默默吸收。
5. **CLI 的自我回報只決定要不要重試**，不決定這一步做完了沒——那是測試與落地檢查的事。
   本模組的 `ok` 語意是「這次呼叫有正常拿到回覆」，不是「任務成功」。

⚠️ Gemini 的成功形狀（`response` 欄位）尚未拿到真樣本（本機待 API 金鑰），照官方 `-o json`
文件寫，`_test_judge.py` 用合成樣本標示為待驗證。

用法：
    python judge.py <claude|codex|gemini|agy> <stdout檔> [--stderr 檔] [--exit N | --exit-file 檔] [--json]
    exit code：0＝ok，1＝not ok，2＝參數／讀檔錯誤。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

CLIS = ("claude", "codex", "gemini", "agy")

# 失敗分類：給上層（重試／換手）用，不給人看的
FAIL_AUTH = "auth"            # 沒設認證、金鑰無效、層級被拒 ⇒ 換配置，重試沒用
FAIL_CONFIG = "config"        # 模型名錯之類的 4xx ⇒ 換配置，重試沒用
FAIL_RATE_LIMIT = "rate_limit"  # 429 ⇒ 撞牆，換手
FAIL_SERVER = "server"        # 5xx ⇒ 暫時性，可重試
FAIL_NO_JSON = "no_json"      # 完全沒有 JSON ⇒ CLI 崩了或形狀變了
FAIL_UNKNOWN = "unknown"


@dataclass
class Verdict:
    cli: str
    ok: bool
    reason: str                       # 一句話，給人看
    failure_class: str | None = None  # 上面的 FAIL_* 之一；ok 時為 None
    http_status: int | None = None    # 從回應裡讀到的 HTTP 狀態碼（若有）
    exit_code: int | None = None
    result_text: str | None = None    # 模型的回覆文字（ok 時）
    usage: dict[str, Any] | None = None
    contradictions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    noise_lines: int = 0              # stdout 裡不屬於任何 JSON 的非空行數
    json_objects: int = 0             # stdout 裡成功解出的 JSON 值個數

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# JSON 擷取：逐行掃，單行 JSON 直接解；解不了就從該行起 raw_decode（多行 pretty JSON）
# ---------------------------------------------------------------------------

def extract_json_values(text: str) -> tuple[list[Any], int]:
    """回傳 (依出現順序的 JSON 值清單, 雜訊行數)。

    雜訊行＝非空、又不屬於任何成功解出的 JSON 片段的行。
    """
    # 先把換行正規化成 \n，行偏移才會精確（Windows 的 \r\n 會讓偏移漂掉）
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    offsets: list[int] = []
    pos = 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1
    decoder = json.JSONDecoder()
    values: list[Any] = []
    consumed = [False] * len(lines)
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped or stripped[0] not in "{[":
            i += 1
            continue
        # 先試單行（Claude 的 result 物件、Codex 的 JSONL 事件）
        try:
            values.append(json.loads(stripped))
            consumed[i] = True
            i += 1
            continue
        except json.JSONDecodeError:
            pass
        # 再試從這一行起的多行片段（Gemini 的 pretty JSON）
        start = offsets[i] + (len(lines[i]) - len(lines[i].lstrip()))
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            i += 1
            continue
        values.append(value)
        end_line = text.count("\n", 0, end)
        for k in range(i, min(end_line, len(lines) - 1) + 1):
            consumed[k] = True
        i = end_line + 1
    noise = sum(1 for k, ln in enumerate(lines) if ln.strip() and not consumed[k])
    return values, noise


def _last_dict(values: list[Any], pred=lambda d: True) -> dict | None:
    for v in reversed(values):
        if isinstance(v, dict) and pred(v):
            return v
    return None


def _classify_http(status: int | None) -> str:
    if status is None:
        return FAIL_UNKNOWN
    if status == 429:
        return FAIL_RATE_LIMIT
    if status in (401, 403):
        return FAIL_AUTH
    if 400 <= status < 500:
        return FAIL_CONFIG
    if 500 <= status < 600:
        return FAIL_SERVER
    return FAIL_UNKNOWN


def _exit_consistency(v: Verdict) -> None:
    """exit code 與判定結果的一致性檢查，只記矛盾，不改判定。"""
    if v.exit_code is None:
        return
    if v.ok and v.exit_code != 0:
        v.contradictions.append(f"判定 ok 但 exit code={v.exit_code}")
    if not v.ok and v.exit_code == 0:
        v.contradictions.append("判定失敗但 exit code=0")
    if v.http_status is not None and v.exit_code not in (0, 1) and v.http_status % 256 != v.exit_code:
        v.contradictions.append(
            f"exit code={v.exit_code} 與 http_status={v.http_status}（mod 256={v.http_status % 256}）不符")


# ---------------------------------------------------------------------------
# Claude：`claude -p ... --output-format json`
# ---------------------------------------------------------------------------

def judge_claude(stdout: str, stderr: str = "", exit_code: int | None = None) -> Verdict:
    values, noise = extract_json_values(stdout)
    v = Verdict(cli="claude", ok=False, reason="", exit_code=exit_code,
                noise_lines=noise, json_objects=len(values))
    res = _last_dict(values, lambda d: d.get("type") == "result")
    if res is None:
        v.reason = "stdout 裡沒有 type=result 的 JSON 物件"
        v.failure_class = FAIL_NO_JSON
        _exit_consistency(v)
        return v

    is_error = res.get("is_error")
    api_status = res.get("api_error_status")
    terminal = res.get("terminal_reason")
    result_text = res.get("result")
    subtype = res.get("subtype")

    # 矛盾偵測（CLI 自己前後不一致）
    if is_error is True and subtype == "success":
        v.contradictions.append("subtype=success 但 is_error=true")
    if api_status is not None and is_error is not True:
        v.contradictions.append(f"api_error_status={api_status} 但 is_error 不是 true")

    # 白名單：四項全中才算成功；subtype 完全不用
    checks = {
        "is_error 為 false": is_error is False,
        "api_error_status 為空": api_status is None,
        "terminal_reason 是 completed 或不存在": terminal in (None, "completed"),
        "result 有內容": isinstance(result_text, str) and result_text.strip() != "",
    }
    failed = [name for name, passed in checks.items() if not passed]
    v.http_status = api_status if isinstance(api_status, int) else None
    v.usage = _claude_usage(res)
    if not failed:
        v.ok = True
        v.reason = "白名單四項全中"
        v.result_text = result_text
    else:
        v.reason = "未通過的條件：" + "；".join(failed)
        v.failure_class = _classify_http(v.http_status) if v.http_status else FAIL_UNKNOWN
        if isinstance(result_text, str) and result_text.strip():
            v.result_text = result_text  # 錯誤說明文字，留給人看
    if stderr.strip():
        v.warnings.append(f"stderr 有 {len(stderr.strip().splitlines())} 行")
    _exit_consistency(v)
    return v


def _claude_usage(res: dict) -> dict[str, Any] | None:
    u = res.get("usage")
    if not isinstance(u, dict):
        return None
    inp = int(u.get("input_tokens") or 0)
    cc = int(u.get("cache_creation_input_tokens") or 0)
    cr = int(u.get("cache_read_input_tokens") or 0)
    return {
        "input_tokens_total": inp + cc + cr,   # 固定開銷看這個（三者相加才是真正送進去的量）
        "input_tokens": inp,
        "cache_creation_input_tokens": cc,
        "cache_read_input_tokens": cr,
        "output_tokens": int(u.get("output_tokens") or 0),
        "total_cost_usd": res.get("total_cost_usd"),
        "models": sorted((res.get("modelUsage") or {}).keys()),
    }


# ---------------------------------------------------------------------------
# Codex：`codex exec --json ...`（JSONL 事件流）
# ---------------------------------------------------------------------------

def judge_codex(stdout: str, stderr: str = "", exit_code: int | None = None) -> Verdict:
    values, noise = extract_json_values(stdout)
    events = [x for x in values if isinstance(x, dict) and isinstance(x.get("type"), str)]
    v = Verdict(cli="codex", ok=False, reason="", exit_code=exit_code,
                noise_lines=noise, json_objects=len(values))
    if not events:
        v.reason = "stdout 裡沒有任何 JSONL 事件"
        v.failure_class = FAIL_NO_JSON
        _exit_consistency(v)
        return v

    completed = [e for e in events if e.get("type") == "turn.completed"]
    failed = [e for e in events if e.get("type") == "turn.failed"]
    errors = [e for e in events if e.get("type") == "error"]
    messages = [e["item"] for e in events
                if e.get("type") == "item.completed"
                and isinstance(e.get("item"), dict)
                and e["item"].get("type") == "agent_message"
                and isinstance(e["item"].get("text"), str)
                and e["item"]["text"].strip()]
    item_errors = [e["item"] for e in events
                   if e.get("type") == "item.completed"
                   and isinstance(e.get("item"), dict)
                   and e["item"].get("type") == "error"]
    for it in item_errors:
        v.warnings.append(f"item error: {str(it.get('message'))[:160]}")

    # 失敗訊息裡常夾上游 JSON（含 status）
    upstream_status = None
    for e in failed + errors:
        msg = e.get("error", {}).get("message") if isinstance(e.get("error"), dict) else e.get("message")
        if isinstance(msg, str):
            inner, _ = extract_json_values(msg)
            for obj in inner:
                if isinstance(obj, dict) and isinstance(obj.get("status"), int):
                    upstream_status = obj["status"]
    v.http_status = upstream_status

    checks = {
        "有 turn.completed": bool(completed),
        "沒有 turn.failed": not failed,
        "沒有 error 事件": not errors,
        "有非空的 agent_message": bool(messages),
    }
    unmet = [name for name, passed in checks.items() if not passed]
    if completed:
        v.usage = _codex_usage(completed[-1])
    if not unmet:
        v.ok = True
        v.reason = "白名單四項全中"
        v.result_text = messages[-1]["text"]
    else:
        v.reason = "未通過的條件：" + "；".join(unmet)
        v.failure_class = _classify_http(upstream_status) if upstream_status else FAIL_UNKNOWN
        if failed:
            v.result_text = str(failed[-1].get("error", {}).get("message"))[:400]
    if completed and failed:
        v.contradictions.append("同時有 turn.completed 與 turn.failed")
    if stderr.strip():
        v.warnings.append(f"stderr 有 {len(stderr.strip().splitlines())} 行")
    _exit_consistency(v)
    return v


def _codex_usage(evt: dict) -> dict[str, Any] | None:
    u = evt.get("usage")
    if not isinstance(u, dict):
        return None
    return {
        "input_tokens_total": int(u.get("input_tokens") or 0),  # Codex 的 input_tokens 已含快取部分
        "cached_input_tokens": int(u.get("cached_input_tokens") or 0),
        "output_tokens": int(u.get("output_tokens") or 0),
        "reasoning_output_tokens": int(u.get("reasoning_output_tokens") or 0),
        "total_cost_usd": None,  # Codex 不回成本
    }


# ---------------------------------------------------------------------------
# Gemini：`gemini -p ... -o json`（單一 pretty JSON 物件，前面可能夾警告與 stack trace）
# ---------------------------------------------------------------------------

def judge_gemini(stdout: str, stderr: str = "", exit_code: int | None = None) -> Verdict:
    values, noise = extract_json_values(stdout)
    v = Verdict(cli="gemini", ok=False, reason="", exit_code=exit_code,
                noise_lines=noise, json_objects=len(values))
    obj = _last_dict(values)
    if obj is None:
        v.reason = "stdout 裡沒有任何 JSON 物件（Gemini 崩潰路徑，例如 GCA 層級被拒只印 stack trace）"
        v.failure_class = FAIL_NO_JSON
        _exit_consistency(v)
        return v

    err = obj.get("error")
    if isinstance(err, dict):
        code = err.get("code")
        msg = str(err.get("message", ""))
        v.result_text = msg[:400]
        if code == 41:
            v.failure_class = FAIL_AUTH
            v.reason = "未設認證方式（Gemini CLI 內部碼 41）"
        elif isinstance(code, int) and 100 <= code < 600:
            v.http_status = code
            v.failure_class = _classify_http(code)
            # API_KEY_INVALID 是 400 但本質是認證問題
            if "API_KEY_INVALID" in msg or "API key not valid" in msg:
                v.failure_class = FAIL_AUTH
            v.reason = f"error.code={code}（{err.get('type')}）"
        else:
            v.failure_class = FAIL_UNKNOWN
            v.reason = f"error.code={code!r}（{err.get('type')}）"
        _exit_consistency(v)
        return v

    # 成功形狀（⚠️ 尚未以真樣本驗證，見檔頭）
    response = obj.get("response")
    checks = {
        "沒有 error 欄位": err is None,
        "response 有內容": isinstance(response, str) and response.strip() != "",
    }
    unmet = [name for name, passed in checks.items() if not passed]
    v.usage = _gemini_usage(obj)
    if not unmet:
        v.ok = True
        v.reason = "白名單兩項全中（成功形狀待真樣本驗證）"
        v.result_text = response
    else:
        v.reason = "未通過的條件：" + "；".join(unmet)
        v.failure_class = FAIL_UNKNOWN
    if stderr.strip():
        v.warnings.append(f"stderr 有 {len(stderr.strip().splitlines())} 行")
    _exit_consistency(v)
    return v


def _gemini_usage(obj: dict) -> dict[str, Any] | None:
    stats = obj.get("stats")
    if not isinstance(stats, dict):
        return None
    # 官方形狀：stats.models.<model>.tokens.{prompt,candidates,total,cached,...}
    total_in = 0
    total_out = 0
    models = stats.get("models")
    if isinstance(models, dict):
        for m in models.values():
            tk = m.get("tokens") if isinstance(m, dict) else None
            if isinstance(tk, dict):
                total_in += int(tk.get("prompt") or 0)
                total_out += int(tk.get("candidates") or 0)
    return {
        "input_tokens_total": total_in,
        "output_tokens": total_out,
        "total_cost_usd": None,
        "models": sorted(models.keys()) if isinstance(models, dict) else [],
    }


# ---------------------------------------------------------------------------
# Antigravity agy：`agy -p ... --output-format json`（單行 JSON；OAuth 提示走 stderr）
# 形狀（1.1.27 實測）：{"conversation_id","status","response","error","duration_seconds",
#                       "num_turns","usage":{input_tokens,output_tokens,thinking_tokens,
#                       cache_read_tokens,total_tokens}}
# ⚠️ status=SUCCESS 只代表這一輪對話正常結束，不代表副作用落地：另一台機器實測無頭模式
#    叫它建檔，回 SUCCESS 但檔案沒建（寫入被軟性拒絕）。落地與否要另外驗，不看這個欄位。
# ---------------------------------------------------------------------------

def judge_agy(stdout: str, stderr: str = "", exit_code: int | None = None) -> Verdict:
    values, noise = extract_json_values(stdout)
    v = Verdict(cli="agy", ok=False, reason="", exit_code=exit_code,
                noise_lines=noise, json_objects=len(values))
    obj = _last_dict(values, lambda d: "status" in d)
    if obj is None:
        v.reason = "stdout 裡沒有帶 status 欄位的 JSON 物件"
        v.failure_class = FAIL_NO_JSON
        _exit_consistency(v)
        return v

    status = obj.get("status")
    err = obj.get("error")
    response = obj.get("response")
    err_text = err if isinstance(err, str) else (json.dumps(err, ensure_ascii=False) if err else "")

    if status == "SUCCESS" and err_text.strip():
        v.contradictions.append("status=SUCCESS 但 error 非空")
    if status != "SUCCESS" and not err_text.strip():
        v.contradictions.append(f"status={status!r} 但 error 是空的")

    checks = {
        "status 為 SUCCESS": status == "SUCCESS",
        "error 為空": not err_text.strip(),
        "response 有內容": isinstance(response, str) and response.strip() != "",
    }
    unmet = [name for name, passed in checks.items() if not passed]
    v.usage = _agy_usage(obj)
    if not unmet:
        v.ok = True
        v.reason = "白名單三項全中（不代表副作用落地，見檔內註解）"
        v.result_text = response
    else:
        v.reason = "未通過的條件：" + "；".join(unmet)
        low = err_text.lower()
        if ("authentication" in low) or ("auth" in low and "fail" in low):
            v.failure_class = FAIL_AUTH
        elif "429" in low or "rate limit" in low or "quota" in low:
            v.failure_class = FAIL_RATE_LIMIT
            v.http_status = 429 if "429" in low else None
        else:
            v.failure_class = FAIL_UNKNOWN
        if err_text.strip():
            v.result_text = err_text[:400]
    if stderr.strip():
        v.warnings.append(f"stderr 有 {len(stderr.strip().splitlines())} 行")
    _exit_consistency(v)
    return v


def _agy_usage(obj: dict) -> dict[str, Any] | None:
    u = obj.get("usage")
    if not isinstance(u, dict):
        return None
    inp = int(u.get("input_tokens") or 0)
    cr = int(u.get("cache_read_tokens") or 0)
    # 口徑（1.1.27 實測）：total_tokens = input_tokens + output_tokens，cache_read_tokens 不在 total 內、
    # 且可以大於 input_tokens（實測 8,128 > 5,139）⇒ cache_read 是「外加的快取命中量」不是子集。
    # 要跟 Claude（input+cache_creation+cache_read）／Codex（input_tokens 已含 cached）比固定開銷，
    # 得用 input + cache_read。另一台 artifact 寫的「agy 約 5,150」是只算 input_tokens 的數字。
    return {
        "input_tokens_total": inp + cr,
        "input_tokens_uncached": inp,
        "cache_read_tokens": cr,
        "output_tokens": int(u.get("output_tokens") or 0),
        "thinking_tokens": int(u.get("thinking_tokens") or 0),
        "total_tokens": int(u.get("total_tokens") or 0),
        "total_cost_usd": None,
    }


# ---------------------------------------------------------------------------

JUDGES = {"claude": judge_claude, "codex": judge_codex, "gemini": judge_gemini, "agy": judge_agy}


def judge(cli: str, stdout: str, stderr: str = "", exit_code: int | None = None) -> Verdict:
    if cli not in JUDGES:
        raise ValueError(f"unknown cli {cli!r}; expected one of {CLIS}")
    return JUDGES[cli](stdout, stderr, exit_code)


def _read(path: str | None) -> str:
    if not path:
        return ""
    return Path(path).read_text(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cli", choices=CLIS)
    ap.add_argument("stdout_file")
    ap.add_argument("--stderr", dest="stderr_file")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--exit", dest="exit_code", type=int)
    g.add_argument("--exit-file", dest="exit_file", help="內容形如 exit=144 或純數字")
    ap.add_argument("--json", action="store_true", help="以 JSON 印出完整判定")
    a = ap.parse_args(argv)

    try:
        stdout = _read(a.stdout_file)
        stderr = _read(a.stderr_file)
        exit_code = a.exit_code
        if a.exit_file:
            raw = _read(a.exit_file).strip()
            exit_code = int(raw.split("=")[-1])
    except (OSError, ValueError) as e:
        print(f"judge: {e}", file=sys.stderr)
        return 2

    v = judge(a.cli, stdout, stderr, exit_code)
    if a.json:
        print(json.dumps(v.to_dict(), ensure_ascii=False, indent=2))
    else:
        flag = "OK " if v.ok else "FAIL"
        print(f"[{flag}] {v.cli}: {v.reason}")
        if v.http_status is not None:
            print(f"       http_status={v.http_status}")
        if v.usage:
            print(f"       input_tokens_total={v.usage.get('input_tokens_total')}")
        for c in v.contradictions:
            print(f"       ⚠ 矛盾: {c}")
        for w in v.warnings:
            print(f"       · {w}")
        print(f"       json_objects={v.json_objects} noise_lines={v.noise_lines}")
    return 0 if v.ok else 1


if __name__ == "__main__":
    sys.exit(main())
