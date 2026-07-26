"""Defect 1: a NO_GATE may never rest on a guessed fixture -> test mapping.

The centrepiece is an end-to-end repository that reproduces the shape that
produced a false NO_GATE on sqlfluff #8221:

* ``test/fixtures/cases/widget.yml`` is the fixture the PR adds,
* ``test/suite/all_cases_test.py`` is the real consumer - it *globs* the fixture
  directory and parametrises over whatever it finds, so it never contains the
  string ``widget``,
* ``test/suite/widget_test.py`` is a decoy that *does* contain the string ``widget``
  and passes whether or not the source change is present.

The literal-token search finds only the decoy. Before harness discovery that
produced a confident NO_GATE; after it, the verdict must be right.
"""

import os
import subprocess

import pytest

from coretexa_verify.classify import ClassifierConfig
from coretexa_verify.models import Verdict
from coretexa_verify.selection import (
    ancestor_dirs,
    directory_reference_forms,
    find_consumers,
    find_harness_consumers,
    list_executable_tests,
    looks_like_harness,
    select_targets,
)
from coretexa_verify.verify import UNPROVEN_FIXTURE_REASON, VerifyOptions, verify

HARNESS = '''\
"""Runs every case file in the fixture directory. Never names one."""
import glob
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from src.transform import apply

CASE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "test/fixtures/cases")
CASES = sorted(glob.glob(os.path.join(CASE_DIR, "*.yml")))


def _load(path):
    data = {}
    for line in open(path):
        if ":" in line:
            key, value = line.split(":", 1)
            data[key.strip()] = value.strip()
    return data


@pytest.mark.parametrize("case", CASES, ids=[os.path.basename(c) for c in CASES])
def test_case(case):
    data = _load(case)
    assert apply(data["input"]) == data["expected"]
'''


DECOY = '''\
"""Mentions the literal 'widget' but never reads the widget fixture."""


def test_widget_name_is_a_string():
    assert isinstance("widget", str)


def test_widget_is_not_empty():
    assert "widget"
'''


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


@pytest.fixture
def autodiscovery_repo(tmp_path):
    """A repo whose fixture is consumed only by a globbing harness."""
    root = str(tmp_path / "auto")
    os.makedirs(root)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")

    write(root, "pytest.ini", "[pytest]\n")
    write(root, "src/__init__.py", "")
    # base: apply leaves the value alone.
    write(root, "src/transform.py", "def apply(value):\n    return value\n")
    # a case that is invariant under the PR, so it never confuses the run
    write(root, "test/fixtures/cases/gadget.yml", "input: AB\nexpected: AB\n")
    write(root, "test/suite/all_cases_test.py", HARNESS)
    write(root, "test/suite/widget_test.py", DECOY)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()

    # head: uppercase the value, and add a fixture that proves it.
    write(root, "src/transform.py", "def apply(value):\n    return value.upper()\n")
    write(root, "test/fixtures/cases/widget.yml", "input: cd\nexpected: CD\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "head")
    return root, base


# --------------------------------------------------------------------------
# the pieces
# --------------------------------------------------------------------------


def test_ancestor_dirs_are_deepest_first():
    assert ancestor_dirs("test/fixtures/dialects/clickhouse/exchange.sql") == [
        "test/fixtures/dialects/clickhouse",
        "test/fixtures/dialects",
        "test/fixtures",
        "test",
    ]


def test_directory_reference_forms_cover_os_path_join():
    forms = directory_reference_forms("test/fixtures/dialects")
    assert "test/fixtures/dialects" in forms
    assert '"test", "fixtures", "dialects"' in forms
    assert "'test', 'fixtures', 'dialects'" in forms
    assert "fixtures/dialects" in forms


def test_looks_like_harness_needs_an_enumerating_construct():
    assert looks_like_harness("cases = Path(d).glob('*.yml')")
    assert looks_like_harness("@pytest.mark.parametrize('case', CASES)")
    assert looks_like_harness("for root, _, files in os.walk(FIXTURES):")
    assert not looks_like_harness("def test_one():\n    assert True\n")


def test_literal_search_alone_finds_only_the_decoy(autodiscovery_repo):
    autodiscovery_repo, base_sha = autodiscovery_repo
    cfg = ClassifierConfig()
    tests = list_executable_tests(autodiscovery_repo, cfg)
    consumers, _ = find_consumers(
        autodiscovery_repo, "test/fixtures/cases/widget.yml", tests, cfg
    )
    assert consumers == ["test/suite/widget_test.py"], (
        "this is the defect: the literal token 'widget' matches a test that never "
        "reads the fixture"
    )


def test_harness_discovery_finds_the_real_consumer(autodiscovery_repo):
    autodiscovery_repo, base_sha = autodiscovery_repo
    cfg = ClassifierConfig()
    tests = list_executable_tests(autodiscovery_repo, cfg)
    harness, why = find_harness_consumers(
        autodiscovery_repo, "test/fixtures/cases/widget.yml", tests, cfg
    )
    assert harness == ["test/suite/all_cases_test.py"]
    assert "test/fixtures/cases" in why


def test_selection_unions_the_decoy_and_the_harness(autodiscovery_repo):
    autodiscovery_repo, base_sha = autodiscovery_repo
    cfg = ClassifierConfig()
    from coretexa_verify.models import ChangedFile, Kind

    fixture = ChangedFile("test/fixtures/cases/widget.yml", "A", Kind.TEST, "")
    targets, entries = select_targets(autodiscovery_repo, [fixture], cfg)
    assert set(targets) == {"test/suite/widget_test.py", "test/suite/all_cases_test.py"}
    assert entries[0].method == "fixture-map+harness"
    assert entries[0].harness_targets == ["test/suite/all_cases_test.py"]
    assert not entries[0].proven, "selection proposes; only collection proves"


# --------------------------------------------------------------------------
# end to end
# --------------------------------------------------------------------------


def test_autodiscovered_fixture_reaches_a_true_verdict(autodiscovery_repo):
    """The whole point: this repo must not come back NO_GATE."""
    autodiscovery_repo, base_sha = autodiscovery_repo
    report = verify(
        VerifyOptions(
            repo=autodiscovery_repo,
            base=base_sha,
            head="HEAD",
            install_deps=False,
            timeout=120,
        )
    )
    assert report.verdict is not Verdict.NO_GATE, report.headline
    assert report.verdict is Verdict.GATE_HOLDS, report.headline

    entry = next(e for e in report.selection if e.source_file == "test/fixtures/cases/widget.yml")
    assert entry.proven, "a verdict may not rest on an unproven mapping"
    assert "widget.yml" in entry.proof
    # the decoy must have been narrowed away entirely
    assert all("widget_test.py" not in t for t in entry.targets)


def test_verdict_degrades_rather_than_claiming_no_gate(autodiscovery_repo, monkeypatch):
    """With harness discovery disabled we are back to the guess - and must refuse.

    This is the assertion that pins the *semantics*: given only the decoy, the
    tool is not allowed to answer NO_GATE. It has to notice that reverting the
    fixture alone changes nothing about the selected tests, and say so.
    """
    autodiscovery_repo, base_sha = autodiscovery_repo
    import coretexa_verify.selection as selection

    monkeypatch.setattr(selection, "find_harness_consumers", lambda *a, **k: ([], ""))
    report = verify(
        VerifyOptions(
            repo=autodiscovery_repo,
            base=base_sha,
            head="HEAD",
            install_deps=False,
            timeout=120,
        )
    )
    assert report.verdict is Verdict.INCONCLUSIVE, report.headline
    assert UNPROVEN_FIXTURE_REASON in report.headline
    assert report.probe_run is not None, "the targeted probe must have been run"
    assert "unchanged" in report.probe_note


# --------------------------------------------------------------------------
# D2: the pathlib spelling of a fixture root
# --------------------------------------------------------------------------

PATHLIB_HARNESS = '''\
"""Declares its fixture root the pathlib way, which 1.2.0 could not see."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.transform import apply

_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "test" / "fixtures" / "cases"
CASES = sorted(_FIXTURE_DIR.glob("*.yml"))


def _load(path):
    data = {}
    for line in path.read_text().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            data[key.strip()] = value.strip()
    return data


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_case(case):
    data = _load(case)
    assert apply(data["input"]) == data["expected"]
'''


@pytest.fixture
def pathlib_repo(tmp_path):
    """Same defect shape as ``autodiscovery_repo``, pathlib-spelled root."""
    root = str(tmp_path / "plib")
    os.makedirs(root)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")

    write(root, "pytest.ini", "[pytest]\n")
    write(root, "src/__init__.py", "")
    write(root, "src/transform.py", "def apply(value):\n    return value\n")
    write(root, "test/fixtures/cases/gadget.yml", "input: AB\nexpected: AB\n")
    write(root, "test/suite/all_cases_test.py", PATHLIB_HARNESS)
    write(root, "test/suite/widget_test.py", DECOY)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()

    write(root, "src/transform.py", "def apply(value):\n    return value.upper()\n")
    write(root, "test/fixtures/cases/widget.yml", "input: cd\nexpected: CD\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "head")
    return root, base


def test_directory_reference_forms_cover_the_pathlib_idiom():
    forms = directory_reference_forms("test/fixtures/dialects")
    assert '"test" / "fixtures" / "dialects"' in forms
    assert "'test' / 'fixtures' / 'dialects'" in forms
    assert '"test"/"fixtures"/"dialects"' in forms
    assert "'test'/'fixtures'/'dialects'" in forms


def test_harness_discovery_finds_a_pathlib_spelled_root(pathlib_repo):
    repo, _ = pathlib_repo
    cfg = ClassifierConfig()
    tests = list_executable_tests(repo, cfg)
    harness, why = find_harness_consumers(repo, "test/fixtures/cases/widget.yml", tests, cfg)
    assert harness == ["test/suite/all_cases_test.py"]
    assert "test/fixtures/cases" in why


def test_pathlib_harness_reaches_a_true_verdict(pathlib_repo):
    repo, base_sha = pathlib_repo
    report = verify(
        VerifyOptions(repo=repo, base=base_sha, head="HEAD", install_deps=False, timeout=120)
    )
    assert report.verdict is Verdict.GATE_HOLDS, report.headline
    entry = next(e for e in report.selection if e.source_file == "test/fixtures/cases/widget.yml")
    assert entry.proven


def test_unnarrowed_harness_modules_are_dropped_from_the_selection(autodiscovery_repo):
    """A harness that yields none of the fixture's cases is not worth running.

    It would be a pile of whole modules chosen because they enumerate a
    directory, not because they were shown to read this fixture.
    """
    from coretexa_verify.models import SelectionEntry
    from coretexa_verify.verify import _prune_unnarrowed_harness

    report = __import__("coretexa_verify.models", fromlist=["Report"]).Report(
        Verdict.INCONCLUSIVE, ""
    )
    entry = SelectionEntry(
        "test/fixtures/cases/widget.yml",
        ["test/suite/widget_test.py", "test/suite/all_cases_test.py"],
        "fixture-map+harness",
        "",
        proof="",
        harness_targets=["test/suite/all_cases_test.py"],
    )
    pruned = _prune_unnarrowed_harness(entry, report)
    assert pruned.targets == ["test/suite/widget_test.py"]
    assert pruned.method == "fixture-map"
    assert any("dropped from the selection" in w for w in report.warnings)


def test_a_proven_harness_entry_is_never_pruned():
    from coretexa_verify.models import Report, SelectionEntry
    from coretexa_verify.verify import _prune_unnarrowed_harness

    entry = SelectionEntry(
        "f.yml", ["a.py", "h.py"], "fixture-map+harness", "", proof="named cases",
        harness_targets=["h.py"],
    )
    assert _prune_unnarrowed_harness(entry, Report(Verdict.NO_GATE, "")).targets == ["a.py", "h.py"]
