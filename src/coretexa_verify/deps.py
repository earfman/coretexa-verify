"""Detecting and running the repository's *own* test-dependency install.

The point of this module is adoption: without it, every user has to add a
"install my test deps" step to their workflow before our Action can do
anything, and that step is the wall most people never get over. With it, the
workflow is one file you paste and forget.

Three rules govern everything here.

1. **Never guess silently.** Detection walks a fixed, documented priority order
   and every result - including "nothing matched" - carries the evidence that
   produced it. The chosen command and its evidence are in the report, always.
2. **Only the repository's own declarations.** Every command we emit reads a
   file the repo already committed (``pyproject.toml``, ``requirements*.txt``,
   a lockfile). We never add a package of our own, never upgrade anything the
   repo did not ask for, and never reach for a package index on our own
   initiative.
3. **Bounded and loud.** Every install has a timeout, and a failure is data
   that becomes ``INCONCLUSIVE`` with the real stderr attached - never a
   verdict.

This module is pure standard library, like the rest of the tool.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import time
from dataclasses import dataclass, field

from .gitops import run

#: Sub-second-cheap ceiling on how much installer output we keep.
TAIL_CHARS = 4000

#: Shell metacharacters that mean an ``--install-command`` cannot be argv-split.
_SHELL_META = re.compile(r"[&|;<>$`\n]|\|\||&&")

# --------------------------------------------------------------------------
# The detection table. These constants are the documented contract; the tests
# assert against them directly so the table in the README cannot silently drift.
# --------------------------------------------------------------------------

#: ``[project.optional-dependencies]`` names we accept as "the test extra",
#: in priority order. The first three are the documented core; the rest are
#: the spellings real repositories actually use (QuantEcon ships ``testing``,
#: CMS ships ``devel``), and matching them is the difference between the
#: Action working out of the box and not.
TEST_EXTRA_NAMES = (
    "test",
    "tests",
    "testing",
    "dev",
    "devel",
    "develop",
    "development",
)

#: Dev/test requirements files, in priority order. Dash and underscore
#: spellings are both real in the wild (sqlfluff ships ``requirements_dev.txt``).
DEV_REQUIREMENTS_FILES = (
    "requirements-dev.txt",
    "requirements_dev.txt",
    "dev-requirements.txt",
    "requirements/dev.txt",
    "requirements-test.txt",
    "requirements_test.txt",
    "requirements/test.txt",
    "requirements/tests.txt",
)

#: Files that mean "this directory is an installable Python distribution".
INSTALLABLE_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg")

#: Paths a dependency install is allowed to create without anyone worrying.
#: Nothing keys off this list behaviourally - the artefact policy is snapshot
#: based, not name based - but naming them makes the report legible.
KNOWN_ARTEFACT_SUFFIXES = (
    ".egg-info",
    ".egg-info/",
    "build/",
    "dist/",
    "__pycache__/",
    ".pytest_cache/",
    "node_modules/",
    ".eggs/",
)

_PIP_FLAGS = ("--disable-pip-version-check", "--no-input")


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------


@dataclass
class InstallPlan:
    """What we decided to run, and the evidence that made us decide it."""

    detector: str
    evidence: str
    commands: list[list[str]]
    language: str = ""
    #: Extra environment for this plan's commands. Used by the Go fallback,
    #: whose only difference from the primary plan is ``GOFLAGS=-mod=mod``.
    env: dict = field(default_factory=dict)

    @property
    def display(self) -> list[str]:
        prefix = "".join(f"{k}={shlex.quote(v)} " for k, v in sorted(self.env.items()))
        return [prefix + " ".join(shlex.quote(c) for c in cmd) for cmd in self.commands]


@dataclass
class InstallReport:
    """The install as it appears in the report. Always populated, never None."""

    enabled: bool = True
    #: how the command was chosen: detected | override | disabled | none
    source: str = "none"
    detector: str = ""
    evidence: str = ""
    commands: list[str] = field(default_factory=list)
    #: ok | failed | timeout | skipped | none
    status: str = "none"
    exit_code: int | None = None
    duration_s: float = 0.0
    timeout_s: int | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    #: untracked paths that appeared while the install ran (egg-info, build/...)
    artefacts: list[str] = field(default_factory=list)
    #: *tracked* files the install modified. These are excluded from the
    #: restoration check so they can never be mistaken for our own mutation.
    dirtied_tracked: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Every installer we tried, in order, including the ones that failed.
    #: A fallback is only honest if the attempt it replaced is still visible.
    attempts: list[dict] = field(default_factory=list)

    @property
    def ran(self) -> bool:
        return self.status in ("ok", "failed", "timeout")

    @property
    def failed(self) -> bool:
        return self.status in ("failed", "timeout")

    def summary(self) -> str:
        if self.status == "ok":
            return f"installed in {self.duration_s}s"
        if self.status == "timeout":
            return f"timed out after {self.timeout_s}s"
        if self.status == "failed":
            return f"failed (exit {self.exit_code})"
        if self.status == "disabled":
            return "disabled by the caller"
        return "no dependency install was detected"

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "source": self.source,
            "detector": self.detector,
            "evidence": self.evidence,
            "commands": self.commands,
            "status": self.status,
            "summary": self.summary(),
            "exit_code": self.exit_code,
            "duration_s": self.duration_s,
            "timeout_s": self.timeout_s,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "artefacts": self.artefacts,
            "dirtied_tracked": self.dirtied_tracked,
            "notes": self.notes,
            "attempts": self.attempts,
        }


# --------------------------------------------------------------------------
# small filesystem helpers (read only)
# --------------------------------------------------------------------------


def _exists(repo: str, rel: str) -> bool:
    return os.path.exists(os.path.join(repo, rel))


def _read(repo: str, rel: str, limit: int = 400_000) -> str:
    try:
        with open(os.path.join(repo, rel), encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def _installable(repo: str) -> str | None:
    for marker in INSTALLABLE_MARKERS:
        if _exists(repo, marker):
            if marker == "setup.cfg" and not _setup_cfg_is_a_distribution(repo):
                continue
            return marker
    return None


def _setup_cfg_is_a_distribution(repo: str) -> bool:
    """``setup.cfg`` alone only means a package if it declares ``[metadata]``."""
    return "[metadata]" in _read(repo, "setup.cfg")


def optional_dependency_extras(pyproject_text: str) -> list[str]:
    """Extra names declared under ``[project.optional-dependencies]``.

    Parsed with :mod:`tomllib` when it is available (3.11+) and with a small,
    deliberately dumb line scan otherwise, because the tool supports 3.9 and
    must not acquire a TOML dependency to read one table.
    """
    try:
        import tomllib
    except ModuleNotFoundError:
        return _scan_optional_dependency_extras(pyproject_text)
    try:
        data = tomllib.loads(pyproject_text)
    except Exception:
        return _scan_optional_dependency_extras(pyproject_text)
    table = (data.get("project") or {}).get("optional-dependencies") or {}
    return [k for k in table if isinstance(k, str)]


def _scan_optional_dependency_extras(text: str) -> list[str]:
    """Fallback: names assigned inside the optional-dependencies table."""
    extras: list[str] = []
    in_table = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_table = line.replace(" ", "") in (
                "[project.optional-dependencies]",
                '["project".optional-dependencies]',
            )
            # A sub-table like [project.optional-dependencies.foo] also counts.
            if line.replace(" ", "").startswith("[project.optional-dependencies."):
                name = line.replace(" ", "")[len("[project.optional-dependencies.") :].rstrip("]")
                if name:
                    extras.append(name.strip('"').strip("'"))
                in_table = False
            continue
        if in_table and "=" in line and not line.startswith("#"):
            name = line.split("=", 1)[0].strip().strip('"').strip("'")
            if name:
                extras.append(name)
    return extras


def _has_poetry_table(pyproject_text: str) -> bool:
    return bool(re.search(r"^\s*\[tool\.poetry[.\]]", pyproject_text, re.MULTILINE))


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------


def detect_python_install(repo: str, python_exe: str) -> tuple[InstallPlan | None, str]:
    """Return ``(plan, note)`` for a Python repository.

    ``note`` is non-empty only when we deliberately declined to produce a plan,
    and it always says why. ``python_exe`` must be the *same* interpreter the
    detected test runner will use, or we would install into one environment and
    test in another.
    """
    pyproject = _read(repo, "pyproject.toml") if _exists(repo, "pyproject.toml") else ""
    pip = [python_exe, "-m", "pip", "install", *_PIP_FLAGS]

    # 1. uv ---------------------------------------------------------------
    if _exists(repo, "uv.lock"):
        if shutil.which("uv"):
            return (
                InstallPlan(
                    detector="python:uv",
                    evidence="uv.lock is committed and `uv` is on PATH",
                    # --frozen so the install cannot rewrite the committed
                    # uv.lock and dirty a tracked file behind the user's back.
                    commands=[["uv", "sync", "--frozen"]],
                    language="python",
                ),
                "",
            )
        return (
            None,
            "uv.lock is committed but `uv` is not on PATH, so no dependency install was attempted",
        )

    # 2. poetry -----------------------------------------------------------
    poetry_marker = ""
    if _exists(repo, "poetry.lock"):
        poetry_marker = "poetry.lock is committed"
    elif _has_poetry_table(pyproject):
        poetry_marker = "pyproject.toml declares a [tool.poetry] table"
    if poetry_marker:
        if shutil.which("poetry"):
            return (
                InstallPlan(
                    detector="python:poetry",
                    evidence=poetry_marker + " and `poetry` is on PATH",
                    commands=[["poetry", "install"]],
                    language="python",
                ),
                "",
            )
        return (
            None,
            f"{poetry_marker} but `poetry` is not on PATH, so no dependency install was attempted "
            f"(pip cannot see poetry's dev-dependency groups, so falling back to pip would have "
            f"installed the wrong thing)",
        )

    # 3. a test/dev extra in pyproject ------------------------------------
    if pyproject:
        declared = optional_dependency_extras(pyproject)
        lowered = {name.lower(): name for name in declared}
        for candidate in TEST_EXTRA_NAMES:
            if candidate in lowered:
                extra = lowered[candidate]
                return (
                    InstallPlan(
                        detector="python:pyproject-extra",
                        evidence=(
                            f"pyproject.toml declares [project.optional-dependencies].{extra} "
                            f"(matched {candidate!r} from the test-extra list)"
                        ),
                        commands=[[*pip, "-e", f".[{extra}]"]],
                        language="python",
                    ),
                    "",
                )

    # 4/5. requirements files ---------------------------------------------
    installable = _installable(repo)
    dev_req = next((f for f in DEV_REQUIREMENTS_FILES if _exists(repo, f)), None)
    base_req = "requirements.txt" if _exists(repo, "requirements.txt") else None

    if dev_req or base_req:
        args: list[str] = []
        evidence_bits: list[str] = []
        if installable:
            # A requirements file lists the test tooling, not the project. If
            # the project is importable-by-install, the tests almost always
            # import it, so it has to go in too - editable, so that reverting a
            # source file is still what the tests see.
            args += ["-e", "."]
            evidence_bits.append(f"{installable} makes the project installable (editable)")
        for rel in (base_req, dev_req):
            if rel:
                args += ["-r", rel]
                evidence_bits.append(f"{rel} is committed")
        return (
            InstallPlan(
                detector="python:requirements-dev" if dev_req else "python:requirements",
                evidence="; ".join(evidence_bits),
                commands=[[*pip, *args]],
                language="python",
            ),
            "",
        )

    # 6. plain installable project ----------------------------------------
    if installable:
        return (
            InstallPlan(
                detector="python:editable",
                evidence=(
                    f"{installable} makes the project installable but declares no test extra "
                    f"and ships no requirements file; installing the project itself is the most "
                    f"we can justify"
                ),
                commands=[[*pip, "-e", "."]],
                language="python",
            ),
            "",
        )

    # 7. nothing -----------------------------------------------------------
    return (
        None,
        "no uv.lock, poetry config, test extra, requirements file or installable project was "
        "found, so there was nothing to install",
    )


def python_install_plans(repo: str, python_exe: str) -> tuple[list[InstallPlan], str]:
    """The whole detection table for a Python repo, in priority order.

    :func:`detect_python_install` returns only the first entry; this returns the
    rest as well so that a failing installer can fall back down the table
    instead of walking us into a guaranteed INCONCLUSIVE. The "tool declared but
    not on PATH" refusals are deliberately *not* softened here: installing a
    poetry project with pip would install a different set of packages, which is
    a wrong answer rather than a slower one.
    """
    first, note = detect_python_install(repo, python_exe)
    if first is None:
        return [], note

    plans = [first]
    pyproject = _read(repo, "pyproject.toml") if _exists(repo, "pyproject.toml") else ""
    pip = [python_exe, "-m", "pip", "install", *_PIP_FLAGS]
    installable = _installable(repo)

    def push(plan: InstallPlan) -> None:
        if all(plan.commands != existing.commands for existing in plans):
            plans.append(plan)

    if pyproject:
        lowered = {name.lower(): name for name in optional_dependency_extras(pyproject)}
        for candidate in TEST_EXTRA_NAMES:
            if candidate in lowered:
                extra = lowered[candidate]
                push(
                    InstallPlan(
                        detector="python:pyproject-extra",
                        evidence=(
                            f"pyproject.toml declares [project.optional-dependencies].{extra} "
                            f"(matched {candidate!r} from the test-extra list)"
                        ),
                        commands=[[*pip, "-e", f".[{extra}]"]],
                        language="python",
                    )
                )
                break

    dev_req = next((f for f in DEV_REQUIREMENTS_FILES if _exists(repo, f)), None)
    base_req = "requirements.txt" if _exists(repo, "requirements.txt") else None
    if dev_req or base_req:
        args: list[str] = []
        evidence_bits: list[str] = []
        if installable:
            args += ["-e", "."]
            evidence_bits.append(f"{installable} makes the project installable (editable)")
        for rel in (base_req, dev_req):
            if rel:
                args += ["-r", rel]
                evidence_bits.append(f"{rel} is committed")
        push(
            InstallPlan(
                detector="python:requirements-dev" if dev_req else "python:requirements",
                evidence="; ".join(evidence_bits),
                commands=[[*pip, *args]],
                language="python",
            )
        )

    if installable:
        push(
            InstallPlan(
                detector="python:editable",
                evidence=f"{installable} makes the project installable",
                commands=[[*pip, "-e", "."]],
                language="python",
            )
        )
    return plans, ""


def javascript_install_plans(repo: str) -> tuple[list[InstallPlan], str]:
    """Lockfile-respecting install first, then the same manager unpinned."""
    first, note = detect_javascript_install(repo)
    if first is None:
        return [], note
    plans = [first]
    relaxed = {
        "js:pnpm": (["pnpm", "install", "--no-frozen-lockfile"], "pnpm"),
        "js:yarn": (["yarn", "install"], "yarn"),
        "js:npm-ci": (["npm", "install", "--no-audit", "--no-fund"], "npm"),
    }.get(first.detector)
    if relaxed is not None:
        argv, tool = relaxed
        plans.append(
            InstallPlan(
                detector=first.detector + "-relaxed",
                evidence=(
                    f"fallback: the lockfile-pinned {tool} install failed, so the same manager "
                    f"was retried without the frozen-lockfile constraint"
                ),
                commands=[argv],
                language="javascript",
            )
        )
    return plans, ""


def go_install_plans(repo: str) -> tuple[list[InstallPlan], str]:
    """``go mod download``, then the same thing with ``-mod=mod``.

    The fallback exists for repositories whose committed ``go.sum`` is
    incomplete or whose ``vendor/`` directory is stale: the default
    ``-mod=readonly`` refuses to touch ``go.mod``, which is the right default,
    but it turns a fixable environment into an unrunnable one. ``-mod=mod``
    lets the go command write the missing entries. It *does* modify tracked
    files, which the artefact snapshot in verify.py records and excludes from
    the restoration check, so the change is visible rather than silent.
    """
    if not _exists(repo, "go.mod"):
        return [], "no go.mod was found, so there was nothing to download"
    if not shutil.which("go"):
        return [], "go.mod is committed but `go` is not on PATH"
    plans = [
        InstallPlan(
            detector="go:mod-download",
            evidence="go.mod is committed and `go` is on PATH",
            commands=[["go", "mod", "download"]],
            language="go",
        ),
        InstallPlan(
            detector="go:mod-download-mod",
            evidence=(
                "fallback: the read-only module download failed, so it was retried with "
                "GOFLAGS=-mod=mod, which allows the go command to repair go.mod/go.sum"
            ),
            commands=[["go", "mod", "download"]],
            language="go",
            env={"GOFLAGS": "-mod=mod"},
        ),
    ]
    return plans, ""


def rust_install_plans(repo: str) -> tuple[list[InstallPlan], str]:
    """``cargo fetch``: download the dependency graph, compile nothing.

    Compilation is deliberately left to ``cargo test`` itself. Building here
    would double the wall time of the first run for no benefit, and - more
    importantly - it would produce artefacts *before* the mutation rather than
    inside it, which is the exact confusion the build-artefact policy exists to
    prevent.
    """
    if not _exists(repo, "Cargo.toml"):
        return [], "no Cargo.toml was found, so there was nothing to fetch"
    if not shutil.which("cargo"):
        return [], "Cargo.toml is committed but `cargo` is not on PATH"
    plans = [
        InstallPlan(
            detector="rust:cargo-fetch",
            evidence=(
                "Cargo.toml is committed and `cargo` is on PATH"
                + ("; Cargo.lock is committed and is honoured" if _exists(repo, "Cargo.lock") else "")
            ),
            commands=[["cargo", "fetch"]],
            language="rust",
        )
    ]
    if _exists(repo, "Cargo.lock"):
        plans.append(
            InstallPlan(
                detector="rust:cargo-fetch-unlocked",
                evidence=(
                    "fallback: the locked fetch failed, so it was retried without --locked "
                    "semantics, which permits cargo to update Cargo.lock"
                ),
                commands=[["cargo", "fetch", "--manifest-path", "Cargo.toml"]],
                language="rust",
            )
        )
    return plans, ""


def java_install_plans(repo: str) -> tuple[list[InstallPlan], str]:
    """Resolve the JVM dependency graph without running any test.

    Both commands are offline-hostile by nature: they reach a remote repository
    on first use. A failure here is reported like any other failed install -
    the run continues and the precondition check decides whether the
    environment was adequate anyway.
    """
    if _exists(repo, "pom.xml"):
        if not shutil.which("mvn"):
            return [], "pom.xml is committed but `mvn` is not on PATH"
        return (
            [
                InstallPlan(
                    detector="java:maven",
                    evidence="pom.xml is committed and `mvn` is on PATH",
                    commands=[["mvn", "-B", "-q", "-DskipTests", "test-compile"]],
                    language="java",
                ),
                InstallPlan(
                    detector="java:maven-resolve",
                    evidence=(
                        "fallback: test-compile failed, so only the dependency graph was resolved"
                    ),
                    commands=[["mvn", "-B", "-q", "dependency:resolve"]],
                    language="java",
                ),
            ],
            "",
        )
    if _exists(repo, "build.gradle") or _exists(repo, "build.gradle.kts"):
        launcher = "./gradlew" if _exists(repo, "gradlew") else "gradle"
        if launcher == "gradle" and not shutil.which("gradle"):
            return [], "build.gradle is committed but there is no gradlew and no `gradle` on PATH"
        return (
            [
                InstallPlan(
                    detector="java:gradle",
                    evidence=f"build.gradle(.kts) is committed and `{launcher}` is available",
                    commands=[[launcher, "testClasses", "--console=plain"]],
                    language="java",
                )
            ],
            "",
        )
    return [], "no pom.xml or build.gradle was found, so there was nothing to resolve"


def detect_install_chain(
    repo: str, language: str, python_exe: str
) -> tuple[list[InstallPlan], str]:
    """Every install we are willing to try, best first. See :func:`detect_install`."""
    if language == "python":
        return python_install_plans(repo, python_exe)
    if language == "javascript":
        return javascript_install_plans(repo)
    if language == "go":
        return go_install_plans(repo)
    if language == "rust":
        return rust_install_plans(repo)
    if language == "java":
        return java_install_plans(repo)
    return [], f"no dependency detection is implemented for the {language!r} runner"


def detect_javascript_install(repo: str) -> tuple[InstallPlan | None, str]:
    """Return ``(plan, note)`` for a JS/TS repository. The lockfile decides."""
    if not _exists(repo, "package.json"):
        return None, "no package.json was found, so there was nothing to install"

    table = (
        ("pnpm-lock.yaml", "pnpm", ["pnpm", "install", "--frozen-lockfile"], "js:pnpm"),
        ("yarn.lock", "yarn", ["yarn", "install", "--frozen-lockfile"], "js:yarn"),
        ("package-lock.json", "npm", ["npm", "ci", "--no-audit", "--no-fund"], "js:npm-ci"),
    )
    for lockfile, tool, argv, detector in table:
        if _exists(repo, lockfile):
            if shutil.which(tool):
                return (
                    InstallPlan(
                        detector=detector,
                        evidence=f"{lockfile} is committed and `{tool}` is on PATH",
                        commands=[argv],
                        language="javascript",
                    ),
                    "",
                )
            return (
                None,
                f"{lockfile} is committed but `{tool}` is not on PATH, so no dependency install "
                f"was attempted (installing with a different package manager would ignore the "
                f"lockfile the repo committed)",
            )

    if shutil.which("npm"):
        return (
            InstallPlan(
                detector="js:npm-install",
                evidence="package.json is committed with no lockfile",
                commands=[["npm", "install", "--no-audit", "--no-fund"]],
                language="javascript",
            ),
            "",
        )
    return None, "package.json is committed but `npm` is not on PATH"


def detect_install(repo: str, language: str, python_exe: str) -> tuple[InstallPlan | None, str]:
    """Dispatch on the language of the *already detected* test runner.

    Keying off the runner rather than re-sniffing the repo is what stops a
    polyglot repository from having its Python tests run against a freshly
    installed ``node_modules`` and nothing else.
    """
    if language == "python":
        return detect_python_install(repo, python_exe)
    if language == "javascript":
        return detect_javascript_install(repo)
    plans, note = detect_install_chain(repo, language, python_exe)
    return (plans[0] if plans else None), note


def parse_override(command: str) -> list[list[str]]:
    """Turn an explicit ``--install-command`` into something we can execute.

    A simple command is run as argv, with no shell between us and it. Anything
    using shell syntax (``&&``, a pipe, a redirect) is handed to ``/bin/sh -c``,
    because refusing it would make the escape hatch useless for the exact cases
    people reach for it.
    """
    text = command.strip()
    if not text:
        return []
    if _SHELL_META.search(text):
        return [["/bin/sh", "-c", text]]
    try:
        argv = shlex.split(text)
    except ValueError:
        return [["/bin/sh", "-c", text]]
    return [argv] if argv else []


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------


def run_plan(repo: str, plan: InstallPlan, timeout: int) -> InstallReport:
    """Run every command in the plan, stopping at the first failure."""
    rep = InstallReport(
        enabled=True,
        source="",  # filled in by the caller
        detector=plan.detector,
        evidence=plan.evidence,
        commands=plan.display,
        timeout_s=timeout,
    )
    started = time.monotonic()
    base_env = {"CI": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    base_env.update(plan.env)
    for argv in plan.commands:
        res = run(argv, cwd=repo, timeout=timeout, env=base_env)
        rep.stdout_tail = _tail(res.stdout)
        rep.stderr_tail = _tail(res.stderr)
        if res.timed_out:
            rep.status = "timeout"
            rep.duration_s = round(time.monotonic() - started, 2)
            return rep
        if res.returncode != 0:
            rep.status = "failed"
            rep.exit_code = res.returncode
            rep.duration_s = round(time.monotonic() - started, 2)
            return rep
        rep.exit_code = res.returncode
    rep.status = "ok"
    rep.duration_s = round(time.monotonic() - started, 2)
    return rep


def run_plans(repo: str, plans: list[InstallPlan], timeout: int) -> InstallReport:
    """Run each plan until one succeeds; record every attempt either way.

    A silent fallback would be worse than no fallback: the report has to say
    which installer we actually ended up using and what the first one did, or
    the user cannot reproduce the run.
    """
    if not plans:  # pragma: no cover - callers check first
        return InstallReport(enabled=True, status="none")

    rep: InstallReport | None = None
    for index, plan in enumerate(plans):
        attempt = run_plan(repo, plan, timeout)
        attempt.attempts = list(rep.attempts) if rep is not None else []
        attempt.attempts.append(
            {
                "detector": plan.detector,
                "evidence": plan.evidence,
                "commands": plan.display,
                "status": attempt.status,
                "exit_code": attempt.exit_code,
                "duration_s": attempt.duration_s,
                "stderr_tail": attempt.stderr_tail[-1200:],
            }
        )
        rep = attempt
        if attempt.status == "ok":
            if index:
                rep.notes.append(
                    f"the first {index} detected installer(s) failed; this run used the "
                    f"fallback `{plan.display[0] if plan.display else ''}`"
                )
            return rep
    assert rep is not None
    rep.notes.append(
        f"all {len(plans)} detected installer(s) failed; the last one's output is shown above "
        f"and every attempt is recorded in the report"
    )
    return rep


def _tail(text: str, limit: int = TAIL_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return "...[truncated]...\n" + text[-limit:]


def failure_reason(rep: InstallReport) -> str:
    """The INCONCLUSIVE headline for a failed install. Never swallow stderr."""
    cmd = rep.commands[0] if rep.commands else "(no command)"
    if rep.status == "timeout":
        return (
            f"the dependency install exceeded the {rep.timeout_s}s install timeout, so the tests "
            f"were never given a chance to run: `{cmd}`. Raise --install-timeout, or pre-install "
            f"the dependencies and pass --no-install-deps."
        )
    detail = (rep.stderr_tail or rep.stdout_tail or "").strip()
    if len(detail) > 1200:
        detail = "...[truncated]...\n" + detail[-1200:]
    tried = ""
    if len(rep.attempts) > 1:
        tried = "\ninstallers tried, in order: " + "; ".join(
            f"{a['detector']} (`{(a['commands'] or [''])[0]}`) -> {a['status']}"
            for a in rep.attempts
        )
    return (
        f"the dependency install failed (exit {rep.exit_code}), so any test result would have "
        f"been about a broken environment rather than about this PR: `{cmd}`{tried}\n{detail}"
    )
