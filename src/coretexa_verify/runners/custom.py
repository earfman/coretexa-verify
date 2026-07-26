"""A test command the user gave us, instead of one we detected.

Detection covers five toolchains. It will never cover all of them, and the
failure mode until now was a dead end: "no test runner could be detected"
recommended a flag that did not exist. This module is that flag.

An explicit command overrides detection *entirely* - no detector runs, no
language is inferred, nothing about the repository's layout is assumed. That
makes it the escape hatch for a Makefile-driven C project, a Bazel target, a
shell script, or a Python repo whose suite only starts through a wrapper.

What we can and cannot know
---------------------------

Everything this tool reports rests on telling three things apart: a test that
ran and failed an assertion, a test binary that never built, and a runner that
was simply used wrongly. A detected runner earns that distinction from a
machine-readable report. An arbitrary command gives us an exit code and two
text streams, so there are two modes and the report always says which was used:

* **JUnit mode** (``--junit-path``). The command is expected to write JUnit XML
  at that path - a file, or a directory of ``*.xml``. Then the counts, the
  failing test names and the failure/error split are all read from the report,
  exactly as they are for pytest or Maven, and the verdict is as trustworthy.
* **Exit-code mode** (no ``--junit-path``). Zero means pass, non-zero means
  fail, and assert-versus-build is a *declared regex heuristic* over the output
  - the same one the ``npm test`` fallback uses, with a few compiled-language
  patterns added. Every result carries the pattern that classified it in its
  ``note``, so a reader can check the guess. This mode can produce
  ``GATE_HOLDS`` and ``GATE_HOLDS_BUILD``; it reports one synthetic "test"
  because it genuinely does not know how many ran.

Targets
-------

Selection still works: it produces the PR's own test files/ids as targets. A
custom command has no convention for receiving them, so:

* if the command contains the placeholder ``{targets}``, the targets are
  substituted there (shell-quoted in shell mode);
* otherwise they are appended as trailing arguments.

Either way the command that actually ran is printed in the report, so what was
executed is never a guess.
"""

from __future__ import annotations

import os
import re
import shlex

from ..gitops import run  # noqa: F401  (kept for symmetry with other runners)
from ..models import Outcome, TestRunResult
from .base import Runner
from .javascript import BUILD_ERROR_PATTERNS as JS_BUILD_ERROR_PATTERNS
from .junit import find_reports, read_reports

#: Shell metacharacters that mean "this needs /bin/sh", matching the rule the
#: dependency-install override already uses so the two behave the same way.
SHELL_CHARS = re.compile(r"[|&;<>()$`\\\"'\n*?\[\]#~=%]")

#: Placeholder for the selected test targets inside a user command.
TARGET_PLACEHOLDER = "{targets}"

#: Output that means the test binary never got as far as running. The JS
#: patterns cover interpreted toolchains; the rest are the compiled-language
#: and interpreter-level spellings a custom command is likely to surface.
BUILD_ERROR_PATTERNS = JS_BUILD_ERROR_PATTERNS + (
    r"\berror\[E\d+\]",
    r"\bundefined reference to\b",
    r"\bfatal error:",
    r"\bcompilation (?:failed|terminated)\b",
    r"\[build failed\]",
    r"\bImportError\b",
    r"\bModuleNotFoundError\b",
    r"\bNameError\b",
    r"\bcannot find symbol\b",
    r"\bno such file or directory\b.*\.(?:h|hpp|so|dll)\b",
    r"\bLNK\d{4}\b",
    r"\bmake: \*\*\* No rule to make target\b",
)


def needs_shell(command: str) -> bool:
    return bool(SHELL_CHARS.search(command))


def parse_custom_output(
    exit_code: int, stdout: str, stderr: str, patterns: tuple = BUILD_ERROR_PATTERNS
) -> TestRunResult:
    """Exit-code classification with the heuristic named in ``note``.

    The note is not decoration. A verdict that turns on "was this a build
    failure or an assertion failure" must show the reader what decided it,
    because in this mode the answer is a guess and has to be legible as one.
    """
    if exit_code == 0:
        return TestRunResult(
            command=[],
            outcome=Outcome.PASS,
            exit_code=exit_code,
            total=1,
            passed=1,
            note=(
                "custom test command, exit-code mode: exit 0 taken as pass. Per-test counts "
                "are unavailable; pass --junit-path if the command can write JUnit XML"
            ),
        )
    blob = f"{stdout}\n{stderr}"
    for pattern in patterns:
        if re.search(pattern, blob):
            return TestRunResult(
                command=[],
                outcome=Outcome.BUILD_ERROR,
                exit_code=exit_code,
                errored=1,
                total=1,
                note=(
                    f"custom test command, exit-code mode: classified as a build/import "
                    f"failure because the output matched /{pattern}/ (declared heuristic)"
                ),
            )
    return TestRunResult(
        command=[],
        outcome=Outcome.ASSERT_FAIL,
        exit_code=exit_code,
        failed=1,
        total=1,
        note=(
            "custom test command, exit-code mode: non-zero exit with no build-error "
            "signature in the output, so treated as an assertion failure (declared heuristic)"
        ),
    )


class CommandRunner(Runner):
    """Runs one user-supplied command. Detection is skipped entirely."""

    id = "custom-command"
    language = "custom"
    report_suffix = "xml"
    #: The command is the selection. A repository whose tests no detector
    #: recognises usually has test files no *selector* recognises either, and
    #: refusing to run because of that would make the escape hatch useless.
    selection_optional = True

    def __init__(
        self,
        repo: str,
        command: str,
        junit_path: str = "",
        extra_args: list[str] | None = None,
    ):
        reason = (
            f"an explicit test command was supplied, so runner detection was skipped: "
            f"`{command}`"
        )
        super().__init__(repo, reason, extra_args)
        self.command = command
        #: Repo-relative (or absolute) path to JUnit XML the command writes.
        self.junit_path = junit_path
        self._shell = needs_shell(command)
        if not junit_path:
            self.setup_warnings.append(
                "the custom test command does not declare a JUnit report path, so results are "
                "classified from the exit code alone and assert-vs-build rests on a text "
                "heuristic that is named in every run's note. Pass --junit-path (Action input "
                "`junit-path`) if the command can write JUnit XML - the verdict is then as "
                "precise as it is for a detected runner"
            )
        self.setup_warnings.append(
            "no build step is detected for a custom test command, so if the suite executes "
            "build output the command itself must rebuild it; otherwise a reverted source file "
            "may never reach the tests"
        )

    # -- command construction ---------------------------------------------
    def _resolved_junit_path(self) -> str:
        if not self.junit_path:
            return ""
        if os.path.isabs(self.junit_path):
            return self.junit_path
        return os.path.join(self.repo, self.junit_path)

    def build_command(self, targets: list[str], report_path: str) -> list[str]:
        """``/bin/sh -c <command>`` when it needs a shell, otherwise argv.

        ``report_path`` is ignored: this runner does not choose where the
        command writes its report, the user does, via ``--junit-path``.
        """
        extras = list(self.extra_args)
        if self._shell:
            joined = " ".join(shlex.quote(t) for t in list(targets) + extras)
            if TARGET_PLACEHOLDER in self.command:
                script = self.command.replace(TARGET_PLACEHOLDER, joined)
            else:
                script = f"{self.command} {joined}".rstrip()
            return ["/bin/sh", "-c", script]
        argv = shlex.split(self.command)
        if TARGET_PLACEHOLDER in argv:
            at = argv.index(TARGET_PLACEHOLDER)
            argv = argv[:at] + list(targets) + argv[at + 1 :]
        else:
            argv = argv + list(targets)
        return argv + extras

    # -- results -----------------------------------------------------------
    def parse(self, report_path: str, exit_code: int, stdout: str, stderr: str) -> TestRunResult:
        junit = self._resolved_junit_path()
        if junit:
            paths = (
                find_reports(junit) if os.path.isdir(junit) else [junit]
            )
            counts = read_reports(paths)
            if counts.parsed:
                return self._from_junit(counts, exit_code)
            # A declared report that is not there is a fact about the command,
            # not about the code. Saying "everything passed" would be a lie and
            # saying "the tests failed" would be a different one, so this is a
            # RUNNER_ERROR - which the verdict layer already treats as "the
            # experiment did not happen".
            return TestRunResult(
                command=[],
                outcome=Outcome.RUNNER_ERROR,
                exit_code=exit_code,
                note=(
                    f"the custom test command was declared to write JUnit XML at "
                    f"{self.junit_path!r} and no readable report was found there, so its "
                    f"result could not be interpreted"
                ),
            )
        return parse_custom_output(exit_code, stdout, stderr)

    def _from_junit(self, counts, exit_code: int) -> TestRunResult:
        if counts.errored:
            outcome = Outcome.BUILD_ERROR
        elif counts.failed:
            outcome = Outcome.ASSERT_FAIL
        elif counts.total == 0:
            outcome = Outcome.NO_TESTS_RUN
        elif exit_code == 0:
            outcome = Outcome.PASS
        else:
            # The report says everything passed and the command disagreed. That
            # is the command telling us about something outside the tests.
            outcome = Outcome.RUNNER_ERROR
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
            note=(
                f"custom test command, JUnit mode: results read from {self.junit_path!r}"
                + (
                    "; the report shows no failure but the command exited "
                    f"{exit_code}"
                    if outcome is Outcome.RUNNER_ERROR
                    else ""
                )
            ),
        )

    # -- deliberately not implemented -------------------------------------
    def collect(self, targets, timeout, extra=None):
        """None: an arbitrary command has no enumeration protocol.

        This switches off selection refinement and the collected-test cap
        rather than guessing at either, and it means the pre-existing-failure
        re-check in :mod:`coretexa_verify.verify` will decline to exclude
        anything. Both are the conservative outcome.
        """
        return None

    def default_test_dir(self) -> str | None:
        return None

    def detect_build_step(self, timeout: int):
        """None: the user's command is the whole pipeline. Warned about in __init__."""
        return None
