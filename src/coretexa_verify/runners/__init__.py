"""Runner registry.

Adding a language is one detector function plus one entry in ``REGISTRY``.
Detectors are tried in order and the first non-None result wins; each returns a
``Runner`` carrying a human-readable ``reason`` that is printed in the report,
so a user can always see *why* a command was chosen.
"""

from __future__ import annotations

from typing import Callable

from .base import DetectionContext, Runner
from .javascript import (
    JestRunner,
    NpmTestRunner,
    VitestRunner,
    detect_javascript,
    parse_exit_code_only,
    parse_jest_json,
)
from .python import PytestRunner, detect_python, parse_pytest_report

Detector = Callable[[DetectionContext, list], "Runner | None"]

#: (name, detector). Order matters: the first match wins.
REGISTRY: list[tuple[str, Detector]] = [
    ("python", detect_python),
    ("javascript", detect_javascript),
]


class DetectionFailed(Exception):
    """Raised when no registered detector recognises the repository."""

    def __init__(self, repo: str, tried: list[str]):
        super().__init__(
            f"no test runner could be detected in {repo!r}; tried: {', '.join(tried)}. "
            f"Pass an explicit command with --test-command to proceed."
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
    "DetectionContext",
    "DetectionFailed",
    "JestRunner",
    "NpmTestRunner",
    "PytestRunner",
    "Runner",
    "VitestRunner",
    "detect_javascript",
    "detect_python",
    "detect_runner",
    "parse_exit_code_only",
    "parse_jest_json",
    "parse_pytest_report",
]
