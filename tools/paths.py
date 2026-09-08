"""paths.py — 解析三個 CLI 執行檔的位置,把寫死路徑抽成可攜的偵測(2026-09-08 可攜化)。

順序一律是:環境變數 → PATH(shutil.which)→ 已知安裝位置 → None。
None 代表這台機器沒裝,relay 啟動時會大聲報錯,不會跑到一半才炸。

換機器只要裝好三個 CLI 並登入,或設環境變數 RELAY_PYTHON／RELAY_CODEX／RELAY_AGY(或 AGY_EXE)。
"""
from __future__ import annotations

import glob
import os
import shutil
import sys
from pathlib import Path


def resolve_python() -> str:
    """跑 relay 的這個 python 通常就是要用的(驗證指令用同一個)。"""
    return os.environ.get("RELAY_PYTHON") or sys.executable or shutil.which("python") or "python"


def resolve_codex() -> str | None:
    env = os.environ.get("RELAY_CODEX")
    if env:
        return env
    w = shutil.which("codex") or shutil.which("codex.exe")
    if w:
        return w
    # VS Code 的 OpenAI 擴充,目錄帶版號,取字串排序最大的(通常是最新版)
    base = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".vscode" / "extensions"
    for pat in ("openai.chatgpt-*/bin/windows-x86_64/codex.exe",
                "openai.chatgpt-*/bin/*/codex.exe",
                "openai.chatgpt-*/bin/*/codex"):
        cands = sorted(glob.glob(str(base / pat)))
        if cands:
            return cands[-1]
    return None


def resolve_agy() -> str | None:
    env = os.environ.get("RELAY_AGY") or os.environ.get("AGY_EXE")
    if env:
        return env
    w = shutil.which("agy") or shutil.which("agy.exe")
    if w:
        return w
    for cand in (Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe",
                 Path(os.environ.get("USERPROFILE", str(Path.home()))) / "AppData" / "Local" / "agy" / "bin" / "agy.exe"):
        if cand.exists():
            return str(cand)
    return None


def check_all(need_codex: bool = True, need_agy: bool = True) -> list[str]:
    """回傳缺少的 CLI 清單(空＝齊全)。給 relay 啟動時檢查。"""
    missing = []
    if need_codex and not resolve_codex():
        missing.append("codex(設 RELAY_CODEX 或裝 OpenAI/Codex CLI 並確認登入)")
    if need_agy and not resolve_agy():
        missing.append("agy(設 RELAY_AGY 或裝 Antigravity CLI 並登入)")
    return missing


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print("python:", resolve_python())
    print("codex :", resolve_codex() or "(找不到)")
    print("agy   :", resolve_agy() or "(找不到)")
