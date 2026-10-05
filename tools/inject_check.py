# -*- coding: utf-8 -*-
"""Repeatable negative-control checks for a test assertion.

The public ``run_injection_check`` helper backs up a target, proves the test is
green, injects one byte-for-byte replacement, proves the named assertion is
red, and restores the original bytes.  Running this file directly exercises
that complete flow only inside a temporary directory.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Iterable, Sequence


class InjectionCheckError(RuntimeError):
    """A failed negative-control step, with a stable step name for reporting."""

    def __init__(self, step: str, detail: str):
        super().__init__(detail)
        self.step = step


@dataclass(frozen=True)
class TestRun:
    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return self.stdout + ("\n" if self.stdout and self.stderr else "") + self.stderr


@dataclass(frozen=True)
class InjectionCheckResult:
    baseline: TestRun
    injected: TestRun
    expected_failure: str


def _as_bytes(value: bytes | str, encoding: str, name: str) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode(encoding)
    raise TypeError("%s must be bytes or str" % name)


def _clear_pycache(roots: Iterable[os.PathLike[str] | str]) -> None:
    """Remove __pycache__ directories without following directory symlinks."""
    seen: set[Path] = set()
    for raw_root in roots:
        root = Path(raw_root).resolve()
        if root in seen or not root.exists():
            continue
        seen.add(root)

        if root.name == "__pycache__":
            if root.is_symlink():
                root.unlink()
            else:
                shutil.rmtree(root)
            continue

        for directory, dirnames, _filenames in os.walk(root, followlinks=False):
            for name in list(dirnames):
                if name != "__pycache__":
                    continue
                cache = Path(directory, name)
                if cache.is_symlink():
                    cache.unlink()
                else:
                    shutil.rmtree(cache)
                dirnames.remove(name)


def _run_test(
    command: Sequence[os.PathLike[str] | str],
    cwd: Path,
    cache_roots: Sequence[Path],
    timeout: float,
    step: str,
    shell: bool = False,
) -> TestRun:
    try:
        _clear_pycache(cache_roots)
    except OSError as exc:
        raise InjectionCheckError(step, "could not clear __pycache__: %s" % exc) from exc

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        completed = subprocess.run(
            command if shell else [os.fspath(part) for part in command],
            shell=shell,
            cwd=os.fspath(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise InjectionCheckError(step, "test timed out after %s seconds" % timeout) from exc
    except OSError as exc:
        raise InjectionCheckError(step, "could not start test: %s" % exc) from exc
    return TestRun(completed.returncode, completed.stdout, completed.stderr)


def _run_detail(run: TestRun) -> str:
    output = run.output.strip()
    return "exit %d%s" % (run.returncode, ("\n" + output) if output else "")


def run_injection_check(
    target_file: os.PathLike[str] | str,
    old: bytes | str,
    new: bytes | str,
    test_command: Sequence[os.PathLike[str] | str],
    expected_failure: str,
    *,
    cwd: os.PathLike[str] | str | None = None,
    cache_roots: Sequence[os.PathLike[str] | str] | None = None,
    encoding: str = "utf-8",
    timeout: float = 60,
    report: Callable[[str], None] = print,
    shell: bool = False,
) -> InjectionCheckResult:
    """Run one injection negative control and restore the target on every path.

    ``expected_failure`` must be a fragment emitted specifically when the
    intended assertion fails (for example, ``"FAIL assertion label"``), not
    merely a test name that is also printed for passing tests.
    """
    target = Path(target_file).resolve()
    workdir = Path(cwd).resolve() if cwd is not None else target.parent
    roots = tuple(Path(root).resolve() for root in (cache_roots or (workdir, target.parent)))

    if not target.is_file():
        raise InjectionCheckError("backup", "target is not a file: %s" % target)
    if not test_command:
        raise InjectionCheckError("input", "test_command must not be empty")
    if isinstance(test_command, (str, bytes)) and not shell:
        raise InjectionCheckError("input", "test_command must be an argv unless shell=True")
    if not expected_failure:
        raise InjectionCheckError("input", "expected_failure must not be empty")

    old_bytes = _as_bytes(old, encoding, "old")
    new_bytes = _as_bytes(new, encoding, "new")
    if not old_bytes:
        raise InjectionCheckError("inject", "old must not be empty")
    if old_bytes == new_bytes:
        raise InjectionCheckError("inject", "old and new must differ")

    with tempfile.TemporaryDirectory(prefix="inject-check-backup-") as backup_dir:
        backup = Path(backup_dir, target.name)
        try:
            shutil.copy2(target, backup)
            with open(backup, "rb") as stream:
                original_bytes = stream.read()
        except OSError as exc:
            raise InjectionCheckError("backup", "could not create file backup: %s" % exc) from exc
        report("[backup] copied %s" % target)

        try:
            report("[baseline] clearing __pycache__ and running test")
            baseline = _run_test(test_command, workdir, roots, timeout, "baseline", shell=shell)
            if baseline.returncode != 0:
                raise InjectionCheckError(
                    "baseline", "test must pass before injection; " + _run_detail(baseline)
                )
            if expected_failure in baseline.output:
                raise InjectionCheckError(
                    "baseline",
                    "expected_failure must be a failure-only marker; it appeared in the passing run",
                )

            try:
                with open(target, "rb") as stream:
                    current_bytes = stream.read()
            except OSError as exc:
                raise InjectionCheckError("inject", "could not read target: %s" % exc) from exc

            hits = current_bytes.count(old_bytes)
            if hits != 1:
                raise InjectionCheckError(
                    "inject", "replacement must match exactly once; found %d matches" % hits
                )
            injected_bytes = current_bytes.replace(old_bytes, new_bytes, 1)
            try:
                # Binary mode preserves every existing LF/CRLF byte.
                with open(target, "wb") as stream:
                    stream.write(injected_bytes)
            except OSError as exc:
                raise InjectionCheckError("inject", "could not write injection: %s" % exc) from exc
            report("[inject] replacement matched exactly once")

            report("[negative-control] clearing __pycache__ and running injected test")
            injected = _run_test(test_command, workdir, roots, timeout, "negative-control", shell=shell)
            if injected.returncode == 0:
                raise InjectionCheckError(
                    "negative-control", "injected test unexpectedly passed; " + _run_detail(injected)
                )
            if expected_failure not in injected.output:
                raise InjectionCheckError(
                    "negative-control",
                    "test failed, but not at expected marker %r; %s"
                    % (expected_failure, _run_detail(injected)),
                )
            report("[negative-control] expected assertion failed: %s" % expected_failure)
            result = InjectionCheckResult(baseline, injected, expected_failure)
        finally:
            try:
                shutil.copy2(backup, target)
                with open(backup, "rb") as stream:
                    backup_bytes = stream.read()
                with open(target, "rb") as stream:
                    restored_bytes = stream.read()
            except OSError as exc:
                raise InjectionCheckError("restore", "could not restore/verify target: %s" % exc) from exc
            if restored_bytes != original_bytes or restored_bytes != backup_bytes:
                raise InjectionCheckError("restore", "restored target is not byte-for-byte identical")
            report("[restore] copied from backup; byte-for-byte identity verified")

        return result


def _expect_rejection(
    step: str, fragment: str, src: bytes, old: bytes, new: bytes, marker: str
) -> None:
    """Require ``run_injection_check`` to reject one bad scenario at ``step``.

    The happy-path self-check only exercises defences ① and ②.  Defences ③
    (match exactly once), ④ (injected run must fail) and ⑤ (fail at the named
    marker) never fire there, so without this helper the negative-control tool
    would itself lack a complete negative control.  If the matching defence were
    removed the call would not raise, or raise elsewhere, and this check fails.
    """
    with tempfile.TemporaryDirectory(prefix="inject-check-selftest-") as temp_dir:
        root = Path(temp_dir)
        target = root / "test_sample.py"
        cache = root / "__pycache__"
        cache.mkdir()
        with open(cache / "stale.pyc", "wb") as stream:
            stream.write(b"stale bytecode sentinel")
        with open(target, "wb") as stream:
            stream.write(src)

        try:
            run_injection_check(
                target, old, new, [sys.executable, target], marker,
                cwd=root, cache_roots=[root], report=lambda _message: None,
            )
        except InjectionCheckError as exc:
            if exc.step != step or fragment not in str(exc):
                raise InjectionCheckError(
                    "self-check",
                    "expected rejection at %r containing %r, got [%s] %s"
                    % (step, fragment, exc.step, exc),
                )
        else:
            raise InjectionCheckError(
                "self-check",
                "expected rejection at %r (%r) but the check passed; defence missing?"
                % (step, fragment),
            )

        with open(target, "rb") as stream:
            if stream.read() != src:
                raise InjectionCheckError(
                    "self-check", "scenario target was not restored byte-for-byte"
                )


def _self_check() -> int:
    marker = "INJECT_CHECK_EXPECTED_ASSERTION"
    original = (
        b"import os\r\n"
        b"VALUE = 'safe'\r\n"
        b"if os.environ.get('PYTHONDONTWRITEBYTECODE') != '1':\r\n"
        b"    raise RuntimeError('PYTHONDONTWRITEBYTECODE was not set')\r\n"
        b"assert VALUE == 'safe', 'INJECT_CHECK_EXPECTED_ASSERTION'\r\n"
        b"print('temporary assertion passed')\r\n"
    )

    try:
        with tempfile.TemporaryDirectory(prefix="inject-check-selftest-") as temp_dir:
            root = Path(temp_dir)
            target = root / "test_sample.py"
            cache = root / "__pycache__"
            cache.mkdir()
            with open(cache / "stale.pyc", "wb") as stream:
                stream.write(b"stale bytecode sentinel")
            with open(target, "wb") as stream:
                stream.write(original)

            result = run_injection_check(
                target,
                b"VALUE = 'safe'",
                b"VALUE = 'unsafe'",
                [sys.executable, target],
                marker,
                cwd=root,
                cache_roots=[root],
            )

            with open(target, "rb") as stream:
                final_bytes = stream.read()
            if final_bytes != original:
                raise InjectionCheckError("self-check", "temporary target was not restored exactly")
            if cache.exists():
                raise InjectionCheckError("self-check", "stale __pycache__ was not removed")
            if result.baseline.returncode != 0 or result.injected.returncode == 0:
                raise InjectionCheckError("self-check", "unexpected recorded test exit codes")

        # ③④⑤ must be exercised too, or this negative-control tool has no
        # regression cover on three of its own six defences.
        _expect_rejection(
            "inject", "exactly once",
            b"V = 'ok'\r\nV = 'ok'\r\nassert V == 'ok', 'MARK_DUP'\r\nprint('dup sample ok')\r\n",
            b"V = 'ok'", b"V = 'no'", "MARK_DUP",
        )
        _expect_rejection(
            "negative-control", "unexpectedly passed",
            b"UNUSED = 'aaa'\r\nassert True, 'MARK_PASS'\r\nprint('pass sample ok')\r\n",
            b"UNUSED = 'aaa'", b"UNUSED = 'bbb'", "MARK_PASS",
        )
        _expect_rejection(
            "negative-control", "not at expected marker",
            b"X = 'ok'\r\nassert X == 'ok', 'MARK_OTHER'\r\n"
            b"assert True, 'MARK_TARGET'\r\nprint('other sample ok')\r\n",
            b"X = 'ok'", b"X = 'no'", "MARK_TARGET",
        )
    except InjectionCheckError as exc:
        print("SELF-CHECK FAIL [%s]: %s" % (exc.step, exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print("SELF-CHECK FAIL [setup]: %s" % exc, file=sys.stderr)
        return 1

    print("SELF-CHECK PASS: injection detected and target restored byte-for-byte")
    return 0


if __name__ == "__main__":
    sys.exit(_self_check())
