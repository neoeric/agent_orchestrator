#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""relay.py — 多 Agent 協作編排器 P1：一個子任務一棒（實作 → 驗證 → 審查 → 判定 → commit），最小可用版。

依「多 Agent 協作方案 v3」與 2026-09-07 目標 repo 重構六組的手動實跑固化而成。一棒＝：
  1. 從 base_branch 開隔離 worktree（生產目錄永遠不碰），跑 repo 級前置步驟（例如建 gitignore 的 views/）
  2. 實作者（Codex 或 Claude Code，task 的 implementer）依「完全指定」的規格改碼；不 commit。
     implementer 寫成清單＝judge 判 rate_limit 時同一輪依序換手（C5：不消耗輪數、只限本棒、不下架任何 Agent）
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
離開碼：0＝收斂並已 commit；2＝不收斂／被擋（含撞牆：可用的實作者都回 rate_limit、或審查者回 rate_limit），
已寫 HANDOFF 給人，命令列參數錯（argparse）也回 2；3＝任務檔內容錯、環境自檢失敗、被鎖擋下拒跑、或例外中止（STATE 記 aborted）；130＝Ctrl-C。
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
from collections.abc import Callable
from contextlib import ExitStack, nullcontext
from dataclasses import MISSING, dataclass, field, asdict
from datetime import datetime, timedelta, timezone
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
# P4：跨任務用量帳本。2026-10-05：可由 RELAY_LEDGER 覆寫（多行程併寫測試用），預設不變；
# C5 起另供換手冷卻「唯讀查詢」（handoff_cooldown_minutes，預設 0＝關），relay 不據以寫任何停用清單。
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


class ProductionTouched(RuntimeError):
    """C4a（2026-10-05）：實作者呼叫前後生產目錄的快照不同（紅線，決策 D13＝一律中止）。
    是 RuntimeError：main() 既有的 except 會走 abort（落檔＋推播），exit 3。"""

# 四個 CLI 的位置：env → PATH → 已知安裝位置 → None（見 tools/paths.py）。可攜化 2026-09-08。
PY = paths.resolve_python()
CODEX = paths.resolve_codex() or "codex"
AGY = paths.resolve_agy() or "agy"
# 2026-10-05（C4a）：Claude 實作者。resolve_claude 會略過 npm 的 .cmd 薄殼改回原生 exe（多行 prompt 經 cmd.exe 會壞）；
# 找不到時的 "claude" 只會出現在 dry-run 計畫裡——真跑前 main() 的環境自檢就會擋下
CLAUDE = paths.resolve_claude() or "claude"
AGY_REVIEW = HERE / "tools" / "agy_review.py"

# ---- C4a（2026-10-05）：實作者可插拔 codex｜claude ------------------------------------------------
IMPLEMENTERS = ("codex", "claude")
MAX_CANDIDATES = 3   # C4（2026-10-05）best-of-N 的候選上限，寫死：N 份＝N 倍實作＋N 倍審查額度，成本要事前可見
IMPL_NAMES = {"codex": "Codex", "claude": "Claude Code"}   # 給人看的名字（log、commit 訊息）
# Claude 沒有 Codex 的沙箱，所以紅線改成「結構上做不到」：只給讀寫檔工具。沒有 Bash ⇒ 不能 git commit／push、
# 不能啟動服務、也不能自己跑驗證指令（那本來就是 relay 的事）
CLAUDE_IMPL_TOOLS = "Read,Edit,Write,Glob,Grep"
# --effort 的合法值，來源：claude --help（2.1.246）。task 寫錯在啟動時就擋，不要等 CLI 回錯再白燒一輪
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# ---- C5（2026-10-05）：撞牆換手（只限實作者）--------------------------------------------------------
# 換手時 worktree 不重置（決策 D9：不做破壞性操作，verify＋審查才是裁判），所以要明講「可能有半成品」，
# 否則接手者會從頭重寫、或把半成品當成既有程式碼繞開。結尾的空行是 impl_prompt 切開說明與其餘發現的界線
HANDOFF_NOTE = ("【換手說明】前一位實作者（{prev}）在本輪中途因額度限制停止，worktree 可能已有部分改動。"
                "請先唯讀檢查現有改動，再依規格完成；已正確完成的部分不要重做。\n\n")
RATE_LIMIT_VERDICTS = ("rate_limit", "reviewer_rate_limit")   # 撞牆停下的兩種 verdict：exit 2、推播 kind rate_limit

# 2026-10-05（C4a）：本機路徑改成執行時的 {HERE}（原本寫死開發機路徑＝PUBLIC repo 洩漏、也不可攜）
IMPL_RULES = f"""【守則，違反即整棒作廢】
- 你在隔離 worktree 裡工作；生產目錄與其他任何目錄絕對不要碰。不要 git commit、不要 push、不要啟動任何服務、
  **規格列的驗證指令由 relay 執行、不是你**——你的環境裡常沒有可用的 Python 3（Claude 實作者根本沒有指令工具；Codex 沙箱裡裸 python 多是 2.7、專案 Python 3 可能被沙箱擋），硬跑只會浪費大量時間且完全不影響判定；你只要改好碼、做唯讀 diff 稽核、回報，不要自己跑那些驗證指令，也不要跑會載入模型的測試。不要修改 {HERE} 底下任何檔案。
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


def diff_line_count(diff: str) -> int:
    """diff 的增刪行數（不含 +++／--- 檔頭）。decide_review 的小改動判準與 C4 排名的 diff_stats 共用同一算法。"""
    return sum(1 for l in diff.splitlines() if (l.startswith("+") or l.startswith("-")) and not l.startswith(("+++", "---")))


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
    lines = diff_line_count(diff)
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
    """各 CLI 的累計。C5（2026-10-05）加 last_rate_limit：最後一筆 rate_limit 的 ts 原字串（沒有＝None）。
    帳本只 append（並行時也在鎖內），檔案順序＝時間順序，所以取最後出現的那筆，不必解析 ts。"""
    tot: dict = {}
    if not LEDGER.exists():
        return tot
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        u = e.get("usage") or {}
        t = tot.setdefault(e.get("cli", "?"), {"calls": 0, "input_total": 0, "output": 0, "seconds": 0.0, "rate_limited": 0,
                                               "last_rate_limit": None})
        t["calls"] += 1
        t["input_total"] += int(u.get("input_tokens_total") or 0)
        t["output"] += int(u.get("output_tokens") or 0)
        t["seconds"] += float(e.get("seconds") or 0)
        if e.get("failure_class") == "rate_limit":
            t["rate_limited"] += 1
            t["last_rate_limit"] = e.get("ts") or t["last_rate_limit"]
    return tot


def ledger_recent_rate_limits(now: datetime, minutes: int, ledger: Path | None = None) -> dict[str, datetime]:
    """C5（2026-10-05）：帳本裡 minutes 分鐘內、各 CLI 最近一次 rate_limit 的時間 {cli: ts}（換手冷卻用，決策 D8 預設關）。
    🔴 紅線：純讀。冷卻是每棒開始時由帳本「即時計算」的時間窗，不落檔、不維護任何停用清單——程式永不下架任何 Agent。
    now 要帶時區；minutes<=0 直接回 {}（不讀檔）；ts 解析失敗、不是物件的行略過；比 now 新的 ts（時鐘被調過）也算在窗內。
    ledger 預設是呼叫當下的 LEDGER（不在定義時綁定：測試會換掉 relay.LEDGER）。
    ponytail：每次讀整份帳本，O(n)、n＝歷史呼叫數（目前百筆級）；上萬筆時改成從檔尾倒讀到超出時間窗為止。"""
    if minutes <= 0:
        return {}
    try:
        lines = (LEDGER if ledger is None else ledger).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    window, out = timedelta(minutes=minutes), {}
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("failure_class") != "rate_limit":
            continue
        cli, ts = e.get("cli"), _parse_ts(e.get("ts"))
        if not isinstance(cli, str) or not cli or ts is None:
            continue
        if now - ts <= window and (cli not in out or ts > out[cli]):
            out[cli] = ts
    return out


def pick_implementer(order: list[str], cooling: set[str], recent: dict[str, datetime], cooldown_min: int) -> str | None:
    """C5（2026-10-05）：這一次實作用誰（純函式）。
    1) 排除 cooling（本棒內撞過牆的）；全被排除 → None（呼叫端停下、verdict rate_limit）。
    2) cooldown_min>0 時跳過 recent 裡的 CLI；若因此全部被跳過 → 回第 1 步剩下的第一個
       （帳本冷卻只是建議，永不讓任務無人可用）。"""
    avail = [c for c in order if c not in cooling]
    if not avail:
        return None
    if cooldown_min > 0:
        fresh = [c for c in avail if c not in recent]
        if fresh:
            return fresh[0]
    return avail[0]



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
    implementer: str = ""     # C4a（2026-10-05）：最近一次實作者呼叫用的 CLI（codex／claude）；舊 STATE 沒有＝""
    # C5（2026-10-05）：每次換手／跳過一筆 {"round","from","to","reason","ts",…}；reason＝rate_limit（同輪換手）、
    # cooling（本棒已撞過牆而略過）、cooldown（帳本冷卻窗內而略過）。只是紀錄：relay 從不讀它來決定下一棒用誰
    handoffs: list = field(default_factory=list)
    # C4（2026-10-05）：best-of-N 排名只讀 STATE，所以把排名要用的兩項落檔（單棒也寫，無害）
    negctl: list = field(default_factory=list)       # 每輪 {"round","ok"}；verify 沒過就沒跑陰性對照，記 ok=True
    diff_stats: dict = field(default_factory=dict)   # 最後一輪 {"files","lines"}（每輪覆寫）


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


def implementer_order(task: dict) -> list[str]:
    """task 的 implementer → 換手順序清單（C5，2026-10-05）。字串＝長度 1（不換手）；省略＝["codex"]。
    合法性由 load_task 擋；回新清單，不和 task 共用（紅線：relay 不改 task 的任何內容）。"""
    impl = task.get("implementer", "codex")
    return [impl] if isinstance(impl, str) else list(impl)


def impl_stem(rnd: int, attempt: int = 0, cli: str = "") -> str:
    """實作者紀錄檔的檔名主幹（prompt／stdout／stderr／exit／last_message 共用）。
    C5（2026-10-05）：attempt>0＝同一輪換手後的第 attempt 次呼叫 → impl_r{rnd}_h{attempt}_{cli}，不覆蓋第一次的紀錄。"""
    return f"impl_r{rnd}" + (f"_h{attempt}_{cli}" if attempt else "")


def has_rate_limit_fixture(cli: str) -> bool:
    """fixtures/ 有沒有這支 CLI 的 rate_limit 真樣本（檔名慣例 <cli>_rate_limit.*）。C5（2026-10-05）：真 429 至今
    未觀察到，各家判定都沒有真樣本；沒有時 relay 在撞牆當下提示人把原始輸出收進來，收了之後就不再嘮叨。"""
    return any((HERE / "fixtures").glob(f"{cli}_rate_limit*"))


def sh(cmd: list[str], cwd: str | None = None, timeout: int = 900, env: dict | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ, PYTHONUTF8="1", GIT_TERMINAL_PROMPT="0")
    if env:
        e.update(env)
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, env=e, stdin=subprocess.DEVNULL)


# ---- 即時串流：讓 VS Code 整合終端機看得到三個角色在協力 --------------------------------
_ESC = chr(27)
_COLORS = {"RELAY": "90", "CODEX": "36", "CLAUDE": "34", "AGY": "35", "JUDGE": "33", "VERIFY": "32", "HUMAN": "93"}
_USE_COLOR = os.environ.get("NO_COLOR") is None


def emit(prefix: str, msg: str, logf=None) -> None:
    tag = f"[{prefix}]"
    shown = f"{_ESC}[{_COLORS.get(prefix, '0')}m{tag}{_ESC}[0m {msg}" if _USE_COLOR else f"{tag} {msg}"
    print(shown, flush=True)
    if logf:
        with open(logf, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {tag} {msg}\n")


def stream(cmd, prefix: str, on_line, cwd=None, timeout=900, env=None, logf=None, shell=False,
           input_text: str | None = None, drop_env: tuple = ()):
    """跑子行程並逐行即時印帶前綴的行（給 VS Code 終端機看），同時完整收集 stdout／stderr 回傳給判定器。
    on_line(rawline)->str|None：回字串就印（已翻成人看得懂），回 None 就吞掉（雜訊）。

    2026-10-05（C4a）：input_text 非 None 時經 stdin 送給子行程（prompt 不再走 argv，不受 Windows 32K 命令列上限）；
    drop_env 是建好 env 後要刪的鍵（巢狀呼叫 claude 要清掉 paths.CLAUDE_NESTED_ENV）。"""
    e = dict(os.environ, PYTHONUTF8="1", GIT_TERMINAL_PROMPT="0")
    if env:
        e.update(env)
    for k in drop_env:
        e.pop(k, None)
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="replace", env=e, bufsize=1, shell=shell,
                         stdin=subprocess.DEVNULL if input_text is None else subprocess.PIPE)
    out, err = [], []
    et = threading.Thread(target=lambda: [err.append(x) for x in iter(p.stderr.readline, "")], daemon=True)
    et.start()
    feeder = None
    if input_text is not None:
        def feed() -> None:
            # 另開 thread 寫：大 prompt 塞滿 pipe 時寫入會阻塞，主執行緒要同時讀 stdout，否則雙方互卡。
            # 寫 bytes 到底層 buffer：text 模式在 Windows 會把 \n 轉成 \r\n，偷偷改了 prompt（council.py 同一個坑）。
            try:
                p.stdin.buffer.write(input_text.encode("utf-8"))
            except OSError:  # 含 BrokenPipeError：子行程提早退出（例如認證失敗），判定器會從 stdout 判失敗
                pass
            finally:
                try:
                    p.stdin.close()
                except OSError:
                    pass
        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
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
    if feeder is not None:
        feeder.join(timeout=5)
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


_CLAUDE_EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def _claude_line(line: str):
    """C4a（2026-10-05）：`claude -p --output-format stream-json --verbose` 的一行 → 給人看的一行，其餘回 None。
    事件形狀以 fixtures/claude_stream_ok.stdout.txt（2.1.246 真樣本）為準：system/init、assistant、rate_limit_event、result。"""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        e = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(e, dict):
        return None
    t = e.get("type")
    if t == "assistant":
        content = (e.get("message") or {}).get("content")
        shown = []
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                tx = (b.get("text") or "").strip().replace("\n", " ")
                if tx:
                    shown.append("💬 " + tx[:100])
            elif b.get("type") == "tool_use":
                name, inp = str(b.get("name") or "?"), b.get("input") if isinstance(b.get("input"), dict) else {}
                target = str(inp.get("file_path") or inp.get("path") or inp.get("pattern") or "")
                target = os.path.basename(target.rstrip("/\\")) or target
                shown.append(f"{'✏' if name in _CLAUDE_EDIT_TOOLS else '$'} {name} {target}".rstrip())
        return " ｜ ".join(shown) or None
    if t == "result":
        u = judge._claude_usage(e) or {}
        head = "✗ 失敗" if e.get("is_error") is not False else "✓ 完成"
        return f"{head} in={u.get('input_tokens_total')} out={u.get('output_tokens')}"
    return None


@dataclass
class ImplCmd:
    """實作者一次呼叫要的東西（C4a，2026-10-05）。prompt 不在 argv 裡：一律由 stream(input_text=…) 走 stdin。"""
    argv: list[str]
    prefix: str                              # 終端機前綴（_COLORS 的鍵）
    line_fn: Callable[[str], str | None]     # stdout 一行 → 給人看的一行
    drop_env: tuple[str, ...] = ()           # 子行程 env 要刪的鍵


def build_impl_command(cli: str, wt: str, out_last, model: str | None = None, effort: str | None = None) -> ImplCmd:
    """組實作者的指令（純函式，不呼叫任何東西）。cli 只能是 IMPLEMENTERS 之一；codex 不支援 effort（傳了就大聲錯）。"""
    if cli == "codex":
        if effort:
            raise ValueError("codex 實作者不支援 effort（implementer_effort 只接受 claude）")
        # 🔴 -o 一定要絕對路徑：相對路徑會以 -C（worktree）為基準，輸出檔會落進受測 repo（council.py 同一個坑）
        argv = [CODEX, "exec", "--json", "-s", "workspace-write", "-C", str(wt), "-o", str(Path(out_last).resolve())]
        argv += ["-m", model] if model else []
        return ImplCmd(argv + ["-"], "CODEX", _codex_line)  # "-"＝prompt 從 stdin 讀（codex exec --help，0.160.0）
    if cli == "claude":
        argv = [CLAUDE, "-p", "--output-format", "stream-json", "--verbose",
                "--permission-mode", "acceptEdits",      # 只自動接受檔案編輯；工作目錄外的寫入另由生產目錄守門兜底
                "--tools", CLAUDE_IMPL_TOOLS,             # 硬限制可用工具：沒有 Bash（理由見 CLAUDE_IMPL_TOOLS）
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',  # 不載入任何 MCP（使用者環境有可寫 GitHub 的 MCP）
                "--no-session-persistence",
                # 決策 D12：不跑使用者 hooks／CLAUDE.md／使用者 skills（無頭子行程裡行為不可預期）。
                # 2026-10-05 fixture 證實加了它 stream-json 照常（fixtures/claude_stream_ok.*），不需退回不加
                "--safe-mode"]
        argv += ["--model", model] if model else []
        argv += ["--effort", effort] if effort else []
        return ImplCmd(argv, "CLAUDE", _claude_line, paths.CLAUDE_NESTED_ENV)
    raise ValueError(f"未知的實作者 {cli!r}（只能是 {' / '.join(IMPLEMENTERS)}）")


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


def worktree_problem(wt: str, branch: str) -> str:
    """沿用既有 worktree 前的檢查（2026-10-05）：回傳問題描述，沒問題回 ""。一般啟動（含 --dry-run）與 --resume 共用，訊息才不會互相矛盾。
    為什麼：relay 紅線是「只 commit 到 task 自己的 branch」，路徑已存在就直接沿用的話，那個目錄若被人切到別的分支，
    commit 會打到錯的分支；若它其實只是別個 repo 底下的普通資料夾，分支檢查會量到外層 repo。
    只讀（rev-parse）；任何不符都只回報，relay 不代為 checkout／switch／reset（寧可多擋一次讓人看）。"""
    top = git(wt, "rev-parse", "--show-toplevel")
    try:  # samefile：git 回的是正斜線長路徑，任務檔可能寫短路徑／反斜線，字串比會誤判
        same = top.returncode == 0 and os.path.samefile(top.stdout.strip(), wt)
    except OSError:
        same = False
    if not same:
        return (f"{wt} 不是 git worktree 的根目錄（{(top.stderr or top.stdout).strip()[-200:]}）；期望分支 {branch!r}；"
                "relay 不代為 checkout，請人處理")
    head = git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if head != branch:
        return f"worktree {wt} 目前在 {head!r}，期望任務分支 {branch!r}；relay 不代為 checkout，請人處理"
    return ""


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


# ---- C4a（2026-10-05）：生產目錄守門。新實作者（Claude）沒有 Codex 的沙箱，「不碰生產目錄」這條紅線要機械化 -----
def prod_snapshot(prod: str | None) -> str | None:
    """生產目錄的快照字串（項目以 NUL 分隔）；None＝沒設 production_dir，或它不是 git repo（守門停用，呼叫端要說出來）。

    內容：`status --porcelain -z --untracked-files=all` 每一項＋該檔的 大小:mtime_ns，最後一項是 HEAD。
    🔴 一定要 --no-optional-locks：普通 git status 會刷新並改寫生產 repo 的 .git/index（拿 index.lock），
       本身就是對生產目錄的寫入，還可能撞到正在運作的人／服務。
    比規格多兩點（紅線寧可誤報）：-uall 讓「未追蹤目錄裡多一個檔」看得到；本來就髒的檔再被改，status 那行不變，
    所以附上大小與 mtime。git 認成巢狀 repo 的目錄（例如放在生產目錄底下的 worktree）只列一項 `dir/`，不看 mtime，
    否則實作者在那個 worktree 裡改檔就會誤報。
    ponytail：被 .gitignore 的檔看不到（例如生產目錄的 .env、資料檔）；升級路徑＝加 `--ignored` 列表或檔案系統監看。
    git status 本身失敗（不是「不是 repo」）→ RuntimeError：驗不了紅線就不跑。"""
    if not prod or not Path(prod).is_dir():
        return None
    top = git(prod, "--no-optional-locks", "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return None
    st = git(prod, "--no-optional-locks", "status", "--porcelain", "-z", "--untracked-files=all", timeout=300)
    if st.returncode != 0:
        raise RuntimeError(f"生產目錄快照失敗（git status exit {st.returncode}）：{st.stderr.strip()[-200:]}")
    root = Path(top.stdout.strip())
    items = []
    for xy, path in parse_porcelain_z(st.stdout):
        sig = "dir"
        if not path.endswith("/"):
            try:
                s = (root / path).stat()
                sig = f"{s.st_size}:{s.st_mtime_ns}"
            except OSError:
                sig = "-"
        items.append(f"{xy} {path} [{sig}]")
    head = git(prod, "--no-optional-locks", "rev-parse", "HEAD")
    items.append("HEAD=" + (head.stdout.strip() if head.returncode == 0 else "(無)"))
    return "\0".join(items)


def snapshot_delta(before: str, after: str, limit: int = 10) -> str:
    """兩份快照的差異（給人看）：新增／消失的項目各列前 limit 筆。"""
    b, a = before.split("\0"), after.split("\0")
    sb, sa = set(b), set(a)
    added, gone = [x for x in a if x not in sb], [x for x in b if x not in sa]
    return f"新增 {added[:limit]}（共 {len(added)}）；消失 {gone[:limit]}（共 {len(gone)}）"


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
        # C4a：合法值由 load_task 擋（寫錯 exit 3，不再靜默用 codex）。C5：清單＝撞牆時依序換手；impl_cli＝第一順位
        self.impl_order = implementer_order(task)
        self.impl_cli = self.impl_order[0]
        # 🔴 C5 紅線（2026-10-05）：本棒內撞過牆的 CLI 只活在這個 Run 物件的記憶體裡——不寫 task 檔、設定檔或任何
        # 停用清單；下一棒是新的 Run，一定從空集合、從第一順位開始。程式永不自動下架任何 Agent
        self.cooling: set[str] = set()
        self._handoffs_from = len(self.state.handoffs)  # 推播／commit 訊息只講這一次執行的換手（resume 會帶著舊紀錄）

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
        """記一次 CLI 呼叫（STATE＋帳本）。C5（2026-10-05）起遇到 rate_limit 不再 raise：由 run() 看 failure_class
        決定換手（實作者）或停下（審查者），撞牆一律走 escalate 出口（exit 2），不再當例外中止（exit 3）。"""
        self.state.calls.append(asdict(rec)); self.save()
        ledger_append({"ts": now(), "task": self.t["id"], "role": rec.role, "cli": rec.cli, "round": rec.round,
                       "ok": rec.ok, "failure_class": rec.failure_class, "seconds": rec.seconds, "usage": rec.usage})

    def log(self, msg: str, prefix: str = "RELAY") -> None:
        emit(prefix, msg, self.dir / "relay.log")

    def abort(self, reason: str, notify: bool = True) -> None:
        """例外中止也要落檔（2026-10-05，C1）：以前 RuntimeError 由 main() 接住後不寫 STATE，階段停在中止前
        那格（例如 review），--status 看起來像還在跑。C2：notify=True 時 STATE／CURRENT 都寫完才推播；
        KeyboardInterrupt（人在場）呼叫端傳 notify=False。C5（2026-10-05）起撞牆不再走這裡（改走 run() 的
        escalate 出口），原本給撞牆用的 kind 參數已拿掉。"""
        self.state.abort_reason = reason[:500]
        self.save("aborted")
        self.write_current(f"# CURRENT\n\n任務 {self.t['id']} 中止：{reason[:500]}；看 relay.log。\n")
        self.log(f"任務中止：{reason[:500]}")
        if notify:
            self.notify("aborted", reason=reason)

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
                    "branch": self.state.branch, "minutes": minutes, "handoff": self.handoff_summary(), **info}
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

    def handoff_summary(self) -> str:
        """這一次執行裡的同輪換手，例如 "codex→claude"；沒有＝""（C5，2026-10-05）。推播第 3 行與 commit 訊息用。
        只算 reason=rate_limit 的真換手；略過（cooling／cooldown）不算，那些在 HANDOFF.md 的換手紀錄看得到。"""
        return "、".join(f"{h.get('from')}→{h.get('to')}" for h in self.state.handoffs[self._handoffs_from:]
                         if isinstance(h, dict) and h.get("reason") == "rate_limit")

    def walled_clis(self) -> list[str]:
        """本棒撞過牆的實作者，依偏好順序（blocker 與推播用）。"""
        return [c for c in self.impl_order if c in self.cooling]

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
            if Path(self.wt).exists():  # 2026-10-05：dry-run 也驗，不符就停（exit 3），免得 dry 綠、真跑才擋
                bad = worktree_problem(self.wt, t["branch"])
                if bad:
                    raise RuntimeError(bad)
                self.log(f"[dry] worktree {self.wt} 已存在且在 {t['branch']}，會沿用")
            else:
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
            bad = worktree_problem(self.wt, t["branch"])  # 2026-10-05：沿用前先驗分支與根目錄，不符就大聲停
            if bad:
                raise RuntimeError(bad)
            self.log(f"worktree 已存在且在 {t['branch']}，沿用 {self.wt}")
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
    def impl_prompt(self, rnd: int, feedback: str, stem: str | None = None) -> str:
        """組實作者 prompt 並落檔 {stem}_prompt.md（預設 impl_r{rnd}_prompt.md；2026-10-05 從 implement 抽出：
        測試替身與真實作共用同一份組法）。C5：換手時 stem 帶 _h{attempt}_{cli}，不覆蓋第一次的 prompt。"""
        spec = Path(self.t["spec_file"]).read_text(encoding="utf-8")
        prompt = IMPL_RULES + "\n【規格】\n" + spec
        if feedback.startswith("【換手說明】"):
            # C5：換手說明自成一段放最前面；其餘（上一輪發現／人工意見）照下面原本的規則加標題，
            # 不然「【上一輪審查／驗證的發現…】」的帽子會蓋在換手說明上、第一輪換手時還會變成空標題
            note, _, feedback = feedback.partition("\n\n")
            prompt += "\n\n" + note
        if feedback:
            # C6：resume 第一輪的 feedback 自帶「【人工審查意見…】」標題，不再套「上一輪審查」的帽子
            head = "" if feedback.startswith("【人工審查意見") else "【上一輪審查／驗證的發現，請逐條修正後再回報】\n"
            prompt += "\n\n" + head + feedback
        (self.dir / f"{stem or impl_stem(rnd)}_prompt.md").write_text(prompt, encoding="utf-8")
        return prompt  # C4a（2026-10-05）起 prompt 走 stdin，C6 那條「超過 30,000 字元命令列放不下」的警告已拿掉

    def guarded_implement(self, rnd: int, feedback: str, cli: str | None = None, attempt: int = 0):
        """實作者呼叫一律走這裡（C4a，2026-10-05）：前後各取一次生產目錄快照，不同就 ProductionTouched（決策 D13）。
        兩種實作者都套用。實作者呼叫本身丟例外時也先比一次——生產目錄被動過比那個例外更要緊。
        誤報（別的 session／服務在這段時間寫了生產目錄）的代價是重跑；漏報的代價是紅線失守。"""
        cli = cli or self.impl_cli
        self.state.implementer = cli
        prod = self.t.get("production_dir")
        if self.dry:
            self.log(f"[dry] 生產目錄守門：{prod or '（未設 production_dir，停用）'}")
            return self.implement(rnd, feedback, cli=cli, attempt=attempt)
        before = prod_snapshot(prod)
        if before is None and prod:
            self.log(f"⚠ production_dir {prod} 不存在或不是 git repo：生產目錄守門停用（只剩 worktree ≠ 生產目錄的檢查）")
        try:
            res = self.implement(rnd, feedback, cli=cli, attempt=attempt)
        except Exception:
            self._check_prod(prod, before, cli)
            raise
        self._check_prod(prod, before, cli)
        return res

    def _check_prod(self, prod: str | None, before: str | None, cli: str) -> None:
        if before is None:
            return
        after = prod_snapshot(prod)
        if after != before:
            raise ProductionTouched(f"{IMPL_NAMES.get(cli, cli)} 實作期間生產目錄 {prod} 有變動（紅線，整棒中止）："
                                    f"{snapshot_delta(before, after or '')}。請人工檢查生產目錄；"
                                    "若是別的 session／服務同時寫入造成的誤報，確認後重跑即可")

    # ---- 2b. 換手（C5，2026-10-05）--------------------------------------------------------------
    # 🔴 紅線（README「換手」同）：冷卻只存在於 (a) 本棒記憶體 self.cooling、(b) 每棒開始時由帳本即時計算的時間窗；
    # 這一段永不寫 task 檔、設定檔或任何「停用清單」，每次跳過／換手都印一行 [JUDGE] 並記入 state.handoffs。
    def pick_cli(self, rnd: int, recent: dict[str, datetime], cd: int) -> str | None:
        """這一輪的第一位實作者（pick_implementer）；被略過的每一支都印 [JUDGE] 並記一筆 state.handoffs。"""
        cli = pick_implementer(self.impl_order, self.cooling, recent, cd)
        if cli is None:
            return None
        for c in self.impl_order[:self.impl_order.index(cli)]:
            if c in self.cooling:
                entry, why = {"reason": "cooling"}, f"{c} 本棒已回過 rate_limit（只限本棒）"
            else:
                ts = recent[c].strftime("%Y-%m-%dT%H:%M:%S%z")
                entry = {"reason": "cooldown", "last_rate_limit": ts}
                why = f"{c} 於 {ts} 回過 rate_limit，在 {cd} 分帳本冷卻窗內（即時計算的建議）"
            self.state.handoffs.append({"round": rnd, "from": c, "to": cli, **entry, "ts": now()})
            self.log(f"略過 {c}，本輪實作者用 {cli}：{why}；不下架任何 Agent，下一棒仍從 {self.impl_order[0]} 開始", prefix="JUDGE")
        avail = [c for c in self.impl_order if c not in self.cooling]
        if cd > 0 and all(c in recent for c in avail):
            self.log(f"帳本冷卻：可用的實作者 {'、'.join(avail)} 都在 {cd} 分內回過 rate_limit，仍用 {cli}"
                     "（冷卻只是建議，永不讓任務無人可用）", prefix="JUDGE")
        return cli

    def log_rate_limit(self, call: dict) -> None:
        """撞牆當下的 [JUDGE] 一行：原始輸出在哪；fixtures/ 還沒有該 CLI 的真樣本時，提示人確認後收進去。"""
        cli = call.get("cli") or "?"
        hint = "" if has_rate_limit_fixture(cli) else (
            f"——fixtures/ 還沒有 {cli} 的 rate_limit 真樣本（真 429 至今未觀察到）：請人工確認這份輸出真的是額度限制，"
            f"再照 fixtures/README.md 收成 fixtures/{cli}_rate_limit.*，並補判定器契約測試")
        self.log(f"🔴 {cli} 回 rate_limit（{call.get('reason')}）；原始輸出 {call.get('stdout_file') or '?'}{hint}", prefix="JUDGE")

    def implement_with_handoff(self, rnd: int, feedback: str, recent: dict[str, datetime], cd: int) -> tuple[bool, str]:
        """一輪的實作者呼叫＋撞牆換手。judge 判 rate_limit → 該 CLI 進本棒 cooling，同一輪改由下一順位接手：
        不消耗輪數（每輪最多 len(impl_order)-1 次）、worktree 不重置（D9：半成品保留，HANDOFF_NOTE 告知接手者）。
        可用的都撞牆 → state.verdict="rate_limit"，呼叫端停下（exit 2）。其他失敗類型照舊交給呼叫端「下一輪重試」。"""
        cli = self.pick_cli(rnd, recent, cd)
        if cli is None:  # 防呆：全部撞牆時上一輪就已停下，照理到不了這裡
            self.state.verdict = "rate_limit"
            return False, ""
        ok, report, _ = self.guarded_implement(rnd, feedback, cli=cli)
        attempt = 0
        while not ok and not self.dry:
            last = self.state.calls[-1] if self.state.calls else {}
            if last.get("role") != "implementer" or last.get("failure_class") != "rate_limit":
                break
            self.cooling.add(cli)
            self.log_rate_limit(last)
            nxt = pick_implementer(self.impl_order, self.cooling, {}, 0)
            if nxt is None:
                self.state.verdict = "rate_limit"
                self.log(f"可用的實作者都撞牆（{'、'.join(self.walled_clis())}），停下給人；"
                         "不下架任何 Agent，額度恢復後重跑仍從第一順位開始", prefix="JUDGE")
                return False, report
            self.state.handoffs.append({"round": rnd, "from": cli, "to": nxt, "reason": "rate_limit", "ts": now(),
                                        "stdout_file": last.get("stdout_file") or ""})
            self.save()
            self.log(f"換手：{cli} → {nxt}（同一輪、不消耗輪數、worktree 不重置；只限本棒，下一棒仍從 "
                     f"{self.impl_order[0]} 開始，不下架任何 Agent）", prefix="JUDGE")
            prev, cli, attempt = cli, nxt, attempt + 1
            ok, report, _ = self.guarded_implement(rnd, HANDOFF_NOTE.format(prev=IMPL_NAMES.get(prev, prev)) + feedback,
                                                   cli=cli, attempt=attempt)
        return ok, report

    def implement(self, rnd: int, feedback: str, cli: str | None = None, attempt: int = 0) -> tuple[bool, str, dict | None]:
        """呼叫實作者一次（C4a 起 codex｜claude 可插拔；prompt 一律走 stdin）。attempt>0 是同一輪換手（C5）。"""
        cli = cli or self.impl_cli
        stem = impl_stem(rnd, attempt, cli)
        prompt = self.impl_prompt(rnd, feedback, stem=stem)
        out_last = self.dir / f"{stem}_last_message.md"
        so, se, ex = (self.dir / f"{stem}.{k}.txt" for k in ("stdout", "stderr", "exit"))
        ic = build_impl_command(cli, self.wt, out_last, model=(self.t.get("implementer_models") or {}).get(cli),
                                effort=(self.t.get("implementer_effort") or {}).get(cli))
        if self.dry:
            self.log(f"[dry] 實作者 {IMPL_NAMES[cli]}：{' '.join(ic.argv[1:])}（round {rnd}，prompt {len(prompt)} 字走 stdin）")
            return True, "(dry)", None
        self.log(f"實作者 {IMPL_NAMES[cli]} 開跑（round {rnd}）…")
        out_last.unlink(missing_ok=True)  # 2026-10-06：同任務重跑沿用 runs/<id>/，codex 在寫 -o 前失敗會把上次的報告檔當成這次的
        t0 = time.monotonic()
        cp = stream(ic.argv, ic.prefix, ic.line_fn, cwd=self.wt, timeout=self.t.get("impl_timeout", 1500),
                    logf=self.dir / "relay.log", input_text=prompt, drop_env=ic.drop_env)
        so.write_text(cp.stdout, encoding="utf-8"); se.write_text(cp.stderr, encoding="utf-8"); ex.write_text(f"exit={cp.returncode}", encoding="utf-8")
        v = judge.judge(cli, cp.stdout, cp.stderr, cp.returncode)
        rec = CallRecord("implementer", rnd, v.ok, v.reason, cp.returncode, round(time.monotonic() - t0, 1), v.usage, str(so),
                         cli=cli, failure_class=v.failure_class)
        self.record(rec)
        self.log(f"實作者結束：{'OK' if v.ok else 'FAIL'}（{rec.seconds}s，{v.reason}）usage={v.usage}")
        if cli == "codex":  # codex 的最終訊息在 -o 檔；claude 的在 stream-json 最後的 result 物件（判定器已抽出）
            report = out_last.read_text(encoding="utf-8") if out_last.exists() else (v.result_text or "")
        else:
            report = v.result_text or ""
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
            # C5：同一輪可能有兩次實作者呼叫（換手），附上 CLI 與失敗類別才分得出誰是誰
            usage_lines.append(f"- round {c['round']} {c['role']}（{c.get('cli') or '?'}）: "
                               f"{'OK' if c['ok'] else 'FAIL' + (' ' + c['failure_class'] if c.get('failure_class') else '')} "
                               f"{c['seconds']}s, input_total={u.get('input_tokens_total')} output={u.get('output_tokens')}")
        handoff_line = ""
        if self.state.handoffs:  # C5：「用量」節前一行換手紀錄（無則省略）
            items = []
            for h in self.state.handoffs:
                if h.get("reason") == "rate_limit":
                    items.append(f"round {h.get('round')} {h.get('from')}→{h.get('to')}（{h.get('from')} 回 rate_limit；"
                                 f"原始輸出 `{h.get('stdout_file') or '?'}`"
                                 f"{'' if has_rate_limit_fixture(str(h.get('from'))) else '，若是第一個真樣本請確認後收進 fixtures/'}）")
                elif h.get("reason") == "cooldown":
                    items.append(f"round {h.get('round')} 略過 {h.get('from')} 改用 {h.get('to')}（帳本冷卻：{h.get('last_rate_limit')} 回過 rate_limit）")
                else:
                    items.append(f"round {h.get('round')} 略過 {h.get('from')} 改用 {h.get('to')}（本棒已撞牆）")
            handoff_line = "換手紀錄：" + "；".join(items) + "（只限該棒；不下架任何 Agent）\n\n"
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

{handoff_line}## 用量（每次呼叫，判定器抽取）
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
        第一輪的 feedback＝人工意見（＋上一輪未解決的發現），不建 worktree、不跑 prebuild。
        C5（2026-10-05）：實作者撞牆在同一輪換手（implement_with_handoff）；可用的實作者都撞牆或審查者撞牆
        → verdict rate_limit／reviewer_rate_limit，走 escalate 出口回 2（不再丟例外、不再 exit 3）。"""
        self.log(f"任務 {self.t['id']}：{self.t.get('title', '')}")
        rounds = int(self.t.get("max_rounds", 2)) if rounds is None else rounds
        last = start_round + rounds - 1
        if resume:
            self.log(f"人工意見回灌（第 {len(self.state.resumed)} 次 resume）：從第 {start_round} 輪接續，最多到第 {last} 輪")
        self.prepare(resume=resume)
        # D11：已 commit 過的棒 resume → 在同一 branch 疊新 commit；審查看整個任務的累積 diff，commit 只收增量
        review_base = self.review_base() if (resume and self.state.commit and not self.dry) else ""
        feedback, impl_report, verify_summary, review_text, paths = initial_feedback, "", "", "", []
        # C5（2026-10-05）：帳本冷卻窗每棒只算一次（決策 D8 預設 0＝關，不讀帳本）；本棒 cooling 在 __init__ 是空集合
        cd = int(self.t.get("handoff_cooldown_minutes", 0))
        recent = ledger_recent_rate_limits(datetime.now().astimezone(), cd)
        if len(self.impl_order) > 1 or cd > 0:
            self.log(f"實作者順序 {' → '.join(self.impl_order)}：judge 判 rate_limit 才同一輪換手（不消耗輪數、只限本棒）；"
                     f"帳本冷卻 {f'{cd} 分' if cd else '關'}", prefix="JUDGE")
        for rnd in range(start_round, last + 1):
            self.state.round = rnd
            self.write_current(f"# CURRENT\n\n任務 {self.t['id']} round {rnd}/{last}：實作中。worktree `{self.wt}`。\n")
            self.save("implement")
            ok, impl_report = self.implement_with_handoff(rnd, feedback, recent, cd)
            if self.state.verdict == "rate_limit":  # 可用的實作者都撞牆：停下給人（不再開下一輪）
                break
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
            if self.dry:
                # 2026-10-06：dry-run 不碰 worktree——以前這三步照跑，沿用既有 worktree 時會把人暫存好的 index reset 掉
                changed_now, gate = [], {}
                self.log("[dry] 不碰 worktree：跳過 git reset／改動清單／簽章閘門（下面的審查判準以空輸入計算，真跑才準）")
            else:
                git(self.wt, "reset", "-q")
                changed_now = self.worktree_changes()
                gate = iface_gate.gate_worktree(self.wt, self.t["base_branch"])
            self.state.iface_gate = gate
            verify_failed_any = any(r["exit"] != 0 for r in self.state.verify)
            need_review, why = decide_review(self.t, changed_now, diff, verify_failed_any, gate)
            self.state.diff_stats = {"files": len(changed_now), "lines": diff_line_count(diff)}
            self.state.review_decisions.append({"round": rnd, "need_review": need_review, "why": why,
                                                "changed": changed_now, "breaking": sum(len(g["breaking"]) for g in gate.values())})
            self.save()
            self.log(f"審查判準：{'送審' if need_review else '跳過審查'}——{why}", prefix="JUDGE")
            nc_ok, nc_summary = self.negative_control(rnd) if v_ok else (True, "")
            self.state.negctl.append({"round": rnd, "ok": nc_ok})
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
                rev = next((c for c in reversed(self.state.calls) if c.get("role") == "reviewer"), {})
                if rev.get("failure_class") == "rate_limit":
                    # C5 決策 D10：審查者撞牆不換別家審（換家會改變審查標準），停下推播
                    self.state.verdict = "reviewer_rate_limit"
                    self.log_rate_limit(rev)
                    self.log("審查者撞牆，停止（不開下一輪、不換別家審）；等額度恢復後重跑或 --resume", prefix="JUDGE")
                else:
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
            # 決策 D15（2026-10-05）：拿掉寫死的 Co-Authored-By 行（以前把 Codex 的產出記成 Claude 共同作者），改在內文寫實際角色
            who = IMPL_NAMES.get(self.state.implementer or self.impl_cli, self.state.implementer or self.impl_cli)
            hs = self.handoff_summary()  # C5：換手後 who＝實際的最後實作者，並註明途中換手
            who += f"（途中 {hs} 換手）" if hs else ""
            msg = f"{self.t.get('title', self.t['id'])}\n\n（編排器 relay.py：{who} 實作、agy 審查、驗證指令全過；task {self.t['id']}{again}）\n"
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
        verdict, tid = self.state.verdict, self.t["id"]
        walled = "、".join(self.walled_clis()) or "?"
        rev_cli = next((c.get("cli") for c in reversed(self.state.calls) if c.get("role") == "reviewer"), None) or "agy"
        if verdict == "review_tool_failure":
            blocker = (f"審查工具故障（round {self.state.round}：agy_review 沒有回傳 JSON／子行程崩潰）。實作與驗證結果見上；"
                       "修好 tools/agy_review.py 後重跑 relay，或人工審 review_r*_diff.txt，或寫 human_notes.md（例如「請照原樣，"
                       "只需重新審查」）後 `relay.py --resume` 接續；worktree 改動保留、未 commit。")
        elif verdict == "rate_limit":  # C5：清單裡每一支實作者都撞牆
            blocker = (f"`{walled}` 回 rate_limit：可用的實作者都撞牆（round {self.state.round}）；worktree 保留未 commit"
                       f"（可能有實作者中途留下的半成品）。等額度恢復後重跑，或寫 human_notes.md 後 `relay.py --resume {tid}`。"
                       f"relay 不下架任何 Agent：下一棒仍從 {self.impl_order[0]} 開始。")
        elif verdict == "reviewer_rate_limit":  # C5 決策 D10：審查者撞牆停下，不換別家審
            blocker = (f"審查者 `{rev_cli}` 回 rate_limit（round {self.state.round}；審查者撞牆不換別家審）。實作與驗證結果見上；"
                       f"worktree 保留未 commit。等額度恢復後重跑，或寫 human_notes.md（例如「請照原樣，只需重新審查」）後 "
                       f"`relay.py --resume {tid}`。")
        else:
            blocker = f"{self.state.round} 輪未收斂（驗證或審查不過），已停止；worktree 保留供人接手。"
        self.handoff("escalate", impl_report, verify_summary, review_text, paths, blocker)
        what = "撞牆停下" if verdict in RATE_LIMIT_VERDICTS else "未收斂"
        self.write_current(f"# CURRENT\n\n任務 {tid} **{what}**，已升給人。看 HANDOFF.md。\n")
        self.log(f"{what}，升給人")
        if verdict == "review_tool_failure":
            fc = next((c.get("failure_class") for c in reversed(self.state.calls) if c.get("role") == "reviewer"), None)
            self.notify("review_tool_failure", failure_class=fc)
        elif verdict == "rate_limit":
            self.notify("rate_limit", cli=walled, role="implementer")
        elif verdict == "reviewer_rate_limit":
            self.notify("rate_limit", cli=rev_cli, role="reviewer")
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


def _check_candidates(task: dict) -> None:
    """C4（2026-10-05）：candidates 欄位驗證——長度 2～MAX_CANDIDATES，每項 implementer 必填且是單一字串（候選模式不換手），
    model／effort 選填；每份候選的 id 要合法、worktree 不得等於生產目錄（原本只檢主 worktree）。缺欄位＝一般單棒。"""
    cands = task.get("candidates")
    if cands is None:
        return
    if not (isinstance(cands, list) and 2 <= len(cands) <= MAX_CANDIDATES):
        raise TaskError(f"candidates 必須是 2～{MAX_CANDIDATES} 項的清單（成本上限寫死；只想跑一份就拿掉這個欄位）；給的是 {cands!r}")
    for k, c in enumerate(cands, 1):
        if not isinstance(c, dict) or set(c) - {"implementer", "model", "effort"}:
            raise TaskError(f"candidates[{k}] 必須是 {{implementer, model?, effort?}} 物件（給的是 {c!r}）")
        if not (isinstance(c.get("implementer"), str) and c["implementer"] in IMPLEMENTERS):
            raise TaskError(f"candidates[{k}].implementer 不合法：{c.get('implementer')!r}（{' / '.join(IMPLEMENTERS)}；候選模式不換手，不接受清單）")
        if "model" in c and not (isinstance(c["model"], str) and c["model"].strip()):
            raise TaskError(f"candidates[{k}].model 必須是非空字串")
        if "effort" in c and not (c["implementer"] == "claude" and c["effort"] in CLAUDE_EFFORTS):
            raise TaskError(f"candidates[{k}].effort 只有 claude 可用，且只能是 {' / '.join(CLAUDE_EFFORTS)}（給的是 {c['effort']!r}）")
    prod = task.get("production_dir")
    for ct in derive_candidate_tasks(task):
        if not valid_task_id(ct["id"]):
            raise TaskError(f"候選 id {ct['id']!r} 不合法（task id 太長？候選要加 .cK 後綴）")
        if prod and Path(ct["worktree"]).resolve() == Path(prod).resolve():
            raise TaskError(f"候選 {ct['id']} 的 worktree 不得等於生產目錄")
        m = ct.get("implementer_models")  # 2026-10-06：候選用 claude 也要指定模型（candidates[k].model 或 task 的 implementer_models.claude）
        if ct["implementer"] == "claude" and not (isinstance(m, dict) and m.get("claude")):
            raise TaskError(f"候選 {ct['id']} 用 claude 但沒指定模型：寫 candidates[k].model 或 task 的 implementer_models.claude（成本要事前可見）")


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
    # C4a（2026-10-05）：以前 implementer 欄位從沒被讀過，寫什麼都靜默用 Codex；現在寫錯就 exit 3。
    # C5（2026-10-05）：也可以是不重複的非空清單＝撞牆時依序換手（重複沒有意義：撞過牆的本棒不會再用）
    impl = task.get("implementer", "codex")
    order = [impl] if isinstance(impl, str) else impl
    if not (isinstance(order, list) and order and all(isinstance(c, str) and c in IMPLEMENTERS for c in order)
            and len(set(order)) == len(order)):
        raise TaskError(f"implementer 不合法：{impl!r}（{' / '.join(IMPLEMENTERS)}，或不重複的清單如 "
                        f"{json.dumps(list(IMPLEMENTERS))}＝撞牆時依序換手；省略＝codex）")
    _check_candidates(task)
    cd = task.get("handoff_cooldown_minutes", 0)
    if isinstance(cd, bool) or not isinstance(cd, int) or cd < 0:
        raise TaskError(f"handoff_cooldown_minutes 必須是 ≥0 的整數分鐘（0＝關，預設）；給的是 {cd!r}")
    for key, allowed in (("implementer_models", IMPLEMENTERS), ("implementer_effort", ("claude",))):
        v = task.get(key)
        if v is not None and not (isinstance(v, dict) and all(k in allowed and isinstance(x, str) and x.strip()
                                                               for k, x in v.items())):
            raise TaskError(f"{key} 必須是 {{實作者: 非空字串}}，實作者只能是 {' / '.join(allowed)}（給的是 {v!r}）")
    # 2026-10-06：implementer 含 claude 卻沒指定模型 → 會跟著使用者的 Claude Code 設定走（可能是最貴的那個），成本要事前可見
    if "candidates" not in task and "claude" in order and not (task.get("implementer_models") or {}).get("claude"):
        raise TaskError('implementer 含 claude 時必須在 implementer_models 指定模型（例如 {"claude": "sonnet"}）；'
                        "不指定會用你的 Claude Code 預設模型，成本不可見")
    eff = (task.get("implementer_effort") or {}).get("claude")
    if eff is not None and eff not in CLAUDE_EFFORTS:
        raise TaskError(f"implementer_effort.claude 不合法：{eff!r}（只能是 {' / '.join(CLAUDE_EFFORTS)}）")
    return task


def required_clis(task: dict) -> dict:
    """環境自檢要哪幾支 CLI（C4a，2026-10-05）→ paths.check_all 的 keyword 參數。
    以前一律要 codex＋agy：review.policy=never 也要求裝 agy（與 README 不符）、claude 實作者也要求裝 codex。
    C5（2026-10-05）：implementer 清單裡的每一支都要在——換手的備援要能用，缺了要在開跑前大聲說，不是撞牆時才發現。
    C4（2026-10-05）：有 candidates 時 task 的 implementer 被忽略，改算 candidates[*].implementer。"""
    cands = task.get("candidates")
    impl = {c["implementer"] for c in cands if isinstance(c, dict)} if cands else set(implementer_order(task))
    return {"need_codex": "codex" in impl, "need_claude": "claude" in impl,
            "need_agy": task["review"].get("policy", "always") != "never"}


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
    bad = worktree_problem(wt, task["branch"])  # 2026-10-05：與一般啟動共用同一個檢查（根目錄＋分支）
    if bad:
        raise TaskError(bad)
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


# ---- C4（2026-10-05）：best-of-N——同任務多份候選，依序跑完、機器排名，人只看第一名 --------------------------
REVIEW_RANK = {"approve": 0, "skipped": 1, "changes": 2, "tool_failure": 3, "none": 4}
CAND_ID_RE = re.compile(r"\.c(\d+)$")


def derive_candidate_tasks(task: dict) -> list[dict]:
    """task（含 candidates）→ 每份候選一個獨立的單棒 task（純函式，不改輸入）。第 k 份：id／branch／worktree 加 -c{k} 後綴
    （id 用 .c{k}，--status 看得到、--resume 可單獨指定），implementer 一律單一字串——候選模式強制關閉換手（C5），
    不然兩份候選會在同一輪互相「換成對方的 CLI」而失去比較意義。model／effort 有給才覆蓋，沒給就沿用 task 本身的設定。
    刪 candidates、加 _group 標回群組 id。"""
    out = []
    for k, cand in enumerate(task["candidates"], 1):
        ct = copy.deepcopy(task)
        impl = cand["implementer"]
        ct.pop("candidates", None)
        ct["id"], ct["_group"] = f"{task['id']}.c{k}", task["id"]
        ct["branch"], ct["worktree"] = f"{task['branch']}-c{k}", f"{task['worktree']}-c{k}"
        ct["implementer"] = impl
        if cand.get("model"):
            ct["implementer_models"] = {**(ct.get("implementer_models") or {}), impl: cand["model"]}
        if cand.get("effort"):
            ct["implementer_effort"] = {**(ct.get("implementer_effort") or {}), "claude": cand["effort"]}
        out.append(ct)
    return out


def candidate_review(s: dict) -> str:
    """一份候選的審查結果 → skipped／tool_failure／approve／changes／none。只看最後一輪（前幾輪的退回已被後面蓋過）。"""
    rnd = s.get("round") or 0
    ds = [d for d in (s.get("review_decisions") or []) if isinstance(d, dict) and d.get("round") == rnd]
    if any(d.get("need_review") is False for d in ds):
        return "skipped"
    rv = [c for c in (s.get("calls") or []) if isinstance(c, dict) and c.get("role") == "reviewer" and c.get("round") == rnd]
    if rv and rv[-1].get("ok") is False:
        return "tool_failure"
    ap = [d for d in ds if "approved" in d]
    if ap:
        return "approve" if ap[-1]["approved"] else "changes"
    return "none"


def rank_candidates(states: list[dict]) -> list[dict]:
    """各候選的 STATE（dict）→ 排名列（第一名在最前）。純函式、只讀 STATE：不碰 worktree／branch。
    STATE 沒有的資訊用保守預設；run_group 會替 STATE 補 _verify_total（沒跑到 verify 時的分母）。
    排序鍵見規格 §7b，另在收斂之後加「aborted 墊後」（中止的候選即使 verify 全過也不可用，不能排在未收斂但完整的候選前面）：
    收斂 > 非 aborted > verify 過的條數 > negctl > 審查 > 介面破壞 > 未申報 > diff 行數 > 輪數 > 秒數 > cand 序。"""
    rows = []
    for s in states:
        rnd = s.get("round") or 0
        ver = [v for v in (s.get("verify") or []) if isinstance(v, dict) and v.get("round") == rnd]
        v_pass = sum(1 for v in ver if v.get("exit") == 0)
        v_total = len(ver) if ver else int(s.get("_verify_total") or 0)
        nc = [n for n in (s.get("negctl") or []) if isinstance(n, dict) and n.get("round") == rnd]
        verdict = s.get("verdict") or ""
        if s.get("phase") == "not_run":  # 2026-10-05：整組停止時沒啟動的候選（run_group 補的合成 STATE）
            verdict = "not_run"
        elif s.get("phase") == "aborted" or not verdict:
            verdict = "aborted" if s.get("phase") == "aborted" else "incomplete"
        ap = [d for d in (s.get("review_decisions") or []) if isinstance(d, dict) and d.get("round") == rnd and "approved" in d]
        m = CAND_ID_RE.search(s.get("task_id") or "")
        rows.append({
            "cand": int(m.group(1)) if m else 0, "implementer": s.get("implementer") or "?", "verdict": verdict,
            "verify_pass": v_pass, "verify_total": v_total, "negctl_ok": all(n.get("ok") for n in nc),
            "review": candidate_review(s),
            "breaking": sum(len((g or {}).get("breaking", [])) for g in (s.get("iface_gate") or {}).values()),
            "unreported": len(ap[-1].get("unreported") or []) if ap else 0,
            "diff_lines": int((s.get("diff_stats") or {}).get("lines", 0)), "rounds": rnd,
            "seconds": round(sum(c.get("seconds") or 0 for c in (s.get("calls") or []) if isinstance(c, dict)), 1),
            "branch": s.get("branch") or "", "commit": s.get("commit") or ""})
    rows.sort(key=lambda r: (r["verdict"] != "converged", r["verdict"] == "not_run", r["verdict"] == "aborted", -r["verify_pass"], not r["negctl_ok"], REVIEW_RANK[r["review"]],
                             r["breaking"], r["unreported"], r["diff_lines"], r["rounds"], r["seconds"], r["cand"]))
    return rows


def render_ranking(gid: str, rows: list[dict], tasks: dict[int, dict], stopped: str = "") -> str:
    """RANKING.md 內容。清理指令只是印給人的文字（relay 紅線：從不代執行 worktree remove／branch -D）。
    stopped（2026-10-05）：整組提前停止的原因；有值時檔頭先講，未執行（not_run）的候選不列清理指令（它們沒有 worktree／分支）。"""
    first = rows[0]
    L = [f"# {gid} best-of-{len(rows)} 排名", ""]
    if stopped:
        nr = [f"c{r['cand']}" for r in rows if r["verdict"] == "not_run"]
        L += [f"> 🔴 **{stopped}**。未執行（not_run）：{'、'.join(nr) or '無'}。先檢查生產目錄，再決定要不要重跑；"
              "下表只是已跑部分的排名，不要直接拿來合併。", ""]
    L += [
         "機器已跑完每份候選的 verify＋閘門＋審查再排名；人只需要看第一名。排序：收斂 > verify 過的條數 > 陰性對照 > 審查 > "
         "介面破壞數 > 未申報數 > diff 行數 > 輪數 > 秒數 > 候選序。", "",
         "| 名次 | 候選 | 實作者 | verdict | verify | negctl | review | breaking | unreported | diff行 | 輪 | 秒 | 分支@commit |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        L.append(f"| {i} | c{r['cand']} | {r['implementer']} | {r['verdict']} | {r['verify_pass']}/{r['verify_total']} | "
                 f"{'ok' if r['negctl_ok'] else 'fail'} | {r['review']} | {r['breaking']} | {r['unreported']} | {r['diff_lines']} | "
                 f"{r['rounds']} | {r['seconds']} | {r['branch'] or '-'}@{r['commit'] or '-'} |")
    L.append("")
    if first["verdict"] == "converged":
        L.append(f"第一名：c{first['cand']} → 分支 `{first['branch']}` @ `{first['commit']}`；"
                 f"人只看 `runs/{gid}.c{first['cand']}/HANDOFF.md`。")
    else:
        L.append(f"沒有候選收斂（排最前的是 c{first['cand']}，verdict={first['verdict']}）；"
                 f"先讀 `runs/{gid}.c{first['cand']}/HANDOFF.md` 看卡點。")
    L += ["", "其餘候選的 worktree 與分支都保留。清理指令（relay **不**代執行，人確認不要了再自己跑）：", "", "```"]
    for r in rows[1:]:
        if r["verdict"] == "not_run":
            continue
        ct = tasks[r["cand"]]
        L += [f'git -C "{ct["repo"]}" worktree remove "{ct["worktree"]}"', f'git -C "{ct["repo"]}" branch -D "{ct["branch"]}"']
    L += ["```", "", f"第一名（c{first['cand']}）合併後，它的 worktree 與分支同理由人清理。", ""]
    return "\n".join(L)


def _notify_group(gid: str, kind: str, info: dict, a) -> str:
    """群組結束時的推播：整個群組只發這一則（候選一律 no_notify）。失敗只記 log，不改 exit code（同 Run.notify）。"""
    if getattr(a, "dry_run", False) or getattr(a, "no_notify", False):
        return ""
    try:
        result = notify.notify(kind, gid, info, config_path=notify_config_path(), ledger_path=NOTIFY_LEDGER,
                               lock_path=LOCKS / "notify.lock")
    except Exception as e:  # ponytail: 同 Run.notify，連 notify 自己的 bug 也吞掉；代價是推播可能靜默沒發，log 留一行
        result = f"推播例外：{type(e).__name__}"
    if result != "推播關閉":
        emit("RELAY", result)
    return result


def run_group(task: dict, a) -> int:
    """best-of-N：依序跑每份候選（決策 D7：不平行，整個群組只佔 1 個並行名額），全跑完讀各自的 STATE 排名，
    寫 runs/<id>/RANKING.md 與 GROUP.json，推播一則。一份候選中止（RuntimeError／其他例外）只標那一份 aborted，
    繼續下一份。排名只讀 STATE、不動任何 worktree／branch；清理指令只印不執行。exit：第一名收斂 0，否則 2。
    例外（2026-10-05）：任一候選丟 ProductionTouched（生產目錄被改）→ 該份 abort、**不再啟動後面的候選**（下一份會把已被改過的
    生產目錄當新基準，前一份造成的改動從此偵測不到）；仍寫 RANKING／GROUP（未跑的記 not_run）、推播一則 escalate、exit 3（同單棒）。
    dry-run：每份候選各走自己的 dry，只印計畫，不取鎖、不寫 RANKING／GROUP。
    ponytail: 候選之間完全串列，N 份的牆鐘時間是 N 倍；升級路徑＝改成多個名額並行跑（需先實測並行的額度與本機狀態）。"""
    gid, dry = task["id"], bool(getattr(a, "dry_run", False))
    cts = derive_candidate_tasks(task)
    tmap = {k: ct for k, ct in enumerate(cts, 1)}
    emit("RELAY", f"best-of-{len(cts)}：{gid} 依序跑 {len(cts)} 份候選（成本＝{len(cts)} 倍實作＋{len(cts)} 倍審查額度；不平行、不換手）")
    if "implementer" in task:
        emit("RELAY", "task 的 implementer 欄位在候選模式下被忽略（各候選用自己的 implementer）")
    for k, ct in tmap.items():
        mdl = (ct.get("implementer_models") or {}).get(ct["implementer"], "")
        emit("RELAY", f"  c{k}：{ct['id']}　{ct['implementer']}{('／' + mdl) if mdl else ''}　分支 {ct['branch']}　worktree {ct['worktree']}")
    gdir = HERE / "runs" / gid
    src = str(Path(a.task).resolve()) if getattr(a, "task", None) else ""
    try:
        locks = nullcontext() if dry else acquire_run_locks(task, queue=bool(getattr(a, "queue", False)))
    except runlock.LockBusy as e:
        print("relay 拒跑：", e, file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130
    t0, skipped, tfiles, stopped, stopped_id, dry_bad = time.monotonic(), set(), {}, "", "", False
    with locks:
        if not dry:
            gdir.mkdir(parents=True, exist_ok=True)
            (gdir / "RANKING.md").unlink(missing_ok=True)  # 上次的排名不可冒充這次的
            # 候選的任務檔落在群組目錄：--resume <id>.cK 要靠它（原任務檔有 candidates，載入會被當群組拒絕）
            for k, ct in tmap.items():
                tf = gdir / f"cand_{k}.task.json"
                tf.write_text(json.dumps(ct, ensure_ascii=False, indent=1), encoding="utf-8")
                tfiles[k] = str(tf)
            (gdir / "GROUP.json").write_text(json.dumps({"id": gid, "state": "running", "candidates": [c["id"] for c in cts]},
                                                       ensure_ascii=False, indent=1), encoding="utf-8")
        for k, ct in tmap.items():
            try:
                lk = nullcontext() if dry else acquire_run_locks(ct, queue=False, take_slot=False)
            except runlock.LockBusy as e:
                skipped.add(ct["id"])
                emit("RELAY", f"候選 {ct['id']} 略過（不跑、不讀它舊的 STATE）：{e}")
                continue
            with lk:
                run = None
                try:
                    run = Run(ct, dry, no_notify=True, task_file=tfiles.get(k, src))
                    if not dry:
                        run.note_interrupted_previous()
                    emit("RELAY", f"=== 候選 c{k}/{len(cts)}：{ct['id']} ===")
                    rc = run.run()
                    emit("RELAY", f"候選 {ct['id']} 結束（exit {rc}）")
                except KeyboardInterrupt:
                    if run:
                        run.abort("使用者中斷", notify=False)
                    if not dry:
                        (gdir / "GROUP.json").write_text(json.dumps({"id": gid, "state": "interrupted"}), encoding="utf-8")
                    return 130
                except ProductionTouched as e:  # 必須排在 RuntimeError 前面（它是子類）：整組停，不再啟動後面的候選
                    emit("RELAY", f"候選 {ct['id']} 中止：{e}")
                    if run:
                        run.abort(str(e), notify=False)
                    stopped, stopped_id = f"生產目錄變動，整組停止（{ct['id']} 實作期間生產目錄被改動）", ct["id"]
                    emit("RELAY", f"🔴 {stopped}；後面的候選不啟動")
                    break
                except RuntimeError as e:  # 規格外改動、worktree／prebuild 失敗…：只中止這一份
                    emit("RELAY", f"候選 {ct['id']} 中止：{e}")
                    dry_bad = dry_bad or dry  # dry-run 的中止＝計畫有問題（例如既有 worktree 分支不符），最後回 3
                    if run:
                        run.abort(str(e), notify=False)
                except Exception as e:
                    emit("RELAY", f"候選 {ct['id']} 崩潰：{type(e).__name__}: {e}")
                    if run:
                        run.abort(f"{type(e).__name__}: {e}", notify=False)
                    traceback.print_exc()
        if dry:
            emit("RELAY", "[dry] 群組只印計畫，不寫 RANKING.md")
            return 3 if dry_bad else 0
        states = []
        last_run = max((k for k, ct in tmap.items() if ct["id"] == stopped_id), default=len(tmap)) if stopped else len(tmap)
        for k, ct in tmap.items():
            s = None if ct["id"] in skipped else _read_state(HERE / "runs" / ct["id"] / "STATE.json")
            if stopped and k > last_run:  # 整組停止之後的候選：沒跑過，不讀它舊的 STATE
                s = {"task_id": ct["id"], "phase": "not_run", "abort_reason": stopped}
            s = {"task_id": ct["id"], "phase": "aborted", "abort_reason": "未執行"} if s is None else dict(s)
            s["task_id"] = ct["id"]
            s["implementer"] = s.get("implementer") or ct["implementer"]
            s["_verify_total"] = len(ct.get("verify", []))
            states.append(s)
        rows = rank_candidates(states)
        (gdir / "RANKING.md").write_text(render_ranking(gid, rows, tmap, stopped), encoding="utf-8")
        first, n = rows[0], len(rows)
        converged = first["verdict"] == "converged"
        info = {"minutes": int((time.monotonic() - t0) // 60), "group": {
            "n": n, "cand": first["cand"], "implementer": first["implementer"], "verdict": first["verdict"]}}
        if stopped:
            info["group"]["stopped"] = stopped
        res = _notify_group(gid, "escalate" if (stopped or not converged) else "ready_to_merge", info, a)
        gj = {"id": gid, "state": "stopped" if stopped else "done", "candidates": [c["id"] for c in cts],
              "ranking": rows, "first": first["cand"], "notified": res}
        if stopped:
            gj["stopped"] = stopped
            gj["not_run"] = [tmap[r["cand"]]["id"] for r in rows if r["verdict"] == "not_run"]
        (gdir / "GROUP.json").write_text(json.dumps(gj, ensure_ascii=False, indent=1), encoding="utf-8")
        emit("RELAY", f"排名完成：第一名 c{first['cand']}（{first['implementer']}，{first['verdict']}）；讀 runs/{gid}/RANKING.md")
        if stopped:
            return 3
        return 0 if converged else 2


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
                  f"seconds={t['seconds']:.0f} rate_limited={t['rate_limited']} last_rate_limit={t['last_rate_limit'] or '-'}")
        return 0
    if a.status:
        print(status_report(None if a.all else STATUS_LIMIT))
        return 0
    if a.rounds is not None and not a.resume:
        print("--rounds 只能配 --resume 使用（一般棒的輪數寫在任務檔 max_rounds）", file=sys.stderr)
        return 3
    if not a.task and not a.resume:
        ap.error("缺 task 檔")
    plan = None
    try:
        if a.resume:
            plan = prepare_resume(a.resume, a.task, a.rounds)
            task = plan.task
        else:
            task = load_task(Path(a.task))
    except TaskError as e:
        print(("relay 拒絕 resume：" if a.resume else "") + str(e), file=sys.stderr)
        return 3
    # C4a（2026-10-05）：先讀任務才知道要哪幾支 CLI（見 required_clis）；dry-run 不呼叫 CLI，不檢查
    if not a.dry_run:
        missing = paths.check_all(**required_clis(task))
        if missing:
            print("環境缺少 CLI,無法動工:\n  - " + "\n  - ".join(missing), file=sys.stderr)
            return 3
    if task.get("candidates") and not plan:  # C4：best-of-N 群組（resume 帶 candidates 的任務已在 prepare_resume 被拒）
        return run_group(task, a)
    if plan:
        run = Run(task, a.dry_run, task_file=plan.task_file, no_notify=a.no_notify, resume_state=plan.state)
    else:
        run = Run(task, a.dry_run, task_file=str(Path(a.task).resolve()), no_notify=a.no_notify)
    return _execute(run, task, a, plan)


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
        except RuntimeError as e:  # relay 自己丟的中止：worktree／prebuild 失敗、規格外改動、等 repo 鎖逾時…（撞牆 C5 起走 run() 回 2）
            print("relay 中止：", e, file=sys.stderr)
            run.abort(str(e))
            return 3
        except Exception as e:  # 其他崩潰（git 逾時 TimeoutExpired 等）也要落檔，並留 traceback
            run.abort(f"{type(e).__name__}: {e}")
            traceback.print_exc()
            return 3


if __name__ == "__main__":
    sys.exit(main())
