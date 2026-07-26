"""Hunk-level diff surgery, used to localise *which* change is ungated.

A whole-file revert answers "do the tests notice this PR at all". When that
revert only breaks an import, no assertion was ever exercised and the answer is
uninformative - so we go finer and revert one hunk at a time.

Three things live here:

* parsing ``git diff`` into hunks and reverting exactly one of them, and
* deciding whether a hunk can possibly change behaviour. A hunk that only
  touches comments, docstrings or blank lines is *inert*: reverting it proves
  nothing, so it is never allowed to produce a ``NO_GATE`` finding. For Python
  we establish inertness by comparing docstring-stripped ASTs, which is exact
  rather than a regex guess.
* detecting **identifier renames**, and keeping them applied when a *sibling*
  hunk is reverted.

Coupled renames
---------------

A rename hunk and the hunk that uses the renamed symbol are not independent.
Reverting either one alone leaves a dangling identifier, the file stops
compiling, and both hunks come back ``BUILD_ERROR`` - which the verdict layer
reads as "the tests only gate the presence of the new code". That is wrong:
under the *consistent* sub-revert, where the rename stays applied and only the
behavioural hunk goes back to base, the tests assert-fail properly. TwiN/gatus
#1719 is exactly this shape.

So a hunk whose entire change is a consistent identifier substitution is:

1. treated as **inert** - renaming a symbol cannot change behaviour, so
   reverting it proves nothing and it may never produce a finding; and
2. mined for a **rename map** (``{old: new}``) which is re-applied to the base
   text spliced in when any sibling hunk in the same file is reverted, so the
   identifiers stay consistent and the file still builds.

When that rewrite cannot be applied cleanly - the new name already means
something else in the reverted text - the rename hunk is instead reverted
*together* with its dependants as one group, and the result is reported as a
coupled-group gate rather than as evidence about either hunk alone.

String and comment content is masked before the comparison, because a rename
almost always drags the symbol's own doc comment and error message along with
it (gatus renamed ``ErrNoEndpointOrSuiteInConfig`` and its message in one
hunk). Masking is safe in the only direction that matters: a hunk called
rename-only is *excluded* from evaluation, so the mistake can suppress a
finding, never manufacture one - the same bias :func:`is_inert` already takes.
"""

from __future__ import annotations

import ast
import difflib
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


def apply_reverse(head_text: str, hunk: Hunk, renames: dict | None = None) -> str:
    """Return ``head_text`` with exactly this hunk rolled back to its base form.

    ``renames`` re-applies an identifier substitution to the base lines being
    spliced in, so a sibling rename hunk that is *not* being reverted does not
    leave the file referring to a symbol that no longer exists. Only the
    spliced-in lines are rewritten; the rest of the file is at head and already
    uses the new names.
    """
    return apply_reverse_many(head_text, [hunk], renames)


def apply_reverse_many(head_text: str, hunk_list: list[Hunk], renames: dict | None = None) -> str:
    """Roll back several hunks of one file in a single pass.

    Applied from the bottom of the file upwards so that each splice leaves the
    line numbers of the hunks above it untouched. This is what makes a
    coupled-group revert (a rename plus the change that consumes it) possible.
    """
    lines = head_text.splitlines(keepends=True)
    ordered = sorted(hunk_list, key=lambda h: h.head_start, reverse=True)
    seen: set[tuple[str, int]] = set()
    for hunk in ordered:
        key = (hunk.path, hunk.index)
        if key in seen:
            continue
        seen.add(key)
        if hunk.head_len == 0:
            # Nothing exists at head here (a pure deletion by the PR); git points
            # at the line *before* the gap, so we splice the base lines in after it.
            start = hunk.head_start
        else:
            start = hunk.head_start - 1
        end = start + hunk.head_len
        if start < 0 or end > len(lines):
            raise ValueError(f"hunk {hunk.label} does not fit the head file")
        body = apply_renames(hunk.base_lines, renames) if renames else hunk.base_lines
        lines = lines[:start] + body + lines[end:]
    return "".join(lines)


# --------------------------------------------------------------------------
# identifier renames
# --------------------------------------------------------------------------

#: An identifier in any language this tool runs tests for.
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Double-quoted, single-quoted and backtick-quoted literals, with escapes.
STRING_LITERAL = re.compile(
    r'"(?:\\.|[^"\\])*"' r"|'(?:\\.|[^'\\])*'" r"|`[^`]*`"
)

#: Placeholders chosen so they can never be produced by :data:`IDENTIFIER`.
_LITERAL_MARK = "\x01"
_IDENT_MARK = "\x02"


def _mask_literals(line: str) -> str:
    return STRING_LITERAL.sub(_LITERAL_MARK, line)


def _skeleton(masked: str) -> str:
    """Everything that is *not* an identifier or a literal, positions kept."""
    return IDENTIFIER.sub(_IDENT_MARK, masked)


def line_rename(base_line: str, head_line: str) -> dict | None:
    """``{old: new}`` if these two lines differ only by renaming identifiers.

    None means the lines differ structurally - different punctuation, a
    different number of identifiers, added or removed code - which is a
    behaviour change as far as we are concerned.
    """
    mb, mh = _mask_literals(base_line), _mask_literals(head_line)
    if _skeleton(mb) != _skeleton(mh):
        return None
    tokens_b = IDENTIFIER.findall(mb)
    tokens_h = IDENTIFIER.findall(mh)
    if len(tokens_b) != len(tokens_h):  # pragma: no cover - implied by skeleton
        return None
    mapping: dict = {}
    for old, new in zip(tokens_b, tokens_h):
        if old == new:
            continue
        if mapping.get(old, new) != new:
            return None
        mapping[old] = new
    return mapping


def rename_map(hunk: Hunk) -> dict | None:
    """``{old: new}`` when this hunk's whole change is an identifier rename.

    Returns None for anything else, including a hunk that adds or deletes
    lines, changes punctuation, or renames inconsistently. A returned map is
    always non-empty and injective, and no name appears on both sides of it, so
    substituting all pairs at once is unambiguous.
    """
    matcher = difflib.SequenceMatcher(a=hunk.base_lines, b=hunk.head_lines, autojunk=False)
    mapping: dict = {}
    changed = False
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changed = True
        if tag != "replace" or (i2 - i1) != (j2 - j1):
            return None  # lines added or removed: structural, not a rename
        for old_line, new_line in zip(hunk.base_lines[i1:i2], hunk.head_lines[j1:j2]):
            pairs = line_rename(old_line, new_line)
            if pairs is None:
                return None
            for old, new in pairs.items():
                if mapping.get(old, new) != new:
                    return None
                mapping[old] = new
    if not changed or not mapping:
        return None
    if len(set(mapping.values())) != len(mapping):
        return None  # two old names collapsing onto one new name
    if any(old in mapping.values() or new in mapping for old, new in mapping.items()):
        return None  # a name on both sides; refuse rather than reason about order
    return mapping


def apply_renames(lines: list[str], renames: dict) -> list[str]:
    """Substitute ``renames`` on whole-identifier boundaries in ``lines``."""
    if not renames:
        return list(lines)
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(k) for k in sorted(renames, key=len, reverse=True)) + r")\b"
    )
    return [pattern.sub(lambda m: renames[m.group(1)], line) for line in lines]


def rename_applies_cleanly(lines: list[str], renames: dict) -> tuple[bool, str]:
    """Can ``renames`` be applied to ``lines`` without merging two symbols?

    Not clean when a name the rename produces already occurs, as its own
    identifier, in the text we are about to rewrite: substituting would make
    two distinct symbols share one name. The caller then falls back to
    reverting the rename hunk and its dependants together as a group.
    """
    if not renames:
        return True, ""
    present = set()
    for line in lines:
        present.update(IDENTIFIER.findall(_mask_literals(line)))
    clash = sorted(n for n in renames.values() if n in present)
    if clash:
        return False, (
            f"the reverted text already uses {', '.join(clash)} as its own identifier, so "
            f"re-applying the rename would merge two distinct symbols"
        )
    return True, ""


def depends_on_renames(hunk: Hunk, renames: dict) -> dict:
    """The subset of ``renames`` whose *old* name occurs in this hunk's base text."""
    if not renames:
        return {}
    present = set()
    for line in hunk.base_lines:
        present.update(IDENTIFIER.findall(_mask_literals(line)))
    return {old: new for old, new in renames.items() if old in present}


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


@dataclass
class FileHunks:
    """One file's hunks, split by what reverting each of them could prove."""

    behavioural: list[Hunk]
    inert: list[tuple[Hunk, str]]
    #: Hunks whose whole change is an identifier rename, with the map each one
    #: performs. They are inert *and* they are the source of :attr:`renames`.
    rename_only: list[tuple[Hunk, dict]]

    @property
    def renames(self) -> dict:
        """Every rename this file performs, merged. ``{old: new}``."""
        merged: dict = {}
        for _, mapping in self.rename_only:
            merged.update(mapping)
        return merged


def split_file_hunks(repo: str, base: str, head_sha: str, path: str, head_text: str) -> FileHunks:
    """Classify a file's hunks as behavioural, inert, or rename-only.

    A rename-only hunk is reported as inert - reverting a consistent identifier
    substitution cannot change behaviour, so the run would establish nothing -
    but unlike a comment hunk it carries information the *other* hunks need:
    the map that keeps their reverted text compiling.
    """
    behavioural: list[Hunk] = []
    inert: list[tuple[Hunk, str]] = []
    rename_only: list[tuple[Hunk, dict]] = []
    for hunk in file_hunks(repo, base, head_sha, path):
        flag, why = is_inert(hunk, head_text)
        if flag:
            inert.append((hunk, why))
            continue
        mapping = rename_map(hunk)
        if mapping:
            rename_only.append((hunk, mapping))
            pairs = ", ".join(f"{old} -> {new}" for old, new in sorted(mapping.items()))
            inert.append(
                (
                    hunk,
                    f"identifier rename only ({pairs}); the rename is kept applied when a "
                    f"sibling hunk in this file is reverted, so it is never evaluated alone",
                )
            )
            continue
        behavioural.append(hunk)
    return FileHunks(behavioural=behavioural, inert=inert, rename_only=rename_only)


def behavioural_hunks(
    repo: str, base: str, head_sha: str, path: str, head_text: str
) -> tuple[list[Hunk], list[tuple[Hunk, str]]]:
    """Split a file's hunks into (behavioural, [(inert, why)])."""
    split = split_file_hunks(repo, base, head_sha, path, head_text)
    return split.behavioural, split.inert


def read_head_text(repo: str, head_sha: str, path: str) -> str | None:
    blob = show_blob(repo, head_sha, path)
    if blob is None:
        return None
    return blob.decode("utf-8", "replace")
