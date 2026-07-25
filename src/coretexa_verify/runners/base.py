"""Runner interface plus the pieces every runner shares."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from ..gitops import run
from ..models import Outcome, RunnerInfo, TestRunResult

TAIL_CHARS = 4000


@dataclass
class DetectionContext:
    """Everything a detector may look at. Filesystem access is read-only."""

    repo: str

    def exists(self, *rel: str) -> bool:
        return any(os.path.exists(os.path.join(self.repo, r)) for r in rel)

    def read(self, rel: str, limit: int = 200_000) -> str:
        try:
            with open(os.path.join(self.repo, rel), encoding="utf-8", errors="replace") as fh:
                return fh.read(limit)
        except OSError:
            return ""


class Runner:
    """A way of executing a subset of a repository's tests.

    Subclasses implement :meth:`build_command` and :meth:`parse`. Everything
    else - timeouts, output capture, tail truncation - is handled here so that
    adding a language is one small class plus one registry entry.
    """

    id: str = "abstract"
    language: str = "unknown"
    #: extension of the machine-readable report this runner writes
    report_suffix: str = "xml"

    def __init__(self, repo: str, reason: str, extra_args: list[str] | None = None):
        self.repo = repo
        self.reason = reason
        self.extra_args = list(extra_args or [])

    @property
    def info(self) -> RunnerInfo:
        return RunnerInfo(id=self.id, language=self.language, reason=self.reason)

    # -- to implement ------------------------------------------------------
    def build_command(self, targets: list[str], report_path: str) -> list[str]:
        raise NotImplementedError

    def parse(self, report_path: str, exit_code: int, stdout: str, stderr: str) -> TestRunResult:
        raise NotImplementedError

    def default_test_dir(self) -> str | None:
        """Directory to fall back to when no specific test can be selected."""
        return None

    def collect(self, targets: list[str], timeout: int) -> list[str] | None:
        """List the test ids under ``targets`` without running them.

        Returning None means "this runner cannot enumerate tests", which
        switches off selection refinement rather than guessing.
        """
        return None

    # -- shared ------------------------------------------------------------
    def execute(self, targets: list[str], timeout: int, report_dir: str, tag: str) -> TestRunResult:
        report_path = os.path.join(report_dir, f"{tag}-report.{self.report_suffix}")
        argv = self.build_command(targets, report_path)
        started = time.monotonic()
        res = run(argv, cwd=self.repo, timeout=timeout, env={"CI": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        duration = time.monotonic() - started
        if res.timed_out:
            return TestRunResult(
                command=argv,
                outcome=Outcome.TIMEOUT,
                exit_code=None,
                duration_s=round(duration, 2),
                timeout_s=timeout,
                note=f"exceeded the {timeout}s per-run timeout",
                stdout_tail=tail(res.stdout),
                stderr_tail=tail(res.stderr),
            )
        parsed = self.parse(report_path, res.returncode, res.stdout, res.stderr)
        parsed.command = argv
        parsed.exit_code = res.returncode
        parsed.duration_s = round(duration, 2)
        parsed.timeout_s = timeout
        parsed.stdout_tail = tail(res.stdout)
        parsed.stderr_tail = tail(res.stderr)
        return parsed


def tail(text: str, limit: int = TAIL_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return "...[truncated]...\n" + text[-limit:]
