"""Maven/Gradle runner: surefire XML reading, class mapping, command shape.

Everything runs against canned XML and canned console output, which is the
whole extent of what this experimental runner has been validated against - see
the module docstring of coretexa_verify.runners.java.
"""

from __future__ import annotations

import os

from coretexa_verify.models import Outcome
from coretexa_verify.runners import junit
from coretexa_verify.runners.base import DetectionContext
from coretexa_verify.runners.java import (
    GradleRunner,
    MavenRunner,
    detect_java,
    fully_qualified_class,
    parse_junit_dirs,
)

SUREFIRE_PASS = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.example.FooTest" tests="3" errors="0" skipped="1" failures="0">
  <testcase name="addsTwoNumbers" classname="com.example.FooTest" time="0.01"/>
  <testcase name="handlesNull" classname="com.example.FooTest" time="0.02"/>
  <testcase name="needsDocker" classname="com.example.FooTest" time="0">
    <skipped message="docker unavailable"/>
  </testcase>
</testsuite>
"""

SUREFIRE_FAILURE = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.example.FooTest" tests="2" errors="0" skipped="0" failures="1">
  <testcase name="addsTwoNumbers" classname="com.example.FooTest" time="0.01">
    <failure message="expected:&lt;3&gt; but was:&lt;2&gt;" type="java.lang.AssertionError"/>
  </testcase>
  <testcase name="handlesNull" classname="com.example.FooTest" time="0.02"/>
</testsuite>
"""

SUREFIRE_ERROR = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.example.FooTest" tests="1" errors="1" skipped="0" failures="0">
  <testcase name="addsTwoNumbers" classname="com.example.FooTest" time="0">
    <error message="NoSuchMethodError: com.example.Foo.add" type="java.lang.NoSuchMethodError"/>
  </testcase>
</testsuite>
"""


def write(root, rel, text=""):
    path = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


# ==========================================================================
# JUnit XML - the reader shared with pytest
# ==========================================================================


def test_a_passing_surefire_report_counts_skips_separately(tmp_path):
    reports = os.path.join(str(tmp_path), "target/surefire-reports")
    write(tmp_path, "target/surefire-reports/TEST-com.example.FooTest.xml", SUREFIRE_PASS)
    res = parse_junit_dirs([reports], 0)
    assert res.outcome is Outcome.PASS
    assert res.passed == 2 and res.skipped == 1
    assert res.executed == 2  # a skip is not a passing test


def test_a_failure_element_is_an_assertion_failure(tmp_path):
    reports = os.path.join(str(tmp_path), "target/surefire-reports")
    write(tmp_path, "target/surefire-reports/TEST-com.example.FooTest.xml", SUREFIRE_FAILURE)
    res = parse_junit_dirs([reports], 1)
    assert res.outcome is Outcome.ASSERT_FAIL
    assert res.failed == 1 and res.passed == 1
    assert "com.example.FooTest::addsTwoNumbers" in res.failing_ids


def test_an_error_element_is_a_build_error_not_an_assertion(tmp_path):
    """`<error>` means the test never got to assert - GATE_HOLDS_BUILD, not GATE_HOLDS."""
    reports = os.path.join(str(tmp_path), "target/surefire-reports")
    write(tmp_path, "target/surefire-reports/TEST-com.example.FooTest.xml", SUREFIRE_ERROR)
    res = parse_junit_dirs([reports], 1)
    assert res.outcome is Outcome.BUILD_ERROR
    assert res.errored == 1 and res.failed == 0


def test_a_compilation_failure_writes_no_xml_and_is_still_a_build_error(tmp_path):
    reports = os.path.join(str(tmp_path), "target/surefire-reports")
    console = (
        "[ERROR] COMPILATION ERROR :\n"
        "[ERROR] /src/test/java/com/example/FooTest.java:[12,9] cannot find symbol\n"
        "[INFO] BUILD FAILURE\n"
    )
    res = parse_junit_dirs([reports], 1, stdout=console)
    assert res.outcome is Outcome.BUILD_ERROR
    assert "no assertion was exercised" in res.note


def test_no_xml_and_a_clean_exit_is_no_tests_run(tmp_path):
    res = parse_junit_dirs([os.path.join(str(tmp_path), "nowhere")], 0)
    assert res.outcome is Outcome.NO_TESTS_RUN


def test_no_xml_and_a_dirty_exit_with_no_signature_is_a_runner_error(tmp_path):
    res = parse_junit_dirs([os.path.join(str(tmp_path), "nowhere")], 1, stdout="something odd")
    assert res.outcome is Outcome.RUNNER_ERROR


def test_one_truncated_document_does_not_erase_the_others(tmp_path):
    """Surefire writes a file per class; a JVM crash must not lose the rest."""
    reports = os.path.join(str(tmp_path), "target/surefire-reports")
    write(tmp_path, "target/surefire-reports/TEST-com.example.FooTest.xml", SUREFIRE_PASS)
    write(tmp_path, "target/surefire-reports/TEST-com.example.BarTest.xml", "<testsuite><testc")
    res = parse_junit_dirs([reports], 0)
    assert res.outcome is Outcome.PASS and res.passed == 2


def test_the_shared_reader_is_the_one_pytest_uses(tmp_path):
    """The <failure>/<error> split is read in exactly one place, not three."""
    path = write(tmp_path, "r.xml", SUREFIRE_ERROR)
    counts = junit.read_reports([path])
    assert counts.parsed and counts.errored == 1 and counts.failed == 0

    from coretexa_verify.runners.python import parse_pytest_report

    res = parse_pytest_report(path, 1)
    assert res.outcome is Outcome.BUILD_ERROR and res.errored == 1


# ==========================================================================
# class names and commands
# ==========================================================================


def test_a_test_path_maps_to_a_fully_qualified_class_name():
    assert fully_qualified_class("src/test/java/com/example/FooTest.java") == "com.example.FooTest"
    assert (
        fully_qualified_class("modules/core/src/test/java/com/example/BarTest.java")
        == "com.example.BarTest"
    )
    assert fully_qualified_class("src/test/kotlin/com/example/BazTest.kt") == "com.example.BazTest"


def test_an_unconventional_layout_yields_no_class_name():
    """A guessed class name would be handed to -Dtest= and match nothing, silently."""
    assert fully_qualified_class("tests/FooTest.java") == ""


def test_maven_builds_a_dtest_selection(tmp_path):
    write(tmp_path, "pom.xml", "<project/>")
    runner = MavenRunner(str(tmp_path), "x")
    argv = runner.build_command(["com.example.FooTest", "com.example.BarTest"], "/tmp/r.xml")
    assert argv[:4] == ["mvn", "-B", "-q", "test"]
    assert "-Dtest=com.example.FooTest,com.example.BarTest" in argv
    assert "-Dsurefire.failIfNoSpecifiedTests=false" in argv


def test_gradle_repeats_the_tests_flag(tmp_path):
    write(tmp_path, "build.gradle", "")
    runner = GradleRunner(str(tmp_path), "x", wrapper=False)
    argv = runner.build_command(["com.example.FooTest", "com.example.BarTest"], "/tmp/r.xml")
    assert argv[0] == "gradle" and argv[1] == "test"
    assert argv.count("--tests") == 2


def test_focus_maps_paths_to_classes_and_finds_the_module(tmp_path):
    write(tmp_path, "pom.xml", "<project/>")
    write(tmp_path, "modules/core/pom.xml", "<project/>")
    write(tmp_path, "modules/core/src/test/java/com/example/FooTest.java", "")
    runner = MavenRunner(str(tmp_path), "x")
    targets, why = runner.focus(["modules/core/src/test/java/com/example/FooTest.java"])
    assert targets == ["com.example.FooTest"]
    assert runner.module == "modules/core"
    assert "modules/core" in why


def test_focus_refuses_targets_spanning_two_modules(tmp_path):
    write(tmp_path, "pom.xml", "<project/>")
    write(tmp_path, "a/pom.xml", "<project/>")
    write(tmp_path, "b/pom.xml", "<project/>")
    write(tmp_path, "a/src/test/java/com/example/ATest.java", "")
    write(tmp_path, "b/src/test/java/com/example/BTest.java", "")
    runner = MavenRunner(str(tmp_path), "x")
    assert runner.focus(
        ["a/src/test/java/com/example/ATest.java", "b/src/test/java/com/example/BTest.java"]
    ) is None


def test_a_test_resource_maps_to_the_modules_tests_by_build_tool_rule(tmp_path):
    write(tmp_path, "pom.xml", "<project/>")
    write(tmp_path, "src/test/java/com/example/FooTest.java", "")
    write(tmp_path, "src/test/resources/fixtures/sample.json", "{}")
    runner = MavenRunner(str(tmp_path), "x")
    targets, detail, proof = runner.fixture_targets("src/test/resources/fixtures/sample.json")
    assert targets == ["src/test/java/com/example/FooTest.java"]
    assert "test resource root" in detail
    assert "test classpath" in proof


# ==========================================================================
# detection, and its honesty about being experimental
# ==========================================================================


def test_maven_detection_warns_that_the_runner_is_experimental(tmp_path, monkeypatch):
    write(tmp_path, "pom.xml", "<project/>")
    monkeypatch.setattr("coretexa_verify.runners.java.shutil.which", lambda t: f"/usr/bin/{t}")
    runner = detect_java(DetectionContext(repo=str(tmp_path)), [])
    assert isinstance(runner, MavenRunner)
    assert any("experimental" in w for w in runner.setup_warnings)


def test_gradle_detection_prefers_the_wrapper(tmp_path, monkeypatch):
    write(tmp_path, "build.gradle.kts", "")
    write(tmp_path, "gradlew", "#!/bin/sh\n")
    monkeypatch.setattr("coretexa_verify.runners.java.shutil.which", lambda t: None)
    runner = detect_java(DetectionContext(repo=str(tmp_path)), [])
    assert isinstance(runner, GradleRunner) and runner.wrapper
    assert "./gradlew" in runner.reason


def test_detection_declines_a_non_jvm_repo(tmp_path):
    assert detect_java(DetectionContext(repo=str(tmp_path)), []) is None


def test_the_jvm_runner_declares_no_separate_build_step(tmp_path):
    """`mvn test` already depends on the compile goal, so there is nothing to re-run."""
    runner = MavenRunner(str(tmp_path), "x")
    assert runner.detect_build_step(900) is None
    assert runner.artifact_risk(["com.example.FooTest"], ["src/main/java/com/example/Foo.java"]) == ""
