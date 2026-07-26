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
from .base import BuildStep, DetectionContext, Runner

#: Directories that hold compiled output rather than source.
BUILD_OUTPUT_DIRS = ("dist", "build", "lib", "out", "es", "esm", "cjs")

#: package.json script names, in priority order, that produce that output.
BUILD_SCRIPT_NAMES = ("build:all", "build", "build:ts", "compile", "prepare:build")

#: Bare or relative import specifier in an ES module or a `require(...)` call.
_IMPORT_RE = re.compile(
    r"""(?:from|import)\s*\(?\s*['"]([^'"]+)['"]|require\(\s*['"]([^'"]+)['"]\s*\)"""
)

#: A build-output directory used as a *path component*. Matching the bare
#: substring instead would call ``src/types/index`` build output, because
#: "types/" ends in "es/".
_BUILT_DIR_RE = re.compile(
    r"""(?:^|[/."'])(?:""" + "|".join(BUILD_OUTPUT_DIRS) + r""")/"""
)

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


# --------------------------------------------------------------------------
# workspaces (monorepos)
# --------------------------------------------------------------------------


def read_json_file(path: str) -> dict:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def workspace_globs(repo: str) -> list[str]:
    """Workspace member patterns declared by the repo, or ``[]``.

    Two shapes are supported because two shapes exist: npm/yarn/bun declare
    ``workspaces`` in package.json (array, or ``{"packages": [...]}``), pnpm
    declares ``packages:`` in ``pnpm-workspace.yaml``. The yaml is read with a
    line scan rather than a YAML parser - this tool has no dependencies, and
    the fragment we need is a flat list of strings.
    """
    pkg = read_json_file(os.path.join(repo, "package.json"))
    declared = pkg.get("workspaces")
    if isinstance(declared, dict):
        declared = declared.get("packages")
    globs = [g for g in (declared or []) if isinstance(g, str)]

    pnpm = os.path.join(repo, "pnpm-workspace.yaml")
    if not os.path.exists(pnpm):
        pnpm = os.path.join(repo, "pnpm-workspace.yml")
    if os.path.exists(pnpm):
        in_packages = False
        try:
            with open(pnpm, encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    line = raw.rstrip("\n")
                    if not line.strip() or line.lstrip().startswith("#"):
                        continue
                    if not line[:1].isspace():
                        in_packages = line.strip().startswith("packages:")
                        continue
                    if in_packages and line.strip().startswith("- "):
                        value = line.strip()[2:].strip().strip("'\"")
                        if value:
                            globs.append(value)
        except OSError:  # pragma: no cover - defensive
            pass
    return globs


def is_workspace(repo: str) -> bool:
    return bool(workspace_globs(repo))


def owning_package(repo: str, rel_path: str) -> str:
    """Nearest ancestor directory of ``rel_path`` holding a package.json.

    Returns a repo-relative directory, or ``""`` when the only package.json is
    the repository root's.
    """
    parts = rel_path.replace("\\", "/").split("/")[:-1]
    while parts:
        candidate = "/".join(parts)
        if os.path.exists(os.path.join(repo, candidate, "package.json")):
            return candidate
        parts.pop()
    return ""


def workspace_package_dirs(repo: str) -> dict:
    """Map every workspace package *name* to its repo-relative directory."""
    out: dict = {}
    for glob_pattern in workspace_globs(repo):
        pattern = glob_pattern.lstrip("!")
        if glob_pattern.startswith("!"):
            continue
        # Only the directory prefix matters; '**/*' and '*' both mean "descend".
        root = pattern.split("*", 1)[0].rstrip("/")
        base = os.path.join(repo, root) if root else repo
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d != "node_modules"]
            if "package.json" not in filenames:
                continue
            rel = os.path.relpath(dirpath, repo).replace("\\", "/")
            if rel == ".":
                continue
            name = read_json_file(os.path.join(dirpath, "package.json")).get("name")
            if isinstance(name, str) and name:
                out.setdefault(name, rel)
    return out


def entry_points_are_built(pkg: dict) -> bool:
    """Does this package.json point consumers at compiled output?"""
    blob = json.dumps(
        {k: pkg.get(k) for k in ("main", "module", "types", "typings", "exports", "browser")}
    )
    return bool(_BUILT_DIR_RE.search(blob))


def detect_build_step(repo: str, timeout: int = 900) -> BuildStep | None:
    """The repo's own build script, if it declares one we recognise."""
    pkg = read_json_file(os.path.join(repo, "package.json"))
    scripts = pkg.get("scripts") or {}
    for name in BUILD_SCRIPT_NAMES:
        if isinstance(scripts.get(name), str) and scripts[name].strip():
            manager = _package_manager(repo)
            if manager is None:
                return None
            return BuildStep(
                argv=[manager, "run", name],
                reason=f"package.json declares scripts.{name}={scripts[name]!r}",
                cwd=repo,
                timeout=timeout,
            )
    return None


def _package_manager(repo: str) -> str | None:
    if os.path.exists(os.path.join(repo, "pnpm-lock.yaml")) or os.path.exists(
        os.path.join(repo, "pnpm-workspace.yaml")
    ):
        if shutil.which("pnpm"):
            return "pnpm"
    if os.path.exists(os.path.join(repo, "yarn.lock")) and shutil.which("yarn"):
        return "yarn"
    return "npm" if shutil.which("npm") else None


class JsWorkspaceMixin:
    """Monorepo awareness shared by every JavaScript runner."""

    def focus(self, targets: list[str]) -> tuple[list[str], str] | None:
        """Run from the workspace package that owns the selected tests.

        Running a workspace's tests from the repository root is the reason
        every JS monorepo came back INCONCLUSIVE: the root has no runner config
        for a package's sources, so every suite fails to transform before a
        single assertion runs. The package directory has the config, so that is
        where the runner belongs.
        """
        if not is_workspace(self.repo):
            return None
        owners = {owning_package(self.repo, t) for t in targets if t}
        if len(owners) != 1:
            return None
        owner = owners.pop()
        if not owner:
            return None
        rewritten = [os.path.relpath(t, owner).replace("\\", "/") for t in targets]
        self.cwd = os.path.join(self.repo, owner)
        return rewritten, (
            f"{owner}/package.json owns every selected test, and this repository declares "
            f"workspaces, so the runner was invoked from {owner} with that package's own config"
        )

    def artifact_risk(self, targets: list[str], source_paths: list[str]) -> str:
        return javascript_artifact_risk(self.repo, targets, source_paths)

    def detect_build_step(self, timeout: int) -> "BuildStep | None":
        """JavaScript is the one language here with a *separate* build.

        ``dist/`` outlives the source it was built from, so unless this step is
        re-run inside every mutation the tests can read a build of code we just
        reverted. Go, Rust and the JVM all recompile as part of running tests;
        this does not.
        """
        return detect_build_step(self.repo, timeout=timeout)


class JsonReportRunner(JsWorkspaceMixin, Runner):
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


def javascript_artifact_risk(repo: str, targets: list[str], source_paths: list[str]) -> str:
    """Do the selected tests read compiled output rather than the changed source?

    Two shapes count. A test that imports a path containing ``dist/`` is
    obvious. The monorepo shape is not: a test imports a *sibling workspace
    package* by name, and that package's package.json resolves the name to
    ``dist/``, so the test is reading a build of the source we are about to
    revert.
    """
    packages = workspace_package_dirs(repo)
    changed_pkgs = {owning_package(repo, p) for p in source_paths}
    changed_pkgs.discard("")
    for target in targets:
        path = os.path.join(repo, target)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read(200_000)
        except OSError:  # pragma: no cover - defensive
            continue
        for match in _IMPORT_RE.finditer(text):
            spec = match.group(1) or match.group(2) or ""
            if not spec:
                continue
            if _BUILT_DIR_RE.search(spec):
                return (
                    f"{target} imports {spec!r}, which is build output rather than source"
                )
            owner = _matching_package(spec, packages)
            if owner and owner in changed_pkgs:
                pkg = read_json_file(os.path.join(repo, owner, "package.json"))
                if entry_points_are_built(pkg):
                    return (
                        f"{target} imports the workspace package {spec!r}, whose package.json "
                        f"resolves to built output under {owner}/; the tests therefore read a "
                        f"build of the changed source, not the source itself"
                    )
    return ""


def _matching_package(spec: str, packages: dict) -> str:
    for name, directory in packages.items():
        if spec == name or spec.startswith(name + "/"):
            return directory
    return ""


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


class NpmTestRunner(JsWorkspaceMixin, Runner):
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
