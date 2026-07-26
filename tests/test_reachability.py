"""Hunks no detected runner can reach (defect D3).

gatus #1725 produced the headline "19 of 34 behavioural changes", of which 15
were ``.vue``/``.html``/``.json``/frontend-JS hunks. No ``go test`` can execute
a Vue component, so those 15 were never candidates for a gate and putting them
in the denominator made the readable part of the finding - the config and
security wiring - impossible to see.

An unreachable hunk is never reverted, never counted, and always listed.
"""

from coretexa_verify.models import HunkResult, Outcome, Report, Verdict
from coretexa_verify.report import render_markdown, render_text
from coretexa_verify.runners.base import Runner
from coretexa_verify.runners.golang import GoTestRunner
from coretexa_verify.runners.python import PytestRunner
from coretexa_verify.verify import _decide, _unreachable_note


# --------------------------------------------------------------------------
# what a runner claims it can reach
# --------------------------------------------------------------------------


def test_go_cannot_reach_a_vue_component():
    runner = GoTestRunner("/tmp", "r")
    why = runner.unreachable_reason("web/app/src/App.vue")
    assert why
    assert ".go" in why


def test_go_cannot_reach_frontend_html_js_json_or_css():
    runner = GoTestRunner("/tmp", "r")
    for path in (
        "web/static/index.html",
        "web/static/js/app.js",
        "web/static/manifest.json",
        "web/static/css/app.css",
        "web/app/vue.config.js",
    ):
        assert runner.unreachable_reason(path), path


def test_go_reaches_go_files():
    runner = GoTestRunner("/tmp", "r")
    assert runner.unreachable_reason("config/config.go") == ""
    assert runner.unreachable_reason("security/oidc.go") == ""


def test_go_still_reaches_testdata_whatever_the_extension():
    """``go help test``: testdata belongs to the package's tests, by rule."""
    runner = GoTestRunner("/tmp", "r")
    assert runner.unreachable_reason("internal/common/testdata/ssh/user_config.tmpl") == ""
    assert runner.unreachable_reason("pkg/testdata/golden.json") == ""


def test_dependency_manifests_are_unreachable_for_every_runner():
    for runner in (GoTestRunner("/tmp", "r"), PytestRunner("/tmp", "r", ["python", "-m", "pytest"]), Runner("/tmp", "r")):
        for path in ("go.mod", "go.sum", "src/go.mod", "gomod2nix.toml"):
            why = runner.unreachable_reason(path)
            assert why, (runner.id, path)
            assert "dependency manifest" in why


def test_lock_files_are_unreachable_for_every_runner():
    runner = PytestRunner("/tmp", "r", ["python", "-m", "pytest"])
    for path in ("poetry.lock", "yarn.lock", "Cargo.lock", "web/package-lock.json", "flake.lock"):
        assert "dependency manifest" in runner.unreachable_reason(path), path


def test_a_runner_that_makes_no_extension_claim_reaches_everything_else():
    """Python genuinely does execute .sql fixtures and render .html templates."""
    runner = PytestRunner("/tmp", "r", ["python", "-m", "pytest"])
    assert runner.source_file_extensions == ()
    assert runner.unreachable_reason("test/fixtures/dialects/clickhouse/exchange.sql") == ""
    assert runner.unreachable_reason("app/templates/index.html") == ""
    assert runner.unreachable_reason("src/app.py") == ""


# --------------------------------------------------------------------------
# how an unreachable hunk is counted
# --------------------------------------------------------------------------


def _result(path, outcome, unreachable=""):
    return HunkResult(
        path=path,
        index=1,
        header="@@",
        label=f"{path} hunk 1",
        outcome=outcome,
        gated=outcome in (Outcome.ASSERT_FAIL, Outcome.BUILD_ERROR),
        summary="s",
        unreachable_reason=unreachable,
    )


def test_an_unreachable_hunk_has_its_own_status():
    h = _result("web/app/src/App.vue", Outcome.NOT_RUN, unreachable="no go test reaches this")
    assert h.status == "unreachable"
    assert h.evaluable is False
    assert h.reachable is False


def test_an_unreachable_hunk_is_not_an_unknown_one():
    """UNKNOWN means the runner broke; UNREACHABLE means we never asked it."""
    broke = _result("a.go", Outcome.RUNNER_ERROR)
    skipped = _result("a.vue", Outcome.NOT_RUN, unreachable="x")
    assert broke.status == "unknown"
    assert skipped.status == "unreachable"


def test_unreachable_hunks_leave_the_denominator():
    report = Report(verdict=Verdict.INCONCLUSIVE, headline="")
    report.hunk_results = [
        _result("config/config.go", Outcome.PASS),
        _result("security/oidc.go", Outcome.PASS),
        _result("config/ui/ui.go", Outcome.ASSERT_FAIL),
    ] + [
        _result(f"web/app/src/View{i}.vue", Outcome.NOT_RUN, unreachable="no go test reaches this")
        for i in range(15)
    ]
    out = _decide(report)
    assert out.verdict is Verdict.NO_GATE
    assert "2 of 3 evaluated behavioural change(s)" in out.headline
    assert "15 further change(s) are outside the reach of the detected test runner" in out.headline
    assert "frontend assets (.vue)" in out.headline


def test_the_note_names_manifests_and_frontend_separately():
    hunks = [
        _result("web/app/src/App.vue", Outcome.NOT_RUN, unreachable="runner executes .go tests"),
        _result("web/static/index.html", Outcome.NOT_RUN, unreachable="runner executes .go tests"),
        _result("go.mod", Outcome.NOT_RUN, unreachable="dependency manifest/lock file: x"),
        _result("go.sum", Outcome.NOT_RUN, unreachable="dependency manifest/lock file: x"),
    ]
    note = _unreachable_note(hunks)
    assert "4 further change(s)" in note
    assert "frontend assets (.html, .vue)" in note
    assert "dependency manifests (go.mod, go.sum)" in note


def test_no_note_when_everything_was_reachable():
    assert _unreachable_note([]) == ""


def test_a_diff_that_is_entirely_unreachable_falls_back_to_stage_one():
    """No per-hunk claim can be made, so stage 1 speaks and the note is kept."""
    report = Report(verdict=Verdict.INCONCLUSIVE, headline="")
    report.localized = True
    report.reverted_run = __import__(
        "coretexa_verify.models", fromlist=["TestRunResult"]
    ).TestRunResult(command=["x"], outcome=Outcome.ASSERT_FAIL, failed=2)
    report.reverted_files = ["web/app/src/App.vue"]
    report.hunk_results = [
        _result("web/app/src/App.vue", Outcome.NOT_RUN, unreachable="runner executes .go tests")
    ]
    out = _decide(report)
    assert out.localized is False
    assert out.verdict is Verdict.GATE_HOLDS
    assert "1 further change(s) are outside the reach" in out.headline


def test_an_unreachable_hunk_can_never_become_a_no_gate_finding():
    """The whole point: a hunk we never ran must not be reported as ungated."""
    report = Report(verdict=Verdict.INCONCLUSIVE, headline="")
    report.hunk_results = [
        _result("config/config.go", Outcome.ASSERT_FAIL),
        _result("web/app/src/App.vue", Outcome.NOT_RUN, unreachable="runner executes .go tests"),
    ]
    out = _decide(report)
    assert out.verdict is Verdict.GATE_HOLDS
    assert "App.vue" not in out.headline.split("outside the reach")[0]


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def test_the_report_lists_unreachable_hunks_separately():
    report = Report(verdict=Verdict.NO_GATE, headline="h")
    report.hunk_results = [
        _result("web/app/src/App.vue", Outcome.NOT_RUN, unreachable="no go test reaches this")
    ]
    report.unreachable_hunks = ["web/app/src/App.vue hunk 1: no go test reaches this"]
    text = render_text(report)
    assert "hunks outside the reach of the detected test runner" in text
    assert "UNREACHED" in text
    md = render_markdown(report)
    assert "out of the runner's reach" in md
    assert "not run_ (no test this runner executes reaches this file)" in md


def test_the_markdown_ungated_ratio_counts_only_reachable_hunks():
    report = Report(verdict=Verdict.NO_GATE, headline="h")
    report.hunk_results = [
        _result("a.go", Outcome.PASS),
        _result("b.vue", Outcome.NOT_RUN, unreachable="x"),
        _result("c.vue", Outcome.NOT_RUN, unreachable="x"),
    ]
    md = render_markdown(report)
    assert "1 of 1 reachable behavioural change(s) ungated" in md


def test_the_json_keeps_the_new_fields():
    from coretexa_verify.report import to_json
    import json

    report = Report(verdict=Verdict.NO_GATE, headline="h")
    report.hunk_results = [_result("b.vue", Outcome.NOT_RUN, unreachable="x")]
    report.unreachable_hunks = ["b.vue hunk 1: x"]
    report.pre_existing_failures = ["t.py::TestZoxide"]
    data = json.loads(to_json(report))
    assert data["unreachable_hunks"] == ["b.vue hunk 1: x"]
    assert data["pre_existing_failures"] == ["t.py::TestZoxide"]
    assert data["hunk_results"][0]["status"] == "unreachable"
    assert data["hunk_results"][0]["outcome"] == "NOT_RUN"
    assert data["hunk_results"][0]["unreachable_reason"] == "x"
    # additive: everything the previous schema promised is still there
    assert data["hunk_results"][0]["gated"] is False
    assert "inert_hunks" in data and "warnings" in data
