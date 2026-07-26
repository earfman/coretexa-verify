"""Fixture-to-consumer mapping and selection refinement."""

import os

from coretexa_verify.classify import ClassifierConfig
from coretexa_verify.refine import (
    added_fixture_keys,
    filter_collected_by_keys,
    python_test_node_ids,
    verify_against_collection,
)
from coretexa_verify.selection import candidate_tokens, enclosing_test_dir

CFG = ClassifierConfig()


def test_candidate_tokens_go_from_specific_to_general():
    tokens = candidate_tokens("test/fixtures/rules/std_rule_cases/ST05.yml", CFG)
    assert tokens[0] == "test/fixtures/rules/std_rule_cases/ST05.yml"
    assert "ST05.yml" in tokens
    # the distinctive fixture directory must be tried before the bare stem,
    # because a harness that globs the directory is the likeliest consumer
    assert tokens.index("std_rule_cases") < tokens.index("ST05")


def test_generic_directory_names_are_never_tokens():
    tokens = candidate_tokens("tests/fixtures/data/thing.yml", CFG)
    for generic in ("tests", "fixtures", "data"):
        assert generic not in tokens


def test_enclosing_test_dir_finds_the_deepest_dir_with_tests():
    executable = ["test/rules/yaml_test_cases_test.py", "test/core/x_test.py"]
    assert enclosing_test_dir(
        "/", "test/fixtures/rules/std_rule_cases/ST05.yml", executable
    ) == "test"
    assert enclosing_test_dir("/", "test/rules/case.yml", executable) == "test/rules"


def test_enclosing_test_dir_returns_none_when_nothing_matches():
    assert enclosing_test_dir("/", "a/b/c.yml", ["tests/test_x.py"]) is None


# --- changed test function selection ---------------------------------------

SOURCE = '''import pytest


class TestOld:
    def test_untouched_one(self):
        pass

    def test_untouched_two(self):
        pass


class TestNew:
    def test_added_one(self):
        pass

    def test_added_two(self):
        pass


def test_module_level_added():
    pass
'''


def line_of(marker: str) -> int:
    """1-based line number of the (unique) line containing ``marker``."""
    for i, line in enumerate(SOURCE.splitlines(), start=1):
        if marker in line:
            return i
    raise AssertionError(f"{marker!r} not in the sample source")


def test_a_wholly_new_class_collapses_to_the_class_id():
    start, end = line_of("class TestNew"), line_of("def test_added_two") + 1
    changed = set(range(start, end + 1))
    ids = python_test_node_ids(SOURCE, "t.py", changed)
    assert "t.py::TestNew" in ids
    assert not any(i.startswith("t.py::TestOld") for i in ids)


def test_a_single_changed_method_selects_only_that_method():
    changed = {line_of("def test_added_one") + 1}
    ids = python_test_node_ids(SOURCE, "t.py", changed)
    assert ids == ["t.py::TestNew::test_added_one"]


def test_module_level_test_function_is_selected():
    changed = {line_of("def test_module_level_added") + 1}
    ids = python_test_node_ids(SOURCE, "t.py", changed)
    assert ids == ["t.py::test_module_level_added"]


def test_untouched_tests_are_never_selected():
    # This is what keeps a neighbouring network-dependent legacy test from
    # turning a good run into INCONCLUSIVE.
    ids = python_test_node_ids(SOURCE, "t.py", {line_of("def test_added_one") + 1})
    assert all("TestOld" not in i for i in ids)


def test_unparseable_test_file_selects_nothing_rather_than_guessing():
    assert python_test_node_ids("def broken(:\n", "t.py", {1}) == []


def test_no_changed_lines_selects_nothing():
    assert python_test_node_ids(SOURCE, "t.py", set()) == []


# --- fixture key narrowing --------------------------------------------------


def test_filter_collected_by_keys_matches_parametrised_ids():
    collected = [
        "test/rules/yaml_test_cases_test.py::test__rule_test_case[ST05_new_case]",
        "test/rules/yaml_test_cases_test.py::test__rule_test_case[ST05_old_case]",
        "test/rules/yaml_test_cases_test.py::test__rule_test_global_config",
    ]
    hits = filter_collected_by_keys(collected, ["new_case"])
    assert hits == ["test/rules/yaml_test_cases_test.py::test__rule_test_case[ST05_new_case]"]


def test_filter_collected_by_keys_is_empty_without_keys():
    assert filter_collected_by_keys(["a::b[c]"], []) == []


def test_verify_against_collection_drops_ids_that_do_not_exist():
    collected = ["t.py::TestNew::test_a", "t.py::TestNew::test_b"]
    assert verify_against_collection(["t.py::TestNew"], collected) == ["t.py::TestNew"]
    assert verify_against_collection(["t.py::Ghost"], collected) == []


def test_verify_against_collection_accepts_parametrised_expansion():
    collected = ["t.py::test_x[1]", "t.py::test_x[2]"]
    assert verify_against_collection(["t.py::test_x"], collected) == ["t.py::test_x"]


def test_added_fixture_keys_ignores_non_fixture_extensions(tmp_path):
    # Guard the extension check without needing a repo.
    assert added_fixture_keys(str(tmp_path), "a", "b", "src/module.py") == []


# ==========================================================================
# polyglot repositories: a runner may only be offered files it can run
# ==========================================================================


def test_only_test_files_the_runner_can_execute_enter_the_candidate_pool(tmp_path):
    """sqlfluff is Python and vendors a Rust crate; pytest must not see the .rs.

    Without this filter, `sqlfluffrs/tests/fixture_tests.rs` joins the pool, a
    literal fixture search matches it, pytest is handed a `.rs` path, collection
    returns nothing, and a real GATE_HOLDS becomes INCONCLUSIVE.
    """
    import subprocess

    from coretexa_verify.selection import list_executable_tests

    repo = str(tmp_path)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    for rel in (
        "test/dialects/clickhouse_test.py",
        "sqlfluffrs/tests/fixture_tests.rs",
        "web/src/thing.test.ts",
    ):
        path = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)

    everything = list_executable_tests(repo, CFG)
    assert set(everything) == {
        "test/dialects/clickhouse_test.py",
        "sqlfluffrs/tests/fixture_tests.rs",
        "web/src/thing.test.ts",
    }
    assert list_executable_tests(repo, CFG, (".py",)) == ["test/dialects/clickhouse_test.py"]
    assert list_executable_tests(repo, CFG, (".rs",)) == ["sqlfluffrs/tests/fixture_tests.rs"]


def test_a_changed_test_file_the_runner_cannot_run_stops_being_a_target():
    """It stays TEST - still evidence, still never reverted - but is not executed."""
    from coretexa_verify.models import ChangedFile, Kind, Report, Verdict
    from coretexa_verify.runners.python import PytestRunner
    from coretexa_verify.verify import _demote_unrunnable_tests

    report = Report(verdict=Verdict.INCONCLUSIVE, headline="")
    report.changed_files = [
        ChangedFile("t/a_test.py", "M", Kind.TEST, "r", executable_test=True),
        ChangedFile("rs/tests/b.rs", "M", Kind.TEST, "r", executable_test=True),
    ]
    _demote_unrunnable_tests(report, PytestRunner("/repo", "x", ["python", "-m", "pytest"]))
    assert report.changed_files[0].executable_test
    assert not report.changed_files[1].executable_test
    assert report.changed_files[1].kind is Kind.TEST
    assert "not runnable by the detected pytest runner" in report.changed_files[1].reason
