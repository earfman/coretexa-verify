"""Runner detection: never guess a command, and always explain the choice."""

import json
import os

import pytest

from coretexa_verify.runners import (
    DetectionFailed,
    JestRunner,
    NpmTestRunner,
    PytestRunner,
    VitestRunner,
    detect_javascript,
    detect_python,
    detect_runner,
)
from coretexa_verify.runners.base import DetectionContext


def write(root, rel, content=""):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def ctx(root):
    return DetectionContext(repo=str(root))


def test_pyproject_selects_pytest(tmp_path):
    write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    runner = detect_python(ctx(tmp_path), [])
    assert isinstance(runner, PytestRunner)
    assert "pyproject.toml" in runner.reason
    assert runner.id == "pytest"


def test_repo_venv_is_preferred_when_present(tmp_path):
    write(tmp_path, "setup.py")
    write(tmp_path, ".venv/bin/python", "#!/bin/sh\n")
    runner = detect_python(ctx(tmp_path), [])
    assert ".venv/bin/python" in runner.reason
    assert runner.launcher[0].endswith(".venv/bin/python")


def test_no_python_markers_means_no_python_runner(tmp_path):
    write(tmp_path, "main.go")
    assert detect_python(ctx(tmp_path), []) is None


def test_vitest_beats_jest_when_both_named(tmp_path):
    write(tmp_path, "package.json", json.dumps({"devDependencies": {"vitest": "^1", "jest": "^29"}}))
    runner = detect_javascript(ctx(tmp_path), [])
    assert isinstance(runner, VitestRunner)
    assert "vitest" in runner.reason


def test_jest_detected_from_dependencies(tmp_path):
    write(tmp_path, "package.json", json.dumps({"devDependencies": {"jest": "^29"}}))
    runner = detect_javascript(ctx(tmp_path), [])
    assert isinstance(runner, JestRunner)


def test_jest_detected_from_test_script(tmp_path):
    write(tmp_path, "package.json", json.dumps({"scripts": {"test": "jest --coverage"}}))
    assert isinstance(detect_javascript(ctx(tmp_path), []), JestRunner)


def test_generic_npm_test_is_last_resort(tmp_path):
    write(tmp_path, "package.json", json.dumps({"scripts": {"test": "node ./run-tests.js"}}))
    runner = detect_javascript(ctx(tmp_path), [])
    assert isinstance(runner, NpmTestRunner)
    assert "exit-code-only" in runner.reason


def test_placeholder_test_script_is_not_a_runner(tmp_path):
    write(tmp_path, "package.json", json.dumps(
        {"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}
    ))
    assert detect_javascript(ctx(tmp_path), []) is None


def test_malformed_package_json_does_not_crash(tmp_path):
    write(tmp_path, "package.json", "{not json")
    assert detect_javascript(ctx(tmp_path), []) is None


def test_detection_failure_names_what_was_tried(tmp_path):
    write(tmp_path, "main.go")
    with pytest.raises(DetectionFailed) as exc:
        detect_runner(str(tmp_path))
    assert "python" in str(exc.value)
    assert "javascript" in str(exc.value)
    assert "--test-command" in str(exc.value)


def test_python_wins_over_javascript_for_a_mixed_repo(tmp_path):
    write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    write(tmp_path, "package.json", json.dumps({"devDependencies": {"jest": "^29"}}))
    assert detect_runner(str(tmp_path)).id == "pytest"


def test_pytest_command_is_reproducible_and_explicit(tmp_path):
    write(tmp_path, "pyproject.toml")
    runner = detect_python(ctx(tmp_path), ["-x"])
    cmd = runner.build_command(["tests/test_a.py"], "/tmp/r.xml")
    assert "tests/test_a.py" in cmd
    assert "--junitxml=/tmp/r.xml" in cmd
    assert cmd[-1] == "-x"
