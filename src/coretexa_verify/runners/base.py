"""Runner interface plus the pieces every runner shares."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass

from ..gitops import run, sanitized_environ
from ..models import BuildInfo, Outcome, RunnerInfo, TestRunResult

TAIL_CHARS = 4000

#: Files that pin dependency *versions*. No assertion in any test suite
#: observes their content: the toolchain reads them long before a test runs,
#: and a revert of one either changes nothing or breaks resolution outright.
#: Counting them as behavioural changes only ever pads a NO_GATE denominator.
DEPENDENCY_MANIFESTS = frozenset(
    {
        "go.mod",
        "go.sum",
        "gomod2nix.toml",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lockb",
        "Cargo.lock",
        "poetry.lock",
        "Pipfile.lock",
        "pdm.lock",
        "uv.lock",
        "composer.lock",
        "Gemfile.lock",
        "flake.lock",
        "gradle.lockfile",
    }
)

#: Directory names whose contents are read *by path* at test time, so a runner
#: can reach them whatever their extension. Keeps a Go ``testdata/*.json`` or a
#: Maven ``src/test/resources/*.xml`` reachable under an extension allow-list.
DATA_DIRECTORIES = frozenset({"testdata", "test-data", "fixtures", "fixture", "resources"})


@dataclass
class BuildStep:
    """A repository build the tests depend on, that we own re-running.

    A test that imports build output (``dist/``, a compiled extension) does not
    see a source revert unless the build runs again *while the source is
    reverted*. When a build step is known we therefore re-run it before every
    single test run, mutated or not; when one is needed and not known, the
    verdict is degraded rather than trusted.
    """

    argv: list[str]
    reason: str
    cwd: str
    timeout: int = 900

    def info(self) -> BuildInfo:
        return BuildInfo(command=list(self.argv), reason=self.reason, cwd=self.cwd)


@dataclass
class DetectionContext:
    """Everything a detector may look at. Filesystem access is read-only."""

    repo: str

    def exists(self, *rel: str) -> bool:
        return any(os.path.exists(os.path.join(self.repo, r)) for r in rel)

    def read(self, rel: str, limit: int = 200_000) -> str:
        try:
            with open(os.path.join(self.repo, rel), encoding="utf-8", errors="replace") as fh:
                return fh.read(limit)
        except OSError:
            return ""


class Runner:
    """A way of executing a subset of a repository's tests.

    Subclasses implement :meth:`build_command` and :meth:`parse`. Everything
    else - timeouts, output capture, tail truncation - is handled here so that
    adding a language is one small class plus one registry entry.
    """

    id: str = "abstract"
    language: str = "unknown"
    #: extension of the machine-readable report this runner writes
    report_suffix: str = "xml"
    #: File extensions this runner can hand straight to its test command.
    #:
    #: A polyglot repository is the reason this exists. sqlfluff is a Python
    #: project that vendors a Rust crate, so it contains
    #: ``sqlfluffrs/tests/fixture_tests.rs`` - a perfectly real integration test
    #: that pytest cannot run and must never be offered. Selection filters
    #: candidate test files through this tuple, so a runner is never handed a
    #: path it would choke on. An empty tuple means "no restriction".
    test_file_extensions: tuple = ()
    #: File extensions whose *behaviour* a test this runner executes can
    #: observe. An empty tuple means "this runner makes no claim", and every
    #: file stays reachable - which is the conservative answer, because an
    #: unreachable hunk is excluded from the ungated count and so a wrong
    #: "unreachable" can only ever hide a finding.
    #:
    #: Only the Go runner fills this in today. A Go test cannot execute a
    #: ``.vue`` file, a stylesheet or a service-worker manifest, and gatus
    #: #1725 spent 15 of its 34 "behavioural changes" on exactly those. A
    #: Python runner, by contrast, genuinely does execute ``.sql`` fixtures and
    #: render ``.html`` templates, so it declares nothing and behaves as before.
    source_file_extensions: tuple = ()
    #: May the experiment proceed when selection maps the PR's test changes to
    #: nothing runnable? False for every detected runner: an empty selection
    #: means we do not know which tests are the PR's evidence, and running the
    #: whole suite as a proxy is exactly what this tool refuses to do. True only
    #: for a command the user wrote out in full, because there the command *is*
    #: the selection and running it as given is precisely what was asked for.
    selection_optional: bool = False

    def __init__(self, repo: str, reason: str, extra_args: list[str] | None = None):
        self.repo = repo
        #: Directory the runner is actually invoked from. Equals ``repo`` unless
        #: :meth:`focus` moved us into a monorepo package.
        self.cwd = repo
        self.reason = reason
        self.extra_args = list(extra_args or [])
        self._pycache_dir: str | None = None
        #: Set by :func:`coretexa_verify.verify` when a build step is detected.
        self.build_step: "BuildStep | None" = None
        self.build_info: BuildInfo | None = None
        #: Warnings a detector wants surfaced in the report - things the user
        #: needs to know about the environment we are about to run in.
        self.setup_warnings: list[str] = []

    # -- environment -------------------------------------------------------
    @property
    def pycache_prefix(self) -> str:
        """A scratch directory that absorbs all bytecode caching for this run."""
        if self._pycache_dir is None:
            self._pycache_dir = tempfile.mkdtemp(prefix="coretexa-verify-pyc-")
        return self._pycache_dir

    def subprocess_env(self) -> dict:
        """Environment for every runner subprocess.

        ``PYTHONDONTWRITEBYTECODE`` stops us dropping ``__pycache__`` into the
        user's tree. ``PYTHONPYCACHEPREFIX`` points bytecode *lookup* at an
        empty scratch directory, which is the load-bearing half: a stale
        ``.pyc`` whose recorded source mtime and size still match a reverted
        file would otherwise let the head version of the code answer for the
        base version, and silently turn GATE_HOLDS into NO_GATE. A revert that
        does not change a file's byte count - swapping ``1`` for ``2`` - hits
        exactly that case.
        """
        return {
            "CI": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": self.pycache_prefix,
        }

    def child_environment(self) -> dict:
        """Exactly what a test-run subprocess will see. Nothing secret in it.

        The real calls pass ``isolate=True`` to :func:`coretexa_verify.gitops.run`
        rather than building this dict, so that no future call site can forget
        by constructing its own. This exists so the guarantee can be asserted
        directly in a test, and so a reader can see the composition in one
        place: sanitised environment first, our own overrides on top.
        """
        env = sanitized_environ()
        env.update(self.subprocess_env())
        return env

    def cleanup(self) -> None:
        """Drop the scratch directory. Safe to call more than once."""
        if self._pycache_dir:
            shutil.rmtree(self._pycache_dir, ignore_errors=True)
            self._pycache_dir = None

    @property
    def info(self) -> RunnerInfo:
        return RunnerInfo(id=self.id, language=self.language, reason=self.reason)

    # -- to implement ------------------------------------------------------
    def build_command(self, targets: list[str], report_path: str) -> list[str]:
        raise NotImplementedError

    def parse(self, report_path: str, exit_code: int, stdout: str, stderr: str) -> TestRunResult:
        raise NotImplementedError

    def default_test_dir(self) -> str | None:
        """Directory to fall back to when no specific test can be selected."""
        return None

    def collect(
        self, targets: list[str], timeout: int, extra: list[str] | None = None
    ) -> list[str] | None:
        """List the test ids under ``targets`` without running them.

        Returning None means "this runner cannot enumerate tests", which
        switches off selection refinement rather than guessing. ``extra`` is
        passed through to the runner (we use it for ``-k <fixture stem>``).
        """
        return None

    def focus(self, targets: list[str]) -> tuple[list[str], str] | None:
        """Move into the monorepo package that owns ``targets``, if there is one.

        Returns ``(rewritten targets, explanation)`` and mutates :attr:`cwd`, or
        None when the repository is not a workspace or the targets span more
        than one package.

        Compiled languages also use this as the point where repo-relative *file
        paths* (which is all selection knows how to produce) become the
        runner's native unit of work - a Go package pattern, a cargo crate
        spec. The rewrite is reported, so the user still sees the path they
        changed next to the target it became.
        """
        return None

    def narrow_from_diff(
        self, repo: str, base_sha: str, head_sha: str, path: str, targets: list[str]
    ) -> tuple[list[str], str, str] | None:
        """Narrow ``targets`` to the individual tests this PR touched in ``path``.

        Returns ``(targets, detail, proof)`` or None. Unlike the pytest path,
        this does not require the runner to be able to enumerate tests first:
        the names come from parsing the head file's own test declarations and
        intersecting them with the diff, so they cannot be invented. If a name
        somehow does not exist the run collects nothing and the verdict is
        INCONCLUSIVE - the failure mode is closed, not open.
        """
        return None

    def fixture_targets(self, fixture_path: str) -> tuple[list[str], str, str] | None:
        """Map a changed fixture to its consumers *by language convention*.

        Returns ``(targets, detail, proof)`` or None. This exists for the cases
        where a toolchain guarantees the mapping - Go's ``testdata/``, Maven's
        ``src/test/resources/`` - which is stronger evidence than the literal
        grep in :mod:`coretexa_verify.selection` and is therefore tried first.
        """
        return None

    def detect_build_step(self, timeout: int) -> "BuildStep | None":
        """The repo's own build step, when tests consume its output.

        Returning None must mean "no separate build is needed", not "we did not
        look". For Go and Rust that is a statement about the toolchain rather
        than a gap - see the comments in those modules.
        """
        return None

    def artifact_risk(self, targets: list[str], source_paths: list[str]) -> str:
        """Why a stale build artefact could mask a source revert, or ``""``."""
        return ""

    def coverage_gap(self, targets: list[str], source_paths: list[str]) -> str:
        """Why these tests could not observe a revert of this source, or ``""``.

        A `NO_GATE` says "the tests passed with the source reverted". That is
        only a statement *about the tests* when the tests could have failed. If
        the selected tests never load the changed code at all, the experiment
        had no power, and reporting `NO_GATE` blames an author for a gap the
        run never measured.

        The defect this exists for: a pull request changed
        ``src/pkg/file_preview/`` and, separately, one test in
        ``src/internal/ui/prompt/``. Selection took the changed test - correctly,
        it is the only one the PR touched - and the run then reverted a package
        that test does not import. Both runs passed identically and the verdict
        read "this PR's tests would pass without the fix."

        Returning ``""`` means *no claim*, which is the default for every runner
        that cannot prove non-coverage. Only positive proof should downgrade a
        verdict, because guessing here would trade a false `NO_GATE` for a false
        `INCONCLUSIVE` and lose real findings.
        """
        return ""

    # -- reachability ------------------------------------------------------
    def unreachable_reason(self, path: str) -> str:
        """Why no test this runner executes can observe a change to ``path``.

        Returns ``""`` when the file *is* reachable, which is the default for
        anything this runner has not positively ruled out. Two rules, in order:

        1. a dependency manifest or lock file is unreachable for every runner -
           its content is consumed by the package manager, not by an assertion;
        2. if the runner declares :attr:`source_file_extensions`, a file with
           some other extension is unreachable unless it sits in a directory
           tests read by path (``testdata/``, ``resources/``).

        Hunks in unreachable files are never reverted and never counted as
        behavioural changes. They are listed separately instead, so a NO_GATE
        headline is about the code the suite could actually have covered.
        """
        rel = path.replace("\\", "/").lstrip("./")
        parts = rel.split("/")
        name = parts[-1]
        if name in DEPENDENCY_MANIFESTS or name.endswith(".lock"):
            return "dependency manifest/lock file: its content is read by the package manager"
        exts = self.source_file_extensions
        if not exts:
            return ""
        if rel.endswith(tuple(exts)):
            return ""
        if any(part in DATA_DIRECTORIES for part in parts[:-1]):
            return ""
        kinds = ", ".join(exts)
        return (
            f"the {self.id} runner executes {kinds} tests, which cannot observe a change to "
            f"this file"
        )

    def rerun_targets(self, failing_ids: list[str], current_targets: list[str]) -> list[str] | None:
        """Targets that re-run exactly ``failing_ids`` and nothing else.

        Used to re-check a head failure at the merge base. Returning None means
        this runner cannot express "just these tests", and the caller then
        leaves the verdict INCONCLUSIVE rather than guessing.

        The default assumes what pytest, vitest and jest all give us: a failing
        id *is* a runnable target.
        """
        ids = [i for i in failing_ids if i]
        if not ids or any("::" not in i for i in ids):
            return None
        return ids

    def test_key(self, ident: str) -> str:
        """Identity of a test, comparable between a failing id and a collected id.

        The default is the id itself, which is right whenever the runner
        reports failures in the same vocabulary it collects in.
        """
        return ident

    # -- shared ------------------------------------------------------------
    def run_build(self) -> BuildInfo | None:
        """Re-run the detected build step. Called before *every* test run."""
        step = self.build_step
        if step is None:
            return None
        if self.build_info is None:
            self.build_info = step.info()
        info = self.build_info
        info.runs += 1
        res = run(step.argv, cwd=step.cwd, timeout=step.timeout, env={"CI": "1"}, isolate=True)
        if res.timed_out:
            info.status = "timeout"
            info.failures += 1
            info.note = f"the build exceeded its {step.timeout}s timeout"
        elif res.returncode != 0:
            info.status = "failed"
            info.failures += 1
            info.note = tail(res.stderr or res.stdout, 1200)
        else:
            info.status = "ok"
        return info

    def execute(self, targets: list[str], timeout: int, report_dir: str, tag: str) -> TestRunResult:
        report_path = os.path.join(report_dir, f"{tag}-report.{self.report_suffix}")
        self.run_build()
        argv = self.build_command(targets, report_path)
        started = time.monotonic()
        res = run(argv, cwd=self.cwd, timeout=timeout, env=self.subprocess_env(), isolate=True)
        duration = time.monotonic() - started
        if res.timed_out:
            return TestRunResult(
                command=argv,
                outcome=Outcome.TIMEOUT,
                exit_code=None,
                duration_s=round(duration, 2),
                timeout_s=timeout,
                note=f"exceeded the {timeout}s per-run timeout",
                stdout_tail=tail(res.stdout),
                stderr_tail=tail(res.stderr),
            )
        parsed = self.parse(report_path, res.returncode, res.stdout, res.stderr)
        parsed.command = argv
        parsed.exit_code = res.returncode
        parsed.duration_s = round(duration, 2)
        parsed.timeout_s = timeout
        parsed.stdout_tail = tail(res.stdout)
        parsed.stderr_tail = tail(res.stderr)
        return parsed


def tail(text: str, limit: int = TAIL_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return "...[truncated]...\n" + text[-limit:]
