"""Shared JUnit XML reading.

pytest, Maven Surefire and Gradle all write the same XML vocabulary, and all
three make the one distinction this tool is built around: ``<failure>`` is an
assertion that fired, ``<error>`` is the test never getting that far (an
exception in setup, a class that would not load, a fixture that blew up). That
is exactly ``GATE_HOLDS`` versus ``GATE_HOLDS_BUILD``, so it is worth one
careful reader rather than three approximate ones.

Only the reading is shared. What a given exit code *means* is runner-specific
and stays in the runner's own module.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET


class JUnitCounts:
    """Case counts and names from one or more JUnit XML documents."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.errored = 0
        self.skipped = 0
        self.failing: list[str] = []
        self.erroring: list[str] = []
        #: False when no document could be parsed at all - which is never the
        #: same thing as "everything passed".
        self.parsed = False

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errored + self.skipped

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "failed": self.failed,
            "errored": self.errored,
            "skipped": self.skipped,
        }


def case_id(case: ET.Element) -> str:
    classname = case.get("classname") or ""
    name = case.get("name") or "<unnamed>"
    return f"{classname}::{name}" if classname else name


def read_reports(paths: list[str]) -> JUnitCounts:
    """Count every ``<testcase>`` across the given XML files.

    A file that does not parse is skipped rather than aborting the whole read:
    Surefire writes one document per test class, and one truncated document
    (a JVM crash mid-class) must not erase the results of the others. If *no*
    document parses, ``parsed`` stays False and the caller decides what that
    means.
    """
    counts = JUnitCounts()
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError):
            continue
        counts.parsed = True
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        for suite in suites:
            for case in suite.iter("testcase"):
                name = case_id(case)
                if case.find("error") is not None:
                    counts.errored += 1
                    counts.erroring.append(name)
                elif case.find("failure") is not None:
                    counts.failed += 1
                    counts.failing.append(name)
                elif case.find("skipped") is not None:
                    counts.skipped += 1
                else:
                    counts.passed += 1
    return counts


def find_reports(*directories: str) -> list[str]:
    """Every ``*.xml`` directly inside the given report directories."""
    out: list[str] = []
    for directory in directories:
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if name.endswith(".xml"):
                out.append(os.path.join(directory, name))
    return out
