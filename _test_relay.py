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
            shutil.rmtree(relay.HERE / "runs" / "_test_diff_for_review", ignore_errors=True)

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


def main() -> int:
    for fn in (test_decide_review, test_parse_review, test_iface_gate, test_ledger_totals, test_diff_for_review,
               test_worktree_changes, test_negative_control_timeout):
        print(f"--- {fn.__name__} ---")
        try:
            fn()
        except Exception as exc:  # 改前（函式還不存在）要紅得有名字，不要整支崩潰
            FAILED.append(f"{fn.__name__} 例外：{exc!r}")
            print(f"[FAIL] {fn.__name__} 例外：{exc!r}")
    print(f"\n{PASSED} passed / {len(FAILED)} failed")
    for n in FAILED:
        print("  ✗", n)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
