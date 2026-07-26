"""Turn the PR's changed test files into things a test runner can execute.

Three cases:

* The changed file is itself a runnable test module -> run it directly.
* The changed file is fixture/snapshot/YAML data whose consumer *names* it ->
  find the test module that contains the literal reference.
* The changed file is fixture data consumed by an **auto-discovery harness** -
  a module that globs/walks/lists a fixture *directory* and parametrises over
  whatever it finds. Such a harness never mentions the fixture by name, so the
  literal search cannot see it, and a literal search that matches some *other*
  module with a similar name produces a confident and completely wrong answer.
  This is the sqlfluff #8221 shape: ``test/fixtures/dialects/clickhouse/
  exchange.sql`` is consumed by ``test/dialects/dialects_test.py``, which
  contains the string ``clickhouse`` exactly zero times.

If nothing matches we fall back to the enclosing test directory and say so in
the report, because a silent widening of scope would make the verdict mean
something different from what the user thinks it means.

Nothing in here *proves* a mapping - it only proposes candidates. Proof is
established later, by collection and by the targeted probe in
:mod:`coretexa_verify.verify`.
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

#: Harness discovery is allowed a wider net than the literal search, because
#: its signal is much more specific (an ancestor-directory reference *and* a
#: directory-enumerating construct) and because the result is then narrowed to
#: individual cases by collecting with ``-k <fixture stem>``. sqlfluff's
#: fixtures/dialects root has 16 such consumers; capping at 8 threw all of them
#: away rather than narrowing them.
MAX_HARNESS_CONSUMERS = 24

#: Source constructs that mean "this module builds its cases by enumerating a
#: directory rather than by naming files". Presence of one of these *plus* a
#: reference to an ancestor directory of the fixture is what makes a module an
#: auto-discovery harness candidate.
AUTO_DISCOVERY_MARKERS = (
    ".glob(",
    ".rglob(",
    "glob.glob(",
    "iglob(",
    "globSync",
    "os.walk(",
    "listdir(",
    "scandir(",
    ".iterdir(",
    "readdirSync",
    "import.meta.glob",
    "parametrize",
    "parameterized",
    "describe.each",
    "test.each",
    "it.each",
)

#: Ancestor directories shallower than this many components are too generic to
#: anchor a harness search on ("test/" would match the entire suite).
MIN_ANCHOR_DEPTH = 2


class SelectionError(Exception):
    """No runnable test target could be derived from the PR's test changes."""


def list_executable_tests(
    repo: str, cfg: ClassifierConfig, extensions: tuple | None = None
) -> list[str]:
    """Every committed file that is a runnable test module.

    ``extensions`` is the detected runner's own list of file types it can
    execute, and filtering by it is not cosmetic. sqlfluff is a Python project
    that vendors a Rust crate; without the filter its
    ``sqlfluffrs/tests/fixture_tests.rs`` joins the candidate pool, a literal
    fixture search matches it, and pytest is handed a ``.rs`` file - which
    collects nothing and turns a good verdict into INCONCLUSIVE.
    """
    res = git(repo, "ls-files")
    if res.returncode != 0:
        return []
    return [
        p
        for p in res.stdout.splitlines()
        if p
        and is_executable_test_name(p, cfg)
        and (not extensions or p.endswith(tuple(extensions)))
    ]


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


def ancestor_dirs(path: str) -> list[str]:
    """Every ancestor directory of ``path``, deepest first."""
    parts = path.replace("\\", "/").split("/")[:-1]
    out: list[str] = []
    while parts:
        out.append("/".join(parts))
        parts = parts[:-1]
    return out


#: How a fixture root gets spelled in real test modules. Each entry is a
#: separator placed between quoted path components:
#:
#:   ``os.path.join("test", "fixtures", "dialects")``  -> ``", "``
#:   ``Path(__file__).parent / "test" / "fixtures"``   -> ``" / "``
#:   ``ROOT / "test"/"fixtures"``                      -> ``"/"``
#:
#: Missing the pathlib spelling is what hid sqlfluff's ``rust_parser_test.py``,
#: which declares its root as ``Path(...) / "test" / "fixtures" / "dialects"``.
COMPONENT_SEPARATORS = (", ", " / ", "/")


def directory_reference_forms(directory: str) -> list[str]:
    """Literal spellings a test module might use to name ``directory``.

    Covers the plain path (``test/fixtures/dialects``), the quoted-component
    spellings above in both quote styles, and the trailing two components,
    which is how a module usually refers to a fixture root that lives under a
    repo-specific prefix.
    """
    parts = [p for p in directory.split("/") if p]
    forms = [directory, directory + "/"]
    if len(parts) >= 2:
        for quote in ('"', "'"):
            for sep in COMPONENT_SEPARATORS:
                forms.append(sep.join(f"{quote}{p}{quote}" for p in parts))
        forms.append("/".join(parts[-2:]))
    seen: set[str] = set()
    ordered = []
    for f in forms:
        if f and f not in seen:
            seen.add(f)
            ordered.append(f)
    return ordered


def _file_text(repo: str, rel: str, limit: int = 400_000) -> str:
    try:
        with open(os.path.join(repo, rel), encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def looks_like_harness(text: str) -> bool:
    return any(marker in text for marker in AUTO_DISCOVERY_MARKERS)


def find_harness_consumers(
    repo: str, fixture: str, executable_tests: list[str], cfg: ClassifierConfig
) -> tuple[list[str], str]:
    """Find test modules that auto-discover the fixture's *directory*.

    Returns ``(modules, explanation)``. A module qualifies when it references
    an ancestor directory of the fixture **and** contains a construct that
    enumerates a directory. Deepest ancestor first, so a harness scoped to
    ``fixtures/dialects/clickhouse`` beats one scoped to ``fixtures``.

    ``conftest.py`` is never a runnable target, so a conftest-only match is
    resolved one step further: to the test modules in that conftest's own
    directory tree that import from it.
    """
    exec_set = set(executable_tests)
    for directory in ancestor_dirs(fixture):
        if len(directory.split("/")) < MIN_ANCHOR_DEPTH:
            break
        hits: list[str] = []
        conftests: list[str] = []
        for form in directory_reference_forms(directory):
            res = git(repo, "grep", "--files-with-matches", "--fixed-strings", form)
            if res.returncode not in (0, 1):
                continue
            for path in res.stdout.splitlines():
                if path not in exec_set or path == fixture:
                    continue
                if not looks_like_harness(_file_text(repo, path)):
                    continue
                if posixpath.basename(path) == "conftest.py":
                    if path not in conftests:
                        conftests.append(path)
                elif path not in hits:
                    hits.append(path)
        if hits and len(hits) <= MAX_HARNESS_CONSUMERS:
            return (
                sorted(hits),
                f"test module(s) that enumerate the fixture directory "
                f"'{directory}' and parametrise over what they find",
            )
        if conftests:
            expanded = _modules_using_conftest(repo, conftests, executable_tests)
            if expanded and len(expanded) <= MAX_HARNESS_CONSUMERS:
                return (
                    sorted(expanded),
                    f"test module(s) importing {', '.join(conftests)}, which enumerates "
                    f"the fixture directory '{directory}'",
                )
    return [], ""


def _modules_using_conftest(
    repo: str, conftests: list[str], executable_tests: list[str]
) -> list[str]:
    """Test modules under a harness ``conftest.py`` that import from it."""
    out: list[str] = []
    for conftest in conftests:
        prefix = posixpath.dirname(conftest)
        prefix = prefix + "/" if prefix else ""
        for path in executable_tests:
            if not path.startswith(prefix) or posixpath.basename(path) == "conftest.py":
                continue
            text = _file_text(repo, path)
            if "conftest import" in text or "from conftest" in text or "conftest." in text:
                if path not in out:
                    out.append(path)
    return out


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
    runner: object | None = None,
) -> tuple[list[str], list[SelectionEntry]]:
    """Map changed TEST files to runner targets.

    Returns the de-duplicated target list and a per-file audit trail.
    """
    extensions = getattr(runner, "test_file_extensions", ()) or None
    executable_tests = list_executable_tests(repo, cfg, extensions)
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
            entries.append(
                SelectionEntry(
                    f.path,
                    [f.path],
                    "direct",
                    "runnable test module",
                    proof="the changed file is itself the test module that was run",
                )
            )
            add([f.path])
            continue

        # A language that *guarantees* which tests read a fixture beats any
        # amount of grepping. Go's testdata/ and Maven's test resource root are
        # both toolchain rules, so the runner is asked first and its answer is
        # proof rather than a proposal.
        by_convention = (
            runner.fixture_targets(f.path) if runner is not None else None
        )
        if by_convention is not None:
            conv_targets, conv_detail, conv_proof = by_convention
            entries.append(
                SelectionEntry(
                    f.path, conv_targets, "fixture-convention", conv_detail, proof=conv_proof
                )
            )
            add(conv_targets)
            continue

        consumers, why = find_consumers(repo, f.path, executable_tests, cfg)
        harness, harness_why = find_harness_consumers(repo, f.path, executable_tests, cfg)
        combined = list(consumers)
        for h in harness:
            if h not in combined:
                combined.append(h)
        if combined:
            if consumers and harness:
                method, detail = "fixture-map+harness", f"{why}; plus {harness_why}"
            elif harness:
                method, detail = "fixture-harness", harness_why
            else:
                method, detail = "fixture-map", why
            entries.append(
                SelectionEntry(f.path, combined, method, detail, harness_targets=list(harness))
            )
            add(combined)
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
