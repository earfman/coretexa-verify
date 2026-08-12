# How coretexa-verify works

The mechanism, in detail: what gets selected, how the experiment is run, and how each verdict is reached. If you only want to install it, the [README](../README.md) is enough.

[← back to the README](../README.md)

---

## How it works

1. Resolve base and head, and compare against the **merge base** so commits that
   landed on `main` since the PR opened do not pollute the diff.
2. Classify every changed file as `SOURCE`, `TEST` or `OTHER`.
3. Select the tests: prefer the tests this PR actually added (see below).
4. **Run them at head. They must pass.** If they do not, everything downstream is
   meaningless → `INCONCLUSIVE`, naming the failing tests.
5. Revert only the `SOURCE` files to their base content, leaving the test changes
   in place. Run the same tests again.
6. Restore the working tree, in a `finally`, byte for byte.

### Test selection

Changed test files are handed to the runner directly. But three refinements make
the result mean what you think it means, and all of them are **verified by asking
the runner to collect them** — nothing is ever guessed:

- **Only the tests this PR added.** The diff's changed line numbers are
  intersected with the test file's AST, so a neighbouring legacy test that hits
  the network cannot turn a good run into `INCONCLUSIVE`.
- **Only the fixture cases this PR added.** For a *modified* YAML/JSON fixture,
  the top-level keys the PR added are matched against collected parametrised test
  ids. On sqlfluff this turned a 2,313-case, multi-minute suite into the 10 cases
  the PR is actually about.
- **Auto-discovery harnesses.** Many suites build their cases by *enumerating a
  fixture directory* — `glob`, `os.walk`, `os.listdir`, a `parametrize` over a
  directory listing — so the consuming module never contains the fixture's name
  and a literal search cannot find it. We therefore also search test modules for
  references to the fixture's **ancestor directories**, in every spelling
  (`test/fixtures/dialects`, `"test", "fixtures", "dialects"`, `fixtures/dialects`),
  and treat a module that both references the fixture root *and* enumerates a
  directory as a candidate consumer. Those candidates are then collected with
  `-k <fixture stem>` to find the exact parametrised cases.

#### Proof, not guesswork

Every selection entry carries a `proof` field, and the report prints it. It is
non-empty only when the link between the changed file and the tests we ran was
**established**:

1. the collected node ids are *parametrised on* the fixture's file name or stem,
   or on a case key the PR added; or
2. a **targeted probe** — the fixture is reverted on its own, with the source left
   at head, and the selection is re-run. If nothing about the result changes, the
   selection provably does not read the fixture.

A `GATE_HOLDS` needs no extra proof: the tests demonstrably reacted to the source
revert, which *is* the proof. A `NO_GATE` is the claim that nothing noticed, and
that claim is only worth something if the tests we ran are the tests that read the
changed files — so **a `NO_GATE` that rests on an unproven mapping is downgraded
to `INCONCLUSIVE`** with the reason *"changed test fixture could not be provably
mapped to a consuming test"*.

If a changed fixture cannot be mapped to a consuming test module at all, the
enclosing test directory is run instead **and the report says so** — a silent
widening of scope would change what the verdict means.

### Localisation

A whole-file revert that only breaks an *import* tells you nothing about whether
the tests can detect a behaviour change: no assertion ever ran. That happens
constantly, because a PR that adds a helper and tests the helper will always
fail to import once the helper is reverted.

So when the whole-PR revert produces a build error, coretexa-verify reverts
**one hunk at a time** and looks for a change of real code that no test notices.
Hunks that only touch comments, docstrings or formatting are excluded — proven
inert by comparing docstring-stripped ASTs, not by a regex — because reverting
them proves nothing.

Two more classes of hunk are excluded, for the same reason and with the same
bias (an exclusion can only hide a finding, never manufacture one):

- **Identifier renames.** A hunk whose entire change is a consistent token
  substitution cannot change behaviour. It is not evaluated on its own; instead
  its `{old: new}` map is kept applied when a *sibling* hunk in the same file is
  reverted, so the reverted file still compiles and the tests can express an
  opinion about the behaviour rather than about a missing symbol. Where that
  rewrite would collide with a symbol the reverted text already uses, the rename
  is reverted together with its dependants and the result is reported as gating
  that coupled group.
- **Hunks the runner cannot reach.** A `.vue` component or a `go.sum` entry is
  not something any `go test` can observe. These get the status `unreachable`,
  are never run, and are reported separately from the behavioural count.

`--localize always` runs this even when the gate holds; `--localize never`
reports only the whole-PR result.

### Language support

Detection is a small registry keyed on repository markers. Adding a language is
one detector function plus one entry in `REGISTRY`. The chosen command and the
reason for choosing it are always printed.

Detectors are tried in this order, and the first match wins:

| # | marker | runner |
|---|---|---|
| 1 | `uv.lock` + `uv` on PATH | `uv run --frozen pytest` |
| 1 | `.venv/bin/python` | `.venv/bin/python -m pytest` |
| 1 | `pyproject.toml`, `setup.py`, `setup.cfg`, `tox.ini`, `pytest.ini`, `conftest.py` | `python -m pytest` |
| 2 | `package.json` naming `vitest` | `npx vitest run` |
| 2 | `package.json` naming `jest` | `npx jest` |
| 2 | `package.json` with some other `scripts.test` | `npm test` (exit-code-only; the reduced confidence is stated in the report) |
| 3 | `go.mod` | `go test -json` |
| 4 | `Cargo.toml` | `cargo test --no-fail-fast` |
| 5 | `pom.xml` | `mvn test -Dtest=<classes>` **(experimental)** |
| 5 | `build.gradle`, `build.gradle.kts` | `./gradlew test --tests <patterns>` **(experimental)** |

Order is policy, not accident. A polyglot repository is usually an interpreted
project with a compiled extension inside it — sqlfluff is a Python package that
vendors a Rust crate — and in that shape the tests that matter are the Python
ones. Java is last because `pom.xml` turns up in repositories that are only
incidentally JVM projects.

Each runner also declares which file extensions it can actually execute, and
selection is filtered through that list. Without it, sqlfluff's genuine
`sqlfluffrs/tests/fixture_tests.rs` gets offered to pytest, collection returns
nothing, and a real `GATE_HOLDS` degrades to `INCONCLUSIVE`.

If detection fails, the verdict is `INCONCLUSIVE` and the reason names every
marker that was looked for. We never guess a command — but you can supply one:

```bash
python -m coretexa_verify --repo . --base origin/main \
  --test-command 'make check' --junit-path build/test-results
```

`--test-command` (Action input `test-command`) **replaces detection entirely**:
no detector runs and nothing about the repository's layout is assumed. It runs
from the repository root. Shell syntax goes through `/bin/sh -c`, anything
simpler runs as argv, and the PR's selected test targets are substituted for
`{targets}` if the command contains it, or appended otherwise.

Results are read one of two ways, and the report always says which:

| mode | when | precision |
|---|---|---|
| JUnit | `--junit-path` names a file or directory of JUnit XML the command writes | per-test counts, failing names, and the assertion-vs-error split — exactly as precise as a detected runner |
| exit code | no `--junit-path` | 0 = pass; non-zero is assert-vs-build by a **declared regex heuristic** over the output, and the pattern that decided it is printed in every run's `note` |

A custom command cannot be asked what it *would* run, so selection refinement,
the collected-test cap and the pre-existing-failure exclusion all switch off
rather than guess, and no build step is re-run around the mutation. Each of
those is stated in the report's warnings.

#### Go

* **Command.** `go test -json` on the package(s) owning the changed test files,
  narrowed with `-run '^(TestA|TestB)$'` to the top-level `func Test…` the diff
  touched. `-run` is only applied when *every* target is narrowed, because it
  applies to every package on the command line.
* **Results.** The JSON event stream. `Action=pass|fail|skip` **with** a `Test`
  field is a test; a terminal `fail` **without** one is a package that never
  compiled, which is `GATE_HOLDS_BUILD`. Plain-text `[build failed]` output from
  older toolchains is classified the same way. Skips are excluded from the
  executed count exactly as pytest skips are.
* **Fixtures.** `pkg/testdata/…` is mapped to `pkg`'s tests as *proof*, not a
  guess: `go help test` makes `testdata/` invisible to the build and reserved
  for its parent package's tests, and `go test` runs each binary with the
  package directory as its working directory.
* **Install.** `go mod download`, falling back to the same command under
  `GOFLAGS=-mod=mod` when a committed `go.sum` is incomplete.
* **Toolchain.** A `go`/`toolchain` directive newer than the installed
  toolchain is reported with both versions. Under `GOTOOLCHAIN=local` that is a
  loud warning and the run ends `INCONCLUSIVE`; otherwise the go command
  downloads the pinned toolchain itself and we say so.

#### Rust

* **Command.** `cargo test --no-fail-fast -p <crate>`, where the crate is the
  nearest owning `Cargo.toml` — so a workspace member is addressed the same way
  as a single crate. A changed `tests/foo.rs` becomes `--test foo`.
* **Results.** libtest's stable text output (`test a::b ... ok` and
  `test result: ok. X passed; Y failed; Z ignored`). Nightly JSON is not
  required. `cargo-nextest` is preferred when installed, but the text parser is
  the default and the one under test.
* **Compile errors.** `error[E0599]` before any `test result:` line is
  `BUILD_ERROR`, which is what makes `GATE_HOLDS_BUILD` meaningful for a
  compiled language. Cargo's own `error: could not compile … due to N previous
  errors` summary is excluded from the count, so the number in the report is the
  number of real diagnostics.
* **`#[ignore]` is a skip**, so it never contributes to the executed count.
* **Timeouts.** Compiling is the job. The default per-run timeout is 900s and is
  stated in the report; a `TIMEOUT` is always `INCONCLUSIVE`, never a finding.
* **Install.** `cargo fetch`.

#### Tests that live inside source files

Rust breaks the assumption every other language here satisfies: that a file is
either the code under test or the test. The idiomatic Rust unit test is a
`#[cfg(test)] mod tests` block at the bottom of the very file it tests. Both
Rust pull requests this release was validated against are that shape.

Reverting such a file wholesale deletes the PR's own evidence; refusing to
revert it means never running the experiment. So the cut moves *inside* the
file. `coretexa_verify.inline_tests` finds the head-side line ranges holding
test code — with a real Rust scanner, because block comments nest, raw strings
have no escapes, and `'a` is a lifetime while `'a'` is a character literal — and
the revert is then done per hunk against a zero-context diff:

* a hunk outside every test region is rolled back to base;
* a hunk inside one is left at head;
* a hunk that *straddles* a boundary is left at head and reported, because we
  cannot say which base lines belong to which half and guessing would delete a
  test;
* if nothing outside the test regions can be reverted, no revert is claimed and
  the verdict is `INCONCLUSIVE`.

The result is the base implementation carrying the PR's new tests. Localisation
applies the same filter, so a per-hunk revert can never delete a test either.
Such a file appears in *both* halves of the report — it is a source file we
revert and a test file we run — which is the honest description of it.

#### Java (experimental)

Command construction and JUnit XML reading are unit-tested against canned
output, and the XML reader is literally the one pytest uses, `<failure>` versus
`<error>` split included. What has **not** happened is an end-to-end validation
against a real JVM pull request. Treat a Java verdict accordingly; the runner
says so in its own warnings, in every report it produces.

### Dependency install

Your tests need your dependencies. Rather than making you write that step,
coretexa-verify detects it — from files **you already committed**, in a fixed
priority order, and it prints both the command it chose and the evidence for
choosing it. It never installs a package the repository did not ask for.

**Python** (first match wins):

| # | evidence | command |
|---|---|---|
| 1 | `uv.lock` committed **and** `uv` on PATH | `uv sync --frozen` |
| 2 | `poetry.lock`, or `[tool.poetry]` in `pyproject.toml`, **and** `poetry` on PATH | `poetry install` |
| 3 | `[project.optional-dependencies]` declares `test`, `tests`, `testing`, `dev`, `devel`, `develop` or `development` | `pip install -e ".[<extra>]"` |
| 4 | a dev/test requirements file: `requirements-dev.txt`, `requirements_dev.txt`, `dev-requirements.txt`, `requirements/dev.txt`, `requirements-test.txt`, `requirements_test.txt`, `requirements/test.txt`, `requirements/tests.txt` | `pip install [-e .] [-r requirements.txt] -r <file>` |
| 5 | `requirements.txt` | `pip install [-e .] -r requirements.txt` |
| 6 | `pyproject.toml`, `setup.py`, or `setup.cfg` with `[metadata]` | `pip install -e .` |
| 7 | none of the above | nothing — see below |

**JavaScript / TypeScript** — the lockfile decides:

| # | evidence | command |
|---|---|---|
| 1 | `pnpm-lock.yaml` | `pnpm install --frozen-lockfile` |
| 2 | `yarn.lock` | `yarn install --frozen-lockfile` |
| 3 | `package-lock.json` | `npm ci --no-audit --no-fund` |
| 4 | `package.json`, no lockfile | `npm install --no-audit --no-fund` |

Notes on the specifics, because they are deliberate:

- **The interpreter is the runner's, not ours.** `pip` is invoked as
  `<the python that will run your tests> -m pip`, so we can never install into
  one environment and test in another.
- **`-e`, always.** An editable install is what makes reverting a source file
  something the tests actually see. A non-editable install would test a
  *copy* of your code and quietly invalidate the whole experiment.
- **Tiers 4 and 5 also install the project itself** when the repo is an
  installable distribution, because a requirements file lists your test tooling,
  not your package, and your tests almost certainly import your package.
- **`uv sync --frozen`** rather than plain `uv sync`, so the install cannot
  rewrite your committed `uv.lock`.
- **If the tool a lockfile names is not on PATH we decline** rather than
  substituting a different one — installing a `pnpm-lock.yaml` project with npm
  would ignore the lockfile you committed. The reason is printed.
- **Detecting nothing is not an error.** We carry on. If the tests then fail to
  run, the verdict is `INCONCLUSIVE` and the headline says no dependency install
  was detected, and why.

Three controls, identical on the CLI and as Action inputs:

```yaml
      - uses: earfman/coretexa-verify@v1
        with:
          install-deps: false                # use the environment as found
          install-command: make test-deps    # or: override detection entirely
          install-timeout: 900
```

```bash
python -m coretexa_verify --repo . --no-install-deps
python -m coretexa_verify --repo . --install-command 'make test-deps'
```

`install-command` wins over detection; `install-deps: false` wins over both.
Shell syntax in `install-command` (`&&`, pipes, redirects) is run through
`/bin/sh -c`; anything simpler runs as argv with no shell in between.

#### Build artefacts

`pip install -e .` writes `*.egg-info/`; builds write `build/`, `dist/`,
`__pycache__/`; `npm install` writes `node_modules/` and possibly a
`package-lock.json`. Those files must never be confused with your edits or with
our mutation, whether or not your repo gitignores them. The policy:

1. **The dirty-tree refusal runs first**, on the tree exactly as we found it.
   Nothing an install generates can cause a refusal to start.
2. **Artefacts are identified by snapshot, not by name.** We diff `git status`
   from immediately before and after the install. No pattern list, and no
   assumption that you gitignore anything.
3. **The post-install snapshot becomes the baseline** for the "did we put
   everything back?" check that runs after every revert, so a file the install
   touched can never be reported as our leftover.
4. **We never revert, clean or delete an artefact.** It was not ours to create.
   We list what appeared and leave it exactly there.

For a compiled language the same question has a different answer, and it is
worth stating rather than assuming. `go test` and `cargo test` **are** the
build: each compiles the package or crate from source on every invocation, keyed
by a content hash (Go) or a fingerprint including each source file (Cargo), so a
reverted file is recompiled before the test binary is linked. There is no
artefact directory a test could read instead — Go has no `dist/`, and `build.rs`
output, `include_str!` data and `CARGO_BIN_EXE_*` binaries are all regenerated
by that same invocation. `mvn test` and `gradle test` do have a separate compile
step, but their test goal already depends on it. So all four compiled runners
report "no build step" and "no artefact risk", and both are claims about the
toolchain rather than gaps in the analysis.

JavaScript is the one language here that genuinely needs the build re-run:
`dist/` outlives the source it was built from. `verify.py` therefore asks the
*runner* for a build step rather than switching on a language name, so every
"no build step" in a report is traceable to a runner that made that claim about
its own toolchain.

Bytecode caches get the same treatment, and it matters more than it sounds:
CPython validates a `.pyc` on the source file's mtime and size alone, and
reverting a hunk very often leaves the byte count unchanged (`return 1` for
`return 2`). A stale cache could therefore hand the *head* implementation to the
*base* run and turn `GATE_HOLDS` into a confident, wrong `NO_GATE`. Every runner
subprocess gets `PYTHONDONTWRITEBYTECODE=1` and a `PYTHONPYCACHEPREFIX` pointing
at an empty scratch directory outside your repository, so no cache — ours or one
already lying around — can ever answer for the source.

### Test-file classification

Configurable, with defaults that work: paths containing `test/`, `tests/`,
`spec/`, `__tests__/`, `unit_tests/`, `*testsuite/`; files matching `test_*.py`,
`*_test.py`, `*Test.py`, `conftest.py`, `*.test.[jt]sx?`, `*.spec.[jt]sx?`,
`*_test.go`, `*Test.java`, `*Tests.java`, `*IT.java`, `*Test.kt`. A `.rs` file
sitting *directly* in a crate's `tests/` directory is an integration test —
nested ones such as `tests/common/mod.rs` are shared modules, not cargo targets,
and are deliberately not offered to `cargo test --test`. Fixture and snapshot data under a
test directory counts as `TEST`.

The separator requirement means `latest/` and `contest/` are *not* mistaken for
test directories.

Override per repository with `.coretexa-verify.toml`:

```toml
[classify]
executable_test_patterns = ["test_*.py", "check_*.py"]
force_source_globs = ["tests/support/production_shim.py"]
```

---

---

## Command line

```bash
# analyse a local checkout
python -m coretexa_verify --repo . --base origin/main --head HEAD

# analyse a GitHub PR: clones and fetches it for you, over plain git/HTTPS.
# No api.github.com required.
python -m coretexa_verify --pr https://github.com/sqlfluff/sqlfluff/pull/8201

# machine-readable
python -m coretexa_verify --repo . --base origin/main --json
```

Useful flags: `--json`, `--markdown`, `--timeout`, `--localize`, `--no-refine`,
`--test-glob`, `--source-glob`, `--fail-on`, `--runner-arg`, `--max-targets`,
`--max-collected`.

Dependency install: `--install-deps` / `--no-install-deps` (default: on),
`--install-command CMD`, `--install-timeout SECONDS` (default: 600). These are
the same three controls as the Action inputs, under the same names.

### Give the target repository its own `.venv`

**On the command line, prefer a repo-local environment.** The pytest runner picks
its interpreter in this order: `uv run` (when `uv.lock` is committed and `uv` is
on `PATH`), then `.venv/bin/python`, then `venv/bin/python`, and only then the
interpreter coretexa-verify itself is installed in. That last case is a real
decision with a real consequence: `--install-deps` (on by default) will install
the *target repository's* test dependencies into **your** environment — the same
one the tool lives in.

The tool says so loudly when it happens: the runner reason names the actual
interpreter path rather than a comfortable-looking `python -m pytest`, and a
warning appears in the report. To avoid it entirely:

```bash
cd /path/to/target-repo
python -m venv .venv          # coretexa-verify will find and use this
python -m coretexa_verify --repo . --base origin/main --head HEAD
```

Or pass `--no-install-deps` and prepare the environment yourself. In the GitHub
Action this is a non-issue: the runner is disposable.

---

---

## Action inputs and outputs, in full

## Quick start (GitHub Action)

Paste one file. There is no step to write.

```yaml
# .github/workflows/coretexa-verify.yml
name: coretexa-verify

on: pull_request

permissions:
  contents: read          # read-only: nothing here can write to your repo

jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0    # required: we need the merge base

      - uses: earfman/coretexa-verify@v1
```

The verdict goes to the job summary and to the step outputs. No token, no write
permission, nothing that can modify your repository — which is what you want on
a public repo that accepts pull requests from forks.

**Optional: post the verdict as a PR comment instead.** This needs write access
to pull requests, so add it deliberately rather than by default:

```yaml
permissions:
  contents: read
  pull-requests: write     # only for the comment

# ...
      - uses: earfman/coretexa-verify@v1
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
          # fail-on: no-gate    # uncomment to make NO_GATE block the merge
```

<details><summary>What changed in 1.1.0 — before and after</summary>

Until 1.1.0 you had to set up a language toolchain and install your own test
dependencies before the Action could do anything. That step was the wall most
people never got over, because getting it exactly right meant duplicating your
existing CI job:

```yaml
      # BEFORE (1.0.x) — you had to write these three steps yourself
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e ".[dev]"      # ...and get it right for YOUR repo
      - uses: earfman/coretexa-verify@v1
```

```yaml
      # AFTER (1.1.0)
      - uses: earfman/coretexa-verify@v1
```

The Action now detects your project's own declared test dependencies and
installs them, using the interpreter already on the runner. It reads only files
you already committed, and it always prints the command it chose and the
evidence that chose it. See [Dependency install](#dependency-install).

</details>

It writes the verdict to the job summary, sets outputs, and keeps **one** PR
comment that it updates in place — never a new comment per push.

**It is non-blocking by default.** A tool that fails builds on its first day
gets turned off on its second. Opt in with `fail-on` when you trust it.

### Inputs

| input | default | meaning |
|---|---|---|
| `base-ref` | the PR's base branch | ref to compare against |
| `head-ref` | `HEAD` | ref to analyse |
| `fail-on` | `never` | `never`, `no-gate`, `no-gate-or-inconclusive`, `not-gate-holds` |
| `timeout` | `900` | per test-run timeout, in seconds |
| `localize` | `auto` | `auto`, `always`, `never` — see [Localisation](#localisation) |
| `max-collected` | `500` | refuse to run when a widened selection collects more than this many tests |
| `install-deps` | `true` | detect and install the repo's own test dependencies — see [Dependency install](#dependency-install) |
| `install-command` | *(none)* | explicit install command; overrides detection entirely |
| `install-timeout` | `600` | timeout for each install command, in seconds |
| `github-token` | *(none)* | enables the PR comment; needs `pull-requests: write` |
| `comment` | `true` | set `false` to skip the comment even with a token |
| `working-directory` | the workspace | repository root to analyse |
| `test-globs` / `source-globs` | *(none)* | force classification for specific paths |

### Outputs

`verdict`, `details` (one-line explanation), `json` (the full report).

---
