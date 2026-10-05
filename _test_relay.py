"""_test_relay.py — relay 的純邏輯測試：難易度判準、結構化審查解析、簽章閘門、帳本、改動判定、
陰性對照逾時、--status 總表（C1）、跨行程鎖與並行上限（C3）。不呼叫任何 AI CLI。

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


def main() -> int:
    # C3（2026-10-05）：測試產生的鎖一律落在暫存目錄，不碰 runs/.locks/
    orig_locks = relay.LOCKS
    locks_tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    relay.LOCKS = Path(locks_tmp.name) / "locks"
    try:
        for fn in (test_decide_review, test_parse_review, test_iface_gate, test_ledger_totals, test_diff_for_review,
                   test_worktree_changes, test_negative_control_timeout, test_status, test_runlock, test_parallel):
            print(f"--- {fn.__name__} ---")
            try:
                fn()
            except Exception as exc:  # 改前（函式還不存在）要紅得有名字，不要整支崩潰
                FAILED.append(f"{fn.__name__} 例外：{exc!r}")
                print(f"[FAIL] {fn.__name__} 例外：{exc!r}")
    finally:
        relay.LOCKS = orig_locks
        locks_tmp.cleanup()
    print(f"\n{PASSED} passed / {len(FAILED)} failed")
    for n in FAILED:
        print("  ✗", n)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
