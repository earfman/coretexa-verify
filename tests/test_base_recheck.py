"""Head failures re-checked at the merge base (defect D2).

superfile #1519 and #1560 both stopped at "the PR's tests do not pass at head",
and in both cases the failing test was ``TestZoxide``, which fails identically
on a clean ``origin/main`` because the machine has no ``zoxide`` binary. That
is a fact about the environment, not about either PR.

The rule these tests pin down: a head failure only blocks a verdict when it is
*this PR's* failure. Re-run the failing ids with source **and** tests at the
merge base and sort them into pre-existing (exclude and carry on), regressed
(this PR broke it) and new (the PR's own tests fail - a real finding).
"""

import os
import subprocess

import pytest

from coretexa_verify.models import (
    ChangedFile,
    Kind,
    Outcome,
    Report,
    TestRunResult,
    Verdict,
)
from coretexa_verify.report import render_text
from coretexa_verify.runners.base import Runner
from coretexa_verify.runners.golang import GoTestRunner
from coretexa_verify.verify import VerifyOptions, _pre_existing_note, _triage_head_failures


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """Two commits, so a revert to base is a real filesystem operation."""
    root = str(tmp_path / "repo")
    os.makedirs(root)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (tmp_path / "repo" / "mod.py").write_text("VALUE = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()
    (tmp_path / "repo" / "mod.py").write_text("VALUE = 2\n")
    (tmp_path / "repo" / "test_new.py").write_text("def test_new():\n    assert True\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "head")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()
    return root, base, head


def _run(outcome, passed=0, failed=0, failing=(), skipped=0):
    return TestRunResult(
        command=["fake"],
        outcome=outcome,
        passed=passed,
        failed=failed,
        skipped=skipped,
        total=passed + failed + skipped,
        failing_ids=list(failing),
    )


class FakeRunner(Runner):
    """Scripted runner: each ``execute`` tag maps to a canned result."""

    id = "fake"
    language = "python"

    def __init__(self, repo, runs, collected=None, base_collected=None):
        super().__init__(repo, "fake")
        self.runs = runs
        self._collected = collected
        self._base_collected = base_collected
        self.executed_tags = []
        self.executed_targets = {}

    @property
    def at_base(self) -> bool:
        """Read the tree, exactly as a real runner would.

        The base re-check reverts the working tree before collecting, so this
        also asserts that the revert really happened rather than trusting a
        flag we set ourselves.
        """
        with open(os.path.join(self.repo, "mod.py")) as fh:
            return fh.read() == "VALUE = 1\n"

    def execute(self, targets, timeout, report_dir, tag):
        self.executed_tags.append(tag)
        self.executed_targets[tag] = list(targets)
        return self.runs[tag]

    def collect(self, targets, timeout, extra=None):
        return self._base_collected if self.at_base else self._collected


def _report(repo_root, head_run):
    report = Report(verdict=Verdict.INCONCLUSIVE, headline="", repo=repo_root)
    report.head_run = head_run
    report.changed_files = [
        ChangedFile("mod.py", "M", Kind.SOURCE, ""),
        ChangedFile("test_new.py", "A", Kind.TEST, "", executable_test=True),
    ]
    return report


def _triage(repo_root, base, runner, report, targets):
    opts = VerifyOptions(repo=repo_root, timeout=30, max_collected=500)
    return _triage_head_failures(
        repo_root, opts, report, base, runner, targets, "/tmp", _baseline(repo_root)
    )


def _baseline(repo_root):
    from coretexa_verify.gitops import TreeState

    return TreeState.capture(repo_root)


# --------------------------------------------------------------------------
# pre-existing: exclude and carry on
# --------------------------------------------------------------------------


def test_a_failure_that_reproduces_at_base_is_excluded(repo):
    """superfile #1519: TestZoxide fails at head and on clean main alike."""
    root, base, head = repo
    runner = FakeRunner(
        root,
        runs={
            "base-recheck": _run(Outcome.ASSERT_FAIL, failed=1, failing=["t.py::TestZoxide"]),
            "head-minus-pre-existing": _run(Outcome.PASS, passed=7),
        },
        collected=["t.py::TestZoxide", "t.py::TestA", "t.py::TestB"],
        base_collected=["t.py::TestZoxide"],
    )
    head_run = _run(Outcome.ASSERT_FAIL, passed=7, failed=1, failing=["t.py::TestZoxide"])
    report = _report(root, head_run)

    got = _triage(root, base, runner, report, ["t.py"])

    assert not isinstance(got, str), got
    targets, retry = got
    assert "t.py::TestZoxide" not in targets
    assert set(targets) == {"t.py::TestA", "t.py::TestB"}
    assert retry.outcome is Outcome.PASS
    assert report.pre_existing_failures == ["t.py::TestZoxide"]
    assert report.prior_head_run is head_run
    assert report.head_run is retry


def test_the_headline_names_the_excluded_failures(repo):
    report = Report(verdict=Verdict.GATE_HOLDS, headline="")
    report.pre_existing_failures = ["t.py::TestZoxide"]
    note = _pre_existing_note(report)
    assert "Pre-existing failures (1), excluded" in note
    assert "TestZoxide" in note


def test_the_report_shows_both_head_runs(repo):
    report = Report(verdict=Verdict.GATE_HOLDS, headline="h")
    report.prior_head_run = _run(Outcome.ASSERT_FAIL, passed=7, failed=1, failing=["a::b"])
    report.base_recheck_run = _run(Outcome.ASSERT_FAIL, failed=1, failing=["a::b"])
    report.head_run = _run(Outcome.PASS, passed=7)
    report.pre_existing_note = "pre-existing failures (1), excluded: a::b"
    text = render_text(report)
    assert "re-run of those failures at the merge base" in text
    assert "run at head, pre-existing failures excluded" in text
    assert "pre-existing: pre-existing failures (1), excluded" in text


# --------------------------------------------------------------------------
# the PR's own new tests failing is a finding, not a limitation
# --------------------------------------------------------------------------


def test_a_test_that_does_not_exist_at_base_keeps_the_verdict_inconclusive(repo):
    """superfile #1534: the flaky SSH tests are the PR's own."""
    root, base, head = repo
    runner = FakeRunner(
        root,
        runs={"base-recheck": _run(Outcome.NO_TESTS_RUN)},
        collected=["t.py::TestSSH", "t.py::TestA"],
        base_collected=["t.py::TestA"],  # TestSSH is new in this PR
    )
    head_run = _run(Outcome.ASSERT_FAIL, passed=3, failed=1, failing=["t.py::TestSSH"])
    report = _report(root, head_run)

    got = _triage(root, base, runner, report, ["t.py"])

    assert isinstance(got, str)
    assert "the PR's own new tests fail" in got
    assert "TestSSH" in got
    assert report.head_run is head_run, "the head run must not be replaced"
    assert report.pre_existing_failures == []


def test_a_new_test_is_still_new_when_the_base_cannot_enumerate(repo):
    """No collection at base, and the re-run collects nothing: the test is new."""
    root, base, head = repo
    runner = FakeRunner(
        root,
        runs={"base-recheck": _run(Outcome.NO_TESTS_RUN)},
        collected=["t.py::TestSSH"],
        base_collected=None,
    )
    head_run = _run(Outcome.ASSERT_FAIL, failed=1, failing=["t.py::TestSSH"])
    report = _report(root, head_run)
    got = _triage(root, base, runner, report, ["t.py"])
    assert isinstance(got, str) and "the PR's own new tests fail" in got


def test_a_package_the_pr_added_does_not_hide_a_new_test(repo):
    """superfile #1534 exactly: one container is new, the others are not.

    Enumerating everything in one call fails because ``./internal/ssh`` does
    not exist at base, and the new SSH test then looks like a test this PR
    broke. Per-container enumeration keeps the two apart.
    """
    root, base, head = repo

    class PerContainer(FakeRunner):
        def collect(self, targets, timeout, extra=None):
            if not self.at_base:
                return ["ssh.py::TestSSH", "zox.py::TestZoxide"]
            containers = {t.partition("::")[0] for t in targets}
            if containers == {"ssh.py"}:
                return None  # the package does not exist at base
            return ["zox.py::TestZoxide"]

    runner = PerContainer(
        root,
        runs={
            "base-recheck": _run(
                Outcome.ASSERT_FAIL, failed=1, failing=["zox.py::TestZoxide"]
            )
        },
    )
    head_run = _run(
        Outcome.ASSERT_FAIL,
        passed=2,
        failed=2,
        failing=["ssh.py::TestSSH", "zox.py::TestZoxide"],
    )
    report = _report(root, head_run)
    got = _triage(root, base, runner, report, ["ssh.py", "zox.py"])
    assert isinstance(got, str)
    assert "the PR's own new tests fail" in got
    assert "ssh.py::TestSSH" in got
    assert "pass with this PR's source" not in got, "it never existed at base"


def test_a_test_that_passes_at_base_is_this_prs_regression(repo):
    root, base, head = repo
    runner = FakeRunner(
        root,
        runs={"base-recheck": _run(Outcome.PASS, passed=1)},
        collected=["t.py::TestA"],
        base_collected=["t.py::TestA"],
    )
    head_run = _run(Outcome.ASSERT_FAIL, failed=1, failing=["t.py::TestA"])
    report = _report(root, head_run)
    got = _triage(root, base, runner, report, ["t.py"])
    assert isinstance(got, str)
    assert "pass with this PR's source and tests reverted" in got


def test_a_mixture_of_new_and_pre_existing_stays_inconclusive(repo):
    """One genuine failure is enough; we do not excuse it by the other."""
    root, base, head = repo
    runner = FakeRunner(
        root,
        runs={"base-recheck": _run(Outcome.ASSERT_FAIL, failed=1, failing=["t.py::TestZoxide"])},
        collected=["t.py::TestZoxide", "t.py::TestSSH"],
        base_collected=["t.py::TestZoxide"],
    )
    head_run = _run(
        Outcome.ASSERT_FAIL, failed=2, failing=["t.py::TestZoxide", "t.py::TestSSH"]
    )
    report = _report(root, head_run)
    got = _triage(root, base, runner, report, ["t.py"])
    assert isinstance(got, str) and "the PR's own new tests fail" in got


# --------------------------------------------------------------------------
# refusals that must stay refusals
# --------------------------------------------------------------------------


def test_a_build_error_at_head_is_not_triaged(repo):
    root, base, head = repo
    runner = FakeRunner(root, runs={})
    report = _report(root, _run(Outcome.BUILD_ERROR))
    assert _triage(root, base, runner, report, ["t.py"]) == ""
    assert runner.executed_tags == [], "nothing should have been run"


def test_a_runner_that_cannot_re_run_a_subset_gives_up_cleanly(repo):
    root, base, head = repo

    class NoSubset(FakeRunner):
        def rerun_targets(self, failing_ids, current_targets):
            return None

    runner = NoSubset(root, runs={})
    report = _report(root, _run(Outcome.ASSERT_FAIL, failed=1, failing=["t.py::TestA"]))
    assert _triage(root, base, runner, report, ["t.py"]) == ""
    assert any("cannot re-run a named subset" in w for w in report.warnings)


def test_every_selected_test_failing_at_base_leaves_nothing_to_run(repo):
    root, base, head = repo
    runner = FakeRunner(
        root,
        runs={"base-recheck": _run(Outcome.ASSERT_FAIL, failed=1, failing=["t.py::TestA"])},
        collected=["t.py::TestA"],
        base_collected=["t.py::TestA"],
    )
    report = _report(root, _run(Outcome.ASSERT_FAIL, failed=1, failing=["t.py::TestA"]))
    got = _triage(root, base, runner, report, ["t.py"])
    assert isinstance(got, str) and "nothing is left to run" in got


def test_a_still_failing_retry_does_not_become_a_verdict(repo):
    root, base, head = repo
    runner = FakeRunner(
        root,
        runs={
            "base-recheck": _run(Outcome.ASSERT_FAIL, failed=1, failing=["t.py::TestZoxide"]),
            "head-minus-pre-existing": _run(
                Outcome.ASSERT_FAIL, failed=1, failing=["t.py::TestB"]
            ),
        },
        collected=["t.py::TestZoxide", "t.py::TestB"],
        base_collected=["t.py::TestZoxide", "t.py::TestB"],
    )
    report = _report(root, _run(Outcome.ASSERT_FAIL, failed=1, failing=["t.py::TestZoxide"]))
    got = _triage(root, base, runner, report, ["t.py"])
    assert isinstance(got, str)
    assert "still" in got and "does not pass at head" in got
    assert report.head_run.outcome is Outcome.ASSERT_FAIL


def test_the_tree_is_restored_after_the_base_recheck(repo):
    from coretexa_verify.gitops import is_clean

    root, base, head = repo
    runner = FakeRunner(
        root,
        runs={
            "base-recheck": _run(Outcome.ASSERT_FAIL, failed=1, failing=["t.py::TestZoxide"]),
            "head-minus-pre-existing": _run(Outcome.PASS, passed=1),
        },
        collected=["t.py::TestZoxide", "t.py::TestA"],
        base_collected=["t.py::TestZoxide"],
    )
    report = _report(root, _run(Outcome.ASSERT_FAIL, failed=1, failing=["t.py::TestZoxide"]))
    _triage(root, base, runner, report, ["t.py"])
    assert is_clean(root)
    assert open(os.path.join(root, "mod.py")).read() == "VALUE = 2\n"
    assert os.path.exists(os.path.join(root, "test_new.py"))


# --------------------------------------------------------------------------
# runner vocabularies
# --------------------------------------------------------------------------


def test_the_default_runner_treats_a_failing_id_as_a_target():
    runner = Runner("/tmp", "r")
    assert runner.rerun_targets(["a.py::test_x"], ["a.py"]) == ["a.py::test_x"]
    assert runner.rerun_targets(["not-an-id"], ["a.py"]) is None
    assert runner.test_key("a.py::test_x") == "a.py::test_x"


def test_go_rerun_pairs_failing_names_with_the_packages_being_run():
    """A Go failure names the package by import path; a target does not."""
    runner = GoTestRunner("/tmp", "r")
    got = runner.rerun_targets(
        ["github.com/x/y/internal::TestZoxide", "github.com/x/y/internal::TestOther"],
        ["./internal"],
    )
    assert got == ["./internal::TestZoxide", "./internal::TestOther"]


def test_go_test_key_drops_the_package_and_the_subtest_path():
    runner = GoTestRunner("/tmp", "r")
    assert runner.test_key("github.com/x/y/internal::TestZoxide/case-1") == "TestZoxide"
    assert runner.test_key("./internal::TestZoxide") == "TestZoxide"


def test_go_rerun_needs_both_a_name_and_a_package():
    runner = GoTestRunner("/tmp", "r")
    assert runner.rerun_targets([], ["./internal"]) is None
    assert runner.rerun_targets(["pkg::TestA"], []) is None
