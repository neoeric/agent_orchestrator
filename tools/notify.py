"""notify.py — relay 的「需要人動手才推播」（C2；只用標準函式庫）。

2026-10-05：relay 一棒常跑十幾分鐘到一小時，人不會一直盯著終端機；但推播一多就變噪音。所以這裡只做三件事：
  1. 只在五種「需要人」的出口發（見 KINDS），穩態不發；
  2. 同任務同類型 dedupe、全體每小時／每月封頂（帳本 runs/notify_ledger.jsonl）；
  3. 不內建任何通道：設定檔的 cmd 是外部指令，訊息走 stdin（UTF-8）。日後換通道只改設定檔。
設定檔不存在＝整個功能關閉（預設零行為改變）。推播失敗只記帳本與 log，永遠不 raise、不影響 relay 的結果。
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools import runlock

KINDS = ("ready_to_merge", "escalate", "review_tool_failure", "rate_limit", "aborted")
DEFAULTS = {"timeout": 30, "max_per_hour": 4, "max_per_month": 40, "dedupe_minutes": 60, "kinds": list(KINDS)}
TZ8 = timezone(timedelta(hours=8))  # 月額度以 UTC+8 曆月算（使用者所在時區）


def load_config(path: Path) -> tuple[dict | None, str]:
    """回 (設定, 說明)。檔不存在 → (None, "off")；壞掉 → (None, "設定錯誤：…")，一律視同關閉。"""
    path = Path(path)
    if not path.is_file():
        return None, "off"
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        return None, f"設定錯誤：讀不了 {path.name}（{type(e).__name__}）"
    if not isinstance(raw, dict):
        return None, "設定錯誤：最上層必須是 JSON 物件"
    cmd = raw.get("cmd")
    if not (isinstance(cmd, list) and cmd and all(isinstance(c, str) and c for c in cmd)):
        return None, "設定錯誤：cmd 必須是非空的字串陣列（不經 shell；token 不可寫在這裡）"
    cfg = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    cfg["cmd"] = cmd
    for k in ("timeout", "max_per_hour", "max_per_month", "dedupe_minutes"):
        if isinstance(cfg[k], bool) or not isinstance(cfg[k], (int, float)) or cfg[k] < 0:
            return None, f"設定錯誤：{k} 必須是非負數字"
    if not (isinstance(cfg["kinds"], list) and all(isinstance(k, str) for k in cfg["kinds"])):
        return None, "設定錯誤：kinds 必須是字串陣列"
    return cfg, "on"


def compose(kind: str, task_id: str, info: dict) -> str:
    """三行訊息。第 1、2 行各自能單獨成立（手機通知常只看得到前兩行）：哪個任務、發生什麼、要你做什麼。
    只陳述事實，不用「沒問題」之類的價值判斷詞。"""
    i = info
    if kind == "ready_to_merge":
        l1 = f"【relay】{task_id} 待合併：第 {i.get('round', '?')} 輪收斂，commit {i.get('commit') or '?'}"
        l2 = f"下一步：讀 runs/{task_id}/HANDOFF.md 後自行合併 {i.get('branch') or '該分支'}（relay 不合併）"
    elif kind == "escalate":
        l1 = f"【relay】{task_id} 未收斂：{i.get('round', '?')}/{i.get('max_rounds', '?')} 輪用完"
        # 2026-10-05（C6）：人看完卡點最常想「補一句意見再跑」，第 2 行直接給接續的指令（不必整棒重開）
        l2 = f"下一步：讀 HANDOFF.md 第 5 節卡點；可寫 runs/{task_id}/human_notes.md 後 relay.py --resume {task_id}，或修規格／放棄"
    elif kind == "review_tool_failure":
        l1 = f"【relay】{task_id} 審查工具故障（{i.get('failure_class') or '未分類'}）：實作與驗證已完成、未 commit"
        l2 = "下一步：修好審查工具（常見＝登入過期）後照 README 補審，不必重跑整棒"
    elif kind == "rate_limit":
        l1 = f"【relay】{task_id} 撞牆停下：{i.get('cli') or '?'} 回 rate_limit"
        l2 = "下一步：等額度恢復或改用另一支 CLI；worktree 保留"
    else:  # aborted
        l1 = f"【relay】{task_id} 中止：{str(i.get('reason') or '?')[:60]}"
        l2 = f"下一步：看 runs/{task_id}/relay.log 末段"
    # 單行化：訊息裡不留 \r／\n，避免第 1 行被原因文字截斷成兩行
    l1 = " ".join(l1.split())
    lines = [l1, l2]
    if i.get("minutes") is not None:
        lines.append(f"耗時 {i['minutes']} 分")
    return "\n".join(lines)


def _parse_ts(s) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=TZ8)


def decide(entries: list[dict], now: datetime, task_id: str, kind: str, cfg: dict) -> tuple[bool, str]:
    """依序判斷：kind 關閉 → 同任務同類型 dedupe → 每小時封頂 → 每月封頂。只數 sent=true 的紀錄。"""
    if kind not in cfg["kinds"]:
        return False, "kind_off"
    sent = []
    for e in entries:
        ts = _parse_ts(e.get("ts"))
        if e.get("sent") is True and ts is not None:
            sent.append((e, ts))
    dedupe = timedelta(minutes=cfg["dedupe_minutes"])
    if any(e.get("task") == task_id and e.get("kind") == kind and now - ts < dedupe for e, ts in sent):
        return False, "dedupe"
    if sum(1 for _, ts in sent if timedelta(0) <= now - ts < timedelta(hours=1)) >= cfg["max_per_hour"]:
        return False, "hourly_cap"
    n8 = now.astimezone(TZ8)
    if sum(1 for _, ts in sent if (ts.astimezone(TZ8).year, ts.astimezone(TZ8).month) == (n8.year, n8.month)) >= cfg["max_per_month"]:
        return False, "monthly_cap"
    return True, "ok"


def send(cfg: dict, text: str, env_extra: dict) -> tuple[int | None, str]:
    """呼叫外部指令，訊息走 stdin（不走 argv：避開長度上限與引號，也不讓內容出現在程序列表）。
    回 (exit code 或 None＝逾時／啟動失敗, stderr 尾 200 字)；不 raise。"""
    try:
        # 以 bytes 交付：text=True 在 Windows 會把 LF 轉成 CRLF，通道端就收到不是我們寫的訊息
        r = subprocess.run(cfg["cmd"], input=text.encode("utf-8"), timeout=cfg["timeout"], capture_output=True,
                           env={**os.environ, **env_extra})
    except subprocess.TimeoutExpired:
        return None, f"逾時（{cfg['timeout']} 秒）"
    except (OSError, ValueError) as e:
        return None, f"無法啟動：{type(e).__name__}"
    return r.returncode, (r.stderr or b"").decode("utf-8", "replace")[-200:]


def _read_entries(ledger_path: Path) -> list[dict]:
    out = []
    try:
        lines = Path(ledger_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            e = json.loads(ln)
        except ValueError:
            continue  # 壞行略過：帳本壞一行不該讓推播整個停擺
        if isinstance(e, dict):
            out.append(e)
    return out


def notify(kind: str, task_id: str, info: dict, *, config_path: Path, ledger_path: Path, lock_path: Path,
           now: datetime | None = None) -> str:
    """載入設定 → 鎖內讀帳本 → decide → send → 寫帳本 → 回一句給 relay.log 的說明。
    整段包在鎖裡：兩個 relay 同時要發時，才數得準每小時／每月上限。不 raise。"""
    cfg, why = load_config(config_path)
    if cfg is None:
        return "推播關閉" if why == "off" else f"推播設定錯誤：{why}"
    now = now or datetime.now(TZ8)
    text = compose(kind, task_id, info)
    try:
        with runlock.locked(lock_path, timeout=float(cfg["timeout"]) + 30):
            ok, reason = decide(_read_entries(ledger_path), now, task_id, kind, cfg)
            code, err = (None, "")
            if ok:
                code, err = send(cfg, text, {"RELAY_NOTIFY_KIND": kind, "RELAY_NOTIFY_TASK": task_id,
                                             "RELAY_NOTIFY_TITLE": text.split("\n", 1)[0]})
                if code != 0:
                    reason = "send_failed"
            entry = {"ts": now.isoformat(timespec="seconds"), "task": task_id, "kind": kind, "sent": ok,
                     "reason": reason, "exit": code}
            Path(ledger_path).parent.mkdir(parents=True, exist_ok=True)
            with open(ledger_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except (runlock.LockBusy, OSError) as e:
        return f"推播未發（帳本鎖或寫入失敗：{type(e).__name__}）"
    if not ok:
        return f"推播未發（{reason}）"
    if code == 0:
        return f"推播已發（{kind}）"
    return f"推播指令失敗（exit={code}）：{err.strip()[-120:]}"
