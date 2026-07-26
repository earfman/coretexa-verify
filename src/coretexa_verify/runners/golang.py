"""`go test` runner.

Results come from ``go test -json``, which emits one JSON object per event and
is the only output shape that reliably separates the three things this tool has
to tell apart:

* a test that ran and failed an assertion (``Action=fail`` **with** a ``Test``
  field) - that is ``GATE_HOLDS``;
* a package that never compiled (``Action=fail`` **without** a ``Test`` field,
  and/or output carrying ``[build failed]``) - that is ``GATE_HOLDS_BUILD``;
* a test that was skipped (``Action=skip``), which is evidence of nothing and is
  excluded from the executed count exactly as a pytest skip is.

Build artefacts
---------------

Every other compiled-language integration in this tool would need a
:class:`~coretexa_verify.runners.base.BuildStep` - a build re-run inside the
mutation so the tests cannot read a stale artefact. Go does not, and the reason
is worth stating precisely rather than assuming:

``go test`` *is* the build. It compiles the package under test and every
dependency from source on each invocation, keyed by a content hash of the
inputs (see ``go help cache``). Reverting a ``.go`` file changes that hash, so
the package is recompiled before the test binary is linked, and the test binary
is rebuilt too. There is no artefact directory a test could import instead: Go
has no equivalent of ``dist/``, and even ``//go:embed`` data is baked in at
compile time from the files as they exist during that same compile. So
:meth:`detect_build_step` returns None because no separate build exists, not
because we did not look, and :meth:`artifact_risk` returns ``""`` for the same
reason. The one thing that *would* be a risk - a test that shells out to a
prebuilt binary in the repo - is not something ``go test`` produces on its own.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil

from ..gitops import run
from ..models import Outcome, TestRunResult
from .base import DetectionContext, Runner

#: Output fragments that mean the package never got as far as running a test.
BUILD_FAILURE_MARKERS = (
    "[build failed]",
    "build failed",
    "[setup failed]",
    "cannot find package",
    "no required module provides package",
    "undefined:",
    "syntax error",
    "typecheck",
    "missing go.sum entry",
)

#: Output that means the *toolchain* refused, which is not a verdict about code.
TOOLCHAIN_FAILURE_MARKERS = (
    "requires go >=",
    "go.mod requires go",
    "but go.mod requires",
    "toolchain not available",
    "updates to go.mod needed",
)

#: Top-level Go test entry points. Fuzz targets run as ordinary tests in a
#: non-fuzzing run, and Examples with an Output comment are real assertions.
GO_TEST_FUNC = re.compile(r"^func\s+((?:Test|Fuzz|Example)[A-Z_]\w*)\s*\(", re.MULTILINE)

GO_VERSION_RE = re.compile(r"go(\d+)\.(\d+)(?:\.(\d+))?")


def _version_tuple(text: str) -> tuple[int, int, int] | None:
    m = GO_VERSION_RE.search(text or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def module_root(repo: str, rel_path: str) -> str:
    """Nearest ancestor directory of ``rel_path`` holding a ``go.mod``.

    Returns a repo-relative directory, or ``""`` for the repository root. This
    is what makes a Go monorepo (several modules in one tree) work: the command
    has to run from the module root or the ``./pkg`` pattern means nothing.
    """
    parts = rel_path.replace("\\", "/").split("/")[:-1]
    while parts:
        candidate = "/".join(parts)
        if os.path.exists(os.path.join(repo, candidate, "go.mod")):
            return candidate
        parts.pop()
    return ""


def package_dir(rel_path: str) -> str:
    """The package directory owning a ``.go`` file. Go packages are directories."""
    return posixpath.dirname(rel_path.replace("\\", "/"))


def go_test_spans(text: str) -> list[tuple[str, int, int]]:
    """``(name, first line, last line)`` for each top-level test function.

    The end is found by scanning for the next line that is exactly ``}``, which
    is what gofmt guarantees for a top-level declaration - and every Go
    repository worth running tests against is gofmt'd.
    """
    lines = text.splitlines()
    starts: list[tuple[str, int]] = []
    for m in GO_TEST_FUNC.finditer(text):
        line_no = text.count("\n", 0, m.start()) + 1
        starts.append((m.group(1), line_no))
    spans: list[tuple[str, int, int]] = []
    for name, start in starts:
        end = len(lines)
        for i in range(start, len(lines)):
            if lines[i].rstrip() == "}":
                end = i + 1
                break
        spans.append((name, start, end))
    return spans


def parse_go_test_json(
    stdout: str, exit_code: int, stderr: str = ""
) -> TestRunResult:
    """Turn a ``go test -json`` event stream into a :class:`TestRunResult`.

    ``go test -json`` interleaves per-test events with per-package events. Only
    events carrying a ``Test`` field are tests; a terminal package event with no
    ``Test`` is the package's own verdict, and a *failing* package that produced
    no test events at all is a compile failure - which is the signal
    ``GATE_HOLDS_BUILD`` is built on.

    Non-JSON lines are kept and searched too. Older toolchains, and any failure
    that happens before the test binary is reached, print plain text; treating
    an unparseable line as absent would silently turn a build failure into
    "nothing happened".
    """
    passed = failed = skipped = 0
    failing: list[str] = []
    erroring: list[str] = []
    raw_lines: list[str] = []
    saw_event = False
    #: package -> whether any per-test event was ever seen for it
    pkg_had_tests: dict[str, bool] = {}
    pkg_failed: list[str] = []
    pkg_output: dict[str, list[str]] = {}

    for line in (stdout or "").splitlines():
        line = line.rstrip("\r")
        if not line.strip():
            continue
        if not line.lstrip().startswith("{"):
            raw_lines.append(line)
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raw_lines.append(line)
            continue
        if not isinstance(event, dict):
            raw_lines.append(line)
            continue
        saw_event = True
        action = event.get("Action") or ""
        pkg = event.get("Package") or ""
        test = event.get("Test") or ""
        pkg_had_tests.setdefault(pkg, False)

        if action == "output":
            pkg_output.setdefault(pkg, []).append(str(event.get("Output") or ""))
            continue
        if test:
            pkg_had_tests[pkg] = True
            # Subtests report their own pass/fail *and* roll up into the parent.
            # Counting both would double-count, so only leaf-or-parent terminal
            # events are counted - go emits one terminal event per test id, and
            # a test id includes its subtest path, so each id is counted once.
            if action == "pass":
                passed += 1
            elif action == "fail":
                failed += 1
                failing.append(f"{pkg}::{test}" if pkg else test)
            elif action == "skip":
                skipped += 1
            continue
        if action == "fail":
            pkg_failed.append(pkg or "<unknown package>")

    blob = "\n".join(raw_lines) + "\n" + (stderr or "")
    for pkg, chunks in pkg_output.items():
        blob += "\n" + "".join(chunks)

    toolchain = [m for m in TOOLCHAIN_FAILURE_MARKERS if m in blob]
    if toolchain:
        return TestRunResult(
            command=[],
            outcome=Outcome.RUNNER_ERROR,
            exit_code=exit_code,
            note=(
                f"the Go toolchain refused to run this module (matched {toolchain[0]!r}); "
                f"this is an environment mismatch, not a result about the code"
            ),
        )

    # A package that failed without producing a single test event never built.
    # That is the whole signal: `go test` only reports a package-level failure
    # with no test underneath it when the compile or the link went wrong.
    build_broken = [p for p in pkg_failed if not pkg_had_tests.get(p, False)]
    marker_hit = next((m for m in BUILD_FAILURE_MARKERS if m in blob), "")
    if marker_hit and not build_broken and not saw_event:
        # Older toolchains print build errors as plain text and never reach the
        # JSON encoder at all.
        build_broken = ["<compile>"]
    erroring = list(build_broken)
    errored = len(erroring)

    total = passed + failed + skipped + errored

    if total == 0:
        if exit_code == 0:
            return TestRunResult(
                command=[],
                outcome=Outcome.NO_TESTS_RUN,
                exit_code=exit_code,
                note="go test matched no tests in the selected package(s)",
            )
        # Non-zero exit with nothing to show for it: a usage error, a missing
        # package, a broken environment. Never a statement about the code.
        return TestRunResult(
            command=[],
            outcome=Outcome.RUNNER_ERROR,
            exit_code=exit_code,
            note=f"go test exited {exit_code} without reporting a single test or package result",
        )

    if failed:
        outcome, note = Outcome.ASSERT_FAIL, ""
    elif errored:
        outcome = Outcome.BUILD_ERROR
        note = (
            f"{errored} package(s) failed before any test ran"
            + (f" ({marker_hit})" if marker_hit else "")
        )
    elif exit_code == 0:
        outcome, note = Outcome.PASS, ""
    else:
        outcome = Outcome.RUNNER_ERROR
        note = f"the event stream shows no failure but go test exited {exit_code}"

    return TestRunResult(
        command=[],
        outcome=outcome,
        exit_code=exit_code,
        passed=passed,
        failed=failed,
        errored=errored,
        skipped=skipped,
        total=total,
        failing_ids=failing[:50],
        erroring_ids=erroring[:50],
        note=note,
    )


class GoTestRunner(Runner):
    id = "go-test"
    language = "go"
    test_file_extensions = (".go",)
    #: `go test` compiles and runs Go. It cannot execute a Vue component, a
    #: stylesheet, a web-app manifest or a bundled JS asset, so a change to one
    #: is outside the reach of anything this runner can run. Files under
    #: ``testdata/`` stay reachable whatever their extension - the base class
    #: handles that - because Go tests open them by path.
    #:
    #: Note the deliberate omission: a file may be pulled into the binary by
    #: ``//go:embed`` and still be listed unreachable here. That is the safe
    #: direction. Excluding a hunk can only remove a NO_GATE finding, never
    #: invent one, and every excluded hunk is printed in the report.
    source_file_extensions = (".go",)
    #: go test writes nothing to a file; results are the JSON event stream.
    report_suffix = "jsonl"

    def __init__(self, repo: str, reason: str, extra_args=None, module: str = ""):
        super().__init__(repo, reason, extra_args)
        #: repo-relative directory of the go.mod we run from
        self.module = module
        self.cwd = os.path.join(repo, module) if module else repo

    # -- target shapes -----------------------------------------------------
    # A target is either "./pkg" (whole package) or "./pkg::TestName" (one test).
    # The "::" spelling is the tool's existing convention for "a single test
    # rather than a whole file", so the max-targets / max-collected accounting
    # in verify.py keeps working without knowing anything about Go.

    @staticmethod
    def _split(target: str) -> tuple[str, str]:
        pkg, sep, name = target.partition("::")
        return pkg, (name if sep else "")

    def build_command(self, targets: list[str], report_path: str) -> list[str]:
        packages: list[str] = []
        names: list[str] = []
        narrowed_all = True
        for target in targets:
            pkg, name = self._split(target)
            if pkg not in packages:
                packages.append(pkg)
            if name:
                if name not in names:
                    names.append(name)
            else:
                narrowed_all = False

        argv = ["go", "test", "-json", "-count=1"]
        # -run applies to *every* package on the command line, so it may only be
        # used when every target is narrowed. Mixing "all of package A" with
        # "one test of package B" into a single -run would silently drop the
        # rest of package A, which would be a quieter and worse bug than just
        # running more than we meant to.
        if names and narrowed_all:
            argv += ["-run", "^(" + "|".join(re.escape(n) for n in names) + ")$"]
        argv += [*self.extra_args, *packages]
        return argv

    def parse(self, report_path: str, exit_code: int, stdout: str, stderr: str) -> TestRunResult:
        return parse_go_test_json(stdout, exit_code, stderr)

    def default_test_dir(self) -> str | None:
        # "./..." would be the whole module - the exact widening this tool
        # refuses to do as a proxy for a PR's own tests.
        return None

    # -- build artefacts ---------------------------------------------------
    def detect_build_step(self, timeout: int) -> None:
        """None: ``go test`` compiles the package itself. See the module docstring."""
        return None

    def artifact_risk(self, targets: list[str], source_paths: list[str]) -> str:
        """Empty: there is no build output for a Go test to read instead of source.

        ``go test`` recompiles every changed package from source on each run
        (the build cache is keyed on content), so a reverted ``.go`` file is
        always reflected in the test binary that runs next.
        """
        return ""

    # -- monorepo / path -> package ---------------------------------------
    def map_to_packages(self, targets: list[str]) -> tuple[str, list[str]] | None:
        """``(module root, ./package targets)`` for a list of file-path targets.

        Selection deals in file paths because that is what a diff contains. Go
        deals in packages, which are directories. This is where the two meet.
        Returns None when the targets span two ``go.mod`` files, since no single
        ``go test`` invocation can cover both.
        """
        roots: set[str] = set()
        parsed: list[tuple[str, str] | str] = []
        for target in targets:
            path, name = self._split(target)
            if path.startswith("./") or path == ".":
                parsed.append(target)
                continue
            roots.add(module_root(self.repo, path))
            parsed.append((path, name))

        if len(roots) > 1:
            return None
        root = roots.pop() if roots else self.module

        out: list[str] = []
        for item in parsed:
            if isinstance(item, str):
                if item not in out:
                    out.append(item)
                continue
            path, name = item
            directory = package_dir(path)
            rel = posixpath.relpath(directory, root) if root else directory
            pkg = "./" + rel if rel not in (".", "") else "."
            target = f"{pkg}::{name}" if name else pkg
            if target not in out:
                out.append(target)
        return root, out

    def focus(self, targets: list[str]) -> tuple[list[str], str] | None:
        """Map file paths to ``./package`` and move to the owning ``go.mod``."""
        mapped = self.map_to_packages(targets)
        if mapped is None:
            return None
        root, out = mapped
        self.module = root
        self.cwd = os.path.join(self.repo, root) if root else self.repo
        where = f"{root}/go.mod" if root else "go.mod"
        return out, (
            f"Go tests are selected by package, not by file, so the changed test file(s) were "
            f"mapped to the package directory that owns them and run from {where}"
        )

    # -- enumeration -------------------------------------------------------
    def collect(self, targets: list[str], timeout: int, extra=None) -> list[str] | None:
        """Test names via ``go test -list``, which compiles but runs nothing.

        Enumeration can be asked for before :meth:`focus` has run, when targets
        are still file paths, so the same path-to-package mapping is applied
        here rather than handing ``go`` a filename it would reject.
        """
        mapped = self.map_to_packages(targets)
        if mapped is None:
            return None
        root, mapped_targets = mapped
        cwd = os.path.join(self.repo, root) if root else self.repo
        packages: list[str] = []
        for target in mapped_targets:
            pkg, _ = self._split(target)
            if pkg not in packages:
                packages.append(pkg)
        if not packages:
            return None
        ids: list[str] = []
        for pkg in packages:
            res = run(
                ["go", "test", "-list", ".*", pkg],
                cwd=cwd,
                timeout=timeout,
                env=self.subprocess_env(),
                isolate=True,
            )
            if res.timed_out or res.returncode != 0:
                return None
            for line in res.stdout.splitlines():
                line = line.strip()
                if not line or line.startswith(("ok ", "ok\t", "FAIL", "?", "---", "PASS")):
                    continue
                if not re.match(r"^(Test|Fuzz|Example|Benchmark)", line):
                    continue
                nid = f"{pkg}::{line}"
                if nid not in ids:
                    ids.append(nid)
        return ids

    # -- re-running a subset ----------------------------------------------
    def rerun_targets(self, failing_ids: list[str], current_targets: list[str]) -> list[str] | None:
        """Re-run just these tests, in the packages we are already running.

        A Go failing id names the package by *import path*
        (``github.com/x/y/internal::TestZoxide``) while a target names it by
        directory relative to the module root (``./internal``). The two cannot
        be compared, so the package side is taken from the targets we already
        have and only the test names come from the failures.

        Subtest paths are cut back to their top-level test: ``go test -run``
        splits its pattern on ``/`` and matches one component per nesting
        level, so handing it ``^(TestX/case)$`` would not be the regex it looks
        like. Running the parent re-runs the subtest anyway.
        """
        names: list[str] = []
        for ident in failing_ids:
            name = self.test_key(ident)
            if name and name not in names:
                names.append(name)
        packages: list[str] = []
        for target in current_targets:
            pkg, _ = self._split(target)
            if pkg and pkg not in packages:
                packages.append(pkg)
        if not names or not packages:
            return None
        return [f"{pkg}::{name}" for pkg in packages for name in names]

    def test_key(self, ident: str) -> str:
        """Top-level test name, package and subtest path dropped.

        The package has to go because a failure and a collection spell it
        differently (import path vs. ``./dir``); the subtest path has to go
        because ``-run`` cannot address one directly. The cost is that two
        packages in the same run sharing a test name are treated as one test,
        which over-excludes rather than under-excludes.
        """
        name = ident.partition("::")[2] or ident
        return name.split("/")[0].strip()

    # -- narrowing ---------------------------------------------------------
    def narrow_from_diff(self, repo, base_sha, head_sha, path, targets):
        """Keep only the ``TestXxx`` functions whose body the diff touched."""
        if not path.endswith("_test.go"):
            return None
        from .. import refine
        from ..hunks import read_head_text

        head_text = read_head_text(repo, head_sha, path)
        if not head_text:
            return None
        changed = refine.changed_line_numbers(repo, base_sha, head_sha, path)
        if not changed:
            return None
        spans = go_test_spans(head_text)
        if not spans:
            return None
        names = [n for n, start, end in spans if changed & set(range(start, end + 1))]
        if not names or len(names) == len(spans):
            # All of them, or none: narrowing would add nothing.
            return None
        out = []
        for target in targets:
            pkg, _ = self._split(target)
            for name in names:
                nid = f"{pkg}::{name}"
                if nid not in out:
                    out.append(nid)
        return (
            out,
            f"only the {len(names)} test function(s) this PR added or modified "
            f"({', '.join(names[:5])})",
            (
                f"the changed lines of {path} fall inside these top-level `func Test...` "
                f"declarations in the head revision, and go test was narrowed to them with "
                f"-run"
            ),
        )

    # -- fixtures ----------------------------------------------------------
    def fixture_targets(self, fixture_path: str):
        """``pkg/testdata/x`` is consumed by ``pkg``'s tests. That is a language rule.

        From ``go help test``: "The go tool will ignore a directory named
        testdata, making it available to hold ancillary data needed by the
        tests." ``go test`` also runs each test binary with its working
        directory set to the package's source directory, so a package's tests
        are the only tests that can open ``testdata/`` with a relative path.
        The mapping from a changed testdata file to the owning package is
        therefore established by the toolchain, not guessed from a grep.
        """
        parts = fixture_path.replace("\\", "/").split("/")
        if "testdata" not in parts:
            return None
        owner = "/".join(parts[: parts.index("testdata")])
        if not owner:
            return None
        abs_owner = os.path.join(self.repo, owner)
        if not os.path.isdir(abs_owner):
            return None
        tests = sorted(
            f"{owner}/{name}"
            for name in os.listdir(abs_owner)
            if name.endswith("_test.go")
        )
        if not tests:
            return None
        return (
            tests,
            f"the fixture lives in {owner}/testdata/, which by Go convention belongs to the "
            f"package in {owner}",
            (
                f"go help test: a directory named testdata is ignored by the build and holds "
                f"data for the tests of its parent package, and go test runs that package's "
                f"binary with {owner} as its working directory - so {owner}'s tests are the "
                f"only tests that can read this file by relative path"
            ),
        )


# --------------------------------------------------------------------------


def read_go_mod(ctx: DetectionContext) -> tuple[str, str]:
    """``(go directive, toolchain directive)`` from go.mod, either possibly ""."""
    text = ctx.read("go.mod")
    go_line = ""
    toolchain = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("go ") and not go_line:
            go_line = line.split(None, 1)[1].strip()
        elif line.startswith("toolchain ") and not toolchain:
            toolchain = line.split(None, 1)[1].strip()
    return go_line, toolchain


def detect_go(ctx: DetectionContext, extra_args=None) -> Runner | None:
    """Pick the Go runner, or return None if this is not a Go module."""
    if not ctx.exists("go.mod"):
        return None
    if not shutil.which("go"):
        return None

    go_directive, toolchain = read_go_mod(ctx)
    reason = "found go.mod" + (f" (go {go_directive})" if go_directive else "") + " -> `go test -json`"
    runner = GoTestRunner(ctx.repo, reason=reason, extra_args=extra_args)

    # Toolchain pinning. Go 1.21+ will download a newer toolchain on demand
    # unless GOTOOLCHAIN=local, so an older `go` on PATH is usually fine and
    # warning about it would be noise. When the download is switched off and
    # the installed version really is too old, say so with both versions rather
    # than letting it surface as an inscrutable build failure.
    installed = _version_tuple(_go_version())
    required = _version_tuple("go" + (toolchain.replace("go", "") or go_directive))
    if installed and required and installed < required:
        mode = os.environ.get("GOTOOLCHAIN", "auto")
        pinned = toolchain or go_directive
        if mode == "local":
            runner.setup_warnings.append(
                f"go.mod requires go{pinned} but the go on PATH is "
                f"go{'.'.join(str(p) for p in installed)}, and GOTOOLCHAIN=local forbids "
                f"downloading the pinned toolchain. The run will fail to build and the verdict "
                f"will be INCONCLUSIVE; install go{pinned} or unset GOTOOLCHAIN."
            )
        else:
            runner.setup_warnings.append(
                f"go.mod pins go{pinned}; the go on PATH is "
                f"go{'.'.join(str(p) for p in installed)}, so the go command will download and "
                f"use go{pinned} (GOTOOLCHAIN={mode})"
            )
    return runner


def _go_version() -> str:
    res = run(["go", "version"], cwd=os.getcwd(), timeout=60, isolate=True)
    return res.stdout or ""
