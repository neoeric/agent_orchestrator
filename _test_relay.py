"""_test_relay.py — relay 的純邏輯測試：難易度判準、結構化審查解析、簽章閘門、帳本、改動判定、
陰性對照逾時、--status 總表（C1）、跨行程鎖與並行上限（C3）、推播（C2）、人工意見回灌 --resume（C6）、
實作者可插拔 codex｜claude＋生產目錄守門（C4a）、撞牆換手（C5）。不呼叫任何 AI CLI。

跑法：PYTHONUTF8=1 python _test_relay.py   （exit 0＝全過）
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
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


def rm_runs(tid: str) -> None:
    """清掉測試產生的 runs/<id> 與 runs/<id>.dry（2026-10-05 起 dry-run 寫 .dry）。"""
    import shutil
    for name in (tid, tid + ".dry"):
        shutil.rmtree(relay.HERE / "runs" / name, ignore_errors=True)


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


    # 2026-09-08 補：與另一台機器的獨立實作（apisig.py）雙向對照後補上的六個漏洞
    r = iface_gate.compare_sources("class Foo:\n    def __init__(self, a):\n        pass\n",
                                   "class Foo:\n    def __init__(self, a, b):\n        pass\n")
    check("__init__ 必填參數增加 → breaking（建構子簽章也是介面）",
          any("required param added -> b" in x for x in r["breaking"]), str(r))
    r = iface_gate.compare_sources("def f(x: str) -> None:\n    pass\n", "def f(x: int) -> None:\n    pass\n")
    check("參數型別註記改變 → breaking", any("param type changed -> x" in x for x in r["breaking"]), str(r))
    r = iface_gate.compare_sources("def f(a, *args):\n    pass\n", "def f(a):\n    pass\n")
    check("*args 被移除 → breaking", any("*args removed" in x for x in r["breaking"]), str(r))
    r = iface_gate.compare_sources("def f(a, **kw):\n    pass\n", "def f(a):\n    pass\n")
    check("**kwargs 被移除 → breaking", any("**kw removed" in x for x in r["breaking"]), str(r))
    r = iface_gate.compare_sources("def f(a):\n    pass\n", "async def f(a):\n    pass\n")
    check("sync 改 async → breaking（呼叫端不 await 會拿到 coroutine）",
          any("sync → async" in x for x in r["breaking"]), str(r))
    r = iface_gate.compare_sources("class C:\n    def v(self):\n        return 1\n",
                                   "class C:\n    @property\n    def v(self):\n        return 1\n")
    check("方法加 @property → breaking（obj.v() 變成 obj.v）",
          any("裝飾器改變" in x for x in r["breaking"]), str(r))
    r = iface_gate.compare_sources("def f(a, b):\n    pass\n", "def f(a, *, b):\n    pass\n")
    check("位置參數改成 kw-only → breaking（對方那支漏掉的 case）", r["breaking"] != [], str(r))
    r = iface_gate.compare_sources("def f(a):\n    pass\n", "def f(a, *args, **kw):\n    pass\n")
    check("新增 *args/**kwargs → additive 不是 breaking",
          r["breaking"] == [] and len(r["additive"]) == 2, str(r))
    r = iface_gate.compare_sources("class C:\n    def v(self):\n        return 1\n",
                                   "class C:\n    @functools.lru_cache()\n    def v(self):\n        return 1\n")
    check("非呼叫慣例的裝飾器（@lru_cache）→ 不算（陰性，避免假 breaking）",
          r == {"breaking": [], "additive": []}, str(r))

    # 2026-09-08 補：同名多定義（property/setter/deleter）——只用名字當 key 會後蓋前，這組全都測不到
    _prop = ("class C:\n    @property\n    def v(self):\n        return 1\n")
    _prop_set = (_prop + "    @v.setter\n    def v(self, x):\n        pass\n")
    _prop_del = (_prop_set + "    @v.deleter\n    def v(self):\n        pass\n")
    r = iface_gate.compare_sources(_prop_set, _prop_set)
    check("property getter＋setter 都不動 → clean（陰性）", r == {"breaking": [], "additive": []}, str(r))
    r = iface_gate.compare_sources(_prop, _prop_set)
    check("加 setter → additive（property 變可寫＝對呼叫端放寬）",
          r["breaking"] == [] and any("C.v[setter]" in x for x in r["additive"]), str(r))
    r = iface_gate.compare_sources(_prop_set, _prop)
    check("移除 setter → breaking（obj.v = x 會炸）",
          any("removed: C.v[setter]" in x for x in r["breaking"]), str(r))
    r = iface_gate.compare_sources(_prop_del, _prop_set)
    check("移除 deleter → breaking（del obj.v 會炸）",
          any("removed: C.v[deleter]" in x for x in r["breaking"]), str(r))
    r = iface_gate.compare_sources(_prop_set,
                                   _prop + "    @v.setter\n    def v(self, x: int):\n        pass\n")
    check("setter 的參數型別改變 → breaking（兩邊都有該 role 時照舊比簽章）",
          any("C.v[setter]" in x and "param type changed" in x for x in r["breaking"]), str(r))


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


def test_diff_for_review() -> None:
    """搬檔任務的審查 diff 必須呈現 rename（2026-09-23 gw-layout-p2b round 1 假退回）。

    病灶：`diff_for_review()` 原本先 `git add --intent-to-add --all` 再 `git diff`（index vs 工作樹）。
    (A) 未暫存刪除＋未追蹤新檔：`--all` 把刪除收進 index，`git diff` 從此看不到刪除，審查者只看到
        N 個 new file、判成「複製不是搬移」退回（真實發生）。
    (B) 實作者用 git mv 暫存好：index==工作樹，`git diff` 對那些檔一片空白。
    兩種狀態都要呈現 `rename from/to`，才算修好。用 20 行同內容檔讓相似度＝100%。
    """
    import shutil
    import subprocess

    def g(wt: str, *a: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", wt, *a], capture_output=True, text=True, encoding="utf-8")

    with tempfile.TemporaryDirectory() as d:
        wt = str(Path(d) / "wt")
        Path(wt, "tools").mkdir(parents=True)
        g(wt, "init", "-q"); g(wt, "config", "user.email", "t@t"); g(wt, "config", "user.name", "t")
        Path(wt, "tools", "a.py").write_text("a = 1\n" * 20, encoding="utf-8")
        g(wt, "add", "-A"); g(wt, "commit", "-qm", "base")
        r = relay.Run({"id": "_test_diff_for_review", "worktree": wt, "repo": wt}, dry=True)
        try:
            # (A) 純檔案系統搬移：舊路徑未暫存刪除、新路徑未追蹤
            Path(wt, "tools", "local").mkdir()
            shutil.move(str(Path(wt, "tools", "a.py")), str(Path(wt, "tools", "local", "a.py")))
            d_a = r.diff_for_review()
            check("審查 diff (A) 未暫存刪除＋未追蹤新檔 → 呈現 rename",
                  "rename from tools/a.py" in d_a and "rename to tools/local/a.py" in d_a, d_a[:300])
            check("審查 diff (A) 沒把搬移呈現成純新增（new file）", "new file mode" not in d_a, d_a[:300])
            g(wt, "reset", "-q"); g(wt, "checkout", "-q", "--", "."); shutil.rmtree(Path(wt, "tools", "local"))
            # (B) 實作者用 git mv 暫存好
            Path(wt, "tools", "local").mkdir()
            g(wt, "mv", "tools/a.py", "tools/local/a.py")
            d_b = r.diff_for_review()
            check("審查 diff (B) git mv 暫存好 → 仍呈現 rename（不是空 diff）",
                  "rename from tools/a.py" in d_b and len(d_b) > 0, f"len={len(d_b)} {d_b[:200]}")
            g(wt, "reset", "-q")
        finally:
            rm_runs("_test_diff_for_review")

def test_worktree_changes() -> None:
    """B4（2026-10-05）：判改動改用內容比對＋`-z`，只差行尾的檔不算改動、中文檔名不亂碼。

    病灶：autocrlf=true 下只差行尾的檔在 `status --porcelain` 標 ` M`，被當成規格外改動而中止 commit；
    同函式解析帶引號路徑，中文檔名變成八進位跳脫字串。臨時 repo 的 autocrlf 一律在 local config 明設。
    """
    import inspect
    import shutil
    import subprocess

    p = relay.parse_porcelain_z
    check("parse_porcelain_z：一般檔", p(" M a.txt\0") == [(" M", "a.txt")], str(p(" M a.txt\0")))
    check("parse_porcelain_z：含空白檔名原樣", p("?? my file.txt\0") == [("??", "my file.txt")])
    check("parse_porcelain_z：中文檔名原樣", p(" M 中文.md\0") == [(" M", "中文.md")])
    check("parse_porcelain_z：R 項跳過舊路徑", p("R  new.txt\0old.txt\0 M b.txt\0") == [("R ", "new.txt"), (" M", "b.txt")],
          str(p("R  new.txt\0old.txt\0 M b.txt\0")))

    def g(wt: str, *a: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", wt, *a], capture_output=True, text=True, encoding="utf-8")

    def w(wt: str, name: str, data: bytes) -> None:
        Path(wt, name).write_bytes(data)

    with tempfile.TemporaryDirectory() as d:
        wt = str(Path(d) / "wt")
        Path(wt).mkdir()
        g(wt, "init", "-q", "-b", "main"); g(wt, "config", "user.email", "t@t"); g(wt, "config", "user.name", "t")
        g(wt, "config", "core.autocrlf", "false")
        w(wt, "a.txt", b"x\ny\n"); w(wt, "crlf.txt", b"x\r\ny\r\n"); w(wt, "real.txt", b"old\n")
        w(wt, "中文.md", b"old\n"); w(wt, "gone.txt", b"bye\n")
        g(wt, "add", "-A"); g(wt, "commit", "-qm", "base")
        g(wt, "config", "core.autocrlf", "true")
        w(wt, "a.txt", b"x\r\ny\r\n"); w(wt, "crlf.txt", b"x\ny\n"); w(wt, "real.txt", b"new\n")
        w(wt, "中文.md", b"new\n"); Path(wt, "gone.txt").unlink()
        w(wt, "new.py", b"print(1)\n"); Path(wt, "views").mkdir(); w(wt, "views/x.txt", b"v\n")
        want = {"real.txt", "中文.md", "gone.txt", "new.py"}
        tid = "_test_worktree_changes"
        try:
            r = relay.Run({"id": tid, "worktree": wt, "repo": wt, "allowed_paths": sorted(want)}, dry=False)
            got = set(r.worktree_changes())
            check("worktree_changes：只差行尾（兩方向）與 views/ 不算改動", got == want, str(got))
            try:
                got2 = set(r.changed_paths())
                check("changed_paths：allowed_paths 恰為真改動 → 不 raise", got2 == want, str(got2))
            except RuntimeError as exc:
                check("changed_paths：allowed_paths 恰為真改動 → 不 raise", False, str(exc))
            r2 = relay.Run({"id": tid, "worktree": wt, "repo": wt, "allowed_paths": sorted(want - {"real.txt"})}, dry=False)
            try:
                r2.changed_paths()
                check("changed_paths：少列 real.txt → raise", False, "沒 raise")
            except RuntimeError as exc:
                check("changed_paths：少列 real.txt → raise，訊息含 real.txt 不含 a.txt",
                      "real.txt" in str(exc) and "a.txt" not in str(exc), str(exc))
            sha = r.commit(sorted(want), "m")
            shown = g(wt, "show", "--name-only", "--format=", "-z", "HEAD").stdout.split("\0")
            check("commit：含 中文.md、不含 a.txt", sha != "" and "中文.md" in shown and "a.txt" not in shown, str(shown))
        finally:
            shutil.rmtree(relay.HERE / "runs" / tid, ignore_errors=True)
    check("Run.run 不再自己解析 `status --porcelain`", "--porcelain" not in inspect.getsource(relay.Run.run))


def test_negative_control_timeout() -> None:
    """B5（2026-10-05）：陰性對照逾時預設要與 verify 一致（1800），否則慢測試被誤判『斷言抓不到違規』。"""
    import shutil

    orig_fn, orig_ledger = relay.inject_check.run_injection_check, relay.LEDGER
    seen: dict = {}

    def fake(*a, **kw):
        seen.update(kw)

    def mk(verify_extra: dict) -> "relay.Run":
        t = {"id": "_test_nc_timeout", "worktree": ".", "repo": ".",
             "verify": [{"name": "v1", "cmd": "echo hi", **verify_extra}],
             "negative_controls": [{"target": "f.py", "old": "a", "new": "b", "expected_failure": "BAD"}]}
        return relay.Run(t, dry=False)

    try:
        relay.inject_check.run_injection_check = fake
        mk({}).negative_control(1)
        check("陰性對照：verify 沒寫 timeout → 1800", seen.get("timeout") == 1800, str(seen.get("timeout")))
        mk({"timeout": 77}).negative_control(1)
        check("陰性對照：verify 寫 timeout=77 → 沿用 77", seen.get("timeout") == 77, str(seen.get("timeout")))

        def boom(*a, **kw):
            raise relay.inject_check.InjectionCheckError("baseline", "x")

        relay.inject_check.run_injection_check = boom
        ok, summary = mk({}).negative_control(1)
        check("陰性對照：InjectionCheckError → (False, 含 [baseline])", ok is False and "[baseline]" in summary, summary)
    finally:
        relay.inject_check.run_injection_check = orig_fn
        relay.LEDGER = orig_ledger
        shutil.rmtree(relay.HERE / "runs" / "_test_nc_timeout", ignore_errors=True)


def test_status() -> None:
    """C1（2026-10-05）：relay.py --status。判活只看任務鎖；例外中止要落檔；dry-run 不覆蓋真實紀錄。"""
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone(timedelta(hours=8)))

    def st(tid: str, phase: str, **kw) -> dict:
        return {"task_id": tid, "phase": phase, "round": 1, "max_rounds": 2,
                "started": "2026-10-05T10:00:00+0800", "updated": "2026-10-05T10:30:00+0800", **kw}

    def row(s: dict, alive: bool = False) -> dict:
        return relay.status_rows([s], {s["task_id"]: alive}, now)[0]

    # 1. 等人？規則（8 項）
    w = row(st("a", "done", commit="abc1234"))["waiting"]
    check("等人？done → 待合併 abc1234", w == "待合併 abc1234", w)
    w = row(st("a", "escalate", verdict="review_tool_failure"))["waiting"]
    check("等人？escalate＋review_tool_failure → 要人看（審查工具故障）", w == "要人看（審查工具故障）", w)
    w = row(st("a", "escalate", verdict="escalate"))["waiting"]
    check("等人？escalate → 要人看（未收斂）", w == "要人看（未收斂）", w)
    w = row(st("a", "aborted", abort_reason="撞牆：codex 回 rate_limit"))["waiting"]
    check("等人？aborted → 含中止原因", w.startswith("要人看（中止：") and "撞牆" in w, w)
    w = row(st("a", "implement"), alive=True)["waiting"]
    check("等人？鎖有人持有 → 跑中", w == "跑中", w)
    w = row(st("a", "queued"), alive=True)["waiting"]
    check("等人？鎖有人持有＋queued → 排隊中", w == "排隊中", w)
    w = row(st("a", "review"), alive=False)["waiting"]
    check("等人？非終態＋鎖沒人持有 → 中斷？", w.startswith("中斷？"), w)
    w = row({"task_id": "a", "_bad": True})["waiting"]
    check("等人？_bad → 讀取失敗", "讀取失敗" in w, w)
    # 2. 耗時與輪次（3 項）
    e = row(st("a", "implement"), alive=True)["elapsed"]
    check("耗時：活著用 now − started（10:00→12:00＝2時0分）", e == "2時0分", e)
    e = row(st("a", "escalate"), alive=False)["elapsed"]
    check("耗時：不在了用 updated − started（10:00→10:30＝30分）", e == "30分", e)
    old = st("a", "escalate", round=2)
    del old["max_rounds"]
    r = row(old)["round"]
    check("輪次：舊 STATE 沒有 max_rounds → 2/?", r == "2/?", r)
    # 3. 排序與 limit（1 項）
    states = [st("old", "done", updated="2026-10-05T09:00:00+0800"), st("newest", "done", updated="2026-10-05T11:00:00+0800"),
              st("mid", "done", updated="2026-10-05T10:00:00+0800")]
    out = relay.render_status(relay.status_rows(states, {}, now), 2)
    body = out.splitlines()[1:3]
    check("排序與 limit：limit=2 只出最新兩筆（新到舊）",
          body[0].startswith("newest") and body[1].startswith("mid") and "old " not in out and "另有 1 筆" in out, out)
    # 4. collect_states（1 項）
    with tempfile.TemporaryDirectory() as d:
        rd = Path(d)
        for name, text in (("ok", json.dumps(st("ok", "done"))), ("x.dry", json.dumps(st("x", "review"))),
                           ("_tmp", json.dumps(st("_tmp", "review"))), ("bad", "{not json")):
            (rd / name).mkdir()
            (rd / name / "STATE.json").write_text(text, encoding="utf-8")
        (rd / "nostate").mkdir()
        got = relay.collect_states(rd)
        check("collect_states：略過 *.dry／_ 開頭／無 STATE，壞 JSON → _bad",
              sorted((g["task_id"], bool(g.get("_bad"))) for g in got) == [("bad", True), ("ok", False)], str(got))
    # 5. 中文寬度（1 項）
    check("_disp_width('中a') == 3", relay._disp_width("中a") == 3)
    # 6. main(["--status"]) 不需要任何 CLI（2 項）
    orig_here, orig_check = relay.HERE, relay.paths.check_all

    def boom(*a, **kw):
        raise AssertionError("--status 不該呼叫 paths.check_all")

    with tempfile.TemporaryDirectory() as d:
        try:
            relay.HERE, relay.paths.check_all = Path(d), boom
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = relay.main(["--status"])
            check("main(--status)：runs/ 不存在 → return 0、印（沒有任何紀錄）、不呼叫 check_all",
                  rc == 0 and "（沒有任何紀錄）" in buf.getvalue(), f"rc={rc} out={buf.getvalue()!r}")
            (Path(d) / "runs" / "t1").mkdir(parents=True)
            (Path(d) / "runs" / "t1" / "STATE.json").write_text(json.dumps(st("t1", "done", commit="deadbee")), encoding="utf-8")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = relay.main(["--status", "--all"])
            check("main(--status --all)：有紀錄 → 表頭＋該列（待合併）", rc == 0 and "等人？" in buf.getvalue()
                  and "t1" in buf.getvalue() and "待合併 deadbee" in buf.getvalue(), buf.getvalue())
        finally:
            relay.HERE, relay.paths.check_all = orig_here, orig_check
    # 7. Run.abort（1 項）
    tid = "_test_abort"
    try:
        r = relay.Run({"id": tid, "worktree": ".", "repo": ".", "title": "t", "max_rounds": 3}, dry=False)
        with contextlib.redirect_stdout(io.StringIO()):
            r.abort("TimeoutExpired: git 逾時（測試）")
        s = json.loads((r.dir / "STATE.json").read_text(encoding="utf-8"))
        cur = (r.dir / "CURRENT.md").read_text(encoding="utf-8")
        check("Run.abort：STATE 階段 aborted、abort_reason 落檔、CURRENT.md 含原因",
              s["phase"] == "aborted" and s["abort_reason"].startswith("TimeoutExpired") and "git 逾時" in cur
              and s["max_rounds"] == 3 and s["pid"] == os.getpid(), str(s)[:300])
    finally:
        rm_runs(tid)
    # 8. dry-run 寫 runs/<id>.dry/、不取鎖（2 項）
    tid = "_test_dry_dir"
    with tempfile.TemporaryDirectory() as d:
        dp = Path(d)
        (dp / "spec.md").write_text("spec", encoding="utf-8")
        (dp / "review.md").write_text("review", encoding="utf-8")
        task = {"id": tid, "repo": str(dp / "repo"), "base_branch": "main", "branch": "feat/x", "worktree": str(dp / "wt"),
                "spec_file": str(dp / "spec.md"), "verify": [{"name": "v", "cmd": "echo hi"}],
                "review": {"policy": "always", "instructions_file": str(dp / "review.md")}}
        (dp / "task.json").write_text(json.dumps(task), encoding="utf-8")
        held = relay.runlock.try_hold(relay.task_lock_path(tid))  # 模擬同任務正在真跑
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = relay.main([str(dp / "task.json"), "--dry-run"])
            real, dry = relay.HERE / "runs" / tid, relay.HERE / "runs" / (tid + ".dry")
            check("dry-run 寫到 runs/<id>.dry/、不建 runs/<id>/", rc == 0 and (dry / "STATE.json").is_file() and not real.exists(),
                  f"rc={rc} dry={dry.exists()} real={real.exists()}")
            check("dry-run 不取任何鎖：同任務鎖被持有時照跑", rc == 0, f"rc={rc}")
        finally:
            held.release()
            rm_runs(tid)


def test_runlock() -> None:
    """C3（2026-10-05）：tools/runlock.py。Windows byte-range lock 與 POSIX flock 都是 per-handle，可同行程測。"""
    from tools import runlock

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        p = Path(d) / "sub" / "x.lock"
        h1, h2 = runlock.try_hold(p), runlock.try_hold(p)
        check("try_hold：第一個 handle 拿到（含建上層目錄）", h1 is not None)
        check("try_hold：第二個 handle 拿不到 → None", h2 is None)
        check("is_held：持有中 → True", runlock.is_held(p))
        t0 = time.monotonic()
        try:
            with runlock.locked(p, timeout=0.3):
                busy = False
        except runlock.LockBusy:
            busy = True
        el = time.monotonic() - t0
        check("locked(timeout=0.3)：別人持有 → LockBusy，耗時 ≥0.3s", busy and el >= 0.3, f"busy={busy} el={el:.3f}")
        h1.release()
        h1.release()  # 可重複呼叫
        h3 = runlock.try_hold(p)
        check("release（重複呼叫也無害）後再 try_hold → 拿到", h3 is not None)
        if h3:
            h3.release()
        check("is_held：釋放後 → False", not runlock.is_held(p))
        nope = Path(d) / "nope.lock"
        check("is_held：鎖檔不存在 → False，且不替呼叫端建檔", not runlock.is_held(nope) and not nope.exists())


def test_parallel() -> None:
    """C3（2026-10-05）：帳本多行程併寫、acquire_run_locks 三種擋法與 --queue、並行上限、task id、agy 不退回 --continue。"""
    import subprocess
    from tools import agy_review, runlock

    # 4. 多行程帳本（1 項）：4 個子行程各 append 200 行，全部要是完整的 JSON 行
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        ledger = Path(d) / "ledger.jsonl"
        script = ("import sys\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\nimport relay\n"
                  "relay.LOCKS = Path(sys.argv[2])\n"
                  "for i in range(200):\n    relay.ledger_append({'task': 't', 'who': sys.argv[3], 'i': i, 'pad': 'x' * 300})\n")
        env = dict(os.environ, RELAY_LEDGER=str(ledger), PYTHONUTF8="1")
        procs = [subprocess.Popen([sys.executable, "-c", script, str(relay.HERE), str(Path(d) / "locks"), str(k)], env=env)
                 for k in range(4)]
        codes = [p.wait(timeout=180) for p in procs]
        keys, bad = set(), 0
        for line in ledger.read_text(encoding="utf-8").splitlines() if ledger.exists() else []:
            try:
                e = json.loads(line)
                keys.add((e["who"], e["i"]))
            except (json.JSONDecodeError, KeyError):
                bad += 1
        check("帳本：4 行程 × 200 行併寫（RELAY_LEDGER 指暫存）→ 800 行全部可 json.loads、無重疊",
              codes == [0, 0, 0, 0] and len(keys) == 800 and bad == 0, f"codes={codes} keys={len(keys)} bad={bad}")

    env_bak, poll_bak = os.environ.get("RELAY_MAX_PARALLEL"), relay.QUEUE_POLL_SECONDS
    stacks = contextlib.ExitStack()
    try:
        with tempfile.TemporaryDirectory() as d:
            wt = {k: str(Path(d) / f"wt-{k}") for k in "abcq"}

            def busy_msg(task: dict) -> str:
                try:
                    relay.acquire_run_locks(task, queue=False).close()
                    return "（沒擋下）"
                except runlock.LockBusy as exc:
                    return str(exc)

            # 5. acquire_run_locks（3 項＋被擋下不殘留鎖＋queue）
            os.environ["RELAY_MAX_PARALLEL"] = "1"
            s1 = stacks.enter_context(relay.acquire_run_locks({"id": "_t_a", "worktree": wt["a"]}, queue=False))
            m = busy_msg({"id": "_t_b", "worktree": wt["b"]})
            check("RELAY_MAX_PARALLEL=1：第二個不同任務、未加 queue → LockBusy 含「並行上限」", "並行上限" in m, m)
            m = busy_msg({"id": "_t_a", "worktree": wt["b"]})
            check("同 task id → LockBusy 含「正在跑」", "正在跑" in m, m)
            m = busy_msg({"id": "_t_c", "worktree": wt["a"]})
            check("不同 task、同 worktree → LockBusy 含「worktree」", "worktree" in m, m)
            check("被擋下的那次不殘留鎖（_t_b 的任務鎖、worktree 鎖都已放掉）",
                  not runlock.is_held(relay.task_lock_path("_t_b")) and not runlock.is_held(relay.worktree_lock_path(wt["b"])))
            relay.QUEUE_POLL_SECONDS = 0.05
            waited, got = threading.Event(), {}

            def worker() -> None:
                try:
                    got["stack"] = relay.acquire_run_locks({"id": "_t_q", "worktree": wt["q"]}, queue=True, on_wait=waited.set)
                except BaseException as exc:  # noqa: BLE001 — 失敗要帶回主執行緒顯示
                    got["err"] = exc

            th = threading.Thread(target=worker, daemon=True)
            with contextlib.redirect_stdout(io.StringIO()):
                th.start()
                saw_wait = waited.wait(5)
                still_waiting = "stack" not in got
                s1.close()
                th.join(5)
            if "stack" in got:
                stacks.enter_context(got["stack"])
            check("--queue：名額滿 → 先 on_wait() 排隊、不拿名額；名額釋放後拿到",
                  saw_wait and still_waiting and "stack" in got, str(got))
            stacks.close()
            # 6. 並行上限（2 項＋設 2 時兩個都拿得到）
            with contextlib.redirect_stderr(io.StringIO()):
                os.environ["RELAY_MAX_PARALLEL"] = "9"
                n9 = relay.max_parallel()
                os.environ["RELAY_MAX_PARALLEL"] = "abc"
                nabc = relay.max_parallel()
            check("RELAY_MAX_PARALLEL=9 → 實際名額 3", n9 == 3, str(n9))
            check("RELAY_MAX_PARALLEL=abc → 1", nabc == 1, str(nabc))
            os.environ["RELAY_MAX_PARALLEL"] = "2"
            try:
                stacks.enter_context(relay.acquire_run_locks({"id": "_t_a", "worktree": wt["a"]}, queue=False))
                stacks.enter_context(relay.acquire_run_locks({"id": "_t_b", "worktree": wt["b"]}, queue=False))
                both = True
            except runlock.LockBusy as exc:
                both = str(exc)
            check("RELAY_MAX_PARALLEL=2：兩個不同任務都拿得到", both is True, str(both))
            stacks.close()
    finally:
        stacks.close()
        relay.QUEUE_POLL_SECONDS = poll_bak
        if env_bak is None:
            os.environ.pop("RELAY_MAX_PARALLEL", None)
        else:
            os.environ["RELAY_MAX_PARALLEL"] = env_bak

    # 7. task id 驗證（2 項）
    rcs = []
    with tempfile.TemporaryDirectory() as d:
        for tid in ("../x", "..", "a/b"):
            tf = Path(d) / "t.json"
            tf.write_text(json.dumps({"id": tid, "repo": d, "base_branch": "main", "branch": "b", "worktree": str(Path(d) / "wt"),
                                      "spec_file": "s.md", "verify": [], "review": {"instructions_file": "r.md"}}), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                rcs.append(relay.main([str(tf), "--dry-run"]))
    check("task id 驗證：'../x'、'..'、'a/b' → main 回 3、沒在 runs/ 外建目錄",
          rcs == [3, 3, 3] and not (relay.HERE / "x.dry").exists() and not (relay.HERE / "runs" / "a").exists(), str(rcs))
    v = relay.valid_task_id
    check("task id 驗證：'gw-x.c1'、'_tmp' 通過；結尾 '.'／'.dry' 不通過",
          v("gw-x.c1") and v("_tmp") and not v("a.") and not v("a.dry") and not v(""))

    # 8. agy_review 第 1 塊沒有 conversation_id（1 項）
    calls: list = []

    def fake_agy(prompt, conv, timeout):
        calls.append(conv)
        return {"status": "SUCCESS", "response": "OK 1"}, "", 0

    orig = agy_review.run_agy
    with tempfile.TemporaryDirectory() as d:
        instr, diff, out = (Path(d) / n for n in ("i.txt", "d.txt", "o.json"))
        instr.write_text("核對", encoding="utf-8")
        diff.write_text("+x = 1\n", encoding="utf-8")
        try:
            agy_review.run_agy = fake_agy
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = agy_review.main(["--instructions", str(instr), "--diff", str(diff), "--out", str(out)])
        finally:
            agy_review.run_agy = orig
    check("agy_review：第 1 塊沒回 conversation_id → 回 1，且沒用 __continue__ 再呼叫", rc == 1 and calls == [None],
          f"rc={rc} calls={calls}")


FAKE_NOTIFIER = r'''import os, sys
data = sys.stdin.buffer.read().decode("utf-8")
mode = sys.argv[2] if len(sys.argv) > 2 else "ok"
with open(sys.argv[1], "a", encoding="utf-8") as f:
    f.write(os.environ.get("RELAY_NOTIFY_KIND", "") + "|" + data + "\n---\n")
if mode == "fail":
    sys.exit(7)
if mode == "hang":
    import time
    time.sleep(30)
'''


def _write_cfg(d: Path, out: Path, mode: str = "ok", **extra) -> Path:
    """寫一份假通道設定：cmd 是把 stdin 追加進 out 的小腳本。"""
    script = d / "fake_notifier.py"
    script.write_text(FAKE_NOTIFIER, encoding="utf-8")
    cfg = d / "notify.json"
    cfg.write_text(json.dumps({"cmd": [sys.executable, str(script), str(out), mode], **extra}), encoding="utf-8")
    return cfg


def _n_sent(out: Path) -> int:
    return out.read_text(encoding="utf-8").count("\n---\n") if out.is_file() else 0


def test_notify() -> None:
    """C2（2026-10-05）：tools/notify.py 純函式＋假指令、Run.notify 接線。全程不連網、不呼叫真的通道。"""
    from datetime import datetime, timedelta, timezone
    from tools import notify

    tz8 = timezone(timedelta(hours=8))
    # 1. compose（5 項）
    verbs = {"ready_to_merge": "待合併", "escalate": "未收斂", "review_tool_failure": "故障", "rate_limit": "撞牆", "aborted": "中止"}
    info = {"round": 2, "max_rounds": 3, "commit": "abc1234", "branch": "feat/x", "failure_class": "login_expired",
            "cli": "codex", "reason": "worktree add 失敗\r\n第二行", "minutes": 12}
    for kind, verb in verbs.items():
        lines = notify.compose(kind, "task-1", info).split("\n")
        check(f"compose[{kind}]：第 1 行含 task id 與「{verb}」、第 2 行以「下一步：」開頭、無 \\r",
              "task-1" in lines[0] and verb in lines[0] and lines[1].startswith("下一步：") and "\r" not in "\n".join(lines),
              str(lines))

    # 2. decide（6 項）
    cfg = {**notify.DEFAULTS}
    now = datetime(2026, 10, 5, 12, 0, tzinfo=tz8)

    def ent(minutes_ago: float, task="t", kind="escalate", sent=True, at=None):
        return {"ts": (at or (now - timedelta(minutes=minutes_ago))).isoformat(), "task": task, "kind": kind, "sent": sent}

    ok, why = notify.decide([], now, "t", "escalate", {**cfg, "kinds": ["aborted"]})
    check("decide：kind 不在 kinds → kind_off", (ok, why) == (False, "kind_off"), why)
    ok, why = notify.decide([ent(59)], now, "t", "escalate", cfg)
    check("decide：同 task+kind 59 分前發過 → dedupe", (ok, why) == (False, "dedupe"), why)
    ok, why = notify.decide([ent(61)], now, "t", "escalate", cfg)
    check("decide：61 分前發過 → ok", (ok, why) == (True, "ok"), why)
    ok, why = notify.decide([ent(m, task=f"o{m}") for m in (5, 10, 20, 30)], now, "t", "escalate", cfg)
    check("decide：近一小時已 4 則 → hourly_cap", (ok, why) == (False, "hourly_cap"), why)
    month = [ent(0, task=f"o{i}", at=datetime(2026, 10, 1 + i % 4, 1, 0, tzinfo=tz8)) for i in range(40)]
    ok, why = notify.decide(month, now, "t", "escalate", cfg)
    check("decide：本月已 40 則 → monthly_cap", (ok, why) == (False, "monthly_cap"), why)
    prev = [ent(0, task=f"o{i}", at=datetime(2026, 9, 30, 23, 50, tzinfo=tz8) - timedelta(days=i % 5)) for i in range(40)]
    prev.append(ent(0, task="o_oct", at=datetime(2026, 10, 1, 0, 10, tzinfo=tz8)))
    ok, why = notify.decide(prev, now, "t", "escalate", {**cfg, "max_per_month": 2})
    check("decide：上月 40 則不算本月（10-01T00:10+0800 算 10 月，本月只 1 則 → ok）", (ok, why) == (True, "ok"), why)
    ok, why = notify.decide([ent(5, sent=False)] * 9, now, "t", "escalate", cfg)
    check("decide：sent=false 的紀錄（被節流）不計入額度", (ok, why) == (True, "ok"), why)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as dd:
        d = Path(dd)
        # 3. load_config（3 項＋預設值 1 項）
        c, why = notify.load_config(d / "nope.json")
        check("load_config：檔不存在 → (None, 'off')", (c, why) == (None, "off"), why)
        (d / "bad.json").write_text("{not json", encoding="utf-8")
        c, why = notify.load_config(d / "bad.json")
        check("load_config：壞 JSON → None 且說明含「設定錯誤」", c is None and "設定錯誤" in why, why)
        (d / "str.json").write_text(json.dumps({"cmd": "echo hi"}), encoding="utf-8")
        c, why = notify.load_config(d / "str.json")
        check("load_config：cmd 不是 list → 設定錯誤", c is None and "設定錯誤" in why, why)
        (d / "ok.json").write_text(json.dumps({"cmd": ["x"], "max_per_hour": 2}), encoding="utf-8")
        c, why = notify.load_config(d / "ok.json")
        check("load_config：缺的欄位補預設（max_per_month=40、dedupe_minutes=60、五類全開）、有給的照用",
              c is not None and c["max_per_hour"] == 2 and c["max_per_month"] == 40 and c["dedupe_minutes"] == 60
              and set(c["kinds"]) == set(notify.KINDS), str(c))

        # 4. send（3 項）
        out = d / "o.txt"
        cfg_path = _write_cfg(d, out)
        c, _ = notify.load_config(cfg_path)
        code, _err = notify.send(c, "第一行：中文\n第二行", {"RELAY_NOTIFY_KIND": "escalate"})
        got = out.read_text(encoding="utf-8") if out.is_file() else ""
        check("send：訊息走 stdin、種類走環境變數、中文不亂碼", code == 0 and got.startswith("escalate|第一行：中文\n第二行"), repr(got))
        code, err = notify.send({**c, "cmd": [str(d / "no_such_exe_xyz")]}, "x", {})
        check("send：指令不存在 → (None, …)、不 raise", code is None and err, f"{code} {err}")
        code, _ = notify.send({**c, "cmd": c["cmd"][:2] + [str(out), "fail"]}, "x", {})
        check("send：exit 7 的假指令 → 回 7", code == 7, str(code))
        code, err = notify.send({**c, "cmd": c["cmd"][:2] + [str(out), "hang"], "timeout": 1}, "x", {})
        check("send：逾時 → (None, …逾時)、不 raise", code is None and "逾時" in err, f"{code} {err}")

        # 5. notify 整合（連發兩次）（1 項＋帳本內容 1 項）
        out2, led, lock = d / "o2.txt", d / "led.jsonl", d / "n.lock"
        cfg2 = _write_cfg(d, out2)
        kw = dict(config_path=cfg2, ledger_path=led, lock_path=lock)
        r1 = notify.notify("escalate", "t1", {"round": 2, "max_rounds": 2}, **kw)
        r2 = notify.notify("escalate", "t1", {"round": 2, "max_rounds": 2}, **kw)
        rows = [json.loads(x) for x in led.read_text(encoding="utf-8").splitlines()]
        check("notify：同 task+kind 連發兩次 → 第二次 dedupe；帳本兩行、只有一行 sent=true、通道只被呼叫一次",
              len(rows) == 2 and [r["sent"] for r in rows] == [True, False] and rows[1]["reason"] == "dedupe" and _n_sent(out2) == 1
              and "dedupe" in r2, f"{r1} / {r2} / {rows}")
        r3 = notify.notify("escalate", "t1", {}, config_path=d / "nope.json", ledger_path=led, lock_path=lock)
        check("notify：設定檔不存在 → 回「推播關閉」、帳本不增行", r3 == "推播關閉" and len(led.read_text(encoding="utf-8").splitlines()) == 2, r3)

        # 5b. 上限實測：最壞情況灌 20 個不同任務 → 全體每小時最多 4 次呼叫（1 項）
        out3, led3 = d / "o3.txt", d / "led3.jsonl"
        cfg3 = _write_cfg(d, out3)
        for i in range(20):
            notify.notify("aborted", f"burst-{i}", {"reason": "x"}, config_path=cfg3, ledger_path=led3, lock_path=lock)
        rows = [json.loads(x) for x in led3.read_text(encoding="utf-8").splitlines()]
        check("notify：20 個不同任務連續中止 → 通道只被呼叫 4 次（max_per_hour），其餘 hourly_cap",
              _n_sent(out3) == 4 and sum(r["sent"] for r in rows) == 4 and rows[-1]["reason"] == "hourly_cap", str(_n_sent(out3)))
        # 5c. 指令失敗也佔額度（sent=true、reason=send_failed）（1 項）
        led4 = d / "led4.jsonl"
        cfg4 = _write_cfg(d, d / "o4.txt", "fail")
        r = notify.notify("aborted", "f1", {"reason": "x"}, config_path=cfg4, ledger_path=led4, lock_path=lock)
        row = json.loads(led4.read_text(encoding="utf-8").splitlines()[0])
        check("notify：指令失敗 → 帳本 sent=true、reason=send_failed、exit=7，回傳說明含 exit=7",
              row["sent"] is True and row["reason"] == "send_failed" and row["exit"] == 7 and "exit=7" in r, f"{row} {r}")

    # 6. Run.notify（3 項＋失敗不影響結果等）
    tid = "_test_run_notify"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as dd:
        d = Path(dd)
        led_bak, env_bak = relay.NOTIFY_LEDGER, os.environ.get("RELAY_NOTIFY_CONFIG")
        relay.NOTIFY_LEDGER = d / "nl.jsonl"
        try:
            out = d / "o.txt"
            os.environ["RELAY_NOTIFY_CONFIG"] = str(_write_cfg(d, out))
            task = {"id": tid, "worktree": ".", "repo": ".", "title": "t", "max_rounds": 3}
            with contextlib.redirect_stdout(io.StringIO()):
                r = relay.Run(task, dry=True)
                r.notify("aborted", reason="x")
                check("Run.notify：dry=True 不發", _n_sent(out) == 0 and not relay.NOTIFY_LEDGER.exists())
                r = relay.Run(task, dry=False, no_notify=True)
                r.notify("aborted", reason="x")
                check("Run.notify：no_notify=True 不發", _n_sent(out) == 0 and not relay.NOTIFY_LEDGER.exists())
                r = relay.Run(task, dry=False)
                r.notify("aborted", reason="x")
                r.notify("escalate")
                r.abort("再中止一次")
            st = json.loads((r.dir / "STATE.json").read_text(encoding="utf-8"))
            check("Run.notify：同一個 Run 呼叫多次（含 abort）只發一次；STATE.notified 記 kind 與結果", _n_sent(out) == 1
                  and st.get("notified", {}).get("kind") == "aborted" and "推播已發" in st["notified"]["result"], str(st.get("notified")))
            # 被節流時 CURRENT.md 附一行（1 項）
            with contextlib.redirect_stdout(io.StringIO()):
                r2 = relay.Run(task, dry=False)
                r2.write_current("# CURRENT\n\n任務中止。\n")
                r2.notify("aborted", reason="x")
            cur = (r2.dir / "CURRENT.md").read_text(encoding="utf-8")
            check("被節流（同任務同類型 dedupe）→ CURRENT.md 末尾附「推播未發（dedupe）」", "推播未發（dedupe）" in cur, cur)
            # 撞牆出口（C5 起走 run() 的 escalate 分支，不再經 abort）：Run.notify("rate_limit", cli=…) 帶 cli 進文案（1 項）
            out_rl = d / "o_rl.txt"
            os.environ["RELAY_NOTIFY_CONFIG"] = str(_write_cfg(d, out_rl))
            with contextlib.redirect_stdout(io.StringIO()):
                r3 = relay.Run({**task, "id": tid + "_rl"}, dry=False)
                r3.notify("rate_limit", cli="codex", role="implementer")
            body = out_rl.read_text(encoding="utf-8") if out_rl.is_file() else ""
            check("Run.notify('rate_limit', cli=…)：推播第 1 行含 codex 與「撞牆」", body.startswith("rate_limit|【relay】") and "codex" in body.split("\n")[0]
                  and "撞牆" in body.split("\n")[0], body)
            rm_runs(tid + "_rl")

            # 推播失敗／逾時／設定壞：Run.notify 不 raise、不改階段；main() 的 exit code 與 run.run() 的結果一致（3 項）
            results = {}
            for label, mode, extra in (("exit 7", "fail", {}), ("逾時", "hang", {"timeout": 1})):
                os.environ["RELAY_NOTIFY_CONFIG"] = str(_write_cfg(d, d / f"o_{mode}.txt", mode, **extra))
                relay.NOTIFY_LEDGER = d / f"nl_{mode}.jsonl"
                rr = relay.Run({**task, "id": tid + "_f"}, dry=False)
                rr.save("escalate")
                t0 = time.monotonic()
                with contextlib.redirect_stdout(io.StringIO()):
                    rr.notify("escalate")
                results[label] = (rr.state.phase, round(time.monotonic() - t0, 1), rr.state.notified.get("result", ""))
            check("推播指令 exit 7／逾時 → Run.notify 不 raise、階段仍是 escalate、逾時有被截斷（<10s）",
                  all(v[0] == "escalate" for v in results.values()) and results["逾時"][1] < 10
                  and "exit=7" in results["exit 7"][2] and "逾時" in results["逾時"][2], str(results))
            rm_runs(tid + "_f")
            (d / "badcfg.json").write_text("{oops", encoding="utf-8")
            os.environ["RELAY_NOTIFY_CONFIG"] = str(d / "badcfg.json")
            rr = relay.Run({**task, "id": tid + "_f"}, dry=False)
            with contextlib.redirect_stdout(io.StringIO()):
                rr.notify("escalate")
            check("設定檔壞 → 視同關閉，記一行「推播設定錯誤」、不 raise", "推播設定錯誤" in rr.state.notified.get("result", ""), str(rr.state.notified))
            rm_runs(tid + "_f")

            # main()：exit code 不受推播影響（3 項）
            os.environ["RELAY_NOTIFY_CONFIG"] = str(_write_cfg(d, d / "o_main.txt", "fail"))
            relay.NOTIFY_LEDGER = d / "nl_main.jsonl"
            (d / "spec.md").write_text("s", encoding="utf-8")
            (d / "rev.md").write_text("r", encoding="utf-8")
            mt = {"id": tid + "_m", "repo": str(d / "repo"), "base_branch": "main", "branch": "feat/x", "worktree": str(d / "wt"),
                  "spec_file": str(d / "spec.md"), "verify": [], "review": {"policy": "always", "instructions_file": str(d / "rev.md")}}
            (d / "task.json").write_text(json.dumps(mt), encoding="utf-8")
            orig_run, orig_check, orig_nl = relay.Run.run, relay.paths.check_all, relay.NOTIFY_LEDGER
            relay.paths.check_all = lambda **kw: []  # C4a 起 main 以 keyword 傳需求

            def run_ok(self):  # 假的「收斂」：只做會推播的那一步
                self.notify("ready_to_merge")
                return 0

            def run_wall(self):  # C5（2026-10-05）起撞牆走 run() 的 escalate 出口回 2，不再丟例外
                self.state.verdict = "rate_limit"
                self.save("escalate")
                self.notify("rate_limit", cli="codex", role="implementer")
                return 2

            try:
                rcs = {}
                for name, fake in (("收斂", run_ok), ("撞牆", run_wall)):
                    relay.Run.run = fake
                    relay.NOTIFY_LEDGER = d / f"nl_main_{name}.jsonl"
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        rcs[name] = relay.main([str(d / "task.json")])
                    rm_runs(tid + "_m")
                check("main：推播指令 exit 7 時，收斂棒仍回 0、撞牆棒仍回 2", rcs == {"收斂": 0, "撞牆": 2}, str(rcs))
                relay.Run.run = run_ok
                n_before = _n_sent(d / "o_main.txt")
                relay.NOTIFY_LEDGER = d / "nl_main_nn.jsonl"
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    rc = relay.main([str(d / "task.json"), "--no-notify"])
                check("main --no-notify：收斂棒回 0 且不呼叫通道", rc == 0 and _n_sent(d / "o_main.txt") == n_before and n_before >= 1,
                      f"rc={rc} {n_before}")
                rm_runs(tid + "_m")
                # dry-run 不觸發推播（1 項）
                before = (d / "o_main.txt").read_text(encoding="utf-8") if (d / "o_main.txt").is_file() else ""
                relay.Run.run = orig_run
                (d / "repo").mkdir(exist_ok=True)
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    rc = relay.main([str(d / "task.json"), "--dry-run"])
                after = (d / "o_main.txt").read_text(encoding="utf-8") if (d / "o_main.txt").is_file() else ""
                check("main --dry-run：不呼叫通道（通道輸出檔沒增加）", before == after, f"rc={rc}")
            finally:
                relay.Run.run, relay.paths.check_all, relay.NOTIFY_LEDGER = orig_run, orig_check, orig_nl
                rm_runs(tid + "_m")
        finally:
            relay.NOTIFY_LEDGER = led_bak
            if env_bak is None:
                os.environ.pop("RELAY_NOTIFY_CONFIG", None)
            else:
                os.environ["RELAY_NOTIFY_CONFIG"] = env_bak
            rm_runs(tid)


def test_notify_telegram() -> None:
    """C2（2026-10-05）：tools/notify_telegram.py。_post 整個換成假函式，不連網；憑證用明顯的假值。"""
    from tools import notify_telegram as nt

    FAKE_TOKEN = "123456:FAKE-TOKEN-FOR-TEST"
    posts: list = []

    def run(argv, env, text="哈囉", post=None):
        posts.clear()
        nt._post = post or (lambda url, data, timeout: (posts.append((url, data)) or (200, '{"ok":true}')))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = nt.main(argv, environ=env, stdin=io.StringIO(text))
        return rc, out.getvalue(), err.getvalue()

    orig_post = nt._post
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as dd:
            d = Path(dd)
            rc, o, e = run([], {})
            check("telegram：缺兩個變數 → exit≠0，錯誤只寫變數名、沒有發出 HTTP", rc != 0 and "TELEGRAM_ALERT_TOKEN" in e
                  and "TELEGRAM_ALERT_TO" in e and not posts, e)
            rc, o, e = run([], {"TELEGRAM_ALERT_TOKEN": FAKE_TOKEN})
            check("telegram：只缺聊天室 → exit≠0、只點名缺的那個、輸出不含 token",
                  rc != 0 and "TELEGRAM_ALERT_TO" in e and FAKE_TOKEN not in o + e, e)
            envf = d / "alert.env"
            envf.write_text("\n".join(["# 註解", "", f"{nt.TOKEN_VAR}='{FAKE_TOKEN}'", f"{nt.CHAT_VAR} = -100999", "OTHER=x=y", ""]), encoding="utf-8-sig")
            vals = nt.load_env_file(str(envf))
            check("env-file 解析：略過註解與空行、去引號、等號兩側空白、值內含 = 保留",
                  vals == {"TELEGRAM_ALERT_TOKEN": FAKE_TOKEN, "TELEGRAM_ALERT_TO": "-100999", "OTHER": "x=y"}, str(vals))
            rc, o, e = run(["--env-file", str(envf)], {})
            check("telegram：從 --env-file 讀憑證 → 成功印 NOTIFY telegram http=200、exit 0、POST 到 bot URL",
                  rc == 0 and o.strip() == "NOTIFY telegram http=200" and len(posts) == 1 and f"/bot{FAKE_TOKEN}/" in posts[0][0], f"{rc} {o} {e}")
            check("telegram：成功時 stdout／stderr 都不含 token 或 bot<token> URL", FAKE_TOKEN not in o + e)
            rc, o, e = run(["--env-file", str(envf)], {"TELEGRAM_ALERT_TOKEN": "ENV-WINS-FAKE", "TELEGRAM_ALERT_TO": "42"})
            check("telegram：環境變數優先於 --env-file", rc == 0 and "/botENV-WINS-FAKE/" in posts[0][0] and b"chat_id=42" in posts[0][1])
            rc, o, e = run(["--env-file", str(d / "nope.env")], {})
            check("telegram：--env-file 讀不到 → exit≠0 並說明", rc != 0 and "env-file" in e, e)
            long_text = "字" * 5000
            rc, o, e = run(["--env-file", str(envf)], {}, text=long_text)
            import urllib.parse as up
            sent = up.parse_qs(posts[0][1].decode("utf-8"))["text"][0]
            check("telegram：超過 4096 字元 → 截斷到 ≤4096 並註明已截斷；未超過則原文送出",
                  len(sent) <= 4096 and "已截斷" in sent and nt.truncate("短") == "短", str(len(sent)))
            rc, o, e = run(["--env-file", str(envf)], {}, post=lambda u, dta, t: (400, f"bad request {u}"))
            check("telegram：HTTP 非 200 → exit≠0、印 http=400、錯誤本文中的 token 已遮掉",
                  rc != 0 and "http=400" in o and FAKE_TOKEN not in o + e and "***" in e, f"{o} {e}")

            def boom(u, dta, t):
                raise OSError(f"connect failed for {u}")

            rc, o, e = run(["--env-file", str(envf)], {}, post=boom)
            check("telegram：網路錯誤 → exit≠0、例外文字夾帶的 URL 已遮掉 token", rc != 0 and FAKE_TOKEN not in o + e and "OSError" in e, e)
            rc, o, e = run(["--env-file", str(envf)], {}, text="  \n")
            check("telegram：stdin 空白 → exit≠0、不發 HTTP", rc != 0 and not posts, e)
    finally:
        nt._post = orig_post


class ScriptedRun(relay.Run):
    """規格 §12 共用測試骨架（2026-10-05，C6 起用）：implement／verify／review／negative_control 依腳本回傳，
    不呼叫任何 CLI。prompt 與審查指令沿用真的組法（impl_prompt／review_inputs），才驗得到兩個角色實際拿到什麼。
    script＝{"impl": [{"write": {相對路徑: 內容}}…], "verify": [bool…], "review": [(approved, 原文)…]}，每次呼叫 pop 一筆。
    C4a（2026-10-05）：impl 步驟另可給 {"write_abs": {絕對路徑: 內容}}（模擬實作者寫到 worktree 外，例如生產目錄）。
    C5（2026-10-05）：impl 步驟可給 "ok": False＋"failure_class"（例如 "rate_limit"）；檔名主幹照真實作（relay.impl_stem），
    stdout 落一份假檔；calls_log 記 cli／attempt／stem 與開工當下 worktree 的 a.py（驗半成品有沒有被保留）。
    review 步驟可給 {"tool_ok": False, "failure_class": "rate_limit"}（審查工具故障／撞牆）。"""

    def __init__(self, task, script, **kw):
        super().__init__(task, dry=False, **kw)
        self.script = script
        self.calls_log: list[dict] = []

    def implement(self, rnd, feedback, cli=None, attempt=0):
        step = self.script["impl"].pop(0)
        cli = cli or self.impl_cli
        stem = relay.impl_stem(rnd, attempt, cli)
        prompt = self.impl_prompt(rnd, feedback, stem=stem)
        a_py = Path(self.wt, "a.py")
        self.calls_log.append({"role": "impl", "round": rnd, "feedback": feedback, "prompt": prompt, "cli": cli,
                               "attempt": attempt, "stem": stem,
                               "a_py": a_py.read_text(encoding="utf-8") if a_py.is_file() else None})
        for name, text in step.get("write", {}).items():
            Path(self.wt, name).write_bytes(text.encode("utf-8"))
        for name, text in step.get("write_abs", {}).items():
            Path(name).write_bytes(text.encode("utf-8"))
        ok = step.get("ok", True)
        so = self.dir / f"{stem}.stdout.txt"
        so.write_text("（腳本實作者的假 stdout）", encoding="utf-8")
        self.record(relay.CallRecord("implementer", rnd, ok, "scripted", 0, 0.0, None, str(so), cli=cli,
                                     failure_class=step.get("failure_class")))
        return ok, f"（腳本實作者第 {rnd} 輪）", None

    def verify(self, rnd):
        ok = self.script["verify"].pop(0)
        self.state.verify.append({"round": rnd, "name": "v", "exit": 0 if ok else 1, "seconds": 0.0, "tail": ""})
        self.save()
        return ok, f"- v: {'PASS' if ok else 'FAIL'}"

    def review(self, rnd, diff):
        step = self.script["review"].pop(0)
        instr_f, _, out_f = self.review_inputs(rnd, diff)
        self.calls_log.append({"role": "review", "round": rnd, "instr": instr_f.read_text(encoding="utf-8"), "diff": diff})
        if isinstance(step, dict) and step.get("tool_ok") is False:
            self.record(relay.CallRecord("reviewer", rnd, False, "scripted", 1, 0.0, None, str(out_f), cli="agy",
                                         failure_class=step.get("failure_class")))
            return False, "", None, False
        approved, text = step
        self.record(relay.CallRecord("reviewer", rnd, True, "scripted", 0, 0.0, None, "", cli="agy"))
        self.state.review_decisions.append({"round": rnd, "structured": True, "approved": approved, "unreported": [], "checks": []})
        self.save()
        return approved, text, None, True

    def negative_control(self, rnd):
        return True, ""


@contextlib.contextmanager
def scripted_main(script: dict, made: list):
    """main() 裡 new 出來的 Run 換成 ScriptedRun（made 收集實例供檢查）；環境自檢換成「什麼都不缺」。"""
    orig_run, orig_check = relay.Run, relay.paths.check_all

    def factory(task, dry, **kw):
        assert not dry, "ScriptedRun 不跑 dry-run"
        r = ScriptedRun(task, script, **kw)
        made.append(r)
        return r

    relay.Run, relay.paths.check_all = factory, (lambda **kw: [])
    try:
        yield
    finally:
        relay.Run, relay.paths.check_all = orig_run, orig_check


def test_resume() -> None:
    """C6（2026-10-05）：relay.py --resume。escalate／已收斂後接續、拒跑條件（不改任何檔）、dry-run、STATE 相容。
    全程 ScriptedRun（不呼叫 CLI）、帳本與鎖指暫存、推播設定指到不存在的路徑；臨時 repo 的 autocrlf 在 local 明設。"""
    import hashlib
    import subprocess
    from datetime import datetime, timedelta, timezone
    from tools import notify

    def g(wt, *a: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True, encoding="utf-8")

    def mk_repo(d: Path, tid: str, **extra) -> tuple[Path, Path]:
        """臨時 repo＝worktree（main 一顆 commit，切到 feat/c6），任務檔放 repo 外。"""
        wt = d / "repo"
        wt.mkdir(parents=True)
        g(wt, "init", "-q", "-b", "main"); g(wt, "config", "user.email", "t@t"); g(wt, "config", "user.name", "t")
        g(wt, "config", "core.autocrlf", "false")
        (wt / "a.py").write_bytes(b"x = 0\n")
        g(wt, "add", "-A"); g(wt, "commit", "-qm", "base"); g(wt, "checkout", "-q", "-b", "feat/c6")
        (d / "spec.md").write_text("把 x 改掉", encoding="utf-8")
        (d / "review.md").write_text("1. x 有改", encoding="utf-8")
        task = {"id": tid, "title": "c6 測試", "repo": str(wt), "base_branch": "main", "branch": "feat/c6", "worktree": str(wt),
                "spec_file": str(d / "spec.md"), "verify": [{"name": "v", "cmd": "echo ok"}],
                "review": {"policy": "always", "instructions_file": str(d / "review.md")},
                "allowed_paths": ["a.py"], "max_rounds": 2, **extra}
        tf = d / "task.json"
        tf.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        return wt, tf

    def call(argv: list[str]) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = relay.main(argv)
        return rc, err.getvalue()

    def snap(rd: Path, wt) -> tuple:
        """runs/<id>/ 每個檔的 bytes＋worktree 的 HEAD 與 status：拒跑前後必須一模一樣。"""
        files = {p.relative_to(rd).as_posix(): p.read_bytes() for p in rd.rglob("*") if p.is_file()} if rd.exists() else None
        gs = (g(wt, "rev-parse", "HEAD").stdout, g(wt, "status", "--porcelain", "-z").stdout) if wt else None
        return files, gs

    def state_of(tid: str) -> dict:
        return json.loads((relay.HERE / "runs" / tid / "STATE.json").read_text(encoding="utf-8"))

    tz8 = timezone(timedelta(hours=8))
    # ---- 純函式：STATE 相容（1＋1 項）、任務檔解析順序（2 項）、推播文案（1 項）、--status 耗時（1 項）
    s = relay.state_from_dict({"task_id": "t", "phase": "done", "round": 2, "commit": "abc", "future_key": 1})
    check("state_from_dict：舊 STATE 缺新欄位、多出未知鍵 → 仍可載入（新欄位用預設值）",
          s.round == 2 and s.commit == "abc" and s.commits == [] and s.last_feedback == "" and s.resumed == []
          and not hasattr(s, "future_key"))
    bad = []
    for d in ({"task_id": "t", "round": "2"}, {"phase": "done"}, {"task_id": "t", "calls": {}}, [1], {"task_id": "t", "round": True},
              {"task_id": "t", "round": -1}):
        try:
            relay.state_from_dict(d)
            bad.append(False)
        except ValueError:
            bad.append(True)
    check("state_from_dict：欄位型別不對／缺 task_id／不是物件／round 負數 → ValueError", all(bad), str(bad))
    with tempfile.TemporaryDirectory() as h:
        hp = Path(h)
        (hp / "tasks").mkdir()
        fallback, st_tf, cli = hp / "tasks" / "t1.json", hp / "state_task.json", hp / "cli.json"
        for p in (fallback, st_tf, cli):
            p.write_text("{}", encoding="utf-8")
        check("任務檔解析：命令列 > state.task_file > tasks/<id>.json；命令列給錯路徑 → None（不默默換別份）",
              relay.resolve_task_file(str(cli), str(st_tf), "t1", hp) == cli
              and relay.resolve_task_file(str(hp / "nope.json"), str(st_tf), "t1", hp) is None)
        orig_here = relay.HERE
        try:
            relay.HERE = hp  # 以臨時 HERE 驗預設值是呼叫當下的 HERE
            got = (relay.resolve_task_file(None, str(st_tf), "t1"), relay.resolve_task_file(None, str(hp / "gone.json"), "t1"),
                   relay.resolve_task_file(None, "", "t2"))
        finally:
            relay.HERE = orig_here
        check("任務檔解析：state.task_file > HERE/tasks/<id>.json；state 那份不在 → 退回 tasks/<id>.json；都沒有 → None",
              got == (st_tf, fallback, None), str(got))
    esc = notify.compose("escalate", "task-1", {"round": 2, "max_rounds": 2}).split("\n")
    print("       compose(escalate) 前兩行：", esc[0], "｜", esc[1])
    check("compose[escalate]：第 2 行提示寫 runs/<id>/human_notes.md 後 relay.py --resume <id>",
          esc[1].startswith("下一步：") and "runs/task-1/human_notes.md" in esc[1] and "relay.py --resume task-1" in esc[1], esc[1])
    row = relay.status_rows([{"task_id": "a", "phase": "done", "round": 3, "max_rounds": 4, "started": "2026-10-01T10:00:00+0800",
                              "updated": "2026-10-05T10:30:00+0800", "resumed": [{"ts": "2026-10-05T10:00:00+0800"}]}],
                            {}, datetime(2026, 10, 5, 12, 0, tzinfo=tz8))[0]
    check("--status 耗時：resume 過的棒從最後一次 resume 算（不含停著等人的那幾天）", row["elapsed"] == "30分", row["elapsed"])

    tids = ["_test_resume_a", "_test_resume_b", "_test_resume_c", "_test_resume_nostate", "_test_resume_bad",
            "_test_resume_nowt", "_test_resume_grp"]
    orig_ledger = relay.LEDGER
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as dd:
        d = Path(dd)
        relay.LEDGER = d / "ledger.jsonl"
        try:
            # ---- ① escalate 後 resume ------------------------------------------------------------
            tid = "_test_resume_a"
            wt, tf = mk_repo(d / "A", tid)
            rd = relay.HERE / "runs" / tid
            made: list = []
            s1 = {"impl": [{"write": {"a.py": "x = 1\n"}}, {"write": {"a.py": "x = 2\n"}}], "verify": [True, True],
                  "review": [(False, "總判定：需修改\n請把 x 改成 3（第一輪）"), (False, "總判定：需修改\n請把 x 改成 3（第二輪）")]}
            with scripted_main(s1, made):
                rc1, _ = call([str(tf)])
            st1 = state_of(tid)
            check("單棒未收斂：last_feedback 落檔（含最後一輪的審查原文）",
                  rc1 == 2 and st1["phase"] == "escalate" and "第二輪" in st1["last_feedback"], f"rc={rc1} {st1.get('last_feedback')!r}")
            notes = "請把 x 改成 42，其餘不動。"
            (rd / "human_notes.md").write_text(notes + "\n", encoding="utf-8-sig")  # 記事本存檔會帶 BOM
            r1_prompt = (rd / "impl_r1_prompt.md").read_bytes()
            made = []
            s2 = {"impl": [{"write": {"a.py": "x = 42\n"}}], "verify": [True], "review": [(True, "總判定：可合併")]}
            with scripted_main(s2, made):
                rc2, err2 = call(["--resume", tid])
            st2 = state_of(tid)
            run = made[0] if made else None
            impl = [c for c in (run.calls_log if run else []) if c["role"] == "impl"]
            rev = [c for c in (run.calls_log if run else []) if c["role"] == "review"]
            check("resume（escalate 後）：exit 0、第一個 resume 輪 rnd=3、STATE.round=3、started 保留原始開跑時間",
                  rc2 == 0 and impl and impl[0]["round"] == 3 and st2["round"] == 3 and st2["started"] == st1["started"],
                  f"rc={rc2} {err2[-300:]} rounds={[c['round'] for c in impl]}")
            check("resume：產生 impl_r3_prompt.md 且含人工意見", (rd / "impl_r3_prompt.md").is_file()
                  and notes in (rd / "impl_r3_prompt.md").read_text(encoding="utf-8"))
            check("resume：impl_r1_prompt.md 原封不動", (rd / "impl_r1_prompt.md").read_bytes() == r1_prompt)
            fb = impl[0]["feedback"] if impl else ""
            check("resume：第一輪 feedback 以「【人工審查意見」開頭", fb.startswith("【人工審查意見"), fb[:80])
            check("resume：第一輪 feedback 含 notes 與上一輪未解決的發現（last_feedback）",
                  notes in fb and "【上一輪未解決的發現" in fb and "第二輪" in fb, fb[:300])
            instr = (rd / "review_r3_instr.txt").read_text(encoding="utf-8") if (rd / "review_r3_instr.txt").is_file() else ""
            check("resume：審查指令 review_r3_instr.txt 含「【人工追加要求」與 notes", "【人工追加要求" in instr and notes in instr, instr[-200:])
            check("resume：人工意見同時進了實作者 prompt 與審查指令，且 BOM 已去掉",
                  bool(impl and rev) and notes in impl[0]["prompt"] and notes in rev[0]["instr"]
                  and "﻿" not in impl[0]["prompt"] + rev[0]["instr"])
            check("resume：human_notes.md 改名為 human_notes.r3.md（原檔不在）",
                  (rd / "human_notes.r3.md").is_file() and not (rd / "human_notes.md").exists())
            res = st2.get("resumed") or [{}]
            check("resume：STATE.resumed 一筆，notes_sha256／from_round=2／first_round=3 正確",
                  len(st2.get("resumed", [])) == 1 and res[0].get("notes_sha256") == hashlib.sha256(notes.encode("utf-8")).hexdigest()
                  and res[0].get("from_round") == 2 and res[0].get("first_round") == 3, str(res))
            handoff_r2 = (rd / "HANDOFF.r2.md").read_text(encoding="utf-8") if (rd / "HANDOFF.r2.md").is_file() else ""
            handoff_now = (rd / "HANDOFF.md").read_text(encoding="utf-8")
            check("resume：舊 HANDOFF 保存成 HANDOFF.r2.md（未收斂那份），新 HANDOFF.md 是 done",
                  bool(handoff_r2) and "（escalate" in handoff_r2.splitlines()[0] and "（done" in handoff_now.splitlines()[0],
                  f"{handoff_r2[:60]!r} {handoff_now[:60]!r}")
            cur = (rd / "CURRENT.md").read_text(encoding="utf-8")
            shown = g(wt, "show", "HEAD:a.py").stdout
            check("resume 收斂：commit 在 feat/c6（a.py＝x = 42）、commits 一筆、CURRENT 標「人工意見回灌，第 1 次 resume」",
                  st2["phase"] == "done" and st2["commits"] == [st2["commit"]] and shown == "x = 42\n"
                  and g(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "feat/c6" and "人工意見回灌，第 1 次 resume" in cur,
                  f"{st2.get('commits')} {shown!r} {cur[:80]!r}")

            # ---- ② 已收斂（已 commit）的棒 resume：同 branch 疊 commit、審查看累積 diff（D11）-------------
            tid = "_test_resume_b"
            wt, tf = mk_repo(d / "B", tid)
            rd = relay.HERE / "runs" / tid
            with scripted_main({"impl": [{"write": {"a.py": "x = 1\n"}}], "verify": [True], "review": [(True, "總判定：可合併")]}, []):
                rc1, _ = call([str(tf)])
            c1, c1_full = state_of(tid)["commit"], g(wt, "rev-parse", "HEAD").stdout.strip()
            task = json.loads(tf.read_text(encoding="utf-8"))
            task["allowed_paths"], task["prebuild"] = ["a.py", "b.py"], ["exit 7"]  # 要擴範圍先改任務檔；prebuild 會失敗
            tf.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
            (rd / "human_notes.md").write_text("再加 b.py：y = 2", encoding="utf-8")
            made = []
            with scripted_main({"impl": [{"write": {"b.py": "y = 2\n"}}], "verify": [True], "review": [(True, "總判定：可合併")]}, made):
                rc2, err2 = call(["--resume", tid])
            st2 = state_of(tid)
            log2 = g(wt, "log", "--oneline", "-2").stdout.strip().splitlines()
            print("       ② git log --oneline -2：", " ｜ ".join(log2))
            check("已收斂棒 resume：exit 0（任務檔的 prebuild 會失敗 → 證明 resume 沒跑 prebuild）", rc1 == 0 and rc2 == 0,
                  f"rc1={rc1} rc2={rc2} {err2[-300:]}")
            rdiff = (rd / "review_r2_diff.txt").read_text(encoding="utf-8") if (rd / "review_r2_diff.txt").is_file() else ""
            check("已收斂棒 resume：審查 diff 以 base_commit 為基準（含第一顆 commit 的 a.py 改動＋新的 b.py）",
                  "+x = 1" in rdiff and "+y = 2" in rdiff, rdiff[:400])
            new_files = g(wt, "show", "--name-only", "--format=", "HEAD").stdout.split()
            check("已收斂棒 resume：同 branch 多一顆 commit、前一顆 SHA 不變、新 commit 只收增量 b.py",
                  len(log2) == 2 and g(wt, "rev-parse", "HEAD~1").stdout.strip() == c1_full and new_files == ["b.py"]
                  and g(wt, "rev-list", "--count", "main..feat/c6").stdout.strip() == "2", f"{log2} {new_files}")
            check("已收斂棒 resume：state.commits 兩筆、commit 欄＝最新一顆",
                  len(st2["commits"]) == 2 and st2["commits"][0] == c1 and st2["commits"][1] == st2["commit"] != c1, str(st2["commits"]))

            # ---- ③ 拒跑：清楚的錯誤、exit 3、不改任何檔 ---------------------------------------------
            tid = "_test_resume_c"
            wt, tf = mk_repo(d / "C", tid, max_rounds=1)
            rd = relay.HERE / "runs" / tid
            with scripted_main({"impl": [{"write": {"a.py": "x = 1\n"}}], "verify": [True],
                                "review": [(False, "總判定：需修改")]}, []):
                call([str(tf)])

            def refused(label: str, argv: list[str], want: str, rd_: Path, wt_) -> None:
                before = snap(rd_, wt_)
                with scripted_main({"impl": [], "verify": [], "review": []}, []):
                    rc, err = call(argv)
                check(f"拒跑：{label} → exit 3、訊息含「{want}」、runs/ 與 worktree 沒有任何改動",
                      rc == 3 and want in err and snap(rd_, wt_) == before, f"rc={rc} err={err.strip()[-200:]}")

            refused("沒有 STATE", ["--resume", "_test_resume_nostate"], "STATE.json", relay.HERE / "runs" / "_test_resume_nostate", None)
            check("拒跑：沒有 STATE 時不會替它建 runs/<id>/", not (relay.HERE / "runs" / "_test_resume_nostate").exists())
            refused("human_notes.md 不存在", ["--resume", tid], "human_notes.md", rd, wt)
            (rd / "human_notes.md").write_bytes(b"\xef\xbb\xbf  \r\n\t\n")
            refused("human_notes.md 只有空白／BOM", ["--resume", tid], "是空的", rd, wt)
            for label, text in (("STATE 損壞（不是 JSON）", "{oops"), ("STATE 損壞（round 型別不對）", json.dumps({"task_id": "_test_resume_bad", "round": "2"}))):
                bd = relay.HERE / "runs" / "_test_resume_bad"
                bd.mkdir(parents=True, exist_ok=True)
                (bd / "STATE.json").write_text(text, encoding="utf-8")
                (bd / "human_notes.md").write_text("x", encoding="utf-8")
                refused(label, ["--resume", "_test_resume_bad"], "損壞", bd, None)
            nd = relay.HERE / "runs" / "_test_resume_nowt"
            nd.mkdir(parents=True, exist_ok=True)
            missing_wt = d / "no_such_worktree"
            ntask = {**json.loads(tf.read_text(encoding="utf-8")), "id": "_test_resume_nowt", "worktree": str(missing_wt)}
            (d / "nowt.json").write_text(json.dumps(ntask, ensure_ascii=False), encoding="utf-8")
            (nd / "STATE.json").write_text(json.dumps({"task_id": "_test_resume_nowt", "phase": "escalate", "round": 1,
                                                       "worktree": str(missing_wt), "task_file": str(d / "nowt.json")}), encoding="utf-8")
            (nd / "human_notes.md").write_text("x", encoding="utf-8")
            refused("worktree 不存在", ["--resume", "_test_resume_nowt"], "worktree 不存在", nd, None)
            (rd / "human_notes.md").write_text("請改成 x = 5", encoding="utf-8")  # 以下各條：除了被測條件，其餘都合格
            held = relay.runlock.try_hold(relay.task_lock_path(tid))
            try:
                refused("任務鎖被持有（同一任務正在跑）", ["--resume", tid], "正在跑", rd, wt)
            finally:
                held.release()
            gd = relay.HERE / "runs" / "_test_resume_grp"
            gd.mkdir(parents=True, exist_ok=True)
            (gd / "GROUP.json").write_text("{}", encoding="utf-8")
            refused("群組 id（runs/<id>/GROUP.json）", ["--resume", "_test_resume_grp"], "群組不能 resume", gd, None)
            orig_task = tf.read_text(encoding="utf-8")
            tf.write_text(json.dumps({**json.loads(orig_task), "candidates": [{"implementer": "codex"}, {"implementer": "codex"}]}),
                          encoding="utf-8")
            refused("任務檔宣告 candidates（群組）", ["--resume", tid], "群組不能 resume", rd, wt)
            tf.write_text(orig_task, encoding="utf-8")
            g(wt, "checkout", "-q", "main")  # 測試自己切走（relay 不會做這件事）
            refused("worktree 不在任務分支上", ["--resume", tid], "relay 不代為 checkout", rd, wt)
            g(wt, "checkout", "-q", "feat/c6")
            (rd / "impl_r5_prompt.md").write_text("殘留", encoding="utf-8")
            refused("runs/<id>/ 有比 STATE.round 新的輪次紀錄", ["--resume", tid], "狀態對不上", rd, wt)
            (rd / "impl_r5_prompt.md").unlink()
            refused("--rounds 0", ["--resume", tid, "--rounds", "0"], "--rounds", rd, wt)
            refused("--rounds 沒配 --resume", [str(tf), "--rounds", "2"], "--rounds", rd, wt)

            # ---- dry-run＋resume：只印計畫（讀 notes、不改名、不寫 .dry 以外的檔）--------------------
            before = snap(rd, wt)
            relay.paths.check_all, orig_check = (lambda **kw: ["不該被呼叫"]), relay.paths.check_all
            try:
                rc, err = call(["--resume", tid, "--dry-run"])
            finally:
                relay.paths.check_all = orig_check
            dry_prompt = relay.HERE / "runs" / (tid + ".dry") / "impl_r2_prompt.md"
            check("dry-run＋resume：exit 0、notes 不改名、runs/<id>/ 與 worktree 不變、計畫寫在 .dry（impl_r2_prompt.md 含 notes）",
                  rc == 0 and snap(rd, wt) == before and (rd / "human_notes.md").is_file() and dry_prompt.is_file()
                  and "請改成 x = 5" in dry_prompt.read_text(encoding="utf-8"), f"rc={rc} {err[-300:]}")
        finally:
            relay.LEDGER = orig_ledger
            for t in tids:
                rm_runs(t)


FAKE_CLAUDE = r'''import os, sys
from pathlib import Path
data = sys.stdin.buffer.read()
out = Path(sys.argv[2])
(out / "got_prompt.bin").write_bytes(data)
(out / "env_seen.txt").write_text("yes" if "CLAUDECODE" in os.environ else "no", encoding="utf-8")
sys.stdout.write(Path(sys.argv[1]).read_text(encoding="utf-8"))
'''


def test_impl_command() -> None:
    """C4a（2026-10-05）：實作者可插拔 codex｜claude、prompt 走 stdin、生產目錄守門、需求計算、IMPL_RULES 不寫死路徑。
    不呼叫任何 AI CLI：claude 的管線測試用假 CLI（python 腳本）吐 fixtures/claude_stream_ok 的真樣本。"""
    import shutil
    import subprocess

    def g(wt, *a: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True, encoding="utf-8")

    def init_repo(p: Path, branch: str | None = None) -> None:
        p.mkdir(parents=True)
        g(p, "init", "-q", "-b", "main"); g(p, "config", "user.email", "t@t"); g(p, "config", "user.name", "t")
        g(p, "config", "core.autocrlf", "false")
        (p / "a.py").write_bytes(b"x = 0\n")
        g(p, "add", "-A"); g(p, "commit", "-qm", "base")
        if branch:
            g(p, "checkout", "-q", "-b", branch)

    # 1. claude 的指令（4 項）
    out_rel = Path("runs") / "_x_last.md"
    c = relay.build_impl_command("claude", "C:/code/wt", out_rel)
    av = c.argv
    tools = av[av.index("--tools") + 1] if "--tools" in av else ""
    check("claude 指令：--tools 只有讀寫檔五個、不含 Bash", "Bash" not in tools and set(tools.split(",")) == {"Read", "Edit", "Write", "Glob", "Grep"}, str(av))
    check("claude 指令：--strict-mcp-config＋空 mcpServers＋--safe-mode（D12）", "--strict-mcp-config" in av and "--safe-mode" in av
          and json.loads(av[av.index("--mcp-config") + 1]) == {"mcpServers": {}}, str(av))
    check("claude 指令：-p＋stream-json＋--verbose＋acceptEdits＋--no-session-persistence",
          "-p" in av and av[av.index("--output-format") + 1] == "stream-json" and "--verbose" in av
          and av[av.index("--permission-mode") + 1] == "acceptEdits" and "--no-session-persistence" in av, str(av))
    check("claude 指令：prompt 不在 argv（沒有守則字串、沒有 '-'）；drop_env＝paths.CLAUDE_NESTED_ENV；前綴 CLAUDE",
          not any("守則" in x for x in av) and "-" not in av and c.drop_env == relay.paths.CLAUDE_NESTED_ENV and c.prefix == "CLAUDE", str(c))
    # 2. codex 的指令（3 項）
    k = relay.build_impl_command("codex", "C:/code/wt", out_rel)
    check("codex 指令：最後一個參數是 '-'（prompt 從 stdin 讀）", k.argv[-1] == "-", str(k.argv))
    check("codex 指令：-s workspace-write、前綴 CODEX、不刪 env", k.argv[k.argv.index("-s") + 1] == "workspace-write"
          and k.prefix == "CODEX" and k.drop_env == (), str(k.argv))
    o = k.argv[k.argv.index("-o") + 1]
    check("codex 指令：-o 是絕對路徑（相對路徑會以 -C 為基準落進受測 repo）", Path(o).is_absolute() and o.endswith("_x_last.md"), o)
    # 3. model／effort 有給才出現（2 項＋錯誤 1 項）
    c1, c0 = relay.build_impl_command("claude", "w", out_rel, model="sonnet", effort="high"), relay.build_impl_command("claude", "w", out_rel)
    check("claude：model／effort 有給才出現 --model sonnet／--effort high",
          c1.argv[c1.argv.index("--model") + 1] == "sonnet" and c1.argv[c1.argv.index("--effort") + 1] == "high"
          and "--model" not in c0.argv and "--effort" not in c0.argv, str(c1.argv))
    k1 = relay.build_impl_command("codex", "w", out_rel, model="gpt-x")
    check("codex：model 有給才出現 -m（且仍在 '-' 之前）", k1.argv[k1.argv.index("-m") + 1] == "gpt-x" and k1.argv[-1] == "-"
          and "-m" not in k.argv, str(k1.argv))
    errs = []
    for kw in ({"cli": "gemini"}, {"cli": "codex", "effort": "high"}):
        try:
            relay.build_impl_command(kw.pop("cli"), "w", out_rel, **kw)
            errs.append(False)
        except ValueError:
            errs.append(True)
    check("build_impl_command：未知實作者、codex 給 effort → ValueError（大聲錯，不靜默）", all(errs), str(errs))

    # 4. stream() 的 input_text／drop_env（2 項＋提早退出 1 項）
    big = ("x" * 99 + "\n") * 1000
    with contextlib.redirect_stdout(io.StringIO()):
        cp = relay.stream([sys.executable, "-c", "import sys;b=sys.stdin.buffer.read();print(len(b), b.count(b'\\r'))"],
                          "RELAY", None, input_text=big, timeout=120)
    check("stream(input_text)：10 萬字元經 stdin 原樣送達（不受 32K 限制、不互卡、\\n 沒被轉成 \\r\\n）",
          cp.returncode == 0 and cp.stdout.strip() == "100000 0", f"rc={cp.returncode} out={cp.stdout!r}")
    probe = [sys.executable, "-c", "import os;print(os.environ.get('RELAY_TEST_FOO', '<none>'))"]
    os.environ["RELAY_TEST_FOO"] = "1"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            cp_drop = relay.stream(probe, "RELAY", None, drop_env=("RELAY_TEST_FOO",), timeout=60)
            cp_keep = relay.stream(probe, "RELAY", None, timeout=60)
    finally:
        os.environ.pop("RELAY_TEST_FOO", None)
    check("stream(drop_env)：外部設 RELAY_TEST_FOO=1 → 子行程看不到（對照：不給 drop_env 看得到）",
          cp_drop.stdout.strip() == "<none>" and cp_keep.stdout.strip() == "1", f"{cp_drop.stdout!r} {cp_keep.stdout!r}")
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        cp = relay.stream([sys.executable, "-c", "import sys;sys.exit(5)"], "RELAY", None, input_text="y" * 5_000_000, timeout=60)
    check("stream(input_text)：子行程沒讀 stdin 就退出 → 寫入端的 BrokenPipe 被吞掉、照常回 exit code",
          cp.returncode == 5 and "Traceback" not in err.getvalue(), f"rc={cp.returncode} {err.getvalue()[-200:]!r}")

    # 5. _claude_line（4 項＋真樣本 1 項）
    L = relay._claude_line
    ev = lambda *blocks: json.dumps({"type": "assistant", "message": {"content": list(blocks)}}, ensure_ascii=False)  # noqa: E731
    check("_claude_line：assistant text → 💬 前 100 字（換行壓成空白）",
          L(ev({"type": "text", "text": "我先讀檔\n再改"})) == "💬 我先讀檔 再改" and len(L(ev({"type": "text", "text": "長" * 300}))) == 102)
    check("_claude_line：tool_use Edit → ✏ Edit <basename>；Read → $ Read <basename>",
          L(ev({"type": "tool_use", "name": "Edit", "input": {"file_path": "C:\\code\\proj\\pkg\\mod.py"}})) == "✏ Edit mod.py"
          and L(ev({"type": "tool_use", "name": "Read", "input": {"file_path": "/code/proj/README.md"}})) == "$ Read README.md")
    res = {"type": "result", "is_error": False, "usage": {"input_tokens": 2, "cache_creation_input_tokens": 10,
                                                            "cache_read_input_tokens": 3, "output_tokens": 4}}
    check("_claude_line：result → ✓ 完成 in=<三者相加> out=<output>", L(json.dumps(res)) == "✓ 完成 in=15 out=4", str(L(json.dumps(res))))
    check("_claude_line：非 JSON／壞 JSON／其他事件 → None",
          L("not json") is None and L("{broken") is None and L(json.dumps({"type": "user"})) is None and L("") is None)
    fx = relay.HERE / "fixtures" / "claude_stream_ok.stdout.txt"
    shown = [s for s in (L(x) for x in fx.read_text(encoding="utf-8").splitlines()) if s]
    check("_claude_line：真樣本 claude_stream_ok 只印兩行（💬 OK、✓ 完成 in=7114 out=4）", shown == ["💬 OK", "✓ 完成 in=7114 out=4"], str(shown))

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as dd:
        d = Path(dd)
        orig_ledger = relay.LEDGER
        relay.LEDGER = d / "ledger.jsonl"
        try:
            # 4b. implement() 管線：假 claude 吐真樣本（2 項）
            wt = d / "wt"
            wt.mkdir()
            (d / "spec.md").write_text("規格第一行\n第二行\n", encoding="utf-8")
            (d / "fake_claude.py").write_text(FAKE_CLAUDE, encoding="utf-8")
            orig_build, seen = relay.build_impl_command, {}

            def fake_build(cli, wt_, out_last, model=None, effort=None):
                real = orig_build(cli, wt_, out_last, model=model, effort=effort)
                seen.update(cli=cli, model=model, effort=effort)
                return relay.ImplCmd([sys.executable, str(d / "fake_claude.py"), str(fx), str(d)], real.prefix, real.line_fn, real.drop_env)

            tid = "_test_impl_fake_claude"
            env_bak = os.environ.get("CLAUDECODE")
            os.environ["CLAUDECODE"] = "1"  # 模擬在 Claude Code 對話裡跑 relay
            try:
                relay.build_impl_command = fake_build
                r = relay.Run({"id": tid, "worktree": str(wt), "repo": str(wt), "spec_file": str(d / "spec.md"),
                               "implementer": "claude", "implementer_models": {"claude": "sonnet"},
                               "implementer_effort": {"claude": "high"}}, dry=False)
                term = io.StringIO()
                with contextlib.redirect_stdout(term):
                    ok, report, usage = r.implement(1, "上一輪：請修 x")
                rec = r.state.calls[-1] if r.state.calls else {}
                check("implement(claude)：判定 ok、報告＝result 文字、CallRecord.cli=claude、model／effort 照任務傳",
                      ok and report == "OK" and rec.get("cli") == "claude" and (usage or {}).get("input_tokens_total") == 7114
                      and seen == {"cli": "claude", "model": "sonnet", "effort": "high"} and "✓ 完成 in=7114 out=4" in term.getvalue(),
                      f"ok={ok} report={report!r} rec={rec} seen={seen}")
                want = (r.dir / "impl_r1_prompt.md").read_text(encoding="utf-8").encode("utf-8")
                got = (d / "got_prompt.bin").read_bytes() if (d / "got_prompt.bin").is_file() else b""
                check("implement(claude)：prompt 經 stdin 逐位元組送達（無 \\r）、子行程看不到 CLAUDECODE",
                      got == want and b"\r" not in got and "【規格】" in got.decode("utf-8")
                      and (d / "env_seen.txt").read_text(encoding="utf-8") == "no", f"len got={len(got)} want={len(want)}")
            finally:
                relay.build_impl_command = orig_build
                if env_bak is None:
                    os.environ.pop("CLAUDECODE", None)
                else:
                    os.environ["CLAUDECODE"] = env_bak
                rm_runs(tid)

            # 6. implementer 欄位驗證（2 項＋models／effort 1 項）
            (d / "rev.md").write_text("1. x 有改", encoding="utf-8")
            base = {"id": "_test_impl_task", "repo": str(d / "nope"), "base_branch": "main", "branch": "feat/x",
                    "worktree": str(d / "nope_wt"), "spec_file": str(d / "spec.md"), "verify": [],
                    "review": {"policy": "always", "instructions_file": str(d / "rev.md")}}

            def main_rc(extra: dict, *flags: str) -> tuple[int, str]:
                tf = d / "t.json"
                tf.write_text(json.dumps({**base, **extra}, ensure_ascii=False), encoding="utf-8")
                e = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(e):
                    rc = relay.main([str(tf), *flags])
                return rc, e.getvalue()

            rc, e = main_rc({"implementer": "gemini"}, "--dry-run")
            check("task implementer='gemini' → main 回 3（dry-run 也擋；訊息點名 implementer）", rc == 3 and "implementer" in e, f"rc={rc} {e!r}")
            (d / "t0.json").write_text(json.dumps(base), encoding="utf-8")
            t0 = relay.load_task(d / "t0.json")
            with contextlib.redirect_stdout(io.StringIO()):
                r0 = relay.Run(t0, dry=True)
            check("task 缺 implementer → 預設 codex（Run.impl_cli 與需求計算）",
                  r0.impl_cli == "codex" and relay.required_clis(t0) == {"need_codex": True, "need_claude": False, "need_agy": True})
            rm_runs("_test_impl_task")
            rcs = [main_rc(x, "--dry-run")[0] for x in ({"implementer_effort": {"codex": "high"}}, {"implementer_effort": {"claude": "turbo"}},
                                                         {"implementer_models": {"gemini": "x"}}, {"implementer_models": {"claude": ""}},
                                                         {"implementer": ["codex", "gemini"]})]
            check("implementer_models／implementer_effort 寫錯、implementer 清單含未知值 → main 回 3", rcs == [3] * 5, str(rcs))

            # 8. check_all 需求計算（2 項）
            calls: list = []

            def rec_check(**kw):
                calls.append(kw)
                return ["（測試）假裝缺 CLI"]

            orig_check = relay.paths.check_all
            relay.paths.check_all = rec_check
            try:
                rc1, _ = main_rc({"review": {**base["review"], "policy": "never"}})
                rc2, _ = main_rc({"implementer": "claude"})
            finally:
                relay.paths.check_all = orig_check
            check("check_all 需求：policy=never＋codex → 不要求 agy、不要求 claude", rc1 == 3 and calls[:1] == [
                {"need_codex": True, "need_agy": False, "need_claude": False}], str(calls))
            check("check_all 需求：implementer=claude＋policy=always → 要求 claude 與 agy、不要求 codex", rc2 == 3 and calls[1:2] == [
                {"need_codex": False, "need_agy": True, "need_claude": True}], str(calls))
            rm_runs("_test_impl_task")

            # 7. prod_snapshot（3 項＋對照 1 項）
            prod = d / "prod"
            init_repo(prod)
            s1 = relay.prod_snapshot(str(prod))
            (prod / "a.py").write_bytes(b"x = 1\n")
            s2 = relay.prod_snapshot(str(prod))
            (prod / "a.py").write_bytes(b"x = 0\n")
            s3 = relay.prod_snapshot(str(prod))
            check("prod_snapshot：改一個追蹤檔 → 快照不同；還原內容 → 與原快照相同", s1 is not None and s2 != s1 and s3 == s1,
                  f"{s1!r} / {s2!r} / {s3!r}")
            (d / "plain").mkdir()
            check("prod_snapshot：None 或不是 git repo → None", relay.prod_snapshot(None) is None and relay.prod_snapshot("") is None
                  and relay.prod_snapshot(str(d / "plain")) is None and relay.prod_snapshot(str(d / "no_such_dir")) is None)
            idx = prod / ".git" / "index"
            st = (prod / "a.py").stat()
            os.utime(prod / "a.py", ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))  # 內容不變、mtime 變：普通 status 會想刷新 index
            m0, b0 = idx.stat().st_mtime_ns, idx.read_bytes()
            relay.prod_snapshot(str(prod))
            check("prod_snapshot：呼叫後生產 repo 的 .git/index mtime 與內容都不變（--no-optional-locks）",
                  idx.stat().st_mtime_ns == m0 and idx.read_bytes() == b0)
            g(prod, "status", "--porcelain")
            check("對照：同樣情況下普通 git status 會改寫 .git/index（證明上一條量得到）", idx.read_bytes() != b0 or idx.stat().st_mtime_ns != m0)
            (prod / "untracked").mkdir()
            (prod / "untracked" / "f1.txt").write_bytes(b"1\n")
            s4 = relay.prod_snapshot(str(prod))
            (prod / "untracked" / "f2.txt").write_bytes(b"2\n")
            (prod / "a.py").write_bytes(b"x = 9\n")
            s5 = relay.prod_snapshot(str(prod))
            (prod / "a.py").write_bytes(b"x = 99\n")
            s6 = relay.prod_snapshot(str(prod))
            check("prod_snapshot：未追蹤目錄裡多一個檔、本來就髒的檔再被改 → 都看得到（比規格多的兩點）",
                  s5 != s4 and "untracked/f2.txt" in s5 and s6 != s5, f"{s4!r} / {s5!r}")
            (prod / "a.py").write_bytes(b"x = 0\n")
            shutil.rmtree(prod / "untracked")

            # 守門整合（經 main＋ScriptedRun）：實作者寫進生產目錄 → 中止；沒寫 → 收斂、commit 訊息照實寫（3 項）
            tids = ["_test_impl_guard_hit", "_test_impl_guard_ok"]
            for tid in tids:
                init_repo(d / tid, branch="feat/c4a")
            (d / "spec.md").write_text("把 x 改掉", encoding="utf-8")

            def guard_task(tid: str) -> Path:
                t = {**base, "id": tid, "title": "c4a 守門", "repo": str(d / tid), "worktree": str(d / tid), "branch": "feat/c4a",
                     "production_dir": str(prod), "verify": [{"name": "v", "cmd": "echo ok"}], "allowed_paths": ["a.py"],
                     "max_rounds": 1, "implementer": "claude"}
                tf = d / f"{tid}.json"
                tf.write_text(json.dumps(t, ensure_ascii=False), encoding="utf-8")
                return tf

            try:
                made: list = []
                hit = {"impl": [{"write": {"a.py": "x = 1\n"}, "write_abs": {str(prod / "evil.txt"): "x"}}], "verify": [True],
                       "review": [(True, "總判定：可合併")]}
                head0 = g(d / tids[0], "rev-parse", "HEAD").stdout
                with scripted_main(hit, made), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    rc = relay.main([str(guard_task(tids[0]))])
                s = json.loads((relay.HERE / "runs" / tids[0] / "STATE.json").read_text(encoding="utf-8"))
                check("守門：實作者寫進生產目錄 → exit 3、STATE aborted、原因點名生產目錄與 evil.txt、沒有 commit",
                      rc == 3 and s["phase"] == "aborted" and "生產目錄" in s["abort_reason"] and "evil.txt" in s["abort_reason"]
                      and g(d / tids[0], "rev-parse", "HEAD").stdout == head0, f"rc={rc} {s.get('abort_reason')!r}")
                (prod / "evil.txt").unlink()
                made = []
                with scripted_main({"impl": [{"write": {"a.py": "x = 2\n"}}], "verify": [True], "review": [(True, "總判定：可合併")]}, made), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    rc = relay.main([str(guard_task(tids[1]))])
                s = json.loads((relay.HERE / "runs" / tids[1] / "STATE.json").read_text(encoding="utf-8"))
                check("守門：生產目錄沒被動 → 不誤報、收斂 exit 0、STATE.implementer=claude、實作者拿到 cli=claude",
                      rc == 0 and s["phase"] == "done" and s.get("implementer") == "claude"
                      and bool(made) and made[0].calls_log[0].get("cli") == "claude", f"rc={rc} {s.get('abort_reason')!r}")
                body = g(d / tids[1], "log", "-1", "--format=%B").stdout
                check("commit 訊息（D15）：寫「Claude Code 實作、agy 審查」、沒有 Co-Authored-By", "Claude Code 實作、agy 審查" in body
                      and "Co-Authored-By" not in body, body)
            finally:
                for tid in tids:
                    rm_runs(tid)
        finally:
            relay.LEDGER = orig_ledger

    # 9. IMPL_RULES 不寫死本機路徑（1 項）：檢查原始碼（執行時的 IMPL_RULES 本來就會含 {HERE}，在 D: 槽也會有 "D:\"）
    src = Path(relay.__file__).read_text(encoding="utf-8")
    seg = src[src.index("IMPL_RULES = "):src.index("REVIEW_RULES = ")]
    check("IMPL_RULES 原始碼不含 \"D:\\\" 字面與 Tooling；執行時改用 {HERE}",
          "D:\\" not in seg and "Tooling" not in seg and str(relay.HERE) in relay.IMPL_RULES, seg[:200])


def test_handoff() -> None:
    """C5（2026-10-05）：judge 判 rate_limit 時換另一支 CLI 接手實作（規格 §8 測試案例 1–7，另加邊界）。
    真 rate_limit 樣本不存在 ⇒ 全程 ScriptedRun 合成，不呼叫任何 AI CLI。帳本、鎖指暫存；推播設定指到不存在的路徑，
    推播內容另以假的 notify.notify 攔截（不呼叫任何通道）；臨時 repo 的 autocrlf 在 local 明設。"""
    import copy
    import re
    import subprocess
    from datetime import datetime, timedelta, timezone
    from tools import notify

    def g(wt, *a: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True, encoding="utf-8")

    def mk_repo(d: Path, tid: str, **extra) -> tuple[Path, Path]:
        """臨時 repo＝worktree（main 一顆 commit，切到 feat/c5），任務檔放 repo 外；預設 implementer＝[codex, claude]。"""
        wt = d / "repo"
        wt.mkdir(parents=True)
        g(wt, "init", "-q", "-b", "main"); g(wt, "config", "user.email", "t@t"); g(wt, "config", "user.name", "t")
        g(wt, "config", "core.autocrlf", "false")
        (wt / "a.py").write_bytes(b"x = 0\n")
        g(wt, "add", "-A"); g(wt, "commit", "-qm", "base"); g(wt, "checkout", "-q", "-b", "feat/c5")
        (d / "spec.md").write_text("把 x 改成 1", encoding="utf-8")
        (d / "review.md").write_text("1. x 有改", encoding="utf-8")
        task = {"id": tid, "title": "c5 測試", "repo": str(wt), "base_branch": "main", "branch": "feat/c5", "worktree": str(wt),
                "spec_file": str(d / "spec.md"), "verify": [{"name": "v", "cmd": "echo ok"}],
                "review": {"policy": "always", "instructions_file": str(d / "review.md")},
                "allowed_paths": ["a.py"], "max_rounds": 2, "implementer": ["codex", "claude"], **extra}
        tf = d / "task.json"
        tf.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        return wt, tf

    def call(argv: list[str]) -> tuple[int, str, str]:
        """跑 main；stdout 去掉 ANSI 色碼（emit 只給 [TAG] 上色）才比對得到「[JUDGE] …」整句。"""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = relay.main(argv)
        return rc, re.sub(r"\x1b\[[0-9;]*m", "", out.getvalue()), err.getvalue()

    def state_of(tid: str) -> dict:
        return json.loads((relay.HERE / "runs" / tid / "STATE.json").read_text(encoding="utf-8"))

    def impls(run) -> list[dict]:
        return [c for c in run.calls_log if c["role"] == "impl"]

    tz8 = timezone(timedelta(hours=8))
    t0 = datetime(2026, 10, 5, 12, 0, tzinfo=tz8)
    fmt = "%Y-%m-%dT%H:%M:%S%z"
    order = ["codex", "claude"]
    P = relay.pick_implementer
    # ---- 1. pick_implementer（5 項）---------------------------------------------------------------
    near = {"codex": t0 - timedelta(minutes=10)}
    both = {"codex": t0 - timedelta(minutes=10), "claude": t0 - timedelta(minutes=20)}
    check("pick_implementer：無冷卻 → 第一個（codex）", P(order, set(), {}, 0) == "codex")
    check("pick_implementer：cooling={codex} → claude", P(order, {"codex"}, {}, 0) == "claude")
    check("pick_implementer：全部 cooling → None", P(order, {"codex", "claude"}, {}, 0) is None)
    check("pick_implementer：cooldown 開且 codex 在窗內 → claude（cooldown=0 時同一份 recent 不影響 → codex）",
          P(order, set(), near, 60) == "claude" and P(order, set(), near, 0) == "codex")
    check("pick_implementer：cooldown 開且兩支都在窗內 → codex（不回 None：永不讓任務無人可用）", P(order, set(), both, 60) == "codex")

    # ---- 2. ledger_recent_rate_limits（1 項）＋ --ledger 的 last_rate_limit（1 項）----------------------
    orig_ledger = relay.LEDGER
    with tempfile.TemporaryDirectory() as h:
        led = Path(h) / "ledger.jsonl"
        rows = [{"ts": (t0 - timedelta(minutes=30)).strftime(fmt), "cli": "codex", "failure_class": "rate_limit"},
                {"ts": (t0 - timedelta(minutes=90)).strftime(fmt), "cli": "claude", "failure_class": "rate_limit"},
                {"ts": "壞掉的時間", "cli": "agy", "failure_class": "rate_limit"},
                {"ts": (t0 - timedelta(minutes=5)).strftime(fmt), "cli": "claude", "failure_class": None}]
        led.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n{壞行\n", encoding="utf-8")
        got = relay.ledger_recent_rate_limits(t0, 60, led)
        check("ledger_recent_rate_limits：30 分前／90 分前／壞 ts／非 rate_limit 各一筆，minutes=60 → 只回 30 分那筆；minutes=0 → {}",
              got == {"codex": t0 - timedelta(minutes=30)} and relay.ledger_recent_rate_limits(t0, 0, led) == {}, str(got))
        relay.LEDGER = led
        try:
            rc, out, _ = call(["--ledger"])
        finally:
            relay.LEDGER = orig_ledger
        lines = {ln.split()[0]: ln for ln in out.splitlines() if ln.strip()}
        check("--ledger：每支 CLI 多 last_rate_limit= 欄（codex＝那筆 ts；沒撞過的 → -）",
              rc == 0 and f"last_rate_limit={rows[0]['ts']}" in lines.get("codex", "")
              and lines.get("claude", "").endswith(f"last_rate_limit={rows[1]['ts']}") and all("last_rate_limit=" in v for v in lines.values()), out)
        led.write_text(json.dumps({"ts": rows[3]["ts"], "cli": "claude", "failure_class": None}) + "\n", encoding="utf-8")
        relay.LEDGER = led
        try:
            rc, out, _ = call(["--ledger"])
        finally:
            relay.LEDGER = orig_ledger
        check("--ledger：從沒撞過牆的 CLI → last_rate_limit=-", rc == 0 and out.strip().endswith("last_rate_limit=-"), out)

    # ---- 任務檔：implementer 清單與 handoff_cooldown_minutes 的驗證（1 項）＋需求計算（1 項）＋推播文案（1 項）----
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as h:
        hp = Path(h)
        (hp / "spec.md").write_text("s", encoding="utf-8")
        (hp / "rev.md").write_text("r", encoding="utf-8")
        base = {"id": "_test_handoff_cfg", "repo": str(hp / "nope"), "base_branch": "main", "branch": "feat/x",
                "worktree": str(hp / "nope_wt"), "spec_file": str(hp / "spec.md"), "verify": [],
                "review": {"policy": "always", "instructions_file": str(hp / "rev.md")}}
        rcs = []
        for extra in ({"implementer": []}, {"implementer": ["codex", "codex"]}, {"implementer": ["codex", 1]}, {"implementer": "Codex"},
                      {"handoff_cooldown_minutes": -1}, {"handoff_cooldown_minutes": True}, {"handoff_cooldown_minutes": "5"},
                      {"handoff_cooldown_minutes": 1.5}, {"implementer": ["codex", "claude"], "handoff_cooldown_minutes": 30}):
            (hp / "t.json").write_text(json.dumps({**base, **extra}), encoding="utf-8")
            rcs.append(call([str(hp / "t.json"), "--dry-run"])[:2])
        rm_runs("_test_handoff_cfg")
        check("任務檔：implementer 空清單／重複／非字串／大小寫錯、cooldown 負數／bool／字串／小數 → 3；[codex, claude]＋cooldown 30 dry-run → 0 且印出換手順序",
              [r[0] for r in rcs] == [3] * 8 + [0] and "實作者順序 codex → claude" in rcs[-1][1] and "帳本冷卻 30 分" in rcs[-1][1],
              str([r[0] for r in rcs]))
        t_list = relay.load_task(hp / "t.json")
        check("required_clis：implementer=[codex, claude] → 兩支都要（備援缺了要在開跑前大聲說）",
              relay.required_clis(t_list) == {"need_codex": True, "need_claude": True, "need_agy": True}, str(relay.required_clis(t_list)))
    c_rl = notify.compose("rate_limit", "t1", {"cli": "agy", "role": "reviewer", "minutes": 4}).split("\n")
    c_ok = notify.compose("ready_to_merge", "t1", {"round": 1, "commit": "abc", "minutes": 3, "handoff": "codex→claude"}).split("\n")
    print("       compose(rate_limit, reviewer)：", " ｜ ".join(c_rl))
    check("推播文案：審查者撞牆第 1 行「撞牆停下：審查者 agy」、第 2 行給 --resume；換手後收斂第 3 行附「途中 codex→claude 換手」",
          "撞牆停下：審查者 agy 回 rate_limit" in c_rl[0] and "relay.py --resume t1" in c_rl[1]
          and c_ok[2] == "耗時 3 分；途中 codex→claude 換手", f"{c_rl} {c_ok}")

    tids = ["_test_handoff_a", "_test_handoff_b", "_test_handoff_c", "_test_handoff_d1", "_test_handoff_d2", "_test_handoff_d3",
            "_test_handoff_rec", "_test_handoff_real"]
    orig_notify, orig_build = notify.notify, relay.build_impl_command
    sent: list = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as dd:
        d = Path(dd)
        relay.LEDGER = d / "ledger.jsonl"
        notify.notify = lambda kind, task_id, info, **kw: (sent.append((kind, task_id, dict(info))) or "推播關閉")
        try:
            # ---- 3. codex 撞牆 → claude 同一輪接手並收斂（5 項＋6 項）-----------------------------------------
            tid = "_test_handoff_a"
            wt, tf = mk_repo(d / "A", tid)
            rd = relay.HERE / "runs" / tid
            made: list = []
            s = {"impl": [{"ok": False, "failure_class": "rate_limit", "write": {"a.py": "x = 1  # 半成品\n"}},
                          {"write": {"a.py": "x = 1\n"}}],
                 "verify": [True], "review": [(True, "總判定：可合併")]}
            with scripted_main(s, made):
                rc, out, err = call([str(tf)])
            st = state_of(tid)
            run = made[0] if made else None
            im = impls(run) if run else []
            print("       ③ [JUDGE] 行：", " ｜ ".join(ln.split("[JUDGE] ", 1)[-1][:70] for ln in out.splitlines() if "[JUDGE]" in ln))
            check("換手不消耗輪數：收斂 exit 0、state.round==1（沒有開第 2 輪）", rc == 0 and st["round"] == 1 and st["phase"] == "done",
                  f"rc={rc} round={st.get('round')} {err[-300:]}")
            check("換手不消耗輪數：該輪 verify 只跑 1 次", len(st["verify"]) == 1 and s["verify"] == [], str(st["verify"]))
            hs = st.get("handoffs", [])
            check("state.handoffs 一筆：round 1 codex→claude、reason=rate_limit、記原始輸出路徑",
                  len(hs) == 1 and (hs[0]["round"], hs[0]["from"], hs[0]["to"], hs[0]["reason"]) == (1, "codex", "claude", "rate_limit")
                  and hs[0].get("stdout_file", "").endswith("impl_r1.stdout.txt"), str(hs))
            check("第二次實作的 feedback 以「【換手說明】」開頭、點名前一位（Codex）",
                  len(im) == 2 and im[1]["feedback"].startswith("【換手說明】") and "Codex" in im[1]["feedback"].split("\n")[0], str(im[1:]))
            check("換手那次的紀錄檔名含 _h1_claude（prompt 與 stdout），第一次的 impl_r1_prompt.md 沒被覆蓋",
                  len(im) == 2 and im[1]["stem"] == "impl_r1_h1_claude" and (rd / "impl_r1_h1_claude_prompt.md").is_file()
                  and "_h1_claude" in st["calls"][1]["stdout_file"] and (rd / "impl_r1_prompt.md").is_file()
                  and "【換手說明】" not in (rd / "impl_r1_prompt.md").read_text(encoding="utf-8"), str([c.get("stem") for c in im]))
            check("D9 worktree 不重置：claude 開工時看得到 codex 留下的半成品", len(im) == 2 and im[1]["a_py"] == "x = 1  # 半成品\n",
                  str([c.get("a_py") for c in im]))
            check("實作者呼叫依序是 codex（attempt 0）→ claude（attempt 1）",
                  [(c["cli"], c["attempt"]) for c in im] == [("codex", 0), ("claude", 1)], str([(c["cli"], c["attempt"]) for c in im]))
            p2 = im[1]["prompt"] if len(im) == 2 else ""
            check("換手 prompt：【換手說明】自成一段，沒有被套上「【上一輪審查／驗證的發現…】」的空帽子",
                  "【換手說明】" in p2 and "【上一輪審查" not in p2, p2[-300:])
            check("[JUDGE] 印出撞牆（含原始輸出與「收進 fixtures」提示）與換手（只限本棒、不下架任何 Agent）",
                  "[JUDGE] 🔴 codex 回 rate_limit" in out and "fixtures/codex_rate_limit" in out
                  and "換手：codex → claude" in out and "不下架任何 Agent" in out, out[-600:])
            body = g(wt, "log", "-1", "--format=%B").stdout
            handoff_md = (rd / "HANDOFF.md").read_text(encoding="utf-8")
            check("收斂後 commit 訊息與 STATE 記實際的最後實作者（Claude Code、途中 codex→claude 換手）",
                  st.get("implementer") == "claude" and "Claude Code（途中 codex→claude 換手） 實作" in body, body)
            ready = [x for x in sent if x[1] == tid]
            check("推播 ready_to_merge 帶 handoff=codex→claude；HANDOFF.md「用量」節前有「換手紀錄：round 1 codex→claude」",
                  len(ready) == 1 and ready[0][0] == "ready_to_merge" and ready[0][2].get("handoff") == "codex→claude"
                  and "換手紀錄：round 1 codex→claude" in handoff_md
                  and handoff_md.index("換手紀錄：") < handoff_md.index("## 用量"), str(ready))

            # ---- 4. 兩支都撞牆 → verdict rate_limit、run() 回 2、沒有例外外拋（3 項＋2 項）--------------------------
            tid = "_test_handoff_b"
            wt, tf = mk_repo(d / "B", tid)
            task = relay.load_task(tf)
            s = {"impl": [{"ok": False, "failure_class": "rate_limit"}, {"ok": False, "failure_class": "rate_limit"}],
                 "verify": [], "review": []}
            exc = None
            r = ScriptedRun(task, s)
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = r.run()
            except Exception as e:  # noqa: BLE001  這條測試要證明「沒有任何例外外拋」
                exc, rc = e, None
            st = state_of(tid)
            check("兩支都撞牆：run() 回 2、沒有任何例外外拋", exc is None and rc == 2, repr(exc))
            check("兩支都撞牆：verdict rate_limit、phase escalate、verify 沒跑、沒開第 2 輪",
                  st["verdict"] == "rate_limit" and st["phase"] == "escalate" and st["verify"] == [] and st["round"] == 1
                  and [c["cli"] for c in impls(r)] == ["codex", "claude"], str({k: st.get(k) for k in ("verdict", "phase", "round")}))
            wall = [x for x in sent if x[1] == tid]
            check("兩支都撞牆：推播 kind rate_limit、cli＝codex、claude（一則）",
                  len(wall) == 1 and wall[0][0] == "rate_limit" and wall[0][2].get("cli") == "codex、claude"
                  and wall[0][2].get("role") == "implementer", str(wall))
            rd = relay.HERE / "runs" / tid
            handoff_md = (rd / "HANDOFF.md").read_text(encoding="utf-8")
            check("兩支都撞牆：HANDOFF 卡點寫「可用的實作者都撞牆」與 --resume、CURRENT 寫「撞牆停下」",
                  "可用的實作者都撞牆" in handoff_md and f"relay.py --resume {tid}" in handoff_md
                  and "撞牆停下" in (rd / "CURRENT.md").read_text(encoding="utf-8"), handoff_md[:400])
            row = relay.status_rows([st], {}, datetime.now(tz8))[0]
            check("--status：verdict rate_limit → 要人看（撞牆）", row["waiting"] == "要人看（撞牆）", row["waiting"])

            # ---- 5. 審查者撞牆 → reviewer_rate_limit、不開下一輪、不換別家審（2 項＋2 項）-------------------------------
            tid = "_test_handoff_c"
            wt, tf = mk_repo(d / "C", tid)
            made = []
            s = {"impl": [{"write": {"a.py": "x = 1\n"}}], "verify": [True], "review": [{"tool_ok": False, "failure_class": "rate_limit"}]}
            with scripted_main(s, made):
                rc, out, err = call([str(tf)])
            st = state_of(tid)
            run = made[0] if made else None
            check("審查者撞牆：verdict reviewer_rate_limit、exit 2", rc == 2 and st["verdict"] == "reviewer_rate_limit"
                  and st["phase"] == "escalate", f"rc={rc} verdict={st.get('verdict')} {err[-300:]}")
            check("審查者撞牆：不開下一輪（max_rounds=2 只跑 1 次實作、round 仍 1）、實作者不換手",
                  run is not None and len(impls(run)) == 1 and st["round"] == 1 and st.get("handoffs") == [], str(st.get("handoffs")))
            rv = [x for x in sent if x[1] == tid]
            check("審查者撞牆：推播 kind rate_limit、role=reviewer、cli=agy；[JUDGE] 印撞牆與「不換別家審」",
                  len(rv) == 1 and rv[0][0] == "rate_limit" and rv[0][2].get("role") == "reviewer" and rv[0][2].get("cli") == "agy"
                  and "[JUDGE] 🔴 agy 回 rate_limit" in out and "不換別家審" in out, str(rv))
            row = relay.status_rows([st], {}, datetime.now(tz8))[0]
            check("--status：verdict reviewer_rate_limit → 要人看（撞牆）", row["waiting"] == "要人看（撞牆）", row["waiting"])

            # ---- 6. 紅線：A 棒換手後，新建 B 棒仍從第一順位開始；task dict／task 檔都沒被改（2 項＋1 項）--------------------
            tid = "_test_handoff_d1"
            wt, tf_a = mk_repo(d / "D1", tid)
            tf_a_bytes = tf_a.read_bytes()
            task_a = relay.load_task(tf_a)
            snap_a = copy.deepcopy(task_a)
            r = ScriptedRun(task_a, {"impl": [{"ok": False, "failure_class": "rate_limit"}, {"write": {"a.py": "x = 1\n"}}],
                                     "verify": [True], "review": [(True, "總判定：可合併")]})
            with contextlib.redirect_stdout(io.StringIO()):
                rc_a = r.run()
            tid = "_test_handoff_d2"
            wt, tf_b = mk_repo(d / "D2", tid, handoff_cooldown_minutes=0)
            tf_b_bytes = tf_b.read_bytes()
            made = []
            with scripted_main({"impl": [{"write": {"a.py": "x = 1\n"}}], "verify": [True], "review": [(True, "總判定：可合併")]}, made):
                rc_b, out_b, err_b = call([str(tf_b)])
            first_b = impls(made[0])[0]["cli"] if made and impls(made[0]) else None
            check("紅線：A 棒 codex 撞牆換 claude 後（帳本已有 codex 的 rate_limit），新建 B 棒（cooldown=0）第一次實作仍用 codex",
                  rc_a == 0 and r.state.handoffs and rc_b == 0 and first_b == "codex" and state_of(tid).get("handoffs") == [],
                  f"rc_a={rc_a} rc_b={rc_b} first_b={first_b} {err_b[-200:]}")
            check("紅線：過程中 task dict（Run.t 與原物件）與兩份 task 檔的內容都沒被修改",
                  task_a == snap_a and r.t == snap_a and tf_a.read_bytes() == tf_a_bytes and tf_b.read_bytes() == tf_b_bytes,
                  f"{task_a == snap_a} {r.t == snap_a} {tf_a.read_bytes() == tf_a_bytes} {tf_b.read_bytes() == tf_b_bytes}")
            tid = "_test_handoff_d3"
            wt, tf_c = mk_repo(d / "D3", tid, handoff_cooldown_minutes=60)
            # 另起一份帳本：上面第 4 條也記了 claude 的 rate_limit，兩支都在窗內會（正確地）退回 codex，量不到這條邊界
            relay.LEDGER = d / "ledger_d3.jsonl"
            relay.LEDGER.write_text(json.dumps({"ts": relay.now(), "cli": "codex", "role": "implementer",
                                                "failure_class": "rate_limit"}) + "\n", encoding="utf-8")
            made = []
            with scripted_main({"impl": [{"write": {"a.py": "x = 1\n"}}], "verify": [True], "review": [(True, "總判定：可合併")]}, made):
                rc_c, out_c, err_c = call([str(tf_c)])
            hs = state_of(tid).get("handoffs", [])
            first_c = impls(made[0])[0]["cli"] if made and impls(made[0]) else None
            check("邊界：cooldown=60 且帳本裡 codex 在窗內撞過 → 本棒從 claude 開始、[JUDGE] 說明、handoffs 記 cooldown（仍收斂）",
                  rc_c == 0 and first_c == "claude" and len(hs) == 1 and hs[0]["reason"] == "cooldown" and hs[0]["from"] == "codex"
                  and "[JUDGE] 略過 codex" in out_c and "不下架任何 Agent" in out_c, f"rc={rc_c} first={first_c} {hs} {err_c[-200:]}")

            # ---- 7. record() 遇到 rate_limit 不 raise（1 項）------------------------------------------------------
            r = relay.Run({"id": "_test_handoff_rec", "worktree": str(d), "repo": str(d)}, dry=False)
            n0 = len(relay.LEDGER.read_text(encoding="utf-8").splitlines())
            try:
                r.record(relay.CallRecord("implementer", 1, False, "429", 1, 0.0, None, "", cli="codex", failure_class="rate_limit"))
                raised = None
            except Exception as e:  # noqa: BLE001
                raised = e
            last = json.loads(relay.LEDGER.read_text(encoding="utf-8").splitlines()[-1])
            check("record()：rate_limit 不 raise，照常記進 STATE 與帳本", raised is None and r.state.calls[-1]["failure_class"] == "rate_limit"
                  and last["failure_class"] == "rate_limit" and len(relay.LEDGER.read_text(encoding="utf-8").splitlines()) == n0 + 1, repr(raised))

            # ---- 真的 implement()：attempt=1 的紀錄檔名（1 項；假 claude 吐真樣本，不呼叫 AI CLI）--------------------------
            wt2 = d / "wt_real"
            wt2.mkdir()
            (d / "fake_claude.py").write_text(FAKE_CLAUDE, encoding="utf-8")
            fx = relay.HERE / "fixtures" / "claude_stream_ok.stdout.txt"

            def fake_build(cli, wt_, out_last, model=None, effort=None):
                real = orig_build(cli, wt_, out_last, model=model, effort=effort)
                return relay.ImplCmd([sys.executable, str(d / "fake_claude.py"), str(fx), str(d)], real.prefix, real.line_fn, real.drop_env)

            relay.build_impl_command = fake_build
            r = relay.Run({"id": "_test_handoff_real", "worktree": str(wt2), "repo": str(wt2), "spec_file": str(d / "A" / "spec.md"),
                           "implementer": ["codex", "claude"]}, dry=False)
            with contextlib.redirect_stdout(io.StringIO()):
                ok, _, _ = r.implement(1, relay.HANDOFF_NOTE.format(prev="Codex"), cli="claude", attempt=1)
            names = {p.name for p in r.dir.iterdir()}
            want = {f"impl_r1_h1_claude{x}" for x in (".stdout.txt", ".stderr.txt", ".exit.txt", "_prompt.md")}
            check("真 implement(cli=claude, attempt=1)：紀錄檔 impl_r1_h1_claude.*（stdout／stderr／exit／prompt），不寫 impl_r1.*",
                  ok and want <= names and not any(n.startswith(("impl_r1.", "impl_r1_prompt")) for n in names), str(sorted(names)))
        finally:
            notify.notify, relay.build_impl_command = orig_notify, orig_build
            relay.LEDGER = orig_ledger
            for t in tids:
                rm_runs(t)


def test_candidates() -> None:
    """C4（2026-10-05）：best-of-N 本體（規格 §7b 測試案例 1–5，另加邊界）。全程 ScriptedRun、不呼叫任何 AI CLI；
    帳本與鎖指暫存、推播設定指到不存在的路徑（只有驗「推播只發 1 次」那段用假指令＋暫存設定）；臨時 repo 的 autocrlf 在 local 明設。"""
    import copy
    import re
    import shutil
    import subprocess

    def g(wt, *a: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True, encoding="utf-8")

    def call(argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = relay.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def mk_repo(d: Path, gid: str, **extra) -> tuple[Path, Path, dict]:
        """臨時 repo（main 一顆 commit），群組 worktree 在 d/wt（候選為 d/wt-c1…）；任務檔放 repo 外。"""
        repo = d / "repo"
        repo.mkdir(parents=True)
        g(repo, "init", "-q", "-b", "main"); g(repo, "config", "user.email", "t@t"); g(repo, "config", "user.name", "t")
        g(repo, "config", "core.autocrlf", "false")
        (repo / "a.py").write_bytes(b"x = 0\n")
        g(repo, "add", "-A"); g(repo, "commit", "-qm", "base")
        (d / "spec.md").write_text("把 x 改成 1", encoding="utf-8")
        (d / "review.md").write_text("1. x 有改", encoding="utf-8")
        task = {"id": gid, "title": "c4 測試", "repo": str(repo), "base_branch": "main", "branch": "feat/c4",
                "worktree": str(d / "wt"), "spec_file": str(d / "spec.md"), "verify": [{"name": "v", "cmd": "echo ok"}],
                "review": {"policy": "always", "instructions_file": str(d / "review.md")},
                "allowed_paths": ["a.py"], "max_rounds": 2,
                "candidates": [{"implementer": "codex"}, {"implementer": "claude", "model": "sonnet"}], **extra}
        tf = d / "task.json"
        tf.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        return repo, tf, task

    def rm_runs(gid: str) -> None:
        for p in (relay.HERE / "runs").glob(gid + "*"):
            shutil.rmtree(p, ignore_errors=True)

    def st(cand: int, **kw) -> dict:
        """合成 STATE：預設＝收斂、1 輪、verify 1/1、審查 approve、diff 10 行。"""
        s = {"task_id": f"g.c{cand}", "phase": "done", "round": 1, "verdict": "converged", "implementer": "codex",
             "branch": f"b-c{cand}", "commit": f"abc{cand}", "verify": [{"round": 1, "exit": 0}], "negctl": [{"round": 1, "ok": True}],
             "review_decisions": [{"round": 1, "need_review": True}, {"round": 1, "approved": True, "unreported": []}],
             "calls": [{"role": "reviewer", "round": 1, "ok": True, "seconds": 5.0}], "iface_gate": {},
             "diff_stats": {"files": 1, "lines": 10}}
        s.update(kw)
        return s

    order = lambda ss: [r["cand"] for r in relay.rank_candidates(ss)]  # noqa: E731

    # ---- 1. derive_candidate_tasks（4 項＋沿用設定 1 項）-----------------------------------------------
    base = {"id": "g", "branch": "feat/x", "worktree": "C:/code/wt", "implementer": ["codex", "claude"],
            "implementer_models": {"codex": "m0"}, "verify": [], "candidates": [
                {"implementer": "codex"}, {"implementer": "claude", "model": "sonnet", "effort": "high"}]}
    snap = copy.deepcopy(base)
    d1, d2 = relay.derive_candidate_tasks(base)
    check("derive：id／branch／worktree 後綴正確（.c{k}／-c{k}）",
          (d1["id"], d1["branch"], d1["worktree"]) == ("g.c1", "feat/x-c1", "C:/code/wt-c1")
          and (d2["id"], d2["branch"], d2["worktree"]) == ("g.c2", "feat/x-c2", "C:/code/wt-c2"), str((d1["id"], d2["branch"])))
    check("derive：candidates 被刪、輸入的 task 沒被改", "candidates" not in d1 and "candidates" not in d2 and base == snap)
    check("derive：implementer 是單一字串（候選模式強制關閉換手）", d1["implementer"] == "codex" and d2["implementer"] == "claude")
    check("derive：_group 是群組 id", d1["_group"] == "g" and d2["_group"] == "g")
    check("derive：model／effort 有給才覆蓋，沒給沿用 task 的 implementer_models",
          d1["implementer_models"] == {"codex": "m0"} and d2["implementer_models"] == {"codex": "m0", "claude": "sonnet"}
          and d2["implementer_effort"] == {"claude": "high"} and "implementer_effort" not in d1, str((d1.get("implementer_models"), d2.get("implementer_models"))))

    # ---- 4. review 欄推導（4 項＋changes 1 項）---------------------------------------------------------
    R = relay.candidate_review
    check("review：need_review=False → skipped", R(st(1, review_decisions=[{"round": 1, "need_review": False}], calls=[])) == "skipped")
    check("review：最後一個 reviewer call ok=False → tool_failure",
          R(st(1, review_decisions=[{"round": 1, "need_review": True}, {"round": 1, "approved": False}],
               calls=[{"role": "reviewer", "round": 1, "ok": False}])) == "tool_failure")
    check("review：approved=True → approve", R(st(1)) == "approve")
    check("review：沒有任何審查條目（實作者失敗、沒走到審查）→ none", R(st(1, review_decisions=[], calls=[])) == "none")
    check("review：approved=False → changes",
          R(st(1, review_decisions=[{"round": 1, "need_review": True}, {"round": 1, "approved": False}])) == "changes")

    # ---- 3. rank_candidates（6 項＋邊界 3 項）------------------------------------------------------------
    check("rank：converged 勝過 verify 全過但未收斂者",
          order([st(1, verdict="escalate", verify=[{"round": 1, "exit": 0}] * 3), st(2, verify=[{"round": 1, "exit": 0}, {"round": 1, "exit": 1}])]) == [2, 1])
    check("rank：同為 converged，verify 過的多者勝",
          order([st(1, verify=[{"round": 1, "exit": 0}, {"round": 1, "exit": 1}]), st(2, verify=[{"round": 1, "exit": 0}] * 2)]) == [2, 1])
    chg = [{"round": 1, "need_review": True}, {"round": 1, "approved": False}]
    check("rank：再相同時 approve 勝 changes", order([st(1, review_decisions=chg), st(2)]) == [2, 1])
    check("rank：再相同時 breaking 少者勝",
          order([st(1, iface_gate={"f.py": {"breaking": ["a", "b"]}}), st(2, iface_gate={"f.py": {"breaking": ["a"]}})]) == [2, 1])
    check("rank：再相同時 diff 行數少者勝",
          order([st(1, diff_stats={"files": 1, "lines": 30}), st(2, diff_stats={"files": 1, "lines": 5})]) == [2, 1])
    check("rank：全部相同依 cand 順序（輸入亂序）", order([st(3), st(1), st(2)]) == [1, 2, 3])
    rows = relay.rank_candidates([st(1, phase="aborted", verdict="converged"), st(2, verdict="escalate")])  # 1 的 verify 比較好，仍排後面
    check("rank：phase=aborted → verdict 顯示 aborted 且排在後面（即使 verdict 欄寫過 converged）",
          [r["cand"] for r in rows] == [2, 1] and rows[1]["verdict"] == "aborted", str([(r["cand"], r["verdict"]) for r in rows]))
    r0 = relay.rank_candidates([st(1, verify=[], round=1, _verify_total=2, verdict="escalate")])[0]
    check("rank：該輪沒跑 verify（實作者失敗）→ 0/任務 verify 數", (r0["verify_pass"], r0["verify_total"]) == (0, 2), str(r0))
    r1 = relay.rank_candidates([st(1)])[0]
    check("rank：列欄位齊全（spec 14 欄）",
          set(r1) == {"cand", "implementer", "verdict", "verify_pass", "verify_total", "negctl_ok", "review", "breaking",
                      "unreported", "diff_lines", "rounds", "seconds", "branch", "commit"} and r1["seconds"] == 5.0, str(sorted(r1)))

    # ---- required_clis（2 項）------------------------------------------------------------------------
    rq = relay.required_clis({"review": {"policy": "never"}, "implementer": "codex",
                              "candidates": [{"implementer": "codex"}, {"implementer": "claude"}]})
    rq2 = relay.required_clis({"review": {"policy": "never"}, "implementer": "claude",
                               "candidates": [{"implementer": "codex"}, {"implementer": "codex"}]})
    check("required_clis：candidates 含 claude → 要求 claude（也要 codex）", rq["need_claude"] and rq["need_codex"], str(rq))
    check("required_clis：有 candidates 時 task 的 implementer 被忽略（只算候選）", rq2["need_codex"] and not rq2["need_claude"], str(rq2))

    gid = "ztest-c4-grp"  # 不用 "_" 開頭：--status 會略過那種目錄，而這裡要驗 --status 看得到候選
    orig_ledger, orig_nl = relay.LEDGER, relay.NOTIFY_LEDGER
    orig_run, orig_check, orig_env = relay.Run, relay.paths.check_all, os.environ.get("RELAY_NOTIFY_CONFIG")
    scripts: dict[str, dict] = {}
    made: list = []

    def factory(task, dry, **kw):
        assert not dry, "ScriptedRun 不跑 dry-run"
        r = ScriptedRun(task, scripts[task["id"]], **kw)
        made.append(r)
        return r

    ok_script = lambda: {"impl": [{"write": {"a.py": "x = 1\n"}}], "verify": [True], "review": [(True, "總判定：可合併")]}  # noqa: E731
    bad_script = lambda: {"impl": [{"write": {"a.py": "x = 2\n"}}] * 2, "verify": [False, False],  # noqa: E731
                          "review": [(False, "總判定：需修改")] * 2}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as dd:
        d = Path(dd)
        relay.LEDGER = d / "ledger.jsonl"
        relay.NOTIFY_LEDGER = d / "notify_ledger.jsonl"
        relay.paths.check_all = lambda **kw: []
        try:
            # ---- 2. 驗證（3 項＋邊界 3 項）：都在跑任何東西之前回 3 --------------------------------------
            repo, tf, task = mk_repo(d / "V", gid)
            for label, over in (("candidates 長度 1", {"candidates": [{"implementer": "codex"}]}),
                                ("candidates 長度 4", {"candidates": [{"implementer": "codex"}] * 4}),
                                ("候選 implementer 非法", {"candidates": [{"implementer": "codex"}, {"implementer": "gemini"}]}),
                                ("候選 implementer 給清單（候選不換手）", {"candidates": [{"implementer": ["codex", "claude"]}, {"implementer": "codex"}]}),
                                ("候選 effort 配 codex", {"candidates": [{"implementer": "codex", "effort": "high"}, {"implementer": "claude"}]}),
                                ("候選 worktree 等於 production_dir", {"production_dir": str(d / "V" / "wt-c2")})):
                tf2 = d / "V" / "t2.json"
                tf2.write_text(json.dumps({**task, **over}, ensure_ascii=False), encoding="utf-8")
                rc, _, err = call([str(tf2)])
                check(f"驗證：{label} → main 回 3、沒建任何 runs", rc == 3 and not list((relay.HERE / "runs").glob(gid + "*")), f"rc={rc} {err.strip()[-120:]}")

            # ---- 5. run_group：候選 1 收斂、候選 2 escalate（經 main 進入）---------------------------------
            out_f = d / "pushed.txt"
            os.environ["RELAY_NOTIFY_CONFIG"] = str(_write_cfg(d, out_f))
            d5 = d / "G"
            repo, tf, task = mk_repo(d5, gid)
            scripts.update({gid + ".c1": ok_script(), gid + ".c2": bad_script()})
            relay.Run = factory
            made.clear()
            try:
                rc, out, err = call([str(tf)])
            finally:
                relay.Run = orig_run
            rk = (relay.HERE / "runs" / gid / "RANKING.md")
            rtxt = rk.read_text(encoding="utf-8") if rk.is_file() else ""
            check("run_group：候選 1 收斂、候選 2 escalate → exit 0", rc == 0, f"rc={rc} {err.strip()[-200:]}")
            check("run_group：RANKING.md 第一名是 c1，指到 c1 的分支與 HANDOFF",
                  "第一名：c1 →" in rtxt and "`feat/c4-c1`" in rtxt and f"runs/{gid}.c1/HANDOFF.md" in rtxt
                  and rtxt.index("| 1 | c1 |") < rtxt.index("| 2 | c2 |"), rtxt[:600])
            br = g(repo, "branch", "--list", "feat/c4-c1", "feat/c4-c2").stdout
            wts = (d5 / "wt-c1").is_dir() and (d5 / "wt-c2").is_dir()
            check("run_group：RANKING.md 內含清理指令字樣，但沒有被執行（c2 的 branch 與 worktree 仍在）",
                  "worktree remove" in rtxt and "branch -D" in rtxt and "feat/c4-c2" in rtxt.split("```")[1]
                  and "feat/c4-c2" in br and wts, f"{br!r} wts={wts}")
            sent = out_f.read_text(encoding="utf-8") if out_f.is_file() else ""
            check("run_group：推播假指令只被呼叫 1 次，第 1 行是群組文案", _n_sent(out_f) == 1
                  and "best-of-2 完成：第一名 c1（codex，converged）" in sent and f"runs/{gid}/RANKING.md" in sent, repr(sent[:300]))
            s1 = json.loads((relay.HERE / "runs" / (gid + ".c1") / "STATE.json").read_text(encoding="utf-8"))
            s2 = json.loads((relay.HERE / "runs" / (gid + ".c2") / "STATE.json").read_text(encoding="utf-8"))
            check("run_group：兩份候選各有自己的 STATE／branch／worktree，c2 未收斂",
                  (s1["verdict"], s2["verdict"]) == ("converged", "escalate") and s1["branch"] == "feat/c4-c1"
                  and s2["worktree"] == str(d5 / "wt-c2") and s1["commit"] and not s2["commit"], str((s1["branch"], s2["worktree"])))
            check("State：negctl 每輪一筆、diff_stats 有 files／lines（單棒也寫）",
                  s1["negctl"] == [{"round": 1, "ok": True}] and s1["diff_stats"]["files"] == 1 and s1["diff_stats"]["lines"] == 2
                  and len(s2["negctl"]) == 2, str((s1["negctl"], s1["diff_stats"])))
            ntf = [json.loads(ln) for ln in relay.NOTIFY_LEDGER.read_text(encoding="utf-8").splitlines()]
            check("run_group：推播帳本只有群組 id 一筆 ready_to_merge（候選自己都沒推）",
                  [(e["task"], e["kind"]) for e in ntf] == [(gid, "ready_to_merge")], str(ntf))
            gj = json.loads((relay.HERE / "runs" / gid / "GROUP.json").read_text(encoding="utf-8"))
            check("run_group：GROUP.json 記排名、第一名與候選 id；候選任務檔落在群組目錄且不含 candidates",
                  gj["state"] == "done" and gj["first"] == 1 and gj["candidates"] == [gid + ".c1", gid + ".c2"]
                  and "candidates" not in json.loads((relay.HERE / "runs" / gid / "cand_2.task.json").read_text(encoding="utf-8")))
            rows_status = relay.status_report(None)
            check("--status：列出 <id>.c1／<id>.c2（群組目錄本身不列）",
                  f"{gid}.c1" in rows_status and f"{gid}.c2" in rows_status and not re.search(rf"^{gid}\s", rows_status, re.M), rows_status)
            rc, _, err = call(["--resume", gid])
            check("--resume <群組 id> → exit 3，提示指定候選 id", rc == 3 and "請指定候選 id" in err and f"{gid}.c1" in err, err.strip()[-150:])
            tfc = relay.HERE / "runs" / gid / "cand_2.task.json"
            rc2, _, err2 = call(["--resume", gid + ".c2", str(tfc)])
            check("--resume <候選 id>：不被當群組拒絕（錯在缺 human_notes，不是群組）", rc2 == 3 and "human_notes" in err2 and "群組" not in err2, err2.strip()[-200:])
            rm_runs(gid)

            # ---- 邊界：全不收斂 → exit 2、推播 escalate；一份中止不影響另一份 ---------------------------------
            repo, tf, task = mk_repo(d / "H", gid)
            scripts.update({gid + ".c1": bad_script(), gid + ".c2": bad_script()})
            out_h = d / "pushed_h.txt"
            os.environ["RELAY_NOTIFY_CONFIG"] = str(_write_cfg(d, out_h))
            relay.NOTIFY_LEDGER = d / "notify_ledger_h.jsonl"
            relay.Run = factory
            try:
                rc, _, _ = call([str(tf)])
            finally:
                relay.Run = orig_run
            ntf = [json.loads(ln) for ln in relay.NOTIFY_LEDGER.read_text(encoding="utf-8").splitlines()]
            check("邊界：所有候選都沒收斂 → 仍排名、exit 2、推播 kind=escalate（1 則）",
                  rc == 2 and (relay.HERE / "runs" / gid / "RANKING.md").is_file() and [e["kind"] for e in ntf] == ["escalate"]
                  and _n_sent(out_h) == 1, f"rc={rc} {ntf}")
            rm_runs(gid)

            repo, tf, task = mk_repo(d / "A", gid)
            relay.NOTIFY_LEDGER = d / "notify_ledger_a.jsonl"
            # c1 寫了規格外的檔 → 收斂時 changed_paths 丟 RuntimeError → 只中止 c1；c2 照跑
            scripts.update({gid + ".c1": {"impl": [{"write": {"a.py": "x = 1\n", "other.py": "y = 1\n"}}], "verify": [True],
                                          "review": [(True, "總判定：可合併")]}, gid + ".c2": ok_script()})
            relay.Run = factory
            try:
                rc, _, _ = call([str(tf), "--no-notify"])
            finally:
                relay.Run = orig_run
            rtxt = (relay.HERE / "runs" / gid / "RANKING.md").read_text(encoding="utf-8")
            sa = json.loads((relay.HERE / "runs" / (gid + ".c1") / "STATE.json").read_text(encoding="utf-8"))
            check("邊界：c1 中止（規格外改動）→ c1 顯示 aborted 排後面、c2 第一名、exit 0，--no-notify 不推播",
                  rc == 0 and sa["phase"] == "aborted" and "| 1 | c2 |" in rtxt and "| 2 | c1 | codex | aborted |" in rtxt
                  and not relay.NOTIFY_LEDGER.exists(), rtxt[:500])
            rm_runs(gid)

            # ---- dry-run：只印計畫、不寫 RANKING／GROUP、不建 runs/<id>（4 項）-------------------------------
            repo, tf, task = mk_repo(d / "D", gid)
            made.clear()
            rc, out, err = call([str(tf), "--dry-run"])
            plan = re.sub(r"\x1b\[[0-9;]*m", "", out)
            runs = relay.HERE / "runs"
            check("dry-run：群組 exit 0、印出每份候選的計畫（id／implementer／model）",
                  rc == 0 and f"{gid}.c1" in plan and f"{gid}.c2" in plan and "claude／sonnet" in plan, plan[:500] + err[-200:])
            check("dry-run：不寫 RANKING.md／GROUP.json，也不建 runs/<群組id>", not (runs / gid).exists())
            check("dry-run：每份候選走自己的 dry 目錄（runs/<id>.c1.dry），沒有真實 STATE", (runs / (gid + ".c1.dry")).is_dir()
                  and not (runs / (gid + ".c1")).exists())
            check("dry-run：沒建任何 worktree／branch", not (d / "D" / "wt-c1").exists() and not g(repo, "branch", "--list", "feat/c4-c1").stdout.strip())
        finally:
            relay.Run, relay.paths.check_all = orig_run, orig_check
            relay.LEDGER, relay.NOTIFY_LEDGER = orig_ledger, orig_nl
            if orig_env is None:
                os.environ["RELAY_NOTIFY_CONFIG"] = str(Path(tempfile.gettempdir()) / "relay_no_such_notify_config.json")
            else:
                os.environ["RELAY_NOTIFY_CONFIG"] = orig_env
            rm_runs(gid)


def test_redlines() -> None:
    """2026-10-05 紅線補強：(a) best-of-N 任一候選 ProductionTouched → 整組停；(b) 沿用既有 worktree 前先驗分支與根目錄。
    全程 ScriptedRun、不呼叫 AI CLI；帳本與鎖指暫存、推播設定指到不存在路徑（只有驗「只推一則」用假指令＋暫存設定）；
    臨時 repo 的 autocrlf 在 local 明設。測試自己造 worktree 用 `worktree add -b <別的分支>`，不靠 checkout。"""
    import shutil
    import subprocess

    def g(wt, *a: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True, encoding="utf-8")

    def call(argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = relay.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def init_repo(p: Path) -> None:
        p.mkdir(parents=True)
        g(p, "init", "-q", "-b", "main"); g(p, "config", "user.email", "t@t"); g(p, "config", "user.name", "t")
        g(p, "config", "core.autocrlf", "false")
        (p / "a.py").write_bytes(b"x = 0\n")
        g(p, "add", "-A"); g(p, "commit", "-qm", "base")

    def mk(d: Path, gid: str, group: bool, **extra) -> tuple[Path, Path, dict]:
        repo = d / "repo"
        init_repo(repo)
        (d / "spec.md").write_text("把 x 改成 1", encoding="utf-8")
        (d / "review.md").write_text("1. x 有改", encoding="utf-8")
        task = {"id": gid, "title": "紅線補強", "repo": str(repo), "base_branch": "main", "branch": "feat/rl",
                "worktree": str(d / "wt"), "spec_file": str(d / "spec.md"), "verify": [{"name": "v", "cmd": "echo ok"}],
                "review": {"policy": "always", "instructions_file": str(d / "review.md")},
                "allowed_paths": ["a.py"], "max_rounds": 2, **extra}
        if group:
            task["candidates"] = [{"implementer": "codex"}, {"implementer": "claude", "model": "sonnet"}]
        tf = d / "task.json"
        tf.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        return repo, tf, task

    def rm_runs(gid: str) -> None:
        for p in (relay.HERE / "runs").glob(gid + "*"):
            shutil.rmtree(p, ignore_errors=True)

    gid = "ztest-rl-grp"
    sid = "ztest-rl-one"
    orig_ledger, orig_nl = relay.LEDGER, relay.NOTIFY_LEDGER
    orig_run, orig_check, orig_env = relay.Run, relay.paths.check_all, os.environ.get("RELAY_NOTIFY_CONFIG")
    scripts: dict[str, dict] = {}

    def factory(task, dry, **kw):
        if dry:
            return orig_run(task, dry, **kw)
        return ScriptedRun(task, scripts[task["id"]], **kw)

    ok_script = lambda: {"impl": [{"write": {"a.py": "x = 1\n"}}], "verify": [True], "review": [(True, "總判定：可合併")]}  # noqa: E731
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as dd:
        d = Path(dd)
        relay.LEDGER = d / "ledger.jsonl"
        relay.paths.check_all = lambda **kw: []
        try:
            # ---- (a1) c1 動了生產目錄 → c2 不啟動、RANKING 有 not_run、exit 3、只推一則 -------------------------
            out_f = d / "pushed.txt"
            os.environ["RELAY_NOTIFY_CONFIG"] = str(_write_cfg(d, out_f))
            relay.NOTIFY_LEDGER = d / "notify_ledger_a1.jsonl"
            prod = d / "A1" / "prod"
            init_repo(prod)
            repo, tf, task = mk(d / "A1", gid, True, production_dir=str(prod))
            c2 = ok_script()
            scripts.update({gid + ".c1": {"impl": [{"write": {"a.py": "x = 1\n"}, "write_abs": {str(prod / "evil.txt"): "x"}}],
                                          "verify": [True], "review": [(True, "總判定：可合併")]}, gid + ".c2": c2})
            relay.Run = factory
            try:
                rc, out, err = call([str(tf)])
            finally:
                relay.Run = orig_run
            rk = relay.HERE / "runs" / gid / "RANKING.md"
            rtxt = rk.read_text(encoding="utf-8") if rk.is_file() else ""
            sent = out_f.read_text(encoding="utf-8") if out_f.is_file() else ""
            gj = relay.HERE / "runs" / gid / "GROUP.json"
            gtxt = gj.read_text(encoding="utf-8") if gj.is_file() else ""
            ntf = [json.loads(ln) for ln in relay.NOTIFY_LEDGER.read_text(encoding="utf-8").splitlines()] if relay.NOTIFY_LEDGER.is_file() else []
            check("(a1) 群組 c1 丟 ProductionTouched → exit 3", rc == 3, f"rc={rc} {err.strip()[-200:]}")
            check("(a1) c2 的實作沒有被呼叫（腳本沒被消耗）、沒有 c2 的 STATE／worktree",
                  len(c2["impl"]) == 1 and not (relay.HERE / "runs" / (gid + ".c2") / "STATE.json").exists()
                  and not (d / "A1" / "wt-c2").exists(), str(c2))
            check("(a1) RANKING.md：c2 為 not_run、寫明「生產目錄變動，整組停止」、c1 為 aborted",
                  "| 2 | c2 | claude | not_run |" in rtxt and "生產目錄變動，整組停止" in rtxt and "| 1 | c1 | codex | aborted |" in rtxt, rtxt[:700])
            check("(a1) GROUP.json 記 stopped 原因與 not_run 的候選", "生產目錄變動，整組停止" in gtxt and gid + ".c2" in gtxt.split("not_run")[-1], gtxt[:300])
            check("(a1) 推播假指令只呼叫 1 次、kind=escalate、第 1 行講生產目錄被改動、整組停止",
                  _n_sent(out_f) == 1 and "生產目錄" in sent.splitlines()[0] and "整組停止" in sent.splitlines()[0]
                  and [(e["task"], e["kind"]) for e in ntf] == [(gid, "escalate")], repr(sent[:300]) + str(ntf))
            rm_runs(gid)

            # ---- (a2) 對照：c1 規格外改動中止（生產目錄沒動）→ c2 照跑，既有行為不變 -----------------------------
            prod2 = d / "A2" / "prod"
            init_repo(prod2)
            repo, tf, task = mk(d / "A2", gid, True, production_dir=str(prod2))
            c2 = ok_script()
            scripts.update({gid + ".c1": {"impl": [{"write": {"a.py": "x = 1\n", "other.py": "y = 1\n"}}], "verify": [True],
                                          "review": [(True, "總判定：可合併")]}, gid + ".c2": c2})
            relay.Run = factory
            try:
                rc, out, err = call([str(tf), "--no-notify"])
            finally:
                relay.Run = orig_run
            rtxt = (relay.HERE / "runs" / gid / "RANKING.md").read_text(encoding="utf-8")
            check("(a2) 規格外改動中止 c1 → c2 照跑（實作被呼叫）、c2 第一名、exit 0、RANKING 無 not_run",
                  rc == 0 and len(c2["impl"]) == 0 and "| 1 | c2 |" in rtxt and "not_run" not in rtxt, f"rc={rc} {rtxt[:400]}")
            rm_runs(gid)

            # ---- (b) 沿用既有 worktree 前驗分支／根目錄 ------------------------------------------------------
            def one(sub: str) -> tuple[Path, Path, dict, Path]:
                repo, tf, task = mk(d / sub, sid, False)
                return repo, tf, task, Path(task["worktree"])

            def heads(*ps: Path) -> list[str]:
                return [g(p, "rev-parse", "HEAD").stdout.strip() + "|" + g(p, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() for p in ps]

            # b1：既有 worktree 在別的分支
            repo, tf, task, wt = one("B1")
            g(repo, "worktree", "add", "-q", "-b", "feat/other", str(wt), "main")
            before = heads(wt, repo)
            r = ScriptedRun(task, {})
            msg = ""
            try:
                r.prepare()
            except RuntimeError as e:
                msg = str(e)
            check("(b1) 既有 worktree 在別的分支 → prepare() raise RuntimeError，訊息含期望／實際分支與路徑、relay 不代為 checkout",
                  "feat/rl" in msg and "feat/other" in msg and str(wt) in msg and "relay 不代為 checkout" in msg and "請人處理" in msg, msg)
            check("(b1) worktree 與主 repo 的 HEAD、分支前後完全沒被改動", heads(wt, repo) == before and before[0].endswith("|feat/other"), str(before))
            # b3：同一份 --dry-run → exit 3，且一樣沒動
            rc, out, err = call([str(tf), "--dry-run"])
            check("(b3) --dry-run 遇分支不符 → exit 3、訊息含兩個分支名、HEAD 未動",
                  rc == 3 and "feat/other" in err and "feat/rl" in err and heads(wt, repo) == before, f"rc={rc} {err.strip()[-250:]}")
            rm_runs(sid)

            # b2：既有 worktree 在正確分支 → 照常沿用
            repo, tf, task, wt = one("B2")
            g(repo, "worktree", "add", "-q", "-b", "feat/rl", str(wt), "main")
            before = heads(wt, repo)
            r = ScriptedRun(task, {})
            err2 = ""
            try:
                r.prepare()
            except RuntimeError as e:
                err2 = str(e)
            check("(b2) 既有 worktree 在正確分支 → prepare() 照常沿用（不 raise、HEAD 不變、state.worktree 設好）",
                  not err2 and heads(wt, repo) == before and r.state.worktree == str(wt) and r.state.branch == "feat/rl", err2)
            rc, out, err = call([str(tf), "--dry-run"])
            check("(b2) 正確分支的 --dry-run → exit 0", rc == 0, f"rc={rc} {err.strip()[-200:]}")
            rm_runs(sid)

            # b4：路徑存在但不是 git worktree 根（受測 repo 的子目錄）
            repo, tf, task, wt = one("B4")
            sub = repo / "subdir"
            sub.mkdir()
            task["worktree"] = str(sub)
            before = heads(repo)
            r = ScriptedRun(task, {})
            msg = ""
            try:
                r.prepare()
            except RuntimeError as e:
                msg = str(e)
            check("(b4) worktree 路徑是 repo 的子目錄（不是 worktree 根）→ raise，訊息含路徑與「根目錄」，repo 未動",
                  str(sub) in msg and "根目錄" in msg and heads(repo) == before, msg)
            rm_runs(sid)
        finally:
            relay.Run, relay.paths.check_all = orig_run, orig_check
            relay.LEDGER, relay.NOTIFY_LEDGER = orig_ledger, orig_nl
            if orig_env is None:
                os.environ["RELAY_NOTIFY_CONFIG"] = str(Path(tempfile.gettempdir()) / "relay_no_such_notify_config.json")
            else:
                os.environ["RELAY_NOTIFY_CONFIG"] = orig_env
            rm_runs(gid)
            rm_runs(sid)


FAKE_CODEX_FAIL = r'''import sys
sys.stdin.buffer.read()
sys.exit(1)
'''


def test_dryrun_and_stale_report() -> None:
    """2026-10-06：① codex 在寫 -o 前失敗不得讀到上次的 last_message 檔；② --dry-run 不碰既有 worktree
    （不 git reset、不列改動、不跑簽章閘門）。不呼叫任何 AI CLI：① 用假 codex（python 腳本，讀完 stdin 直接 exit 1）。"""
    import re
    import subprocess

    def g(wt, *a: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True, encoding="utf-8")

    def init_repo(p: Path) -> None:
        p.mkdir(parents=True)
        g(p, "init", "-q", "-b", "main"); g(p, "config", "user.email", "t@t"); g(p, "config", "user.name", "t")
        g(p, "config", "core.autocrlf", "false")
        (p / "a.py").write_bytes(b"x = 0\n")
        g(p, "add", "-A"); g(p, "commit", "-qm", "base")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as dd:
        d = Path(dd)
        (d / "spec.md").write_text("規格第一行\n", encoding="utf-8")
        (d / "rev.md").write_text("1. x 有改", encoding="utf-8")

        # ---- ① codex 失敗且沒寫 -o：上次留下的 last_message 檔不得當成本次報告（2 項）----------------
        wt = d / "wt"
        wt.mkdir()
        (d / "fake_codex_fail.py").write_text(FAKE_CODEX_FAIL, encoding="utf-8")
        tid = "_test_stale_last_message"
        orig_build, orig_ledger = relay.build_impl_command, relay.LEDGER
        relay.LEDGER = d / "ledger.jsonl"

        def fake_build(cli, wt_, out_last, model=None, effort=None):
            real = orig_build(cli, wt_, out_last, model=model, effort=effort)
            return relay.ImplCmd([sys.executable, str(d / "fake_codex_fail.py")], real.prefix, real.line_fn, real.drop_env)

        try:
            relay.build_impl_command = fake_build
            r = relay.Run({"id": tid, "worktree": str(wt), "repo": str(wt), "spec_file": str(d / "spec.md"),
                           "implementer": "codex"}, dry=False)
            stale = r.dir / "impl_r1_last_message.md"
            stale.write_text("舊報告：上一次跑留下的", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                ok, report, usage = r.implement(1, "")
            check("implement(codex)：舊的 last_message 檔不得當成本次報告", not ok and report == "",
                  f"ok={ok} report={report!r}")
            exit_f = r.dir / "impl_r1.exit.txt"
            exit_txt = exit_f.read_text(encoding="utf-8") if exit_f.is_file() else ""
            last_call = r.state.calls[-1] if r.state.calls else {}
            check("implement(codex)：呼叫前已刪舊檔、exit 檔記 exit=1、判定器記 failure_class",
                  not stale.exists() and exit_txt == "exit=1" and bool(last_call.get("failure_class")),
                  f"stale={stale.exists()} exit={exit_txt!r} call={last_call}")
        finally:
            relay.build_impl_command, relay.LEDGER = orig_build, orig_ledger
            rm_runs(tid)

        # ---- ② --dry-run 沿用既有 worktree：暫存區與狀態前後一致（2 項）--------------------------------
        repo, wt2 = d / "repo", d / "wt2"
        init_repo(repo)
        g(repo, "worktree", "add", "-q", "-b", "feat/dry", str(wt2), "main")
        (wt2 / "a.py").write_bytes(b"x = 1\n")
        g(wt2, "add", "a.py")                      # 人暫存好的改動
        (wt2 / "new.txt").write_bytes(b"n\n")      # 未追蹤檔
        tid2 = "_test_dry_keeps_index"
        task = {"id": tid2, "title": "dry 不碰 worktree", "repo": str(repo), "base_branch": "main", "branch": "feat/dry",
                "worktree": str(wt2), "spec_file": str(d / "spec.md"), "verify": [{"name": "v", "cmd": "echo ok"}],
                "review": {"policy": "always", "instructions_file": str(d / "rev.md")}, "allowed_paths": ["a.py"], "max_rounds": 1}
        tf = d / "task.json"
        tf.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")

        def snap(p: Path) -> tuple[str, str, str]:
            return (g(p, "rev-parse", "HEAD").stdout, g(p, "status", "--porcelain", "-z").stdout,
                    g(p, "diff", "--cached", "--name-only").stdout)

        before_wt, before_repo = snap(wt2), snap(repo)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = relay.main([str(tf), "--dry-run"])
        text = re.sub(r"\x1b\[[0-9;]*m", "", out.getvalue())
        check("dry-run：既有 worktree 的暫存區不被 reset（a.py 仍在 index）",
              rc == 0 and before_wt[2].strip() == "a.py" and snap(wt2) == before_wt,
              f"rc={rc} cached_after={snap(wt2)[2]!r} err={err.getvalue()[-300:]}")
        check("dry-run：印出「[dry] 不碰 worktree」、主 repo HEAD／status 不變、不建 runs/<id>/",
              "[dry] 不碰 worktree" in text and snap(repo) == before_repo and not (relay.HERE / "runs" / tid2).exists(),
              text[-600:])
        rm_runs(tid2)


def main() -> int:
    # C3（2026-10-05）：測試產生的鎖一律落在暫存目錄，不碰 runs/.locks/
    orig_locks = relay.LOCKS
    # C2：任何測試都不可真的推播——設定檔指到不存在的路徑、推播帳本寫暫存
    os.environ["RELAY_NOTIFY_CONFIG"] = str(Path(tempfile.gettempdir()) / "relay_no_such_notify_config.json")
    orig_nl = relay.NOTIFY_LEDGER
    relay.NOTIFY_LEDGER = Path(tempfile.gettempdir()) / "relay_test_notify_ledger.jsonl"
    locks_tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    relay.LOCKS = Path(locks_tmp.name) / "locks"
    try:
        for fn in (test_decide_review, test_parse_review, test_iface_gate, test_ledger_totals, test_diff_for_review,
                   test_worktree_changes, test_negative_control_timeout, test_status, test_runlock, test_parallel,
                   test_notify, test_notify_telegram, test_resume, test_impl_command, test_handoff, test_candidates, test_redlines, test_dryrun_and_stale_report):
            print(f"--- {fn.__name__} ---")
            try:
                fn()
            except Exception as exc:  # 改前（函式還不存在）要紅得有名字，不要整支崩潰
                FAILED.append(f"{fn.__name__} 例外：{exc!r}")
                print(f"[FAIL] {fn.__name__} 例外：{exc!r}")
    finally:
        relay.LOCKS = orig_locks
        relay.NOTIFY_LEDGER = orig_nl
        locks_tmp.cleanup()
    print(f"\n{PASSED} passed / {len(FAILED)} failed")
    for n in FAILED:
        print("  ✗", n)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
