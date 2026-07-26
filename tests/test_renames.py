"""Identifier renames coupled to a behaviour change (defect D1).

A rename hunk and the hunk that consumes the renamed symbol are not
independent. Reverting either alone leaves a dangling identifier, both come
back BUILD_ERROR, and the verdict layer concludes the tests "gate only the
presence of the new code". TwiN/gatus #1719 is that shape, and the ground truth
is the opposite: revert only the behavioural condition, keep the rename, and
``TestParseAndValidateOnlyRemote`` fails as an assertion.
"""

import os
import subprocess

import pytest

from coretexa_verify.hunks import (
    apply_renames,
    apply_reverse,
    apply_reverse_many,
    depends_on_renames,
    line_rename,
    parse_hunks,
    rename_applies_cleanly,
    rename_map,
    split_file_hunks,
)
from coretexa_verify.models import HunkResult, Outcome, Report, Verdict
from coretexa_verify.report import render_markdown, render_text
from coretexa_verify.verify import _decide, _prepare_sub_revert, _HunkPlan


def _hunk(body: str, path: str = "config.go"):
    return parse_hunks(body, path)[0]


# --------------------------------------------------------------------------
# line-level detection
# --------------------------------------------------------------------------


def test_line_rename_reads_a_single_consistent_substitution():
    assert line_rename("\terr = ErrOld\n", "\terr = ErrNew\n") == {"ErrOld": "ErrNew"}


def test_line_rename_refuses_a_structural_change():
    assert line_rename("if a && b {\n", "if a && b && c {\n") is None


def test_line_rename_refuses_an_inconsistent_substitution():
    # One old name cannot become two different new names on the same line.
    assert line_rename("x = Old + Old\n", "x = New + Other\n") is None


def test_line_rename_ignores_string_literal_content():
    """A rename drags its own error message along; that must not defeat it.

    Masking literals is the only reason gatus #1719 is detectable at all: the
    hunk renames the symbol *and* rewrites the message in one go.
    """
    got = line_rename(
        '\tErrOld = errors.New("at least one endpoint")\n',
        '\tErrNew = errors.New("at least one endpoint or remote")\n',
    )
    assert got == {"ErrOld": "ErrNew"}


def test_line_rename_is_empty_when_only_a_literal_changed():
    assert line_rename('x = "a"\n', 'x = "b"\n') == {}


# --------------------------------------------------------------------------
# hunk-level detection
# --------------------------------------------------------------------------

RENAME_HUNK = """@@ -46,5 +46,5 @@
 var (
-\t// ErrNoEndpointOrSuiteInConfig is an error returned when there are none
-\tErrNoEndpointOrSuiteInConfig = errors.New("at least one endpoint or suite")
+\t// ErrNoEndpointOrSuiteOrRemoteInConfig is an error returned when there are none
+\tErrNoEndpointOrSuiteOrRemoteInConfig = errors.New("at least one endpoint or suite or remote")
 )
"""

BEHAVIOUR_HUNK = """@@ -292,4 +292,4 @@
 \t// Check the configuration
-\tif config == nil || (len(config.Endpoints) == 0) {
-\t\terr = ErrNoEndpointOrSuiteInConfig
+\tif config == nil || (len(config.Endpoints) == 0 && config.Remote == nil) {
+\t\terr = ErrNoEndpointOrSuiteOrRemoteInConfig
 \t} else {
"""


def test_rename_map_finds_the_gatus_rename():
    assert rename_map(_hunk(RENAME_HUNK)) == {
        "ErrNoEndpointOrSuiteInConfig": "ErrNoEndpointOrSuiteOrRemoteInConfig"
    }


def test_the_coupled_behaviour_hunk_is_not_a_rename():
    """It mentions the renamed symbol, but it also changes the condition."""
    assert rename_map(_hunk(BEHAVIOUR_HUNK)) is None


def test_a_hunk_that_adds_a_line_is_never_a_rename():
    added = """@@ -1,2 +1,3 @@
 a := Old
+b := Old
 c := 1
"""
    assert rename_map(_hunk(added, "m.go")) is None


def test_a_hunk_with_no_identifier_change_is_not_a_rename():
    literal_only = """@@ -1,1 +1,1 @@
-msg := "hello"
+msg := "goodbye"
"""
    assert rename_map(_hunk(literal_only, "m.go")) is None


def test_two_names_collapsing_onto_one_is_refused():
    collapse = """@@ -1,2 +1,2 @@
-a := Alpha
-b := Beta
+a := Gamma
+b := Gamma
"""
    assert rename_map(_hunk(collapse, "m.go")) is None


def test_a_name_on_both_sides_of_the_map_is_refused():
    """``A -> B`` together with ``B -> C`` is ambiguous; refuse rather than order it."""
    aliased = """@@ -1,2 +1,2 @@
-x := Alpha
-y := Beta
+x := Beta
+y := Gamma
"""
    assert rename_map(_hunk(aliased, "m.go")) is None


# --------------------------------------------------------------------------
# applying the rename to a sibling revert
# --------------------------------------------------------------------------


def test_apply_renames_respects_identifier_boundaries():
    got = apply_renames(["Err = ErrOld\n", "ErrOldish = 1\n"], {"ErrOld": "ErrNew"})
    assert got == ["Err = ErrNew\n", "ErrOldish = 1\n"]


def test_depends_on_renames_selects_only_the_pairs_the_hunk_uses():
    hunk = _hunk(BEHAVIOUR_HUNK)
    deps = depends_on_renames(
        hunk,
        {"ErrNoEndpointOrSuiteInConfig": "ErrNoEndpointOrSuiteOrRemoteInConfig", "Zzz": "Yyy"},
    )
    assert deps == {"ErrNoEndpointOrSuiteInConfig": "ErrNoEndpointOrSuiteOrRemoteInConfig"}


def test_rename_is_not_clean_when_the_new_name_already_means_something():
    clean, why = rename_applies_cleanly(["a := ErrNew\n", "b := ErrOld\n"], {"ErrOld": "ErrNew"})
    assert clean is False
    assert "merge two distinct symbols" in why


def test_rename_is_clean_when_the_new_name_is_absent():
    assert rename_applies_cleanly(["b := ErrOld\n"], {"ErrOld": "ErrNew"})[0] is True


HEAD_TEXT = "".join(
    [
        "package config\n",
        "\n",
        "var (\n",
        "\t// ErrNew doc\n",
        "\tErrNew = errors.New(\"new\")\n",
        ")\n",
        "\n",
        "func f() {\n",
        "\tif a && b {\n",
        "\t\terr = ErrNew\n",
        "\t}\n",
        "}\n",
    ]
)

TWO_HUNK_DIFF = """@@ -4,2 +4,2 @@
-\t// ErrOld doc
-\tErrOld = errors.New("old")
+\t// ErrNew doc
+\tErrNew = errors.New("new")
@@ -9,2 +9,2 @@
-\tif a {
-\t\terr = ErrOld
+\tif a && b {
+\t\terr = ErrNew
"""


def test_apply_reverse_rewrites_the_spliced_base_lines():
    behaviour = parse_hunks(TWO_HUNK_DIFF, "config.go")[1]
    out = apply_reverse(HEAD_TEXT, behaviour, {"ErrOld": "ErrNew"})
    assert "\tif a {\n" in out, "the condition really did go back to base"
    assert "err = ErrNew" in out, "the rename stayed applied, so this still compiles"
    assert "ErrOld" not in out


def test_apply_reverse_without_renames_leaves_the_dangling_identifier():
    """The old behaviour, kept available: this is what BUILD_ERROR looked like."""
    behaviour = parse_hunks(TWO_HUNK_DIFF, "config.go")[1]
    out = apply_reverse(HEAD_TEXT, behaviour)
    assert "err = ErrOld" in out
    assert "ErrNew = errors.New" in out  # declaration still uses the new name


def test_apply_reverse_many_rolls_back_both_hunks_bottom_up():
    hunks = parse_hunks(TWO_HUNK_DIFF, "config.go")
    out = apply_reverse_many(HEAD_TEXT, hunks)
    assert "ErrOld = errors.New" in out
    assert "err = ErrOld" in out
    assert "ErrNew" not in out
    assert "\tif a {\n" in out


# --------------------------------------------------------------------------
# split_file_hunks against a real git repository
# --------------------------------------------------------------------------


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def go_repo(tmp_path):
    """A two-hunk Go file: hunk 1 renames a symbol, hunk 2 uses it and changes."""
    root = str(tmp_path / "repo")
    os.makedirs(root)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    # The filler matters: git's three lines of context would otherwise fuse the
    # rename and the condition into a single hunk, and nothing could separate
    # them. Real files put the var block and the parser far apart, as gatus does
    # (lines 46 and 292).
    filler = "".join(f"// filler {i}\n" for i in range(12))
    base_text = (
        "package config\n\nvar (\n"
        "\t// ErrOld is returned when nothing is configured\n"
        '\tErrOld = errors.New("nothing configured")\n'
        ")\n\n" + filler + "\nfunc parse(c *Config) error {\n"
        "\tvar err error\n"
        "\tif c == nil || len(c.Endpoints) == 0 {\n"
        "\t\terr = ErrOld\n"
        "\t}\n"
        "\treturn err\n}\n"
    )
    path = os.path.join(root, "config.go")
    with open(path, "w") as fh:
        fh.write(base_text)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()

    head_text = (
        base_text.replace("ErrOld", "ErrNew")
        .replace('errors.New("nothing configured")', 'errors.New("nothing configured at all")')
        .replace(
            "if c == nil || len(c.Endpoints) == 0 {",
            "if c == nil || (len(c.Endpoints) == 0 && c.Remote == nil) {",
        )
    )
    with open(path, "w") as fh:
        fh.write(head_text)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "head")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()
    return root, base, head, head_text


def test_split_file_hunks_separates_the_rename_from_the_behaviour(go_repo):
    root, base, head, head_text = go_repo
    split = split_file_hunks(root, base, head, "config.go", head_text)
    assert len(split.rename_only) == 1
    assert split.renames == {"ErrOld": "ErrNew"}
    assert len(split.behavioural) == 1
    assert "c.Remote" in "".join(split.behavioural[0].head_lines)
    # The rename is also reported as inert, with a reason a human can read.
    assert any("identifier rename only" in why for _, why in split.inert)


def test_a_rename_only_hunk_is_never_evaluated_on_its_own(go_repo):
    """It cannot change behaviour, so reverting it would establish nothing."""
    root, base, head, head_text = go_repo
    split = split_file_hunks(root, base, head, "config.go", head_text)
    rename_hunk = split.rename_only[0][0]
    assert rename_hunk not in split.behavioural


def test_prepare_sub_revert_keeps_the_rename_applied(go_repo):
    root, base, head, head_text = go_repo
    split = split_file_hunks(root, base, head, "config.go", head_text)
    report = Report(verdict=Verdict.INCONCLUSIVE, headline="")
    plan = _HunkPlan(
        path="config.go",
        hunk=split.behavioural[0],
        head_text=head_text,
        renames=split.renames,
        rename_hunks=list(split.rename_only),
    )
    text, applied, group = _prepare_sub_revert(plan, report)
    assert applied == {"ErrOld": "ErrNew"}
    assert group == []
    assert "err = ErrNew" in text, "identifier stays consistent"
    assert "if c == nil || len(c.Endpoints) == 0 {" in text, "condition went back to base"
    assert "ErrOld" not in text


def test_prepare_sub_revert_falls_back_to_a_coupled_group(go_repo, monkeypatch):
    """When the rewrite would merge two symbols, revert the rename too."""
    root, base, head, head_text = go_repo
    split = split_file_hunks(root, base, head, "config.go", head_text)
    report = Report(verdict=Verdict.INCONCLUSIVE, headline="")
    plan = _HunkPlan(
        path="config.go",
        hunk=split.behavioural[0],
        head_text=head_text,
        renames=split.renames,
        rename_hunks=list(split.rename_only),
    )
    monkeypatch.setattr(
        "coretexa_verify.hunks.rename_applies_cleanly",
        lambda lines, renames: (False, "contrived clash"),
    )
    text, applied, group = _prepare_sub_revert(plan, report)
    assert applied == {}
    assert group == [split.rename_only[0][0].short_label]
    assert "ErrOld = errors.New" in text, "the rename was rolled back with its dependant"
    assert any("coupled group" in w for w in report.warnings)


# --------------------------------------------------------------------------
# what the verdict says
# --------------------------------------------------------------------------


def _result(label, outcome, **kw):
    return HunkResult(
        path="config.go",
        index=1,
        header="@@",
        label=label,
        outcome=outcome,
        gated=outcome in (Outcome.ASSERT_FAIL, Outcome.BUILD_ERROR),
        summary="s",
        **kw,
    )


def test_an_assertion_gate_under_a_rename_is_reported_as_gate_holds():
    report = Report(verdict=Verdict.INCONCLUSIVE, headline="")
    report.hunk_results = [
        _result("h2", Outcome.ASSERT_FAIL, renames_applied={"ErrOld": "ErrNew"})
    ]
    out = _decide(report)
    assert out.verdict is Verdict.GATE_HOLDS
    assert "1 by an assertion" in out.headline
    assert "ErrOld -> ErrNew" in out.headline


def test_a_coupled_group_never_claims_presence_only():
    """The exact wrong sentence D1 exists to delete."""
    report = Report(verdict=Verdict.INCONCLUSIVE, headline="")
    report.hunk_results = [_result("h2", Outcome.BUILD_ERROR, group=["config.go hunk 1"])]
    out = _decide(report)
    assert out.verdict is Verdict.GATE_HOLDS_BUILD
    assert "presence of the new code" not in out.headline
    assert "coupled group" in out.headline


def test_an_uncoupled_build_gate_still_says_presence_only():
    report = Report(verdict=Verdict.INCONCLUSIVE, headline="")
    report.hunk_results = [_result("h2", Outcome.BUILD_ERROR)]
    out = _decide(report)
    assert out.verdict is Verdict.GATE_HOLDS_BUILD
    assert "presence of the new code" in out.headline


def test_the_report_shows_the_rename_and_the_group():
    report = Report(verdict=Verdict.GATE_HOLDS, headline="h")
    report.hunk_results = [
        _result("h2", Outcome.ASSERT_FAIL, renames_applied={"ErrOld": "ErrNew"}),
        _result("h3", Outcome.BUILD_ERROR, group=["config.go hunk 1"]),
    ]
    text = render_text(report)
    assert "rename kept applied: ErrOld -> ErrNew" in text
    assert "co-reverted with: config.go hunk 1" in text
    md = render_markdown(report)
    assert "ErrOld→ErrNew" in md
    assert "co-reverted with" in md
