"""Runner registry.

Adding a language is one detector function plus one entry in ``REGISTRY``.
Detectors are tried in order and the first non-None result wins; each returns a
``Runner`` carrying a human-readable ``reason`` that is printed in the report,
so a user can always see *why* a command was chosen.

Order is the whole of the policy. Interpreted languages come first because a
polyglot repository is usually a Python or JavaScript project with a compiled
extension inside it (sqlfluff ships a Rust parser; plenty of Python packages
vendor Go or C), and in that shape the tests that matter are the Python ones.
Java is last because ``pom.xml`` and ``build.gradle`` turn up in repositories
that are only incidentally JVM projects, and because that runner is
experimental.
"""

from __future__ import annotations

from typing import Callable

from .base import BuildStep, DetectionContext, Runner
from .custom import CommandRunner, parse_custom_output
from .golang import GoTestRunner, detect_go, parse_go_test_json
from .java import GradleRunner, MavenRunner, detect_java, parse_junit_dirs
from .javascript import (
    JestRunner,
    NpmTestRunner,
    VitestRunner,
    detect_javascript,
    parse_exit_code_only,
    parse_jest_json,
)
from .python import PytestRunner, detect_python, parse_pytest_report
from .rust import CargoTestRunner, detect_rust, parse_cargo_test_text

Detector = Callable[[DetectionContext, list], "Runner | None"]

#: (name, detector). Order matters: the first match wins.
REGISTRY: list[tuple[str, Detector]] = [
    ("python", detect_python),
    ("javascript", detect_javascript),
    ("go", detect_go),
    ("rust", detect_rust),
    ("java", detect_java),
]


class DetectionFailed(Exception):
    """Raised when no registered detector recognises the repository."""

    def __init__(self, repo: str, tried: list[str]):
        super().__init__(
            f"no test runner could be detected in {repo!r}; tried: {', '.join(tried)} "
            f"(markers looked for: pyproject.toml/setup.cfg/tox.ini, package.json, go.mod, "
            f"Cargo.toml, pom.xml/build.gradle). Override detection with "
            f"`--test-command '<your command>'` on the CLI, or the `test-command` input in "
            f"the Action; add `--junit-path` / `junit-path` if that command can write JUnit "
            f"XML, which makes the assert-vs-build distinction exact instead of heuristic."
        )
        self.tried = tried


def detect_runner(repo: str, extra_args: list[str] | None = None) -> Runner:
    ctx = DetectionContext(repo=repo)
    tried: list[str] = []
    for name, detector in REGISTRY:
        tried.append(name)
        runner = detector(ctx, extra_args or [])
        if runner is not None:
            return runner
    raise DetectionFailed(repo, tried)


__all__ = [
    "REGISTRY",
    "BuildStep",
    "CargoTestRunner",
    "CommandRunner",
    "DetectionContext",
    "DetectionFailed",
    "GoTestRunner",
    "GradleRunner",
    "JestRunner",
    "MavenRunner",
    "NpmTestRunner",
    "PytestRunner",
    "Runner",
    "VitestRunner",
    "detect_go",
    "detect_java",
    "detect_javascript",
    "detect_python",
    "detect_runner",
    "detect_rust",
    "parse_cargo_test_text",
    "parse_custom_output",
    "parse_exit_code_only",
    "parse_go_test_json",
    "parse_jest_json",
    "parse_junit_dirs",
    "parse_pytest_report",
]
