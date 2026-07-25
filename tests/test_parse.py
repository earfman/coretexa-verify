"""Result parsing.

The assertion-failure vs build-failure distinction is what separates
GATE_HOLDS from GATE_HOLDS_BUILD, so it gets the most attention here.
"""

import json
import os

from coretexa_verify.models import Outcome
from coretexa_verify.runners.javascript import parse_exit_code_only, parse_jest_json
from coretexa_verify.runners.python import parse_collect_only, parse_pytest_report

JUNIT_PASS = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="0" skipped="0" tests="2">
<testcase classname="tests.test_a" name="test_one" time="0.01"/>
<testcase classname="tests.test_a" name="test_two" time="0.01"/>
</testsuite></testsuites>
"""

JUNIT_ASSERT_FAIL = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="0" tests="2">
<testcase classname="tests.test_a" name="test_one"/>
<testcase classname="tests.test_a" name="test_two">
<failure message="assert 1 == 2">boom</failure>
</testcase>
</testsuite></testsuites>
"""

JUNIT_COLLECTION_ERROR = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="0" skipped="0" tests="1">
<testcase classname="" name="tests/test_a.py">
<error message="collection failure">ImportError: cannot import name 'thing'</error>
</testcase>
</testsuite></testsuites>
"""

JUNIT_MIXED = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="1" skipped="1" tests="4">
<testcase classname="t" name="ok"/>
<testcase classname="t" name="bad"><failure message="x">boom</failure></testcase>
<testcase classname="t" name="broken"><error message="y">setup blew up</error></testcase>
<testcase classname="t" name="skipped"><skipped message="z"/></testcase>
</testsuite></testsuites>
"""


def write_report(tmp_path, text):
    path = os.path.join(tmp_path, "report.xml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def test_pytest_pass(tmp_path):
    r = parse_pytest_report(write_report(tmp_path, JUNIT_PASS), 0)
    assert r.outcome is Outcome.PASS
    assert (r.passed, r.failed, r.errored) == (2, 0, 0)


def test_pytest_assertion_failure_is_not_a_build_error(tmp_path):
    r = parse_pytest_report(write_report(tmp_path, JUNIT_ASSERT_FAIL), 1)
    assert r.outcome is Outcome.ASSERT_FAIL
    assert r.failed == 1
    assert r.failing_ids == ["tests.test_a::test_two"]


def test_pytest_collection_error_is_a_build_error(tmp_path):
    r = parse_pytest_report(write_report(tmp_path, JUNIT_COLLECTION_ERROR), 2)
    assert r.outcome is Outcome.BUILD_ERROR
    assert r.errored == 1
    assert "no assertion ran" in r.note


def test_an_assertion_failure_outranks_a_concurrent_error(tmp_path):
    # If anything actually asserted and failed, the gate genuinely held.
    r = parse_pytest_report(write_report(tmp_path, JUNIT_MIXED), 1)
    assert r.outcome is Outcome.ASSERT_FAIL
    assert (r.passed, r.failed, r.errored, r.skipped) == (1, 1, 1, 1)


def test_missing_report_never_reads_as_a_pass(tmp_path):
    r = parse_pytest_report(os.path.join(tmp_path, "nope.xml"), 1)
    assert r.outcome is Outcome.RUNNER_ERROR


def test_missing_report_with_exit_5_is_no_tests(tmp_path):
    r = parse_pytest_report(os.path.join(tmp_path, "nope.xml"), 5)
    assert r.outcome is Outcome.NO_TESTS_RUN


def test_missing_report_with_exit_2_is_a_build_error(tmp_path):
    r = parse_pytest_report(os.path.join(tmp_path, "nope.xml"), 2)
    assert r.outcome is Outcome.BUILD_ERROR


def test_clean_report_but_nonzero_exit_is_a_runner_error(tmp_path):
    # e.g. a coverage threshold or a plugin failing at teardown.
    r = parse_pytest_report(write_report(tmp_path, JUNIT_PASS), 1)
    assert r.outcome is Outcome.RUNNER_ERROR
    assert "exited 1" in r.note


def test_corrupt_xml_is_a_runner_error(tmp_path):
    r = parse_pytest_report(write_report(tmp_path, "<testsuite"), 0)
    assert r.outcome is Outcome.RUNNER_ERROR


def test_parse_collect_only_keeps_only_node_ids():
    stdout = (
        "tests/test_a.py::test_one\n"
        "tests/test_a.py::TestX::test_two\n"
        "tests/rules_test.py::test_case[ST05_thing]\n"
        "\n"
        "3 tests collected in 0.12s\n"
    )
    assert parse_collect_only(stdout) == [
        "tests/test_a.py::test_one",
        "tests/test_a.py::TestX::test_two",
        "tests/rules_test.py::test_case[ST05_thing]",
    ]


# --- javascript -------------------------------------------------------------


def jest_report(tmp_path, payload):
    path = os.path.join(tmp_path, "jest.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return path


def test_jest_pass(tmp_path):
    p = jest_report(tmp_path, {
        "numPassedTests": 3, "numFailedTests": 0, "numPendingTests": 0,
        "testResults": [{"name": "a.test.js", "status": "passed",
                         "assertionResults": [{"fullName": "a", "status": "passed"}]}],
    })
    assert parse_jest_json(p, 0).outcome is Outcome.PASS


def test_jest_assertion_failure(tmp_path):
    p = jest_report(tmp_path, {
        "numPassedTests": 1, "numFailedTests": 1,
        "testResults": [{"name": "a.test.js", "status": "failed", "assertionResults": [
            {"fullName": "keeps working", "status": "passed"},
            {"fullName": "detects the bug", "status": "failed"},
        ]}],
    })
    r = parse_jest_json(p, 1)
    assert r.outcome is Outcome.ASSERT_FAIL
    assert r.failing_ids == ["detects the bug"]


def test_jest_suite_that_would_not_load_is_a_build_error(tmp_path):
    p = jest_report(tmp_path, {
        "numPassedTests": 0, "numFailedTests": 0, "numRuntimeErrorTestSuites": 1,
        "testResults": [{"name": "a.test.js", "status": "failed",
                         "message": "Cannot find module './thing'", "assertionResults": []}],
    })
    r = parse_jest_json(p, 1)
    assert r.outcome is Outcome.BUILD_ERROR
    assert r.erroring_ids == ["a.test.js"]


def test_jest_missing_report_does_not_invent_a_result(tmp_path):
    r = parse_jest_json(os.path.join(tmp_path, "nope.json"), 1)
    assert r.outcome is Outcome.RUNNER_ERROR


def test_exit_code_only_declares_its_heuristic():
    ok = parse_exit_code_only(0, "", "")
    assert ok.outcome is Outcome.PASS

    build = parse_exit_code_only(1, "", "Error: Cannot find module './new-helper'")
    assert build.outcome is Outcome.BUILD_ERROR
    assert "heuristic" in build.note

    fail = parse_exit_code_only(1, "1 test failed\nexpected 3 to equal 4", "")
    assert fail.outcome is Outcome.ASSERT_FAIL
    assert "heuristic" in fail.note
