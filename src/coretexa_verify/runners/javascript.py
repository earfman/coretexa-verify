"""jest / vitest / `npm test` runners.

jest and vitest both emit jest-shaped JSON, which gives us the suite-level
"this file blew up before any assertion ran" signal we need for
``GATE_HOLDS_BUILD``. The generic ``npm test`` fallback has no structured
output, so there we fall back to a *declared* heuristic and say so in the report
rather than pretending to the same confidence.
"""

from __future__ import annotations

import json
import os
import re
import shutil

from ..models import Outcome, TestRunResult
from .base import DetectionContext, Runner

#: Patterns that mean "the module graph did not load", i.e. a build-level gate.
BUILD_ERROR_PATTERNS = (
    r"Cannot find module",
    r"Failed to resolve import",
    r"Could not resolve",
    r"SyntaxError",
    r"Transform failed",
    r"error TS\d+",
    r"Module not found",
    r"is not exported by",
    r"ERR_MODULE_NOT_FOUND",
    r"Your test suite must contain at least one test",
)


def _read_package_json(ctx: DetectionContext) -> dict:
    raw = ctx.read("package.json")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


class JsonReportRunner(Runner):
    """Shared behaviour for jest and vitest."""

    language = "javascript"
    report_suffix = "json"

    def default_test_dir(self) -> str | None:
        for candidate in ("test", "tests", "src", "__tests__"):
            if os.path.isdir(os.path.join(self.repo, candidate)):
                return candidate
        return None

    def parse(self, report_path: str, exit_code: int, stdout: str, stderr: str) -> TestRunResult:
        return parse_jest_json(report_path, exit_code, stdout, stderr)


class JestRunner(JsonReportRunner):
    id = "jest"

    def build_command(self, targets: list[str], report_path: str) -> list[str]:
        return [
            "npx", "--no-install", "jest",
            *targets,
            "--ci",
            "--json",
            f"--outputFile={report_path}",
            *self.extra_args,
        ]


class VitestRunner(JsonReportRunner):
    id = "vitest"

    def build_command(self, targets: list[str], report_path: str) -> list[str]:
        return [
            "npx", "--no-install", "vitest", "run",
            *targets,
            "--reporter=json",
            f"--outputFile={report_path}",
            *self.extra_args,
        ]


class NpmTestRunner(Runner):
    """Last resort: `npm test -- <paths>`, exit-code only."""

    id = "npm-test"
    language = "javascript"
    report_suffix = "json"

    def build_command(self, targets: list[str], report_path: str) -> list[str]:
        cmd = ["npm", "test", "--silent"]
        if targets or self.extra_args:
            cmd += ["--", *targets, *self.extra_args]
        return cmd

    def default_test_dir(self) -> str | None:
        for candidate in ("test", "tests", "__tests__"):
            if os.path.isdir(os.path.join(self.repo, candidate)):
                return candidate
        return None

    def parse(self, report_path: str, exit_code: int, stdout: str, stderr: str) -> TestRunResult:
        return parse_exit_code_only(exit_code, stdout, stderr)


# --------------------------------------------------------------------------
# parsers
# --------------------------------------------------------------------------


def parse_jest_json(
    report_path: str, exit_code: int, stdout: str = "", stderr: str = ""
) -> TestRunResult:
    data = _load_json_report(report_path, stdout)
    if data is None:
        outcome = Outcome.PASS if exit_code == 0 else Outcome.RUNNER_ERROR
        return TestRunResult(
            command=[],
            outcome=outcome,
            exit_code=exit_code,
            note="runner produced no JSON report; falling back to exit code only",
        )

    passed = int(data.get("numPassedTests") or 0)
    failed = int(data.get("numFailedTests") or 0)
    skipped = int(data.get("numPendingTests") or 0) + int(data.get("numTodoTests") or 0)
    runtime_error_suites = int(data.get("numRuntimeErrorTestSuites") or 0)

    failing_ids: list[str] = []
    erroring_ids: list[str] = []
    for suite in data.get("testResults") or []:
        assertions = suite.get("assertionResults") or []
        suite_name = suite.get("name") or suite.get("testFilePath") or "<suite>"
        suite_failed_without_assertions = (
            suite.get("status") == "failed" and not any(
                a.get("status") == "failed" for a in assertions
            )
        )
        if suite_failed_without_assertions or suite.get("testExecError"):
            erroring_ids.append(suite_name)
        for a in assertions:
            if a.get("status") == "failed":
                failing_ids.append(a.get("fullName") or a.get("title") or "<test>")

    errored = max(runtime_error_suites, len(erroring_ids))
    total = passed + failed + skipped + errored

    if total == 0:
        return TestRunResult(
            command=[], outcome=Outcome.NO_TESTS_RUN, exit_code=exit_code,
            note="no test files matched the selection",
        )
    if failed:
        outcome = Outcome.ASSERT_FAIL
        note = ""
    elif errored:
        outcome = Outcome.BUILD_ERROR
        note = "test suite failed to load/transform (no assertion ran)"
    elif exit_code == 0:
        outcome, note = Outcome.PASS, ""
    else:
        outcome = Outcome.RUNNER_ERROR
        note = f"report shows no failures but the runner exited {exit_code}"

    return TestRunResult(
        command=[], outcome=outcome, exit_code=exit_code,
        passed=passed, failed=failed, errored=errored, skipped=skipped, total=total,
        failing_ids=failing_ids[:50], erroring_ids=erroring_ids[:50], note=note,
    )


def _load_json_report(report_path: str, stdout: str) -> dict | None:
    if os.path.exists(report_path):
        try:
            with open(report_path, encoding="utf-8", errors="replace") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            pass
    # Some versions ignore --outputFile and print to stdout instead.
    start = stdout.find("{")
    while start != -1:
        try:
            return json.loads(stdout[start:])
        except json.JSONDecodeError:
            start = stdout.find("{", start + 1)
    return None


def parse_exit_code_only(exit_code: int, stdout: str, stderr: str) -> TestRunResult:
    """Exit-code-only classification, with the heuristic declared in ``note``."""
    if exit_code == 0:
        return TestRunResult(command=[], outcome=Outcome.PASS, exit_code=exit_code, total=1, passed=1,
                             note="exit-code-only runner: pass/fail counts unavailable")
    blob = f"{stdout}\n{stderr}"
    for pattern in BUILD_ERROR_PATTERNS:
        if re.search(pattern, blob):
            return TestRunResult(
                command=[], outcome=Outcome.BUILD_ERROR, exit_code=exit_code, errored=1, total=1,
                note=(f"exit-code-only runner: classified as a build/import failure "
                      f"because the output matched /{pattern}/ (heuristic)"),
            )
    return TestRunResult(
        command=[], outcome=Outcome.ASSERT_FAIL, exit_code=exit_code, failed=1, total=1,
        note="exit-code-only runner: non-zero exit with no build-error signature (heuristic)",
    )


# --------------------------------------------------------------------------


def detect_javascript(ctx: DetectionContext, extra_args=None) -> Runner | None:
    if not ctx.exists("package.json"):
        return None
    if not shutil.which("npx") and not shutil.which("npm"):
        return None

    pkg = _read_package_json(ctx)
    deps = {}
    for key in ("devDependencies", "dependencies", "peerDependencies"):
        deps.update(pkg.get(key) or {})
    test_script = (pkg.get("scripts") or {}).get("test", "")

    if "vitest" in deps or "vitest" in test_script:
        why = "vitest in package.json dependencies" if "vitest" in deps else "`scripts.test` runs vitest"
        return VitestRunner(ctx.repo, reason=f"found package.json with {why} -> `npx vitest run`", extra_args=extra_args)
    if "jest" in deps or re.search(r"\bjest\b", test_script):
        why = "jest in package.json dependencies" if "jest" in deps else "`scripts.test` runs jest"
        return JestRunner(ctx.repo, reason=f"found package.json with {why} -> `npx jest`", extra_args=extra_args)
    if test_script and "no test specified" not in test_script:
        return NpmTestRunner(
            ctx.repo,
            reason=(f"found package.json with scripts.test={test_script!r} but no recognised "
                    f"test framework -> `npm test` (exit-code-only results)"),
            extra_args=extra_args,
        )
    return None
