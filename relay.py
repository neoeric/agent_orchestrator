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
  PYTHONUTF8=1 python relay.py tasks/<task>.json --dry-run  # 只印計畫不呼叫任何 CLI（紀錄寫到 runs/<id>.dry/）
  PYTHONUTF8=1 python relay.py tasks/<task>.json --queue    # 並行名額（RELAY_MAX_PARALLEL，預設 1）滿了就排隊
  PYTHONUTF8=1 python relay.py --status [--all]             # 所有棒的階段、輪次、耗時、是否等人
  PYTHONUTF8=1 python relay.py --resume <task_id> [--rounds N]  # 人工意見回灌：讀 runs/<id>/human_notes.md 接續下一輪
離開碼：0＝收斂並已 commit；2＝不收斂／被擋，已寫 HANDOFF 給人；3＝參數／環境錯、被鎖擋下拒跑、或例外中止
（STATE 記 aborted）；130＝Ctrl-C。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
from contextlib import ExitStack, nullcontext
from dataclasses import MISSING, dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import judge  # noqa: E402
from tools import iface_gate, inject_check, notify, paths, runlock  # noqa: E402

# 2026-10-05：verify 與陰性對照共用同一個逾時預設。原本陰性對照寫死 300、verify 是 1800，
# 沒寫 timeout 的慢測試 verify 綠、陰性對照卻逾時，被誤判成「新斷言沒抓到違規」而退回實作者。
VERIFY_TIMEOUT_DEFAULT = 1800
# 2026-10-05：「新增但不算改動」的未追蹤路徑前綴預設值，changed_paths 與送審判準兩處共用。
IGNORE_NEW_DEFAULT = ["_refactor/", "views/", "__pycache__"]
# P4：跨任務用量帳本（只記帳，不換手）。2026-10-05：可由 RELAY_LEDGER 覆寫（多行程併寫測試用），預設不變。
LEDGER = Path(os.environ.get("RELAY_LEDGER") or HERE / "runs" / "usage_ledger.jsonl")

# ---- C3（2026-10-05）：並行鎖。鎖檔都在 runs/.locks/（runs/ 已 gitignore），語意見 tools/runlock.py。
# 鎖只在同一份 relay 安裝目錄內有效（每份安裝各有自己的 runs/）。
LOCKS = HERE / "runs" / ".locks"
MAX_PARALLEL_HARD = 3     # RELAY_MAX_PARALLEL 硬上限：再多＝同時燒更多份訂閱額度，且 agy／Codex 本機狀態並行未實測
REPO_LOCK_TIMEOUT = 600   # 同 repo 的 worktree add／commit 互斥；別棒的 commit 正常是秒級，等 10 分鐘還拿不到就大聲停
QUEUE_POLL_SECONDS = 30   # --queue 時重試並行名額的間隔
TASK_LOCK_GRACE = 0.5     # 取任務鎖時多等的秒數：吸收 --status 判活「拿一下立刻放」的瞬間（見 runlock.is_held）
TASK_ID_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,99}")
# ---- C1（2026-10-05）：--status 總表
STATUS_LIMIT = 15
TERMINAL_PHASES = ("done", "escalate", "aborted")
# ---- C2（2026-10-05）：只在需要人動手時推播。設定檔不存在＝整個功能關閉；測試把 RELAY_NOTIFY_CONFIG 指到不存在的路徑
NOTIFY_LEDGER = HERE / "runs" / "notify_ledger.jsonl"


def notify_config_path() -> Path:
    """每次呼叫才讀環境變數（不在 import 時固定），測試與臨時換設定才有效。"""
    return Path(os.environ.get("RELAY_NOTIFY_CONFIG") or HERE / "notify.json")


class RateLimitStop(RuntimeError):
    """撞牆（CLI 回 rate_limit）。獨立型別是為了讓 main() 以 kind="rate_limit" 中止並推播；仍是 RuntimeError，既有 except 照舊接得到。"""

    def __init__(self, msg: str, cli: str = "") -> None:
        super().__init__(msg)
        self.cli = cli

# 三個 CLI 的位置：env → PATH → 已知安裝位置 → None（見 tools/paths.py）。可攜化 2026-09-08。
PY = paths.resolve_python()
CODEX = paths.resolve_codex() or "codex"
AGY = paths.resolve_agy() or "agy"
AGY_REVIEW = HERE / "tools" / "agy_review.py"

IMPL_RULES = """【守則，違反即整棒作廢】
- 你在隔離 worktree 裡工作；生產目錄與其他任何目錄絕對不要碰。不要 git commit、不要 push、不要啟動任何服務、
  **規格列的驗證指令由 relay 執行、不是你**——你的沙箱裡常沒有可用的 Python 3（裸 python 多是 2.7、專案 Python 3 可能被沙箱擋），硬跑只會浪費大量時間且完全不影響判定；你只要改好碼、做唯讀 diff 稽核、回報，不要自己跑那些驗證指令，也不要跑會載入模型的測試。不要修改 D:\\Tooling\\agent_orchestrator 底下任何檔案。
- 只做規格寫的事，不順手擴張；規格沒寫的檔案不要動。
- 做完用「六欄交接簿」回報：1.完成了什麼 2.改了哪些檔（各幾行） 3.測試結果（指令＋通過數／總數，分「本次新增／既有紅燈／未跑」）
  4.有沒有動到公開介面（函式簽章、路由；有就列簽章） 5.目前卡點 6.建議下一步。
"""

REVIEW_RULES = """你是程式碼審查者，只看 diff 不看實作者的回報。請逐項核對下列條件，每條給 通過／不通過／無法判定 並附 diff 行號當證據；
最後列「未申報問題」（若無請明說「無」）。不要提修法以外的擴張建議。
輸出格式：**第一行必須是**「總判定：可合併」或「總判定：需修改」，再逐條，再「未申報問題」。
**最後一行**再輸出一個單行 JSON（編排器機械讀，前面的文字給人看）：
{"verdict":"approve"或"changes_requested","checks":[{"id":1,"result":"pass|fail|unknown"},...],"unreported":["..."]}
"""

# ---- P2：難易度判準（客觀訊號，不用 Agent 自填的風險等級）-------------------------------
POLICY_DEFAULTS = {"max_files": 2, "max_lines": 60}


def decide_review(task: dict, changed: list[str], diff: str, verify_failed_any: bool, gate: dict) -> tuple[bool, str]:
    """回 (要不要送審, 理由)。policy=always/never/auto；auto 依序：介面破壞→審、碰核心→審、驗證曾失敗→審、
    小改動（檔數≤max_files 且 diff 行數≤max_lines）→跳過只跑測試、其餘→審。介面變更永遠不套用跳過規則。"""
    pol = task.get("review", {}).get("policy", "always")
    if pol == "always":
        return True, "policy=always"
    if pol == "never":
        return False, "policy=never"
    breaking = [f"{k}: {b}" for k, v in gate.items() for b in v.get("breaking", [])]
    if breaking:
        return True, "介面破壞性變更：" + "; ".join(breaking[:3])
    core = task.get("core_paths", [])
    hit = [c for c in changed if any(c == x or c.startswith(x.rstrip("/") + "/") or (x.endswith("/") and c.startswith(x)) for x in core)]
    if hit:
        return True, "碰到核心模組：" + ", ".join(hit[:3])
    if verify_failed_any:
        return True, "本任務曾有驗證失敗"
    lines = sum(1 for l in diff.splitlines() if (l.startswith("+") or l.startswith("-")) and not l.startswith(("+++", "---")))
    lim = {**POLICY_DEFAULTS, **task.get("policy", {})}
    if len(changed) <= lim["max_files"] and lines <= lim["max_lines"]:
        return False, f"小改動（{len(changed)} 檔、{lines} 行）且無介面／核心／驗證失敗訊號 → 跳過審查，只靠測試"
    return True, f"改動量（{len(changed)} 檔、{lines} 行）超過門檻"


def parse_review(text: str) -> dict:
    """優先讀最後一個單行 JSON；沒有就退回看第一行的『可合併／需修改』。"""
    vals, _ = judge.extract_json_values(text)
    for v in reversed(vals):
        if isinstance(v, dict) and v.get("verdict") in ("approve", "changes_requested"):
            return {"structured": True, "approved": v["verdict"] == "approve",
                    "checks": v.get("checks", []), "unreported": v.get("unreported", [])}
    first = next((l for l in text.splitlines() if l.strip()), "")
    return {"structured": False, "approved": ("可合併" in first) and ("需修改" not in first), "checks": [], "unreported": []}


def ledger_append(entry: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    # 2026-10-05（C3）：並行時兩個行程會同時 append；Windows 的 append 是「先 seek 到尾再寫」非原子，會交錯／覆寫
    with runlock.locked(LOCKS / "ledger.lock"):
        with open(LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def ledger_totals() -> dict:
    tot: dict = {}
    if not LEDGER.exists():
        return tot
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        u = e.get("usage") or {}
        t = tot.setdefault(e.get("cli", "?"), {"calls": 0, "input_total": 0, "output": 0, "seconds": 0.0, "rate_limited": 0})
        t["calls"] += 1
        t["input_total"] += int(u.get("input_tokens_total") or 0)
        t["output"] += int(u.get("output_tokens") or 0)
        t["seconds"] += float(e.get("seconds") or 0)
        t["rate_limited"] += 1 if e.get("failure_class") == "rate_limit" else 0
    return tot



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
    cli: str = ""
    failure_class: str | None = None


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
    review_decisions: list = field(default_factory=list)
    iface_gate: dict = field(default_factory=dict)
    started: str = ""
    updated: str = ""
    # 2026-10-05（C1）：--status 要用的欄位；都有預設值，舊 STATE 讀得進來
    title: str = ""
    task_file: str = ""       # 絕對路徑
    max_rounds: int = 0       # 0＝舊 STATE（總表顯示 round/?）
    pid: int = 0              # 只供人看；判活一律看任務鎖（見 tools/runlock.py）
    abort_reason: str = ""
    notified: dict = field(default_factory=dict)  # C2：這一棒發過的推播 {"kind","result","ts"}；沒發過是 {}
    # 2026-10-05（C6）：--resume 要接得上的欄位。以前上一輪的發現只是區域變數、commit 只記一顆，resume 會遺失
    last_feedback: str = ""   # 最後一輪組好、尚未解決的發現（收斂時清空）；resume 時附給實作者當參考
    resumed: list = field(default_factory=list)   # 每次 resume 一筆 {"ts","from_round","first_round","rounds","notes_sha256",…}
    commits: list = field(default_factory=list)   # relay 在這個 branch 上疊過的每一顆 commit；commit 欄＝最新一顆


def state_from_dict(d) -> State:
    """STATE.json 的內容 → State（C6 resume 用）。未知鍵略過、缺的新欄位用預設值（舊 STATE 讀得進來）；
    有給的欄位型別要和預設值同型，否則丟 ValueError——損壞的 STATE 要大聲停下，不可帶著錯的輪次接著跑。
    ponytail：只驗頂層欄位型別，不驗 calls／verify 等清單的內部形狀（那些只給人看與 HANDOFF 用）。"""
    if not isinstance(d, dict):
        raise ValueError("最上層不是 JSON 物件")
    kw = {}
    for name, f in State.__dataclass_fields__.items():
        if name not in d:
            continue
        v = d[name]
        if f.default is not MISSING:
            want = type(f.default)
        elif f.default_factory is not MISSING:
            want = type(f.default_factory())
        else:
            want = str  # task_id
        if not isinstance(v, want) or (want is int and isinstance(v, bool)):
            raise ValueError(f"欄位 {name} 型別不對（應為 {want.__name__}，實為 {type(v).__name__}）")
        kw[name] = copy.deepcopy(v)  # 不和輸入共用清單：main 拿到鎖後還要拿原 dict 比對 STATE 有沒有被改過
    if not kw.get("task_id"):
        raise ValueError("缺 task_id")
    if kw.get("round", 0) < 0:
        raise ValueError("round 為負數")
    return State(**kw)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def sh(cmd: list[str], cwd: str | None = None, timeout: int = 900, env: dict | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ, PYTHONUTF8="1", GIT_TERMINAL_PROMPT="0")
    if env:
        e.update(env)
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, env=e, stdin=subprocess.DEVNULL)


# ---- 即時串流：讓 VS Code 整合終端機看得到三個角色在協力 --------------------------------
_ESC = chr(27)
_COLORS = {"RELAY": "90", "CODEX": "36", "AGY": "35", "JUDGE": "33", "VERIFY": "32", "HUMAN": "93"}
_USE_COLOR = os.environ.get("NO_COLOR") is None


def emit(prefix: str, msg: str, logf=None) -> None:
    tag = f"[{prefix}]"
    shown = f"{_ESC}[{_COLORS.get(prefix, '0')}m{tag}{_ESC}[0m {msg}" if _USE_COLOR else f"{tag} {msg}"
    print(shown, flush=True)
    if logf:
        with open(logf, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {tag} {msg}\n")


def stream(cmd, prefix: str, on_line, cwd=None, timeout=900, env=None, logf=None, shell=False):
    """跑子行程並逐行即時印帶前綴的行（給 VS Code 終端機看），同時完整收集 stdout／stderr 回傳給判定器。
    on_line(rawline)->str|None：回字串就印（已翻成人看得懂），回 None 就吞掉（雜訊）。"""
    e = dict(os.environ, PYTHONUTF8="1", GIT_TERMINAL_PROMPT="0")
    if env:
        e.update(env)
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="replace", env=e, stdin=subprocess.DEVNULL, bufsize=1, shell=shell)
    out, err = [], []
    et = threading.Thread(target=lambda: [err.append(x) for x in iter(p.stderr.readline, "")], daemon=True)
    et.start()
    killed = {"v": False}
    timer = threading.Timer(timeout, lambda: (killed.__setitem__("v", True), p.kill()))
    timer.start()
    try:
        for line in iter(p.stdout.readline, ""):
            out.append(line)
            m = on_line(line.rstrip("\r\n")) if on_line else line.rstrip("\r\n")
            if m:
                emit(prefix, m, logf)
    finally:
        timer.cancel()
    p.wait()
    et.join(timeout=5)
    r = subprocess.CompletedProcess(cmd, p.returncode if not killed["v"] else 124, "".join(out), "".join(err))
    return r


def _codex_line(line: str):
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        e = json.loads(line)
    except json.JSONDecodeError:
        return None
    t = e.get("type")
    if t == "item.completed":
        it = e.get("item") or {}
        k = it.get("type")
        if k == "agent_message":
            tx = (it.get("text") or "").strip().replace("\n", " ")
            return "💬 " + tx[:100] if tx else None
        if k == "command_execution":
            return f"$ [{it.get('status')}] " + (it.get("command") or "").strip().replace("\n", " ")[:88]
        if k == "file_change":
            ch = it.get("changes")
            paths = [c.get("path", "") for c in ch] if isinstance(ch, list) else []
            return "✏ 改檔 " + (", ".join(os.path.basename(x) for x in paths) if paths else str(ch)[:60])
        if k == "error":
            return "⚠ " + str(it.get("message", ""))[:88]
    if t == "turn.completed":
        u = e.get("usage") or {}
        return f"✓ 完成一輪 in={u.get('input_tokens')} out={u.get('output_tokens')}"
    if t == "turn.failed":
        return "✗ turn.failed"
    return None


def _agy_line(line: str):
    s = line.rstrip()
    if not s.strip() or set(s.strip()) <= set("=-"):
        return None
    return s[:200]


def _verify_line(line: str):
    s = line.rstrip()
    if s.strip() and re.search(r"PASS|FAIL|Ran \d+ test|^OK$|Error|Traceback|assert|exit=|差異|零差異|通過", s):
        return s[:160]
    return None


def git(repo: str, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return sh(["git", "-C", repo, *args], timeout=timeout)


def parse_porcelain_z(out: str) -> list[tuple[str, str]]:
    """`git status --porcelain -z` → [(XY, path)]。

    為什麼用 -z：非 -z 會把中文檔名轉成八進位跳脫字串（2026-10-05 實測，路徑因此全壞），
    -z 的路徑原樣輸出、以 NUL 分隔。X 為 R／C 時下一個 NUL 欄位是舊路徑，略過。"""
    fields = out.split("\0")
    res: list[tuple[str, str]] = []
    i = 0
    while i < len(fields):
        f = fields[i]
        i += 1
        if len(f) < 4:  # 結尾空欄位或格式不符
            continue
        xy, path = f[:2], f[3:]
        if xy[0] in "RC" or xy[1] in "RC":
            i += 1  # 跳過舊路徑
        res.append((xy, path))
    return res


# ---- C3（2026-10-05）：不同任務並行——加鎖＋全域並行上限 -------------------------------------
def _path_key(p: str) -> str:
    """路徑 → 12 碼雜湊；normcase＋abspath 讓 Windows 上大小寫／斜線方向不同的同一路徑拿到同一把鎖。"""
    return hashlib.sha1(os.path.normcase(os.path.abspath(p)).encode("utf-8")).hexdigest()[:12]


def task_lock_path(task_id: str) -> Path:
    return LOCKS / f"task-{task_id}.lock"


def worktree_lock_path(worktree: str) -> Path:
    return LOCKS / f"wt-{_path_key(worktree)}.lock"


def repo_lock_path(repo: str) -> Path:
    return LOCKS / f"repo-{_path_key(repo)}.lock"


def valid_task_id(tid) -> bool:
    """task id 直接拼進 runs/<id>/ 與鎖檔名（信任邊界輸入）：以前 `..\\x` 這種 id 會寫到 runs/ 外。
    另擋結尾 `.`（Windows 會吃掉結尾的點：`a.` 與 `a` 落在同一目錄、卻是兩把不同的任務鎖）
    與結尾 `.dry`（和 dry-run 目錄撞名，--status 也會把它當 dry-run 略過）。"""
    return isinstance(tid, str) and TASK_ID_RE.fullmatch(tid) is not None and not tid.endswith((".", ".dry"))


def max_parallel() -> int:
    """RELAY_MAX_PARALLEL → 實際名額，夾在 [1, MAX_PARALLEL_HARD]；預設 1＝一次一棒（決策 D4）。
    值不合法不靜默：印警告後當 1／夾到上限。"""
    raw = os.environ.get("RELAY_MAX_PARALLEL", "1")
    try:
        n = int(raw)
    except ValueError:
        print(f"⚠ RELAY_MAX_PARALLEL={raw!r} 不是整數，當成 1", file=sys.stderr)
        return 1
    clamped = min(max(n, 1), MAX_PARALLEL_HARD)
    if clamped != n:
        print(f"⚠ RELAY_MAX_PARALLEL={n} 超出 1～{MAX_PARALLEL_HARD}，當成 {clamped}", file=sys.stderr)
    return clamped


def _take_slot(n: int) -> runlock.Held | None:
    for i in range(n):
        h = runlock.try_hold(LOCKS / f"slot-{i}.lock")
        if h is not None:
            return h
    return None


def acquire_run_locks(task: dict, *, queue: bool, take_slot: bool = True, on_wait=None) -> ExitStack:
    """取一棒要整棒持有的鎖，回 ExitStack（with 結束或例外時全部釋放；行程被硬殺則由 OS 釋放）。

    順序固定 任務 → worktree → 名額：所有行程同序取鎖，不會死結。任務鎖同時是 --status 判活的依據。
    名額全滿：沒加 queue → LockBusy；有 queue → 先呼叫 on_wait()（main 用來把 STATE 記成 queued），
    之後每 QUEUE_POLL_SECONDS 秒重試，不設逾時（人可 Ctrl-C）。
    ponytail：排隊不保證先來先跑（誰先輪詢到誰拿）；名額上限 3、人工啟動的量下不值得做票號。
    升級路徑：runs/.locks/ 下依時間戳排序的票號檔，只有排第一的才去搶名額。"""
    stack = ExitStack()
    try:
        h = runlock.wait_hold(task_lock_path(task["id"]), TASK_LOCK_GRACE)
        if h is None:
            raise runlock.LockBusy(f"任務 {task['id']} 正在跑或排隊（relay.py --status）")
        stack.callback(h.release)
        h = runlock.try_hold(worktree_lock_path(task["worktree"]))
        if h is None:
            raise runlock.LockBusy(f"worktree {task['worktree']} 正被另一個任務使用")
        stack.callback(h.release)
        if take_slot:
            n, waited, last_note = max_parallel(), False, None
            while True:
                h = _take_slot(n)
                if h is not None:
                    stack.callback(h.release)
                    if waited:
                        emit("RELAY", f"拿到並行名額（{h.path.name}），開跑")
                    break
                if not queue:
                    raise runlock.LockBusy(f"並行上限 {n} 已滿（RELAY_MAX_PARALLEL）；要排隊加 --queue")
                if not waited:
                    waited = True
                    if on_wait:
                        on_wait()
                if last_note is None or time.monotonic() - last_note >= 600:
                    emit("RELAY", f"等待並行名額…（上限 {n}＝RELAY_MAX_PARALLEL；每 {QUEUE_POLL_SECONDS} 秒重試，Ctrl-C 放棄）")
                    last_note = time.monotonic()
                time.sleep(QUEUE_POLL_SECONDS)
    except BaseException:
        stack.close()
        raise
    return stack


def _read_state(path: Path) -> dict | None:
    """讀一份 STATE.json；不存在、讀不到、不是 JSON 物件都回 None。"""
    try:
        s = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return s if isinstance(s, dict) else None


class Run:
    def __init__(self, task: dict, dry: bool, *, task_file: str = "", no_notify: bool = False,
                 resume_state: State | None = None):
        self.t = task
        self.dry = dry
        self.no_notify = no_notify  # 人坐在終端機前跑時用（--no-notify）
        self._notified = False      # 一棒最多一則：任何出口發過就不再發
        # 2026-10-05（C1）：dry-run 寫到 runs/<id>.dry/，不再覆蓋已跑過任務的真實紀錄（--status 略過 *.dry）
        self.dir = HERE / "runs" / (task["id"] + (".dry" if dry else ""))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.prev_state = _read_state(self.dir / "STATE.json")  # 上一次留下的 STATE：認出「前次沒收尾」用
        if resume_state is not None:
            # C6（2026-10-05）：resume 沿用前次 STATE（保留 started、輪次、calls…），不新建——新建會在第一次 save 蓋掉舊紀錄
            self.state = resume_state
            self.state.pid = os.getpid()
            self.state.title = task.get("title", self.state.title)
            self.state.task_file = task_file or self.state.task_file
        else:
            self.state = State(task_id=task["id"], started=now(), title=task.get("title", ""), task_file=task_file,
                               max_rounds=int(task.get("max_rounds", 2)), pid=os.getpid())
        self.resuming = False   # start_resume() 之後為 True
        self.review_extra = ""  # resume 時附在審查核對條件後面的人工追加要求
        self.wt = task["worktree"]
        self.repo = task["repo"]

    # ---- 狀態檔 ------------------------------------------------------------
    def save(self, phase: str | None = None) -> None:
        if phase:
            self.state.phase = phase
        self.state.updated = now()
        (self.dir / "STATE.json").write_text(json.dumps(asdict(self.state), ensure_ascii=False, indent=1), encoding="utf-8")

    def write_current(self, text: str) -> None:
        if self.resuming:  # C6：接手的人第一眼就知道這不是原始那一棒
            text = text.replace("# CURRENT\n", f"# CURRENT（人工意見回灌，第 {len(self.state.resumed)} 次 resume）\n", 1)
        (self.dir / "CURRENT.md").write_text(text, encoding="utf-8")

    def record(self, rec: CallRecord) -> None:
        self.state.calls.append(asdict(rec)); self.save()
        ledger_append({"ts": now(), "task": self.t["id"], "role": rec.role, "cli": rec.cli, "round": rec.round,
                       "ok": rec.ok, "failure_class": rec.failure_class, "seconds": rec.seconds, "usage": rec.usage})
        if rec.failure_class == "rate_limit":
            # P4 只偵測不換手：真正的 429 至今未直接觀察到，先讓它大聲停下來
            raise RateLimitStop(f"撞牆：{rec.cli} 回 rate_limit（{rec.reason}）。本版不自動換手，請人決定換誰。", cli=rec.cli)

    def log(self, msg: str, prefix: str = "RELAY") -> None:
        emit(prefix, msg, self.dir / "relay.log")

    def abort(self, reason: str, notify: bool = True, kind: str = "aborted", **info) -> None:
        """例外中止也要落檔（2026-10-05，C1）：以前 RuntimeError 由 main() 接住後不寫 STATE，階段停在中止前
        那格（例如 review），--status 看起來像還在跑。C2：notify=True 時 STATE／CURRENT 都寫完才推播；
        KeyboardInterrupt（人在場）呼叫端傳 notify=False。kind 讓撞牆走自己的文案。"""
        self.state.abort_reason = reason[:500]
        self.save("aborted")
        self.write_current(f"# CURRENT\n\n任務 {self.t['id']} 中止：{reason[:500]}；看 relay.log。\n")
        self.log(f"任務中止：{reason[:500]}")
        if notify:
            self.notify(kind, reason=reason, **info)

    def notify(self, kind: str, **info) -> None:
        """C2（2026-10-05）：在「需要人」的出口推播，且一棒最多一則。dry-run／--no-notify／已發過都直接略過。
        推播的任何失敗（指令掛了、逾時、帳本鎖、設定壞）都只記 log，永遠不改變這一棒的結果與 exit code。"""
        if self.dry or self.no_notify or self._notified:
            return
        self._notified = True
        try:
            started = _parse_ts(_segment_start(asdict(self.state)))  # C6：resume 的棒只算這一次接續的耗時
            minutes =int((datetime.now(timezone.utc) - started).total_seconds() // 60) if started else None
            info = {"round": self.state.round, "max_rounds": self.state.max_rounds, "commit": self.state.commit,
                    "branch": self.state.branch, "minutes": minutes, **info}
            result = notify.notify(kind, self.t["id"], info, config_path=notify_config_path(), ledger_path=NOTIFY_LEDGER,
                                   lock_path=LOCKS / "notify.lock")
        except Exception as e:  # ponytail: 連 notify 自己的 bug 也吞掉；代價是推播可能靜默沒發，relay.log 會留這行
            result = f"推播例外：{type(e).__name__}"
        if result == "推播關閉":
            return
        self.log(result)
        self.state.notified = {"kind": kind, "result": result, "ts": now()}
        self.save()
        if result.startswith(("推播未發", "推播指令失敗")):  # 人回來看 CURRENT.md 就知道為什麼沒收到
            try:
                with open(self.dir / "CURRENT.md", "a", encoding="utf-8") as f:
                    f.write(f"\n{result}\n")
            except OSError:
                pass

    def note_interrupted_previous(self) -> None:
        """拿到任務鎖之後呼叫（2026-10-05，C3）：上一次的 STATE 停在非終態＝那個行程沒收尾就不在了
        （被硬殺、關機…）。OS 已釋放它的鎖、這裡拿得到就是證明；只負責把「認出殘留」記下來，本次照常從頭跑
        （C6 resume 則是從那份 STATE 接續）。"""
        p = self.prev_state
        if p and p.get("phase") not in TERMINAL_PHASES:
            self.log(f"前一次行程（pid {p.get('pid') or '?'}）停在「{p.get('phase')}」（{p.get('updated') or '?'}）沒有收尾；"
                     "它的任務鎖已無人持有＝行程已不在，" + ("本次從它的 STATE 接續（resume）" if self.resuming else "本次從頭重跑"))

    def start_resume(self, notes: str, rounds: int) -> dict:
        """C6（2026-10-05）：拿到鎖、確認 STATE 沒被別人動過之後呼叫；回傳給 run() 的參數。
        先保存紀錄再改狀態：HANDOFF.md 複製成 HANDOFF.r{round}.md（已存在就不蓋）；human_notes.md 改名成
        human_notes.r{first}.md——之後的編輯不會被這次誤用，下次 resume 也不會重用舊意見。dry-run 兩者都不做。"""
        st = self.state
        first = st.round + 1
        if not self.dry:
            h, h_old = self.dir / "HANDOFF.md", self.dir / f"HANDOFF.r{st.round}.md"
            if h.is_file() and not h_old.exists():
                shutil.copy2(h, h_old)
            (self.dir / "human_notes.md").rename(self.dir / f"human_notes.r{first}.md")  # 目標已存在時 main 先擋下
        if st.commit and not st.commits:  # C6 之前的 STATE 只有 commit 一欄
            st.commits = [st.commit]
        st.resumed.append({"ts": now(), "from_round": st.round, "first_round": first, "rounds": rounds,
                           "prev_phase": st.phase, "notes_sha256": hashlib.sha256(notes.encode("utf-8")).hexdigest(),
                           "notes_chars": len(notes)})
        st.abort_reason, st.verdict = "", ""
        st.max_rounds = first + rounds - 1  # --status 與推播的「N/M 輪」：M＝這次最多跑到第幾輪
        self.resuming = True
        # 審查者也要拿到人工意見：否則人要求的改動會被依舊條件判「不通過」而無限退回
        self.review_extra = "\n\n【人工追加要求（優先於上列條件；衝突時以此為準）】\n" + notes
        fb = "【人工審查意見（最高優先；與規格或先前審查意見衝突時以此為準）】\n" + notes
        if st.last_feedback:
            fb += "\n\n【上一輪未解決的發現（參考）】\n" + st.last_feedback
        self.save()
        return {"start_round": first, "rounds": rounds, "initial_feedback": fb, "resume": True}

    # ---- 1. worktree ---------------------------------------------------------
    def prepare(self, resume: bool = False) -> None:
        t = self.t
        if resume:
            # C6（2026-10-05）：worktree 已由前次備妥（main 已驗過存在、且在任務分支上）；不建 worktree、不跑 prebuild。
            # 需要重建產物時人自己跑，或開新棒。base_commit 沿用前次（審查累積 diff 的基準），前次沒記到才補。
            if not self.state.base_commit and not self.dry:
                self.state.base_commit = git(self.repo, "rev-parse", "--short", t["base_branch"]).stdout.strip()
            self.state.branch, self.state.worktree = t["branch"], self.wt
            self.log(f"{'[dry] ' if self.dry else ''}resume：沿用 worktree {self.wt}（{t['branch']}），不重建、不跑 prebuild")
            return
        if self.dry:
            self.log(f"[dry] worktree {self.wt} ← {t['base_branch']} 新分支 {t['branch']}")
            return
        base = git(self.repo, "rev-parse", "--short", t["base_branch"]).stdout.strip()
        self.state.base_commit = base
        if not Path(self.wt).exists():
            # -c core.autocrlf=false：checkout 成 LF、與 git blob 一致。在 core.autocrlf=true
            # 的機器上，worktree 會被轉成 CRLF，撞到以 LF 內容釘死的 SHA 類斷言（假紅）。
            # 2026-10-05（C3）：在 repo 鎖內做——同 repo 兩棒同時 worktree add 會撞 git 自己的 ref／index lock，
            # 下面的 fallback 又會把那種錯誤誤當成「分支已存在」再試一次，最後以誤導的訊息中止。
            with runlock.locked(repo_lock_path(self.repo), timeout=REPO_LOCK_TIMEOUT):
                r = git(self.repo, "-c", "core.autocrlf=false", "worktree", "add", "-b", t["branch"], self.wt, t["base_branch"], timeout=300)
                if r.returncode != 0:
                    # 分支可能已存在：直接掛上
                    r = git(self.repo, "-c", "core.autocrlf=false", "worktree", "add", self.wt, t["branch"], timeout=300)
            if r.returncode != 0:
                raise RuntimeError("worktree add 失敗：" + r.stderr[-400:])
            self.log(f"worktree 建立 {self.wt}（{t['branch']} ← {t['base_branch']}@{base}）")
        else:
            self.log(f"worktree 已存在，沿用 {self.wt}")
        self.state.branch, self.state.worktree = t["branch"], self.wt
        for cmd in t.get("prebuild", []):
            self.log(f"prebuild: {cmd}")
            cmd_py = cmd.replace("python ", f'"{self.t.get("python") or PY}" ', 1) if cmd.startswith("python ") else cmd
            r = subprocess.run(cmd_py, cwd=self.wt, shell=True, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", env=dict(os.environ, PYTHONUTF8="1"), timeout=900)
            if r.returncode != 0:
                raise RuntimeError(f"prebuild 失敗（{cmd}）：{r.stderr[-300:]}")
        self.save("prepared")

    # ---- 2. 實作者 -----------------------------------------------------------
    def impl_prompt(self, rnd: int, feedback: str) -> str:
        """組實作者 prompt 並落檔 impl_r{rnd}_prompt.md（2026-10-05 從 implement 抽出：測試替身與真實作共用同一份組法）。"""
        spec = Path(self.t["spec_file"]).read_text(encoding="utf-8")
        prompt = IMPL_RULES + "\n【規格】\n" + spec
        if feedback:
            # C6：resume 第一輪的 feedback 自帶「【人工審查意見…】」標題，不再套「上一輪審查」的帽子
            head = "" if feedback.startswith("【人工審查意見") else "【上一輪審查／驗證的發現，請逐條修正後再回報】\n"
            prompt += "\n\n" + head + feedback
        (self.dir / f"impl_r{rnd}_prompt.md").write_text(prompt, encoding="utf-8")
        if len(prompt) > 30000:
            # C6：prompt 目前走 argv（Windows 命令列上限 32,767 字元）；人工意見很長時先大聲說，失敗了才知道為什麼
            self.log(f"⚠ 實作者 prompt {len(prompt)} 字元，超過 30,000：命令列可能放不下（上限約 32K），請精簡 human_notes 或規格")
        return prompt

    def implement(self, rnd: int, feedback: str) -> tuple[bool, str, dict | None]:
        prompt = self.impl_prompt(rnd, feedback)
        out_last = self.dir / f"impl_r{rnd}_last_message.md"
        so, se, ex = self.dir / f"impl_r{rnd}.stdout.txt", self.dir / f"impl_r{rnd}.stderr.txt", self.dir / f"impl_r{rnd}.exit.txt"
        if self.dry:
            self.log(f"[dry] codex exec（round {rnd}，prompt {len(prompt)} 字）")
            return True, "(dry)", None
        self.log(f"實作者 Codex 開跑（round {rnd}）…")
        t0 = time.monotonic()
        cp = stream([CODEX, "exec", "--json", "-s", "workspace-write", "-C", self.wt, "-o", str(out_last), prompt],
                    "CODEX", _codex_line, cwd=self.wt, timeout=self.t.get("impl_timeout", 1500), logf=self.dir / "relay.log")
        so.write_text(cp.stdout, encoding="utf-8"); se.write_text(cp.stderr, encoding="utf-8"); ex.write_text(f"exit={cp.returncode}", encoding="utf-8")
        v = judge.judge("codex", cp.stdout, cp.stderr, cp.returncode)
        rec = CallRecord("implementer", rnd, v.ok, v.reason, cp.returncode, round(time.monotonic() - t0, 1), v.usage, str(so),
                         cli="codex", failure_class=v.failure_class)
        self.record(rec)
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
            cmd_py = cmd.replace("python ", f'"{self.t.get("python") or PY}" ', 1) if cmd.startswith("python ") else cmd
            t0 = time.monotonic()
            cp = stream(cmd_py, "VERIFY", _verify_line, cwd=self.wt, shell=True,
                        env=item.get("env", {}), timeout=item.get("timeout", VERIFY_TIMEOUT_DEFAULT), logf=self.dir / "relay.log")
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

    # ---- 3b. 陰性對照：verify 綠之後，證明新斷言真的抓得到違規（不是只存在）--------------
    def negative_control(self, rnd: int) -> tuple[bool, str]:
        """對每條 negative_controls 注入一個真違規、重跑對應 verify、確認它紅在指定 marker、還原。
        任何一條「注入後沒紅在指定處」= 那條斷言是空的 ⇒ 整棒判 fail（機器審從『斷言在不在』
        升級到『斷言真的抓得到』）。設定見 README。"""
        controls = self.t.get("negative_controls", [])
        if not controls:
            return True, "（無 negative_controls）"
        verify_by_name = {v["name"]: v for v in self.t.get("verify", [])}
        py_exe = self.t.get("python") or PY
        lines, all_ok = [], True
        for nc in controls:
            tgt = nc["target"]
            vname = nc.get("verify_name") or (self.t.get("verify") or [{}])[0].get("name")
            vitem = verify_by_name.get(vname)
            if vitem is None:
                all_ok = False
                lines.append(f"- {tgt}: 找不到 verify 名稱 {vname!r}")
                continue
            if self.dry:
                self.log(f"[dry] 陰性對照 {tgt}: 注入 → 期望 verify {vname} 紅在 {nc['expected_failure']!r}", prefix="NEGCTL")
                continue
            cmd = vitem["cmd"]
            cmd_py = cmd.replace("python ", f'"{py_exe}" ', 1) if cmd.startswith("python ") else cmd
            try:
                # verify 是 shell 字串（跟 verify() 一樣用 shell 跑），inject_check 以 shell=True 執行
                inject_check.run_injection_check(
                    str(Path(self.wt) / tgt), nc["old"], nc["new"], cmd_py, nc["expected_failure"],
                    cwd=self.wt, timeout=vitem.get("timeout", VERIFY_TIMEOUT_DEFAULT), shell=True,
                    report=lambda m: self.log(m, prefix="NEGCTL"),
                )
                lines.append(f"- {tgt}: 注入後正確紅在 {nc['expected_failure']!r} ✓")
            except inject_check.InjectionCheckError as exc:
                all_ok = False
                lines.append(f"- {tgt}: 陰性對照未過 [{exc.step}] {exc}")
                self.log(f"陰性對照 FAIL [{exc.step}]: {exc}", prefix="NEGCTL")
        return all_ok, "\n".join(lines)

    # ---- 4. 審查 -------------------------------------------------------------
    def diff_for_review(self, base: str = "HEAD") -> str:
        """base 預設 HEAD；C6（2026-10-05）resume 已 commit 過的棒時傳任務的分岔點，審查者才看得到整個任務的累積改動。"""
        # 未追蹤但不被 ignore 的新檔用 intent-to-add 納入 diff（不改 index 內容）
        git(self.wt, "add", "--intent-to-add", "--all")
        # 🔴 2026-09-23 修（gw-layout-p2b round 1 假退回）：原本是 `git diff`（index vs 工作樹），
        #    搬檔任務會整批失真——(a) `add --all` 把未暫存的刪除收進 index，`git diff` 從此看不到
        #    那些刪除，審查者只看到 N 個 new file、判成「複製不是搬移」；(b) 實作者若用 git mv 暫存好，
        #    index==工作樹，`git diff` 對那些檔一片空白。兩種都是同一個原因：比對基準錯了。
        #    改成對 HEAD 比並開 -M（rename 偵測），暫存與否都呈現 R 列；驗證＝2026-09-23 臨時 repo
        #    兩情境實測（deleted=0/rename=0 → deleted=1/rename=1）。（原指 memory 9/23 條，該條 2026-10-05
        #    整理時已刪，以本註解為準。）
        d = git(self.wt, "diff", "-M", base, "--", ".", ":(exclude)_refactor/*").stdout
        return d

    def review_base(self) -> str:
        """C6：已 commit 過的棒 resume 時，審查 diff 的基準＝state.base_commit 與 HEAD 的 merge-base。
        通常就是 base_commit；分支是沿用既有的（prepare 的「已存在就掛上」）時 base_commit 可能比分岔點新，
        直接比會把 base 那邊的新 commit 顯示成反向改動。merge-base 失敗才退回 base_commit。"""
        r = git(self.wt, "merge-base", self.state.base_commit, "HEAD")
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else self.state.base_commit

    def review_inputs(self, rnd: int, diff: str) -> tuple[Path, Path, Path]:
        """寫審查指令與 diff 檔，回 (instr, diff, out) 路徑（2026-10-05 從 review 抽出：測試替身與真審查共用）。
        C6：resume 時核對條件後面追加人工要求（self.review_extra）。"""
        instr = (REVIEW_RULES + "\n【核對條件】\n" + Path(self.t["review"]["instructions_file"]).read_text(encoding="utf-8")
                 + self.review_extra)
        instr_f, diff_f, out_f = self.dir / f"review_r{rnd}_instr.txt", self.dir / f"review_r{rnd}_diff.txt", self.dir / f"review_r{rnd}_agy.json"
        instr_f.write_text(instr, encoding="utf-8"); diff_f.write_text(diff, encoding="utf-8")
        return instr_f, diff_f, out_f

    def review(self, rnd: int, diff: str) -> tuple[bool, str, dict | None]:
        instr_f, diff_f, out_f = self.review_inputs(rnd, diff)
        if self.dry:
            self.log(f"[dry] agy review（round {rnd}，diff {len(diff)} 字）")
            return True, "(dry)", None, True
        self.log(f"審查者 agy 開跑（round {rnd}，diff {len(diff.encode('utf-8'))} bytes）…")
        t0 = time.monotonic()
        cp = stream([PY, str(AGY_REVIEW), "--instructions", str(instr_f), "--diff", str(diff_f), "--out", str(out_f)],
                    "AGY", _agy_line, cwd=str(HERE), timeout=self.t.get("review_timeout", 1500),
                    env={"AGY_EXE": AGY}, logf=self.dir / "relay.log")
        (self.dir / f"review_r{rnd}_tool.log").write_text(cp.stdout + "\n--- stderr ---\n" + cp.stderr, encoding="utf-8")
        obj = json.loads(out_f.read_text(encoding="utf-8")) if out_f.exists() and out_f.stat().st_size else None
        v = judge.judge("agy", json.dumps(obj) if obj else "", cp.stderr, cp.returncode)
        rec = CallRecord("reviewer", rnd, v.ok, v.reason, cp.returncode, round(time.monotonic() - t0, 1), v.usage, str(out_f),
                         cli="agy", failure_class=v.failure_class)
        self.record(rec)
        text = (obj or {}).get("response", "") or ""
        pr = parse_review(text)
        self.state.review_decisions.append({"round": rnd, **{k: v2 for k, v2 in pr.items() if k != "checks"},
                                            "checks": pr["checks"]}); self.save()
        if not v.ok:
            # 2026-09-14：審查「工具」故障（沒有 JSON／子行程崩潰，例如 diff 單行超過 Windows 命令列上限）
            # 不是審查「退回」。以前兩者混在一起＝把空的修正要求送給實作者空轉一輪。呼叫端看第四個回傳值。
            self.log(f"審查工具故障（{v.failure_class or 'unknown'}：{v.reason}）——不是審查退回，本輪不送實作者", prefix="JUDGE")
        self.log(f"審查者結束：{'OK' if v.ok else 'FAIL'}（{rec.seconds}s）判定："
                 f"{'工具故障' if not v.ok else ('approve' if pr['approved'] else 'changes_requested')}"
                 f"{'（結構化）' if pr['structured'] else '（退回字串判斷）'} 未申報 {len(pr['unreported'])} 項")
        return v.ok and pr["approved"], text, v.usage, v.ok

    # ---- 6. commit -----------------------------------------------------------
    def worktree_changes(self) -> list[str]:
        """worktree 裡「真的有改」的路徑清單（commit 清單與送審檔數共用）。

        2026-10-05：autocrlf 下只差行尾的檔在 `status` 標 ` M`，舊版因此當成規格外改動而中止 commit
        （verify 全過、審查通過卻 commit 失敗）。改成對 ` M` 檔再比一次內容（`--ignore-cr-at-eol`），
        純行尾差異略過；這種檔 `git add` 後 diff 為空，排除不會丟東西。
        ponytail：每個 ` M` 檔一次子行程，O(n)，n 是改動檔數（實務 <20）；
        升級路徑＝一次 `git diff --name-only -z --ignore-cr-at-eol` 取交集（舊版 git 行為未驗）。"""
        git(self.wt, "reset", "-q")  # 清掉 intent-to-add
        ignore_new = self.t.get("ignore_new", IGNORE_NEW_DEFAULT)
        paths, eol_only = [], []
        for xy, path in parse_porcelain_z(git(self.wt, "status", "--porcelain", "-z").stdout):
            if xy == "??" and any(path.startswith(x) for x in ignore_new):
                continue
            if xy == " M":
                # exit 0＝只差行尾；1＝真改動；其他（git 錯誤）一律保守當成改動，寧可多擋一次讓人看
                if git(self.wt, "diff", "--quiet", "--ignore-cr-at-eol", "--", path).returncode == 0:
                    eol_only.append(path)
                    continue
            paths.append(path)
        if eol_only:
            self.log(f"略過只差行尾的檔（不 commit）：{eol_only}")
        return paths

    def changed_paths(self) -> list[str]:
        paths = self.worktree_changes()
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
        msg_f = self.dir / "commit_msg.txt"
        msg_f.write_text(message, encoding="utf-8")
        # 2026-10-05（C3）：add／commit 在 repo 鎖內（同 repo 別棒的 worktree add／commit 會撞 git 的 lock 檔）
        with runlock.locked(repo_lock_path(self.repo), timeout=REPO_LOCK_TIMEOUT):
            r = git(self.wt, "add", "--", *paths)
            if r.returncode != 0:
                raise RuntimeError("git add 失敗：" + r.stderr[-300:])
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
        resumed = ""
        if self.state.resumed:  # C6：上一版 HANDOFF 已存成 HANDOFF.r{N}.md，這裡說清楚這份是第幾次接續
            r0 = self.state.resumed[-1]
            resumed = (f"人工意見回灌第 {len(self.state.resumed)} 次：本次從第 {r0.get('first_round')} 輪起"
                       f"（意見原文 `runs/{self.t['id']}/human_notes.r{r0.get('first_round')}.md`）；"
                       f"relay 在此分支的 commit：{', '.join(self.state.commits) or '（無）'}。\n")
        next_step = ("人：審過 HANDOFF 後合併到 base branch、依 repo 紀律部署；編排器不合併不重啟。" if status == "done" else
                     "人：讀下方審查／驗證輸出決定修法或放棄；worktree 與分支保留。要補一句意見再跑：寫 "
                     f"`runs/{self.t['id']}/human_notes.md` 後 `relay.py --resume {self.t['id']}`（輪次接續、不重建 worktree）。")
        text = f"""# HANDOFF — {self.t['id']}（{status}，{now()}）

## 1. 完成了什麼
{'（未收斂，見卡點）' if status != 'done' else self.t.get('title', self.t['id'])}
分支 `{self.state.branch}` @ `{self.state.commit or '未 commit'}`，worktree `{self.wt}`，base `{self.state.base_commit}`。
{resumed}
## 2. 改了哪些檔
{chr(10).join('- ' + p for p in paths) if paths else '- （無）'}

## 3. 測試結果（最後一輪）
> 🔑 **本節是 relay 自己實跑的結果，是唯一權威。** 下方「實作者最後一輪回報（原文）」的測試自述
> 僅供參考——實作者的無頭環境常跑不動測試（例如 Windows 上裸 `python` 解析到 WindowsApps stub），
> 它回報「全部失敗」而本節全 PASS 是**已知且正常**的情況。**兩者不一致時一律以本節為準。**

{verify_summary or '- （未跑）'}

## 4. 有沒有動到公開介面
（見實作者回報第 4 欄；審查者核對條件含簽章檢查）

## 5. 目前卡點
{blocker or '無'}

## 6. 建議下一步
{next_step}

## 審查判準與簽章閘門
{chr(10).join('- round ' + str(d.get('round')) + '：' + ('送審' if d.get('need_review') else '跳過') + '——' + str(d.get('why')) for d in self.state.review_decisions if 'need_review' in d) or '- （無）'}
- 簽章閘門：breaking {sum(len(g['breaking']) for g in self.state.iface_gate.values())}、additive {sum(len(g['additive']) for g in self.state.iface_gate.values())}
{chr(10).join('  - ' + k + ': ' + '; '.join(v['breaking'] + v['additive']) for k, v in self.state.iface_gate.items() if v['breaking'] or v['additive']) or ''}
- 審查者結構化判定：{[d.get('approved') for d in self.state.review_decisions if 'approved' in d] or '（未送審）'}；未申報問題：{[d.get('unreported') for d in self.state.review_decisions if 'approved' in d] or '—'}

## 用量（每次呼叫，判定器抽取）
{chr(10).join(usage_lines)}
- 帳本累計（所有任務，runs/usage_ledger.jsonl）：{json.dumps(ledger_totals(), ensure_ascii=False)}

---
### 實作者最後一輪回報（原文）
{impl_report}

---
### 審查者最後一輪（原文）
{review_text}
"""
        (self.dir / "HANDOFF.md").write_text(text, encoding="utf-8")

    # ---- 主流程 --------------------------------------------------------------
    def run(self, start_round: int = 1, rounds: int | None = None, initial_feedback: str = "", resume: bool = False) -> int:
        """跑一棒。C6（2026-10-05）一般化：resume 時從 start_round 接續編號（r3、r4…，不覆蓋舊紀錄），
        第一輪的 feedback＝人工意見（＋上一輪未解決的發現），不建 worktree、不跑 prebuild。"""
        self.log(f"任務 {self.t['id']}：{self.t.get('title', '')}")
        rounds = int(self.t.get("max_rounds", 2)) if rounds is None else rounds
        last = start_round + rounds - 1
        if resume:
            self.log(f"人工意見回灌（第 {len(self.state.resumed)} 次 resume）：從第 {start_round} 輪接續，最多到第 {last} 輪")
        self.prepare(resume=resume)
        # D11：已 commit 過的棒 resume → 在同一 branch 疊新 commit；審查看整個任務的累積 diff，commit 只收增量
        review_base = self.review_base() if (resume and self.state.commit and not self.dry) else ""
        feedback, impl_report, verify_summary, review_text, paths = initial_feedback, "", "", "", []
        for rnd in range(start_round, last + 1):
            self.state.round = rnd
            self.write_current(f"# CURRENT\n\n任務 {self.t['id']} round {rnd}/{last}：實作中。worktree `{self.wt}`。\n")
            self.save("implement")
            ok, impl_report, _ = self.implement(rnd, feedback)
            if not ok and not self.dry:
                feedback = "實作者的 CLI 呼叫沒有正常結束（判定器：" + self.state.calls[-1]["reason"] + "）。請重做規格。"
                self.state.last_feedback = feedback
                self.log("實作者呼叫失敗，下一輪重試")
                continue
            self.save("verify")
            v_ok, verify_summary = self.verify(rnd)
            self.save("review")
            diff = self.diff_for_review() if not self.dry else "(dry)"
            if not self.dry and not diff.strip():
                feedback = ("worktree 相對上一顆 commit 沒有任何新改動。請照人工審查意見實際修改檔案。" if review_base
                            else "worktree 沒有任何改動。請照規格實際修改檔案。")
                self.state.last_feedback = feedback
                self.log("沒有 diff，下一輪")
                continue
            if review_base:
                diff = self.diff_for_review(review_base)
            # P2：簽章閘門 + 難易度判準
            git(self.wt, "reset", "-q")
            changed_now = self.worktree_changes()
            gate = iface_gate.gate_worktree(self.wt, self.t["base_branch"])
            self.state.iface_gate = gate
            verify_failed_any = any(r["exit"] != 0 for r in self.state.verify)
            need_review, why = decide_review(self.t, changed_now, diff, verify_failed_any, gate)
            self.state.review_decisions.append({"round": rnd, "need_review": need_review, "why": why,
                                                "changed": changed_now, "breaking": sum(len(g["breaking"]) for g in gate.values())})
            self.save()
            self.log(f"審查判準：{'送審' if need_review else '跳過審查'}——{why}", prefix="JUDGE")
            nc_ok, nc_summary = self.negative_control(rnd) if v_ok else (True, "")
            if need_review:
                r_ok, review_text, _, tool_ok = self.review(rnd, diff)
            else:
                r_ok, review_text, tool_ok = True, f"（依判準跳過審查：{why}）", True
            fb = []
            if not v_ok:
                fb.append("【驗證未過】\n" + verify_summary)
            if not nc_ok:
                fb.append("【陰性對照未過：新斷言沒抓到被注入的違規】\n" + nc_summary)
            if tool_ok and not r_ok:  # 工具故障時的「退回」不是審查意見，不餵給實作者
                fb.append("【審查判定需修改，原文】\n" + review_text)
            feedback = "\n\n".join(fb)
            self.state.last_feedback = feedback  # C6：落檔，resume 時當「上一輪未解決的發現」；收斂時為空
            self.save()
            if not tool_ok:
                # 審查工具故障：停下來給人，不開下一輪（實作沒問題時再跑一輪只是燒錢）
                self.state.verdict = "review_tool_failure"
                self.log("審查工具故障，停止（不開下一輪）；修好 tools/agy_review.py 後重跑，或人工審查 review_r*_diff.txt")
                break
            if v_ok and nc_ok and r_ok:
                self.state.verdict = "converged"
                break
            self.log(f"round {rnd} 未收斂（verify={'ok' if v_ok else 'fail'} negctl={'ok' if nc_ok else 'fail'} review={'ok' if r_ok else 'fail'}）")
        else:
            self.state.verdict = "escalate"

        if self.dry:
            self.log("[dry] 結束")
            return 0
        if self.state.verdict == "converged":
            paths = self.changed_paths()  # 一律對 HEAD：resume 已 commit 過的棒時只收增量
            again = f"；人工意見回灌第 {len(self.state.resumed)} 次" if self.resuming else ""
            msg = f"{self.t.get('title', self.t['id'])}\n\n（編排器 relay.py：Codex 實作、agy 審查、驗證指令全過；task {self.t['id']}{again}）\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n"
            self.state.commit = self.commit(paths, msg)
            self.state.commits.append(self.state.commit)  # 不改寫既有 commit：同一 branch 上疊新的一顆
            self.save("done")
            self.handoff("done", impl_report, verify_summary, review_text, paths, "")
            self.write_current(f"# CURRENT\n\n任務 {self.t['id']} 已收斂並 commit `{self.state.commit}` 於 `{self.state.branch}`。\n下一步是人：審 HANDOFF.md → 合併 → 部署。編排器到此為止。\n")
            self.log(f"收斂：commit {self.state.commit}（{len(paths)} 個檔）")
            self.notify("ready_to_merge")
            return 0
        self.save("escalate")
        try:
            paths = self.changed_paths()
        except RuntimeError as e:
            paths = [f"（{e}）"]
        if self.state.verdict == "review_tool_failure":
            blocker = (f"審查工具故障（round {self.state.round}：agy_review 沒有回傳 JSON／子行程崩潰）。實作與驗證結果見上；"
                       "修好 tools/agy_review.py 後重跑 relay，或人工審 review_r*_diff.txt，或寫 human_notes.md（例如「請照原樣，"
                       "只需重新審查」）後 `relay.py --resume` 接續；worktree 改動保留、未 commit。")
        else:
            blocker = f"{self.state.round} 輪未收斂（驗證或審查不過），已停止；worktree 保留供人接手。"
        self.handoff("escalate", impl_report, verify_summary, review_text, paths, blocker)
        self.write_current(f"# CURRENT\n\n任務 {self.t['id']} **未收斂**，已升給人。看 HANDOFF.md。\n")
        self.log("未收斂，升給人")
        if self.state.verdict == "review_tool_failure":
            fc = next((c.get("failure_class") for c in reversed(self.state.calls) if c.get("role") == "reviewer"), None)
            self.notify("review_tool_failure", failure_class=fc)
        else:
            self.notify("escalate")
        return 2


# ---- C1（2026-10-05）：relay.py --status 狀態總表 ------------------------------------------------
def collect_states(runs_dir: Path) -> list[dict]:
    """掃 runs_dir/*/STATE.json（不遞迴；略過 *.dry 與 "_" 開頭的目錄）。JSON 壞時等 0.2 秒重讀一次，
    仍壞回 {"task_id": 目錄名, "_bad": True}（寫入中被讀到的競態）。
    ponytail：save() 不改成「寫暫存檔＋os.replace」——Windows 上讀取端開著檔時 os.replace 會 PermissionError，
    反而可能讓 relay 本身崩潰；改由讀取端重讀一次容忍半寫狀態。已知上限：極少數時候某列顯示「讀取失敗」，再跑一次即可。"""
    if not runs_dir.is_dir():
        return []
    out = []
    for d in sorted(runs_dir.iterdir()):
        f = d / "STATE.json"
        if not d.is_dir() or d.name.endswith(".dry") or d.name.startswith("_") or not f.is_file():
            continue
        s = _read_state(f)
        if s is None:
            time.sleep(0.2)
            s = _read_state(f)
        out.append({**s, "task_id": s.get("task_id") or d.name} if s is not None else {"task_id": d.name, "_bad": True})
    return out


def _parse_ts(s) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")  # now() 的格式，帶 +0800
    except (TypeError, ValueError):
        return None


def _segment_start(s: dict):
    """這一次執行的開始時間：有 resume 紀錄就用最後一次 resume 的 ts，否則 started（C6，2026-10-05）。
    started 保留原始開跑時間；耗時若從它算，會把「棒停著等人」的幾小時到幾天也算進去。"""
    r = s.get("resumed")
    last = r[-1] if isinstance(r, list) and r else None  # --status 也會讀到壞 STATE：形狀不對就退回 started
    return last.get("ts") if isinstance(last, dict) and last.get("ts") else s.get("started")


def _fmt_minutes(seconds: float) -> str:
    m = max(int(seconds // 60), 0)
    return f"{m}分" if m < 60 else f"{m // 60}時{m % 60}分"


def _waiting(s: dict, alive: bool) -> str:
    """「等人？」欄，依序比對（README「跑」段有摘要）。活著只看鎖，不看 STATE 的階段說什麼。"""
    phase, verdict = s.get("phase"), s.get("verdict")
    if s.get("_bad"):
        return "（STATE 讀取失敗）"
    if alive:
        return "排隊中" if phase == "queued" else "跑中"
    if phase == "done":
        return f"待合併 {s.get('commit') or '?'}"
    if phase == "escalate":
        if verdict == "review_tool_failure":
            return "要人看（審查工具故障）"
        if verdict in ("rate_limit", "reviewer_rate_limit"):
            return "要人看（撞牆）"
        return "要人看（未收斂）"
    if phase == "aborted":
        return f"要人看（中止：{(s.get('abort_reason') or '')[:20]}）"
    return "中斷？（行程已不在）"


def status_rows(states: list[dict], alive: dict[str, bool], now: datetime) -> list[dict]:
    """STATE 清單 → 總表列，依 updated 由新到舊。alive[task_id]＝任務鎖有沒有人持有；now 要帶時區。
    耗時：活著＝now − started；否則＝updated − started（resume 過的棒 started 取最後一次 resume，見 _segment_start）。"""
    rows = []
    for s in states:
        tid, bad = s.get("task_id", "?"), bool(s.get("_bad"))
        is_alive = bool(alive.get(tid))
        started, updated = _parse_ts(_segment_start(s)), _parse_ts(s.get("updated"))
        end = now if is_alive else updated
        rows.append({
            "task": tid,
            "phase": "?" if bad else str(s.get("phase", "?")),
            "round": "?" if bad else f"{s.get('round', 0)}/{s.get('max_rounds') or '?'}",
            "elapsed": _fmt_minutes((end - started).total_seconds()) if started and end else "?",
            "waiting": _waiting(s, is_alive),
            "updated": updated.strftime("%m-%d %H:%M") if updated else "?",  # STATE 本來就是 +0800，直接顯示
            "_ts": updated,
        })
    rows.sort(key=lambda r: r["_ts"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return rows


_STATUS_COLS = (("task", "任務"), ("phase", "階段"), ("round", "輪次"), ("elapsed", "耗時"),
                ("waiting", "等人？"), ("updated", "更新"))


def _disp_width(s: str) -> int:
    """終端機顯示寬度：East Asian Wide／Fullwidth 算 2，其餘 1（對齊中文欄用）。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def render_status(rows: list[dict], limit: int | None) -> str:
    if not rows:
        return "（沒有任何紀錄）"
    shown = rows if limit is None else rows[:limit]
    table = [[h for _, h in _STATUS_COLS]] + [[str(r[k]) for k, _ in _STATUS_COLS] for r in shown]
    widths = [max(_disp_width(line[i]) for line in table) for i in range(len(_STATUS_COLS))]
    lines = ["  ".join(c + " " * (w - _disp_width(c)) for c, w in zip(line, widths)).rstrip() for line in table]
    if len(shown) < len(rows):
        lines.append(f"（另有 {len(rows) - len(shown)} 筆較舊的紀錄；--all 看全部）")
    return "\n".join(lines)


def status_report(limit: int | None) -> str:
    states = collect_states(HERE / "runs")
    alive = {s["task_id"]: valid_task_id(s["task_id"]) and runlock.is_held(task_lock_path(s["task_id"]))
             for s in states}
    return render_status(status_rows(states, alive, datetime.now().astimezone()), limit)


class TaskError(ValueError):
    """任務檔不合法，或 resume 的前置檢查不過：main() 印訊息後 exit 3。"""


def load_task(path: Path) -> dict:
    """讀任務檔＋一般啟動與 resume 共用的驗證（2026-10-05 從 main() 抽出，C6 resume 會重讀任務檔）。
    相對路徑一律相對於 relay.py 所在目錄。"""
    try:
        task = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise TaskError(f"讀不了任務檔 {path}：{type(e).__name__}: {e}") from None
    if not isinstance(task, dict):
        raise TaskError("任務檔最上層必須是 JSON 物件")
    for k in ("id", "repo", "base_branch", "branch", "worktree", "spec_file", "verify", "review"):
        if k not in task:
            raise TaskError(f"task 缺欄位 {k}")
    if not valid_task_id(task["id"]):
        raise TaskError(f"task id 不合法：{task['id']!r}（英數或底線開頭，其後英數 . _ -，最長 100 字，不可以 . 或 .dry 結尾）")
    if not isinstance(task["review"], dict) or "instructions_file" not in task["review"]:
        raise TaskError("task 缺欄位 review.instructions_file")
    for k in ("spec_file",):
        if not Path(task[k]).is_absolute():
            task[k] = str(HERE / task[k])
    if not Path(task["review"]["instructions_file"]).is_absolute():
        task["review"]["instructions_file"] = str(HERE / task["review"]["instructions_file"])
    prod = task.get("production_dir")
    if prod and Path(task["worktree"]).resolve() == Path(prod).resolve():
        raise TaskError("worktree 不得等於生產目錄")
    return task


# ---- C6（2026-10-05）：人工意見回灌 relay.py --resume <task_id> --------------------------------------
ROUND_FILE_RE = re.compile(r"(impl|verify|review)_r(\d+)[._]")


@dataclass
class ResumePlan:
    task: dict
    state: State
    notes: str
    rounds: int
    task_file: str
    snapshot: dict  # 前置檢查讀到的 STATE 原文：拿到鎖後再比一次，確認沒被別的行程改過


def resolve_task_file(cli: str | None, state_task_file: str, task_id: str, here: Path | None = None) -> Path | None:
    """resume 用哪份任務檔：命令列 > STATE 記的 task_file > <here>/tasks/<id>.json；都找不到回 None。
    命令列有給就只認它——給錯路徑要大聲說，不默默換成別份。"""
    if cli:
        return Path(cli) if Path(cli).is_file() else None
    for c in (state_task_file, (HERE if here is None else here) / "tasks" / f"{task_id}.json"):
        if c and Path(c).is_file():
            return Path(c)
    return None


def prepare_resume(task_id: str, cli_task: str | None, rounds_arg: int | None) -> ResumePlan:
    """resume 的前置檢查，只讀不寫；任一不過丟 TaskError（訊息說明怎麼補救）。
    取捨原則：狀態對不上就停下讓人看，絕不靜默從頭重跑；不做任何破壞性 git 操作（不 checkout／reset／rebase）。
    「同一任務正在跑」由 main 取任務鎖時擋（C3）；STATE 停在非終態但沒人持有鎖＝崩潰殘留，拿到鎖後記一行警告照常接續。"""
    if not valid_task_id(task_id):
        raise TaskError(f"task id 不合法：{task_id!r}")
    rd = HERE / "runs" / task_id
    group_msg = f"{task_id} 是 best-of-N 群組：群組不能 resume，請指定候選 id（例如 {task_id}.c1）"
    if (rd / "GROUP.json").exists():
        raise TaskError(group_msg)
    if not (rd / "STATE.json").is_file():
        raise TaskError(f"找不到 runs/{task_id}/STATE.json：這棒沒跑過或 id 打錯（resume 只接續跑過的棒；新任務用 relay.py <task.json>）")
    snap = _read_state(rd / "STATE.json")
    try:
        if snap is None:
            raise ValueError("不是合法的 JSON 物件")
        state = state_from_dict(snap)
    except ValueError as e:
        raise TaskError(f"runs/{task_id}/STATE.json 損壞（{e}）：請人工檢查；relay 不會從頭重跑") from None
    if state.task_id != task_id:
        raise TaskError(f"STATE.json 的 task_id 是 {state.task_id!r}，與 --resume {task_id!r} 不符")
    tf = resolve_task_file(cli_task, state.task_file, task_id)
    if tf is None:
        where = f"命令列給的 {cli_task} 不存在" if cli_task else f"STATE 記的 task_file 與 tasks/{task_id}.json 都不在"
        raise TaskError(f"找不到任務檔（{where}）；請用 relay.py <task.json> --resume {task_id}")
    task = load_task(tf)
    if task["id"] != task_id:
        raise TaskError(f"任務檔 {tf} 的 id 是 {task['id']!r}，與 --resume {task_id!r} 不符")
    if task.get("candidates"):
        raise TaskError(group_msg)
    wt = task["worktree"]
    if state.worktree and _path_key(state.worktree) != _path_key(wt):
        raise TaskError(f"STATE 記的 worktree {state.worktree} 與任務檔的 {wt} 不同：換 worktree 就是另一棒，請開新棒")
    if state.branch and state.branch != task["branch"]:
        raise TaskError(f"STATE 記的分支 {state.branch} 與任務檔的 {task['branch']} 不同：換分支就是另一棒，請開新棒")
    if not Path(wt).is_dir():
        raise TaskError(f"worktree 不存在：{wt}。resume 不重建 worktree（重建＝從頭重跑）；要從頭請用 relay.py <task.json> 另開一棒")
    top = git(wt, "rev-parse", "--show-toplevel")
    try:  # samefile：git 回的是正斜線長路徑，任務檔可能寫短路徑／反斜線，字串比會誤判
        same = top.returncode == 0 and os.path.samefile(top.stdout.strip(), wt)
    except OSError:
        same = False
    if not same:  # 例如一般資料夾剛好在別的 repo 底下：分支檢查會量到外層 repo，必須先擋
        raise TaskError(f"{wt} 不是 git worktree 的根目錄（{(top.stderr or top.stdout).strip()[-200:]}）；請人工確認")
    head = git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if head != task["branch"]:
        raise TaskError(f"worktree 目前在 {head!r}，不是任務分支 {task['branch']!r}；relay 不代為 checkout，請人工確認後再 resume")
    if state.commit and git(wt, "merge-base", "--is-ancestor", state.commit, "HEAD").returncode != 0:
        raise TaskError(f"分支 {task['branch']} 上找不到上次的 commit {state.commit}（被 reset／rebase 過？）；relay 不改寫歷史，請人工確認")
    np = rd / "human_notes.md"
    if not np.is_file():
        raise TaskError(f"沒有 runs/{task_id}/human_notes.md：把要給實作者與審查者的意見寫進這個檔再 resume")
    try:
        notes = np.read_text(encoding="utf-8-sig").strip()  # utf-8-sig：記事本存檔帶的 BOM 去掉
    except (OSError, UnicodeDecodeError) as e:
        raise TaskError(f"讀不了 runs/{task_id}/human_notes.md（請存成 UTF-8）：{type(e).__name__}") from None
    if not notes:
        raise TaskError(f"runs/{task_id}/human_notes.md 是空的（只有空白／BOM）：寫下意見再 resume")
    rounds = int(task.get("max_rounds", 2)) if rounds_arg is None else rounds_arg
    if rounds < 1:
        raise TaskError(f"--rounds 必須 ≥ 1（給的是 {rounds}）")
    first = state.round + 1
    if (rd / f"human_notes.r{first}.md").exists():
        raise TaskError(f"runs/{task_id}/human_notes.r{first}.md 已存在（上次 resume 中途中止？）：確認內容後移走或併進 human_notes.md 再 resume")
    seen = [int(m.group(2)) for p in rd.iterdir() if (m := ROUND_FILE_RE.match(p.name))]
    if seen and max(seen) > state.round:
        raise TaskError(f"runs/{task_id}/ 已有第 {max(seen)} 輪的紀錄，但 STATE 的 round 是 {state.round}：狀態對不上，"
                        "請人工檢查（resume 不覆蓋舊紀錄）")
    return ResumePlan(task, state, notes, rounds, str(tf.resolve()), snap)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task", nargs="?")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ledger", action="store_true", help="印出跨任務用量帳本總計")
    ap.add_argument("--status", action="store_true", help=f"所有棒的階段、輪次、耗時、是否等人（預設最近 {STATUS_LIMIT} 筆）")
    ap.add_argument("--all", action="store_true", help="配 --status：列出全部紀錄")
    ap.add_argument("--no-notify", action="store_true", help="這一棒不推播（人坐在終端機前跑時用）")
    ap.add_argument("--queue", action="store_true", help="並行名額（RELAY_MAX_PARALLEL，預設 1）滿了就排隊等，不直接拒跑")
    ap.add_argument("--resume", metavar="TASK_ID", help="人工意見回灌：讀 runs/<id>/human_notes.md，從上次的 STATE 接續下一輪")
    ap.add_argument("--rounds", type=int, help="配 --resume：這次最多跑幾輪（預設＝任務的 max_rounds）")
    a = ap.parse_args(argv)
    if a.ledger:
        for cli, t in ledger_totals().items():
            print(f"{cli:6s} calls={t['calls']} input_total={t['input_total']:,} output={t['output']:,} "
                  f"seconds={t['seconds']:.0f} rate_limited={t['rate_limited']}")
        return 0
    if a.status:
        print(status_report(None if a.all else STATUS_LIMIT))
        return 0
    if a.rounds is not None and not a.resume:
        print("--rounds 只能配 --resume 使用（一般棒的輪數寫在任務檔 max_rounds）", file=sys.stderr)
        return 3
    if not a.task and not a.resume:
        ap.error("缺 task 檔")
    if not a.dry_run:
        missing = paths.check_all()
        if missing:
            print("環境缺少 CLI,無法動工:\n  - " + "\n  - ".join(missing), file=sys.stderr)
            return 3
    if a.resume:
        try:
            plan = prepare_resume(a.resume, a.task, a.rounds)
        except TaskError as e:
            print("relay 拒絕 resume：", e, file=sys.stderr)
            return 3
        run = Run(plan.task, a.dry_run, task_file=plan.task_file, no_notify=a.no_notify, resume_state=plan.state)
        return _execute(run, plan.task, a, plan)
    try:
        task = load_task(Path(a.task))
    except TaskError as e:
        print(e, file=sys.stderr)
        return 3
    run = Run(task, a.dry_run, task_file=str(Path(a.task).resolve()), no_notify=a.no_notify)
    return _execute(run, task, a)


def _execute(run: Run, task: dict, a, plan: ResumePlan | None = None) -> int:
    """取鎖 → 跑棒 → 例外落檔（一般與 resume 共用；2026-10-05 從 main() 抽出）。"""
    # 2026-10-05（C3）：取鎖與跑棒分兩段——取鎖階段被擋時 runs/<id>/ 可能正被別的行程寫，不可寫 STATE；
    # 跑棒階段的任何例外都在鎖還握著時落檔（C1：中止要寫進 STATE，總表才不會說謊）。dry-run 不取任何鎖。
    # C6：resume 排隊時不寫 queued——STATE 要保持前置檢查讀到的原樣，拿到鎖後才比對得出有沒有被別人動過。
    # ponytail：代價是 resume 排隊中 --status 顯示「跑中」而非「排隊中」。
    on_wait = None if plan else (lambda: run.save("queued"))
    try:
        locks = nullcontext() if a.dry_run else acquire_run_locks(task, queue=a.queue, on_wait=on_wait)
    except runlock.LockBusy as e:
        print("relay 拒跑：", e, file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        if not plan and run.state.phase == "queued":  # 排隊中按 Ctrl-C：queued 是自己寫的，改記中止，總表才不會顯示「中斷？」
            run.abort("使用者中斷（排隊中）", notify=False)
        return 130
    with locks:
        kw: dict = {}
        if plan:
            if not a.dry_run and _read_state(HERE / "runs" / task["id"] / "STATE.json") != plan.snapshot:
                print("relay 拒絕 resume：STATE.json 在檢查之後被改動（另一個 relay 剛跑過這棒？）；請重看 HANDOFF 後再 resume",
                      file=sys.stderr)
                return 3
            try:
                kw = run.start_resume(plan.notes, plan.rounds)
            except OSError as e:
                print(f"relay 拒絕 resume：保存上一輪紀錄失敗（{type(e).__name__}: {e}）", file=sys.stderr)
                return 3
        try:
            if not a.dry_run:
                run.note_interrupted_previous()
            return run.run(**kw)
        except KeyboardInterrupt:
            run.abort("使用者中斷", notify=False)
            return 130
        except RateLimitStop as e:  # 撞牆：獨立 kind，文案是「等額度或換 CLI」；exit code 仍 3
            print("relay 中止：", e, file=sys.stderr)
            run.abort(str(e), kind="rate_limit", cli=e.cli)
            return 3
        except RuntimeError as e:  # relay 自己丟的中止：撞牆、worktree／prebuild 失敗、規格外改動、等 repo 鎖逾時…
            print("relay 中止：", e, file=sys.stderr)
            run.abort(str(e))
            return 3
        except Exception as e:  # 其他崩潰（git 逾時 TimeoutExpired 等）也要落檔，並留 traceback
            run.abort(f"{type(e).__name__}: {e}")
            traceback.print_exc()
            return 3


if __name__ == "__main__":
    sys.exit(main())
