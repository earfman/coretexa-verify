"""D3: a skipped test is not a passing test.

pytest exits 0 when 100% of the selected tests skip, and the JUnit report then
reads ``0 passed, N skipped``. Reverting the source and getting the same
``0 passed, N skipped`` back proves nothing whatsoever - but the 1.2.0 verdict
logic compared count tuples, found them equal, and emitted a confident
``NO_GATE`` backed by zero executed assertions.

This is not a corner case. Optional dependencies, platform guards, absent
services and unbuilt artefacts all produce it; sqlfluff #8225 hit it because the
Rust parser the skipped tests need was never built, so head and reverted were
both "12 passed, 52 skipped".
"""

import os
import subprocess

import pytest

from coretexa_verify.models import Outcome, Report, TestRunResult, Verdict
from coretexa_verify.verify import (
    VerifyOptions,
    _all_skipped_reason,
    _enforce_soundness,
    _skip_note,
    verify,
)

SKIP_ALL = '''\
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:  # an optional dependency that is definitely not installed
    import coretexa_verify_definitely_absent_dependency  # noqa: F401

    HAVE_OPTIONAL = True
except ImportError:
    HAVE_OPTIONAL = False

pytestmark = pytest.mark.skipif(not HAVE_OPTIONAL, reason="optional dependency missing")


def test_shouts():
    from src.opt import shout

    assert shout("ab") == "AB"


def test_shouts_twice():
    from src.opt import shout

    assert shout("cd") == "CD"
'''

SKIP_SOME = '''\
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import coretexa_verify_definitely_absent_dependency  # noqa: F401

    HAVE_OPTIONAL = True
except ImportError:
    HAVE_OPTIONAL = False


@pytest.mark.skipif(not HAVE_OPTIONAL, reason="optional dependency missing")
def test_needs_the_optional_dependency():
    from src.opt import shout

    assert shout("ab") == "AB"


def test_runs_for_real():
    from src.opt import shout

    assert shout("cd") == "CD"
'''


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def build_repo(tmp_path, name, test_body):
    """A repo whose PR changes source *and* a test that may not execute."""
    root = str(tmp_path / name)
    os.makedirs(root)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")

    write(root, "pytest.ini", "[pytest]\n")
    write(root, "src/__init__.py", "")
    write(root, "src/opt.py", "def shout(value):\n    return value\n")
    write(root, "tests/opt_test.py", "def test_placeholder():\n    assert True\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()

    write(root, "src/opt.py", "def shout(value):\n    return value.upper()\n")
    write(root, "tests/opt_test.py", test_body)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "head")
    return root, base


# --------------------------------------------------------------------------
# the pieces
# --------------------------------------------------------------------------


def test_executed_excludes_skips():
    run = TestRunResult(command=[], outcome=Outcome.PASS, passed=0, skipped=52, total=52)
    assert run.executed == 0
    run = TestRunResult(
        command=[], outcome=Outcome.PASS, passed=12, skipped=52, total=64
    )
    assert run.executed == 12


def test_all_skipped_reason_names_the_count():
    run = TestRunResult(command=[], outcome=Outcome.PASS, passed=0, skipped=52, total=52)
    reason = _all_skipped_reason(run)
    assert "all 52 selected test(s) were skipped" in reason
    assert "nothing executed" in reason


def test_skip_note_is_empty_when_nothing_skipped():
    report = Report(Verdict.NO_GATE, "h")
    report.head_run = TestRunResult(command=[], outcome=Outcome.PASS, passed=4, total=4)
    assert _skip_note(report) == ""


def test_skip_note_names_the_split_when_some_skipped():
    report = Report(Verdict.NO_GATE, "h")
    report.head_run = TestRunResult(
        command=[], outcome=Outcome.PASS, passed=12, skipped=52, total=64
    )
    note = _skip_note(report)
    assert "52 of the 64 selected test(s) were skipped" in note
    assert "rests only on the 12 that ran" in note


@pytest.mark.parametrize("verdict", [Verdict.NO_GATE, Verdict.GATE_HOLDS, Verdict.GATE_HOLDS_BUILD])
def test_no_verdict_survives_a_run_that_executed_nothing(verdict, tmp_path):
    """Both signs of verdict are refused, not just the negative one."""
    report = Report(verdict, "some confident headline")
    report.head_run = TestRunResult(
        command=[], outcome=Outcome.PASS, passed=0, skipped=52, total=52
    )
    out = _enforce_soundness(
        str(tmp_path), VerifyOptions(repo=str(tmp_path)), report, "base",
        None, [], [], str(tmp_path), None,
    )
    assert out.verdict is Verdict.INCONCLUSIVE
    assert "all 52 selected test(s) were skipped" in out.headline


def test_a_partially_skipped_verdict_keeps_its_verdict_and_gains_the_note(tmp_path):
    report = Report(Verdict.GATE_HOLDS, "Reverting x makes 1 test fail.")
    report.head_run = TestRunResult(
        command=[], outcome=Outcome.PASS, passed=12, skipped=52, total=64
    )
    out = _enforce_soundness(
        str(tmp_path), VerifyOptions(repo=str(tmp_path)), report, "base",
        None, [], [], str(tmp_path), None,
    )
    assert out.verdict is Verdict.GATE_HOLDS
    assert "52 of the 64 selected test(s) were skipped" in out.headline


# --------------------------------------------------------------------------
# end to end
# --------------------------------------------------------------------------


def test_a_wholly_skipped_suite_is_inconclusive_never_no_gate(tmp_path):
    """The D3 repro: every selected test skips, so nothing can have noticed."""
    root, base = build_repo(tmp_path, "allskip", SKIP_ALL)
    report = verify(
        VerifyOptions(repo=root, base=base, head="HEAD", install_deps=False, timeout=120)
    )
    assert report.verdict is not Verdict.NO_GATE, report.headline
    assert report.verdict is Verdict.INCONCLUSIVE, report.headline
    assert "were skipped - nothing executed" in report.headline
    assert report.head_run is not None
    assert report.head_run.outcome is Outcome.PASS, "pytest really does exit 0 here"
    assert report.head_run.passed == 0 and report.head_run.skipped == 2
    assert report.head_run.executed == 0
    # and we never wasted a revert on it
    assert report.reverted_run is None


def test_a_partly_skipped_suite_still_reaches_a_verdict(tmp_path):
    """The guard must not swallow suites where something did execute."""
    root, base = build_repo(tmp_path, "someskip", SKIP_SOME)
    report = verify(
        VerifyOptions(repo=root, base=base, head="HEAD", install_deps=False, timeout=120)
    )
    assert report.verdict is Verdict.GATE_HOLDS, report.headline
    assert report.head_run.passed == 1 and report.head_run.skipped == 1
    assert "1 of the 2 selected test(s) were skipped" in report.headline
