"""No secret reaches code the pull request controls.

This tool runs a PR's own test suite, its own build step and its own dependency
installer. In the GitHub Action it may also hold a token so it can post a
comment. Handing ``os.environ`` to those subprocesses would hand the token, and
every other credential the job holds, to the code under examination — a PR
could exfiltrate it from a test.

These tests are the guarantee: the sanitised environment lacks the variables,
and a real subprocess launched through the runner's own ``execute`` path really
does come back without them. The comment path, which reads the token from *our*
process after the experiment is over, still finds it.
"""

import json
import os
import subprocess
import sys

import pytest

from coretexa_verify import action_main, deps
from coretexa_verify.gitops import (
    ALLOWLIST_ENV_VAR,
    SECRET_ENV_NAMES,
    is_secret_env_name,
    redacted_env_names,
    run,
    sanitized_environ,
)
from coretexa_verify.models import Outcome, Report, TestRunResult, Verdict
from coretexa_verify.report import render_markdown, render_text, to_json
from coretexa_verify.runners.base import Runner

#: A command that prints its whole environment as JSON. The only honest way to
#: ask "what did the child actually get?".
DUMP_ENV = [sys.executable, "-c", "import json,os;print(json.dumps(dict(os.environ)))"]

SECRETS = {
    "INPUT_GITHUB_TOKEN": "ghs_input",
    "GITHUB_TOKEN": "ghs_github",
    "GH_TOKEN": "ghs_gh",
    "ACTIONS_RUNTIME_TOKEN": "runtime",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "idtoken",
    "MY_SERVICE_TOKEN": "svc",
    "SOME_SECRET": "s3cret",
    "DB_PASSWORD": "hunter2",
    "STRIPE_API_KEY": "sk_live",
    "AWS_SECRET_ACCESS_KEY": "aws",
    "REGISTRY_CREDENTIAL": "cred",
    "npm_token": "npm-lowercase",
}


@pytest.fixture
def secrets_in_environ(monkeypatch):
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    monkeypatch.setenv("HARMLESS_SETTING", "keep-me")
    monkeypatch.delenv(ALLOWLIST_ENV_VAR, raising=False)
    return SECRETS


# --------------------------------------------------------------------------
# the name rule
# --------------------------------------------------------------------------


def test_every_named_secret_is_recognised():
    for name in SECRET_ENV_NAMES:
        assert is_secret_env_name(name), name


@pytest.mark.parametrize(
    "name",
    [
        "GITHUB_TOKEN",
        "INPUT_GITHUB_TOKEN",
        "GH_TOKEN",
        "ACTIONS_RUNTIME_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "MY_SERVICE_TOKEN",
        "SOME_SECRET",
        "DB_PASSWORD",
        "PGPASSWORD",
        "STRIPE_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "REGISTRY_CREDENTIAL",
        "DEPLOY_CREDENTIALS",
        "SIGNING_PRIVATE_KEY",
        "npm_token",
        "BASIC_AUTH",
    ],
)
def test_credential_shaped_names_are_secret(name):
    assert is_secret_env_name(name)


@pytest.mark.parametrize(
    "name",
    ["PATH", "HOME", "CI", "LANG", "GITHUB_WORKSPACE", "GITHUB_REPOSITORY", "TOKEN_URL",
     "SECRET_SANTA_MODE", "PYTHONPATH", "GOFLAGS", "npm_config_cache"],
)
def test_ordinary_names_survive(name):
    assert not is_secret_env_name(name)


def test_the_pattern_is_anchored_at_a_word_boundary():
    """``TOKEN_URL`` is a URL, not a token; ``MY_TOKEN`` is a token."""
    assert not is_secret_env_name("TOKEN_URL")
    assert is_secret_env_name("MY_TOKEN")
    assert not is_secret_env_name("TOKENISER")


# --------------------------------------------------------------------------
# the sanitised environment
# --------------------------------------------------------------------------


def test_sanitized_environ_lacks_every_secret(secrets_in_environ):
    env = sanitized_environ()
    for name in secrets_in_environ:
        assert name not in env, name
    assert env["HARMLESS_SETTING"] == "keep-me"
    assert "PATH" in env, "a test suite needs PATH; we strip credentials, not the environment"


def test_redacted_env_names_reports_what_was_dropped(secrets_in_environ):
    names = redacted_env_names()
    for name in secrets_in_environ:
        assert name in names
    assert "HARMLESS_SETTING" not in names


def test_the_allowlist_is_per_name_and_opt_in(secrets_in_environ, monkeypatch):
    monkeypatch.setenv(ALLOWLIST_ENV_VAR, "MY_SERVICE_TOKEN")
    env = sanitized_environ()
    assert env["MY_SERVICE_TOKEN"] == "svc", "explicitly allowed"
    assert "GITHUB_TOKEN" not in env, "the allowlist is per-name, not a master switch"


# --------------------------------------------------------------------------
# a real subprocess
# --------------------------------------------------------------------------


def _child_env(argv, **kwargs):
    res = run(argv, cwd=os.getcwd(), timeout=60, **kwargs)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_an_isolated_subprocess_never_sees_a_token(secrets_in_environ):
    env = _child_env(DUMP_ENV, isolate=True)
    for name in secrets_in_environ:
        assert name not in env, f"{name} leaked into a repository-controlled subprocess"
    assert env["HARMLESS_SETTING"] == "keep-me"


def test_git_is_not_isolated_because_it_is_ours(secrets_in_environ):
    """git may need the job's credentials to fetch a base ref. It is our code."""
    env = _child_env(DUMP_ENV)
    assert env["GITHUB_TOKEN"] == "ghs_github"


class DumpingRunner(Runner):
    """A runner whose "test command" prints its environment into the report."""

    id = "dump"
    language = "test"
    report_suffix = "json"

    def build_command(self, targets, report_path):
        self.report_path = report_path
        return [
            sys.executable,
            "-c",
            f"import json,os;open({report_path!r},'w').write(json.dumps(dict(os.environ)))",
        ]

    def parse(self, report_path, exit_code, stdout, stderr):
        with open(report_path) as fh:
            self.seen = json.load(fh)
        return TestRunResult(command=[], outcome=Outcome.PASS, passed=1, total=1)


def test_the_runner_execute_path_strips_secrets(secrets_in_environ, tmp_path):
    """The guarantee where it matters: Runner.execute, not a helper."""
    runner = DumpingRunner(str(tmp_path), "r")
    try:
        result = runner.execute([], 60, str(tmp_path), "head")
        assert result.outcome is Outcome.PASS
        for name in secrets_in_environ:
            assert name not in runner.seen, f"{name} reached the test run"
        assert runner.seen["CI"] == "1", "our own overrides still apply"
        assert "PATH" in runner.seen
    finally:
        runner.cleanup()


def test_the_build_step_strips_secrets(secrets_in_environ, tmp_path):
    from coretexa_verify.runners.base import BuildStep

    out = tmp_path / "buildenv.json"
    runner = DumpingRunner(str(tmp_path), "r")
    runner.build_step = BuildStep(
        argv=[
            sys.executable,
            "-c",
            f"import json,os;open({str(out)!r},'w').write(json.dumps(dict(os.environ)))",
        ],
        reason="test",
        cwd=str(tmp_path),
    )
    info = runner.run_build()
    assert info.status == "ok", info.note
    seen = json.loads(out.read_text())
    for name in secrets_in_environ:
        assert name not in seen, f"{name} reached the build step"


def test_the_dependency_installer_strips_secrets(secrets_in_environ, tmp_path):
    out = tmp_path / "installenv.json"
    plan = deps.InstallPlan(
        detector="test",
        evidence="test",
        commands=[
            [
                sys.executable,
                "-c",
                f"import json,os;open({str(out)!r},'w').write(json.dumps(dict(os.environ)))",
            ]
        ],
        language="python",
    )
    report = deps.run_plans(str(tmp_path), [plan], 60)
    assert report.status == "ok", report.summary()
    seen = json.loads(out.read_text())
    for name in secrets_in_environ:
        assert name not in seen, f"{name} reached the dependency installer"


def test_go_collection_would_be_isolated(monkeypatch, secrets_in_environ):
    """Enumeration compiles the package, so it runs repo code too."""
    from coretexa_verify.runners import golang

    seen = {}

    def fake_run(argv, cwd, timeout=120, env=None, isolate=False):
        seen["isolate"] = isolate
        from coretexa_verify.gitops import CommandResult

        return CommandResult(argv=argv, returncode=1, stdout="", stderr="")

    monkeypatch.setattr(golang, "run", fake_run)
    runner = golang.GoTestRunner(os.getcwd(), "r")
    runner.collect(["./pkg::TestA"], 30)
    assert seen.get("isolate") is True


# --------------------------------------------------------------------------
# the comment path still works
# --------------------------------------------------------------------------


def test_the_comment_path_still_reads_the_token(secrets_in_environ):
    """os.environ is never mutated, so action_main still finds its token."""
    assert action_main.env("INPUT_GITHUB_TOKEN") == "ghs_input"
    assert os.environ["INPUT_GITHUB_TOKEN"] == "ghs_input"


def test_upsert_comment_uses_the_token_it_is_given(monkeypatch, secrets_in_environ, tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 7}}))
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    calls = []

    def fake_api(url, token, method="GET", payload=None):
        calls.append((url, token, method))
        return [] if method == "GET" else {"id": 42}

    monkeypatch.setattr(action_main, "_api", fake_api)
    out = action_main.upsert_comment("body", action_main.env("INPUT_GITHUB_TOKEN"))
    assert out == "created comment 42"
    assert all(token == "ghs_input" for _, token, _ in calls)


# --------------------------------------------------------------------------
# the report says so
# --------------------------------------------------------------------------


def test_the_report_names_what_was_withheld():
    report = Report(verdict=Verdict.GATE_HOLDS, headline="h")
    report.redacted_env = ["GITHUB_TOKEN", "INPUT_GITHUB_TOKEN"]
    text = render_text(report)
    assert "secrets   : 2 environment variable(s) withheld" in text
    assert "GITHUB_TOKEN" in text
    md = render_markdown(report)
    assert "secrets withheld" in md
    assert json.loads(to_json(report))["redacted_env"] == [
        "GITHUB_TOKEN",
        "INPUT_GITHUB_TOKEN",
    ]


def test_no_secret_value_is_ever_printed(secrets_in_environ):
    """Names are useful and safe; values are neither."""
    report = Report(verdict=Verdict.GATE_HOLDS, headline="h")
    report.redacted_env = redacted_env_names()
    blob = render_text(report) + render_markdown(report) + to_json(report)
    for value in secrets_in_environ.values():
        assert value not in blob


def test_the_action_passes_no_secret_on_the_command_line():
    """A token in argv is visible in `ps`; it must travel by env only."""
    source = open("action.yml").read()
    assert "INPUT_GITHUB_TOKEN: ${{ inputs.github-token }}" in source
    assert "--github-token" not in source


def test_subprocess_isolation_is_the_default_nowhere_but_git():
    """Every runner/deps call site opts in explicitly; grep is the check."""
    import pathlib

    offenders = []
    for path in list(pathlib.Path("src/coretexa_verify/runners").glob("*.py")) + [
        pathlib.Path("src/coretexa_verify/deps.py")
    ]:
        text = path.read_text()
        for i, line in enumerate(text.splitlines()):
            stripped = line.strip()
            if stripped.startswith(("#", "*", '"')) or "def run" in stripped:
                continue
            if "= run(" in stripped or stripped.endswith("run("):
                block = "\n".join(text.splitlines()[i : i + 8])
                if "isolate=True" not in block:
                    offenders.append(f"{path}:{i + 1}")
    assert offenders == [], f"subprocess call sites missing isolate=True: {offenders}"
