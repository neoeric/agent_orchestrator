"""council.py — 三方討論執行器(2026-10-05 由三份實戰暫存版收斂成正式工具)。

「三方討論」:同一份簡報(brief)給 Codex／Claude／agy 各自獨立回答,主持人整理後第二輪再交叉評審收斂。
跟 relay 派工不同:這裡沒有實作者與審查者,三方都只回答、不改任何檔案。

用法(PYTHONUTF8=1):
  python council.py --dir C:/work/council-topic --round 1
  python council.py --dir C:/work/council-topic --round 2 --repo C:/code/myproject
  python council.py --dir C:/work/council-topic --round 2 --who agy      # 只補跑一方,只覆寫該方的檔

目錄慣例(--dir):
  00_briefing.md          簡報(可用 --brief 改名)
  instructions_r{N}.md    第 N 輪指令(必須存在;不存在就停,不會默默用別輪的)
  r{N}_{codex,claude,agy}.md   產出(統一 LF;失敗的一方不會動它舊的檔)
  _raw/                   各方原始 stdout／stderr 與 prompt(--raw-dir 可改)

離開碼:0＝要求的每一方都有答案;1＝有任一方失敗(其他方的檔照寫);2＝參數錯誤。

唯讀怎麼保證:codex 用 `-s read-only`;claude 只開 Read／Glob／Grep 三個工具(或完全無工具);agy 無工具。
給了 --repo 時,codex 與 claude 預設有該 repo 的唯讀權;agy 無頭模式讀不到檔,不能給。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import judge  # noqa: E402
import relay  # noqa: E402
from tools import agy_review, paths  # noqa: E402

PARTIES = ("codex", "claude", "agy")
DEFAULT_TIMEOUT = {"codex": 1800, "claude": 1200, "agy": 900}  # 秒;實戰上 codex 讀 repo 最久
# agy 的命令列上限約 32K 字元(Windows),所以整包 prompt ≤28000 才單次送;超過就分塊(agy_review.py 同理)。
AGY_SINGLE_MAX = 28000
AGY_CHUNK_MIN = 18000
AGY_MAX_CHUNKS = 3  # README「已知行為」:agy 超過 3 塊最後一問會只回塊確認
CLAUDE_REPO_TOOLS = "Read,Glob,Grep"  # 唯讀硬限制:可用工具只有這三個,沒有 Edit／Write／Bash


# ---------------------------------------------------------------------------
# 純函式:讀檔、組 prompt、分塊、落檔
# ---------------------------------------------------------------------------

def read_text(path: Path) -> str:
    """一律 utf-8-sig:使用者用記事本存的檔可能帶 BOM,不剝掉會變成 prompt 開頭的怪字元。"""
    return Path(path).read_text(encoding="utf-8-sig")


def build_prompt(rnd: int, who: str, *, instr: str, brief: str, prev: dict[str, str] | None,
                 extras: list[tuple[str, str]], repo_access: bool, repo_name: str | None) -> str:
    """順序:指令 → 【簡報】(有 repo 權時加唯讀說明)→ 簡報 → 上一輪各方意見 → 附加材料。

    prev=None 代表不附上一輪(第 1 輪或 --no-prev);prev 是 dict 時,PARTIES 裡缺的那方明寫「無回覆」,
    讓讀的人知道不是漏貼。who 目前不影響內容(差異全在 repo_access),保留是為了之後各方可有不同措辭。
    """
    parts = [instr]
    if repo_access and repo_name:
        parts.append(f"【簡報】（你的工作目錄就是 `{repo_name}` repo，可唯讀核對其中檔案來校正簡報裡的事實；"
                     "不要修改任何檔案，不要讀取 .env 等憑證檔的值）")
    else:
        parts.append("【簡報】")
    parts.append(brief)
    if rnd > 1 and prev is not None:
        parts.append(f"\n---\n# 第 {rnd - 1} 輪各方意見")
        for src in PARTIES:
            text = prev.get(src)
            parts.append(f"\n## 來源：{src}\n\n" + (text if text and text.strip() else "（該方本輪無回覆）"))
    for name, text in extras:
        parts.append(f"\n---\n# 附加材料：{name}\n\n{text}")
    return "\n\n".join(parts)


def plan_agy_chunks(body: str) -> tuple[list[str], str | None]:
    """把簡報本體(不含指令)切成給 agy 的塊;回 (塊們, 錯誤訊息)。以「字元」計,不是 bytes。

    body 本身 ≤28000 字元就不分(1 塊)。否則塊大小 = min(28000, max(18000, ceil(len/3))):
    先試著剛好切成 3 塊,但不小於 18000(塊太小只會多繞)也不超過 28000(命令列上限)。
    算出來超過 3 塊 → 失敗,不硬送(2026-09-16 實測 4 塊時 agy 最後一問只回塊確認)。
    """
    if len(body) <= AGY_SINGLE_MAX:
        return [body], None
    size = min(AGY_SINGLE_MAX, max(AGY_CHUNK_MIN, math.ceil(len(body) / 3)))
    chunks = [body[i:i + size] for i in range(0, len(body), size)]
    if len(chunks) > AGY_MAX_CHUNKS:
        return [], (f"簡報太長（{len(body)} 字元會切成 {len(chunks)} 塊），agy 超過 {AGY_MAX_CHUNKS} 塊不可靠"
                    "（README 已知行為），請精簡或給 agy 摘要")
    return chunks, None


def write_answer(path: Path, text: str) -> None:
    """統一寫成 LF。Windows 上 text mode 會把 \\n 寫成 \\r\\n,同一份討論檔混 CRLF／LF 讓 diff 很吵
    (2026-10-02 實戰版改成 newline="" 的原因);先把 \\r\\n 與孤立 \\r 都正規化,再用 newline="" 原樣寫出。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def write_raw(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text or "")


def codex_cmd(codex: str, raw_dir: Path, rnd: int, workdir: str, model: str | None = None) -> list[str]:
    """prompt 走 stdin(最後的 `-`)以避開命令列 32K 上限(9/29 r2 已到 27K)。

    🔴 -o 必須是絕對路徑:相對路徑會以 -C 為基準,曾差點把輸出檔落進受測 repo。"""
    cmd = [codex, "exec", "--json", "-s", "read-only", "--skip-git-repo-check", "-C", str(workdir),
           "-o", str(Path(raw_dir).resolve() / f"codex_r{rnd}_last.md")]
    if model:
        cmd += ["-m", model]
    return cmd + ["-"]


def claude_cmd(claude: str, repo_access: bool, model: str, safe_mode: bool = True) -> list[str]:
    """有 repo 權:工具只開 Read／Glob／Grep;無權:--tools "" 完全無工具。--strict-mcp-config 搭空
    mcpServers 擋掉使用者全域的 MCP;--safe-mode 擋掉使用者 hooks／plugins／CLAUDE.md(規格 D12),
    因為無頭子行程裡那些東西行為不可預期。prompt 走 stdin。"""
    cmd = [claude, "-p", "--output-format", "json", "--no-session-persistence", "--strict-mcp-config",
           "--mcp-config", '{"mcpServers":{}}', "--tools", CLAUDE_REPO_TOOLS if repo_access else "",
           "--model", model]
    if safe_mode:
        cmd.append("--safe-mode")
    return cmd


def pick_codex_answer(last_msg_path: Path, verdict: judge.Verdict) -> str:
    """-o 檔非空就用它(最乾淨的最終訊息);空或不存在才退回判定器從事件流抽出的 result_text。"""
    p = Path(last_msg_path)
    try:
        if p.exists() and p.stat().st_size:
            t = read_text(p)
            if t.strip():
                return t
    except OSError:
        pass
    return verdict.result_text or ""


def sum_usage(usages: list[dict | None]) -> dict | None:
    """agy 分塊時多次呼叫,用量加總才是這輪真正的花費。"""
    us = [u for u in usages if u]
    if not us:
        return None
    return {"input_tokens_total": sum(int(u.get("input_tokens_total") or 0) for u in us),
            "output_tokens": sum(int(u.get("output_tokens") or 0) for u in us)}


def raw_dir_warning(raw_dir: Path) -> str | None:
    """raw-dir 落在某個 git repo 內且沒被 ignore → 回警告字串(原始串流可能含內部資訊,別被誤 commit)。"""
    d = Path(raw_dir)
    try:
        r = subprocess.run(["git", "-C", str(d), "check-ignore", "-q", str(d.resolve())],
                           capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None  # 沒有 git 就無從檢查,不擋
    if r.returncode == 1:  # 0＝已被 ignore;128＝不在 repo 內
        return f"警告：raw-dir {d} 在某個 git repo 內且未被 ignore，原始輸出可能被誤 commit"
    return None


# ---------------------------------------------------------------------------
# 三方呼叫
# ---------------------------------------------------------------------------

@dataclass
class PartyResult:
    who: str
    ok: bool
    answer: str
    seconds: float
    usage: dict | None
    failure_class: str | None
    note: str = ""


@dataclass
class Ctx:
    rnd: int
    raw_dir: Path
    prompt: str
    instr: str
    repo: Path | None
    repo_access: bool
    scratch: Path          # 空暫存目錄:沒有 repo 權的方當工作目錄,避免它讀到任何東西
    timeout: int
    codex_model: str | None = None
    claude_model: str = "opus"
    safe_mode: bool = True


def run_cli(cmd: list[str], prompt: str, cwd: str | None, env: dict | None, timeout: int) -> tuple[str, str, int]:
    """prompt 以 bytes 走 stdin:text mode 在 Windows 會把 \\n 寫成 \\r\\n,偷偷改了 prompt。"""
    cp = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True, cwd=cwd, env=env, timeout=timeout)
    return (cp.stdout.decode("utf-8", errors="replace"), cp.stderr.decode("utf-8", errors="replace"), cp.returncode)


def _guard(who: str, fn, ctx: Ctx) -> PartyResult:
    """任何例外(含逾時、執行檔不存在)都轉成這一方的失敗,不讓它拖垮其他方的 thread。"""
    t0 = time.monotonic()
    try:
        return fn(ctx)
    except subprocess.TimeoutExpired:
        return PartyResult(who, False, "", time.monotonic() - t0, None, "timeout", f"逾時（{ctx.timeout}s）")
    except Exception as e:  # noqa: BLE001 — 刻意全接,理由見 docstring
        return PartyResult(who, False, "", time.monotonic() - t0, None, "exception", f"{type(e).__name__}: {e}")


def _result_from_verdict(who: str, v: judge.Verdict, answer: str, dt: float) -> PartyResult:
    ok = bool(v.ok and answer.strip())
    note = "" if ok else (v.reason + ("；答案為空" if v.ok else ""))
    return PartyResult(who, ok, answer if ok else "", dt, v.usage, None if ok else (v.failure_class or "unknown"), note)


def _run_codex(ctx: Ctx) -> PartyResult:
    exe = paths.resolve_codex()
    if not exe:
        return PartyResult("codex", False, "", 0.0, None, "config", "找不到 codex(設 RELAY_CODEX)")
    cmd = codex_cmd(exe, ctx.raw_dir, ctx.rnd, str(ctx.repo if ctx.repo_access else ctx.scratch), ctx.codex_model)
    last = Path(cmd[cmd.index("-o") + 1])
    last.unlink(missing_ok=True)  # 舊檔會被誤當這次的答案
    t0 = time.monotonic()
    out, err, rc = run_cli(cmd, ctx.prompt, None, None, ctx.timeout)
    dt = time.monotonic() - t0
    write_raw(ctx.raw_dir / f"codex_r{ctx.rnd}.stdout.jsonl", out)
    write_raw(ctx.raw_dir / f"codex_r{ctx.rnd}.stderr.txt", err)
    v = judge.judge("codex", out, err, rc)
    return _result_from_verdict("codex", v, pick_codex_answer(last, v), dt)


def _run_claude(ctx: Ctx) -> PartyResult:
    exe = paths.resolve_claude()
    if not exe:
        return PartyResult("claude", False, "", 0.0, None, "config", "找不到 claude(設 RELAY_CLAUDE)")
    cmd = claude_cmd(exe, ctx.repo_access, ctx.claude_model, ctx.safe_mode)
    cwd = str(ctx.repo if ctx.repo_access else ctx.scratch)
    t0 = time.monotonic()
    out, err, rc = run_cli(cmd, ctx.prompt, cwd, paths.claude_env(), ctx.timeout)
    dt = time.monotonic() - t0
    write_raw(ctx.raw_dir / f"claude_r{ctx.rnd}.stdout.json", out)
    write_raw(ctx.raw_dir / f"claude_r{ctx.rnd}.stderr.txt", err)
    v = judge.judge("claude", out, err, rc)
    return _result_from_verdict("claude", v, v.result_text or "", dt)


def _run_agy(ctx: Ctx) -> PartyResult:
    t0 = time.monotonic()
    full = agy_review.NO_TOOLS + "\n\n" + ctx.prompt
    usages: list[dict | None] = []
    if len(full) <= AGY_SINGLE_MAX:
        obj, err, rc = agy_review.run_agy(full, None, ctx.timeout)
    else:
        body = ctx.prompt[len(ctx.instr):]
        chunks, problem = plan_agy_chunks(body)
        if problem:
            return PartyResult("agy", False, "", time.monotonic() - t0, None, "too_long", problem)
        conv: str | None = None
        for i, ch in enumerate(chunks, 1):
            msg = (f"{agy_review.NO_TOOLS}\n以下是討論材料第 {i}/{len(chunks)} 段，先不要作答，只回「OK」。\n\n" + ch)
            o, e, r = agy_review.run_agy(msg, conv, ctx.timeout)
            v = judge.judge("agy", json.dumps(o, ensure_ascii=False) if o else "", e, r)
            usages.append(v.usage)
            if not v.ok:
                write_raw(ctx.raw_dir / f"agy_r{ctx.rnd}.stderr.txt", e)
                return PartyResult("agy", False, "", time.monotonic() - t0, sum_usage(usages),
                                   v.failure_class or "unknown", f"第 {i}/{len(chunks)} 塊送入失敗：{v.reason}")
            if i == 1:
                conv = (o or {}).get("conversation_id")
                if not conv:
                    # 不退回 --continue:它接的是「最近一個對話」,使用者同時開著 agy 就會接到別人的對話
                    return PartyResult("agy", False, "", time.monotonic() - t0, sum_usage(usages), "no_conversation",
                                       "第 1 塊沒有回傳 conversation_id，無法釘住同一個對話（不退回 --continue）")
        obj, err, rc = agy_review.run_agy(
            agy_review.NO_TOOLS + "\n材料已送完。現在依下面指令作答：\n\n" + ctx.instr, conv, ctx.timeout)
    dt = time.monotonic() - t0
    write_raw(ctx.raw_dir / f"agy_r{ctx.rnd}.json", json.dumps(obj, ensure_ascii=False, indent=1) if obj else "")
    write_raw(ctx.raw_dir / f"agy_r{ctx.rnd}.stderr.txt", err)
    v = judge.judge("agy", json.dumps(obj, ensure_ascii=False) if obj else "", err, rc)
    usages.append(v.usage)
    res = _result_from_verdict("agy", v, v.result_text or "", dt)
    res.usage = sum_usage(usages) if len(usages) > 1 else v.usage
    return res


RUNNERS = {"codex": _run_codex, "claude": _run_claude, "agy": _run_agy}


def ledger_record(dir_name: str, rnd: int, r: PartyResult) -> None:
    """用量入帳,讓 `relay.py --ledger` 也看得到討論用量。記帳失敗只警告,不影響討論結果。"""
    try:
        relay.ledger_append({"ts": relay.now(), "task": f"council:{dir_name}", "role": f"council-r{rnd}",
                             "cli": r.who, "round": rnd, "ok": r.ok, "failure_class": r.failure_class,
                             "seconds": round(r.seconds, 1), "usage": r.usage})
    except Exception as e:  # noqa: BLE001
        print(f"警告：帳本寫入失敗：{e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def parse_list(s: str, what: str) -> list[str]:
    items = [x.strip() for x in s.split(",") if x.strip()]
    bad = [x for x in items if x not in PARTIES]
    if bad or not items:
        raise ValueError(f"{what} 只能是 {','.join(PARTIES)} 的組合，收到：{s!r}")
    return list(dict.fromkeys(items))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="三方討論執行器：同一份簡報給 codex／claude／agy 各自回答。")
    ap.add_argument("--dir", required=True, help="討論目錄（放 00_briefing.md、instructions_r{N}.md）")
    ap.add_argument("--round", type=int, required=True, help="第幾輪（>=1）")
    ap.add_argument("--who", default="codex,claude,agy", help="要跑哪幾方，預設三方（逗號分隔）")
    ap.add_argument("--repo", help="受測 repo 路徑（唯讀參考）")
    ap.add_argument("--repo-access", help="哪幾方可唯讀存取 repo；給了 --repo 預設 codex,claude；不可含 agy")
    ap.add_argument("--brief", default="00_briefing.md", help="簡報檔名（相對 --dir）")
    ap.add_argument("--extra", action="append", default=[], metavar="FILE", help="附加材料，可重複，依序附在最後")
    ap.add_argument("--no-prev", action="store_true", help="第 N>1 輪不附上一輪各方意見")
    ap.add_argument("--claude-model", default="opus")
    ap.add_argument("--codex-model")
    ap.add_argument("--no-safe-mode", action="store_true", help="claude 不加 --safe-mode（若它影響輸出格式時退路）")
    ap.add_argument("--timeout", type=int, help="覆寫各方逾時秒數（預設 codex 1800／claude 1200／agy 900）")
    ap.add_argument("--raw-dir", help="原始輸出目錄，預設 <dir>/_raw")
    ap.add_argument("--dump", action="store_true", help="只把各方 prompt 寫到 raw-dir，不呼叫任何 CLI")
    return ap


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    a = build_parser().parse_args(argv)

    def usage_error(msg: str) -> int:
        print(f"參數錯誤：{msg}", file=sys.stderr)
        return 2

    if a.round < 1:
        return usage_error("--round 必須 >= 1")
    d = Path(a.dir)
    if not d.is_dir():
        return usage_error(f"--dir 不存在：{d}")
    try:
        who = parse_list(a.who, "--who")
        if a.repo_access is not None:
            access = parse_list(a.repo_access, "--repo-access")
        else:
            access = ["codex", "claude"] if a.repo else []
    except ValueError as e:
        return usage_error(str(e))
    if "agy" in access:
        return usage_error("--repo-access 不可含 agy（agy 無頭模式讀不到檔，給了也沒用）")
    repo: Path | None = None
    if a.repo:
        repo = Path(a.repo).resolve()
        if not repo.is_dir():
            return usage_error(f"--repo 不存在或不是目錄：{a.repo}")
    elif access:
        return usage_error("給了 --repo-access 卻沒給 --repo")

    instr_path = d / f"instructions_r{a.round}.md"
    brief_path = d / a.brief
    for p, label in ((instr_path, "第 N 輪指令檔"), (brief_path, "簡報檔")):
        if not p.is_file():
            return usage_error(f"{label}不存在：{p}")
    extras: list[tuple[str, str]] = []
    for e in a.extra:
        ep = Path(e) if Path(e).is_absolute() or Path(e).exists() else d / e
        if not ep.is_file():
            return usage_error(f"--extra 檔案不存在：{e}")
        extras.append((ep.name, read_text(ep)))

    instr, brief = read_text(instr_path), read_text(brief_path)
    prev: dict[str, str] | None = None
    if a.round > 1 and not a.no_prev:
        prev = {}
        for src in PARTIES:
            pp = d / f"r{a.round - 1}_{src}.md"
            if pp.is_file():
                prev[src] = read_text(pp)

    raw_dir = Path(a.raw_dir) if a.raw_dir else d / "_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = raw_dir.resolve()

    if repo and len(who) > 1 and len([w for w in who if w in access]) < 2:
        print("警告：審查程式設計類題目建議至少兩方有 repo 唯讀權", file=sys.stderr)
    warn = raw_dir_warning(raw_dir)
    if warn:
        print(warn, file=sys.stderr)

    prompts = {w: build_prompt(a.round, w, instr=instr, brief=brief, prev=prev, extras=extras,
                               repo_access=(w in access), repo_name=(repo.name if repo else None)) for w in who}
    if a.dump:
        for w, p in prompts.items():
            write_raw(raw_dir / f"prompt_r{a.round}_{w}.md", p)
            print(f"dump: {raw_dir / f'prompt_r{a.round}_{w}.md'} ({len(p)} 字)")
        return 0

    scratch = Path(tempfile.mkdtemp(prefix="council_empty_"))
    results: dict[str, PartyResult] = {}
    try:
        def work(w: str) -> None:
            ctx = Ctx(rnd=a.round, raw_dir=raw_dir, prompt=prompts[w], instr=instr, repo=repo,
                      repo_access=(w in access), scratch=scratch, timeout=a.timeout or DEFAULT_TIMEOUT[w],
                      codex_model=a.codex_model, claude_model=a.claude_model, safe_mode=not a.no_safe_mode)
            results[w] = _guard(w, RUNNERS[w], ctx)

        threads = [threading.Thread(target=work, args=(w,)) for w in who]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    for w in who:
        r = results[w]
        if r.ok:
            write_answer(d / f"r{a.round}_{w}.md", r.answer)
        ledger_record(d.resolve().name, a.round, r)
        inp = (r.usage or {}).get("input_tokens_total", "?")
        status = "OK" if r.ok else f"FAIL({r.failure_class})"
        print(f"{w}: {status} {r.seconds:.0f}s {len(r.answer)}字 input_total={inp}" + (f"  {r.note}" if r.note else ""))
    print(f"council r{a.round}: " + " ".join(
        f"{w}={'OK' if results[w].ok else 'FAIL(' + str(results[w].failure_class) + ')'}" for w in who))
    return 0 if all(results[w].ok for w in who) else 1


if __name__ == "__main__":
    sys.exit(main())
