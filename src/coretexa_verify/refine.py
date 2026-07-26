"""Narrow "the PR's tests" from whole files down to the tests the PR added.

Running a whole test file is often wrong in both directions. It drags in
neighbouring tests the PR never touched - which can fail for unrelated reasons
and turn a perfectly good run into ``INCONCLUSIVE`` - and for parametrised
fixture suites it runs thousands of cases to exercise ten.

Two refinements, both *verified by collection* rather than assumed:

* **Changed test functions.** Intersect the diff's changed line numbers with the
  head file's AST to get ``file::Class::method`` node ids.
* **Added fixture cases.** For a YAML/JSON fixture, take the top-level keys the
  PR added and keep the collected parametrised ids that mention them.

In both cases we ask the runner to collect the proposed ids. Anything that does
not collect is dropped, and if nothing survives we fall back to the whole file.
Narrowing therefore can never invent a target that does not exist.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from .gitops import git

#: Top-level YAML mapping key, e.g. ``my_test_case:`` at column zero.
YAML_TOP_KEY = re.compile(r"^([A-Za-z_][\w.\- ]*):\s*$")
#: Depth-1 JSON key in a pretty-printed file, e.g. ``  "my_case": {``.
JSON_TOP_KEY = re.compile(r'^\s{1,4}"([^"]+)"\s*:')


@dataclass
class Refinement:
    targets: list[str]
    method: str
    detail: str


def changed_line_numbers(repo: str, base: str, head: str, path: str) -> set[int]:
    """Head-side line numbers the PR added or modified in ``path``."""
    res = git(repo, "diff", "--unified=0", base, head, "--", path)
    if res.returncode != 0:
        return set()
    changed: set[int] = set()
    head_line = 0
    for line in res.stdout.split("\n"):
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if m:
            head_line = int(m.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            changed.add(head_line)
            head_line += 1
    return changed


def python_test_node_ids(source: str, path: str, changed_lines: set[int]) -> list[str]:
    """pytest node ids for the test functions containing any changed line."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    ids: list[str] = []

    def span(node: ast.AST) -> range:
        start = getattr(node, "lineno", 0)
        # decorators sit above `lineno`; include them so a changed decorator counts
        for dec in getattr(node, "decorator_list", []) or []:
            start = min(start, getattr(dec, "lineno", start))
        end = getattr(node, "end_lineno", start) or start
        return range(start, end + 1)

    def is_test(name: str) -> bool:
        return name.startswith("test") or name.startswith("Test")

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and is_test(node.name):
            if changed_lines & set(span(node)):
                ids.append(f"{path}::{node.name}")
        elif isinstance(node, ast.ClassDef) and is_test(node.name):
            class_span = set(span(node))
            methods = [
                m for m in node.body
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and is_test(m.name)
            ]
            hit_methods = [m for m in methods if changed_lines & set(span(m))]
            if hit_methods and len(hit_methods) == len(methods):
                ids.append(f"{path}::{node.name}")  # whole class is new
            elif hit_methods:
                ids.extend(f"{path}::{node.name}::{m.name}" for m in hit_methods)
            elif changed_lines & class_span:
                # something changed in the class body but outside any test method
                ids.append(f"{path}::{node.name}")
    return ids


def added_fixture_keys(repo: str, base: str, head: str, path: str) -> list[str]:
    """Top-level keys the PR added to a YAML/JSON fixture file."""
    if not path.endswith((".yml", ".yaml", ".json")):
        return []
    res = git(repo, "diff", "--unified=0", base, head, "--", path)
    if res.returncode != 0:
        return []
    pattern = JSON_TOP_KEY if path.endswith(".json") else YAML_TOP_KEY
    keys: list[str] = []
    for line in res.stdout.split("\n"):
        if not line.startswith("+") or line.startswith("+++"):
            continue
        m = pattern.match(line[1:])
        if m and m.group(1) not in keys:
            keys.append(m.group(1))
    return keys


#: Shorter than this and a fixture stem matches too much to be evidence.
MIN_STEM_LENGTH = 3


def fixture_stem(path: str) -> str:
    """``test/fixtures/dialects/clickhouse/exchange.sql`` -> ``exchange``."""
    import posixpath

    return posixpath.splitext(posixpath.basename(path.replace("\\", "/")))[0]


def parametrised_id(node_id: str) -> str:
    """The ``[...]`` part of a node id, or ``""`` for an unparametrised test."""
    open_at = node_id.find("[")
    close_at = node_id.rfind("]")
    return node_id[open_at + 1 : close_at] if open_at != -1 and close_at > open_at else ""


def filter_collected_by_stem(node_ids: list[str], path: str) -> tuple[list[str], str]:
    """Collected ids whose *parameter* literally names the fixture.

    The basename (``exchange.sql``) is stronger evidence than the bare stem
    (``exchange``), so it wins when both are present. This is the sqlfluff
    #8221 signal: ``dialects_test.py`` parametrises its cases on the fixture
    file name, so the fixture's own name shows up in the collected node id even
    though the module never mentions the dialect.

    Matching is deliberately restricted to the parametrised part of the id. A
    match anywhere in the id would be satisfied by ``widget_test.py::
    test_widget_is_a_string`` - a module that merely shares the fixture's name -
    which is precisely the false positive this whole change exists to kill.
    """
    import posixpath

    basename = posixpath.basename(path.replace("\\", "/"))
    stem = fixture_stem(path)
    if len(stem) < MIN_STEM_LENGTH:
        return [], ""
    params = [(nid, parametrised_id(nid)) for nid in node_ids]
    by_basename = [nid for nid, param in params if param and basename in param]
    if by_basename:
        return by_basename, (
            f"the collected test id(s) are parametrised on the fixture file name {basename!r}"
        )
    by_stem = [nid for nid, param in params if param and stem in param]
    if by_stem:
        return by_stem, (
            f"the collected test id(s) are parametrised on the fixture stem {stem!r}"
        )
    return [], ""


def filter_collected_by_keys(node_ids: list[str], keys: list[str]) -> list[str]:
    """Keep collected parametrised ids whose parameter mentions an added key."""
    if not keys:
        return []
    hits: list[str] = []
    for nid in node_ids:
        param = parametrised_id(nid)
        if not param:
            continue
        if any(key in param for key in keys):
            hits.append(nid)
    return hits


def verify_against_collection(proposed: list[str], collected: list[str]) -> list[str]:
    """Drop proposed ids that the runner did not actually collect.

    A proposed id is kept when it collects exactly, or when it is a prefix of a
    collected id (a class or a parametrised function expands to several ids).
    """
    kept: list[str] = []
    collected_set = set(collected)
    for nid in proposed:
        if nid in collected_set:
            kept.append(nid)
            continue
        if any(c == nid or c.startswith(nid + "::") or c.startswith(nid + "[") for c in collected):
            kept.append(nid)
    return kept
