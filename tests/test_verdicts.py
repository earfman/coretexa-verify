"""Verdict logic, report rendering, and the git safety net.

These use a real (tiny) git repository built in a temp directory, so the
revert/restore path is genuinely exercised without touching the network.
"""

import os
import subprocess

import pytest

from coretexa_verify.cli import should_fail
from coretexa_verify.gitops import (
    TreeMutator,
    changed_files,
    is_clean,
    parse_pr_url,
    show_blob,
)
from coretexa_verify.models import ChangedFile, Kind, Outcome, Report, TestRunResult, Verdict
from coretexa_verify.report import render_markdown, render_text, to_json
from coretexa_verify.verify import _decide, _decide_stage1, _head_failure_reason


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = str(tmp_path / "repo")
    os.makedirs(root)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    (tmp_path / "repo" / "mod.py").write_text("def f():\n    return 1\n")
    (tmp_path / "repo" / "gone.py").write_text("OLD = 1\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()

    (tmp_path / "repo" / "mod.py").write_text("def f():\n    return 2\n")
    (tmp_path / "repo" / "added.py").write_text("NEW = 1\n")
    os.remove(str(tmp_path / "repo" / "gone.py"))
    git(root, "add", "-A")
    git(root, "commit", "-qm", "head")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    return root, base, head


def test_changed_files_reports_add_modify_delete(repo):
    root, base, head = repo
    got = {path: status for status, path, _ in changed_files(root, base, head)}
    assert got == {"mod.py": "M", "added.py": "A", "gone.py": "D"}


def test_revert_and_restore_leaves_the_tree_pristine(repo):
    root, base, head = repo
    files = [
        ChangedFile("mod.py", "M", Kind.SOURCE, ""),
        ChangedFile("added.py", "A", Kind.SOURCE, ""),
        ChangedFile("gone.py", "D", Kind.SOURCE, ""),
    ]
    with TreeMutator(root, base) as m:
        m.revert(files)
        assert "return 1" in open(os.path.join(root, "mod.py")).read()
        assert not os.path.exists(os.path.join(root, "added.py"))
        assert os.path.exists(os.path.join(root, "gone.py"))
        assert not is_clean(root)

    assert is_clean(root), "the working tree must be byte-identical afterwards"
    assert "return 2" in open(os.path.join(root, "mod.py")).read()
    assert os.path.exists(os.path.join(root, "added.py"))
    assert not os.path.exists(os.path.join(root, "gone.py"))


def test_restore_happens_even_when_the_body_raises(repo):
    root, base, head = repo
    with pytest.raises(RuntimeError):
        with TreeMutator(root, base) as m:
            m.revert([ChangedFile("mod.py", "M", Kind.SOURCE, "")])
            raise RuntimeError("test runner exploded")
    assert is_clean(root)


def test_revert_does_not_disturb_the_git_index(repo):
    # A `git checkout <base> -- file` would stage the base content and make a
    # later `git checkout -- .` destructive. We must not do that.
    root, base, head = repo
    with TreeMutator(root, base) as m:
        m.revert([ChangedFile("mod.py", "M", Kind.SOURCE, "")])
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"], cwd=root, capture_output=True, text=True
        ).stdout.strip()
        assert staged == ""
    assert is_clean(root)


def test_write_then_restore_round_trips(repo):
    root, base, head = repo
    with TreeMutator(root, base) as m:
        m.write("mod.py", b"def f():\n    return 999\n")
        assert "999" in open(os.path.join(root, "mod.py")).read()
    assert is_clean(root)


def test_show_blob_returns_none_for_a_path_absent_at_that_ref(repo):
    root, base, head = repo
    assert show_blob(root, base, "added.py") is None
    assert show_blob(root, base, "mod.py") == b"def f():\n    return 1\n"


# --- verdicts ---------------------------------------------------------------


def base_report(**kw):
    r = Report(verdict=Verdict.INCONCLUSIVE, headline="")
    r.head_run = TestRunResult(command=["pytest"], outcome=Outcome.PASS, passed=5, total=5)
    r.reverted_files = ["src/a.py"]
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def result(outcome, **kw):
    return TestRunResult(command=["pytest"], outcome=outcome, **kw)


def test_stage1_pass_is_no_gate():
    r = base_report(reverted_run=result(Outcome.PASS, passed=5, total=5))
    assert _decide_stage1(r).verdict is Verdict.NO_GATE
    assert "would pass without the fix" in r.headline


def test_stage1_assert_fail_is_gate_holds():
    r = base_report(reverted_run=result(Outcome.ASSERT_FAIL, failed=6, passed=4, total=10))
    assert _decide_stage1(r).verdict is Verdict.GATE_HOLDS
    assert "6" in r.headline


def test_stage1_timeout_is_inconclusive_and_says_so():
    r = base_report(reverted_run=result(Outcome.TIMEOUT, timeout_s=900))
    assert _decide_stage1(r).verdict is Verdict.INCONCLUSIVE
    assert "900" in r.headline


def test_stage1_no_tests_is_inconclusive():
    r = base_report(reverted_run=result(Outcome.NO_TESTS_RUN))
    assert _decide_stage1(r).verdict is Verdict.INCONCLUSIVE


def hunk(outcome, label="src/a.py hunk 1"):
    from coretexa_verify.models import HunkResult

    return HunkResult(
        path="src/a.py", index=1, header="@@", label=label, outcome=outcome,
        gated=outcome is not Outcome.PASS, summary="s",
    )


def test_any_ungated_behavioural_hunk_is_no_gate():
    r = base_report(hunk_results=[hunk(Outcome.ASSERT_FAIL), hunk(Outcome.PASS, "src/a.py hunk 2")])
    assert _decide(r).verdict is Verdict.NO_GATE
    assert "hunk 2" in r.headline


def test_all_hunks_gated_by_assertions_is_gate_holds():
    r = base_report(hunk_results=[hunk(Outcome.ASSERT_FAIL), hunk(Outcome.BUILD_ERROR)])
    assert _decide(r).verdict is Verdict.GATE_HOLDS


def test_only_build_gated_hunks_is_gate_holds_build():
    r = base_report(hunk_results=[hunk(Outcome.BUILD_ERROR), hunk(Outcome.BUILD_ERROR)])
    assert _decide(r).verdict is Verdict.GATE_HOLDS_BUILD
    assert "No assertion was ever exercised" in r.headline


def test_localisation_with_no_results_falls_back_to_stage_one():
    r = base_report(reverted_run=result(Outcome.BUILD_ERROR, errored=1, total=1), hunk_results=[])
    assert _decide(r).verdict is Verdict.GATE_HOLDS_BUILD
    assert r.localized is False


def test_head_failure_reason_names_the_failing_tests():
    run = result(Outcome.ASSERT_FAIL, failed=2, passed=1, total=3,
                 failing_ids=["t::a", "t::b"])
    reason = _head_failure_reason(run)
    assert "t::a" in reason and "t::b" in reason
    assert "proves nothing" in reason


def test_head_timeout_reason_surfaces_the_timeout():
    assert "300s" in _head_failure_reason(result(Outcome.TIMEOUT, timeout_s=300))


# --- fail-on ----------------------------------------------------------------


@pytest.mark.parametrize(
    "verdict,fail_on,expected",
    [
        (Verdict.NO_GATE, "never", False),
        (Verdict.NO_GATE, "no-gate", True),
        (Verdict.GATE_HOLDS, "no-gate", False),
        (Verdict.INCONCLUSIVE, "no-gate", False),
        (Verdict.INCONCLUSIVE, "no-gate-or-inconclusive", True),
        (Verdict.GATE_HOLDS_BUILD, "not-gate-holds", False),
        (Verdict.NO_NEW_TESTS, "not-gate-holds", True),
    ],
)
def test_fail_on_policy(verdict, fail_on, expected):
    assert should_fail(verdict, fail_on) is expected


# --- rendering --------------------------------------------------------------


def test_renderers_include_the_provenance():
    r = base_report(
        base_sha="a" * 40, head_sha="b" * 40, base_ref="origin/main", head_ref="pr1",
        test_targets=["tests/test_a.py"],
        reverted_run=result(Outcome.PASS, passed=5, total=5),
    )
    _decide_stage1(r)
    text = render_text(r)
    assert "a" * 40 in text and "b" * 40 in text
    assert "pytest" in text
    assert "NO_GATE" in text

    md = render_markdown(r)
    assert "NO_GATE" in md and "aaaaaaaaaaaa" in md
    assert "tests/test_a.py" in md

    data = to_json(r)
    assert '"verdict": "NO_GATE"' in data


def test_json_is_complete_enough_to_rerun_by_hand():
    import json

    r = base_report(reverted_run=result(Outcome.PASS, passed=1, total=1))
    _decide_stage1(r)
    data = json.loads(to_json(r))
    assert data["head_run"]["command_str"].startswith("pytest")
    assert data["refs"]["base_sha"] == ""
    assert "reverted_files" in data


# --- PR url parsing ---------------------------------------------------------


def test_parse_pr_url():
    pr = parse_pr_url("https://github.com/sqlfluff/sqlfluff/pull/8201")
    assert (pr.owner, pr.repo, pr.number) == ("sqlfluff", "sqlfluff", 8201)
    assert pr.refspec == "pull/8201/head:coretexa-pr8201"
    assert pr.clone_url == "https://github.com/sqlfluff/sqlfluff.git"


def test_parse_pr_url_rejects_other_urls():
    from coretexa_verify.gitops import GitError

    with pytest.raises(GitError):
        parse_pr_url("https://github.com/sqlfluff/sqlfluff/issues/8201")


def test_dirty_paths_does_not_eat_the_first_character_of_a_path(repo):
    # Porcelain v1 status is two columns wide, so an unstaged modification
    # reads " M mod.py". Stripping before slicing used to report "od.py".
    root, base, head = repo
    with open(os.path.join(root, "mod.py"), "w") as fh:
        fh.write("def f():\n    return 3\n")
    from coretexa_verify.gitops import dirty_paths

    assert dirty_paths(root) == ["mod.py"]
    git(root, "add", "mod.py")
    assert dirty_paths(root) == ["mod.py"]
