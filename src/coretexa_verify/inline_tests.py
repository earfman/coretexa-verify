"""Test code that lives *inside* a source file.

The whole experiment rests on a file-level split: revert SOURCE, keep TEST. In
Python and JavaScript that split is real - a test lives in ``test_foo.py`` or
``foo.test.ts`` and the code under test lives somewhere else. In Rust it is not.
The idiomatic place for a unit test is a ``#[cfg(test)] mod tests`` block at the
bottom of the very file it tests, so one file is *both* halves of the
experiment. Reverting it wholesale deletes the PR's own evidence and the run
then measures nothing; refusing to revert it at all means never running the
experiment on the most common shape of Rust PR there is.

Both of the real Rust pull requests this tool was validated against are exactly
that shape (ripgrep #3485 and gtk4-rs' WidgetClassExt branch each change a
single ``src/*.rs`` file containing both the fix and its ``#[cfg(test)]``
tests), so this is the common case, not an edge case.

So the cut moves inside the file. This module finds the head-side line ranges
that hold test code, and the revert is then done *per hunk*: a diff hunk that
lies outside every test region is rolled back to base, and a hunk that touches a
test region is left at head. The result is the base implementation carrying the
PR's new tests - precisely the state the experiment wants, and the state that
makes ``GATE_HOLDS_BUILD`` mean something for a compiled language.

Three rules, all in the direction of never destroying evidence:

1. A hunk that *straddles* a region boundary is left at head. We cannot say
   which base lines correspond to which half, and guessing wrong would delete a
   test. It is reported, not silently dropped.
2. A file added wholesale by the PR is one straddling hunk, so it is left at
   head in full and reported. A new file cannot be partially reverted.
3. If nothing outside the test regions can be reverted, no revert is claimed.
   The caller turns that into INCONCLUSIVE rather than a verdict.
"""

from __future__ import annotations

import re
from typing import Iterable

from .hunks import Hunk, apply_reverse, file_hunks
from .models import ChangedFile, Kind

#: Attribute path segments that mark the item below them as test code.
#: ``tokio::test`` and ``async_std::test`` end in ``test``; ``rstest`` and
#: friends are named outright because they do not.
TEST_ATTRIBUTE_LEAVES = frozenset(
    {"test", "bench", "rstest", "test_case", "proptest", "quickcheck"}
)

#: Start of a Rust raw string: ``r"``, ``r#"``, ``br##"`` ...
_RAW_STRING_START = re.compile(r"b?r(?P<hashes>#*)\"")

_IDENT_CHARS = re.compile(r"[A-Za-z0-9_]")


def _is_ident_char(ch: str) -> bool:
    return bool(_IDENT_CHARS.match(ch))


def code_mask(text: str) -> list[bool]:
    """One flag per character: True when it is code, False in a comment/literal.

    Brace matching and attribute scanning both have to ignore braces that live
    inside a string or a comment, or ``mod tests { ... "}" ... }`` closes the
    module three characters early and the region is nonsense. Rust needs a real
    scanner rather than a regex for this: block comments nest, raw strings
    (``r#"..."#``) have no escapes, and ``'a`` is a lifetime while ``'a'`` is a
    character literal.
    """
    n = len(text)
    mask = [True] * n

    def blank(start: int, stop: int) -> None:
        for k in range(start, min(stop, n)):
            mask[k] = False

    i = 0
    while i < n:
        ch = text[i]

        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            end = text.find("\n", i)
            end = n if end == -1 else end
            blank(i, end)
            i = end
            continue

        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            depth = 1
            j = i + 2
            while j < n and depth:
                if text[j] == "/" and j + 1 < n and text[j + 1] == "*":
                    depth += 1
                    j += 2
                elif text[j] == "*" and j + 1 < n and text[j + 1] == "/":
                    depth -= 1
                    j += 2
                else:
                    j += 1
            blank(i, j)
            i = j
            continue

        if ch in "rb" and (i == 0 or not _is_ident_char(text[i - 1])):
            m = _RAW_STRING_START.match(text, i)
            if m:
                closer = '"' + m.group("hashes")
                end = text.find(closer, m.end())
                end = n if end == -1 else end + len(closer)
                blank(i, end)
                i = end
                continue

        if ch == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    j += 1
                    break
                j += 1
            blank(i, j)
            i = j
            continue

        if ch == "'":
            # `'\n'` and `'a'` are literals; `'a` on its own is a lifetime.
            if i + 1 < n and text[i + 1] == "\\":
                j = i + 2
                while j < n and text[j] != "'":
                    j += 1
                blank(i, j + 1)
                i = j + 1
                continue
            if i + 2 < n and text[i + 2] == "'":
                blank(i, i + 3)
                i += 3
                continue
            i += 1  # lifetime: the quote is code, nothing to skip
            continue

        i += 1
    return mask


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for idx, ch in enumerate(text):
        if ch == "\n":
            starts.append(idx + 1)
    return starts


def _line_of(offset: int, starts: list[int]) -> int:
    """1-based line number containing ``offset``."""
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def _match_bracket(text: str, mask: list[bool], start: int, open_ch: str, close_ch: str) -> int:
    """Index just past the bracket opened at ``start``, or -1."""
    depth = 0
    i = start
    n = len(text)
    while i < n:
        if mask[i]:
            if text[i] == open_ch:
                depth += 1
            elif text[i] == close_ch:
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1
    return -1


def _attribute_is_test(attr: str) -> bool:
    """Does this attribute body mark the item below it as test-only code?

    ``cfg(test)``, ``cfg(all(test, unix))`` and ``cfg(any(test, feature = "x"))``
    all qualify. ``cfg(not(test))`` is the opposite - it marks code compiled
    only when *not* testing - so it never qualifies. A bare ``#[test]`` or a
    framework spelling like ``#[tokio::test]`` qualifies on its leaf segment.
    """
    body = attr.strip()
    if body.startswith("cfg"):
        inner = body[3:].strip()
        if not inner.startswith("("):
            return False
        if "not(" in inner.replace(" ", ""):
            return False
        return bool(re.search(r"\btest\b", inner))
    path = body.split("(", 1)[0].strip()
    if not path:
        return False
    leaf = path.split("::")[-1].strip()
    return leaf in TEST_ATTRIBUTE_LEAVES


def _extend_start_backwards(text: str, starts: list[int], line: int) -> int:
    """Walk back over attribute and doc-comment lines glued to this item."""
    while line > 1:
        prev = line - 1
        begin = starts[prev - 1]
        end = starts[prev] - 1 if prev < len(starts) else len(text)
        stripped = text[begin:end].strip()
        if stripped.startswith("#[") or stripped.startswith("//"):
            line = prev
            continue
        break
    return line


def rust_test_regions(text: str) -> list[tuple[int, int]]:
    """1-based inclusive line ranges of ``#[cfg(test)]``/``#[test]`` items.

    Returns merged, ascending ranges. A file whose braces do not balance yields
    whatever it could prove and nothing more - an unterminated region is dropped
    rather than being assumed to run to end of file, because a region that is
    too big would suppress a revert that ought to happen.
    """
    if "test" not in text:
        return []
    mask = code_mask(text)
    starts = _line_starts(text)
    n = len(text)
    regions: list[tuple[int, int]] = []

    i = 0
    while True:
        i = text.find("#[", i)
        if i == -1:
            break
        if not mask[i]:
            i += 2
            continue
        close = _match_bracket(text, mask, i + 1, "[", "]")
        if close == -1:
            break
        attr = "".join(text[k] if mask[k] else " " for k in range(i + 2, close - 1))
        if not _attribute_is_test(attr):
            i = close
            continue

        # Walk forward over any further attributes glued to the same item, then
        # take the item itself: a braced block, or a statement ending in `;`
        # (`#[cfg(test)] use super::*;`).
        j = close
        while j < n:
            while j < n and (text[j].isspace() or not mask[j]):
                j += 1
            if j < n and text[j] == "#" and j + 1 < n and text[j + 1] == "[":
                nxt = _match_bracket(text, mask, j + 1, "[", "]")
                if nxt == -1:
                    break
                j = nxt
                continue
            break

        end = -1
        k = j
        while k < n:
            if mask[k] and text[k] == "{":
                end = _match_bracket(text, mask, k, "{", "}")
                break
            if mask[k] and text[k] == ";":
                end = k + 1
                break
            k += 1
        if end == -1:
            i = close
            continue

        start_line = _extend_start_backwards(text, starts, _line_of(i, starts))
        end_line = _line_of(end - 1, starts)
        regions.append((start_line, end_line))
        i = end

    return _merge(regions)


def _merge(regions: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not regions:
        return []
    ordered = sorted(regions)
    out = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = out[-1]
        if start <= last_end + 1:
            out[-1] = (last_start, max(last_end, end))
        else:
            out.append((start, end))
    return out


#: Extension -> region finder. One entry per language whose tests can live
#: inside a source file. Go and Java cannot: ``_test.go`` and ``src/test/java``
#: are enforced by the toolchain, so their tests are always separate files.
REGION_FINDERS = {".rs": rust_test_regions}

#: Which runner language may claim which extensions. This is the guard that
#: stops a Python project that vendors a Rust parser (sqlfluff does exactly
#: that) from having one of its ``.rs`` files reclassified as a pytest target.
LANGUAGE_EXTENSIONS = {"rust": (".rs",)}


def find_regions(path: str, head_text: str) -> list[tuple[int, int]]:
    for suffix, finder in REGION_FINDERS.items():
        if path.endswith(suffix):
            return finder(head_text)
    return []


# --------------------------------------------------------------------------
# hunk classification
# --------------------------------------------------------------------------


def _hunk_span(hunk: Hunk) -> tuple[int, int]:
    """Head-side inclusive line span of a hunk, as a range we can intersect.

    A pure deletion has ``head_len == 0`` and sits *between* two head lines; we
    give it the zero-width span at its insertion point so that it is judged by
    the region it would be reinserted into.
    """
    if hunk.head_len == 0:
        return hunk.head_start, hunk.head_start
    return hunk.head_start, hunk.head_start + hunk.head_len - 1


def _overlap(span: tuple[int, int], regions: Iterable[tuple[int, int]]) -> bool:
    lo, hi = span
    return any(not (hi < rlo or lo > rhi) for rlo, rhi in regions)


def _contained(span: tuple[int, int], regions: Iterable[tuple[int, int]]) -> bool:
    lo, hi = span
    return any(rlo <= lo and hi <= rhi for rlo, rhi in regions)


def classify_hunks(
    hunks: list[Hunk], regions: list[tuple[int, int]]
) -> tuple[list[Hunk], list[tuple[Hunk, str]]]:
    """Split hunks into (revertable, [(kept, why)]) against the test regions."""
    revertable: list[Hunk] = []
    kept: list[tuple[Hunk, str]] = []
    for hunk in hunks:
        span = _hunk_span(hunk)
        if not _overlap(span, regions):
            revertable.append(hunk)
        elif _contained(span, regions):
            kept.append((hunk, "the hunk is entirely inside a #[cfg(test)] region (it is the PR's own test)"))
        else:
            kept.append(
                (
                    hunk,
                    "the hunk straddles the edge of a #[cfg(test)] region, so reverting it "
                    "would have deleted part of the PR's own test",
                )
            )
    return revertable, kept


def revert_outside_regions(
    repo: str, base_sha: str, head_sha: str, path: str, head_text: str,
    regions: list[tuple[int, int]],
) -> tuple[str | None, list[str]]:
    """Head text with every non-test hunk rolled back to base.

    Returns ``(text, notes)``. ``text`` is None when nothing outside the test
    regions could be reverted - there is then no experiment to run on this file
    and the caller must not pretend otherwise.

    Hunks are taken with zero context (``--unified=0``) so that they hug the
    lines that actually changed; three lines of context would routinely drag a
    hunk across the ``#[cfg(test)]`` line and force the conservative
    "straddles, keep at head" branch for changes that are cleanly separable.
    They are applied bottom-up so that each reverse patch sees the head line
    numbers it was computed against.
    """
    hunks = file_hunks(repo, base_sha, head_sha, path, context=0)
    if not hunks:
        return None, [f"{path}: no diff hunks could be read, so it was left at head"]

    revertable, kept = classify_hunks(hunks, regions)
    # These hunk numbers come from a zero-context diff and are deliberately
    # labelled as such: localisation numbers hunks from git's default
    # three-line-context diff, so "hunk 3" can mean two different things in one
    # report unless each says which diff it is counting.
    notes = [
        f"{path} zero-context hunk {h.index} left at head: {why}" for h, why in kept
    ]
    if not revertable:
        notes.append(
            f"{path}: every changed hunk touches the PR's own #[cfg(test)] code, so there was "
            f"nothing in this file to revert"
        )
        return None, notes

    text = head_text
    for hunk in sorted(revertable, key=lambda h: h.head_start, reverse=True):
        try:
            text = apply_reverse(text, hunk)
        except ValueError as exc:
            notes.append(f"{path} hunk {hunk.index} could not be reverted: {exc}")
    if text == head_text:
        return None, notes
    notes.insert(
        0,
        f"{path}: {len(revertable)} non-test hunk(s) reverted to base, "
        f"{len(kept)} test hunk(s) kept at head (zero-context diff)",
    )
    return text, notes


# --------------------------------------------------------------------------
# annotation
# --------------------------------------------------------------------------


def annotate(
    repo: str, base_sha: str, head_sha: str, changed: list[ChangedFile], language: str
) -> list[str]:
    """Mark SOURCE files that also carry test code this PR touched.

    Such a file ends up in *both* halves of the report: it is a source file we
    must revert and a test file we must run. That is not a contradiction - it is
    the honest description of a Rust module with a ``#[cfg(test)]`` block - and
    every downstream step is written to expect it.

    Returns human-readable notes for the report.
    """
    from .hunks import read_head_text

    suffixes = LANGUAGE_EXTENSIONS.get(language, ())
    if not suffixes:
        return []

    notes: list[str] = []
    for f in changed:
        if f.kind is not Kind.SOURCE or f.status == "D":
            continue
        if not f.path.endswith(suffixes):
            continue
        head_text = read_head_text(repo, head_sha, f.path)
        if head_text is None:
            continue
        regions = find_regions(f.path, head_text)
        if not regions:
            continue
        hunks = file_hunks(repo, base_sha, head_sha, f.path, context=0)
        touched = [h for h in hunks if _overlap(_hunk_span(h), regions)]
        if not touched:
            # The file has inline tests but this PR did not touch them. It is
            # plain source: revert it whole, exactly as before.
            continue
        f.inline_test_regions = list(regions)
        f.executable_test = True
        f.reason += (
            f"; also carries {len(regions)} inline #[cfg(test)] region(s) that this PR "
            f"changed, so it is both the source under test and the test itself"
        )
        span = ", ".join(f"lines {a}-{b}" for a, b in regions[:4])
        notes.append(
            f"{f.path} contains the PR's own tests inline ({span}); it will be reverted "
            f"hunk by hunk with those regions left at head"
        )
    return notes
