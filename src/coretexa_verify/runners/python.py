"""pytest runner.

Results are read from pytest's JUnit XML rather than scraped from stdout,
because JUnit XML is the only place pytest reliably distinguishes a ``<failure>``
(an assertion that fired) from an ``<error>`` (the test could not be imported,
collected or set up). That distinction is exactly ``GATE_HOLDS`` vs
``GATE_HOLDS_BUILD``, so it is worth the structured parse.
"""

from __future__ import annotations

import os
import shutil
import sys

from ..models import Outcome, TestRunResult
from . import junit
from .base import DetectionContext, Runner

# pytest exit codes
EXIT_OK = 0
EXIT_TESTS_FAILED = 1
EXIT_INTERRUPTED = 2  # collection error, KeyboardInterrupt
EXIT_INTERNAL_ERROR = 3
EXIT_USAGE_ERROR = 4
EXIT_NO_TESTS_COLLECTED = 5

#: Sources that only reach the interpreter after a compile step.
COMPILED_SOURCE_EXTENSIONS = frozenset(
    {".pyx", ".pxd", ".pxi", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".rs", ".go"}
)

PYTEST_MARKERS = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "tox.ini",
    "pytest.ini",
    "conftest.py",
    "requirements.txt",
    "requirements-dev.txt",
)


class PytestRunner(Runner):
    id = "pytest"
    language = "python"
    test_file_extensions = (".py",)

    def __init__(self, repo: str, reason: str, launcher: list[str], extra_args=None):
        super().__init__(repo, reason, extra_args)
        self.launcher = launcher

    def build_command(self, targets: list[str], report_path: str) -> list[str]:
        return [
            *self.launcher,
            *targets,
            f"--junitxml={report_path}",
            "-p",
            "no:cacheprovider",
            "--tb=short",
            "-q",
            "--no-header",
            *self.extra_args,
        ]

    def default_test_dir(self) -> str | None:
        for candidate in ("tests", "test"):
            if os.path.isdir(os.path.join(self.repo, candidate)):
                return candidate
        return None

    def parse(self, report_path: str, exit_code: int, stdout: str, stderr: str) -> TestRunResult:
        return parse_pytest_report(report_path, exit_code, stdout, stderr)

    def collect(
        self, targets: list[str], timeout: int, extra: list[str] | None = None
    ) -> list[str] | None:
        from ..gitops import run

        argv = [*self.launcher, *targets, "--collect-only", "-q",
                "-p", "no:cacheprovider", "--no-header", *self.extra_args, *(extra or [])]
        res = run(argv, cwd=self.cwd, timeout=timeout, env=self.subprocess_env(), isolate=True)
        if res.timed_out or res.returncode not in (0, 5):
            return None
        return parse_collect_only(res.stdout)

    def artifact_risk(self, targets: list[str], source_paths: list[str]) -> str:
        """Compiled sources cannot be reverted by rewriting the source file."""
        compiled = sorted(
            p for p in source_paths
            if os.path.splitext(p)[1] in COMPILED_SOURCE_EXTENSIONS
        )
        if compiled:
            return (
                "the PR changes compiled source file(s) "
                + ", ".join(compiled[:4])
                + "; the tests import the already-built extension, which a source revert "
                "does not change"
            )
        return ""


def parse_pytest_report(
    report_path: str, exit_code: int, stdout: str = "", stderr: str = ""
) -> TestRunResult:
    """Turn a JUnit XML file + exit code into a :class:`TestRunResult`."""
    # The XML reading is shared with the Maven/Gradle runner - see
    # coretexa_verify.runners.junit. Only the exit-code interpretation below is
    # pytest's own.
    report = junit.read_reports([report_path])
    counts = report.as_dict()
    failing = report.failing
    erroring = report.erroring
    parsed_xml = report.parsed

    total = report.total

    if not parsed_xml:
        # pytest died before it could write a report: usage error, missing
        # plugin, import crash at conftest level. Never guess a pass.
        outcome = Outcome.RUNNER_ERROR
        note = f"pytest produced no JUnit report (exit code {exit_code})"
        if exit_code == EXIT_NO_TESTS_COLLECTED:
            outcome, note = Outcome.NO_TESTS_RUN, "pytest collected no tests"
        elif exit_code in (EXIT_INTERRUPTED, EXIT_INTERNAL_ERROR):
            outcome, note = Outcome.BUILD_ERROR, "pytest aborted during collection"
        return TestRunResult(
            command=[], outcome=outcome, exit_code=exit_code, total=0, note=note
        )

    if total == 0:
        return TestRunResult(
            command=[],
            outcome=Outcome.NO_TESTS_RUN,
            exit_code=exit_code,
            note="pytest collected no tests",
        )

    if counts["failed"]:
        outcome = Outcome.ASSERT_FAIL
    elif counts["errored"]:
        outcome = Outcome.BUILD_ERROR
    elif exit_code == EXIT_OK:
        outcome = Outcome.PASS
    elif exit_code == EXIT_NO_TESTS_COLLECTED:
        outcome = Outcome.NO_TESTS_RUN
    else:
        # Report says everything is fine but pytest disagrees (plugin error,
        # -W error at teardown, coverage threshold). Do not call that a pass.
        outcome = Outcome.RUNNER_ERROR

    note = ""
    if outcome is Outcome.RUNNER_ERROR:
        note = f"all collected tests passed but pytest exited {exit_code}"
    elif outcome is Outcome.BUILD_ERROR:
        note = "tests could not be collected/imported/set up (no assertion ran)"

    return TestRunResult(
        command=[],
        outcome=outcome,
        exit_code=exit_code,
        passed=counts["passed"],
        failed=counts["failed"],
        errored=counts["errored"],
        skipped=counts["skipped"],
        total=total,
        failing_ids=failing[:50],
        erroring_ids=erroring[:50],
        note=note,
    )


def parse_collect_only(stdout: str) -> list[str]:
    """Node ids from ``pytest --collect-only -q`` output.

    The tail of that output is a human summary ("123 tests collected in 1.2s"),
    so we keep only lines that look like a node id.
    """
    ids: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith(("=", "-", "<", "!")):
            continue
        if "::" not in line:
            continue
        if line.endswith((" tests collected", " test collected")) or " in " in line.split("::")[0]:
            continue
        ids.append(line)
    return ids


#: Kept as an alias so existing imports of the private helper keep working.
_case_id = junit.case_id


# --------------------------------------------------------------------------


def detect_python(ctx: DetectionContext, extra_args=None) -> PytestRunner | None:
    """Pick a pytest launcher, or return None if this is not a Python repo."""
    if not ctx.exists(*PYTEST_MARKERS):
        return None

    markers = [m for m in PYTEST_MARKERS if ctx.exists(m)]
    marker_note = ", ".join(markers[:3])

    # `uv run` gives us the project's locked environment without activating it.
    if ctx.exists("uv.lock") and shutil.which("uv"):
        return PytestRunner(
            ctx.repo,
            reason=f"found uv.lock (with {marker_note}) and `uv` on PATH -> `uv run pytest`",
            launcher=["uv", "run", "--frozen", "pytest"],
            extra_args=extra_args,
        )

    for venv_dir in (".venv", "venv"):
        venv_py = os.path.join(ctx.repo, venv_dir, "bin", "python")
        if os.path.exists(venv_py):
            return PytestRunner(
                ctx.repo,
                reason=f"found {venv_dir}/bin/python (with {marker_note}) -> `{venv_dir}/bin/python -m pytest`",
                launcher=[venv_py, "-m", "pytest"],
                extra_args=extra_args,
            )

    # Last resort: the interpreter coretexa-verify itself is running in. That is
    # a real decision with a real consequence - the repository's test
    # dependencies get installed into *our* environment - so it is named in the
    # reason string and warned about loudly rather than printed as the
    # comfortable-looking `python -m pytest`.
    runner = PytestRunner(
        ctx.repo,
        reason=(
            f"found {marker_note}, and the repository has no uv.lock, .venv or venv, so the "
            f"interpreter running coretexa-verify was used -> `{sys.executable} -m pytest`"
        ),
        launcher=[sys.executable, "-m", "pytest"],
        extra_args=extra_args,
    )
    runner.setup_warnings.append(
        f"no repository-local environment was found (.venv, venv or uv.lock), so the tests run "
        f"under {sys.executable} - the interpreter coretexa-verify itself is installed in - and "
        f"the dependency install writes the repository's test dependencies into that same "
        f"environment. Create a repo-local .venv (python -m venv .venv) to keep the target "
        f"repository's dependencies out of the tool's own environment, or pass --no-install-deps."
    )
    return runner
