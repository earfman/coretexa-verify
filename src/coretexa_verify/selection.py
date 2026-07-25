"""Turn the PR's changed test files into things a test runner can execute.

Two cases:

* The changed file is itself a runnable test module -> run it directly.
* The changed file is fixture/snapshot/YAML data -> find the test module that
  consumes it. We look for literal references, most specific first, and stop at
  the first token that identifies a small set of consumers. If nothing does, we
  fall back to the enclosing test directory and say so in the report, because a
  silent widening of scope would make the verdict mean something different from
  what the user thinks it means.
"""

from __future__ import annotations

import os
import posixpath

from .classify import ClassifierConfig, is_executable_test_name
from .gitops import git
from .models import ChangedFile, Kind, SelectionEntry

#: Directory names too generic to identify a fixture's consumer.
GENERIC_DIR_NAMES = frozenset(
    {
        "test", "tests", "spec", "specs", "__tests__", "fixtures", "fixture",
        "data", "src", "lib", "cases", "resources", "snapshots", "__snapshots__",
        "files", "input", "output", "expected", "golden", "testdata",
    }
)

#: More consumers than this and the token is too vague to trust.
MAX_CONSUMERS = 8


class SelectionError(Exception):
    """No runnable test target could be derived from the PR's test changes."""


def list_executable_tests(repo: str, cfg: ClassifierConfig) -> list[str]:
    res = git(repo, "ls-files")
    if res.returncode != 0:
        return []
    return [p for p in res.stdout.splitlines() if p and is_executable_test_name(p, cfg)]


def candidate_tokens(path: str, cfg: ClassifierConfig) -> list[str]:
    """Literal strings that a consuming test is likely to contain, most specific first."""
    path = path.replace("\\", "/")
    parts = path.split("/")
    tokens: list[str] = [path]
    # progressively shorter path suffixes
    for i in range(1, len(parts) - 1):
        tokens.append("/".join(parts[i:]))
    basename = parts[-1]
    tokens.append(basename)
    stem = posixpath.splitext(basename)[0]
    # directory names, nearest first, skipping ones that mean nothing
    for part in reversed(parts[:-1]):
        if part.lower() not in GENERIC_DIR_NAMES:
            tokens.append(part)
    if stem and stem != basename:
        tokens.append(stem)
    # de-duplicate, keep order
    seen: set[str] = set()
    ordered = []
    for t in tokens:
        if t and t not in seen:
            seen.add(t)
            ordered.append(t)
    return ordered


def find_consumers(
    repo: str, fixture: str, executable_tests: list[str], cfg: ClassifierConfig
) -> tuple[list[str], str]:
    """Return ``(consumer test files, explanation)`` for a fixture file."""
    exec_set = set(executable_tests)
    for token in candidate_tokens(fixture, cfg):
        res = git(repo, "grep", "--files-with-matches", "--fixed-strings", token)
        if res.returncode not in (0, 1) or not res.stdout.strip():
            continue
        hits = [p for p in res.stdout.splitlines() if p in exec_set]
        if 1 <= len(hits) <= MAX_CONSUMERS:
            return sorted(hits), f"test files containing the literal {token!r}"
    return [], ""


def enclosing_test_dir(repo: str, path: str, executable_tests: list[str]) -> str | None:
    """Deepest ancestor directory of ``path`` that actually contains test modules."""
    parts = path.replace("\\", "/").split("/")[:-1]
    while parts:
        prefix = "/".join(parts) + "/"
        if any(t.startswith(prefix) for t in executable_tests):
            return "/".join(parts)
        parts.pop()
    return None


def select_targets(
    repo: str,
    test_files: list[ChangedFile],
    cfg: ClassifierConfig,
    default_test_dir: str | None = None,
) -> tuple[list[str], list[SelectionEntry]]:
    """Map changed TEST files to runner targets.

    Returns the de-duplicated target list and a per-file audit trail.
    """
    executable_tests = list_executable_tests(repo, cfg)
    entries: list[SelectionEntry] = []
    targets: list[str] = []

    def add(paths: list[str]) -> None:
        for p in paths:
            if p not in targets:
                targets.append(p)

    for f in test_files:
        if f.status == "D":
            continue  # deleted at head: nothing to run
        if not os.path.exists(os.path.join(repo, f.path)):
            continue
        if f.executable_test:
            entries.append(SelectionEntry(f.path, [f.path], "direct", "runnable test module"))
            add([f.path])
            continue

        consumers, why = find_consumers(repo, f.path, executable_tests, cfg)
        if consumers:
            entries.append(SelectionEntry(f.path, consumers, "fixture-map", why))
            add(consumers)
            continue

        fallback = enclosing_test_dir(repo, f.path, executable_tests) or default_test_dir
        if fallback:
            entries.append(
                SelectionEntry(
                    f.path,
                    [fallback],
                    "directory-fallback",
                    (f"no test module referenced this file, so the whole "
                     f"'{fallback}' directory was run instead"),
                )
            )
            add([fallback])
        else:
            entries.append(
                SelectionEntry(f.path, [], "directory-fallback", "no consumer and no enclosing test directory found")
            )

    return targets, entries


def has_selectable_tests(entries: list[SelectionEntry]) -> bool:
    return any(e.targets for e in entries)


def classify_all(
    raw: list[tuple[str, str, str | None]], cfg: ClassifierConfig
) -> list[ChangedFile]:
    """Apply :func:`coretexa_verify.classify.classify` to a git name-status list."""
    from .classify import classify

    out: list[ChangedFile] = []
    for status, path, old_path in raw:
        c = classify(path, cfg)
        # A rename out of a test tree into source (or vice versa) is ambiguous;
        # treat the file by its head-side path, which is what will be executed.
        out.append(
            ChangedFile(
                path=path,
                status=status,
                kind=c.kind,
                reason=c.reason,
                old_path=old_path,
                executable_test=c.executable_test and c.kind is Kind.TEST,
            )
        )
    return out
