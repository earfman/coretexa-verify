"""File classification: the decision that everything else depends on."""

import pytest

from coretexa_verify.classify import (
    ClassifierConfig,
    classify,
    is_executable_test_name,
    matching_test_dir,
)
from coretexa_verify.models import Kind


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_thing.py",
        "test/thing_test.py",
        "src/pkg/tests/test_x.py",
        "conftest.py",
        "test/conftest.py",
        # camel-case style used by e.g. the CMS project
        "cmstestsuite/unit_tests/cmscontrib/ItalyYamlLoaderTest.py",
        "cmstestsuite/unit_tests/grading/tasktypes/BatchAndOutputTest.py",
        # JS/TS conventions
        "src/components/Button.test.tsx",
        "src/components/Button.spec.ts",
        "__tests__/thing.js",
        "spec/thing.js",
    ],
)
def test_test_files_are_tests(path):
    assert classify(path).kind is Kind.TEST


@pytest.mark.parametrize(
    "path",
    [
        "src/pkg/module.py",
        "cmscontrib/loaders/italy_yaml.py",
        "src/sqlfluff/rules/structure/ST05.py",
        "lib/index.ts",
        "setup.py",
        "package.json",
        # 'latest' contains the substring 'test' but is not a test directory
        "src/latest/api/module.py",
        "src/contest/scoring.py",
    ],
)
def test_source_files_are_source(path):
    assert classify(path).kind is Kind.SOURCE, classify(path)


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "docs/guide.rst",
        "LICENSE",
        "CHANGELOG.md",
        ".github/workflows/ci.yml",
        "docs/img/diagram.png",
    ],
)
def test_docs_and_metadata_are_other(path):
    assert classify(path).kind is Kind.OTHER


def test_fixtures_under_a_test_tree_are_tests_but_not_runnable():
    c = classify("test/fixtures/rules/std_rule_cases/ST05.yml")
    assert c.kind is Kind.TEST
    assert c.executable_test is False
    assert "test directory" in c.reason


def test_runnable_test_modules_are_flagged():
    assert classify("tests/test_thing.py").executable_test is True
    assert classify("tests/helpers.py").executable_test is False


def test_helper_module_in_test_tree_is_test_data_not_runnable():
    c = classify("tests/helpers.py")
    assert c.kind is Kind.TEST
    assert c.executable_test is False


def test_markdown_inside_a_test_tree_is_still_test():
    # Reverting it cannot matter, but it must not be counted as source either.
    assert classify("test/AGENTS.md").kind is Kind.TEST


def test_force_globs_win():
    cfg = ClassifierConfig(force_source_globs=["tests/support/shim.py"])
    assert classify("tests/support/shim.py", cfg).kind is Kind.SOURCE

    cfg = ClassifierConfig(force_test_globs=["scenarios/*"])
    assert classify("scenarios/basic.yaml", cfg).kind is Kind.TEST


def test_matching_test_dir_reports_the_marker():
    assert matching_test_dir("a/tests/b/c.yml") == "tests"
    assert matching_test_dir("cmstestsuite/unit_tests/x.py") == "cmstestsuite"
    assert matching_test_dir("src/pkg/module.py") is None
    assert matching_test_dir("src/latest/thing.py") is None


def test_only_code_extensions_can_be_runnable_tests():
    assert is_executable_test_name("tests/test_data.yml") is False
    assert is_executable_test_name("tests/test_data.py") is True


def test_a_directory_named_test_makes_everything_under_it_test():
    assert classify("test/fixtures/dialects/ansi/select.sql").kind is Kind.TEST
    assert classify("test/fixtures/dialects/ansi/select.yml").kind is Kind.TEST


def test_config_replaces_rather_than_extends():
    cfg = ClassifierConfig(executable_test_patterns=["check_*.py"], test_dir_patterns=[])
    assert classify("tests/test_thing.py", cfg).kind is Kind.SOURCE
    assert classify("tests/check_thing.py", cfg).kind is Kind.TEST
