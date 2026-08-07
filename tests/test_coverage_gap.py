"""A NO_GATE is only a claim about the tests when the tests could have failed.

The defect (D5, found by running the tool against live pull requests, not by
reading it): yorukot/superfile#1619 changed ``src/pkg/file_preview/`` and, quite
separately, one test in ``src/internal/ui/prompt/``. Selection took that test —
correctly, it was the only one the PR touched — the run reverted a package the
test does not import, both runs passed identically, and the verdict read *"this
PR's tests would pass without the fix."*

Nothing about that sentence was earned. The experiment had no power, and the
headline blamed an author for a gap the run never measured.

The subtlety these tests pin down: **directory disjointness proves nothing**. A
Go test in one package may import another and exercise it perfectly well, so the
only honest source of truth is the toolchain's own dependency closure. The
runner asks ``go list -deps -test`` and downgrades *only* on a positive answer;
anything else — a failure, a timeout, a runner that cannot tell — leaves the
verdict alone. Trading a false NO_GATE for a false INCONCLUSIVE would lose real
findings, which is the failure mode nobody would notice.
"""

import pytest

from coretexa_verify.gitops import CommandResult
from coretexa_verify.models import Outcome, Report, TestRunResult, Verdict
from coretexa_verify.runners.base import Runner
from coretexa_verify.runners.golang import GoTestRunner
from coretexa_verify.verify import _decide_stage1

CHANGED = ["src/pkg/file_preview/thumbnail_generator.go", "src/pkg/file_preview/constants.go"]
PROMPT_TEST = ["src/internal/ui/prompt/tokenize_test.go::Test_resolveShellSubstitution"]


def _result(stdout: str, returncode: int = 0, timed_out: bool = False) -> CommandResult:
    return CommandResult(
        argv=["go", "list"], returncode=returncode, stdout=stdout, stderr="", timed_out=timed_out
    )


def _go_list_returning(monkeypatch, result: CommandResult) -> dict:
    """Stub ``go list`` and capture the argv it was asked to run."""
    seen: dict = {}

    def fake_run(argv, cwd, timeout=120, env=None, isolate=False):
        seen["argv"] = list(argv)
        seen["isolate"] = isolate
        return result

    monkeypatch.setattr("coretexa_verify.runners.golang.run", fake_run)
    return seen


# --------------------------------------------------------------------------
# the default is silence
# --------------------------------------------------------------------------


def test_a_runner_that_cannot_tell_makes_no_claim():
    assert Runner("/repo", "r").coverage_gap(PROMPT_TEST, CHANGED) == ""


def test_no_go_source_changed_makes_no_claim(monkeypatch):
    """A PR that only edits TOML or docs says nothing about package coverage."""
    _go_list_returning(monkeypatch, _result(""))
    runner = GoTestRunner("/repo", "r")
    assert runner.coverage_gap(PROMPT_TEST, ["src/superfile_config/hotkeys.toml"]) == ""


def test_the_same_package_is_never_a_gap(monkeypatch):
    """The common case must not even shell out."""
    seen = _go_list_returning(monkeypatch, _result(""))
    runner = GoTestRunner("/repo", "r")
    gap = runner.coverage_gap(
        ["src/pkg/file_preview/thumb_test.go::TestThumb"], CHANGED
    )
    assert gap == ""
    assert "argv" not in seen, "go list was run for a same-package change"


# --------------------------------------------------------------------------
# the defect itself
# --------------------------------------------------------------------------


def test_a_test_that_does_not_import_the_changed_package_is_a_gap(monkeypatch):
    """superfile#1619, reduced."""
    _go_list_returning(
        monkeypatch,
        _result("/repo/src/internal/ui/prompt\n/repo/src/internal/common\n"),
    )
    gap = GoTestRunner("/repo", "r").coverage_gap(PROMPT_TEST, CHANGED)
    assert gap
    assert "src/pkg/file_preview" in gap
    assert "src/internal/ui/prompt" in gap


def test_the_gap_turns_a_passing_reverted_run_into_inconclusive():
    report = Report(verdict=Verdict.INCONCLUSIVE, headline="")
    report.head_run = TestRunResult(command=["go","test"], outcome=Outcome.PASS, passed=12, total=12)
    report.reverted_run = TestRunResult(command=["go","test"], outcome=Outcome.PASS, passed=12, total=12)
    report.reverted_files = list(CHANGED)
    report.coverage_gap = "the selected test package(s) do not import src/pkg/file_preview"

    decided = _decide_stage1(report)

    assert decided.verdict is Verdict.INCONCLUSIVE
    assert "no power to detect that revert" in decided.headline
    assert "would pass without the fix" not in decided.headline


def test_without_a_gap_the_same_run_is_still_no_gate():
    """Guard the guard: the downgrade must not swallow real findings."""
    report = Report(verdict=Verdict.INCONCLUSIVE, headline="")
    report.head_run = TestRunResult(command=["go","test"], outcome=Outcome.PASS, passed=12, total=12)
    report.reverted_run = TestRunResult(command=["go","test"], outcome=Outcome.PASS, passed=12, total=12)
    report.reverted_files = list(CHANGED)

    assert _decide_stage1(report).verdict is Verdict.NO_GATE


# --------------------------------------------------------------------------
# why directory disjointness is not enough
# --------------------------------------------------------------------------


def test_a_cross_package_import_is_not_a_gap(monkeypatch):
    """Different directories, but the test really does reach the changed code.

    This is the case a naive "are the directories the same?" check would get
    wrong, and it would get it wrong in the expensive direction: silently
    discarding a true NO_GATE.
    """
    _go_list_returning(
        monkeypatch,
        _result("/repo/src/internal/ui/prompt\n/repo/src/pkg/file_preview\n"),
    )
    assert GoTestRunner("/repo", "r").coverage_gap(PROMPT_TEST, CHANGED) == ""


def test_packages_outside_the_repo_are_ignored(monkeypatch):
    """The stdlib and the module cache are in the closure and are not ours."""
    _go_list_returning(
        monkeypatch,
        _result("/usr/local/go/src/fmt\n/root/go/pkg/mod/x\n/repo/src/internal/ui/prompt\n"),
    )
    assert GoTestRunner("/repo", "r").coverage_gap(PROMPT_TEST, CHANGED)


# --------------------------------------------------------------------------
# no answer is not a positive answer
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result",
    [
        _result("", returncode=1),
        _result("", timed_out=True),
        _result(""),
    ],
    ids=["go-list-failed", "go-list-timed-out", "go-list-said-nothing"],
)
def test_an_unusable_go_list_leaves_the_verdict_alone(monkeypatch, result):
    _go_list_returning(monkeypatch, result)
    assert GoTestRunner("/repo", "r").coverage_gap(PROMPT_TEST, CHANGED) == ""


def test_go_list_runs_isolated_like_every_other_repository_command(monkeypatch):
    """It executes repository-controlled build files, so it gets no credentials."""
    seen = _go_list_returning(monkeypatch, _result("/repo/src/internal/ui/prompt\n"))
    GoTestRunner("/repo", "r").coverage_gap(PROMPT_TEST, CHANGED)
    assert seen["isolate"] is True
    assert seen["argv"][:4] == ["go", "list", "-deps", "-test"]


def test_targets_are_made_relative_to_the_module_root(monkeypatch):
    """In a monorepo, go list is invoked from the module and wants module paths."""
    seen = _go_list_returning(monkeypatch, _result("/repo/sub/internal/prompt\n"))
    runner = GoTestRunner("/repo", "r", module="sub")
    runner.coverage_gap(
        ["sub/internal/prompt/tokenize_test.go::TestX"], ["sub/pkg/preview/gen.go"]
    )
    assert seen["argv"][-1] == "./internal/prompt"
