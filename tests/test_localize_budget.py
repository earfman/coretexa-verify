"""Localisation must not be able to cost an unbounded amount of someone's CI.

Stage 1 is two test runs. Stage 2 adds one more per behavioural hunk, and it
fires whenever the whole-file revert fails to build - which in a compiled
language is the common case, not the rare one. Before the budget, the only
ceiling was ``--max-hunks`` at 40, so a large diff could mean 42 full suite
runs on a stranger's runner. On a five-minute suite that is three and a half
hours of billable time to answer one question.

The budget bounds it. The part that matters for soundness is what happens to
the hunks we did not get to: they must be reported as *skipped*, and they must
never land in the denominator of a NO_GATE headline. "We ran out of time" and
"no test could observe this" are different claims.
"""

from __future__ import annotations

from coretexa_verify.models import HunkResult, Outcome


def _hunk(**kw) -> HunkResult:
    base = dict(
        path="mod.py",
        index=1,
        header="@@ -1 +1 @@",
        label="mod.py hunk 1",
        outcome=Outcome.PASS,
        gated=False,
        summary="",
        preview="",
    )
    base.update(kw)
    return HunkResult(**base)


def test_a_budget_skipped_hunk_is_its_own_status() -> None:
    h = _hunk(outcome=Outcome.NOT_RUN, budget_skipped_reason="localisation stopped after 600s")
    assert h.status == "skipped"


def test_skipped_is_not_confused_with_unreachable() -> None:
    """The two must stay distinguishable in the report.

    `unreachable` is a statement about the code: no test this runner executes
    can see the file. `skipped` is a statement about us. Rendering the second
    as the first would tell a maintainer their frontend asset is unobservable
    when in fact we simply stopped early.
    """
    skipped = _hunk(outcome=Outcome.NOT_RUN, budget_skipped_reason="ran out of budget")
    unreachable = _hunk(outcome=Outcome.NOT_RUN, unreachable_reason="the go runner cannot see .vue")

    assert skipped.status == "skipped"
    assert unreachable.status == "unreachable"


def test_skipped_hunks_are_excluded_from_every_count() -> None:
    """The soundness property: a hunk we never ran cannot be called ungated.

    If `reachable` were true for a skipped hunk it would enter the denominator
    of "N of M behavioural changes are ungated", and an unmeasured hunk would
    be quietly counted as measured.
    """
    h = _hunk(outcome=Outcome.NOT_RUN, budget_skipped_reason="ran out of budget")

    assert h.reachable is False
    assert h.evaluable is False
    assert h.status != "ungated"


def test_an_evaluated_hunk_is_unaffected() -> None:
    ungated = _hunk(outcome=Outcome.PASS, gated=False)
    gated = _hunk(outcome=Outcome.ASSERT_FAIL, gated=True)

    assert (ungated.status, ungated.reachable, ungated.evaluable) == ("ungated", True, True)
    assert (gated.status, gated.reachable, gated.evaluable) == ("gated", True, True)


def test_the_budget_has_a_default_and_can_be_switched_off() -> None:
    from coretexa_verify.verify import VerifyOptions

    assert VerifyOptions(repo=".").localize_budget == 600.0
    assert VerifyOptions(repo=".", localize_budget=0).localize_budget == 0


def test_both_renderers_can_label_a_skipped_hunk() -> None:
    """A status with no label crashes the report at the worst possible moment."""
    from coretexa_verify.report import _HUNK_MARK

    for status in ("gated", "ungated", "unknown", "unreachable", "skipped"):
        assert status in _HUNK_MARK, f"text renderer has no label for {status!r}"
