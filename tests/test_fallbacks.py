"""Defects 3, 4 and 5, plus the two smaller items.

* Defect 3 - a failing installer falls back down the detection table instead of
  walking into a guaranteed INCONCLUSIVE, and every attempt is reported.
* Defect 4 - a widened selection is bounded by how many tests it *collects*,
  not by how many command-line arguments it happens to have.
* Defect 5 - a build step is re-run around every mutation, and a NO_GATE that
  could have been served by stale build output is refused.
"""

import os
import shutil

import pytest

from coretexa_verify import deps as depsmod
from coretexa_verify.classify import ClassifierConfig
from coretexa_verify.deps import (
    InstallPlan,
    detect_install_chain,
    failure_reason,
    javascript_install_plans,
    python_install_plans,
    run_plans,
)
from coretexa_verify.models import (
    ChangedFile,
    Kind,
    Outcome,
    Report,
    SelectionEntry,
    TestRunResult,
    Verdict,
)
from coretexa_verify.runners.base import BuildStep, Runner
from coretexa_verify.runners.javascript import (
    detect_build_step,
    entry_points_are_built,
    javascript_artifact_risk,
    owning_package,
    workspace_globs,
    workspace_package_dirs,
)
from coretexa_verify.verify import (
    VerifyOptions,
    _collection_cap,
    _enforce_soundness,
    _runs_differ,
    _soften_deletion_only,
)


def write(root, rel, text):
    path = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    return path


# --------------------------------------------------------------------------
# Defect 3: installer fallback
# --------------------------------------------------------------------------


@pytest.fixture
def uv_repo(tmp_path, monkeypatch):
    """A repo whose first-choice installer is uv but which pip could also do."""
    write(
        tmp_path,
        "pyproject.toml",
        '[project]\nname = "x"\n[project.optional-dependencies]\ntest = ["pytest"]\n',
    )
    write(tmp_path, "uv.lock", "version = 1\n")
    write(tmp_path, "requirements-dev.txt", "pytest\n")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    return str(tmp_path)


def test_python_chain_puts_uv_first_then_walks_the_pip_path(uv_repo):
    plans, note = python_install_plans(uv_repo, "/py")
    assert note == ""
    detectors = [p.detector for p in plans]
    assert detectors[0] == "python:uv"
    assert detectors[1] == "python:pyproject-extra"
    assert "python:requirements-dev" in detectors
    assert detectors[-1] == "python:editable"
    assert plans[0].commands == [["uv", "sync", "--frozen"]]


def test_chain_never_repeats_the_same_command(uv_repo):
    plans, _ = python_install_plans(uv_repo, "/py")
    seen = [tuple(map(tuple, p.commands)) for p in plans]
    assert len(seen) == len(set(seen))


def test_chain_is_empty_when_the_declared_tool_is_missing(tmp_path, monkeypatch):
    """A poetry repo installed with pip installs the wrong packages, so we still
    refuse rather than 'fall back' to a different answer."""
    write(tmp_path, "pyproject.toml", "[tool.poetry]\nname = 'x'\n")
    write(tmp_path, "poetry.lock", "")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    plans, note = python_install_plans(str(tmp_path), "/py")
    assert plans == []
    assert "poetry" in note


def test_javascript_chain_relaxes_the_frozen_lockfile(tmp_path, monkeypatch):
    write(tmp_path, "package.json", "{}")
    write(tmp_path, "pnpm-lock.yaml", "")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    plans, _ = javascript_install_plans(str(tmp_path))
    assert plans[0].commands == [["pnpm", "install", "--frozen-lockfile"]]
    assert plans[1].commands == [["pnpm", "install", "--no-frozen-lockfile"]]


def test_detect_install_chain_dispatches_on_language(uv_repo):
    plans, _ = detect_install_chain(uv_repo, "python", "/py")
    assert plans and plans[0].detector == "python:uv"
    assert detect_install_chain(uv_repo, "ruby", "/py") == (
        [],
        "no dependency detection is implemented for the 'ruby' runner",
    )


def test_run_plans_uses_the_fallback_when_the_first_fails(tmp_path):
    plans = [
        InstallPlan("first", "e1", [["/bin/false"]]),
        InstallPlan("second", "e2", [["/bin/true"]]),
    ]
    rep = run_plans(str(tmp_path), plans, timeout=60)
    assert rep.status == "ok"
    assert [a["detector"] for a in rep.attempts] == ["first", "second"]
    assert [a["status"] for a in rep.attempts] == ["failed", "ok"]
    assert rep.detector == "second"
    assert any("fallback" in n for n in rep.notes)


def test_run_plans_reports_both_attempts_when_the_fallback_also_fails(tmp_path):
    plans = [
        InstallPlan("first", "e1", [["/bin/false"]]),
        InstallPlan("second", "e2", [["/bin/false"]]),
    ]
    rep = run_plans(str(tmp_path), plans, timeout=60)
    assert rep.failed
    assert len(rep.attempts) == 2
    assert all(a["status"] == "failed" for a in rep.attempts)
    # ...and the precondition run is what judges: the failure reason names both.
    reason = failure_reason(rep)
    assert "first" in reason and "second" in reason
    assert "installers tried, in order" in reason
    # the report payload keeps them too
    assert len(rep.to_dict()["attempts"]) == 2


# --------------------------------------------------------------------------
# Defect 4: bound by collected test count
# --------------------------------------------------------------------------


class FakeRunner(Runner):
    id = "fake"
    language = "python"

    def __init__(self, repo, collected=None):
        super().__init__(repo, "fake")
        self._collected = collected
        self.executions = []
        self.builds_before_execute = []

    def build_command(self, targets, report_path):
        return ["fake", *targets]

    def parse(self, report_path, exit_code, stdout, stderr):
        return TestRunResult(command=[], outcome=Outcome.PASS, passed=1, total=1)

    def collect(self, targets, timeout, extra=None):
        return self._collected


def test_collection_cap_refuses_a_widened_selection(tmp_path):
    os.makedirs(str(tmp_path / "test"))
    runner = FakeRunner(str(tmp_path), collected=[f"t{i}::x" for i in range(6600)])
    entries = [SelectionEntry("f.yml", ["test"], "directory-fallback", "")]
    reason = _collection_cap(
        str(tmp_path),
        runner,
        VerifyOptions(repo=str(tmp_path), max_collected=500),
        Report(Verdict.INCONCLUSIVE, ""),
        ["test"],
        entries,
        None,
    )
    assert "6600" in reason and "500" in reason
    assert "could not map f.yml" in reason


def test_a_bare_directory_target_counts_as_widening(tmp_path):
    """Even without a directory-fallback entry, a bare directory is the suite."""
    os.makedirs(str(tmp_path / "tests"))
    runner = FakeRunner(str(tmp_path), collected=[f"t{i}::x" for i in range(900)])
    reason = _collection_cap(
        str(tmp_path),
        runner,
        VerifyOptions(repo=str(tmp_path), max_collected=500),
        Report(Verdict.INCONCLUSIVE, ""),
        ["tests"],
        [SelectionEntry("tests/a_test.py", ["tests"], "direct", "")],
        None,
    )
    assert "bare directory target(s) tests" in reason


def test_collection_cap_allows_a_small_widened_selection(tmp_path):
    os.makedirs(str(tmp_path / "tests"))
    runner = FakeRunner(str(tmp_path), collected=[f"t{i}::x" for i in range(10)])
    assert (
        _collection_cap(
            str(tmp_path),
            runner,
            VerifyOptions(repo=str(tmp_path), max_collected=500),
            Report(Verdict.INCONCLUSIVE, ""),
            ["tests"],
            [SelectionEntry("f.yml", ["tests"], "directory-fallback", "")],
            None,
        )
        == ""
    )


def test_collection_cap_is_a_no_op_for_precise_node_ids(tmp_path):
    runner = FakeRunner(str(tmp_path), collected=None)
    assert (
        _collection_cap(
            str(tmp_path),
            runner,
            VerifyOptions(repo=str(tmp_path)),
            Report(Verdict.INCONCLUSIVE, ""),
            ["a_test.py::test_one"],
            [SelectionEntry("a_test.py", ["a_test.py::test_one"], "direct", "", "proved")],
            None,
        )
        == ""
    )


def test_uncountable_runner_says_so_rather_than_guessing(tmp_path):
    os.makedirs(str(tmp_path / "tests"))
    report = Report(Verdict.INCONCLUSIVE, "")
    runner = FakeRunner(str(tmp_path), collected=None)
    assert (
        _collection_cap(
            str(tmp_path),
            runner,
            VerifyOptions(repo=str(tmp_path)),
            report,
            ["tests"],
            [SelectionEntry("f.yml", ["tests"], "directory-fallback", "")],
            None,
        )
        == ""
    )
    assert any("cap could not be applied" in w for w in report.warnings)


# --------------------------------------------------------------------------
# Defect 2 + 5: workspaces and build artefacts
# --------------------------------------------------------------------------


@pytest.fixture
def monorepo(tmp_path):
    write(tmp_path, "package.json", '{"name":"root","scripts":{"build:all":"pnpm -r build"}}')
    write(tmp_path, "pnpm-workspace.yaml", "packages:\n    - packages/*\n")
    write(
        tmp_path,
        "packages/server/package.json",
        '{"name":"@x/server","main":"./dist/index.cjs","scripts":{"test":"vitest run"}}',
    )
    write(
        tmp_path,
        "packages/core/package.json",
        '{"name":"@x/core","exports":{".":{"import":"./dist/index.mjs"}}}',
    )
    write(
        tmp_path,
        "packages/server/test/server.test.ts",
        "import { Thing } from '@x/core';\nimport { S } from '../src/server';\n",
    )
    return str(tmp_path)


def test_pnpm_workspace_yaml_is_detected_without_a_yaml_parser(monorepo):
    assert workspace_globs(monorepo) == ["packages/*"]


def test_npm_style_workspaces_are_detected(tmp_path):
    write(tmp_path, "package.json", '{"workspaces":{"packages":["libs/*"]}}')
    assert workspace_globs(str(tmp_path)) == ["libs/*"]


def test_owning_package_is_the_nearest_ancestor(monorepo):
    assert owning_package(monorepo, "packages/server/test/server.test.ts") == "packages/server"
    assert owning_package(monorepo, "scripts/tool.ts") == ""


def test_workspace_package_dirs_maps_names(monorepo):
    assert workspace_package_dirs(monorepo) == {
        "@x/server": "packages/server",
        "@x/core": "packages/core",
    }


def test_build_step_prefers_build_all(monorepo, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    step = detect_build_step(monorepo)
    assert step.argv == ["pnpm", "run", "build:all"]
    assert "build:all" in step.reason


def test_entry_points_are_built():
    assert entry_points_are_built({"main": "./dist/index.cjs"})
    assert entry_points_are_built({"exports": {".": {"import": "./lib/x.mjs"}}})
    assert not entry_points_are_built({"main": "./src/index.ts"})


def test_focus_moves_the_runner_into_the_owning_package(monorepo):
    from coretexa_verify.runners.javascript import VitestRunner

    runner = VitestRunner(monorepo, "test")
    got = runner.focus(["packages/server/test/server.test.ts"])
    assert got is not None
    targets, why = got
    assert targets == ["test/server.test.ts"]
    assert runner.cwd == os.path.join(monorepo, "packages/server")
    assert "packages/server" in why


def test_focus_declines_when_targets_span_packages(monorepo):
    from coretexa_verify.runners.javascript import VitestRunner

    write(monorepo, "packages/core/test/core.test.ts", "")
    runner = VitestRunner(monorepo, "test")
    assert (
        runner.focus(
            ["packages/server/test/server.test.ts", "packages/core/test/core.test.ts"]
        )
        is None
    )
    assert runner.cwd == monorepo


def test_artifact_risk_sees_a_sibling_package_resolved_to_dist(monorepo):
    risk = javascript_artifact_risk(
        monorepo,
        ["packages/server/test/server.test.ts"],
        ["packages/core/src/index.ts"],
    )
    assert "@x/core" in risk and "built output" in risk


def test_artifact_risk_is_silent_when_the_test_reads_source(monorepo):
    assert (
        javascript_artifact_risk(
            monorepo,
            ["packages/server/test/server.test.ts"],
            ["packages/server/src/server.ts"],
        )
        == ""
    )


def test_python_artifact_risk_flags_compiled_sources(tmp_path):
    from coretexa_verify.runners.python import PytestRunner

    runner = PytestRunner(str(tmp_path), "r", ["python", "-m", "pytest"])
    assert "compiled source" in runner.artifact_risk([], ["src/_speedups.pyx"])
    assert runner.artifact_risk([], ["src/mod.py"]) == ""


def test_build_is_re_run_before_every_test_run(tmp_path):
    """Defect 5's minimum fix: the build runs again inside every mutation."""
    runner = FakeRunner(str(tmp_path))
    runner.build_step = BuildStep(argv=["/bin/true"], reason="test", cwd=str(tmp_path))
    for tag in ("head", "reverted", "hunk0"):
        runner.execute([], 30, str(tmp_path), tag)
    assert runner.build_info.runs == 3
    assert runner.build_info.failures == 0
    assert runner.build_info.status == "ok"


def test_build_failures_are_counted_not_swallowed(tmp_path):
    runner = FakeRunner(str(tmp_path))
    runner.build_step = BuildStep(argv=["/bin/false"], reason="test", cwd=str(tmp_path))
    runner.execute([], 30, str(tmp_path), "head")
    assert runner.build_info.status == "failed"
    assert runner.build_info.failures == 1


def test_no_gate_is_refused_when_build_output_could_mask_the_revert(tmp_path):
    report = Report(Verdict.NO_GATE, "all tests still pass")
    report.build_artifact_risk = "the test imports @x/core, which resolves to dist/"
    runner = FakeRunner(str(tmp_path))
    out = _enforce_soundness(
        str(tmp_path), VerifyOptions(repo=str(tmp_path)), report, "base",
        runner, [], [], str(tmp_path), None,
    )
    assert out.verdict is Verdict.INCONCLUSIVE
    assert "stale build" in out.headline


def test_gate_holds_is_never_second_guessed(tmp_path):
    """The tests demonstrably reacted; that *is* the proof of the mapping."""
    report = Report(Verdict.GATE_HOLDS, "tests fail without the fix")
    report.build_artifact_risk = "risky"
    report.selection = [SelectionEntry("f.yml", ["t.py"], "fixture-map", "")]
    runner = FakeRunner(str(tmp_path))
    out = _enforce_soundness(
        str(tmp_path), VerifyOptions(repo=str(tmp_path)), report, "base",
        runner, [], [], str(tmp_path), None,
    )
    assert out.verdict is Verdict.GATE_HOLDS


# --------------------------------------------------------------------------
# smaller items
# --------------------------------------------------------------------------


def test_runs_differ_notices_counts_and_names():
    a = TestRunResult(command=[], outcome=Outcome.PASS, passed=11, total=11)
    assert not _runs_differ(a, TestRunResult(command=[], outcome=Outcome.PASS, passed=11, total=11))
    assert _runs_differ(a, TestRunResult(command=[], outcome=Outcome.PASS, passed=10, total=10))
    assert _runs_differ(
        a, TestRunResult(command=[], outcome=Outcome.ASSERT_FAIL, passed=11, failed=1)
    )
    assert _runs_differ(a, None)


def test_deletion_only_pr_keeps_the_verdict_but_softens_the_headline():
    report = Report(Verdict.NO_GATE, "All 4 of the PR's selected test(s) still pass.")
    report.changed_files = [
        ChangedFile("a.py", "D", Kind.SOURCE, ""),
        ChangedFile("b.py", "D", Kind.SOURCE, ""),
        ChangedFile("t_test.py", "M", Kind.TEST, ""),
    ]
    out = _soften_deletion_only(report)
    assert out.verdict is Verdict.NO_GATE
    assert out.headline.startswith("this PR only removes code; NO_GATE is expected")
    assert "still pass" in out.headline


def test_a_pr_that_adds_code_is_not_softened():
    report = Report(Verdict.NO_GATE, "headline")
    report.changed_files = [
        ChangedFile("a.py", "D", Kind.SOURCE, ""),
        ChangedFile("b.py", "M", Kind.SOURCE, ""),
    ]
    assert _soften_deletion_only(report).headline == "headline"


def test_running_as_root_is_warned_about(tmp_path, monkeypatch):
    import subprocess

    from coretexa_verify.verify import verify

    root = str(tmp_path / "r")
    os.makedirs(root)
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@e.com"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    write(root, "mod.py", "X = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True, capture_output=True)

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    report = verify(VerifyOptions(repo=root, base="main", head="HEAD", install_deps=False))
    assert any("uid 0" in w for w in report.warnings)

    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    report = verify(VerifyOptions(repo=root, base="main", head="HEAD", install_deps=False))
    assert not any("uid 0" in w for w in report.warnings)


# --------------------------------------------------------------------------
# D4: a runner usage error is not a gate
# --------------------------------------------------------------------------


def hunk(outcome, label="mod.py hunk 1"):
    from coretexa_verify.models import HunkResult

    return HunkResult(
        path="mod.py", index=1, header="@@ -1 +1 @@", label=label,
        outcome=outcome,
        gated=outcome in (Outcome.ASSERT_FAIL, Outcome.BUILD_ERROR),
        summary="s",
    )


def test_runner_error_hunk_is_unknown_not_gated():
    h = hunk(Outcome.RUNNER_ERROR)
    assert h.status == "unknown"
    assert not h.evaluable
    assert not h.gated


def test_timeout_and_empty_collection_are_also_unknown():
    assert hunk(Outcome.TIMEOUT).status == "unknown"
    assert hunk(Outcome.NO_TESTS_RUN).status == "unknown"


def test_assert_and_build_failures_are_still_gated():
    assert hunk(Outcome.ASSERT_FAIL).status == "gated"
    assert hunk(Outcome.BUILD_ERROR).status == "gated"
    assert hunk(Outcome.PASS).status == "ungated"


def test_gate_holds_claim_excludes_unevaluated_hunks():
    """pytest exit 4 next to a real detection must not inflate the claim."""
    from coretexa_verify.verify import _decide

    report = Report(Verdict.INCONCLUSIVE, "")
    report.head_run = TestRunResult(command=[], outcome=Outcome.PASS, passed=3, total=3)
    report.hunk_results = [
        hunk(Outcome.ASSERT_FAIL, "mod.py hunk 1"),
        hunk(Outcome.RUNNER_ERROR, "mod.py hunk 2"),
    ]
    out = _decide(report)
    assert out.verdict is Verdict.GATE_HOLDS
    assert "Every one of the 1 evaluated behavioural change(s)" in out.headline
    assert "1 further change(s) could not be evaluated" in out.headline
    assert "not counted as detected" in out.headline


def test_all_hunks_unevaluated_stays_inconclusive():
    from coretexa_verify.verify import _decide

    report = Report(Verdict.INCONCLUSIVE, "")
    report.head_run = TestRunResult(command=[], outcome=Outcome.PASS, passed=3, total=3)
    report.hunk_results = [hunk(Outcome.RUNNER_ERROR), hunk(Outcome.TIMEOUT)]
    assert _decide(report).verdict is Verdict.INCONCLUSIVE


def test_no_gate_counts_only_evaluated_hunks():
    from coretexa_verify.verify import _decide

    report = Report(Verdict.INCONCLUSIVE, "")
    report.head_run = TestRunResult(command=[], outcome=Outcome.PASS, passed=3, total=3)
    report.hunk_results = [
        hunk(Outcome.PASS, "mod.py hunk 1"),
        hunk(Outcome.ASSERT_FAIL, "mod.py hunk 2"),
        hunk(Outcome.RUNNER_ERROR, "mod.py hunk 3"),
    ]
    out = _decide(report)
    assert out.verdict is Verdict.NO_GATE
    assert "1 of 2 evaluated behavioural change(s)" in out.headline


def test_reports_render_unknown_hunks_distinctly():
    from coretexa_verify.report import render_markdown, render_text, to_json

    report = Report(Verdict.GATE_HOLDS, "h")
    report.head_run = TestRunResult(command=[], outcome=Outcome.PASS, passed=1, total=1)
    report.hunk_results = [hunk(Outcome.ASSERT_FAIL, "a"), hunk(Outcome.RUNNER_ERROR, "b")]
    text = render_text(report)
    assert "UNKNOWN  " in text
    md = render_markdown(report)
    assert "not evaluated" in md
    assert "1 not evaluable" in md
    assert '"status": "unknown"' in to_json(report)


# --------------------------------------------------------------------------
# D1: which interpreter are we actually about to install into
# --------------------------------------------------------------------------


def test_fallback_interpreter_is_named_and_warned_about(tmp_path):
    import sys

    from coretexa_verify.runners.base import DetectionContext
    from coretexa_verify.runners.python import detect_python

    write(tmp_path, "pyproject.toml", '[project]\nname = "x"\n')
    runner = detect_python(DetectionContext(repo=str(tmp_path)))
    assert runner.launcher == [sys.executable, "-m", "pytest"]
    assert sys.executable in runner.reason, "the printed command must be the real one"
    assert "`python -m pytest`" not in runner.reason
    assert len(runner.setup_warnings) == 1
    warning = runner.setup_warnings[0]
    assert "no repository-local environment was found" in warning
    assert sys.executable in warning
    assert ".venv" in warning and "--no-install-deps" in warning


def test_a_repo_local_venv_is_preferred_and_warns_about_nothing(tmp_path):
    from coretexa_verify.runners.base import DetectionContext
    from coretexa_verify.runners.python import detect_python

    write(tmp_path, "pyproject.toml", '[project]\nname = "x"\n')
    write(tmp_path, ".venv/bin/python", "#!/bin/sh\n")
    runner = detect_python(DetectionContext(repo=str(tmp_path)))
    assert runner.launcher == [os.path.join(str(tmp_path), ".venv", "bin", "python"), "-m", "pytest"]
    assert runner.setup_warnings == []


def test_setup_warnings_reach_the_report(tmp_path):
    """The warning is useless if it never leaves the runner."""
    import subprocess

    from coretexa_verify.verify import verify

    root = str(tmp_path / "r")
    os.makedirs(root)
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@e.com"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    write(root, "pyproject.toml", '[project]\nname = "x"\n')
    write(root, "src.py", "X = 1\n")
    write(root, "tests/a_test.py", "def test_a():\n    assert True\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True, capture_output=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()
    write(root, "src.py", "X = 2\n")
    write(root, "tests/a_test.py", "def test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "head"], cwd=root, check=True, capture_output=True)

    report = verify(VerifyOptions(repo=root, base=base, head="HEAD", install_deps=False))
    assert any("no repository-local environment was found" in w for w in report.warnings)


def test_a_js_runner_exposes_the_build_step_through_the_runner_hook(monorepo, monkeypatch):
    """verify.py asks the *runner* for a build step, not a hardcoded language.

    JavaScript is the only registered language with a genuinely separate build:
    `dist/` outlives the source it came from, so the step must be re-run inside
    every mutation. Go, Rust and the JVM all recompile as part of running their
    tests and say so by returning None. Keeping that as one polymorphic call
    means "no build step" is always a claim some runner made about its own
    toolchain.
    """
    from coretexa_verify.runners.javascript import VitestRunner

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = VitestRunner(monorepo, reason="test")
    step = runner.detect_build_step(900)
    assert step is not None
    assert step.argv == ["pnpm", "run", "build:all"]


def test_prepare_build_installs_whatever_the_runner_returns(monorepo, monkeypatch):
    from coretexa_verify.models import Report, Verdict
    from coretexa_verify.runners.javascript import VitestRunner
    from coretexa_verify.verify import VerifyOptions, _prepare_build

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    report = Report(verdict=Verdict.INCONCLUSIVE, headline="")
    runner = VitestRunner(monorepo, reason="test")
    _prepare_build(monorepo, VerifyOptions(repo=monorepo), report, runner)
    assert runner.build_step is not None
    assert report.build is not None
    assert report.build.command == ["pnpm", "run", "build:all"]


def test_prepare_build_records_nothing_for_a_runner_that_compiles_as_it_tests(tmp_path):
    from coretexa_verify.models import Report, Verdict
    from coretexa_verify.runners.golang import GoTestRunner
    from coretexa_verify.verify import VerifyOptions, _prepare_build

    report = Report(verdict=Verdict.INCONCLUSIVE, headline="")
    runner = GoTestRunner(str(tmp_path), reason="test")
    _prepare_build(str(tmp_path), VerifyOptions(repo=str(tmp_path)), report, runner)
    assert runner.build_step is None and report.build is None
