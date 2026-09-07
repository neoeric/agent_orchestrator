"""_test_relay.py — relay 的 P2／P4 純邏輯測試：難易度判準、結構化審查解析、簽章閘門、帳本總計。

跑法：PYTHONUTF8=1 python _test_relay.py   （exit 0＝全過）
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import relay  # noqa: E402
from tools import iface_gate  # noqa: E402

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


def diff_of(n_lines: int) -> str:
    return "--- a/x.py\n+++ b/x.py\n" + "\n".join("+line" for _ in range(n_lines)) + "\n"


def test_decide_review() -> None:
    t_auto = {"review": {"policy": "auto"}, "core_paths": ["server.py", "applib/", "models/"]}
    ok, why = relay.decide_review({"review": {"policy": "always"}}, ["a.py"], diff_of(1), False, {})
    check("policy=always → 送審", ok and "always" in why)
    ok, why = relay.decide_review({"review": {"policy": "never"}}, ["a.py"] * 9, diff_of(999), True, {})
    check("policy=never → 不送審（即使很大）", not ok)
    ok, why = relay.decide_review(t_auto, ["_test_x.ps1"], diff_of(3), False, {})
    check("auto：1 檔 3 行、非核心、無失敗 → 跳過", not ok, why)
    ok, why = relay.decide_review(t_auto, ["a.py", "b.py", "c.py"], diff_of(3), False, {})
    check("auto：3 檔超過 max_files=2 → 送審", ok, why)
    ok, why = relay.decide_review(t_auto, ["a.py"], diff_of(61), False, {})
    check("auto：61 行超過 max_lines=60 → 送審", ok, why)
    ok, why = relay.decide_review(t_auto, ["applib/render.py"], diff_of(1), False, {})
    check("auto：碰核心目錄 applib/ → 送審", ok and "核心" in why, why)
    ok, why = relay.decide_review(t_auto, ["server.py"], diff_of(1), False, {})
    check("auto：碰核心檔 server.py → 送審", ok and "核心" in why, why)
    ok, why = relay.decide_review(t_auto, ["tools/x.py"], diff_of(1), True, {})
    check("auto：本任務曾驗證失敗 → 送審", ok and "驗證" in why, why)
    gate = {"tools/x.py": {"breaking": ["f: required param added -> z"], "additive": []}}
    ok, why = relay.decide_review(t_auto, ["tools/x.py"], diff_of(1), False, gate)
    check("auto：簽章閘門 breaking → 送審（介面變更不套用跳過規則）", ok and "介面" in why, why)
    t_custom = {"review": {"policy": "auto"}, "policy": {"max_files": 3, "max_lines": 20}}
    ok, why = relay.decide_review(t_custom, ["a", "b", "c"], diff_of(20), False, {})
    check("auto：任務自訂門檻 3 檔 20 行 → 跳過", not ok, why)


def test_parse_review() -> None:
    txt = "總判定：可合併\n\n1. 通過\n\n未申報問題\n無\n{\"verdict\":\"approve\",\"checks\":[{\"id\":1,\"result\":\"pass\"}],\"unreported\":[]}\n"
    r = relay.parse_review(txt)
    check("結構化 JSON：approve", r["structured"] and r["approved"] and r["checks"][0]["result"] == "pass", str(r))
    txt2 = "總判定：可合併\n...\n{\"verdict\":\"changes_requested\",\"checks\":[],\"unreported\":[\"孤兒註解\"]}"
    r = relay.parse_review(txt2)
    check("JSON 說 changes_requested 時以 JSON 為準（勝過第一行文字）", r["structured"] and not r["approved"] and r["unreported"] == ["孤兒註解"], str(r))
    r = relay.parse_review("總判定：需修改\n1. 不通過")
    check("無 JSON → 退回第一行：需修改", not r["structured"] and not r["approved"])
    r = relay.parse_review("總判定：可合併\n1. 通過")
    check("無 JSON → 退回第一行：可合併", not r["structured"] and r["approved"])
    r = relay.parse_review("")
    check("空回覆 → 不核准", not r["approved"])


def test_iface_gate() -> None:
    old = "def f(a, b=1):\n    pass\n\nclass K:\n    def m(self, x):\n        pass\n\ndef _private(q):\n    pass\n"
    new_same = old
    r = iface_gate.compare_sources(old, new_same)
    check("陰性對照：不變 → 0 breaking 0 additive", r == {"breaking": [], "additive": []}, str(r))
    new_add_opt = old.replace("def f(a, b=1):", "def f(a, b=1, c=None):")
    r = iface_gate.compare_sources(old, new_add_opt)
    check("加選填參數 → additive", r["breaking"] == [] and any("optional param added -> c" in x for x in r["additive"]), str(r))
    new_req = old.replace("def f(a, b=1):", "def f(a, z, b=1):")
    r = iface_gate.compare_sources(old, new_req)
    check("加必填參數 → breaking", any("required param added -> z" in x for x in r["breaking"]), str(r))
    new_rm = old.replace("    def m(self, x):\n        pass\n", "    pass\n")
    r = iface_gate.compare_sources(old, new_rm)
    check("刪公開方法 → breaking removed: K.m", any("removed: K.m" in x for x in r["breaking"]), str(r))
    new_ret = old.replace("def f(a, b=1):", "def f(a, b=1) -> int:")
    r = iface_gate.compare_sources(old, new_ret)
    check("改回傳型別註記 → breaking", any("return type changed" in x for x in r["breaking"]), str(r))
    new_priv = old.replace("def _private(q):", "def _private(q, r):")
    r = iface_gate.compare_sources(old, new_priv)
    check("私有函式改簽章 → 不算（陰性）", r == {"breaking": [], "additive": []}, str(r))
    r = iface_gate.compare_sources("", "def g():\n    pass\n")
    check("新檔 → 全 additive", r["breaking"] == [] and r["additive"] == ["added: g"], str(r))
    r = iface_gate.compare_sources(old, "def f(a b):\n")
    check("新版語法錯誤 → 記成 breaking 不崩潰", any("syntax error" in x for x in r["breaking"]), str(r))


def test_ledger_totals() -> None:
    orig = relay.LEDGER
    with tempfile.TemporaryDirectory() as d:
        relay.LEDGER = Path(d) / "ledger.jsonl"
        check("空帳本 → {}", relay.ledger_totals() == {})
        relay.ledger_append({"task": "t", "role": "implementer", "cli": "codex", "round": 1, "ok": True, "failure_class": None,
                             "seconds": 10.5, "usage": {"input_tokens_total": 100, "output_tokens": 5}})
        relay.ledger_append({"task": "t", "role": "reviewer", "cli": "agy", "round": 1, "ok": False, "failure_class": "rate_limit",
                             "seconds": 2, "usage": None})
        relay.ledger_append({"task": "t", "role": "reviewer", "cli": "agy", "round": 2, "ok": True, "failure_class": None,
                             "seconds": 3, "usage": {"input_tokens_total": 7, "output_tokens": 1}})
        t = relay.ledger_totals()
        check("帳本：codex 1 次 100 in", t["codex"]["calls"] == 1 and t["codex"]["input_total"] == 100, str(t))
        check("帳本：agy 2 次、rate_limited 1、usage None 不崩", t["agy"]["calls"] == 2 and t["agy"]["rate_limited"] == 1 and t["agy"]["input_total"] == 7, str(t))
    relay.LEDGER = orig


def main() -> int:
    for fn in (test_decide_review, test_parse_review, test_iface_gate, test_ledger_totals):
        print(f"--- {fn.__name__} ---")
        fn()
    print(f"\n{PASSED} passed / {len(FAILED)} failed")
    for n in FAILED:
        print("  ✗", n)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
