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
    #: The experiment was never attempted for this hunk, because no test the
    #: detected runner executes can reach the file it lives in. Distinct from
    #: every outcome above: those record something we ran, this records
    #: something we deliberately did not.
    NOT_RUN = "NOT_RUN"

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
    #: Head-side 1-based inclusive line ranges holding test code *inside* this
    #: source file (Rust's ``#[cfg(test)] mod tests``). Non-empty means the file
    #: is simultaneously SOURCE (revert it) and TEST (run it), and that the
    #: revert must be done per hunk so the PR's own tests survive it. See
    #: :mod:`coretexa_verify.inline_tests`.
    inline_test_regions: list[tuple[int, int]] = field(default_factory=list)

    @property
    def has_inline_tests(self) -> bool:
        return bool(self.inline_test_regions)


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

    @property
    def executed(self) -> int:
        """Tests that actually ran a body. A skip is not evidence of anything.

        pytest exits 0 when 100% of the selected tests skip, so ``PASS`` with
        ``passed == 0`` is a real and common shape (an optional dependency, a
        platform guard, an absent service). A verdict drawn from a run with
        ``executed == 0`` would be backed by zero executed assertions.
        """
        return self.passed + self.failed + self.errored

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
    """How one changed test file became a thing we can hand to the runner.

    ``proof`` is the load-bearing addition: it is non-empty only when the link
    between the changed file and these targets was *established*, not guessed.
    A ``NO_GATE`` verdict is never allowed to rest on an entry with no proof -
    see :func:`coretexa_verify.verify._enforce_soundness`.
    """

    source_file: str
    targets: list[str]
    method: str  # "direct" | "fixture-map" | "fixture-harness" | "directory-fallback"
    detail: str = ""
    #: Evidence that these targets really consume ``source_file``. Empty = guess.
    proof: str = ""
    #: Targets that came from auto-discovery-harness detection rather than a
    #: literal name match; collected with ``-k <fixture stem>``.
    harness_targets: list[str] = field(default_factory=list)

    @property
    def is_fallback(self) -> bool:
        return self.method == "directory-fallback"

    @property
    def proven(self) -> bool:
        return bool(self.proof)


#: Per-hunk outcomes that establish nothing either way. A runner usage error
#: (pytest exit 4, no JUnit report), a timeout, or an empty collection tells us
#: the experiment did not happen - not that the hunk is gated.
UNEVALUABLE_OUTCOMES = (Outcome.RUNNER_ERROR, Outcome.TIMEOUT, Outcome.NO_TESTS_RUN)


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
    #: Non-empty when this hunk could not be reverted alone and was reverted
    #: together with the hunks named here - an identifier rename and the change
    #: that consumes it. The result then gates the *group*, not this hunk.
    group: list[str] = field(default_factory=list)
    #: Identifier renames re-applied to the reverted text so the file still
    #: compiles: ``{"OldName": "NewName"}``. See :mod:`coretexa_verify.hunks`.
    renames_applied: dict = field(default_factory=dict)
    #: Why no test the detected runner executes can reach this file. Non-empty
    #: means the hunk was never run and is excluded from every count.
    unreachable_reason: str = ""
    #: Why localisation stopped before reaching this hunk - a time or count
    #: budget, not a property of the code. Kept distinct from
    #: ``unreachable_reason`` because "we ran out of time" and "no test could
    #: ever observe this" are different claims, and reporting the first as the
    #: second would overstate what was established. Excluded from every count.
    budget_skipped_reason: str = ""

    @property
    def status(self) -> str:
        """``gated`` | ``ungated`` | ``unknown`` | ``unreachable``.

        ``unknown`` is the important one: reverting the hunk made the *runner*
        fail, so no test ever expressed an opinion. Reporting that as "gated"
        would let a broken command masquerade as a passing safety net.

        ``skipped`` means localisation ran out of its time budget before it
        got here. Like ``unreachable`` it is excluded from every count, but it
        says nothing about the code - only about how long we were willing to
        spend.

        ``unreachable`` is the quieter one: the hunk was never reverted at all,
        because the file it lives in is a frontend asset or a dependency
        manifest that no test this runner executes can observe. Counting such a
        hunk as "ungated" was how a NO_GATE headline came to say "19 of 34
        behavioural changes" when 15 of the 34 were .vue files.
        """
        if self.budget_skipped_reason:
            return "skipped"
        if self.unreachable_reason or self.outcome is Outcome.NOT_RUN:
            return "unreachable"
        if self.outcome in UNEVALUABLE_OUTCOMES:
            return "unknown"
        return "gated" if self.gated else "ungated"

    @property
    def evaluable(self) -> bool:
        return self.status not in ("unknown", "unreachable", "skipped")

    @property
    def reachable(self) -> bool:
        return self.status not in ("unreachable", "skipped")


@dataclass
class BuildInfo:
    """A repository build step we detected and are responsible for re-running.

    Tests that execute build output cannot see a source revert unless the build
    is re-run *inside* the mutation, so every mutated test run is preceded by
    one of these. ``runs``/``failures`` make that auditable from the report.
    """

    command: list[str] = field(default_factory=list)
    reason: str = ""
    cwd: str = ""
    runs: int = 0
    failures: int = 0
    status: str = "none"  # none | ok | failed | timeout
    note: str = ""

    @property
    def command_str(self) -> str:
        return " ".join(_shquote(c) for c in self.command)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "command_str": self.command_str,
            "reason": self.reason,
            "cwd": self.cwd,
            "runs": self.runs,
            "failures": self.failures,
            "status": self.status,
            "note": self.note,
        }


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
    #: Populated by :func:`coretexa_verify.verify.verify`; never None after a run.
    install: Any = None
    selection: list[SelectionEntry] = field(default_factory=list)
    test_targets: list[str] = field(default_factory=list)
    head_run: TestRunResult | None = None
    reverted_run: TestRunResult | None = None
    #: The Defect-1 targeted probe: the fixture reverted alone, source intact.
    probe_run: TestRunResult | None = None
    probe_note: str = ""
    #: The *first* head run, kept when it failed and the failures turned out to
    #: pre-date the PR. :attr:`head_run` is then the re-run that excludes them.
    prior_head_run: TestRunResult | None = None
    #: The failing tests re-run with source *and* tests at the merge base.
    base_recheck_run: TestRunResult | None = None
    #: Test ids that failed at head and fail identically at the merge base.
    pre_existing_failures: list[str] = field(default_factory=list)
    #: How the pre-existing failures were established, for the report.
    pre_existing_note: str = ""
    reverted_files: list[str] = field(default_factory=list)
    #: Build step re-run around every mutation, when one was detected.
    build: BuildInfo | None = None
    #: Monorepo package the tests were actually run from, relative to the repo.
    workspace_package: str = ""
    #: Non-empty when test results may be served by stale build output.
    build_artifact_risk: str = ""
    #: Names (never values) of environment variables withheld from every
    #: subprocess that executes repository-controlled code.
    redacted_env: list[str] = field(default_factory=list)
    hunk_results: list[HunkResult] = field(default_factory=list)
    inert_hunks: list[str] = field(default_factory=list)
    #: Hunks skipped because no test the detected runner executes can reach the
    #: file they live in (frontend assets, dependency manifests). Reported
    #: separately from the behavioural count rather than diluting it.
    unreachable_hunks: list[str] = field(default_factory=list)
    localized: bool = False
    warnings: list[str] = field(default_factory=list)
    tree_restored: bool = True
    tool_version: str = ""

    @property
    def source_files(self) -> list[ChangedFile]:
        return [f for f in self.changed_files if f.kind is Kind.SOURCE]

    @property
    def test_files(self) -> list[ChangedFile]:
        """Files carrying the PR's evidence.

        A source file with an inline ``#[cfg(test)]`` block appears here *and*
        in :attr:`source_files`. It is genuinely both, and pretending otherwise
        is what would make a Rust verdict meaningless.
        """
        return [
            f for f in self.changed_files
            if f.kind is Kind.TEST or f.has_inline_tests
        ]

    def to_dict(self) -> dict[str, Any]:
        def run(r: TestRunResult | None) -> dict[str, Any] | None:
            if r is None:
                return None
            d = dataclasses.asdict(r)
            d["outcome"] = r.outcome.value
            d["command_str"] = r.command_str
            d["summary"] = r.summary()
            d["executed"] = r.executed
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
            "install": self.install.to_dict() if self.install is not None else None,
            "changed_files": [
                {
                    "path": f.path,
                    "status": f.status,
                    "kind": f.kind.value,
                    "reason": f.reason,
                    "old_path": f.old_path,
                    "executable_test": f.executable_test,
                    "inline_test_regions": [list(r) for r in f.inline_test_regions],
                }
                for f in self.changed_files
            ],
            "selection": [
                {**dataclasses.asdict(s), "proven": s.proven} for s in self.selection
            ],
            "test_targets": self.test_targets,
            "reverted_files": self.reverted_files,
            "head_run": run(self.head_run),
            "reverted_run": run(self.reverted_run),
            "probe_run": run(self.probe_run),
            "probe_note": self.probe_note,
            "prior_head_run": run(self.prior_head_run),
            "base_recheck_run": run(self.base_recheck_run),
            "pre_existing_failures": self.pre_existing_failures,
            "pre_existing_note": self.pre_existing_note,
            "build": self.build.to_dict() if self.build is not None else None,
            "workspace_package": self.workspace_package,
            "build_artifact_risk": self.build_artifact_risk,
            "redacted_env": self.redacted_env,
            "localized": self.localized,
            "hunk_results": [
                {**dataclasses.asdict(h), "outcome": h.outcome.value, "status": h.status}
                for h in self.hunk_results
            ],
            "inert_hunks": self.inert_hunks,
            "unreachable_hunks": self.unreachable_hunks,
            "warnings": self.warnings,
            "tree_restored": self.tree_restored,
        }


def _shquote(s: str) -> str:
    import shlex

    return shlex.quote(s)
