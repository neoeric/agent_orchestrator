"""paths.py — 解析三個 CLI 執行檔的位置,把寫死路徑抽成可攜的偵測(2026-09-08 可攜化)。

順序一律是:環境變數 → PATH(shutil.which)→ 已知安裝位置 → None。
None 代表這台機器沒裝,relay 啟動時會大聲報錯,不會跑到一半才炸。

換機器只要裝好三個 CLI 並登入,或設環境變數 RELAY_PYTHON／RELAY_CODEX／RELAY_AGY(或 AGY_EXE)。

2026-10-05 加 Claude CLI:resolve_claude()(環境變數 RELAY_CLAUDE)與巢狀呼叫要清掉的環境變數
CLAUDE_NESTED_ENV／claude_env()。council.py 與之後的 Claude 實作者共用。
"""
from __future__ import annotations

import glob
import os
import re
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


# 2026-10-05 在 Claude Code 對話裡巢狀呼叫 claude -p 要先清掉的環境變數(來源:fixtures/README.md)。
# 刻意列舉而不是整批刪 CLAUDE_*:那一批可能含認證變數(例如 OAuth token),刪了子行程就登不進去。
CLAUDE_NESTED_ENV = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET",
                     "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_PID",
                     "CLAUDE_CODE_ENTRYPOINT",
                     # 2026-10-06 本機實查（VS Code 擴充內的 Claude Code 2.1.290 對話）：子行程另會繼承這 6 個 session 注入變數，
                     # 都不是使用者設定用的。使用者設定變數（CLAUDE_CODE_USE_BEDROCK／CLAUDE_CODE_EFFORT_LEVEL／*_OAUTH_TOKEN…）不在此列、要保留，
                     # 所以只能明確列舉、不可用前綴整批刪
                     "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_EMIT_STARTUP_TIMING", "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
                     "CLAUDE_CODE_ENABLE_TASKS", "CLAUDE_CODE_QUESTION_PREVIEW_FORMAT", "CLAUDE_CODE_SESSION_ATTENDED",)

_THIN_SHELLS = (".cmd", ".bat", ".ps1")


def claude_env(base: dict | None = None) -> dict:
    """複製 base(預設 os.environ)並刪掉 CLAUDE_NESTED_ENV;不改動傳入的 dict。"""
    env = dict(os.environ if base is None else base)
    for k in CLAUDE_NESTED_ENV:
        env.pop(k, None)
    return env


def _version_key(name: str) -> tuple[int, ...]:
    """從目錄名抽版本號成 tuple(2.1.289 > 2.1.30);抽不到排最後。字串排序會把 2.1.30 排在 2.1.289 後面,所以不用。"""
    m = re.search(r"claude-code-(\d+(?:\.\d+)*)", name)
    return tuple(int(x) for x in m.group(1).split(".")) if m else (-1,)


def resolve_claude() -> str | None:
    """找原生 claude 執行檔。順序:RELAY_CLAUDE → PATH → ~/.local/bin → VS Code 擴充(取版本最大)。

    為什麼不能用 npm 的 claude.cmd:它只是薄殼,多行 prompt 經 cmd.exe 會被截斷／轉義(2026-10-05 設計時確認),
    所以 Windows 上遇到 .cmd/.bat/.ps1 就改找同目錄 node_modules 底下的原生 exe,找不到寧可略過也不回薄殼。
    """
    env = os.environ.get("RELAY_CLAUDE")
    if env:
        return env
    w = shutil.which("claude")
    if w:
        if Path(w).suffix.lower() in _THIN_SHELLS:
            native = Path(w).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
            if native.exists():
                return str(native)
        else:
            return w
    home = Path(os.environ.get("USERPROFILE", str(Path.home())))
    for cand in (home / ".local" / "bin" / "claude.exe", home / ".local" / "bin" / "claude"):
        if cand.exists():
            return str(cand)
    cands = glob.glob(str(home / ".vscode" / "extensions" / "anthropic.claude-code-*"
                          / "resources" / "native-binary" / "claude*"))
    cands = [c for c in cands if Path(c).name in ("claude.exe", "claude")]
    if cands:
        return max(cands, key=lambda c: _version_key(Path(c).parents[2].name))
    return None


def check_all(need_codex: bool = True, need_agy: bool = True, need_claude: bool = False) -> list[str]:
    """回傳缺少的 CLI 清單(空＝齊全)。給 relay 啟動時檢查。"""
    missing = []
    if need_codex and not resolve_codex():
        missing.append("codex(設 RELAY_CODEX 或裝 OpenAI/Codex CLI 並確認登入)")
    if need_agy and not resolve_agy():
        missing.append("agy(設 RELAY_AGY 或裝 Antigravity CLI 並登入)")
    if need_claude and not resolve_claude():
        missing.append("claude(設 RELAY_CLAUDE 或裝 Claude Code 原生版並登入)")
    return missing


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print("python:", resolve_python())
    print("codex :", resolve_codex() or "(找不到)")
    print("agy   :", resolve_agy() or "(找不到)")
    print("claude:", resolve_claude() or "(找不到)")
