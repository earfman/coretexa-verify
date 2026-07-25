"""The experiment.

Run the PR's own new/changed tests at head, revert only the SOURCE files to
their base content, run the same tests again, and see whether anything noticed.

The experiment runs in up to two stages.

**Stage 1 - whole-PR revert.** Every changed source file goes back to its base
content. If the tests still pass, the answer is ``NO_GATE`` and we stop. If they
assert-fail, the answer is ``GATE_HOLDS`` and we stop.

**Stage 2 - localisation.** If the whole-PR revert only *broke the build* - the
test module could not import, so no assertion was ever exercised - we have
learned nothing about whether the tests can detect a behaviour change. So we
revert one hunk at a time and look for a change of real code that no test
notices. Hunks that only touch comments, docstrings or formatting are excluded,
because reverting them proves nothing. Stage 2 is what catches the common shape
where a PR adds a helper, tests the helper, and quietly changes something else.

Every verdict returned by this module is established by having actually run
something. When we cannot run the experiment - dirty tree, unknown runner,
nothing selectable, failing preconditions, a crash - the answer is
``INCONCLUSIVE`` with the specific reason, never a guess.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field

from . import gitops, hunks as hunkmod, refine
from .classify import ClassifierConfig
from .models import (
    ChangedFile,
    HunkResult,
    Outcome,
    Report,
    SelectionEntry,
    TestRunResult,
    Verdict,
)
from .runners import DetectionFailed, Runner, detect_runner
from .selection import classify_all, select_targets

__version__ = "1.0.0"

LOCALIZE_AUTO = "auto"
LOCALIZE_ALWAYS = "always"
LOCALIZE_NEVER = "never"


@dataclass
class VerifyOptions:
    repo: str
    base: str | None = None
    head: str = "HEAD"
    timeout: int = 900
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    extra_runner_args: list[str] = field(default_factory=list)
    allow_checkout: bool = True
    max_targets: int = 50
    #: auto = localise only when the whole-PR revert merely broke the build.
    localize: str = LOCALIZE_AUTO
    max_hunks: int = 40
    refine_selection: bool = True


def verify(opts: VerifyOptions) -> Report:
    repo = os.path.abspath(opts.repo)
    report = Report(
        verdict=Verdict.INCONCLUSIVE,
        headline="not run",
        repo=repo,
        tool_version=__version__,
    )

    if not gitops.is_git_repo(repo):
        return _inconclusive(report, f"{repo} is not a git repository")

    # ---- refs -------------------------------------------------------------
    try:
        head_sha = gitops.rev_parse(repo, opts.head)
        base_ref = opts.base or gitops.default_base_ref(repo)
        base_tip = gitops.rev_parse(repo, base_ref)
        merge_base_sha = gitops.merge_base(repo, base_tip, head_sha)
    except gitops.GitError as exc:
        return _inconclusive(report, str(exc))

    report.head_ref = opts.head
    report.head_sha = head_sha
    report.base_ref = base_ref
    report.base_sha = merge_base_sha
    report.merge_base_sha = merge_base_sha
    if base_tip != merge_base_sha:
        report.warnings.append(
            f"base ref {base_ref} is at {base_tip[:12]}; comparing against the merge "
            f"base {merge_base_sha[:12]} so unrelated commits on {base_ref} are excluded"
        )

    # ---- working tree must be clean so we can guarantee restoration -------
    if not gitops.is_clean(repo):
        dirty = gitops.dirty_paths(repo)
        return _inconclusive(
            report,
            "working tree has uncommitted changes, so the source revert could not be "
            f"undone safely: {', '.join(dirty[:5])}"
            + (f" (+{len(dirty) - 5} more)" if len(dirty) > 5 else ""),
        )

    original_ref = gitops.current_ref(repo)
    needs_checkout = gitops.rev_parse(repo, "HEAD") != head_sha
    if needs_checkout and not opts.allow_checkout:
        return _inconclusive(
            report,
            f"the checkout is not at {opts.head} ({head_sha[:12]}) and --no-checkout was given",
        )

    try:
        if needs_checkout:
            res = gitops.git(repo, "checkout", "--quiet", head_sha)
            if res.returncode != 0:
                return _inconclusive(report, f"could not check out {opts.head}: {res.stderr.strip()}")
        return _run_experiment(repo, opts, report, merge_base_sha)
    finally:
        if needs_checkout:
            restore = gitops.git(repo, "checkout", "--quiet", original_ref)
            if restore.returncode != 0:  # pragma: no cover - defensive
                report.tree_restored = False
                report.warnings.append(
                    f"could not return the checkout to {original_ref}: {restore.stderr.strip()}"
                )


# --------------------------------------------------------------------------


def _run_experiment(repo: str, opts: VerifyOptions, report: Report, base_sha: str) -> Report:
    try:
        raw = gitops.changed_files(repo, base_sha, report.head_sha)
    except gitops.GitError as exc:
        return _inconclusive(report, str(exc))
    report.changed_files = classify_all(raw, opts.classifier)

    source = report.source_files
    tests = report.test_files
    if not source and not tests:
        return _inconclusive(report, "the diff contains no source or test files")
    if not source:
        return _inconclusive(
            report, "the PR changes tests but no source files, so there is nothing to revert"
        )
    if not tests:
        report.verdict = Verdict.NO_NEW_TESTS
        report.headline = (
            f"{len(source)} source file(s) changed and no test file was added or modified."
        )
        return report

    try:
        runner: Runner = detect_runner(repo, opts.extra_runner_args)
    except DetectionFailed as exc:
        return _inconclusive(report, str(exc))
    report.runner = runner.info

    targets, entries = select_targets(repo, tests, opts.classifier, runner.default_test_dir())
    if opts.refine_selection:
        targets, entries = _refine(repo, runner, opts, report, base_sha, tests, targets, entries)
    report.selection = entries
    report.test_targets = targets

    if not targets:
        return _inconclusive(
            report, "none of the PR's test changes could be mapped to a runnable test target"
        )
    if len(targets) > opts.max_targets:
        return _inconclusive(
            report,
            f"selection widened to {len(targets)} targets (limit {opts.max_targets}); refusing "
            f"to run a suite that large as a proxy for this PR's tests",
        )
    if any(e.is_fallback for e in entries):
        widened = ", ".join(e.source_file for e in entries if e.is_fallback)
        report.warnings.append(
            f"could not map {widened} to a specific test module; ran the enclosing test "
            f"directory instead, so the result covers more than just this PR's tests"
        )

    with tempfile.TemporaryDirectory(prefix="coretexa-verify-") as report_dir:
        # ---- precondition -------------------------------------------------
        head_run = runner.execute(targets, opts.timeout, report_dir, "head")
        report.head_run = head_run
        if head_run.outcome is not Outcome.PASS:
            return _inconclusive(report, _head_failure_reason(head_run))

        # ---- stage 1: revert every source file ----------------------------
        reverted_run = _timed_revert_all(repo, base_sha, source, runner, targets, opts, report, report_dir)
        if reverted_run is None:
            return _inconclusive(report, "no source file could be reverted to its base content")
        report.reverted_run = reverted_run

        if reverted_run.outcome is Outcome.PASS:
            return _decide_stage1(report)
        if reverted_run.outcome in (Outcome.TIMEOUT, Outcome.RUNNER_ERROR, Outcome.NO_TESTS_RUN):
            return _decide_stage1(report)
        if reverted_run.outcome is Outcome.ASSERT_FAIL and opts.localize != LOCALIZE_ALWAYS:
            return _decide_stage1(report)
        if opts.localize == LOCALIZE_NEVER:
            return _decide_stage1(report)

        # ---- stage 2: localise --------------------------------------------
        _localize(repo, base_sha, source, runner, targets, opts, report, report_dir)

    return _decide(report)


def _timed_revert_all(
    repo, base_sha, source, runner, targets, opts, report, report_dir
) -> TestRunResult | None:
    mutator = gitops.TreeMutator(repo, base_sha)
    try:
        with mutator:
            mutator.revert(source)
            report.reverted_files = list(mutator.reverted)
            if not report.reverted_files:
                return None
            return runner.execute(targets, opts.timeout, report_dir, "reverted")
    finally:
        _check_restored(repo, mutator, report)


def _localize(repo, base_sha, source, runner, targets, opts, report, report_dir) -> None:
    """Revert one behavioural hunk at a time and record what the tests do."""
    report.localized = True
    all_hunks: list[tuple[str, object, str]] = []  # (path, hunk, head_text)
    for f in source:
        if f.status in ("A", "D", "R"):
            # Whole-file add/delete/rename has no meaningful intermediate state.
            continue
        head_text = hunkmod.read_head_text(repo, report.head_sha, f.path)
        if head_text is None:
            continue
        behavioural, inert = hunkmod.behavioural_hunks(repo, base_sha, report.head_sha, f.path, head_text)
        for hunk, why in inert:
            report.inert_hunks.append(f"{hunk.short_label}: {why}")
        for hunk in behavioural:
            all_hunks.append((f.path, hunk, head_text))

    if not all_hunks:
        report.warnings.append(
            "localisation found no behavioural hunks to revert individually "
            "(the diff is comment/docstring-only, or the files were added or renamed wholesale)"
        )
        return
    if len(all_hunks) > opts.max_hunks:
        report.warnings.append(
            f"localisation skipped: {len(all_hunks)} behavioural hunks exceeds the "
            f"--max-hunks limit of {opts.max_hunks}"
        )
        report.localized = False
        return

    for i, (path, hunk, head_text) in enumerate(all_hunks):
        try:
            reverted_text = hunkmod.apply_reverse(head_text, hunk)
        except ValueError as exc:
            report.warnings.append(f"could not revert {hunk.short_label}: {exc}")
            continue
        mutator = gitops.TreeMutator(repo, base_sha)
        try:
            with mutator:
                mutator.write(path, reverted_text.encode("utf-8"))
                run = runner.execute(targets, opts.timeout, report_dir, f"hunk{i}")
        finally:
            _check_restored(repo, mutator, report)
        report.hunk_results.append(
            HunkResult(
                path=path,
                index=hunk.index,
                header=hunk.header,
                label=hunk.short_label,
                outcome=run.outcome,
                gated=run.outcome is not Outcome.PASS,
                summary=run.summary(),
                preview=hunk.preview(),
                failing_ids=(run.failing_ids + run.erroring_ids)[:5],
            )
        )


def _check_restored(repo: str, mutator: gitops.TreeMutator, report: Report) -> None:
    if mutator.errors:
        report.tree_restored = False
        report.warnings.extend(mutator.errors)
        mutator.errors = []
    elif not gitops.is_clean(repo):  # pragma: no cover - defensive
        report.tree_restored = False
        report.warnings.append(
            "the working tree is still dirty after restoration: "
            + ", ".join(gitops.dirty_paths(repo)[:5])
        )


# --------------------------------------------------------------------------
# selection refinement
# --------------------------------------------------------------------------


def _refine(
    repo: str,
    runner: Runner,
    opts: VerifyOptions,
    report: Report,
    base_sha: str,
    tests: list[ChangedFile],
    targets: list[str],
    entries: list[SelectionEntry],
) -> tuple[list[str], list[SelectionEntry]]:
    """Try to narrow each selection entry to the tests the PR actually added."""
    collected = runner.collect(targets, min(opts.timeout, 300))
    if collected is None:
        report.warnings.append(
            f"the {runner.id} runner cannot enumerate tests, so the whole selected test "
            f"file(s) were run rather than only the tests this PR added"
        )
        return targets, entries

    by_file = {f.path: f for f in tests}
    new_targets: list[str] = []
    new_entries: list[SelectionEntry] = []

    for entry in entries:
        f = by_file.get(entry.source_file)
        proposal: list[str] = []
        method = entry.method
        detail = entry.detail

        if f is not None and f.executable_test and entry.targets:
            changed = refine.changed_line_numbers(repo, base_sha, report.head_sha, f.path)
            head_text = hunkmod.read_head_text(repo, report.head_sha, f.path)
            if head_text and f.path.endswith(".py"):
                proposal = refine.python_test_node_ids(head_text, f.path, changed)
                if proposal:
                    method = "direct+changed-tests"
                    detail = f"only the {len(proposal)} test(s) this PR added or modified"
        elif f is not None and not f.executable_test and entry.targets:
            keys = refine.added_fixture_keys(repo, base_sha, report.head_sha, f.path)
            if keys:
                hits = refine.filter_collected_by_keys(collected, keys)
                if hits:
                    proposal = hits
                    method = entry.method + "+added-cases"
                    detail = (
                        f"{entry.detail}; narrowed to the {len(hits)} collected case(s) matching "
                        f"the {len(keys)} fixture key(s) this PR added"
                    )

        if proposal:
            kept = refine.verify_against_collection(proposal, collected)
            if kept:
                new_entries.append(SelectionEntry(entry.source_file, kept, method, detail))
                for t in kept:
                    if t not in new_targets:
                        new_targets.append(t)
                continue
            report.warnings.append(
                f"narrowed selection for {entry.source_file} did not collect; "
                f"fell back to the whole test file"
            )

        new_entries.append(entry)
        for t in entry.targets:
            if t not in new_targets:
                new_targets.append(t)

    return new_targets, new_entries


# --------------------------------------------------------------------------
# verdicts
# --------------------------------------------------------------------------


def _reverted_desc(report: Report) -> str:
    files = report.reverted_files
    return ", ".join(files[:4]) + (f" (+{len(files) - 4} more)" if len(files) > 4 else "")


def _decide_stage1(report: Report) -> Report:
    run = report.reverted_run
    assert run is not None
    reverted = _reverted_desc(report)
    n_tests = report.head_run.passed if report.head_run else 0

    if run.outcome is Outcome.PASS:
        report.verdict = Verdict.NO_GATE
        report.headline = (
            f"All {n_tests} of the PR's selected test(s) still pass with {reverted} reverted to "
            f"base: this PR's tests would pass without the fix."
        )
    elif run.outcome is Outcome.ASSERT_FAIL:
        report.verdict = Verdict.GATE_HOLDS
        report.headline = (
            f"Reverting {reverted} makes {run.failed} of the PR's test(s) fail: "
            f"the tests really do gate the change."
        )
    elif run.outcome is Outcome.BUILD_ERROR:
        report.verdict = Verdict.GATE_HOLDS_BUILD
        report.headline = (
            f"Reverting {reverted} stops {run.errored} test(s) from building/importing rather "
            f"than assert-failing. That is still a real gate, but no assertion was exercised."
        )
    elif run.outcome is Outcome.NO_TESTS_RUN:
        report.verdict = Verdict.INCONCLUSIVE
        report.headline = (
            "With the source reverted the runner collected no tests at all, so nothing was "
            "measured."
        )
    elif run.outcome is Outcome.TIMEOUT:
        report.verdict = Verdict.INCONCLUSIVE
        report.headline = (
            f"The reverted run exceeded the {run.timeout_s}s timeout, so no verdict was established."
        )
    else:
        report.verdict = Verdict.INCONCLUSIVE
        report.headline = f"The reverted run could not be interpreted: {run.summary()}."
    return report


def _decide(report: Report) -> Report:
    """Verdict after localisation."""
    results = report.hunk_results
    if not results:
        # Localisation could not run; fall back to what stage 1 established.
        report.localized = False
        return _decide_stage1(report)

    ungated = [h for h in results if h.outcome is Outcome.PASS]
    assert_gated = [h for h in results if h.outcome is Outcome.ASSERT_FAIL]
    build_gated = [h for h in results if h.outcome is Outcome.BUILD_ERROR]
    broken = [h for h in results if h.outcome in (Outcome.TIMEOUT, Outcome.RUNNER_ERROR, Outcome.NO_TESTS_RUN)]

    n_tests = report.head_run.passed if report.head_run else 0

    if ungated:
        report.verdict = Verdict.NO_GATE
        names = "; ".join(h.label for h in ungated[:3])
        more = f" (+{len(ungated) - 3} more)" if len(ungated) > 3 else ""
        report.headline = (
            f"{len(ungated)} of {len(results)} behavioural change(s) in this PR can be reverted "
            f"with all {n_tests} of its tests still passing: {names}{more}. "
            f"This PR's tests would pass without that change."
        )
    elif assert_gated:
        report.verdict = Verdict.GATE_HOLDS
        report.headline = (
            f"Every one of the {len(results)} behavioural change(s) in this PR is detected by its "
            f"own tests ({len(assert_gated)} by an assertion, {len(build_gated)} at import time)."
        )
    elif build_gated:
        report.verdict = Verdict.GATE_HOLDS_BUILD
        report.headline = (
            f"All {len(build_gated)} behavioural change(s) are detected only because reverting "
            f"them stops the tests building/importing. No assertion was ever exercised, so the "
            f"tests gate the presence of the new code, not its behaviour."
        )
    elif broken:
        report.verdict = Verdict.INCONCLUSIVE
        report.headline = (
            f"Localisation ran but every per-hunk run failed to produce a usable result "
            f"({broken[0].summary})."
        )
    else:  # pragma: no cover - unreachable
        report.verdict = Verdict.INCONCLUSIVE
        report.headline = "localisation produced no interpretable results"
    return report


def _head_failure_reason(run: TestRunResult) -> str:
    if run.outcome is Outcome.TIMEOUT:
        return (
            f"the PR's own tests exceeded the {run.timeout_s}s timeout at head, so the "
            f"precondition for the experiment was never established"
        )
    if run.outcome is Outcome.NO_TESTS_RUN:
        return "the selected targets collected no tests at head"
    if run.outcome is Outcome.RUNNER_ERROR:
        return f"the test runner errored at head: {run.note or 'exit ' + str(run.exit_code)}"
    names = run.failing_ids + run.erroring_ids
    shown = "; ".join(names[:5]) + (f" (+{len(names) - 5} more)" if len(names) > 5 else "")
    kind = "failed" if run.outcome is Outcome.ASSERT_FAIL else "errored"
    return (
        f"the PR's tests do not pass at head ({run.summary()}), so reverting the source "
        f"proves nothing. {kind.capitalize()}: {shown}"
    )


def _inconclusive(report: Report, reason: str) -> Report:
    report.verdict = Verdict.INCONCLUSIVE
    report.headline = reason
    return report
