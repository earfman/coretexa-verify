"""Core data types shared by every part of coretexa-verify.

Nothing in here does any work; keeping the vocabulary in one place makes the
control flow in :mod:`coretexa_verify.verify` readable end to end.
"""

from __future__ import annotations

import dataclasses
import enum
from dataclasses import dataclass, field
from typing import Any


class Verdict(str, enum.Enum):
    """The product. These exact strings are what we promise to emit."""

    NO_GATE = "NO_GATE"
    GATE_HOLDS = "GATE_HOLDS"
    GATE_HOLDS_BUILD = "GATE_HOLDS_BUILD"
    NO_NEW_TESTS = "NO_NEW_TESTS"
    INCONCLUSIVE = "INCONCLUSIVE"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class Outcome(str, enum.Enum):
    """What a single test run did.

    The distinction between ``ASSERT_FAIL`` and ``BUILD_ERROR`` is load bearing:
    it is the difference between ``GATE_HOLDS`` and ``GATE_HOLDS_BUILD``.
    """

    PASS = "PASS"
    ASSERT_FAIL = "ASSERT_FAIL"
    BUILD_ERROR = "BUILD_ERROR"
    NO_TESTS_RUN = "NO_TESTS_RUN"
    TIMEOUT = "TIMEOUT"
    RUNNER_ERROR = "RUNNER_ERROR"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class Kind(str, enum.Enum):
    """How a changed file is treated."""

    SOURCE = "SOURCE"
    TEST = "TEST"
    OTHER = "OTHER"  # docs/licence/CI metadata: never reverted, never counted

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass
class ChangedFile:
    path: str
    status: str  # git name-status letter: A, M, D, R...
    kind: Kind
    reason: str
    old_path: str | None = None  # set for renames
    executable_test: bool = False


@dataclass
class TestRunResult:
    """The outcome of one invocation of the detected test runner."""

    __test__ = False  # not a pytest test class, despite the name

    command: list[str]
    outcome: Outcome
    exit_code: int | None = None
    duration_s: float = 0.0
    timeout_s: int | None = None
    passed: int = 0
    failed: int = 0
    errored: int = 0
    skipped: int = 0
    total: int = 0
    failing_ids: list[str] = field(default_factory=list)
    erroring_ids: list[str] = field(default_factory=list)
    note: str = ""
    stdout_tail: str = ""
    stderr_tail: str = ""

    @property
    def command_str(self) -> str:
        return " ".join(_shquote(c) for c in self.command)

    def summary(self) -> str:
        if self.outcome is Outcome.TIMEOUT:
            return f"timed out after {self.timeout_s}s"
        if self.outcome is Outcome.RUNNER_ERROR:
            return f"runner error (exit {self.exit_code}): {self.note}".strip()
        if self.outcome is Outcome.NO_TESTS_RUN:
            return "no tests were collected"
        bits = [f"{self.passed} passed"]
        if self.failed:
            bits.append(f"{self.failed} failed")
        if self.errored:
            bits.append(f"{self.errored} errored")
        if self.skipped:
            bits.append(f"{self.skipped} skipped")
        return ", ".join(bits)


@dataclass
class RunnerInfo:
    """Which test runner we picked and, crucially, why."""

    id: str
    language: str
    reason: str


@dataclass
class SelectionEntry:
    """How one changed test file became a thing we can hand to the runner."""

    source_file: str
    targets: list[str]
    method: str  # "direct" | "fixture-map" | "directory-fallback"
    detail: str = ""

    @property
    def is_fallback(self) -> bool:
        return self.method == "directory-fallback"


@dataclass
class HunkResult:
    """One hunk reverted on its own, and what the PR's tests made of it."""

    path: str
    index: int
    header: str
    label: str
    outcome: Outcome
    gated: bool
    summary: str
    preview: str = ""
    failing_ids: list[str] = field(default_factory=list)


@dataclass
class Report:
    verdict: Verdict
    headline: str
    repo: str = ""
    base_ref: str = ""
    head_ref: str = ""
    base_sha: str = ""
    head_sha: str = ""
    merge_base_sha: str = ""
    changed_files: list[ChangedFile] = field(default_factory=list)
    runner: RunnerInfo | None = None
    selection: list[SelectionEntry] = field(default_factory=list)
    test_targets: list[str] = field(default_factory=list)
    head_run: TestRunResult | None = None
    reverted_run: TestRunResult | None = None
    reverted_files: list[str] = field(default_factory=list)
    hunk_results: list[HunkResult] = field(default_factory=list)
    inert_hunks: list[str] = field(default_factory=list)
    localized: bool = False
    warnings: list[str] = field(default_factory=list)
    tree_restored: bool = True
    tool_version: str = ""

    @property
    def source_files(self) -> list[ChangedFile]:
        return [f for f in self.changed_files if f.kind is Kind.SOURCE]

    @property
    def test_files(self) -> list[ChangedFile]:
        return [f for f in self.changed_files if f.kind is Kind.TEST]

    def to_dict(self) -> dict[str, Any]:
        def run(r: TestRunResult | None) -> dict[str, Any] | None:
            if r is None:
                return None
            d = dataclasses.asdict(r)
            d["outcome"] = r.outcome.value
            d["command_str"] = r.command_str
            d["summary"] = r.summary()
            return d

        return {
            "verdict": self.verdict.value,
            "headline": self.headline,
            "tool_version": self.tool_version,
            "repo": self.repo,
            "refs": {
                "base_ref": self.base_ref,
                "head_ref": self.head_ref,
                "base_sha": self.base_sha,
                "head_sha": self.head_sha,
                "merge_base_sha": self.merge_base_sha,
            },
            "runner": dataclasses.asdict(self.runner) if self.runner else None,
            "changed_files": [
                {
                    "path": f.path,
                    "status": f.status,
                    "kind": f.kind.value,
                    "reason": f.reason,
                    "old_path": f.old_path,
                    "executable_test": f.executable_test,
                }
                for f in self.changed_files
            ],
            "selection": [dataclasses.asdict(s) for s in self.selection],
            "test_targets": self.test_targets,
            "reverted_files": self.reverted_files,
            "head_run": run(self.head_run),
            "reverted_run": run(self.reverted_run),
            "localized": self.localized,
            "hunk_results": [
                {**dataclasses.asdict(h), "outcome": h.outcome.value} for h in self.hunk_results
            ],
            "inert_hunks": self.inert_hunks,
            "warnings": self.warnings,
            "tree_restored": self.tree_restored,
        }


def _shquote(s: str) -> str:
    import shlex

    return shlex.quote(s)
