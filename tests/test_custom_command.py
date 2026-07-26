"""An explicit test command, and the detection message that recommends it.

Until 1.3.2 the "no test runner could be detected" error told the user to pass
``--test-command`` and no such flag existed — a dead end dressed up as advice.
This covers the flag, the two result-reading modes, and the corrected message.
"""

import os
import subprocess
import sys

import pytest

from coretexa_verify.cli import build_parser
from coretexa_verify.models import Outcome
from coretexa_verify.runners import CommandRunner, DetectionFailed, detect_runner
from coretexa_verify.runners.custom import (
    BUILD_ERROR_PATTERNS,
    TARGET_PLACEHOLDER,
    needs_shell,
    parse_custom_output,
)
from coretexa_verify.verify import VerifyOptions, verify


# --------------------------------------------------------------------------
# the detection message
# --------------------------------------------------------------------------


def test_the_detection_failure_recommends_a_flag_that_exists(tmp_path):
    root = str(tmp_path)
    with pytest.raises(DetectionFailed) as exc:
        detect_runner(root)
    message = str(exc.value)
    assert "--test-command" in message
    assert "test-command" in message  # the Action input
    assert "--junit-path" in message

    flags = {a.option_strings[0] for a in build_parser()._actions if a.option_strings}
    assert "--test-command" in flags, "the message must not recommend a flag we lack"
    assert "--junit-path" in flags


def test_the_detection_message_names_what_it_looked_for(tmp_path):
    with pytest.raises(DetectionFailed) as exc:
        detect_runner(str(tmp_path))
    message = str(exc.value)
    for marker in ("pyproject.toml", "package.json", "go.mod", "Cargo.toml", "pom.xml"):
        assert marker in message


def test_the_cli_passes_the_flags_through():
    args = build_parser().parse_args(
        ["--repo", ".", "--test-command", "make test", "--junit-path", "out/junit.xml"]
    )
    assert args.test_command == "make test"
    assert args.junit_path == "out/junit.xml"


# --------------------------------------------------------------------------
# command construction
# --------------------------------------------------------------------------


def test_a_simple_command_runs_as_argv_with_no_shell():
    runner = CommandRunner("/repo", "pytest -q")
    assert runner.build_command(["tests/test_a.py::test_x"], "/tmp/r.xml") == [
        "pytest",
        "-q",
        "tests/test_a.py::test_x",
    ]


def test_shell_syntax_goes_through_sh_minus_c():
    assert needs_shell("make build && make test")
    argv = CommandRunner("/repo", "make build && make test").build_command(["a"], "/tmp/r.xml")
    assert argv[:2] == ["/bin/sh", "-c"]
    assert "make build && make test a" == argv[2]


def test_targets_are_substituted_at_the_placeholder():
    runner = CommandRunner("/repo", f"bazel test {TARGET_PLACEHOLDER} --verbose")
    assert runner.build_command(["//pkg:a", "//pkg:b"], "/tmp/r.xml") == [
        "bazel",
        "test",
        "//pkg:a",
        "//pkg:b",
        "--verbose",
    ]


def test_the_placeholder_is_quoted_in_shell_mode():
    runner = CommandRunner("/repo", f"sh -c 'true' && run {TARGET_PLACEHOLDER}")
    argv = runner.build_command(["a b.py"], "/tmp/r.xml")
    assert "'a b.py'" in argv[2], "a target with a space must not split"


def test_no_targets_leaves_the_command_alone():
    assert CommandRunner("/repo", "make test").build_command([], "/tmp/r.xml") == [
        "make",
        "test",
    ]


def test_detection_is_replaced_not_consulted():
    runner = CommandRunner("/repo", "make test")
    assert runner.id == "custom-command"
    assert "detection was skipped" in runner.reason
    assert runner.collect(["x"], 10) is None
    assert runner.detect_build_step(10) is None
    assert runner.default_test_dir() is None


# --------------------------------------------------------------------------
# exit-code mode
# --------------------------------------------------------------------------


def test_exit_zero_is_a_pass():
    got = parse_custom_output(0, "all good", "")
    assert got.outcome is Outcome.PASS
    assert got.passed == 1
    assert "exit 0 taken as pass" in got.note
    assert "--junit-path" in got.note, "the note must say how to do better"


def test_a_plain_failure_is_an_assertion_failure():
    got = parse_custom_output(1, "FAIL: expected 3 got 4", "")
    assert got.outcome is Outcome.ASSERT_FAIL
    assert got.failed == 1
    assert "declared heuristic" in got.note


@pytest.mark.parametrize(
    "output",
    [
        "ModuleNotFoundError: No module named 'widget'",
        "ImportError: cannot import name 'thing'",
        "error[E0432]: unresolved import",
        "src/main.c:3:10: fatal error: missing.h: No such file or directory",
        "undefined reference to `frobnicate'",
        "Cannot find module './helper'",
        "make: *** No rule to make target 'test'.  Stop.",
        "error TS2304: Cannot find name 'x'",
        "cannot find symbol",
    ],
)
def test_build_signatures_are_classified_as_build_errors(output):
    got = parse_custom_output(2, output, "")
    assert got.outcome is Outcome.BUILD_ERROR
    assert got.errored == 1
    assert "matched /" in got.note, "the pattern that decided it must be named"


def test_the_heuristic_pattern_is_reported_verbatim():
    got = parse_custom_output(1, "", "ModuleNotFoundError: nope")
    assert any(p.strip("\\b") in got.note for p in BUILD_ERROR_PATTERNS)


# --------------------------------------------------------------------------
# JUnit mode
# --------------------------------------------------------------------------


JUNIT_PASS = """<testsuite name="s" tests="2">
  <testcase classname="T" name="a"/>
  <testcase classname="T" name="b"/>
</testsuite>
"""

JUNIT_FAIL = """<testsuite name="s" tests="2">
  <testcase classname="T" name="a"/>
  <testcase classname="T" name="b"><failure message="boom">x</failure></testcase>
</testsuite>
"""

JUNIT_ERROR = """<testsuite name="s" tests="1">
  <testcase classname="T" name="a"><error message="import">x</error></testcase>
</testsuite>
"""


def test_junit_mode_reads_real_counts(tmp_path):
    (tmp_path / "junit.xml").write_text(JUNIT_PASS)
    runner = CommandRunner(str(tmp_path), "make test", junit_path="junit.xml")
    got = runner.parse("/unused", 0, "", "")
    assert got.outcome is Outcome.PASS
    assert (got.passed, got.total) == (2, 2)
    assert "JUnit mode" in got.note


def test_junit_mode_separates_failure_from_error(tmp_path):
    (tmp_path / "junit.xml").write_text(JUNIT_FAIL)
    runner = CommandRunner(str(tmp_path), "make test", junit_path="junit.xml")
    got = runner.parse("/unused", 1, "", "")
    assert got.outcome is Outcome.ASSERT_FAIL
    assert got.failing_ids == ["T::b"]

    (tmp_path / "junit.xml").write_text(JUNIT_ERROR)
    got = runner.parse("/unused", 1, "", "")
    assert got.outcome is Outcome.BUILD_ERROR
    assert got.erroring_ids == ["T::a"]


def test_junit_mode_accepts_a_directory(tmp_path):
    reports = tmp_path / "surefire"
    reports.mkdir()
    (reports / "one.xml").write_text(JUNIT_PASS)
    (reports / "two.xml").write_text(JUNIT_FAIL)
    runner = CommandRunner(str(tmp_path), "make test", junit_path="surefire")
    got = runner.parse("/unused", 1, "", "")
    assert got.total == 4
    assert got.outcome is Outcome.ASSERT_FAIL


def test_a_declared_report_that_never_appeared_is_a_runner_error(tmp_path):
    """Not "everything passed", and not "the tests failed". We do not know."""
    runner = CommandRunner(str(tmp_path), "make test", junit_path="missing.xml")
    got = runner.parse("/unused", 0, "", "")
    assert got.outcome is Outcome.RUNNER_ERROR
    assert "no readable report was found" in got.note


def test_junit_mode_flags_a_clean_report_with_a_dirty_exit(tmp_path):
    (tmp_path / "junit.xml").write_text(JUNIT_PASS)
    runner = CommandRunner(str(tmp_path), "make test", junit_path="junit.xml")
    got = runner.parse("/unused", 3, "", "")
    assert got.outcome is Outcome.RUNNER_ERROR


# --------------------------------------------------------------------------
# what the user is warned about
# --------------------------------------------------------------------------


def test_exit_code_mode_warns_that_it_is_a_heuristic():
    runner = CommandRunner("/repo", "make test")
    assert any("heuristic" in w for w in runner.setup_warnings)
    assert any("--junit-path" in w for w in runner.setup_warnings)


def test_junit_mode_does_not_warn_about_the_heuristic():
    runner = CommandRunner("/repo", "make test", junit_path="j.xml")
    assert not any("heuristic" in w for w in runner.setup_warnings)


def test_every_custom_run_warns_about_the_missing_build_step():
    for runner in (CommandRunner("/repo", "m"), CommandRunner("/repo", "m", junit_path="j.xml")):
        assert any("no build step is detected" in w for w in runner.setup_warnings)


# --------------------------------------------------------------------------
# end to end, on a repository no detector recognises
# --------------------------------------------------------------------------


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def undetectable_repo(tmp_path):
    """A tiny C-ish project: no pyproject, no package.json, no go.mod."""
    root = str(tmp_path / "proj")
    os.makedirs(root)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    os.makedirs(os.path.join(root, "tests"))
    (tmp_path / "proj" / "value.txt").write_text("1\n")
    (tmp_path / "proj" / "tests" / "check.sh").write_text("#!/bin/sh\nexit 0\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    (tmp_path / "proj" / "value.txt").write_text("2\n")
    (tmp_path / "proj" / "tests" / "check.sh").write_text(
        '#!/bin/sh\ntest "$(cat value.txt)" = "2"\n'
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "head")
    return root


def test_without_a_command_the_repository_is_inconclusive(undetectable_repo):
    report = verify(
        VerifyOptions(repo=undetectable_repo, base="main~1", head="HEAD", install_deps=False)
    )
    assert report.verdict.value == "INCONCLUSIVE"
    assert "no test runner could be detected" in report.headline


def test_an_explicit_command_makes_the_experiment_run(undetectable_repo):
    """The gate really holds: reverting value.txt makes the script exit 1."""
    report = verify(
        VerifyOptions(
            repo=undetectable_repo,
            base="main~1",
            head="HEAD",
            install_deps=False,
            test_command="sh tests/check.sh",
        )
    )
    assert report.runner is not None
    assert report.runner.id == "custom-command"
    assert report.head_run is not None and report.head_run.outcome is Outcome.PASS
    assert report.verdict.value == "GATE_HOLDS"
    assert report.reverted_run.outcome is Outcome.ASSERT_FAIL
    assert any("run exactly as given" in w for w in report.warnings), (
        "an unmapped selection must be said out loud, not swallowed"
    )


def test_a_custom_command_still_obeys_the_clean_tree_rule(undetectable_repo):
    """No escape hatch bypasses the safety net."""
    with open(os.path.join(undetectable_repo, "value.txt"), "a") as fh:
        fh.write("dirty\n")
    report = verify(
        VerifyOptions(
            repo=undetectable_repo,
            base="main~1",
            head="HEAD",
            install_deps=False,
            test_command="sh tests/check.sh",
        )
    )
    assert report.verdict.value == "INCONCLUSIVE"
    assert "uncommitted changes" in report.headline


def test_a_custom_command_run_leaves_the_tree_pristine(undetectable_repo):
    from coretexa_verify.gitops import is_clean

    verify(
        VerifyOptions(
            repo=undetectable_repo,
            base="main~1",
            head="HEAD",
            install_deps=False,
            test_command="sh tests/check.sh",
        )
    )
    assert is_clean(undetectable_repo)
    assert open(os.path.join(undetectable_repo, "value.txt")).read() == "2\n"
