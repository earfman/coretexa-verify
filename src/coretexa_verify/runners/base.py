"""Runner interface plus the pieces every runner shares."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass

from ..gitops import run
from ..models import BuildInfo, Outcome, RunnerInfo, TestRunResult

TAIL_CHARS = 4000


@dataclass
class BuildStep:
    """A repository build the tests depend on, that we own re-running.

    A test that imports build output (``dist/``, a compiled extension) does not
    see a source revert unless the build runs again *while the source is
    reverted*. When a build step is known we therefore re-run it before every
    single test run, mutated or not; when one is needed and not known, the
    verdict is degraded rather than trusted.
    """

    argv: list[str]
    reason: str
    cwd: str
    timeout: int = 900

    def info(self) -> BuildInfo:
        return BuildInfo(command=list(self.argv), reason=self.reason, cwd=self.cwd)


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
        #: Directory the runner is actually invoked from. Equals ``repo`` unless
        #: :meth:`focus` moved us into a monorepo package.
        self.cwd = repo
        self.reason = reason
        self.extra_args = list(extra_args or [])
        self._pycache_dir: str | None = None
        #: Set by :func:`coretexa_verify.verify` when a build step is detected.
        self.build_step: "BuildStep | None" = None
        self.build_info: BuildInfo | None = None
        #: Warnings a detector wants surfaced in the report - things the user
        #: needs to know about the environment we are about to run in.
        self.setup_warnings: list[str] = []

    # -- environment -------------------------------------------------------
    @property
    def pycache_prefix(self) -> str:
        """A scratch directory that absorbs all bytecode caching for this run."""
        if self._pycache_dir is None:
            self._pycache_dir = tempfile.mkdtemp(prefix="coretexa-verify-pyc-")
        return self._pycache_dir

    def subprocess_env(self) -> dict:
        """Environment for every runner subprocess.

        ``PYTHONDONTWRITEBYTECODE`` stops us dropping ``__pycache__`` into the
        user's tree. ``PYTHONPYCACHEPREFIX`` points bytecode *lookup* at an
        empty scratch directory, which is the load-bearing half: a stale
        ``.pyc`` whose recorded source mtime and size still match a reverted
        file would otherwise let the head version of the code answer for the
        base version, and silently turn GATE_HOLDS into NO_GATE. A revert that
        does not change a file's byte count - swapping ``1`` for ``2`` - hits
        exactly that case.
        """
        return {
            "CI": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": self.pycache_prefix,
        }

    def cleanup(self) -> None:
        """Drop the scratch directory. Safe to call more than once."""
        if self._pycache_dir:
            shutil.rmtree(self._pycache_dir, ignore_errors=True)
            self._pycache_dir = None

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

    def collect(
        self, targets: list[str], timeout: int, extra: list[str] | None = None
    ) -> list[str] | None:
        """List the test ids under ``targets`` without running them.

        Returning None means "this runner cannot enumerate tests", which
        switches off selection refinement rather than guessing. ``extra`` is
        passed through to the runner (we use it for ``-k <fixture stem>``).
        """
        return None

    def focus(self, targets: list[str]) -> tuple[list[str], str] | None:
        """Move into the monorepo package that owns ``targets``, if there is one.

        Returns ``(rewritten targets, explanation)`` and mutates :attr:`cwd`, or
        None when the repository is not a workspace or the targets span more
        than one package.
        """
        return None

    def artifact_risk(self, targets: list[str], source_paths: list[str]) -> str:
        """Why a stale build artefact could mask a source revert, or ``""``."""
        return ""

    # -- shared ------------------------------------------------------------
    def run_build(self) -> BuildInfo | None:
        """Re-run the detected build step. Called before *every* test run."""
        step = self.build_step
        if step is None:
            return None
        if self.build_info is None:
            self.build_info = step.info()
        info = self.build_info
        info.runs += 1
        res = run(step.argv, cwd=step.cwd, timeout=step.timeout, env={"CI": "1"})
        if res.timed_out:
            info.status = "timeout"
            info.failures += 1
            info.note = f"the build exceeded its {step.timeout}s timeout"
        elif res.returncode != 0:
            info.status = "failed"
            info.failures += 1
            info.note = tail(res.stderr or res.stdout, 1200)
        else:
            info.status = "ok"
        return info

    def execute(self, targets: list[str], timeout: int, report_dir: str, tag: str) -> TestRunResult:
        report_path = os.path.join(report_dir, f"{tag}-report.{self.report_suffix}")
        self.run_build()
        argv = self.build_command(targets, report_path)
        started = time.monotonic()
        res = run(argv, cwd=self.cwd, timeout=timeout, env=self.subprocess_env())
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
