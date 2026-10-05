"""runlock.py — 跨行程檔案鎖（relay 並行用；只用標準函式庫）。

2026-10-05（C3）：兩個 relay 行程同時跑會互踩（同一任務被開兩次、帳本併發 append、同一 repo 的
`git worktree add`／commit 撞 git 自己的 lock）。這裡提供兩種鎖：
  - 長鎖：`try_hold()` 非阻塞拿、拿到就一直持有到 `release()`（任務、worktree、並行名額）
  - 短鎖：`locked()` 每 0.1 秒重試、逾時丟 `LockBusy`（帳本 append、repo 的 worktree add／commit）

為什麼用 OS 的 byte-range／flock 鎖，而不是「O_EXCL 建檔＋寫 PID」：
  - 行程被硬殺時 OS 會自動釋放鎖 ⇒ 殘留的鎖檔只是一個「沒人持有」的空檔，下一個行程直接拿得到，
    不必猜 PID 是不是還活著（PID 會被重用）。
  - 🔴 判活**不可**用 `os.kill(pid, 0)`：Windows 上那是 `TerminateProcess`，會把對方殺掉。
平台：Windows 用 `msvcrt.locking`（🔴 不可用 `LK_LOCK`：它內建重試 10 次後丟例外，改自己迴圈 `LK_NBLCK`）；
POSIX 用 `fcntl.flock`。兩者都是 per-handle，同一行程開兩個 handle 也會互斥（測試靠這點）。

鎖檔保持空檔：Windows 上被鎖的 byte 其他行程讀不到，別把資訊放鎖檔（pid 已在 STATE.json）。
鎖檔不在釋放時刪除：Windows 上別的行程開著它時刪不掉（2026-10-05 實測 PermissionError），
POSIX 上「刪掉再重建」會讓兩個行程各鎖到不同 inode ⇒ 兩邊都以為自己持有。留著空檔是安全的。
"""
from __future__ import annotations

import errno
import os
import time
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

# 「被別人鎖住」的 errno；其他 OSError（磁碟、權限…）照常往外丟，不當成「忙碌」吞掉
_BUSY_ERRNOS = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK}


class LockBusy(RuntimeError):
    """拿不到鎖：別的行程持有中。"""


class Held:
    """一把拿到手的鎖；`release()` 解鎖並關檔，可重複呼叫。"""

    def __init__(self, path: Path, f) -> None:
        self.path = path
        self._f = f

    def release(self) -> None:
        f, self._f = self._f, None
        if f is None:
            return
        try:
            if os.name == "nt":
                f.seek(0)  # msvcrt 從目前位置算 byte 範圍，解鎖前要回到 0
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()  # 就算解鎖失敗，關檔也會讓 OS 釋放鎖

    def __enter__(self) -> "Held":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def try_hold(path: Path) -> Held | None:
    """非阻塞拿鎖：拿到回 Held，別人持有回 None。鎖檔不存在就建（含上層目錄）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+b")
    try:
        f.seek(0)
        if os.name == "nt":
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)  # 鎖第 0 個 byte（空檔也可以鎖超過 EOF 的範圍）
        else:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        f.close()
        if e.errno in _BUSY_ERRNOS:
            return None
        raise
    except BaseException:
        f.close()
        raise
    return Held(path, f)


def wait_hold(path: Path, timeout: float, interval: float = 0.1) -> Held | None:
    """每 interval 秒重試 try_hold，最多等 timeout 秒；逾時回 None。timeout=0 等於只試一次。"""
    deadline = time.monotonic() + timeout
    while True:
        h = try_hold(path)
        if h is not None or time.monotonic() >= deadline:
            return h
        time.sleep(interval)


def is_held(path: Path) -> bool:
    """有沒有別的 handle 持有這把鎖（C1 判活用）。

    鎖檔不存在＝沒人持有（持有者一定先建檔）；不存在時也不替呼叫端建檔，免得 `--status` 掃一次就
    在 runs/.locks/ 憑空生出一堆空檔。
    ponytail：探測本身會「拿一下立刻放」，與正要開跑的行程撞在同一瞬間時對方會拿不到；
    relay 取任務鎖時因此多等 0.5 秒（見 relay.acquire_run_locks）。"""
    path = Path(path)
    if not path.exists():
        return False
    h = try_hold(path)
    if h is None:
        return True
    h.release()
    return False


@contextmanager
def locked(path: Path, timeout: float = 30.0):
    """短鎖：拿到才進 with、離開就放；等超過 timeout 秒丟 LockBusy（不靜默略過寫入）。"""
    path = Path(path)
    h = wait_hold(path, timeout)
    if h is None:
        raise LockBusy(f"等鎖 {path.name} 超過 {timeout:g} 秒（另一個 relay 行程持有中）")
    try:
        yield h
    finally:
        h.release()
