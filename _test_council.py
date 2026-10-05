"""_test_council.py — council.py 與 tools/paths.py(Claude 解析、巢狀環境)的純邏輯測試。

跑法：PYTHONUTF8=1 python _test_council.py   （exit 0＝全過）
不呼叫任何 AI CLI:執行檔用假名字、subprocess 與 agy_review.run_agy 都被 monkeypatch。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import council  # noqa: E402
import judge  # noqa: E402
import relay  # noqa: E402
from tools import agy_review, paths  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
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


@contextlib.contextmanager
def env_patch(**kw):
    """暫時改環境變數(值 None＝刪掉),離開還原。"""
    old = {k: os.environ.get(k) for k in kw}
    try:
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")
    return p


# ---------------------------------------------------------------------------
# S0:Claude CLI 解析與巢狀環境
# ---------------------------------------------------------------------------

def test_s0() -> None:
    e = paths.claude_env({"CLAUDECODE": "1", "CLAUDE_CODE_OAUTH_TOKEN": "x", "PATH": "p"})
    check("S0 claude_env：清掉 CLAUDECODE", "CLAUDECODE" not in e)
    check("S0 claude_env：保留認證變數與 PATH", e.get("CLAUDE_CODE_OAUTH_TOKEN") == "x" and e.get("PATH") == "p", str(e))

    orig_which = paths.shutil.which
    try:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            home = root / "home"
            home.mkdir()
            npm = root / "npm"
            cmd = touch(npm / "claude.cmd")
            exe = touch(npm / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe")
            with env_patch(RELAY_CLAUDE=None, USERPROFILE=str(home)):
                paths.shutil.which = lambda n: str(cmd) if n == "claude" else None
                check("S0 resolve_claude：.cmd 薄殼 → 同目錄原生 exe", paths.resolve_claude() == str(exe), str(paths.resolve_claude()))
                exe.unlink()
                got = paths.resolve_claude()
                check("S0 resolve_claude：exe 不存在時不回 .cmd", got is None or not str(got).lower().endswith(".cmd"), str(got))
                with env_patch(RELAY_CLAUDE="C:/x/my-claude"):
                    check("S0 resolve_claude：RELAY_CLAUDE 優先", paths.resolve_claude() == "C:/x/my-claude")
                paths.shutil.which = lambda n: None
                ext = home / ".vscode" / "extensions"
                touch(ext / "anthropic.claude-code-2.1.30-win32-x64" / "resources" / "native-binary" / "claude.exe")
                want = touch(ext / "anthropic.claude-code-2.1.289-win32-x64" / "resources" / "native-binary" / "claude.exe")
                check("S0 resolve_claude：擴充目錄 2.1.289 勝過 2.1.30（版本 tuple 排序）",
                      paths.resolve_claude() == str(want), str(paths.resolve_claude()))
    finally:
        paths.shutil.which = orig_which


# ---------------------------------------------------------------------------
# C7 純函式
# ---------------------------------------------------------------------------

def bp(rnd=1, **kw):
    base = dict(instr="INSTR", brief="BRIEF", prev=None, extras=[], repo_access=False, repo_name=None)
    base.update(kw)
    return council.build_prompt(rnd, "codex", **base)


def test_build_prompt() -> None:
    check("build_prompt：r1 不含「第 0 輪」", "第 0 輪" not in bp(1))
    p2 = bp(2, prev={"codex": "CODEX-R1", "agy": "AGY-R1"})
    check("build_prompt：r2 附上存在的 r1 內容", "CODEX-R1" in p2 and "AGY-R1" in p2 and "# 第 1 輪各方意見" in p2)
    check("build_prompt：缺檔那方標「無回覆」", "## 來源：claude\n\n（該方本輪無回覆）" in p2, p2)
    check("build_prompt：--no-prev（prev=None）不附", "各方意見" not in bp(2, prev=None))
    pr = bp(1, repo_access=True, repo_name="myproject")
    check("build_prompt：有 repo 權含「唯讀核對」", "唯讀核對" in pr and "myproject" in pr)
    check("build_prompt：無 repo 權不含「唯讀核對」", "唯讀核對" not in bp(1, repo_access=False, repo_name="myproject"))
    px = bp(2, prev={}, extras=[("a.md", "AAA"), ("b.md", "BBB")])
    check("build_prompt：extra 依序附在最後", px.index("附加材料：a.md") < px.index("附加材料：b.md")
          and px.index("無回覆") < px.index("附加材料：a.md") and px.rstrip().endswith("BBB"))
    check("build_prompt：指令在最前面", bp(1).startswith("INSTR"))


def test_plan_agy_chunks() -> None:
    c, err = council.plan_agy_chunks("測" * 20000)
    check("plan_agy_chunks：20,000 字 → 1 塊", len(c) == 1 and err is None)
    c, err = council.plan_agy_chunks("測" * 60000)
    check("plan_agy_chunks：60,000 字 → 3 塊且每塊 ≤28,000", len(c) == 3 and all(len(x) <= 28000 for x in c) and err is None,
          f"{[len(x) for x in c]} {err}")
    check("plan_agy_chunks：分塊後串回等於原文", "".join(c) == "測" * 60000)
    c, err = council.plan_agy_chunks("測" * 100000)
    check("plan_agy_chunks：100,000 字 → 失敗且訊息含「3 塊」", not c and err is not None and "3 塊" in err, str(err))


def test_write_answer() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "a.md"
        council.write_answer(p, "一\r\n二\r三\n四")
        b = p.read_bytes()
        check("write_answer：bytes 不含 \\r（CRLF 與孤立 CR 都轉 LF）", b"\r" not in b and b == "一\n二\n三\n四".encode("utf-8"), repr(b))


def test_commands() -> None:
    with tempfile.TemporaryDirectory() as d:
        raw = Path(d) / "raw"
        repo = Path(d) / "repo"
        empty = Path(d) / "empty"
        cmd = council.codex_cmd("codex", raw, 2, str(repo), None)
        check("codex 指令：最後參數是 -（prompt 走 stdin）", cmd[-1] == "-", str(cmd))
        check("codex 指令：-s read-only", cmd[cmd.index("-s") + 1] == "read-only")
        o = cmd[cmd.index("-o") + 1]
        check("codex 指令：-o 是絕對路徑", os.path.isabs(o) and o.endswith("codex_r2_last.md"), o)
        cmd2 = council.codex_cmd("codex", raw, 1, str(empty), "m1")
        check("codex 指令：無 repo 權時 -C 不是 repo", cmd2[cmd2.index("-C") + 1] == str(empty) and cmd2[cmd2.index("-C") + 1] != str(repo)
              and "-m" in cmd2 and cmd2[cmd2.index("-m") + 1] == "m1")
    c1 = council.claude_cmd("claude", True, "opus")
    c0 = council.claude_cmd("claude", False, "opus")
    check("claude 指令：有 repo 權 --tools Read,Glob,Grep", c1[c1.index("--tools") + 1] == "Read,Glob,Grep")
    check("claude 指令：無權 --tools \"\"", c0[c0.index("--tools") + 1] == "")
    flat = " ".join(c1 + c0)
    check("claude 指令：任何情況都不含 Edit／Write／Bash", not any(w in flat for w in ("Edit", "Write", "Bash")), flat)
    with env_patch(CLAUDECODE="1", CLAUDE_CODE_SESSION_ID="s"):
        check("claude env：不含 CLAUDECODE 等巢狀變數", not any(k in paths.claude_env() for k in paths.CLAUDE_NESTED_ENV))
    check("claude 指令：預設含 --safe-mode、--no-safe-mode 時不含",
          "--safe-mode" in c1 and "--safe-mode" not in council.claude_cmd("claude", True, "opus", safe_mode=False))


def test_pick_answer() -> None:
    out = (FIX / "codex_ok.stdout.txt").read_text(encoding="utf-8")
    v = judge.judge("codex", out, "", 0)
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "last.md"
        f.write_text("FROM-O-FILE", encoding="utf-8")
        check("答案優先序：-o 檔非空用它", council.pick_codex_answer(f, v) == "FROM-O-FILE")
        f.write_text("", encoding="utf-8")
        check("答案優先序：-o 檔空 → 用 judge 的 result_text", council.pick_codex_answer(f, v) == "OK" == v.result_text)


# ---------------------------------------------------------------------------
# main:參數驗證、dump、整體流程(全用假 CLI)
# ---------------------------------------------------------------------------

def run_main(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = council.main(argv)
    return rc, out.getvalue(), err.getvalue()


def make_dir(root: Path, rounds=(1,)) -> Path:
    d = root / "topic"
    d.mkdir()
    (d / "00_briefing.md").write_text("簡報內容\n", encoding="utf-8")
    for r in rounds:
        (d / f"instructions_r{r}.md").write_text(f"第{r}輪指令\n", encoding="utf-8")
    return d


def test_args_and_dump() -> None:
    with tempfile.TemporaryDirectory() as t:
        d = make_dir(Path(t))
        rc, _, err = run_main(["--dir", str(d), "--round", "1", "--repo", t, "--repo-access", "agy"])
        check("參數驗證：--repo-access agy → 2", rc == 2, err)
        rc, _, err = run_main(["--dir", str(d), "--round", "2"])
        check("參數驗證：指令檔不存在 → 2", rc == 2 and "instructions_r2.md" in err, err)
        rc, _, _ = run_main(["--dir", str(d), "--round", "1", "--who", "gemini"])
        check("參數驗證：--who 含未知方 → 2", rc == 2)
        rc, _, _ = run_main(["--dir", str(d), "--round", "1", "--repo-access", "codex"])
        check("參數驗證：給 --repo-access 沒給 --repo → 2", rc == 2)

        # --dump:三方函式一律丟例外,證明沒有任何呼叫
        def boom(ctx):
            raise AssertionError("dump 不該呼叫任何 CLI")
        orig = dict(council.RUNNERS), council.run_cli
        council.RUNNERS.update({k: boom for k in council.PARTIES})
        council.run_cli = lambda *a, **k: boom(None)
        try:
            raw = Path(t) / "raw"
            rc, out, err = run_main(["--dir", str(d), "--round", "1", "--dump", "--raw-dir", str(raw)])
            files = sorted(p.name for p in raw.glob("prompt_r1_*.md"))
            check("--dump：不呼叫 CLI，raw-dir 出現三份 prompt", rc == 0 and files == [f"prompt_r1_{w}.md" for w in sorted(council.PARTIES)],
                  f"{rc} {files} {err}")
        finally:
            council.RUNNERS.update(orig[0])
            council.run_cli = orig[1]


def test_agy_no_conversation() -> None:
    calls: list[str | None] = []
    orig = agy_review.run_agy

    def fake(prompt, conv, timeout):
        calls.append(conv)
        # 成功但沒有 conversation_id
        return ({"status": "SUCCESS", "response": "OK", "usage": {"input_tokens": 1, "output_tokens": 1}}, "", 0)
    agy_review.run_agy = fake
    try:
        with tempfile.TemporaryDirectory() as t:
            ctx = council.Ctx(rnd=1, raw_dir=Path(t), prompt="I" + "測" * 40000, instr="I", repo=None, repo_access=False,
                              scratch=Path(t), timeout=5)
            r = council.RUNNERS["agy"](ctx)
        check("agy 分塊：第 1 塊無 conversation_id → 失敗且未退回 --continue",
              (not r.ok) and r.failure_class == "no_conversation" and "__continue__" not in calls and len(calls) == 1,
              f"{r} {calls}")
    finally:
        agy_review.run_agy = orig


def test_raw_dir_warning() -> None:
    with tempfile.TemporaryDirectory() as t:
        repo = Path(t) / "r"
        raw = repo / "_raw"
        raw.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        check("raw-dir 在 git repo 內未 ignore → 警告", council.raw_dir_warning(raw) is not None)
        (repo / ".gitignore").write_text("_raw/\n", encoding="utf-8")
        check("raw-dir 已被 ignore → 無警告", council.raw_dir_warning(raw) is None)
        other = Path(t) / "plain"
        other.mkdir()
        check("raw-dir 不在 repo 內 → 無警告", council.raw_dir_warning(other) is None)


def test_end_to_end() -> None:
    """假 CLI 跑兩輪:r1 全成功、r2 agy 登入過期(部分失敗 exit 1)。驗證檔案結構、LF、帳本、巢狀環境。"""
    codex_out = (FIX / "codex_ok.stdout.txt").read_text(encoding="utf-8")
    claude_out = (FIX / "claude_ok.stdout.txt").read_text(encoding="utf-8")
    agy_out = json.loads((FIX / "agy_ok.stdout.txt").read_text(encoding="utf-8"))
    seen: list[dict] = []
    agy_fail = {"on": False}

    def fake_run_cli(cmd, prompt, cwd, env, timeout):
        seen.append({"exe": cmd[0], "cmd": cmd, "prompt": prompt, "cwd": cwd, "env": env})
        if cmd[0] == "fake-codex":
            Path(cmd[cmd.index("-o") + 1]).write_text("CODEX 答案\r\n第二行", encoding="utf-8", newline="")
            return codex_out, "", 0
        return claude_out, "", 0

    def fake_agy(prompt, conv, timeout):
        seen.append({"exe": "agy", "prompt": prompt, "conv": conv})
        if agy_fail["on"]:
            return ({"status": "ERROR", "error": "authentication failed or timed out"}, "Authentication required", 1)
        return (dict(agy_out, response="AGY 答案\r\n"), "", 0)

    saved = (council.run_cli, agy_review.run_agy, paths.resolve_codex, paths.resolve_claude, relay.LEDGER)
    council.run_cli, agy_review.run_agy = fake_run_cli, fake_agy
    paths.resolve_codex, paths.resolve_claude = (lambda: "fake-codex"), (lambda: "fake-claude")
    try:
        with tempfile.TemporaryDirectory() as t, env_patch(CLAUDECODE="1"):
            relay.LEDGER = Path(t) / "ledger.jsonl"
            d = make_dir(Path(t), rounds=(1, 2))
            repo = Path(t) / "myproject"
            repo.mkdir()
            rc, out, err = run_main(["--dir", str(d), "--round", "1", "--repo", str(repo)])
            check("e2e r1：三方全成功 exit 0 且最後一行彙總", rc == 0 and out.strip().splitlines()[-1] == "council r1: codex=OK claude=OK agy=OK",
                  f"{rc}\n{out}\n{err}")
            names = sorted(p.name for p in d.iterdir())
            check("e2e r1：產出 r1_{codex,claude,agy}.md 與 _raw", all(f"r1_{w}.md" in names for w in council.PARTIES) and "_raw" in names, str(names))
            crs = sum(p.read_bytes().count(b"\r") for p in d.glob("r1_*.md"))
            check("e2e r1：回覆檔 CR 計數＝0（LF）", crs == 0, f"CR={crs}")
            check("e2e r1：codex 答案取自 -o 檔", (d / "r1_codex.md").read_text(encoding="utf-8") == "CODEX 答案\n第二行")
            cl = next(s for s in seen if s["exe"] == "fake-claude")
            check("e2e r1：claude 在 repo 內以 Read,Glob,Grep 跑、env 無 CLAUDECODE",
                  cl["cwd"] == str(repo.resolve()) and "Read,Glob,Grep" in cl["cmd"] and "CLAUDECODE" not in cl["env"])
            check("e2e r1：prompt 以 stdin 傳、不在 argv", cl["prompt"].startswith("第1輪指令") and not any("簡報內容" in str(c) for c in cl["cmd"]))
            ag = next(s for s in seen if s["exe"] == "agy")
            check("e2e r1：agy 的 prompt 不含 repo 說明", "唯讀核對" not in ag["prompt"])
            raw = sorted(p.name for p in (d / "_raw").iterdir())
            check("e2e r1：raw-dir 有各方原始輸出", {"codex_r1.stdout.jsonl", "claude_r1.stdout.json", "agy_r1.json"} <= set(raw), str(raw))
            lines = relay.LEDGER.read_text(encoding="utf-8").splitlines()
            e0 = json.loads(lines[0])
            check("e2e r1：帳本入帳三筆（task=council:topic）", len(lines) == 3 and e0["task"] == "council:topic" and e0["role"] == "council-r1", str(lines))

            seen.clear()
            agy_fail["on"] = True
            rc, out, err = run_main(["--dir", str(d), "--round", "2", "--repo", str(repo)])
            last = out.strip().splitlines()[-1]
            check("e2e r2：agy 登入過期 → exit 1、failure_class=auth、其他兩方照寫",
                  rc == 1 and last == "council r2: codex=OK claude=OK agy=FAIL(auth)"
                  and (d / "r2_codex.md").exists() and (d / "r2_claude.md").exists() and not (d / "r2_agy.md").exists(), f"{rc}\n{out}")
            cp = next(s for s in seen if s["exe"] == "fake-codex")["prompt"]
            check("e2e r2：prompt 帶上 r1 各方意見", "AGY 答案" in cp and "CODEX 答案" in cp)

            seen.clear()
            rc, out, _ = run_main(["--dir", str(d), "--round", "2", "--who", "agy", "--no-prev"])
            check("e2e 補跑單方：只呼叫該方、不附上一輪", [s["exe"] for s in seen] == ["agy"] and "各方意見" not in seen[0]["prompt"] and rc == 1)
    finally:
        council.run_cli, agy_review.run_agy, paths.resolve_codex, paths.resolve_claude, relay.LEDGER = saved


def main() -> int:
    for fn in (test_s0, test_build_prompt, test_plan_agy_chunks, test_write_answer, test_commands, test_pick_answer,
               test_args_and_dump, test_agy_no_conversation, test_raw_dir_warning, test_end_to_end):
        print(f"--- {fn.__name__} ---")
        fn()
    print(f"\n{PASSED} passed / {len(FAILED)} failed")
    for n in FAILED:
        print("  ✗", n)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
