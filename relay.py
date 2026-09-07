#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""relay.py — 多 Agent 協作編排器 P1：一個子任務一棒（實作 → 驗證 → 審查 → 判定 → commit），最小可用版。

依「多 Agent 協作方案 v3」與 2026-09-07 目標 repo 重構六組的手動實跑固化而成。一棒＝：
  1. 從 base_branch 開隔離 worktree（生產目錄永遠不碰），跑 repo 級前置步驟（例如建 gitignore 的 views/）
  2. 實作者（Codex）依「完全指定」的規格改碼；不 commit
  3. 驗證指令逐條在 worktree 跑，全部 exit 0 才算過（最終事實來源＝測試，不是 CLI 的自我回報）
  4. 審查者（Antigravity agy，唯讀、diff 分塊內嵌）看 diff，回「可合併／需修改」＋未申報問題
  5. 判定器（judge.py）核對每一次 CLI 呼叫真的正常結束；驗證或審查不過 → 把發現餵回實作者再來一輪
  6. 最多 max_rounds 輪不收斂 → 停下來寫 HANDOFF 給人；收斂 → 以**明列路徑**commit 到 branch
  7. 從頭到尾不合併、不重啟、不 push——那三步是人閘門

狀態檔（v3 的三份，放在編排器自己的 runs/<task_id>/，不進受測 repo）：
  STATE.json（機器讀：階段、輪次、每次呼叫的 usage）／CURRENT.md（接手的人第一眼看什麼）／HANDOFF.md（六欄交接簿）

用法：
  PYTHONUTF8=1 python relay.py tasks/<task>.json            # 跑一棒
  PYTHONUTF8=1 python relay.py tasks/<task>.json --dry-run  # 只印計畫不呼叫任何 CLI
離開碼：0＝收斂並已 commit；2＝不收斂／被擋，已寫 HANDOFF 給人；3＝參數／環境錯。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import judge  # noqa: E402

PY = os.environ.get("RELAY_PYTHON", r"C:\Users\<user>\AppData\Local\Programs\Python\Python311\python.exe")
CODEX = os.environ.get("RELAY_CODEX", r"C:\Users\<user>\.vscode\extensions\openai.chatgpt-26.5901.22334-win32-x64\bin\windows-x86_64\codex.exe")
AGY = os.environ.get("RELAY_AGY", r"C:\Users\<user>\AppData\Local\agy\bin\agy.exe")
AGY_REVIEW = HERE / "tools" / "agy_review.py"

IMPL_RULES = """【守則，違反即整棒作廢】
- 你在隔離 worktree 裡工作；生產目錄與其他任何目錄絕對不要碰。不要 git commit、不要 push、不要啟動任何服務、
  不要跑會載入模型的測試（規格列的驗證指令除外）。不要修改 D:\\Tooling\\agent_orchestrator 底下任何檔案。
- 只做規格寫的事，不順手擴張；規格沒寫的檔案不要動。
- 做完用「六欄交接簿」回報：1.完成了什麼 2.改了哪些檔（各幾行） 3.測試結果（指令＋通過數／總數，分「本次新增／既有紅燈／未跑」）
  4.有沒有動到公開介面（函式簽章、路由；有就列簽章） 5.目前卡點 6.建議下一步。
"""

REVIEW_RULES = """你是程式碼審查者，只看 diff 不看實作者的回報。請逐項核對下列條件，每條給 通過／不通過／無法判定 並附 diff 行號當證據；
最後列「未申報問題」（若無請明說「無」）。不要提修法以外的擴張建議。
輸出格式：**第一行必須是**「總判定：可合併」或「總判定：需修改」，再逐條，再「未申報問題」。
"""


@dataclass
class CallRecord:
    role: str
    round: int
    ok: bool
    reason: str
    exit_code: int | None
    seconds: float
    usage: dict | None
    stdout_file: str


@dataclass
class State:
    task_id: str
    phase: str = "init"
    round: int = 0
    branch: str = ""
    worktree: str = ""
    base_commit: str = ""
    commit: str = ""
    verdict: str = ""
    calls: list = field(default_factory=list)
    verify: list = field(default_factory=list)
    started: str = ""
    updated: str = ""


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def sh(cmd: list[str], cwd: str | None = None, timeout: int = 900, env: dict | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ, PYTHONUTF8="1", GIT_TERMINAL_PROMPT="0")
    if env:
        e.update(env)
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, env=e, stdin=subprocess.DEVNULL)


def git(repo: str, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return sh(["git", "-C", repo, *args], timeout=timeout)


class Run:
    def __init__(self, task: dict, dry: bool):
        self.t = task
        self.dry = dry
        self.dir = HERE / "runs" / task["id"]
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state = State(task_id=task["id"], started=now())
        self.wt = task["worktree"]
        self.repo = task["repo"]

    # ---- 狀態檔 ------------------------------------------------------------
    def save(self, phase: str | None = None) -> None:
        if phase:
            self.state.phase = phase
        self.state.updated = now()
        (self.dir / "STATE.json").write_text(json.dumps(asdict(self.state), ensure_ascii=False, indent=1), encoding="utf-8")

    def write_current(self, text: str) -> None:
        (self.dir / "CURRENT.md").write_text(text, encoding="utf-8")

    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        with open(self.dir / "relay.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ---- 1. worktree ---------------------------------------------------------
    def prepare(self) -> None:
        t = self.t
        if self.dry:
            self.log(f"[dry] worktree {self.wt} ← {t['base_branch']} 新分支 {t['branch']}")
            return
        base = git(self.repo, "rev-parse", "--short", t["base_branch"]).stdout.strip()
        self.state.base_commit = base
        if not Path(self.wt).exists():
            r = git(self.repo, "worktree", "add", "-b", t["branch"], self.wt, t["base_branch"], timeout=300)
            if r.returncode != 0:
                # 分支可能已存在：直接掛上
                r = git(self.repo, "worktree", "add", self.wt, t["branch"], timeout=300)
            if r.returncode != 0:
                raise RuntimeError("worktree add 失敗：" + r.stderr[-400:])
            self.log(f"worktree 建立 {self.wt}（{t['branch']} ← {t['base_branch']}@{base}）")
        else:
            self.log(f"worktree 已存在，沿用 {self.wt}")
        self.state.branch, self.state.worktree = t["branch"], self.wt
        for cmd in t.get("prebuild", []):
            self.log(f"prebuild: {cmd}")
            cmd_py = cmd.replace("python ", f'"{PY}" ', 1) if cmd.startswith("python ") else cmd
            r = subprocess.run(cmd_py, cwd=self.wt, shell=True, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", env=dict(os.environ, PYTHONUTF8="1"), timeout=900)
            if r.returncode != 0:
                raise RuntimeError(f"prebuild 失敗（{cmd}）：{r.stderr[-300:]}")
        self.save("prepared")

    # ---- 2. 實作者 -----------------------------------------------------------
    def implement(self, rnd: int, feedback: str) -> tuple[bool, str, dict | None]:
        spec = Path(self.t["spec_file"]).read_text(encoding="utf-8")
        prompt = IMPL_RULES + "\n【規格】\n" + spec
        if feedback:
            prompt += "\n\n【上一輪審查／驗證的發現，請逐條修正後再回報】\n" + feedback
        out_last = self.dir / f"impl_r{rnd}_last_message.md"
        so, se, ex = self.dir / f"impl_r{rnd}.stdout.txt", self.dir / f"impl_r{rnd}.stderr.txt", self.dir / f"impl_r{rnd}.exit.txt"
        (self.dir / f"impl_r{rnd}_prompt.md").write_text(prompt, encoding="utf-8")
        if self.dry:
            self.log(f"[dry] codex exec（round {rnd}，prompt {len(prompt)} 字）")
            return True, "(dry)", None
        self.log(f"實作者 Codex 開跑（round {rnd}）…")
        t0 = time.monotonic()
        cp = sh([CODEX, "exec", "--json", "-s", "workspace-write", "-C", self.wt, "-o", str(out_last), prompt],
                cwd=self.wt, timeout=self.t.get("impl_timeout", 1500))
        so.write_text(cp.stdout, encoding="utf-8"); se.write_text(cp.stderr, encoding="utf-8"); ex.write_text(f"exit={cp.returncode}", encoding="utf-8")
        v = judge.judge("codex", cp.stdout, cp.stderr, cp.returncode)
        rec = CallRecord("implementer", rnd, v.ok, v.reason, cp.returncode, round(time.monotonic() - t0, 1), v.usage, str(so))
        self.state.calls.append(asdict(rec)); self.save()
        self.log(f"實作者結束：{'OK' if v.ok else 'FAIL'}（{rec.seconds}s，{v.reason}）usage={v.usage}")
        report = out_last.read_text(encoding="utf-8") if out_last.exists() else (v.result_text or "")
        return v.ok, report, v.usage

    # ---- 3. 驗證 -------------------------------------------------------------
    def verify(self, rnd: int) -> tuple[bool, str]:
        results, all_ok = [], True
        for item in self.t.get("verify", []):
            name, cmd = item["name"], item["cmd"]
            if self.dry:
                self.log(f"[dry] verify {name}: {cmd}")
                continue
            self.log(f"驗證 {name}: {cmd}")
            cmd_py = cmd.replace("python ", f'"{PY}" ', 1) if cmd.startswith("python ") else cmd
            t0 = time.monotonic()
            cp = subprocess.run(cmd_py, cwd=self.wt, shell=True, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", env=dict(os.environ, PYTHONUTF8="1", **item.get("env", {})),
                                timeout=item.get("timeout", 1800))
            tail = "\n".join((cp.stdout + "\n" + cp.stderr).strip().splitlines()[-6:])
            ok = cp.returncode == 0
            all_ok &= ok
            results.append({"round": rnd, "name": name, "exit": cp.returncode, "seconds": round(time.monotonic() - t0, 1), "tail": tail})
            (self.dir / f"verify_r{rnd}_{name}.txt").write_text(cp.stdout + "\n--- stderr ---\n" + cp.stderr, encoding="utf-8")
            self.log(f"  → {'PASS' if ok else 'FAIL'} exit={cp.returncode} {results[-1]['seconds']}s")
        self.state.verify.extend(results); self.save()
        summary = "\n".join(f"- {r['name']}: {'PASS' if r['exit']==0 else 'FAIL'} (exit {r['exit']})\n  " + r["tail"].replace("\n", "\n  ")
                            for r in results)
        return all_ok, summary

    # ---- 4. 審查 -------------------------------------------------------------
    def diff_for_review(self) -> str:
        # 未追蹤但不被 ignore 的新檔用 intent-to-add 納入 diff（不改 index 內容）
        git(self.wt, "add", "--intent-to-add", "--all")
        d = git(self.wt, "diff", "--", ".", ":(exclude)_refactor/*").stdout
        return d

    def review(self, rnd: int, diff: str) -> tuple[bool, str, dict | None]:
        instr = REVIEW_RULES + "\n【核對條件】\n" + Path(self.t["review"]["instructions_file"]).read_text(encoding="utf-8")
        instr_f, diff_f, out_f = self.dir / f"review_r{rnd}_instr.txt", self.dir / f"review_r{rnd}_diff.txt", self.dir / f"review_r{rnd}_agy.json"
        instr_f.write_text(instr, encoding="utf-8"); diff_f.write_text(diff, encoding="utf-8")
        if self.dry:
            self.log(f"[dry] agy review（round {rnd}，diff {len(diff)} 字）")
            return True, "(dry)", None
        self.log(f"審查者 agy 開跑（round {rnd}，diff {len(diff.encode('utf-8'))} bytes）…")
        t0 = time.monotonic()
        cp = sh([PY, str(AGY_REVIEW), "--instructions", str(instr_f), "--diff", str(diff_f), "--out", str(out_f)],
                cwd=str(HERE), timeout=self.t.get("review_timeout", 1500), env={"AGY_EXE": AGY})
        (self.dir / f"review_r{rnd}_tool.log").write_text(cp.stdout + "\n--- stderr ---\n" + cp.stderr, encoding="utf-8")
        obj = json.loads(out_f.read_text(encoding="utf-8")) if out_f.exists() and out_f.stat().st_size else None
        v = judge.judge("agy", json.dumps(obj) if obj else "", cp.stderr, cp.returncode)
        rec = CallRecord("reviewer", rnd, v.ok, v.reason, cp.returncode, round(time.monotonic() - t0, 1), v.usage, str(out_f))
        self.state.calls.append(asdict(rec)); self.save()
        text = (obj or {}).get("response", "") or ""
        first = next((l for l in text.splitlines() if l.strip()), "")
        approved = ("可合併" in first) and ("需修改" not in first)
        self.log(f"審查者結束：{'OK' if v.ok else 'FAIL'}（{rec.seconds}s）判定行：{first[:40]!r}")
        return v.ok and approved, text, v.usage

    # ---- 6. commit -----------------------------------------------------------
    def changed_paths(self) -> list[str]:
        git(self.wt, "reset", "-q")  # 清掉 intent-to-add
        st = git(self.wt, "status", "--porcelain").stdout.splitlines()
        paths = []
        for line in st:
            code, path = line[:2], line[3:].strip().strip('"')
            if code.strip() == "??" and any(path.startswith(x) for x in self.t.get("ignore_new", ["_refactor/", "views/", "__pycache__"])):
                continue
            paths.append(path)
        allowed = self.t.get("allowed_paths")
        if allowed:
            bad = [p for p in paths if not any(p == a or p.startswith(a.rstrip("/") + "/") for a in allowed)]
            if bad:
                raise RuntimeError(f"實作者動了規格外的檔案：{bad}")
        return paths

    def commit(self, paths: list[str], message: str) -> str:
        if self.dry:
            self.log(f"[dry] commit {paths}")
            return "(dry)"
        r = git(self.wt, "add", "--", *paths)
        if r.returncode != 0:
            raise RuntimeError("git add 失敗：" + r.stderr[-300:])
        msg_f = self.dir / "commit_msg.txt"
        msg_f.write_text(message, encoding="utf-8")
        r = git(self.wt, "commit", "-q", "-F", str(msg_f))
        if r.returncode != 0:
            raise RuntimeError("git commit 失敗：" + r.stderr[-300:])
        return git(self.wt, "rev-parse", "--short", "HEAD").stdout.strip()

    # ---- HANDOFF -------------------------------------------------------------
    def handoff(self, status: str, impl_report: str, verify_summary: str, review_text: str, paths: list[str], blocker: str) -> None:
        usage_lines = []
        for c in self.state.calls:
            u = c["usage"] or {}
            usage_lines.append(f"- round {c['round']} {c['role']}: {'OK' if c['ok'] else 'FAIL'} {c['seconds']}s, "
                               f"input_total={u.get('input_tokens_total')} output={u.get('output_tokens')}")
        text = f"""# HANDOFF — {self.t['id']}（{status}，{now()}）

## 1. 完成了什麼
{'（未收斂，見卡點）' if status != 'done' else self.t.get('title', self.t['id'])}
分支 `{self.state.branch}` @ `{self.state.commit or '未 commit'}`，worktree `{self.wt}`，base `{self.state.base_commit}`。

## 2. 改了哪些檔
{chr(10).join('- ' + p for p in paths) if paths else '- （無）'}

## 3. 測試結果（最後一輪）
{verify_summary or '- （未跑）'}

## 4. 有沒有動到公開介面
（見實作者回報第 4 欄；審查者核對條件含簽章檢查）

## 5. 目前卡點
{blocker or '無'}

## 6. 建議下一步
{'人：審過 HANDOFF 後合併到 base branch、依 repo 紀律部署；編排器不合併不重啟。' if status == 'done' else '人：讀下方審查／驗證輸出決定修法或放棄；worktree 與分支保留。'}

## 用量（每次呼叫，判定器抽取）
{chr(10).join(usage_lines)}

---
### 實作者最後一輪回報（原文）
{impl_report}

---
### 審查者最後一輪（原文）
{review_text}
"""
        (self.dir / "HANDOFF.md").write_text(text, encoding="utf-8")

    # ---- 主流程 --------------------------------------------------------------
    def run(self) -> int:
        self.log(f"任務 {self.t['id']}：{self.t.get('title', '')}")
        self.prepare()
        feedback, impl_report, verify_summary, review_text, paths = "", "", "", "", []
        for rnd in range(1, int(self.t.get("max_rounds", 2)) + 1):
            self.state.round = rnd
            self.write_current(f"# CURRENT\n\n任務 {self.t['id']} round {rnd}/{self.t.get('max_rounds', 2)}：實作中。worktree `{self.wt}`。\n")
            self.save("implement")
            ok, impl_report, _ = self.implement(rnd, feedback)
            if not ok and not self.dry:
                feedback = "實作者的 CLI 呼叫沒有正常結束（判定器：" + self.state.calls[-1]["reason"] + "）。請重做規格。"
                self.log("實作者呼叫失敗，下一輪重試")
                continue
            self.save("verify")
            v_ok, verify_summary = self.verify(rnd)
            self.save("review")
            diff = self.diff_for_review() if not self.dry else "(dry)"
            if not self.dry and not diff.strip():
                feedback = "worktree 沒有任何改動。請照規格實際修改檔案。"
                self.log("沒有 diff，下一輪")
                continue
            r_ok, review_text, _ = self.review(rnd, diff)
            if v_ok and r_ok:
                self.state.verdict = "converged"
                break
            fb = []
            if not v_ok:
                fb.append("【驗證未過】\n" + verify_summary)
            if not r_ok:
                fb.append("【審查判定需修改，原文】\n" + review_text)
            feedback = "\n\n".join(fb)
            self.log(f"round {rnd} 未收斂（verify={'ok' if v_ok else 'fail'} review={'ok' if r_ok else 'fail'}）")
        else:
            self.state.verdict = "escalate"

        if self.dry:
            self.log("[dry] 結束")
            return 0
        if self.state.verdict == "converged":
            paths = self.changed_paths()
            msg = f"{self.t.get('title', self.t['id'])}\n\n（編排器 relay.py：Codex 實作、agy 審查、驗證指令全過；task {self.t['id']}）\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n"
            self.state.commit = self.commit(paths, msg)
            self.save("done")
            self.handoff("done", impl_report, verify_summary, review_text, paths, "")
            self.write_current(f"# CURRENT\n\n任務 {self.t['id']} 已收斂並 commit `{self.state.commit}` 於 `{self.state.branch}`。\n下一步是人：審 HANDOFF.md → 合併 → 部署。編排器到此為止。\n")
            self.log(f"收斂：commit {self.state.commit}（{len(paths)} 個檔）")
            return 0
        self.save("escalate")
        try:
            paths = self.changed_paths()
        except RuntimeError as e:
            paths = [f"（{e}）"]
        self.handoff("escalate", impl_report, verify_summary, review_text, paths,
                     f"{self.state.round} 輪未收斂（驗證或審查不過），已停止；worktree 保留供人接手。")
        self.write_current(f"# CURRENT\n\n任務 {self.t['id']} **未收斂**，已升給人。看 HANDOFF.md。\n")
        self.log("未收斂，升給人")
        return 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    task = json.loads(Path(a.task).read_text(encoding="utf-8"))
    for k in ("id", "repo", "base_branch", "branch", "worktree", "spec_file", "verify", "review"):
        if k not in task:
            print(f"task 缺欄位 {k}", file=sys.stderr)
            return 3
    # 相對路徑一律相對於 relay.py 所在目錄
    for k in ("spec_file",):
        if not Path(task[k]).is_absolute():
            task[k] = str(HERE / task[k])
    if not Path(task["review"]["instructions_file"]).is_absolute():
        task["review"]["instructions_file"] = str(HERE / task["review"]["instructions_file"])
    prod = task.get("production_dir")
    if prod and Path(task["worktree"]).resolve() == Path(prod).resolve():
        print("worktree 不得等於生產目錄", file=sys.stderr)
        return 3
    try:
        return Run(task, a.dry_run).run()
    except RuntimeError as e:
        print("relay 中止：", e, file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
