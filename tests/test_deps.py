"""Dependency auto-install: detection order, controls, and the artefact policy.

The end-to-end tests here build a real (tiny) git repository with a real pytest
suite and run :func:`coretexa_verify.verify.verify` against it, so the install
step is exercised in its real position in the sequence rather than in isolation.
No test in this file touches the network: the "install" is always an explicit
``--install-command`` that runs a local script, which is also how we get
deterministic control over the artefacts it generates.
"""

import json
import os
import shutil
import subprocess
import sys
import textwrap

import pytest

from coretexa_verify import deps
from coretexa_verify.deps import (
    DEV_REQUIREMENTS_FILES,
    TEST_EXTRA_NAMES,
    detect_install,
    detect_javascript_install,
    detect_python_install,
    parse_override,
)
from coretexa_verify.gitops import TreeState, is_clean, untracked_paths
from coretexa_verify.models import Verdict
from coretexa_verify.report import render_markdown, render_text, to_json
from coretexa_verify.verify import VerifyOptions, verify

PY = "/usr/bin/python3"


def write(root, rel, content=""):
    path = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(path) or str(root), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def plan_for(root, python_exe=PY):
    plan, note = detect_python_install(str(root), python_exe)
    return plan, note


def cmd(plan):
    return plan.display[0]


# ==========================================================================
# detection: priority order
# ==========================================================================


def test_uv_lock_wins_over_everything_else(tmp_path, monkeypatch):
    write(tmp_path, "uv.lock", "version = 1\n")
    write(tmp_path, "poetry.lock", "")
    write(tmp_path, "pyproject.toml", "[project]\nname='x'\n[project.optional-dependencies]\ntest=['pytest']\n")
    write(tmp_path, "requirements-dev.txt", "pytest\n")
    monkeypatch.setattr(deps.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    plan, _ = plan_for(tmp_path)
    assert plan.detector == "python:uv"
    assert cmd(plan) == "uv sync --frozen"
    assert "uv.lock" in plan.evidence


def test_uv_lock_without_uv_on_path_declines_rather_than_guessing(tmp_path, monkeypatch):
    write(tmp_path, "uv.lock", "version = 1\n")
    write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    monkeypatch.setattr(deps.shutil, "which", lambda tool: None)
    plan, note = plan_for(tmp_path)
    assert plan is None
    assert "uv.lock" in note and "not on PATH" in note


def test_poetry_beats_the_pyproject_extra(tmp_path, monkeypatch):
    write(tmp_path, "poetry.lock", "")
    write(tmp_path, "pyproject.toml", "[project]\nname='x'\n[project.optional-dependencies]\ntest=['pytest']\n")
    monkeypatch.setattr(deps.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    plan, _ = plan_for(tmp_path)
    assert plan.detector == "python:poetry"
    assert cmd(plan) == "poetry install"


def test_tool_poetry_table_alone_is_enough(tmp_path, monkeypatch):
    write(tmp_path, "pyproject.toml", "[tool.poetry]\nname='x'\n[tool.poetry.dependencies]\npython='^3.9'\n")
    monkeypatch.setattr(deps.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    plan, _ = plan_for(tmp_path)
    assert plan.detector == "python:poetry"
    assert "[tool.poetry]" in plan.evidence


def test_poetry_project_without_poetry_declines_instead_of_falling_back_to_pip(tmp_path, monkeypatch):
    write(tmp_path, "pyproject.toml", "[tool.poetry]\nname='x'\n")
    monkeypatch.setattr(deps.shutil, "which", lambda tool: None)
    plan, note = plan_for(tmp_path)
    assert plan is None
    assert "poetry" in note and "wrong thing" in note


def test_pyproject_extra_beats_requirements_files(tmp_path):
    write(tmp_path, "pyproject.toml", "[project]\nname='x'\n[project.optional-dependencies]\ntests=['pytest']\n")
    write(tmp_path, "requirements-dev.txt", "pytest\n")
    plan, _ = plan_for(tmp_path)
    assert plan.detector == "python:pyproject-extra"
    assert cmd(plan).endswith("-e '.[tests]'")


@pytest.mark.parametrize("extra", TEST_EXTRA_NAMES)
def test_every_documented_extra_name_is_matched(tmp_path, extra):
    write(tmp_path, "pyproject.toml", f"[project]\nname='x'\n[project.optional-dependencies]\n{extra}=['pytest']\n")
    plan, _ = plan_for(tmp_path)
    assert plan.detector == "python:pyproject-extra"
    assert f"-e '.[{extra}]'" in cmd(plan)


def test_extra_names_are_tried_in_declared_priority_order(tmp_path):
    # Both 'test' and 'dev' are declared; 'test' comes first in the table.
    write(
        tmp_path,
        "pyproject.toml",
        "[project]\nname='x'\n[project.optional-dependencies]\ndev=['a']\ntest=['b']\n",
    )
    plan, _ = plan_for(tmp_path)
    assert "-e '.[test]'" in cmd(plan)
    assert TEST_EXTRA_NAMES.index("test") < TEST_EXTRA_NAMES.index("dev")


def test_an_unrecognised_extra_is_not_silently_used(tmp_path):
    # sqlfluff's real shape: the only extra is `testutils`, which is a plugin
    # helper, not the dev environment. We must fall through, not guess.
    write(
        tmp_path,
        "pyproject.toml",
        "[project]\nname='x'\n[project.optional-dependencies]\ntestutils=['pytest']\nrs=['x']\n",
    )
    write(tmp_path, "requirements_dev.txt", "pytest\nhypothesis\n")
    plan, _ = plan_for(tmp_path)
    assert plan.detector == "python:requirements-dev"
    assert "requirements_dev.txt" in cmd(plan)


@pytest.mark.parametrize("rel", DEV_REQUIREMENTS_FILES)
def test_every_documented_dev_requirements_file_is_matched(tmp_path, rel):
    write(tmp_path, rel, "pytest\n")
    plan, _ = plan_for(tmp_path)
    assert plan.detector == "python:requirements-dev"
    assert f"-r {rel}" in cmd(plan)


def test_dev_requirements_files_are_tried_in_declared_order(tmp_path):
    write(tmp_path, "requirements-test.txt", "pytest\n")
    write(tmp_path, "requirements-dev.txt", "pytest\n")
    plan, _ = plan_for(tmp_path)
    assert "-r requirements-dev.txt" in cmd(plan)
    assert "-r requirements-test.txt" not in cmd(plan)


def test_dev_requirements_also_installs_the_project_and_its_base_requirements(tmp_path):
    write(tmp_path, "setup.py", "from setuptools import setup; setup()")
    write(tmp_path, "requirements.txt", "requests\n")
    write(tmp_path, "requirements-dev.txt", "pytest\n")
    plan, _ = plan_for(tmp_path)
    line = cmd(plan)
    assert "-e ." in line
    assert "-r requirements.txt" in line
    assert "-r requirements-dev.txt" in line
    assert "editable" in plan.evidence


def test_plain_requirements_txt_without_a_package_does_not_add_editable(tmp_path):
    write(tmp_path, "requirements.txt", "pytest\n")
    plan, _ = plan_for(tmp_path)
    assert plan.detector == "python:requirements"
    assert "-e ." not in cmd(plan)
    assert "-r requirements.txt" in cmd(plan)


def test_installable_project_with_nothing_else_gets_an_editable_install(tmp_path):
    write(tmp_path, "setup.py", "from setuptools import setup; setup()")
    plan, _ = plan_for(tmp_path)
    assert plan.detector == "python:editable"
    assert cmd(plan).endswith("-e .")


def test_bare_setup_cfg_without_metadata_is_not_a_distribution(tmp_path):
    write(tmp_path, "setup.cfg", "[flake8]\nmax-line-length = 100\n")
    plan, note = plan_for(tmp_path)
    assert plan is None
    assert "nothing to install" in note


def test_setup_cfg_with_metadata_is_a_distribution(tmp_path):
    write(tmp_path, "setup.cfg", "[metadata]\nname = x\n")
    plan, _ = plan_for(tmp_path)
    assert plan.detector == "python:editable"


def test_nothing_detected_is_not_an_error_but_is_explained(tmp_path):
    write(tmp_path, "main.go", "package main\n")
    plan, note = plan_for(tmp_path)
    assert plan is None
    assert note and "nothing to install" in note


def test_the_install_uses_the_runners_own_interpreter(tmp_path):
    write(tmp_path, "setup.py", "from setuptools import setup; setup()")
    plan, _ = plan_for(tmp_path, python_exe="/somewhere/.venv/bin/python")
    assert plan.commands[0][:3] == ["/somewhere/.venv/bin/python", "-m", "pip"]


# --- extras parsing without tomllib ---------------------------------------


def test_extras_scan_fallback_matches_the_tomllib_result():
    text = textwrap.dedent(
        """
        [project]
        name = "x"

        [project.optional-dependencies]
        test = ["pytest"]
        docs = ["sphinx"]

        [tool.black]
        test = ["not-an-extra"]
        """
    )
    assert deps.optional_dependency_extras(text) == ["test", "docs"]
    assert deps._scan_optional_dependency_extras(text) == ["test", "docs"]


# ==========================================================================
# detection: javascript
# ==========================================================================


@pytest.mark.parametrize(
    "lockfile,expected",
    [
        ("pnpm-lock.yaml", "pnpm install --frozen-lockfile"),
        ("yarn.lock", "yarn install --frozen-lockfile"),
        ("package-lock.json", "npm ci --no-audit --no-fund"),
    ],
)
def test_the_lockfile_decides_the_js_package_manager(tmp_path, monkeypatch, lockfile, expected):
    write(tmp_path, "package.json", "{}")
    write(tmp_path, lockfile, "")
    monkeypatch.setattr(deps.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    plan, _ = detect_javascript_install(str(tmp_path))
    assert cmd(plan) == expected
    assert lockfile in plan.evidence


def test_js_lockfile_precedence_is_pnpm_then_yarn_then_npm(tmp_path, monkeypatch):
    write(tmp_path, "package.json", "{}")
    for lock in ("pnpm-lock.yaml", "yarn.lock", "package-lock.json"):
        write(tmp_path, lock, "")
    monkeypatch.setattr(deps.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    plan, _ = detect_javascript_install(str(tmp_path))
    assert plan.detector == "js:pnpm"


def test_bare_package_json_falls_back_to_npm_install(tmp_path, monkeypatch):
    write(tmp_path, "package.json", "{}")
    monkeypatch.setattr(deps.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    plan, _ = detect_javascript_install(str(tmp_path))
    assert cmd(plan) == "npm install --no-audit --no-fund"
    assert "no lockfile" in plan.evidence


def test_a_lockfile_whose_manager_is_missing_declines(tmp_path, monkeypatch):
    write(tmp_path, "package.json", "{}")
    write(tmp_path, "pnpm-lock.yaml", "")
    monkeypatch.setattr(deps.shutil, "which", lambda tool: None)
    plan, note = detect_javascript_install(str(tmp_path))
    assert plan is None
    assert "pnpm-lock.yaml" in note and "lockfile the repo committed" in note


def test_detect_install_dispatches_on_the_runner_language(tmp_path, monkeypatch):
    write(tmp_path, "package.json", "{}")
    write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    monkeypatch.setattr(deps.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    py, _ = detect_install(str(tmp_path), "python", PY)
    js, _ = detect_install(str(tmp_path), "javascript", PY)
    assert py.language == "python" and js.language == "javascript"
    none, note = detect_install(str(tmp_path), "rust", PY)
    assert none is None and "rust" in note


# ==========================================================================
# the override escape hatch
# ==========================================================================


def test_a_simple_override_runs_as_argv_with_no_shell():
    assert parse_override("pip install -r reqs.txt") == [["pip", "install", "-r", "reqs.txt"]]


def test_an_override_with_shell_syntax_goes_through_sh():
    assert parse_override("make deps && pip install -e .") == [
        ["/bin/sh", "-c", "make deps && pip install -e ."]
    ]
    assert parse_override("echo x | tee log")[0][:2] == ["/bin/sh", "-c"]


def test_an_empty_override_is_no_override():
    assert parse_override("   ") == []


# ==========================================================================
# end to end, against a real repository
# ==========================================================================


@pytest.fixture
def project(tmp_path):
    """A real git repo whose test genuinely gates its source change.

    ``mod.f()`` returns 1 at base and 2 at head; the head test asserts 2. So
    reverting the source must produce ASSERT_FAIL -> GATE_HOLDS, and any
    deviation from that in these tests is caused by the install step.
    """
    root = str(tmp_path / "proj")
    os.makedirs(root)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    write(root, "pyproject.toml", "[project]\nname = 'proj'\nversion = '0.1'\n")
    write(root, "mod.py", "def f():\n    return 1\n")
    write(root, "tests/test_mod.py", "from mod import f\n\n\ndef test_f():\n    assert f() == 1\n")
    write(root, "_version.py", "VERSION = 'from-git'\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")

    write(root, "mod.py", "def f():\n    return 2\n")
    write(root, "tests/test_mod.py", "from mod import f\n\n\ndef test_f():\n    assert f() == 2\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "head")
    return root


def opts(root, **kw):
    kw.setdefault("install_deps", True)
    kw.setdefault("timeout", 300)
    kw.setdefault("install_timeout", 120)
    return VerifyOptions(repo=root, base="main~1", head="HEAD", **kw)


def script(tmp_path, name, body):
    """A local 'installer' living outside the repo, so it is not an artefact."""
    path = str(tmp_path / name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(textwrap.dedent(body))
    return f"{sys.executable} {path}"


def test_baseline_the_project_gates_without_any_install(project):
    report = verify(opts(project, install_deps=False))
    assert report.verdict is Verdict.GATE_HOLDS
    assert report.install.status == "disabled"
    assert report.install.source == "disabled"
    assert report.install.commands == []


def test_the_opt_out_really_runs_nothing(project, tmp_path):
    marker = str(tmp_path / "ran")
    report = verify(
        opts(
            project,
            install_deps=False,
            install_command=script(tmp_path, "inst.py", f"""
                open({marker!r}, 'w').write('x')
                """),
        )
    )
    assert report.verdict is Verdict.GATE_HOLDS
    assert not os.path.exists(marker), "install-deps=false must beat install-command"
    assert report.install.status == "disabled"


def test_the_override_replaces_detection_entirely(project, tmp_path):
    marker = str(tmp_path / "ran")
    report = verify(
        opts(project, install_command=script(tmp_path, "inst.py", f"""
            open({marker!r}, 'w').write('x')
            """)),
    )
    assert report.verdict is Verdict.GATE_HOLDS
    assert os.path.exists(marker)
    assert report.install.source == "override"
    assert report.install.detector == "override"
    assert report.install.status == "ok"
    assert "detection was skipped entirely" in report.install.evidence


def test_detection_result_and_evidence_reach_every_output_format(project):
    # No override: the real detector must fire on the real pyproject.toml.
    # `pip install -e .` is not run here - we only need the *plan* to surface -
    # so the command is swapped for a no-op with the same reporting path.
    report = verify(opts(project, install_command=f"{sys.executable} -c pass"))
    for text in (render_text(report), render_markdown(report), to_json(report)):
        assert "install" in text.lower()
    assert json.loads(to_json(report))["install"]["status"] == "ok"


BOOM = """
    import sys
    print('E: could not resolve dependency frobnicator==9.9', file=sys.stderr)
    sys.exit(3)
    """


def test_a_failing_install_that_breaks_the_tests_is_inconclusive_with_the_real_stderr(
    unimportable, tmp_path
):
    report = verify(opts(unimportable, install_command=script(tmp_path, "boom.py", BOOM)))
    assert report.verdict is Verdict.INCONCLUSIVE
    assert report.install.status == "failed"
    assert report.install.exit_code == 3
    # the installer's own stderr, not a paraphrase of it
    assert "frobnicator==9.9" in report.headline
    assert "dependency install failed (exit 3)" in report.headline


def test_a_failing_install_does_not_manufacture_an_inconclusive_when_the_tests_run(
    project, tmp_path
):
    """The cms shape: a build dependency that will not compile on this box.

    Those repos install their deps in their own workflow today and work fine.
    Turning them into INCONCLUSIVE because *our* extra install step failed
    would be a verdict we invented rather than one we measured - so we let the
    precondition run decide, and say loudly what happened.
    """
    report = verify(opts(project, install_command=script(tmp_path, "boom.py", BOOM)))
    assert report.verdict is Verdict.GATE_HOLDS, report.headline
    assert report.install.status == "failed"
    assert any("dependency install failed and the run continued" in w for w in report.warnings)
    assert "frobnicator==9.9" in report.install.stderr_tail


def test_an_install_timeout_is_surfaced_not_swallowed(project, tmp_path):
    report = verify(
        opts(
            project,
            install_timeout=1,
            install_command=script(tmp_path, "slow.py", """
                import time
                time.sleep(30)
                """),
        )
    )
    assert report.install.status == "timeout"
    # the project's own tests are fine, so a slow installer must not fabricate
    # a verdict - but it must be visible.
    assert report.verdict is Verdict.GATE_HOLDS, report.headline
    assert any("dependency install failed and the run continued" in w for w in report.warnings)


def test_an_install_timeout_that_breaks_the_tests_is_surfaced_in_the_headline(
    unimportable, tmp_path
):
    report = verify(
        opts(
            unimportable,
            install_timeout=1,
            install_command=script(tmp_path, "slow.py", """
                import time
                time.sleep(30)
                """),
        )
    )
    assert report.verdict is Verdict.INCONCLUSIVE
    assert report.install.status == "timeout"
    assert "install timeout" in report.headline
    assert "--install-timeout" in report.headline


@pytest.fixture
def unimportable(tmp_path):
    """A repo whose head tests cannot import, i.e. the missing-deps symptom."""
    root = str(tmp_path / "bare")
    os.makedirs(root)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    write(root, "conftest.py", "")
    write(root, "mod.py", "X = 1\n")
    write(root, "tests/test_mod.py", "def test_x():\n    assert True\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    write(root, "mod.py", "X = 2\n")
    write(root, "tests/test_mod.py", "import nonexistent_package_xyz\n\n\ndef test_x():\n    assert True\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "head")
    return root


def test_no_detection_is_not_an_error_and_is_noted_on_the_head_failure(unimportable):
    report = verify(opts(unimportable))
    assert report.install.status == "none"
    assert report.verdict is Verdict.INCONCLUSIVE
    assert "No dependency install was detected" in report.headline
    assert any("no dependency install was detected" in w for w in report.warnings)


# ==========================================================================
# THE ARTEFACT POLICY
# ==========================================================================


ARTEFACT_INSTALLER = """
    import os
    os.makedirs('proj.egg-info', exist_ok=True)
    open(os.path.join('proj.egg-info', 'PKG-INFO'), 'w').write('Name: proj\\n')
    open(os.path.join('proj.egg-info', 'SOURCES.txt'), 'w').write('mod.py\\n')
    os.makedirs('build/lib', exist_ok=True)
    open(os.path.join('build', 'lib', 'mod.py'), 'w').write('stale copy\\n')
    """


def test_install_artefacts_do_not_break_the_run_when_egg_info_is_not_gitignored(project, tmp_path):
    """The load-bearing case: the repo has *no* .gitignore at all.

    `pip install -e .` would leave `proj.egg-info/` sitting untracked in the
    working tree. That must not: refuse the run, be reverted, be deleted, be
    reported as our own leftover, or block the next run.
    """
    assert not os.path.exists(os.path.join(project, ".gitignore"))

    report = verify(opts(project, install_command=script(tmp_path, "inst.py", ARTEFACT_INSTALLER)))

    # 1. the experiment still reached a real verdict
    assert report.verdict is Verdict.GATE_HOLDS, report.headline
    assert report.install.status == "ok"

    # 2. the artefacts were attributed to the install, by snapshot not by name
    assert "proj.egg-info/" in report.install.artefacts
    assert "build/" in report.install.artefacts

    # 3. they were never reverted or deleted
    assert os.path.exists(os.path.join(project, "proj.egg-info", "PKG-INFO"))
    assert os.path.exists(os.path.join(project, "build", "lib", "mod.py"))

    # 4. they were never mistaken for our own failure to restore
    assert report.tree_restored is True
    assert not any("still dirty after restoration" in w for w in report.warnings)

    # 5. git agrees the artefacts are untracked-but-present, and every tracked
    #    file is back exactly as it was
    assert is_clean(project)
    assert set(untracked_paths(project)) >= {"proj.egg-info/", "build/"}

    # 6. and a second run, with the artefacts already lying around, is not
    #    spuriously refused
    again = verify(opts(project, install_command=script(tmp_path, "inst.py", ARTEFACT_INSTALLER)))
    assert again.verdict is Verdict.GATE_HOLDS, again.headline


def test_a_tracked_file_the_install_rewrites_is_excluded_from_the_restore_check(project, tmp_path):
    """setuptools_scm-style: the install regenerates a *committed* file.

    That dirties a tracked path after our cleanliness gate. It is not our
    change, so we must neither revert it nor count it as a restoration failure -
    but we must say out loud that it happened.
    """
    installer = script(tmp_path, "scm.py", """
        open('_version.py', 'w').write("VERSION = 'generated-by-install'\\n")
        """)
    report = verify(opts(project, install_command=installer))

    assert report.verdict is Verdict.GATE_HOLDS, report.headline
    assert report.install.dirtied_tracked == ["_version.py"]
    assert report.tree_restored is True
    assert not any("still dirty after restoration" in w for w in report.warnings)
    assert any("modified tracked file(s) _version.py" in w for w in report.warnings)
    # never reverted: the install's content is what survives
    with open(os.path.join(project, "_version.py")) as fh:
        assert fh.read().strip() == "VERSION = 'generated-by-install'"


def test_a_genuinely_dirty_tree_is_still_refused(project, tmp_path):
    """The gate we must not have weakened."""
    write(project, "mod.py", "def f():\n    return 99  # local edit\n")
    report = verify(opts(project, install_command=f"{sys.executable} -c pass"))
    assert report.verdict is Verdict.INCONCLUSIVE
    assert "uncommitted changes" in report.headline
    assert report.install is None, "we must not install into a tree we refused to touch"


def test_pre_existing_untracked_artefacts_are_not_attributed_to_the_install(project, tmp_path):
    os.makedirs(os.path.join(project, "proj.egg-info"), exist_ok=True)
    write(project, "proj.egg-info/PKG-INFO", "Name: proj\n")
    before = TreeState.capture(project)
    assert "proj.egg-info/" in before.untracked

    report = verify(opts(project, install_command=script(tmp_path, "inst.py", ARTEFACT_INSTALLER)))
    assert report.verdict is Verdict.GATE_HOLDS
    assert "proj.egg-info/" not in report.install.artefacts, "it was already there"
    assert "build/" in report.install.artefacts
    assert os.path.exists(os.path.join(project, "proj.egg-info", "PKG-INFO"))


def test_the_mutate_restore_cycle_is_measured_against_the_post_install_baseline(project, tmp_path):
    """Localisation runs many mutate/restore cycles; none may drift."""
    report = verify(
        opts(
            project,
            localize="always",
            install_command=script(tmp_path, "inst.py", ARTEFACT_INSTALLER),
        )
    )
    assert report.localized
    assert report.hunk_results
    assert report.tree_restored is True
    assert is_clean(project)
    assert os.path.exists(os.path.join(project, "proj.egg-info", "PKG-INFO"))


# --- bytecode caches are build artefacts too -------------------------------


POISON_PYC = """
    import importlib.util, marshal, os, sys

    src = 'mod.py'
    st = os.stat(src)
    # A .pyc that is *valid* for the current mod.py - the recorded source mtime
    # and size both match - but whose code says something else entirely. If any
    # run reads it, f() returns 777 instead of what mod.py says.
    code = compile("def f():\\n    return 777\\n", src, "exec")
    blob = (
        importlib.util.MAGIC_NUMBER
        + (0).to_bytes(4, 'little')
        + int(st.st_mtime).to_bytes(4, 'little')
        + (st.st_size & 0xFFFFFFFF).to_bytes(4, 'little')
        + marshal.dumps(code)
    )
    dest = importlib.util.cache_from_source(src)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    open(dest, 'wb').write(blob)
    """


def test_a_stale_bytecode_cache_cannot_answer_for_the_source(project, tmp_path):
    """A ``__pycache__`` left by an earlier run must not decide the verdict.

    Reverting a source file very often leaves its byte count unchanged (``1``
    for ``2``), and CPython validates a .pyc on source mtime + size alone. So a
    cache that survives the revert can hand the *head* implementation to the
    *base* run, and turn GATE_HOLDS into a confident, wrong NO_GATE. We make
    that impossible by pointing bytecode lookup at an empty scratch directory
    for every runner subprocess.
    """
    report = verify(opts(project, install_command=script(tmp_path, "poison.py", POISON_PYC)))

    cached = os.path.join(project, "__pycache__")
    assert os.path.exists(cached), "the fixture must really have planted a poisoned cache"

    # If the poisoned cache were honoured, f() would return 777, the head test
    # (assert f() == 2) would fail, and we would never get past the precondition.
    assert report.verdict is Verdict.GATE_HOLDS, report.headline
    assert report.head_run.outcome.value == "PASS"


def test_no_pycache_is_left_in_the_users_tree(project):
    verify(opts(project, install_deps=False))
    stray = [
        os.path.join(dirpath, d)
        for dirpath, dirnames, _ in os.walk(project)
        for d in dirnames
        if d == "__pycache__"
    ]
    assert stray == [], stray


def test_the_runner_env_isolates_bytecode_outside_the_repo(tmp_path):
    from coretexa_verify.runners.base import Runner

    r = Runner(str(tmp_path), "test")
    try:
        env = r.subprocess_env()
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert os.path.isdir(env["PYTHONPYCACHEPREFIX"])
        assert not env["PYTHONPYCACHEPREFIX"].startswith(str(tmp_path))
        prefix = env["PYTHONPYCACHEPREFIX"]
    finally:
        r.cleanup()
    assert not os.path.exists(prefix)
    r.cleanup()  # idempotent


def test_the_tool_itself_still_declares_no_runtime_dependencies():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as fh:
        assert "dependencies = []" in fh.read()
    # and deps.py imports nothing outside the standard library
    with open(os.path.join(root, "src", "coretexa_verify", "deps.py"), encoding="utf-8") as fh:
        body = fh.read()
    stdlib = getattr(sys, "stdlib_module_names", None)  # 3.10+
    for line in body.splitlines():
        if line.startswith(("import ", "from ")) and not line.startswith("from ."):
            module = line.split()[1].split(".")[0]
            if stdlib is not None:
                assert module in set(stdlib), module
            assert module in {
                "__future__", "os", "re", "shlex", "shutil", "time", "dataclasses",
            }, module


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_untracked_paths_collapses_directories(tmp_path):
    root = str(tmp_path / "r")
    os.makedirs(root)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@e.com")
    git(root, "config", "user.name", "t")
    write(root, "a.txt", "a")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "x")
    for i in range(5):
        write(root, f"node_modules/pkg{i}/index.js", "x")
    assert untracked_paths(root) == ["node_modules/"]
