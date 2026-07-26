"""Hunk-level diff surgery, used to localise *which* change is ungated.

A whole-file revert answers "do the tests notice this PR at all". When that
revert only breaks an import, no assertion was ever exercised and the answer is
uninformative - so we go finer and revert one hunk at a time.

Two things live here:

* parsing ``git diff`` into hunks and reverting exactly one of them, and
* deciding whether a hunk can possibly change behaviour. A hunk that only
  touches comments, docstrings or blank lines is *inert*: reverting it proves
  nothing, so it is never allowed to produce a ``NO_GATE`` finding. For Python
  we establish inertness by comparing docstring-stripped ASTs, which is exact
  rather than a regex guess.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from .gitops import git, show_blob

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

#: Comment syntax used by the lexical fallback for non-Python files.
LINE_COMMENT_PREFIXES = ("#", "//", "--", "*", "/*", "*/")


@dataclass
class Hunk:
    path: str
    index: int  # 1-based position within the file's diff
    header: str
    base_start: int
    base_len: int
    head_start: int
    head_len: int
    base_lines: list[str]
    head_lines: list[str]

    @property
    def label(self) -> str:
        return f"{self.path}@{self.header}"

    @property
    def short_label(self) -> str:
        return f"{self.path} hunk {self.index} (head lines {self.head_start}-{self.head_start + max(self.head_len, 1) - 1})"

    def preview(self, max_lines: int = 6) -> str:
        out = []
        for line in self.base_lines[:max_lines]:
            out.append("-" + line.rstrip("\n"))
        for line in self.head_lines[:max_lines]:
            out.append("+" + line.rstrip("\n"))
        return "\n".join(out)


def parse_hunks(diff_text: str, path: str) -> list[Hunk]:
    """Parse the hunks of a single-file unified diff."""
    hunks: list[Hunk] = []
    lines = diff_text.split("\n")
    i = 0
    index = 0
    while i < len(lines):
        m = HUNK_HEADER.match(lines[i])
        if not m:
            i += 1
            continue
        index += 1
        base_start = int(m.group(1))
        base_len = int(m.group(2)) if m.group(2) is not None else 1
        head_start = int(m.group(3))
        head_len = int(m.group(4)) if m.group(4) is not None else 1
        header = m.group(0)
        base_body: list[str] = []
        head_body: list[str] = []
        i += 1
        while i < len(lines):
            line = lines[i]
            if line.startswith("@@") or line.startswith("diff --git"):
                break
            if line.startswith("\\"):  # "\ No newline at end of file"
                i += 1
                continue
            if line.startswith("-"):
                base_body.append(line[1:] + "\n")
            elif line.startswith("+"):
                head_body.append(line[1:] + "\n")
            elif line.startswith(" "):
                base_body.append(line[1:] + "\n")
                head_body.append(line[1:] + "\n")
            elif line == "":
                # A truly empty context line (git emits " " but be forgiving).
                if i + 1 < len(lines):
                    base_body.append("\n")
                    head_body.append("\n")
            i += 1
        hunks.append(
            Hunk(
                path=path,
                index=index,
                header=header,
                base_start=base_start,
                base_len=base_len,
                head_start=head_start,
                head_len=head_len,
                base_lines=base_body,
                head_lines=head_body,
            )
        )
    return hunks


def file_hunks(repo: str, base: str, head: str, path: str, context: int | None = None) -> list[Hunk]:
    """Hunks for one file. ``context`` overrides git's default of three lines.

    Zero context is what :mod:`coretexa_verify.inline_tests` needs: it makes each
    hunk hug the lines that actually changed, so a change next to a
    ``#[cfg(test)]`` block does not get glued to it by shared context lines.
    """
    args = ["diff"]
    if context is not None:
        args.append(f"--unified={context}")
    args += [base, head, "--", path]
    res = git(repo, *args)
    if res.returncode != 0:
        return []
    return parse_hunks(res.stdout, path)


def apply_reverse(head_text: str, hunk: Hunk) -> str:
    """Return ``head_text`` with exactly this hunk rolled back to its base form."""
    lines = head_text.splitlines(keepends=True)
    if hunk.head_len == 0:
        # Nothing exists at head here (a pure deletion by the PR); git points at
        # the line *before* the gap, so we splice the base lines in after it.
        start = hunk.head_start
    else:
        start = hunk.head_start - 1
    end = start + hunk.head_len
    if start < 0 or end > len(lines):
        raise ValueError(f"hunk {hunk.label} does not fit the head file")
    return "".join(lines[:start] + hunk.base_lines + lines[end:])


# --------------------------------------------------------------------------
# inertness
# --------------------------------------------------------------------------


class _DocstringStripper(ast.NodeTransformer):
    """Delete docstring expressions so they do not show up in the AST dump."""

    def _strip(self, node):  # type: ignore[no-untyped-def]
        body = getattr(node, "body", None)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            if isinstance(body[0].value.value, str):
                if len(body) == 1:
                    node.body = [ast.Pass()]
                else:
                    node.body = body[1:]
        self.generic_visit(node)
        return node

    visit_Module = _strip
    visit_ClassDef = _strip
    visit_FunctionDef = _strip
    visit_AsyncFunctionDef = _strip


def python_semantic_fingerprint(source: str) -> str | None:
    """A dump of the AST with docstrings removed, or None if it will not parse."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    tree = _DocstringStripper().visit(tree)
    ast.fix_missing_locations(tree)
    return ast.dump(tree, annotate_fields=True, include_attributes=False)


def lexical_significant_lines(lines: list[str]) -> list[str]:
    out = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith(LINE_COMMENT_PREFIXES):
            continue
        out.append(stripped)
    return out


def is_inert(hunk: Hunk, head_text: str) -> tuple[bool, str]:
    """Can reverting this hunk possibly change program behaviour?

    Returns ``(inert, reason)``. When in doubt we answer "not inert", because a
    hunk wrongly called inert would be excluded from the search and could only
    ever hide a finding, never invent one.
    """
    if hunk.path.endswith(".py"):
        try:
            reverted = apply_reverse(head_text, hunk)
        except ValueError:
            return False, "could not apply the reverse hunk"
        fp_head = python_semantic_fingerprint(head_text)
        fp_rev = python_semantic_fingerprint(reverted)
        if fp_head is not None and fp_rev is not None:
            if fp_head == fp_rev:
                return True, "comments/docstrings/formatting only (identical AST once docstrings are stripped)"
            return False, "changes the parsed program"
        return False, "file does not parse; treated as behavioural"

    if lexical_significant_lines(hunk.base_lines) == lexical_significant_lines(hunk.head_lines):
        return True, "comments/blank lines only"
    return False, "changes non-comment content"


def behavioural_hunks(
    repo: str, base: str, head_sha: str, path: str, head_text: str
) -> tuple[list[Hunk], list[tuple[Hunk, str]]]:
    """Split a file's hunks into (behavioural, [(inert, why)])."""
    behavioural: list[Hunk] = []
    inert: list[tuple[Hunk, str]] = []
    for hunk in file_hunks(repo, base, head_sha, path):
        flag, why = is_inert(hunk, head_text)
        (inert.append((hunk, why)) if flag else behavioural.append(hunk))
    return behavioural, inert


def read_head_text(repo: str, head_sha: str, path: str) -> str | None:
    blob = show_blob(repo, head_sha, path)
    if blob is None:
        return None
    return blob.decode("utf-8", "replace")
