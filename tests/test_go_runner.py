"""Go runner: event-stream parsing, narrowing, package mapping, testdata proof.

Everything here runs against canned output, so no Go toolchain is needed.
"""

from __future__ import annotations

import json
import os


from coretexa_verify.classify import classify
from coretexa_verify.models import Kind, Outcome
from coretexa_verify.runners.golang import (
    GoTestRunner,
    go_test_spans,
    module_root,
    package_dir,
    parse_go_test_json,
    read_go_mod,
)
from coretexa_verify.runners.base import DetectionContext

PKG = "github.com/example/x/jsonpath"


def ev(**kw) -> str:
    return json.dumps(kw)


def stream(*events: str) -> str:
    return "\n".join(events) + "\n"


def write(root, rel, text=""):
    path = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


# ==========================================================================
# the event stream
# ==========================================================================


def test_a_clean_pass_counts_every_terminal_test_event():
    out = stream(
        ev(Action="start", Package=PKG),
        ev(Action="run", Package=PKG, Test="TestEval"),
        ev(Action="output", Package=PKG, Test="TestEval", Output="=== RUN   TestEval\n"),
        ev(Action="pass", Package=PKG, Test="TestEval/one", Elapsed=0),
        ev(Action="pass", Package=PKG, Test="TestEval", Elapsed=0.01),
        ev(Action="pass", Package=PKG, Elapsed=0.02),
    )
    res = parse_go_test_json(out, 0)
    assert res.outcome is Outcome.PASS
    assert res.passed == 2  # the subtest and its parent are distinct ids
    assert res.failed == res.errored == res.skipped == 0
    assert res.executed == 2


def test_an_assertion_failure_is_assert_fail_and_names_the_test():
    out = stream(
        ev(Action="start", Package=PKG),
        ev(Action="pass", Package=PKG, Test="TestA", Elapsed=0),
        ev(Action="fail", Package=PKG, Test="TestB/negative-index", Elapsed=0),
        ev(Action="fail", Package=PKG, Test="TestB", Elapsed=0),
        ev(Action="fail", Package=PKG, Elapsed=0.01),
    )
    res = parse_go_test_json(out, 1)
    assert res.outcome is Outcome.ASSERT_FAIL
    assert res.passed == 1 and res.failed == 2
    # The package-level fail must not be double counted as a build error: the
    # package plainly built, because two of its tests ran.
    assert res.errored == 0
    assert f"{PKG}::TestB/negative-index" in res.failing_ids


def test_a_package_that_fails_with_no_test_events_is_a_build_error():
    """This is the GATE_HOLDS_BUILD signal - the shape superfile #1552 produces."""
    out = stream(
        ev(Action="start", Package=PKG),
        ev(Action="output", Package=PKG, Output="# github.com/example/x/jsonpath [x.test]\n"),
        ev(Action="output", Package=PKG, Output="./x_test.go:93:19: undefined: formatUnknown\n"),
        ev(Action="output", Package=PKG, Output="FAIL\tgithub.com/example/x/jsonpath [build failed]\n"),
        ev(Action="fail", Package=PKG, Elapsed=0),
    )
    res = parse_go_test_json(out, 1)
    assert res.outcome is Outcome.BUILD_ERROR
    assert res.errored == 1 and res.passed == 0 and res.failed == 0
    assert res.erroring_ids == [PKG]
    assert "before any test ran" in res.note


def test_a_plain_text_build_failure_still_classifies_as_build_error():
    """Older toolchains never reach the JSON encoder; the text must still count."""
    out = "# github.com/example/x\n./a.go:3:2: undefined: Helper\nFAIL\tgithub.com/example/x [build failed]\n"
    res = parse_go_test_json(out, 2)
    assert res.outcome is Outcome.BUILD_ERROR
    assert res.errored == 1


def test_skips_are_excluded_from_the_executed_count():
    """A skipped test is not a passing test - the 1.2.1 rule, in Go."""
    out = stream(
        ev(Action="start", Package=PKG),
        ev(Action="skip", Package=PKG, Test="TestNeedsDocker", Elapsed=0),
        ev(Action="skip", Package=PKG, Test="TestNeedsNetwork", Elapsed=0),
        ev(Action="pass", Package=PKG, Elapsed=0.01),
    )
    res = parse_go_test_json(out, 0)
    assert res.outcome is Outcome.PASS
    assert res.skipped == 2
    # The skip guard in verify.py keys off exactly this.
    assert res.executed == 0


def test_no_matching_tests_is_no_tests_run_not_a_pass():
    out = stream(
        ev(Action="start", Package=PKG),
        ev(Action="output", Package=PKG, Output="testing: warning: no tests to run\n"),
        ev(Action="pass", Package=PKG, Elapsed=0),
    )
    res = parse_go_test_json(out, 0)
    assert res.outcome is Outcome.NO_TESTS_RUN


def test_a_nonzero_exit_with_nothing_reported_is_a_runner_error():
    res = parse_go_test_json("", 2, stderr="go: cannot find main module\n")
    assert res.outcome is Outcome.RUNNER_ERROR
    assert res.exit_code == 2


def test_a_toolchain_refusal_is_a_runner_error_not_a_build_error():
    """An environment mismatch says nothing about the code, so it may not gate."""
    err = "go: go.mod requires go >= 1.30 (running go 1.24.7; GOTOOLCHAIN=local)\n"
    res = parse_go_test_json("", 1, stderr=err)
    assert res.outcome is Outcome.RUNNER_ERROR
    assert "toolchain" in res.note
    # RUNNER_ERROR is in UNEVALUABLE_OUTCOMES, so it can never read as "gated".
    from coretexa_verify.models import UNEVALUABLE_OUTCOMES

    assert res.outcome in UNEVALUABLE_OUTCOMES


def test_malformed_json_lines_are_kept_as_text_rather_than_dropped():
    out = "not json at all\n" + ev(Action="pass", Package=PKG, Test="TestA", Elapsed=0) + "\n" + \
        ev(Action="pass", Package=PKG, Elapsed=0)
    res = parse_go_test_json(out, 0)
    assert res.outcome is Outcome.PASS and res.passed == 1


def test_a_report_shows_no_failure_but_go_exited_nonzero():
    out = stream(
        ev(Action="pass", Package=PKG, Test="TestA", Elapsed=0),
        ev(Action="pass", Package=PKG, Elapsed=0),
    )
    res = parse_go_test_json(out, 3)
    assert res.outcome is Outcome.RUNNER_ERROR


# ==========================================================================
# classification
# ==========================================================================


def test_go_test_files_are_recognised_as_executable_tests():
    c = classify("jsonpath/jsonpath_test.go")
    assert c.kind is Kind.TEST and c.executable_test


def test_a_plain_go_file_is_source():
    c = classify("jsonpath/jsonpath.go")
    assert c.kind is Kind.SOURCE


def test_testdata_is_a_fixture_not_a_module():
    c = classify("jsonpath/testdata/sample.json")
    assert c.kind is Kind.TEST and not c.executable_test


# ==========================================================================
# command construction and narrowing
# ==========================================================================


def test_the_command_runs_packages_and_narrows_with_an_anchored_run():
    runner = GoTestRunner("/repo", "x")
    argv = runner.build_command(["./jsonpath::TestA", "./jsonpath::TestB"], "/tmp/r.jsonl")
    assert argv[:4] == ["go", "test", "-json", "-count=1"]
    assert "-run" in argv
    pattern = argv[argv.index("-run") + 1]
    assert pattern == "^(TestA|TestB)$"
    assert argv[-1] == "./jsonpath"


def test_run_is_not_applied_when_only_some_targets_are_narrowed():
    """-run applies to every package on the line, so a mixed selection must not use it.

    Narrowing package A while package B is meant to run whole would silently
    drop most of B - a quieter and worse failure than running more than asked.
    """
    runner = GoTestRunner("/repo", "x")
    argv = runner.build_command(["./a::TestA", "./b"], "/tmp/r.jsonl")
    assert "-run" not in argv
    assert argv[-2:] == ["./a", "./b"]


def test_extracting_top_level_test_functions_with_their_line_spans():
    src = (
        "package x\n"
        "\n"
        "import \"testing\"\n"
        "\n"
        "func helper() int { return 1 }\n"
        "\n"
        "func TestAlpha(t *testing.T) {\n"
        "\tif helper() != 1 {\n"
        "\t\tt.Fatal(\"no\")\n"
        "\t}\n"
        "}\n"
        "\n"
        "func TestBeta(t *testing.T) {\n"
        "\tt.Log(\"hi\")\n"
        "}\n"
    )
    spans = go_test_spans(src)
    assert [n for n, _, _ in spans] == ["TestAlpha", "TestBeta"]
    assert spans[0][1] == 7 and spans[0][2] == 11
    assert spans[1][1] == 13


def test_fuzz_and_example_functions_count_as_test_entry_points():
    src = "package x\nfunc FuzzThing(f *testing.F) {\n}\nfunc ExampleThing() {\n}\n"
    assert [n for n, _, _ in go_test_spans(src)] == ["FuzzThing", "ExampleThing"]


def test_helper_functions_are_not_mistaken_for_tests():
    src = "package x\nfunc testHelper() {\n}\nfunc TestingUtil() {\n}\n"
    # `testHelper` is lowercase; `TestingUtil` does not start Test + uppercase.
    assert [n for n, _, _ in go_test_spans(src)] == []


# ==========================================================================
# packages, modules, testdata
# ==========================================================================


def test_package_dir_and_module_root(tmp_path):
    write(tmp_path, "go.mod", "module example.com/x\n\ngo 1.22\n")
    write(tmp_path, "src/internal/ui/metadata/architecture_test.go", "package metadata\n")
    assert package_dir("src/internal/ui/metadata/architecture_test.go") == "src/internal/ui/metadata"
    assert module_root(str(tmp_path), "src/internal/ui/metadata/architecture_test.go") == ""


def test_a_nested_go_mod_becomes_the_module_root(tmp_path):
    write(tmp_path, "go.mod", "module example.com/x\n")
    write(tmp_path, "tools/go.mod", "module example.com/x/tools\n")
    write(tmp_path, "tools/lint/lint_test.go", "package lint\n")
    assert module_root(str(tmp_path), "tools/lint/lint_test.go") == "tools"


def test_focus_maps_file_paths_to_package_patterns(tmp_path):
    write(tmp_path, "go.mod", "module example.com/x\n")
    write(tmp_path, "jsonpath/jsonpath_test.go", "package jsonpath\n")
    runner = GoTestRunner(str(tmp_path), "x")
    targets, why = runner.focus(["jsonpath/jsonpath_test.go"])
    assert targets == ["./jsonpath"]
    assert "package" in why
    assert runner.cwd == str(tmp_path)


def test_focus_moves_into_a_nested_module(tmp_path):
    write(tmp_path, "go.mod", "module example.com/x\n")
    write(tmp_path, "tools/go.mod", "module example.com/x/tools\n")
    write(tmp_path, "tools/lint/lint_test.go", "package lint\n")
    runner = GoTestRunner(str(tmp_path), "x")
    targets, _ = runner.focus(["tools/lint/lint_test.go::TestA"])
    assert targets == ["./lint::TestA"]
    assert runner.cwd == os.path.join(str(tmp_path), "tools")


def test_focus_refuses_targets_spanning_two_modules(tmp_path):
    write(tmp_path, "go.mod", "module example.com/x\n")
    write(tmp_path, "tools/go.mod", "module example.com/x/tools\n")
    write(tmp_path, "a/a_test.go", "package a\n")
    write(tmp_path, "tools/lint/lint_test.go", "package lint\n")
    runner = GoTestRunner(str(tmp_path), "x")
    assert runner.focus(["a/a_test.go", "tools/lint/lint_test.go"]) is None


def test_testdata_maps_to_its_owning_package_by_language_rule(tmp_path):
    """`pkg/testdata/x` is read by `pkg`'s tests. That is a toolchain guarantee."""
    write(tmp_path, "go.mod", "module example.com/x\n")
    write(tmp_path, "jsonpath/jsonpath_test.go", "package jsonpath\n")
    write(tmp_path, "jsonpath/helpers_test.go", "package jsonpath\n")
    write(tmp_path, "jsonpath/jsonpath.go", "package jsonpath\n")
    write(tmp_path, "jsonpath/testdata/sample.json", "{}")
    runner = GoTestRunner(str(tmp_path), "x")
    targets, detail, proof = runner.fixture_targets("jsonpath/testdata/sample.json")
    assert targets == ["jsonpath/helpers_test.go", "jsonpath/jsonpath_test.go"]
    assert "testdata" in detail
    assert "go help test" in proof and "working directory" in proof


def test_a_file_outside_testdata_gets_no_convention_mapping(tmp_path):
    write(tmp_path, "go.mod", "module example.com/x\n")
    runner = GoTestRunner(str(tmp_path), "x")
    assert runner.fixture_targets("jsonpath/fixtures/sample.json") is None


# ==========================================================================
# no separate build step
# ==========================================================================


def test_go_declares_no_build_step_and_no_artefact_risk():
    """`go test` compiles from source every run, so there is nothing to re-run.

    This is the build-artefact rule for a compiled language: the guarantee is
    not "we re-run the build", it is "the test command *is* the build".
    """
    runner = GoTestRunner("/repo", "x")
    assert runner.detect_build_step(900) is None
    assert runner.artifact_risk(["./pkg"], ["pkg/thing.go"]) == ""


# ==========================================================================
# detection
# ==========================================================================


def test_go_mod_directives_are_read(tmp_path):
    write(tmp_path, "go.mod", "module example.com/x\n\ngo 1.24\n\ntoolchain go1.26.3\n")
    ctx = DetectionContext(repo=str(tmp_path))
    assert read_go_mod(ctx) == ("1.24", "go1.26.3")


def test_detection_declines_a_repo_with_no_go_mod(tmp_path):
    from coretexa_verify.runners.golang import detect_go

    assert detect_go(DetectionContext(repo=str(tmp_path)), []) is None
