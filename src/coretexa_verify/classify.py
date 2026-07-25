"""Classify changed files as SOURCE, TEST or OTHER.

The whole experiment hinges on this: we revert SOURCE and keep TEST. Getting a
test file wrong and reverting it would silently destroy the PR's own evidence,
so the rules are deliberately explicit and every decision carries a reason
string that ends up in the report.

Three buckets, not two:

* ``TEST``   - executable tests *and* the fixture/snapshot data they read.
* ``SOURCE`` - everything that could plausibly change program behaviour.
* ``OTHER``  - documentation and repo metadata. Reverting a README cannot make a
  test fail, and counting it as SOURCE would let a docs-only PR be reported as
  "source changed but no tests added". We never revert OTHER and we say so.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from dataclasses import dataclass, field

from .models import Kind

# --- directory components that mark a test tree -----------------------------
# A separator is required before "test" so that ``latest/`` or ``contest/`` are
# not swallowed, but ``unit_tests/`` and ``cmstestsuite/`` are.
DEFAULT_TEST_DIR_PATTERNS: tuple[str, ...] = (
    r"^__tests?__$",
    r"^tests?$",
    r"^specs?$",
    r"^testing$",
    r"^test[-_]?(suite|cases|data|fixtures|resources)$",
    r"^.+[-_]tests?$",  # unit_tests, integration-test
    r"^tests?[-_].+$",  # test_data, test-helpers
    r"^[a-z0-9]+testsuite$",  # cmstestsuite
)

# --- basenames that are themselves runnable test modules --------------------
DEFAULT_EXECUTABLE_TEST_PATTERNS: tuple[str, ...] = (
    "test_*.py",
    "*_test.py",
    "*Test.py",
    "*Tests.py",
    "conftest.py",
    "*.test.js",
    "*.test.jsx",
    "*.test.ts",
    "*.test.tsx",
    "*.test.mjs",
    "*.test.cjs",
    "*.spec.js",
    "*.spec.jsx",
    "*.spec.ts",
    "*.spec.tsx",
    "*.spec.mjs",
    "*.spec.cjs",
    "*Test.js",
    "*Test.ts",
    "*Spec.js",
    "*Spec.ts",
)

# --- documentation / metadata ------------------------------------------------
DEFAULT_OTHER_PATTERNS: tuple[str, ...] = (
    "*.md",
    "*.rst",
    "*.adoc",
    "LICENSE*",
    "COPYING*",
    "NOTICE*",
    "AUTHORS*",
    "CHANGELOG*",
    "CODEOWNERS",
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.svg",
    "*.ico",
    "*.pdf",
)
DEFAULT_OTHER_DIR_PREFIXES: tuple[str, ...] = ("docs/", "doc/", ".github/")

CODE_EXTENSIONS: frozenset[str] = frozenset(
    {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
)


@dataclass
class ClassifierConfig:
    """Everything a user can override, with the shipped defaults."""

    test_dir_patterns: list[str] = field(
        default_factory=lambda: list(DEFAULT_TEST_DIR_PATTERNS)
    )
    executable_test_patterns: list[str] = field(
        default_factory=lambda: list(DEFAULT_EXECUTABLE_TEST_PATTERNS)
    )
    other_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_OTHER_PATTERNS))
    other_dir_prefixes: list[str] = field(
        default_factory=lambda: list(DEFAULT_OTHER_DIR_PREFIXES)
    )
    # Explicit user escape hatches, checked before anything else.
    force_test_globs: list[str] = field(default_factory=list)
    force_source_globs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Classification:
    kind: Kind
    reason: str
    executable_test: bool = False


def _norm(path: str) -> str:
    """Normalise separators and strip a leading ``./`` (but never a leading dot)."""
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def matches_any_glob(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    base = posixpath.basename(path)
    for pat in patterns:
        if "/" in pat:
            if fnmatch.fnmatch(path, pat):
                return True
        elif fnmatch.fnmatch(base, pat):
            return True
    return False


def matching_test_dir(path: str, cfg: ClassifierConfig | None = None) -> str | None:
    """Return the first path component that marks this as living in a test tree."""
    cfg = cfg or ClassifierConfig()
    parts = _norm(path).split("/")[:-1]  # directories only
    for part in parts:
        low = part.lower()
        for pat in cfg.test_dir_patterns:
            if re.match(pat, low):
                return part
    return None


def is_executable_test_name(path: str, cfg: ClassifierConfig | None = None) -> bool:
    """True when the file can be handed straight to the test runner."""
    cfg = cfg or ClassifierConfig()
    path = _norm(path)
    if posixpath.splitext(path)[1] not in CODE_EXTENSIONS:
        return False
    return matches_any_glob(path, cfg.executable_test_patterns)


def classify(path: str, cfg: ClassifierConfig | None = None) -> Classification:
    """Classify one repo-relative path."""
    cfg = cfg or ClassifierConfig()
    p = _norm(path)

    if cfg.force_test_globs and matches_any_glob(p, cfg.force_test_globs):
        return Classification(Kind.TEST, "matched configured test glob", is_executable_test_name(p, cfg))
    if cfg.force_source_globs and matches_any_glob(p, cfg.force_source_globs):
        return Classification(Kind.SOURCE, "matched configured source glob")

    executable = is_executable_test_name(p, cfg)
    if executable:
        return Classification(Kind.TEST, "filename matches an executable test pattern", True)

    marker = matching_test_dir(p, cfg)
    if marker is not None:
        # Fixture / snapshot / helper data that lives inside a test tree is TEST:
        # it is part of the evidence the PR is offering, not the code under test.
        return Classification(Kind.TEST, f"lives under test directory '{marker}/'", False)

    if matches_any_glob(p, cfg.other_patterns) or any(
        p.startswith(prefix) for prefix in cfg.other_dir_prefixes
    ):
        return Classification(Kind.OTHER, "documentation or repository metadata")

    return Classification(Kind.SOURCE, "not a test path and not documentation")
