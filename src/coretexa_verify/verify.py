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

Build-artefact policy
---------------------

Installing the repository's test dependencies generates files: ``*.egg-info/``,
``build/``, ``dist/``, ``__pycache__/``, ``node_modules/``, occasionally a
regenerated but *tracked* ``_version.py``. Those files must never be confused
with the user's own edits or with our mutation. The policy, in four parts:

1. **Order.** The cleanliness gate runs first, on the tree exactly as we found
   it. Only then do we install. Nothing the install generates can therefore
   cause a refusal to start, whether or not the repo gitignores it.
2. **Snapshot, don't guess by name.** We snapshot ``git status`` immediately
   before and after the install. The difference *is* the artefact set - no
   pattern list, no assumption that the repo gitignores anything.
3. **New baseline.** The post-install snapshot becomes the baseline for the
   "did we put everything back?" check after every revert. A file the install
   dirtied can never be reported as our own leftover.
4. **Hands off.** We never back up, revert, clean or delete an artefact. It was
   not ours to create and it is not ours to destroy - the user may have had an
   ``*.egg-info`` before we arrived. We list what appeared and leave it there.
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass, field

from . import deps as depsmod, gitops, hunks as hunkmod, inline_tests, refine
from .classify import ClassifierConfig
from .models import (
    ChangedFile,
    HunkResult,
    Kind,
    Outcome,
    Report,
    SelectionEntry,
    TestRunResult,
    Verdict,
)
from .runners import DetectionFailed, Runner, detect_runner
from .selection import classify_all, select_targets

__version__ = "1.3.0"

#: Reason attached to a verdict that had to be downgraded because a changed
#: fixture could not be tied to a test that reads it. Quoted in the README.
UNPROVEN_FIXTURE_REASON = "changed test fixture could not be provably mapped to a consuming test"

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
    #: Refuse to run when a *widened* selection collects more tests than this.
    #: Bounding by collected count rather than target count is the difference
    #: between refusing in 20s and timing out after 900.
    max_collected: int = 500
    #: auto = localise only when the whole-PR revert merely broke the build.
    localize: str = LOCALIZE_AUTO
    max_hunks: int = 40
    refine_selection: bool = True
    #: Detect and install the repository's own test dependencies before running.
    install_deps: bool = True
    #: Explicit install command; overrides detection entirely when set.
    install_command: str = ""
    install_timeout: int = 600


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

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        report.warnings.append(
            "running as uid 0: a test that asserts a permission error cannot fail for root, so "
            "any such test is incapable of detecting the revert and NO_GATE may be an artefact "
            "of the user this ran as"
        )

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

    # Runner detection moves ahead of the source/test split, because in a
    # compiled language that split can run *through* a file instead of between
    # files - Rust's `#[cfg(test)] mod tests` makes one file both halves of the
    # experiment - and only the runner's language says whether that applies
    # here. The detection *failure* is still raised at exactly the point it
    # always was, so a docs-only PR in an unrecognised repository still answers
    # NO_NEW_TESTS rather than becoming INCONCLUSIVE.
    runner: Runner | None = None
    detection_error = ""
    try:
        runner = detect_runner(repo, opts.extra_runner_args)
    except DetectionFailed as exc:
        detection_error = str(exc)

    if runner is not None:
        _demote_unrunnable_tests(report, runner)
        report.warnings.extend(
            inline_tests.annotate(
                repo, base_sha, report.head_sha, report.changed_files, runner.language
            )
        )

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

    if runner is None:
        return _inconclusive(report, detection_error)
    report.runner = runner.info
    report.warnings.extend(runner.setup_warnings)

    # ---- dependency install ------------------------------------------------
    # Deliberately here: after the cleanliness gate (so generated artefacts can
    # never cause a refusal) and before selection refinement (which shells out
    # to the runner and would otherwise fail for want of the very deps we are
    # about to install).
    baseline = _install_dependencies(repo, opts, report, runner)
    if report.install.failed:
        # We do *not* abandon here. A failed install matters only if it left the
        # tests unable to run, and the precondition run is precisely the check
        # for that - so we let it speak. If the tests do not pass at head, the
        # verdict is INCONCLUSIVE and its headline is this failure with the real
        # stderr attached. If they do pass, the environment was demonstrably
        # already adequate and the verdict rests on runs that actually happened,
        # not on a guess. Bailing out unconditionally would turn every repo with
        # a system build dependency or an offline runner - repos whose workflows
        # install their own deps today and work fine - into INCONCLUSIVE.
        report.warnings.append(
            f"the dependency install failed and the run continued against the environment as "
            f"found: {report.install.commands[0] if report.install.commands else '(no command)'} "
            f"-> {report.install.summary()}"
        )

    try:
        return _soften_deletion_only(
            _run_selected(repo, opts, report, base_sha, runner, source, tests, baseline)
        )
    finally:
        # The bytecode scratch directory is ours; nothing in the user's tree is.
        runner.cleanup()


def _demote_unrunnable_tests(report: Report, runner: Runner) -> None:
    """A changed test file the detected runner cannot execute is not a target.

    A polyglot repository has test files in more than one language. sqlfluff's
    ``sqlfluffrs/tests/fixture_tests.rs`` is a genuine Rust integration test and
    a genuine TEST file, but pytest cannot run it, and handing it over produces
    a collection of zero and an INCONCLUSIVE that says nothing. It stays TEST -
    it is still the PR's evidence and is still never reverted - it just stops
    being something we try to *execute*.
    """
    extensions = tuple(runner.test_file_extensions or ())
    if not extensions:
        return
    for f in report.changed_files:
        if f.executable_test and not f.path.endswith(extensions):
            f.executable_test = False
            f.reason += (
                f"; not runnable by the detected {runner.id} runner, which executes "
                f"{', '.join(extensions)} files"
            )


def _run_selected(repo, opts, report, base_sha, runner, source, tests, baseline) -> Report:
    targets, entries = select_targets(
        repo, tests, opts.classifier, runner.default_test_dir(), runner
    )
    collected: list[str] | None = None
    if opts.refine_selection:
        targets, entries, collected = _refine(
            repo, runner, opts, report, base_sha, tests, targets, entries
        )
    report.selection = entries
    report.test_targets = targets

    if not targets:
        return _inconclusive(
            report, "none of the PR's test changes could be mapped to a runnable test target"
        )
    # --max-targets exists to stop us running a pile of whole files or whole
    # directories. A selection of individual node ids is the opposite of that -
    # it is the narrowest thing we can ask for - so it is bounded by
    # --max-collected instead, which counts tests rather than command arguments.
    whole_paths = [t for t in targets if "::" not in t]
    if len(whole_paths) > opts.max_targets:
        return _inconclusive(
            report,
            f"selection widened to {len(whole_paths)} whole test file/directory targets "
            f"(limit {opts.max_targets}); refusing to run a suite that large as a proxy for "
            f"this PR's tests",
        )
    if len(targets) > opts.max_collected:
        return _inconclusive(
            report,
            f"selection resolved to {len(targets)} individual tests, over the --max-collected "
            f"limit of {opts.max_collected}; refusing to run a suite that large as a proxy for "
            f"this PR's tests",
        )
    if any(e.is_fallback for e in entries):
        widened = ", ".join(e.source_file for e in entries if e.is_fallback)
        report.warnings.append(
            f"could not map {widened} to a specific test module; ran the enclosing test "
            f"directory instead, so the result covers more than just this PR's tests"
        )

    refusal = _collection_cap(repo, runner, opts, report, targets, entries, collected)
    if refusal:
        return _inconclusive(report, refusal)

    # ---- build artefacts (computed on repo-relative paths) ----------------
    repo_targets = list(targets)
    report.build_artifact_risk = runner.artifact_risk(repo_targets, [f.path for f in source])

    # ---- monorepo: run from the package that owns the tests ---------------
    focused = runner.focus(targets)
    if focused is not None:
        targets, why = focused
        rel = os.path.relpath(runner.cwd, repo).replace("\\", "/")
        # A runner may use focus() purely to rewrite file paths into its own
        # unit of work (a Go package, a cargo crate) without moving anywhere.
        # Reporting "." as the workspace package would be noise.
        if rel not in ("", "."):
            report.workspace_package = rel
        report.test_targets = targets
        report.warnings.append(why)

    _prepare_build(repo, opts, report, runner)

    mutated_build = False
    try:
        with tempfile.TemporaryDirectory(prefix="coretexa-verify-") as report_dir:
            # ---- precondition ---------------------------------------------
            head_run = runner.execute(targets, opts.timeout, report_dir, "head")
            report.head_run = head_run
            if head_run.outcome is not Outcome.PASS:
                return _inconclusive(report, _head_failure_reason(head_run, report.install))
            if head_run.executed == 0:
                return _inconclusive(report, _all_skipped_reason(head_run))

            # ---- stage 1: revert every source file ------------------------
            mutated_build = runner.build_step is not None
            reverted_run = _timed_revert_all(
                repo, base_sha, source, runner, targets, opts, report, report_dir, baseline
            )
            if reverted_run is None:
                return _inconclusive(report, "no source file could be reverted to its base content")
            report.reverted_run = reverted_run

            stage2 = (
                reverted_run.outcome is Outcome.BUILD_ERROR
                or (reverted_run.outcome is Outcome.ASSERT_FAIL and opts.localize == LOCALIZE_ALWAYS)
            ) and opts.localize != LOCALIZE_NEVER
            if stage2:
                _localize(
                    repo, base_sha, source, runner, targets, opts, report, report_dir, baseline
                )
                report = _decide(report)
            else:
                report = _decide_stage1(report)

            return _enforce_soundness(
                repo, opts, report, base_sha, runner, tests, targets, report_dir, baseline
            )
    finally:
        if mutated_build:
            _rebuild_at_head(report, runner)


def _collection_cap(
    repo: str,
    runner: Runner,
    opts: VerifyOptions,
    report: Report,
    targets: list[str],
    entries: list[SelectionEntry],
    collected: list[str] | None,
) -> str:
    """Refuse a *widened* selection by how many tests it collects, not how many
    paths it names.

    A single bare directory target is one "target" and six thousand tests. The
    old target-count limit waved it through and the run then died on the 900s
    timeout with nothing to show; counting what the runner actually collected
    costs one ``--collect-only`` and refuses in seconds.
    """
    widened_by = [e.source_file for e in entries if e.is_fallback]
    # An unnarrowed harness selection is a widening too: it is a pile of whole
    # test modules chosen because they enumerate a directory, not because they
    # were shown to read this fixture.
    widened_by += [
        e.source_file for e in entries
        if e.harness_targets and not e.proven and e.source_file not in widened_by
    ]
    bare_dirs = [
        t for t in targets
        if "::" not in t and os.path.isdir(os.path.join(repo, t))
    ]
    if not widened_by and not bare_dirs:
        return ""

    counted = collected
    if counted is None:
        counted = runner.collect(targets, min(opts.timeout, 300))
    if counted is None:
        report.warnings.append(
            "the selection was widened to a whole directory and this runner cannot enumerate "
            "tests, so the collected-test cap could not be applied"
        )
        return ""
    if len(counted) <= opts.max_collected:
        return ""
    cause = (
        f"could not map {', '.join(widened_by)} to a specific test module"
        if widened_by
        else f"the selection includes the bare directory target(s) {', '.join(bare_dirs)}"
    )
    return (
        f"{cause}, and the widened selection collects {len(counted)} tests, over the "
        f"--max-collected limit of {opts.max_collected}. Running a suite that size as a proxy "
        f"for this PR's tests would measure the suite, not the PR."
    )


def _prepare_build(repo: str, opts: VerifyOptions, report: Report, runner: Runner) -> None:
    """Ask the runner for the repo's build step and hand it back to it.

    From here on the runner re-runs it before *every* test run, so a reverted
    source file is always reflected in whatever the tests import.

    This asks the *runner* rather than switching on a language name, so that
    "no build step" is always a claim the runner made about its own toolchain
    and can be read next to that toolchain's reasons. The three compiled
    languages all return None, and all three say why in their module
    docstrings: ``go test`` and ``cargo test`` compile from source on every
    invocation, and ``mvn test``/``gradle test`` already depend on their own
    compile task. None of them can serve a test from an artefact that predates
    the mutation, which is the only thing this machinery exists to prevent.
    """
    step = runner.detect_build_step(min(opts.timeout, 900))
    if step is None:
        return
    runner.build_step = step
    report.build = step.info()
    runner.build_info = report.build


def _rebuild_at_head(report: Report, runner: Runner) -> None:
    """Leave the build output matching the restored head source, not a revert."""
    info = runner.run_build()
    if info is not None and info.status != "ok":
        report.warnings.append(
            "the final rebuild at head failed, so the repository's build output may still "
            "reflect the reverted source: " + (info.note or info.status)
        )


def _repo_relative(repo: str, cwd: str, targets: list[str]) -> list[str]:
    """Map package-relative targets back to repo-relative paths."""
    prefix = os.path.relpath(cwd, repo).replace("\\", "/")
    if prefix in ("", "."):
        return list(targets)
    return [f"{prefix}/{t}" for t in targets]


def _timed_revert_all(
    repo, base_sha, source, runner, targets, opts, report, report_dir, baseline
) -> TestRunResult | None:
    mutator = gitops.TreeMutator(repo, base_sha)
    try:
        with mutator:
            _revert_source(repo, base_sha, report.head_sha, source, mutator, report)
            report.reverted_files = list(mutator.reverted)
            if not report.reverted_files:
                return None
            return runner.execute(targets, opts.timeout, report_dir, "reverted")
    finally:
        _check_restored(repo, mutator, report, baseline)


def _revert_source(repo, base_sha, head_sha, source, mutator, report) -> None:
    """Put every source file back to base - except the PR's own inline tests.

    A file with no inline test regions is reverted wholesale, exactly as it
    always was. A file that carries a ``#[cfg(test)]`` block the PR touched is
    reverted *hunk by hunk*, with the hunks inside those regions left at head,
    so what runs is the base implementation carrying the PR's new tests. That
    is the only state in which the experiment means anything for a language
    whose tests live inside the file they test.
    """
    plain = [f for f in source if not f.has_inline_tests]
    mutator.revert(plain)
    for f in source:
        if not f.has_inline_tests:
            continue
        head_text = hunkmod.read_head_text(repo, head_sha, f.path)
        if head_text is None:  # pragma: no cover - defensive
            continue
        text, notes = inline_tests.revert_outside_regions(
            repo, base_sha, head_sha, f.path, head_text, f.inline_test_regions
        )
        report.warnings.extend(notes)
        if text is None:
            continue
        mutator.write(f.path, text.encode("utf-8"))
        mutator.reverted.append(f.path)


def _localize(repo, base_sha, source, runner, targets, opts, report, report_dir, baseline) -> None:
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
        if f.has_inline_tests:
            # Localisation must not revert the PR's own tests any more than the
            # whole-file revert may. A hunk inside a #[cfg(test)] region is the
            # evidence, not the change under test.
            behavioural, held = inline_tests.classify_hunks(behavioural, f.inline_test_regions)
            for hunk, why in held:
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
            _check_restored(repo, mutator, report, baseline)
        report.hunk_results.append(
            HunkResult(
                path=path,
                index=hunk.index,
                header=hunk.header,
                label=hunk.short_label,
                outcome=run.outcome,
                # A runner usage error, a timeout or an empty collection means
                # the experiment did not happen. Calling that "gated" lets a
                # broken command stand in for a test that noticed something.
                gated=run.outcome in (Outcome.ASSERT_FAIL, Outcome.BUILD_ERROR),
                summary=run.summary(),
                preview=hunk.preview(),
                failing_ids=(run.failing_ids + run.erroring_ids)[:5],
            )
        )


def _check_restored(
    repo: str, mutator: gitops.TreeMutator, report: Report, baseline: gitops.TreeState
) -> None:
    """Did we put back everything *we* changed?

    ``baseline`` is the post-install snapshot, so a tracked file the dependency
    install rewrote is not counted against us: it was never ours, and reverting
    it would be us editing the user's environment, not restoring it.
    """
    if mutator.errors:
        report.tree_restored = False
        report.warnings.extend(mutator.errors)
        mutator.errors = []
        return
    leftover = sorted(set(gitops.dirty_paths(repo)) - baseline.tracked_dirty)
    if leftover:  # pragma: no cover - defensive
        report.tree_restored = False
        report.warnings.append(
            "the working tree is still dirty after restoration: " + ", ".join(leftover[:5])
        )


# --------------------------------------------------------------------------
# soundness: a NO_GATE may only rest on a proven mapping
# --------------------------------------------------------------------------


def _all_skipped_reason(run: TestRunResult) -> str:
    """The precondition nobody thought to check: did anything actually run?

    pytest (and vitest) exit 0 when every selected test skips, and the JUnit
    report then says ``0 passed, N skipped``. Reverting the source and getting
    the same ``0 passed, N skipped`` back is not evidence that the tests do not
    gate the change - it is evidence that no test ran at all. sqlfluff #8225 was
    exactly this shape: head and reverted both "12 passed, 52 skipped" because
    the Rust parser the skipped tests need was never built.
    """
    return (
        f"all {run.skipped} selected test(s) were skipped - nothing executed, so reverting "
        f"the source could not have been detected by anything. A skip is not a passing test: "
        f"the usual causes are a missing optional dependency, a platform guard, an absent "
        f"service, or a build artefact the suite needs and does not have."
    )


def _skip_note(report: Report) -> str:
    """A sentence naming the skipped tests, for any headline that has some."""
    run = report.head_run
    if run is None or not run.skipped:
        return ""
    return (
        f" {run.skipped} of the {run.skipped + run.executed} selected test(s) were skipped and "
        f"never executed; the verdict rests only on the {run.executed} that ran."
    )


def _unproven_fixture_entries(report: Report) -> list[SelectionEntry]:
    return [e for e in report.selection if e.targets and not e.proven]


def _enforce_soundness(
    repo, opts, report, base_sha, runner, tests, targets, report_dir, baseline
) -> Report:
    """Gate the *negative* verdict on evidence, and soften a deletion-only one.

    ``GATE_HOLDS`` needs no extra proof: the tests demonstrably reacted to the
    source revert, which is itself the proof that they exercise it. ``NO_GATE``
    is the claim that nothing noticed, and that claim is only worth anything if
    we know the tests we ran are the tests that read the changed files. Two ways
    that can be false, and both are checked here:

    * the changed file was a fixture and we merely *guessed* which module reads
      it (sqlfluff #8221: guessed ``clickhouse_test.py``, real consumer
      ``dialects_test.py``, 11 happily passing tests either way), and
    * the tests read build output that no longer matches the reverted source.

    A third check applies to *both* signs of verdict: neither ``NO_GATE`` nor
    ``GATE_HOLDS`` may be claimed from a run in which nothing executed.
    """
    if report.verdict in (Verdict.NO_GATE, Verdict.GATE_HOLDS, Verdict.GATE_HOLDS_BUILD):
        head = report.head_run
        if head is not None and head.executed == 0:
            return _inconclusive(report, _all_skipped_reason(head))
        report.headline += _skip_note(report)

    if report.verdict is not Verdict.NO_GATE:
        return report

    if report.build_artifact_risk and runner.build_step is None:
        return _inconclusive(
            report,
            f"the selected tests execute build output and no build step was detected, so the "
            f"source revert may never have reached them: {report.build_artifact_risk}. A "
            f"NO_GATE verdict here would be an artefact of a stale build, not a finding.",
        )

    unproven = _unproven_fixture_entries(report)
    if unproven:
        by_path = {f.path: f for f in tests}
        fixtures = [by_path[e.source_file] for e in unproven if e.source_file in by_path]
        probe = _probe_fixture_mapping(
            repo, base_sha, fixtures, runner, targets, opts, report, report_dir, baseline
        )
        names = ", ".join(e.source_file for e in unproven)
        if probe is None:
            return _inconclusive(
                report,
                f"{UNPROVEN_FIXTURE_REASON}: {names}. The fixture could not be reverted on its "
                f"own, so no evidence of a consumer could be gathered.",
            )
        report.probe_run = probe
        if _runs_differ(report.head_run, probe):
            report.probe_note = (
                f"targeted probe: reverting {names} alone (source left at head) changed the "
                f"selected tests' result from '{report.head_run.summary()}' to "
                f"'{probe.summary()}', which proves the selection really does read the fixture"
            )
            for entry in unproven:
                entry.proof = "targeted probe: reverting the fixture alone changed the outcome"
            return report
        report.probe_note = (
            f"targeted probe: reverting {names} alone (source left at head) left every selected "
            f"test's result unchanged ({probe.summary()}), so the selection does not read it"
        )
        return _inconclusive(
            report,
            f"{UNPROVEN_FIXTURE_REASON}: {names}. The tests we selected "
            f"({', '.join(targets[:3])}{'...' if len(targets) > 3 else ''}) behave identically "
            f"with that fixture reverted, so they cannot be the tests it gates and no NO_GATE "
            f"conclusion can rest on them.",
        )
    return report


def _probe_fixture_mapping(
    repo, base_sha, fixtures, runner, targets, opts, report, report_dir, baseline
) -> TestRunResult | None:
    """Run the selection with only the fixture reverted, source untouched."""
    if not fixtures:
        return None
    mutator = gitops.TreeMutator(repo, base_sha)
    try:
        with mutator:
            mutator.revert(fixtures, kinds=(Kind.TEST,))
            if not mutator.reverted:
                return None
            return runner.execute(targets, opts.timeout, report_dir, "fixture-probe")
    finally:
        _check_restored(repo, mutator, report, baseline)


def _runs_differ(a: TestRunResult | None, b: TestRunResult | None) -> bool:
    """Did anything at all about the run change? Counts and names, not just pass/fail."""
    if a is None or b is None:
        return True
    if a.outcome is not b.outcome:
        return True
    if (a.passed, a.failed, a.errored, a.skipped) != (b.passed, b.failed, b.errored, b.skipped):
        return True
    return set(a.failing_ids + a.erroring_ids) != set(b.failing_ids + b.erroring_ids)


def _soften_deletion_only(report: Report) -> Report:
    """A PR that only deletes code cannot be gated by a test it did not add."""
    source = report.source_files
    if not source or report.verdict is not Verdict.NO_GATE:
        return report
    if not all(f.status == "D" for f in source):
        return report
    report.headline = (
        "this PR only removes code; NO_GATE is expected if the removed behaviour had no other "
        "coverage. " + report.headline
    )
    return report


# --------------------------------------------------------------------------
# dependency install
# --------------------------------------------------------------------------


def _install_dependencies(
    repo: str, opts: VerifyOptions, report: Report, runner: Runner
) -> gitops.TreeState:
    """Detect + run the repo's own dependency install. Returns the new baseline.

    The returned :class:`~coretexa_verify.gitops.TreeState` is the snapshot taken
    *after* the install, and is what every later restoration check is measured
    against. See the artefact policy in this module's docstring.
    """
    rep = depsmod.InstallReport(enabled=opts.install_deps, timeout_s=opts.install_timeout)
    report.install = rep
    before = gitops.TreeState.capture(repo)

    if not opts.install_deps:
        rep.source = "disabled"
        rep.status = "disabled"
        rep.evidence = "install-deps is off, so the environment was used exactly as found"
        return before

    if opts.install_command.strip():
        commands = depsmod.parse_override(opts.install_command)
        plans = [
            depsmod.InstallPlan(
                detector="override",
                evidence=(
                    "an explicit install-command was supplied, so detection was skipped entirely"
                ),
                commands=commands,
                language=runner.language,
            )
        ]
        source = "override"
    else:
        plans, note = depsmod.detect_install_chain(
            repo, runner.language, _python_executable(runner)
        )
        source = "detected"
        if not plans:
            rep.source = "none"
            rep.status = "none"
            rep.evidence = note
            report.warnings.append(f"no dependency install was detected: {note}")
            return before

    rep_run = depsmod.run_plans(repo, plans, opts.install_timeout)
    rep_run.source = source
    rep_run.enabled = True
    report.install = rep = rep_run
    if len(rep.attempts) > 1:
        report.warnings.append(
            "the first detected installer failed and the run fell back down the detection "
            "table: "
            + "; ".join(
                f"{a['detector']} (`{(a['commands'] or [''])[0]}`) -> {a['status']}"
                for a in rep.attempts
            )
        )

    after = gitops.TreeState.capture(repo)
    rep.artefacts = after.new_untracked_since(before)
    rep.dirtied_tracked = after.new_tracked_since(before)
    if rep.dirtied_tracked:
        rep.notes.append(
            "the dependency install modified tracked file(s) "
            + ", ".join(rep.dirtied_tracked[:5])
            + "; they were left exactly as the install left them and excluded from the "
            "restoration check, so they can never be mistaken for this tool's own mutation"
        )
        report.warnings.append(rep.notes[-1])
    if rep.artefacts:
        rep.notes.append(
            "the dependency install created untracked path(s) "
            + ", ".join(rep.artefacts[:5])
            + "; build artefacts are never reverted or deleted by this tool"
        )
    return after


def _python_executable(runner: Runner) -> str:
    """The interpreter the runner will actually use.

    Installing with one interpreter and testing with another is the single most
    confusing way this feature could fail, so we take the launcher's own python
    whenever the runner exposes one.
    """
    launcher = getattr(runner, "launcher", None)
    if isinstance(launcher, list) and launcher:
        head = os.path.basename(launcher[0])
        if head.startswith("python") or head in ("python", "python3"):
            return launcher[0]
    return sys.executable


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
) -> tuple[list[str], list[SelectionEntry], list[str] | None]:
    """Narrow each selection entry to the tests the PR actually added, and -
    the part that matters - record *why* we believe each entry's targets are
    the tests that read the changed file.

    Evidence, strongest first:

    1. the collected node ids literally contain the fixture's own file name or
       stem (an auto-discovery harness parametrises on the file name, so this
       is what proves ``dialects_test.py`` reads ``exchange.sql``);
    2. the collected node ids contain a case key the PR *added* to a modified
       fixture (this is the sqlfluff #8201 path and it stays exactly as it was);
    3. nothing - the entry stays unproven and a NO_GATE built on it will be
       downgraded later by :func:`_enforce_soundness`.
    """
    collected = runner.collect(targets, min(opts.timeout, 300))
    if collected is None:
        report.warnings.append(
            f"the {runner.id} runner could not enumerate tests for this selection, so "
            f"refinement rests on what can be proved from the diff alone"
        )

    by_file = {f.path: f for f in tests}
    new_targets: list[str] = []
    new_entries: list[SelectionEntry] = []

    for entry in entries:
        f = by_file.get(entry.source_file)
        proposal: list[str] = []
        method = entry.method
        detail = entry.detail
        proof = entry.proof

        # Ids proposed for a fixture entry are read straight out of a real
        # collection, so they need no second validation pass; ids derived from
        # a test file's AST do.
        validate = True
        if f is not None and f.executable_test and entry.targets:
            # A runner that can narrow straight from the diff gets first refusal.
            # Its proposal needs no collection to validate it: the names are read
            # out of the head file's own test declarations, so they cannot be
            # invented, and a name that somehow does not exist makes the runner
            # select nothing - which surfaces as NO_TESTS_RUN and INCONCLUSIVE,
            # never as a silent pass.
            narrowed = runner.narrow_from_diff(
                repo, base_sha, report.head_sha, f.path, entry.targets
            )
            if narrowed is not None:
                proposal, detail, proof = narrowed
                method = entry.method + "+changed-tests"
                validate = False
            else:
                changed = refine.changed_line_numbers(repo, base_sha, report.head_sha, f.path)
                head_text = hunkmod.read_head_text(repo, report.head_sha, f.path)
                if head_text and f.path.endswith(".py"):
                    proposal = refine.python_test_node_ids(head_text, f.path, changed)
                    if proposal:
                        method = "direct+changed-tests"
                        detail = f"only the {len(proposal)} test(s) this PR added or modified"
        elif f is not None and not f.executable_test and entry.targets and collected is not None:
            proposal, method, detail, proof = _prove_fixture_entry(
                repo, runner, opts, report, base_sha, f, entry, collected
            )
            validate = False

        if proposal:
            kept = (
                refine.verify_against_collection(proposal, collected)
                if validate and collected is not None
                else proposal
            )
            if kept:
                new_entries.append(
                    SelectionEntry(
                        entry.source_file, kept, method, detail, proof, entry.harness_targets
                    )
                )
                for t in kept:
                    if t not in new_targets:
                        new_targets.append(t)
                continue
            report.warnings.append(
                f"narrowed selection for {entry.source_file} did not collect; "
                f"fell back to the whole test file"
            )

        # A harness module earns its place in the selection by yielding the
        # fixture's own cases. If narrowing found none, running the whole
        # harness is a large, unproven widening - so drop it back out whenever
        # a literal consumer remains to run instead.
        entry = _prune_unnarrowed_harness(entry, report)
        new_entries.append(entry)
        for t in entry.targets:
            if t not in new_targets:
                new_targets.append(t)

    return new_targets, new_entries, collected


def _prune_unnarrowed_harness(entry: SelectionEntry, report: Report) -> SelectionEntry:
    if not entry.harness_targets or entry.proven:
        return entry
    remaining = [t for t in entry.targets if t not in set(entry.harness_targets)]
    if not remaining:
        return entry
    report.warnings.append(
        f"{len(entry.harness_targets)} auto-discovery harness module(s) were dropped from the "
        f"selection for {entry.source_file}: collecting them with the fixture's name found no "
        f"case that reads it, so running them whole would widen the run without proving anything"
    )
    return SelectionEntry(
        entry.source_file,
        remaining,
        entry.method.replace("+harness", ""),
        entry.detail,
        entry.proof,
        [],
    )


def _prove_fixture_entry(
    repo, runner, opts, report, base_sha, f: ChangedFile, entry: SelectionEntry, collected
) -> tuple[list[str], str, str, str]:
    """Return ``(targets, method, detail, proof)`` for one changed fixture."""
    stem = refine.fixture_stem(f.path)

    # A harness never names the fixture, so its cases may not be in `collected`
    # if the harness module was only just added to the selection. Ask the runner
    # for them directly with -k <stem>, which is cheap and exact.
    pool = list(collected)
    if entry.harness_targets and stem.replace("_", "").isalnum():
        extra = runner.collect(entry.harness_targets, min(opts.timeout, 300), ["-k", stem])
        for nid in extra or []:
            if nid not in pool:
                pool.append(nid)

    stem_hits, how = refine.filter_collected_by_stem(pool, f.path)

    # Added-case keys are evidence only for a *modified* fixture. For a file the
    # PR created wholesale every key is "added", including structural ones like
    # `file:`, and matching on those selects tests at random.
    keys = (
        refine.added_fixture_keys(repo, base_sha, report.head_sha, f.path)
        if f.status == "M"
        else []
    )
    key_hits = refine.filter_collected_by_keys(pool, keys) if keys else []

    if stem_hits and key_hits:
        both = [nid for nid in stem_hits if nid in set(key_hits)]
        if both:
            return (
                both,
                entry.method + "+added-cases",
                f"{entry.detail}; narrowed to the {len(both)} collected case(s) that name the "
                f"fixture and match the {len(keys)} case key(s) this PR added",
                f"{how} and a case key this PR added",
            )
    if stem_hits:
        return (
            stem_hits,
            entry.method + "+named-cases",
            f"{entry.detail}; narrowed to the {len(stem_hits)} collected case(s) that name "
            f"the fixture",
            how,
        )
    if key_hits:
        return (
            key_hits,
            entry.method + "+added-cases",
            f"{entry.detail}; narrowed to the {len(key_hits)} collected case(s) matching the "
            f"{len(keys)} fixture key(s) this PR added",
            f"the collected test id(s) contain case key(s) this PR added to the fixture",
        )
    return [], entry.method, entry.detail, ""


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

    ungated = [h for h in results if h.status == "ungated"]
    assert_gated = [h for h in results if h.outcome is Outcome.ASSERT_FAIL]
    build_gated = [h for h in results if h.outcome is Outcome.BUILD_ERROR]
    # Not "gated by something we could not name" - not evaluated at all. These
    # are excluded from every count below, so no claim of detection can rest on
    # a hunk whose reverted run merely broke the runner.
    broken = [h for h in results if not h.evaluable]
    evaluated = [h for h in results if h.evaluable]
    unknown_note = (
        f" {len(broken)} further change(s) could not be evaluated because reverting them made "
        f"the runner itself fail ({broken[0].summary}); they are listed in the report and are "
        f"not counted as detected."
        if broken
        else ""
    )

    n_tests = report.head_run.passed if report.head_run else 0

    if ungated:
        report.verdict = Verdict.NO_GATE
        names = "; ".join(h.label for h in ungated[:3])
        more = f" (+{len(ungated) - 3} more)" if len(ungated) > 3 else ""
        report.headline = (
            f"{len(ungated)} of {len(evaluated)} evaluated behavioural change(s) in this PR can "
            f"be reverted with all {n_tests} of its tests still passing: {names}{more}. "
            f"This PR's tests would pass without that change.{unknown_note}"
        )
    elif assert_gated:
        report.verdict = Verdict.GATE_HOLDS
        report.headline = (
            f"Every one of the {len(evaluated)} evaluated behavioural change(s) in this PR is "
            f"detected by its own tests ({len(assert_gated)} by an assertion, "
            f"{len(build_gated)} at import time).{unknown_note}"
        )
    elif build_gated:
        report.verdict = Verdict.GATE_HOLDS_BUILD
        report.headline = (
            f"All {len(build_gated)} evaluated behavioural change(s) are detected only because "
            f"reverting them stops the tests building/importing. No assertion was ever "
            f"exercised, so the tests gate the presence of the new code, not its "
            f"behaviour.{unknown_note}"
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


def _head_failure_reason(run: TestRunResult, install: "depsmod.InstallReport | None" = None) -> str:
    """Why the experiment could not start.

    When the dependency install failed, that is almost certainly *the* reason
    the tests will not run, so it leads - with the installer's real stderr, not
    a paraphrase of it.
    """
    if install is not None and install.failed:
        return depsmod.failure_reason(install) + "\n\nThe tests were run anyway and " + (
            _base_head_failure_reason(run)
        )
    return _base_head_failure_reason(run) + _install_hint(install)


def _install_hint(install: "depsmod.InstallReport | None") -> str:
    """Why the precondition may have failed, when the environment is suspect.

    A failure to import at head is the exact symptom of missing dependencies,
    so if we did not install any it is dishonest to leave that out of the
    explanation.
    """
    if install is None:
        return ""
    if install.status == "none":
        return f" No dependency install was detected: {install.evidence}."
    if install.status == "disabled":
        return (
            " Dependency installation was disabled, so the tests ran against the environment "
            "exactly as it was found."
        )
    return ""


def _base_head_failure_reason(run: TestRunResult) -> str:
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
