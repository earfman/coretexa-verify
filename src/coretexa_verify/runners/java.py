"""Maven and Gradle runners. **Experimental.**

Both build tools already write JUnit XML - Surefire to
``target/surefire-reports/``, Gradle to ``build/test-results/test/`` - so the
result parsing is the shared reader in :mod:`coretexa_verify.runners.junit`,
the same one pytest uses, with the same ``<failure>`` vs ``<error>`` split that
separates ``GATE_HOLDS`` from ``GATE_HOLDS_BUILD``.

What makes this runner experimental is not the parsing but the *running*. A
Maven or Gradle build resolves its dependency graph from a remote repository on
first use, and a JVM project's test phase is routinely coupled to plugins
(shading, code generation, containerised integration tests) that this tool
cannot reason about. Selection is also weaker than elsewhere: ``-Dtest=`` and
``--tests`` take class names, and mapping a changed ``.java`` file to a class
name is a filesystem convention rather than something the toolchain guarantees.

So: the command construction and the report reading are covered by unit tests
over canned output, and the runner is registered last so it can only ever be
chosen for a repository that is neither Python, JavaScript, Go nor Rust. What
has *not* been done is an end-to-end validation against a real JVM pull
request. Treat a Java verdict accordingly.

Build artefacts
---------------

Unlike ``go test`` and ``cargo test``, a JVM build genuinely does have a
separate compile step producing artefacts (``target/classes``,
``build/classes``) that a test could read instead of the source. Both ``mvn
test`` and ``gradle test`` depend on their own compile task and re-run it when
a source file changes, so the artefact is regenerated inside each mutated run -
which is the property the tool needs. That is why :meth:`detect_build_step`
returns None here too, but the reason is "the test goal already depends on the
compile goal", not "there is no compile".
"""

from __future__ import annotations

import os
import posixpath
import shutil

from ..models import Outcome, TestRunResult
from . import junit
from .base import DetectionContext, Runner

#: Source roots whose contents map to a package-qualified class name.
TEST_SOURCE_ROOTS = (
    "src/test/java/",
    "src/test/kotlin/",
    "src/test/groovy/",
    "src/test/scala/",
)

#: Resource roots whose contents are on the test classpath of the same module.
TEST_RESOURCE_ROOTS = ("src/test/resources/",)

#: Output that means the build never produced a test to run.
BUILD_FAILURE_MARKERS = (
    "COMPILATION ERROR",
    "BUILD FAILURE",
    "Compilation failed",
    "error: cannot find symbol",
    "Could not resolve dependencies",
    "Execution failed for task ':compileJava'",
    "Execution failed for task ':compileTestJava'",
)


def fully_qualified_class(rel_path: str) -> str:
    """``src/test/java/com/x/FooTest.java`` -> ``com.x.FooTest``.

    Returns ``""`` when the path is not under a recognised test source root,
    because a class name guessed from an unconventional layout would be handed
    straight to ``-Dtest=`` and silently match nothing.
    """
    path = rel_path.replace("\\", "/")
    for root in TEST_SOURCE_ROOTS:
        marker = "/" + root
        idx = path.find(marker)
        if idx != -1:
            tail = path[idx + len(marker):]
        elif path.startswith(root):
            tail = path[len(root):]
        else:
            continue
        stem = posixpath.splitext(tail)[0]
        return stem.replace("/", ".")
    return ""


def module_root(repo: str, rel_path: str, manifests: tuple[str, ...]) -> str:
    """Nearest ancestor directory holding one of ``manifests``."""
    parts = rel_path.replace("\\", "/").split("/")[:-1]
    while parts:
        candidate = "/".join(parts)
        if any(os.path.exists(os.path.join(repo, candidate, m)) for m in manifests):
            return candidate
        parts.pop()
    return ""


def parse_junit_dirs(
    directories: list[str], exit_code: int, stdout: str = "", stderr: str = ""
) -> TestRunResult:
    """Read every JUnit XML the build wrote and classify the run.

    The build's own console output is consulted only when there is no XML at
    all. That case is the important one: a compilation failure produces zero
    reports, and calling zero reports "no tests ran" would turn the strongest
    gate signal a JVM project can give into a shrug.
    """
    reports = junit.find_reports(*directories)
    counts = junit.read_reports(reports)
    blob = (stdout or "") + "\n" + (stderr or "")

    if not counts.parsed:
        marker = next((m for m in BUILD_FAILURE_MARKERS if m in blob), "")
        if marker:
            return TestRunResult(
                command=[],
                outcome=Outcome.BUILD_ERROR,
                exit_code=exit_code,
                errored=1,
                total=1,
                erroring_ids=["<compilation>"],
                note=(
                    f"the build failed before any test ran (matched {marker!r}) and wrote no "
                    f"JUnit XML, so no assertion was exercised"
                ),
            )
        if exit_code == 0:
            return TestRunResult(
                command=[], outcome=Outcome.NO_TESTS_RUN, exit_code=exit_code,
                note="the build succeeded but wrote no JUnit XML reports",
            )
        return TestRunResult(
            command=[], outcome=Outcome.RUNNER_ERROR, exit_code=exit_code,
            note=f"the build exited {exit_code} and wrote no JUnit XML reports",
        )

    if counts.total == 0:
        return TestRunResult(
            command=[], outcome=Outcome.NO_TESTS_RUN, exit_code=exit_code,
            note="JUnit XML was written but contained no test cases",
        )

    if counts.failed:
        outcome, note = Outcome.ASSERT_FAIL, ""
    elif counts.errored:
        outcome = Outcome.BUILD_ERROR
        note = "test(s) errored before or during setup rather than failing an assertion"
    elif exit_code == 0:
        outcome, note = Outcome.PASS, ""
    else:
        outcome = Outcome.RUNNER_ERROR
        note = f"every reported test passed but the build exited {exit_code}"

    return TestRunResult(
        command=[],
        outcome=outcome,
        exit_code=exit_code,
        passed=counts.passed,
        failed=counts.failed,
        errored=counts.errored,
        skipped=counts.skipped,
        total=counts.total,
        failing_ids=counts.failing[:50],
        erroring_ids=counts.erroring[:50],
        note=note,
    )


class JvmRunner(Runner):
    """Shared behaviour for Maven and Gradle."""

    language = "java"
    report_suffix = "xml"
    test_file_extensions = (".java", ".kt", ".groovy", ".scala")
    #: repo-relative module the command runs in; "" is the repository root
    manifests: tuple[str, ...] = ()
    report_dirs: tuple[str, ...] = ()

    def __init__(self, repo: str, reason: str, extra_args=None):
        super().__init__(repo, reason, extra_args)
        self.module = ""

    def default_test_dir(self) -> str | None:
        return None

    def detect_build_step(self, timeout: int) -> None:
        """None: `mvn test` and `gradle test` already depend on their compile task."""
        return None

    def artifact_risk(self, targets: list[str], source_paths: list[str]) -> str:
        """Empty: the test goal re-runs compilation, so classes match the source."""
        return ""

    def _report_directories(self) -> list[str]:
        base = os.path.join(self.repo, self.module) if self.module else self.repo
        return [os.path.join(base, d) for d in self.report_dirs]

    def parse(self, report_path: str, exit_code: int, stdout: str, stderr: str) -> TestRunResult:
        return parse_junit_dirs(self._report_directories(), exit_code, stdout, stderr)

    def focus(self, targets: list[str]) -> tuple[list[str], str] | None:
        """Turn changed test file paths into fully-qualified class names."""
        classes: list[str] = []
        roots: set[str] = set()
        mapped = False
        for target in targets:
            if "/" not in target:
                if target not in classes:
                    classes.append(target)
                continue
            fqcn = fully_qualified_class(target)
            if not fqcn:
                return None
            mapped = True
            roots.add(module_root(self.repo, target, self.manifests))
            if fqcn not in classes:
                classes.append(fqcn)
        if not mapped:
            return None
        if len(roots) > 1:
            return None  # one command cannot span two modules
        self.module = roots.pop() if roots else ""
        if self.module:
            self.cwd = os.path.join(self.repo, self.module)
        where = f"the {self.module} module" if self.module else "the project root"
        return list(classes), (
            f"{self.id} selects tests by class name, so the changed test file(s) were mapped to "
            f"{', '.join(classes[:4])} and run in {where}"
        )

    def fixture_targets(self, fixture_path: str):
        """``src/test/resources/**`` is on the same module's test classpath.

        Both Maven and Gradle put every file under a module's test resource
        root onto that module's test classpath, so the consuming tests are
        exactly that module's tests. That is a build-tool guarantee rather than
        a grep, which is why it is offered ahead of the literal search.
        """
        path = fixture_path.replace("\\", "/")
        for root in TEST_RESOURCE_ROOTS:
            idx = path.find("/" + root)
            prefix = path[:idx] if idx != -1 else ("" if path.startswith(root) else None)
            if prefix is None:
                continue
            src_root = f"{prefix}/{root}" if prefix else root
            test_root = src_root.replace("resources/", "java/")
            abs_root = os.path.join(self.repo, test_root)
            if not os.path.isdir(abs_root):
                return None
            tests: list[str] = []
            for dirpath, _dirnames, filenames in os.walk(abs_root):
                for name in sorted(filenames):
                    if name.endswith((".java", ".kt")):
                        rel = os.path.relpath(os.path.join(dirpath, name), self.repo)
                        tests.append(rel.replace("\\", "/"))
            if not tests:
                return None
            return (
                sorted(tests),
                f"the fixture lives under {src_root}, the test resource root of this module",
                (
                    f"Maven and Gradle place every file under a module's test resource root on "
                    f"that module's test classpath, so {src_root} is readable only by the tests "
                    f"in {test_root}"
                ),
            )
        return None


class MavenRunner(JvmRunner):
    id = "maven"
    manifests = ("pom.xml",)
    report_dirs = ("target/surefire-reports", "target/failsafe-reports")

    def build_command(self, targets: list[str], report_path: str) -> list[str]:
        argv = ["mvn", "-B", "-q", "test"]
        if targets:
            argv.append("-Dtest=" + ",".join(targets))
            # Without this, Surefire fails the build when a named class holds
            # no matching test - which would look like a gate that is really a
            # selection mistake.
            argv.append("-Dsurefire.failIfNoSpecifiedTests=false")
        return argv + self.extra_args


class GradleRunner(JvmRunner):
    id = "gradle"
    manifests = ("build.gradle", "build.gradle.kts")
    report_dirs = ("build/test-results/test",)

    def __init__(self, repo: str, reason: str, extra_args=None, wrapper: bool = True):
        super().__init__(repo, reason, extra_args)
        self.wrapper = wrapper

    def build_command(self, targets: list[str], report_path: str) -> list[str]:
        launcher = os.path.join(self.repo, "gradlew") if self.wrapper else "gradle"
        argv = [launcher, "test", "--console=plain"]
        for target in targets:
            argv += ["--tests", target]
        return argv + self.extra_args


# --------------------------------------------------------------------------


def detect_java(ctx: DetectionContext, extra_args=None) -> Runner | None:
    """Pick a JVM runner, or return None if this is not a JVM project."""
    experimental = (
        "the Java runner is experimental: its command construction and JUnit XML reading are "
        "unit-tested, but it has not been validated end to end against a real JVM pull request"
    )

    if ctx.exists("pom.xml"):
        if not shutil.which("mvn"):
            return None
        runner = MavenRunner(
            ctx.repo,
            reason="found pom.xml -> `mvn test -Dtest=<classes>` (surefire JUnit XML)",
            extra_args=extra_args,
        )
        runner.setup_warnings.append(experimental)
        return runner

    if ctx.exists("build.gradle", "build.gradle.kts"):
        wrapper = ctx.exists("gradlew")
        if not wrapper and not shutil.which("gradle"):
            return None
        launcher = "./gradlew" if wrapper else "gradle"
        runner = GradleRunner(
            ctx.repo,
            reason=(
                f"found build.gradle(.kts) -> `{launcher} test --tests <patterns>` "
                f"(build/test-results JUnit XML)"
            ),
            extra_args=extra_args,
            wrapper=wrapper,
        )
        runner.setup_warnings.append(experimental)
        return runner

    return None
