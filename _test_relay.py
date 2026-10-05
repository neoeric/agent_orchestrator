"""_test_relay.py — relay 的純邏輯測試：難易度判準、結構化審查解析、簽章閘門、帳本、改動判定、
陰性對照逾時、--status 總表（C1）、跨行程鎖與並行上限（C3）、推播（C2）、人工意見回灌 --resume（C6）。不呼叫任何 AI CLI。

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
            # 一棒的所有出口只發一則：Run.abort(kind=rate_limit) 帶 cli 進文案（1 項）
            out_rl = d / "o_rl.txt"
            os.environ["RELAY_NOTIFY_CONFIG"] = str(_write_cfg(d, out_rl))
            with contextlib.redirect_stdout(io.StringIO()):
                r3 = relay.Run({**task, "id": tid + "_rl"}, dry=False)
                r3.abort("撞牆：codex 回 rate_limit", kind="rate_limit", cli="codex")
            body = out_rl.read_text(encoding="utf-8") if out_rl.is_file() else ""
            check("abort(kind='rate_limit', cli=…)：推播第 1 行含 codex 與「撞牆」", body.startswith("rate_limit|【relay】") and "codex" in body.split("\n")[0]
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
            relay.paths.check_all = lambda: []

            def run_ok(self):  # 假的「收斂」：只做會推播的那一步
                self.notify("ready_to_merge")
                return 0

            def run_wall(self):
                raise relay.RateLimitStop("撞牆：codex 回 rate_limit（測試）", cli="codex")

            try:
                rcs = {}
                for name, fake in (("收斂", run_ok), ("撞牆", run_wall)):
                    relay.Run.run = fake
                    relay.NOTIFY_LEDGER = d / f"nl_main_{name}.jsonl"
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        rcs[name] = relay.main([str(d / "task.json")])
                    rm_runs(tid + "_m")
                check("main：推播指令 exit 7 時，收斂棒仍回 0、撞牆棒仍回 3", rcs == {"收斂": 0, "撞牆": 3}, str(rcs))
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
    script＝{"impl": [{"write": {相對路徑: 內容}}…], "verify": [bool…], "review": [(approved, 原文)…]}，每次呼叫 pop 一筆。"""

    def __init__(self, task, script, **kw):
        super().__init__(task, dry=False, **kw)
        self.script = script
        self.calls_log: list[dict] = []

    def implement(self, rnd, feedback, *a, **kw):
        step = self.script["impl"].pop(0)
        prompt = self.impl_prompt(rnd, feedback)
        self.calls_log.append({"role": "impl", "round": rnd, "feedback": feedback, "prompt": prompt})
        for name, text in step.get("write", {}).items():
            Path(self.wt, name).write_bytes(text.encode("utf-8"))
        ok = step.get("ok", True)
        self.record(relay.CallRecord("implementer", rnd, ok, "scripted", 0, 0.0, None, "", cli="codex",
                                     failure_class=step.get("failure_class")))
        return ok, f"（腳本實作者第 {rnd} 輪）", None

    def verify(self, rnd):
        ok = self.script["verify"].pop(0)
        self.state.verify.append({"round": rnd, "name": "v", "exit": 0 if ok else 1, "seconds": 0.0, "tail": ""})
        self.save()
        return ok, f"- v: {'PASS' if ok else 'FAIL'}"

    def review(self, rnd, diff):
        approved, text = self.script["review"].pop(0)
        instr_f, _, _ = self.review_inputs(rnd, diff)
        self.calls_log.append({"role": "review", "round": rnd, "instr": instr_f.read_text(encoding="utf-8"), "diff": diff})
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

    relay.Run, relay.paths.check_all = factory, (lambda: [])
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
            relay.paths.check_all, orig_check = (lambda: ["不該被呼叫"]), relay.paths.check_all
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
                   test_notify, test_notify_telegram, test_resume):
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
