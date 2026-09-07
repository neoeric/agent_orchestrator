"""_test_judge.py — judge.py 的契約測試（常設，每次 CLI 升版就重跑）。

fixtures/ 裡是 2026-09-07 在本機抓的真實樣本（來源與指令見 fixtures/README.md）。
CLI 一升版、欄位語意一變，這裡就會紅燈；沒有這組測試，編排器會默默把失敗記成成功。

跑法：PYTHONUTF8=1 python _test_judge.py   （exit 0＝全過）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import judge  # noqa: E402

HERE = Path(__file__).resolve().parent
FX = HERE / "fixtures"

PASSED = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"[PASS] {name}")
    else:
        FAILED.append(name)
        print(f"[FAIL] {name}" + (f"\n       {detail}" if detail else ""))


def read(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8", errors="replace")


def read_exit(name: str) -> int:
    return int(read(name).strip().split("=")[-1])


# ---------------------------------------------------------------------------
# 1. 七個真實樣本：預期判定 + 分類 + 狀態碼 + 矛盾
# ---------------------------------------------------------------------------

def test_real_fixtures() -> None:
    v = judge.judge("claude", read("claude_ok.stdout.txt"), read("claude_ok.stderr.txt"),
                    read_exit("claude_ok.exit.txt"))
    check("claude_ok → ok", v.ok, v.reason)
    check("claude_ok result_text=OK", v.result_text == "OK", repr(v.result_text))
    check("claude_ok 無矛盾", not v.contradictions, str(v.contradictions))
    check("claude_ok 有 usage 且 input_tokens_total>0",
          bool(v.usage) and v.usage["input_tokens_total"] > 0, str(v.usage))

    v = judge.judge("claude", read("claude_err.stdout.txt"), read("claude_err.stderr.txt"),
                    read_exit("claude_err.exit.txt"))
    check("claude_err → not ok", not v.ok)
    check("claude_err http_status=404", v.http_status == 404, str(v.http_status))
    check("claude_err failure_class=config", v.failure_class == judge.FAIL_CONFIG, str(v.failure_class))
    check("claude_err 記到 subtype/is_error 矛盾",
          any("subtype=success" in c for c in v.contradictions), str(v.contradictions))

    v = judge.judge("codex", read("codex_ok.stdout.txt"), read("codex_ok.stderr.txt"),
                    read_exit("codex_ok.exit.txt"))
    check("codex_ok → ok", v.ok, v.reason)
    check("codex_ok result_text=OK", v.result_text == "OK", repr(v.result_text))
    check("codex_ok usage.input_tokens_total>0",
          bool(v.usage) and v.usage["input_tokens_total"] > 0, str(v.usage))
    check("codex_ok 無矛盾", not v.contradictions, str(v.contradictions))

    v = judge.judge("codex", read("codex_err.stdout.txt"), read("codex_err.stderr.txt"),
                    read_exit("codex_err.exit.txt"))
    check("codex_err → not ok", not v.ok)
    check("codex_err 從 turn.failed 夾帶的上游 JSON 讀到 status=400", v.http_status == 400, str(v.http_status))
    check("codex_err failure_class=config", v.failure_class == judge.FAIL_CONFIG, str(v.failure_class))
    check("codex_err item error 進 warnings", any("item error" in w for w in v.warnings), str(v.warnings))

    v = judge.judge("gemini", read("gemini_noauth_exit41.txt"), "", 41)
    check("gemini_noauth → not ok", not v.ok)
    check("gemini_noauth failure_class=auth", v.failure_class == judge.FAIL_AUTH, str(v.failure_class))
    check("gemini_noauth http_status=None（41 是 CLI 內部碼）", v.http_status is None, str(v.http_status))

    v = judge.judge("gemini", read("gemini_badkey_exit144.txt"), "", 144)
    check("gemini_badkey → not ok", not v.ok)
    check("gemini_badkey 在 15 行雜訊後仍抓到最後的 JSON", v.json_objects == 1 and v.noise_lines >= 10,
          f"json_objects={v.json_objects} noise={v.noise_lines}")
    check("gemini_badkey http_status=400", v.http_status == 400, str(v.http_status))
    check("gemini_badkey failure_class=auth（API_KEY_INVALID 雖是 400 但本質是認證）",
          v.failure_class == judge.FAIL_AUTH, str(v.failure_class))
    check("gemini_badkey exit 144 與 400 mod 256 一致 ⇒ 無矛盾", not v.contradictions, str(v.contradictions))

    v = judge.judge("gemini", read("gemini_gca_exit1.txt"), "", 1)
    check("gemini_gca → not ok", not v.ok)
    check("gemini_gca failure_class=no_json", v.failure_class == judge.FAIL_NO_JSON, str(v.failure_class))
    check("gemini_gca json_objects=0", v.json_objects == 0, str(v.json_objects))


# ---------------------------------------------------------------------------
# 2. 天真寫法對照組：證明陷阱真的會中，判定器不是「一律判失敗」的假防線
# ---------------------------------------------------------------------------

def test_naive_contrast() -> None:
    err = read("claude_err.stdout.txt")
    naive_subtype_ok = json.loads(err.strip())["subtype"] == "success"
    check("對照：只看 subtype 會把 claude_err 當成功", naive_subtype_ok is True)
    check("判定器對 claude_err 判失敗", not judge.judge("claude", err).ok)

    for name in ("gemini_badkey_exit144.txt", "gemini_gca_exit1.txt"):
        raw = read(name)
        try:
            json.loads(raw)
            whole_parse_raised = False
        except json.JSONDecodeError:
            whole_parse_raised = True
        check(f"對照：整包解析 {name} 會拋例外", whole_parse_raised)

    ok = read("claude_ok.stdout.txt")
    check("判定器對 claude_ok 仍判成功（不是一律失敗）", judge.judge("claude", ok).ok)


# ---------------------------------------------------------------------------
# 3. Gemini 成功形狀：⚠️ 合成樣本，待金鑰後換成真樣本
# ---------------------------------------------------------------------------

def test_gemini_success_shape_synthetic() -> None:
    synthetic = json.dumps({
        "session_id": "00000000-0000-0000-0000-000000000000",
        "response": "OK",
        "stats": {"models": {"gemini-2.5-pro": {"tokens": {"prompt": 1234, "candidates": 2, "total": 1236}}}},
    }, indent=2)
    noisy = "Warning: True color (24-bit) support not detected.\n" + synthetic + "\n"
    v = judge.judge("gemini", noisy, "", 0)
    check("gemini 合成成功樣本 → ok（⚠️待真樣本驗證）", v.ok, v.reason)
    check("gemini 合成成功樣本 usage.input_tokens_total=1234",
          bool(v.usage) and v.usage["input_tokens_total"] == 1234, str(v.usage))
    check("gemini 合成成功樣本 noise_lines=1", v.noise_lines == 1, str(v.noise_lines))

    empty = json.dumps({"session_id": "x", "response": "   "})
    check("gemini response 空白 → not ok", not judge.judge("gemini", empty, "", 0).ok)


# ---------------------------------------------------------------------------
# 4. exit code 只當佐證：不改判定，但要記矛盾
# ---------------------------------------------------------------------------

def test_exit_code_is_evidence_not_boolean() -> None:
    ok = read("claude_ok.stdout.txt")
    v = judge.judge("claude", ok, "", 1)
    check("claude_ok 但 exit=1 → 仍判 ok", v.ok)
    check("claude_ok 但 exit=1 → 記矛盾", any("exit code=1" in c for c in v.contradictions), str(v.contradictions))

    bad = read("gemini_badkey_exit144.txt")
    v = judge.judge("gemini", bad, "", 173)  # 173 是 429 mod 256，與 400 不符
    check("gemini http 400 但 exit=173 → 記 mod 256 不符的矛盾",
          any("mod 256" in c for c in v.contradictions), str(v.contradictions))

    v = judge.judge("claude", read("claude_err.stdout.txt"), "", 0)
    check("claude_err 但 exit=0 → 記矛盾", any("exit code=0" in c for c in v.contradictions), str(v.contradictions))


# ---------------------------------------------------------------------------
# 5. JSON 擷取器本身
# ---------------------------------------------------------------------------

def test_extract_json_values() -> None:
    crlf = "noise line\r\n{\r\n  \"a\": 1,\r\n  \"b\": [1, 2]\r\n}\r\ntrailing noise\r\n"
    vals, noise = judge.extract_json_values(crlf)
    check("CRLF 多行 JSON 解得出來", vals == [{"a": 1, "b": [1, 2]}], str(vals))
    check("CRLF 雜訊行數=2", noise == 2, str(noise))

    jsonl = '{"type":"a"}\n{"type":"b","x":"{\\"nested\\":1}"}\n'
    vals, noise = judge.extract_json_values(jsonl)
    check("JSONL 兩行各解一個物件", len(vals) == 2 and vals[1]["type"] == "b", str(vals))
    check("JSONL 雜訊=0", noise == 0, str(noise))

    broken = "{ not json\n{\"ok\": true}\n"
    vals, noise = judge.extract_json_values(broken)
    check("壞掉的 { 行算雜訊、後面的好 JSON 照抓", vals == [{"ok": True}] and noise == 1, f"{vals} noise={noise}")

    vals, noise = judge.extract_json_values("")
    check("空字串 → 0 值 0 雜訊", vals == [] and noise == 0)


# ---------------------------------------------------------------------------
# 6. Codex 事件流的邊界
# ---------------------------------------------------------------------------

def test_codex_edge_cases() -> None:
    both = ('{"type":"turn.completed","usage":{"input_tokens":10}}\n'
            '{"type":"turn.failed","error":{"message":"x"}}\n')
    v = judge.judge("codex", both, "", 1)
    check("codex 同時 completed 與 failed → not ok 且記矛盾",
          not v.ok and any("同時" in c for c in v.contradictions), str(v.contradictions))

    no_msg = '{"type":"turn.started"}\n{"type":"turn.completed","usage":{"input_tokens":10}}\n'
    v = judge.judge("codex", no_msg, "", 0)
    check("codex completed 但沒有 agent_message → not ok（白名單）", not v.ok, v.reason)

    rate = ('{"type":"error","message":"{\\"type\\":\\"error\\",\\"status\\":429,\\"error\\":{\\"message\\":\\"slow down\\"}}"}\n'
            '{"type":"turn.failed","error":{"message":"{\\"status\\":429}"}}\n')
    v = judge.judge("codex", rate, "", 1)
    check("codex 上游 429 → failure_class=rate_limit（合成，真 429 尚未觀察到）",
          v.failure_class == judge.FAIL_RATE_LIMIT and v.http_status == 429, f"{v.failure_class} {v.http_status}")


# ---------------------------------------------------------------------------
# 7. Antigravity agy：真實樣本三個（未登入、預設模式成功、plan 模式）＋合成矛盾樣本
# ---------------------------------------------------------------------------

def test_agy() -> None:
    v = judge.judge("agy", read("agy_unauth.stdout.txt"), read("agy_unauth.stderr.txt"),
                    read_exit("agy_unauth.exit.txt"))
    check("agy_unauth → not ok", not v.ok)
    check("agy_unauth failure_class=auth", v.failure_class == judge.FAIL_AUTH, str(v.failure_class))
    check("agy_unauth stdout 是乾淨單行 JSON（雜訊在 stderr）", v.json_objects == 1 and v.noise_lines == 0,
          f"json_objects={v.json_objects} noise={v.noise_lines}")
    check("agy_unauth 無矛盾（status=ERROR 且 error 非空）", not v.contradictions, str(v.contradictions))
    check("agy_unauth stderr 進 warnings", any("stderr" in w for w in v.warnings), str(v.warnings))

    # 真樣本（9/07 登入後，預設模式）：response="OK\n"、input 5,139＋cache_read 8,128
    v = judge.judge("agy", read("agy_ok.stdout.txt"), read("agy_ok.stderr.txt"), read_exit("agy_ok.exit.txt"))
    check("agy_ok → ok", v.ok, v.reason)
    check("agy_ok result_text=OK", (v.result_text or "").strip() == "OK", repr(v.result_text))
    check("agy_ok 無矛盾、無警告（stderr 空）", not v.contradictions and not v.warnings,
          f"{v.contradictions} {v.warnings}")
    check("agy_ok usage：input_tokens_total = input + cache_read（快取量比 input 還大，是外加不是子集）",
          bool(v.usage) and v.usage["input_tokens_total"] == v.usage["input_tokens_uncached"] + v.usage["cache_read_tokens"]
          and v.usage["cache_read_tokens"] > v.usage["input_tokens_uncached"] > 0, str(v.usage))

    # 真樣本（--mode plan）：同一句「回覆 OK」不回答、改寫 plan.md 要人確認 ⇒ 判定器只看「呼叫正常結束」，仍 ok；
    # 「有沒有真的回答」是上層（任務落地檢查）的事，這正是「CLI 自我回報不是最終事實」
    v = judge.judge("agy", read("agy_ok_plan.stdout.txt"), read("agy_ok_plan.stderr.txt"), read_exit("agy_ok_plan.exit.txt"))
    check("agy_ok_plan → ok（呼叫層面）", v.ok, v.reason)
    check("agy_ok_plan 的 response 是計畫提示而不是 OK（上層要自己驗落地）",
          "plan.md" in (v.result_text or "") and (v.result_text or "").strip() != "OK", repr(v.result_text)[:80])

    lying = json.dumps({"status": "SUCCESS", "response": "OK", "error": "something went wrong"})
    v = judge.judge("agy", lying, "", 0)
    check("agy status=SUCCESS 但 error 非空 → not ok 且記矛盾",
          not v.ok and any("SUCCESS" in c for c in v.contradictions), str(v.contradictions))

    empty = json.dumps({"status": "SUCCESS", "response": "", "error": ""})
    check("agy SUCCESS 但 response 空 → not ok（白名單）", not judge.judge("agy", empty, "", 0).ok)

    check("agy 完全沒 JSON → no_json",
          judge.judge("agy", "Authentication required...\n", "", 1).failure_class == judge.FAIL_NO_JSON)


def main() -> int:
    for fn in (test_real_fixtures, test_naive_contrast, test_gemini_success_shape_synthetic,
               test_exit_code_is_evidence_not_boolean, test_extract_json_values, test_codex_edge_cases,
               test_agy):
        print(f"--- {fn.__name__} ---")
        fn()
    print(f"\n{PASSED} passed / {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  ✗ {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
