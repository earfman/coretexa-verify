# coretexa-verify

**Can this pull request's own new tests actually fail?**

A PR says it fixes a bug and adds tests. coretexa-verify puts the source code
back the way it was, leaves the new tests in place, and runs them again. If they
still pass, the tests do not detect the thing the PR changed — and now you know
that before you merge, not six months later.

It answers exactly one question and reports one of five verdicts. Everything it
says, it established by running something.

```
==============================================================================
[!!] NO_GATE
==============================================================================
3 of 5 behavioural change(s) in this PR can be reverted with all 5 of its
tests still passing: cmscontrib/loaders/italy_yaml.py hunk 2 (head lines
1318-1326) ...
```

- Runs entirely on **your** runner. We never execute your code on our infrastructure.
- **Zero dependencies.** Pure Python standard library, so the Action installs nothing.
- No telemetry, no analytics, no network access beyond git and (optionally) the
  GitHub API with the token you hand it.

---

## Quick start (GitHub Action)

Paste one file. There is no step to write.

```yaml
# .github/workflows/coretexa-verify.yml
name: coretexa-verify

on: pull_request

permissions:
  contents: read
  pull-requests: write   # only needed for the PR comment

jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0            # required: we need the merge base

      - uses: earfman/coretexa-verify@v1
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
          # fail-on: no-gate     # uncomment to make NO_GATE block the merge
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

## The verdicts

| verdict | meaning |
|---|---|
| `NO_GATE` | Tests pass at head **and** pass with the source reverted. **The headline finding: this PR's tests would pass without the fix.** |
| `GATE_HOLDS` | Tests pass at head and *assert-fail* with the source reverted. Healthy. |
| `GATE_HOLDS_BUILD` | Reverting the source makes the tests fail to **build/import/collect** rather than assert-fail. Still a real gate — and the only possible shape for type-level or trait-level fixes — but reported distinctly, never silently as `GATE_HOLDS`. |
| `NO_NEW_TESTS` | Source changed; no test file was added or modified. Not a failure, but worth saying out loud. |
| `INCONCLUSIVE` | The experiment could not be run: tests do not pass at head, no tests could be selected, no runner could be detected, or a step errored. Always says which. |

`INCONCLUSIVE` is a legitimate answer and is used in preference to guessing.

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

## Verified against real pull requests

Every verdict below is real output from the CLI, not a mock-up.

### `NO_GATE` — [ioi-isr/cms#174](https://github.com/ioi-isr/cms/pull/174)

```bash
python -m coretexa_verify --repo /path/to/cms --base origin/main --head pr174
```

```
[!!] NO_GATE

3 of 5 behavioural change(s) in this PR can be reverted with all 5 of its tests
still passing: cmscontrib/loaders/italy_yaml.py hunk 2 (head lines 1318-1326);
cmscontrib/loaders/italy_yaml.py hunk 3 (head lines 1339-1345);
cmscontrib/loaders/italy_yaml.py hunk 4 (head lines 1348-1362).
This PR's tests would pass without that change.

per-hunk localisation (each hunk reverted on its own)
  GATED     cms/grading/tasktypes/BatchAndOutput.py hunk 1   ASSERT_FAIL: 3 passed, 2 failed
  GATED     cmscontrib/loaders/italy_yaml.py hunk 1          BUILD_ERROR: 0 passed, 1 errored
  UNGATED   cmscontrib/loaders/italy_yaml.py hunk 2          PASS: 5 passed
  UNGATED   cmscontrib/loaders/italy_yaml.py hunk 3          PASS: 5 passed
  UNGATED   cmscontrib/loaders/italy_yaml.py hunk 4          PASS: 5 passed
```

The PR is named after the loader change. Its five new tests cover the new helper
function and a compatibility shim — but the loader hunk itself can be thrown away
and all five still pass.

### `GATE_HOLDS` — [sqlfluff/sqlfluff#8201](https://github.com/sqlfluff/sqlfluff/pull/8201)

```bash
python -m coretexa_verify --repo /path/to/sqlfluff --base origin/main --head pr8201
```

```
[ok] GATE_HOLDS

Reverting src/sqlfluff/rules/structure/ST05.py makes 6 of the PR's test(s) fail:
the tests really do gate the change.

test selection
  test/fixtures/rules/std_rule_cases/ST05.yml
    -> 10 x test/rules/yaml_test_cases_test.py::test__rule_test_case[ST05_...]
       [fixture-map+added-cases]
       test files containing the literal 'std_rule_cases'; narrowed to the 10
       collected case(s) matching the 10 fixture key(s) this PR added

run at head:              PASS         - 10 passed in 3.36s
run with source reverted: ASSERT_FAIL  - 4 passed, 6 failed in 3.3s
```

The PR's tests are YAML fixtures, not test modules. They were mapped to
`test/rules/yaml_test_cases_test.py` and narrowed to the ten cases the PR added.

### `GATE_HOLDS_BUILD` — [QuantEcon/QuantEcon.py#905](https://github.com/QuantEcon/QuantEcon.py/pull/905), `--localize never`

```bash
python -m coretexa_verify --repo /path/to/quantecon --base origin/main \
    --head pr905 --localize never
```

```
[ok] GATE_HOLDS_BUILD

Reverting quantecon/util/notebooks.py stops 1 test(s) from building/importing
rather than assert-failing. That is still a real gate, but no assertion was
exercised.
```

The test module imports `TIMEOUT`, a constant the PR introduced, so a whole-file
revert cannot even reach an assertion. This is exactly why localisation exists:
with the default `--localize auto` the same PR drills down per hunk and reports
`GATE_HOLDS`, because each behavioural hunk really is detected.

### `NO_NEW_TESTS` — sqlfluff commit `a83c069`

```bash
python -m coretexa_verify --repo /path/to/sqlfluff \
    --base a83c069^ --head a83c069
```

```
[--] NO_NEW_TESTS

1 source file(s) changed and no test file was added or modified.

changed files
  M SOURCE src/sqlfluff/core/helpers/string.py
```

### `INCONCLUSIVE` — QuantEcon#905 with refinement disabled

```bash
python -m coretexa_verify --repo /path/to/quantecon --base origin/main \
    --head pr905 --no-refine
```

```
[??] INCONCLUSIVE

the PR's tests do not pass at head (4 passed, 1 failed, 2 errored), so reverting
the source proves nothing. Failed:
quantecon.util.tests.test_notebooks.TestNotebookUtils::test_fetch_nb_dependencies
```

That test file contains legacy tests that fetch live URLs, which fail in a
sandbox. The tool refuses to produce a verdict rather than blaming the PR for
its neighbours. (With refinement on — the default — only the four tests the PR
added are selected and the verdict is `GATE_HOLDS`.)

---

## What this tool cannot tell you

Read this part. It is short and it is the honest bit.

**It does not measure test quality.** `GATE_HOLDS` means one specific revert was
detected. It does not mean the tests are good, thorough, or check the right
thing. A test asserting `result is not None` will happily produce `GATE_HOLDS`.

**It does not measure coverage.** With `--localize auto` (the default) we stop as
soon as the whole-PR revert assert-fails. Some individual hunk inside that PR may
still be untested. Use `--localize always` if you want every hunk probed.

**`NO_GATE` is not proof of a bad PR.** Legitimate reasons for it: refactors,
performance work, logging, defensive branches, changes covered by an existing
test the PR did not touch, and fixes whose tests live somewhere the tool did not
select. Read the hunks it names.

**`GATE_HOLDS_BUILD` is weaker evidence than `GATE_HOLDS`.** It often means "the
test imports a symbol this PR introduced", which gates the *presence* of the new
code rather than its behaviour. That is why it is a separate verdict.

**Hunk-level reverts can produce nonsense intermediate states.** Reverting one
hunk of a coordinated multi-hunk change can yield code that was never valid.
That biases toward `GATED` (things break) rather than a false `NO_GATE`, but it
means a `GATED` hunk result is not proof the hunk is well tested.

**It only reverts, never mutates.** Classic mutation testing perturbs operators
and constants. This tool asks one narrower question: does the PR's diff matter to
the PR's tests? A test suite that survives real mutants but catches the diff will
still read as `GATE_HOLDS`.

### PR shapes that produce `INCONCLUSIVE`

- Tests that do not pass at head in the CI environment: flaky, network-dependent,
  or requiring services the workflow did not start. **This is the most common
  cause by far.**
- No runner detected. Five toolchains are recognised — pytest, jest/vitest/npm,
  `go test`, `cargo test` and Maven/Gradle (experimental). Anything else (C/C++,
  Ruby, .NET, a Makefile, Bazel) needs `--test-command` / the `test-command`
  Action input, which replaces detection entirely; without one the answer is
  `INCONCLUSIVE` and the error names the markers it looked for.
- A changed fixture that cannot be mapped to a consumer and has no enclosing test
  directory.
- A changed fixture whose mapping to a consuming test could not be *proven* —
  see [Proof, not guesswork](#proof-not-guesswork). Downgraded from `NO_GATE`,
  never reported as one.
- Selection that widens to more than `--max-targets` (default 50) whole
  files/directories, or that **collects** more than `--max-collected` tests
  (default 500). The second limit is the one that matters: a single bare
  directory target is one argument and six thousand tests.
- Tests that execute build output where no build step could be detected, when the
  verdict would otherwise have been `NO_GATE`.
- **Selected tests that all skip.** pytest and vitest exit 0 when 100% of the
  selected tests skip, so `0 passed, N skipped` looks like success. No verdict
  may rest on a run that executed nothing, so this is `INCONCLUSIVE` with the
  count. Where *some* tests skipped, every headline says how many.
- Any run that times out; the timeout is reported, never swallowed.
- A dirty working tree — we refuse to start rather than risk not restoring it.
- A shallow clone with no merge base (use `fetch-depth: 0`).

## Known issues

### Fixed in 1.3.1

The 1.3.0 field test surfaced three defects, all of them about *what gets
counted*, and all three are now closed.

**A rename coupled to a behaviour change looked like a build-only gate —
fixed.** On [gatus#1719](https://github.com/TwiN/gatus/pull/1719) hunk 1 renames
`ErrNoEndpointOrSuiteInConfig` to `ErrNoEndpointOrSuiteOrRemoteInConfig` and
hunk 2 both changes the validation condition *and* uses the renamed symbol.
Reverting either hunk alone leaves a dangling identifier, so both came back
`BUILD_ERROR` and 1.3.0 concluded "the tests gate the presence of the new code,
not its behaviour". They gate its behaviour: keep the rename and revert only the
condition and `TestParseAndValidateOnlyRemote` fails an assertion.

Localisation now detects a hunk whose entire change is a consistent identifier
substitution (string and comment content masked, since a rename drags its own
error message along). Such a hunk is *inert* — reverting a rename cannot change
behaviour — and its `{old: new}` map is re-applied to the base lines spliced in
when any sibling hunk in the same file is reverted, so the file still compiles.
Where the rewrite would merge two distinct symbols, the rename is reverted
*with* its dependants as one group and the headline says so rather than
claiming presence-only. gatus#1719 on 1.3.1:

```
[ok] GATE_HOLDS
Every one of the 1 evaluated behavioural change(s) in this PR is detected by
its own tests (1 by an assertion, 0 at import time). 1 of them had to be
reverted with the identifier rename(s) ErrNoEndpointOrSuiteInConfig ->
ErrNoEndpointOrSuiteOrRemoteInConfig kept applied so the file still compiled.
```

**A head failure that pre-dates the PR blocked every verdict — fixed.**
superfile [#1519](https://github.com/yorukot/superfile/pull/1519) and
[#1560](https://github.com/yorukot/superfile/pull/1560) both stopped at "the
PR's tests do not pass at head". The failing test was `TestZoxide`, which fails
identically on a clean `origin/main` because the machine has no `zoxide` binary.

When the head run fails, the failing ids are now re-run with **source and tests
both at the merge base**, and sorted three ways:

- failing at base too → pre-existing. Excluded from the gate set, reported as
  `pre-existing failures (N), excluded`, and the experiment proceeds on what is
  left (subject to the usual "something must actually have executed" rule).
- passing at base → this PR broke it. Still `INCONCLUSIVE`, now with the cause.
- absent at base → the PR's *own new tests* are the ones failing. Still
  `INCONCLUSIVE`, and that is a finding about the PR rather than a limitation
  of ours — superfile [#1534](https://github.com/yorukot/superfile/pull/1534),
  whose new SSH tests are genuinely flaky, still stops here and says why.

**Unreachable hunks diluted `NO_GATE` headlines — fixed.**
[gatus#1725](https://github.com/TwiN/gatus/pull/1725) reported "19 of 34
behavioural changes", but 17 of the 34 were `.vue`, `.html`, `.json` and bundled
frontend JS. No `go test` executes a Vue component, so those hunks were never
candidates for a gate.

A runner may now declare which file extensions its tests can observe (only the
Go runner does; a Python suite genuinely does execute `.sql` fixtures and render
`.html` templates, so it declares nothing and behaves exactly as before), and
dependency manifests and lock files — `go.mod`, `go.sum`, `*.lock`,
`package-lock.json` — are unreachable for every runner. Files under `testdata/`
and `resources/` stay reachable whatever their extension, because tests open
them by path. Unreachable hunks get the new per-hunk status `unreachable`
(`outcome: NOT_RUN` in the JSON, additive), are never reverted, never counted,
and always listed. gatus#1725 on 1.3.1:

```
[!!] NO_GATE
2 of 17 evaluated behavioural change(s) in this PR can be reverted with all 63
of its tests still passing: config/config.go hunk 1 (head lines 341-350);
security/oidc.go hunk 6 (head lines 132-149). This PR's tests would pass
without that change. 17 further change(s) are outside the reach of the detected
test runner (frontend assets (.html, .js, .json, .vue)) and were not evaluated.
```

Excluding a hunk can only ever remove a `NO_GATE` finding, never invent one, so
all three exclusions take the same direction as the existing inertness rule —
and every excluded hunk is printed in the report so the choice is auditable.

### Fixed in 1.2.0

The 1.1.0 field test surfaced two classes of defect that are now closed.

**False `NO_GATE` from a guessed fixture mapping — fixed.** In 1.1.0 a changed
fixture was mapped to a consuming test by literal search alone. On
[sqlfluff#8221](https://github.com/sqlfluff/sqlfluff/pull/8221) — which adds
`test/fixtures/dialects/clickhouse/exchange.sql|yml` plus the ClickHouse dialect
support that makes them parse — that search matched
`test/dialects/clickhouse_test.py`, eleven tests that pass whether or not the fix
is present, and 1.1.0 emitted a confident `NO_GATE`. The real consumer is
`test/dialects/dialects_test.py`, which auto-discovers `test/fixtures/dialects/**`
and contains the literal `clickhouse` exactly zero times.

Three changes close this:

1. **Harness discovery.** Test modules are searched for references to the
   fixture's *ancestor directories* and for directory-enumerating constructs, so
   `dialects_test.py` is found even though it never names the fixture.
2. **Proof-carrying selection.** A mapping counts only when the collected node ids
   are parametrised on the fixture's name/stem or on a case key the PR added.
3. **A targeted probe.** If a mapping is still unproven and the verdict would be
   `NO_GATE`, the fixture is reverted *on its own* with the source left at head
   and the selection is re-run. No change in outcome means the selection does not
   read the fixture, and the verdict degrades to `INCONCLUSIVE` — never `NO_GATE`.

sqlfluff#8221 re-run on 1.2.0:

```
[ok] GATE_HOLDS
Reverting src/sqlfluff/dialects/dialect_clickhouse.py,
src/sqlfluff/dialects/dialect_clickhouse_keywords.py makes 3 of the PR's
test(s) fail: the tests really do gate the change.

test/fixtures/dialects/clickhouse/exchange.sql
  -> test/dialects/dialects_test.py::test__dialect__base_file_parse[clickhouse-exchange.sql],
     test/dialects/dialects_test.py::test__dialect__base_broad_fix[clickhouse-exchange.sql],
     test/dialects/dialects_test.py::test__dialect__base_parse_struct[clickhouse-exchange.sql-True-exchange.yml],
     ...                                       [fixture-map+harness+named-cases]
     proof: the collected test id(s) are parametrised on the fixture file name
            'exchange.sql'
```

23 s, three real failures on revert — the same three the PR author would see from
`pytest test/dialects/dialects_test.py -k exchange`.

**JS/TS monorepos returning `INCONCLUSIVE` — fixed.** 1.1.0 ran every JavaScript
suite from the repository root, where a workspace package's runner config does not
apply, so every monorepo suite failed to transform before a single assertion ran.
1.2.0 detects workspaces (`workspaces` in package.json, `pnpm-workspace.yaml`),
maps each changed test to its **owning package** (nearest ancestor package.json),
runs the tests from that package directory with that package's own config, and
detects the repo's build script (`build:all`, `build`, …) — running it before the
head run **and again inside every revert**, so tests that import build output see
the reverted source. modelcontextprotocol/typescript-sdk
[#2540](https://github.com/modelcontextprotocol/typescript-sdk/pull/2540) and
[#2550](https://github.com/modelcontextprotocol/typescript-sdk/pull/2550) both
reach `GATE_HOLDS` out of the box, with no `--install-command` and no
`--runner-arg`; #2550 needed manual flags in 1.1.0 and was `INCONCLUSIVE` even
with them.

Also fixed: the installer now falls back down the detection table when the
first-choice installer fails (and reports every attempt); a widened selection is
bounded by **collected test count** rather than target count, which turns
sqlfluff#8222's 939-second timeout into a 71-second `GATE_HOLDS`; running as
uid 0 is warned about; and a deletion-only PR's `NO_GATE` headline says so.

### Limitations

These are real and they remain.

- **Tests split across files.** If the PR changes `src/a.py` and adds
  `tests/test_b.py` that only covers something else, we run the changed test file
  and may report `NO_GATE` when the real gate is an existing untouched test. The
  question we answer is about the PR's *own* tests, by design — but the headline
  can read as a broader claim than it is.
- **Root user.** Running as uid 0 makes every permission-based assertion
  unfailable, so such a test cannot detect a revert. We warn; we cannot fix it.
  Run the Action as a non-root user if your suite tests permissions.
- **System build dependencies.** A wheel needing C headers (`psycopg2` wants
  libpq, `pycups` wants CUPS) will not build on a bare runner. The fallback chain
  makes this fail *informatively* — both attempts are in the report — but it does
  not make the tests runnable. Add an `apt-get` step.
- **conda / mamba environments** are not detected at all. Use `install-command`.
- **Languages with no detector.** Five are detected: Python (pytest),
  JavaScript/TypeScript (jest, vitest, `npm test`), Go (`go test -json`), Rust
  (`cargo test`) and the JVM (Maven/Gradle, **experimental** — see below).
  Anything else — C/C++, Ruby, .NET, Bazel, a bare Makefile — is reached with
  `--test-command`, which overrides detection entirely. That path classifies
  results from the exit code with a declared heuristic unless the command writes
  JUnit XML at `--junit-path`, in which case it is as precise as a detected
  runner. Adding a real detector is still one function plus a registry entry.
- **The JVM runner is experimental.** Maven and Gradle are detected and their
  Surefire/Gradle JUnit XML is read, but far fewer real pull requests have been
  put through it than through the other four. Treat its verdicts as indicative.
- **A custom `--test-command` has no build step and cannot enumerate.** We do not
  know how to ask an arbitrary command what it would run, so selection refinement,
  the collected-test cap and the pre-existing-failure exclusion all switch off
  rather than guess, and no build is re-run around the mutation. All of this is
  said in the report's warnings.
- **Build artefacts beyond what we re-run.** We re-run a *detected* build step
  around every mutation, and we refuse `NO_GATE` when the selected tests import
  build output and no build step was found. Two gaps remain: a build produced by
  something we do not recognise as a build script (a Makefile, a custom shell
  step, `cargo build` behind a Python extension), and an incremental build whose
  cache does not invalidate on the reverted file. Compiled Python sources
  (`.pyx`, `.c`, `.rs`) are flagged as a risk but there is no Python build step
  we re-run, so those PRs are refused rather than answered.
- **JavaScript refinement is absent.** jest and vitest cannot cheaply enumerate
  tests for us, so JS/TS runs execute the whole changed test file. Legacy tests in
  that file can cause `INCONCLUSIVE`. This is stated in the report's warnings.
- **The `npm test` fallback is exit-code-only.** The assert-vs-build distinction
  there is a declared heuristic on output patterns, not structured data. The
  report says so in the `note` field. Do not trust `GATE_HOLDS_BUILD` from that
  runner as strongly.
- **Type-only and compile-level fixes** can only ever produce
  `GATE_HOLDS_BUILD`, and JS runners do not typecheck at all, so a TypeScript
  type fix may look like `NO_GATE` under vitest/jest. Run `tsc` separately.
- **Multi-package selections.** When a PR's changed tests span two workspace
  packages we fall back to running from the repository root, which is the shape
  that used to fail. Such a PR may still be `INCONCLUSIVE`.
- **Non-hermetic tests.** A test that writes into the repository, caches to disk,
  or depends on run order can make the second run differ from the first for
  reasons that have nothing to do with the revert.
- **Generated code and lockfiles** are classified `SOURCE` and reverted. For a PR
  that regenerates a large artifact, the revert is real but the result is rarely
  interesting.
- **Renames and whole-file additions are skipped during localisation**, because
  there is no meaningful intermediate state to revert one hunk of.
- **Skipped tests are not evidence.** We refuse a verdict when nothing executed
  and we report the skip count when some did, but we cannot tell you *why* a
  test skipped or make it run. A suite that skips most of itself on your runner
  is testing much less than its name suggests.
- **Per-hunk runs that break the runner cannot be judged.** If reverting one hunk
  in isolation produces code the runner refuses to start on (pytest exit 4, no
  report), that hunk is reported `UNKNOWN` and excluded from every count. It is
  neither counted as gated nor as ungated, so a coordinated multi-hunk change may
  leave part of the diff genuinely unevaluated.
- **Harness discovery is bounded.** A module qualifies only if it references an
  ancestor directory of the fixture *and* contains a directory-enumerating
  construct. A harness that computes its fixture root from configuration, or that
  lives more than one `conftest.py` hop away from its tests, will not be found —
  in which case the probe refuses `NO_GATE` rather than guessing.

### Repo shapes where the dependency install will not help

Auto-install covers the common cases, not all of them. Where it cannot, it says
so and the verdict is `INCONCLUSIVE` rather than wrong — but you will want
`install-command` or `install-deps: false` plus your own step:

- **System build dependencies.** A wheel that needs C headers (`pycups` wants
  CUPS, `psycopg2` wants libpq) fails to build on a bare runner. This is real:
  `ioi-isr/cms` cannot `pip install -e ".[devel]"` without `libcups2-dev`.
  Add an `apt-get install` step and keep auto-install on — it does not have to
  succeed for the run to work, only for the tests to become runnable.
- **Services and databases.** Nothing we install starts a Postgres.
- **conda / mamba environments.** Not detected at all; use `install-command`.
- **A test extra under a name we do not recognise.** We match seven spellings.
  A repo whose extra is `ci`, `qa` or `all` falls through to a plain
  `pip install -e .` and its test tooling is not installed.
- **`testutils`-style extras that are not the dev environment.** We deliberately
  do not match these, so a repo whose *only* extra is one of them and which has
  no requirements file gets `pip install -e .` and nothing more.
- **Poetry groups other than the default**, and `poetry install --with docs`
  style invocations. We run a plain `poetry install`.
- **Yarn Berry (v2+)** rejects `--frozen-lockfile`; it wants `--immutable`. A
  yarn 2/3/4 repo needs `install-command: yarn install --immutable`.
- **Monorepos and workspaces.** We install at the repository root, which is
  correct for pnpm/yarn/npm workspaces — the installer is workspace-aware and
  links every package. Since 1.2.0 the *tests* are then run from the package that
  owns them. A package with its own manifest outside the declared workspace globs
  is still not installed.
- **Multi-language repos.** We install for the language of the *detected test
  runner* only. A Python repo whose tests shell out to a JS build gets the
  Python half and nothing else.
- **Offline or index-restricted runners.** Every tier except `uv`/`poetry`
  reaches an index. On an air-gapped runner, use `install-deps: false`.
- **Constraint files, `PIP_*` environment pinning, and custom indexes** are
  respected only insofar as pip reads them from the environment; we add no flags
  of our own beyond `--disable-pip-version-check` and `--no-input`.
- **`requirements.txt` that is a lockfile for the app, not the tests.** If the
  test tooling lives somewhere we do not look, the install succeeds and the
  tests still cannot run.
- **A tracked file the install regenerates** (setuptools_scm's `_version.py`)
  stays modified after we finish, by design — reverting it would be us editing
  your environment. A *second* run then starts on a dirty tree and is refused.
  That refusal is correct but surprising; commit or ignore the file.

---

## Security

This Action runs a pull request's own test suite, its own build step and its own
dependency installer. All three are code the PR author wrote. Treat it exactly as
you would any other workflow that executes untrusted code.

### Token isolation

Since 1.3.2, **no credential in the job's environment is passed to any
subprocess that runs repository-controlled code.** Before the installer, the
build step, the test runs and the collection pass are launched, the environment
is copied and every credential-shaped variable is removed:

- by exact name: `INPUT_GITHUB_TOKEN`, `GITHUB_TOKEN`, `GH_TOKEN`,
  `ACTIONS_RUNTIME_TOKEN`, `ACTIONS_ID_TOKEN_REQUEST_TOKEN`,
  `AWS_SECRET_ACCESS_KEY`, `NPM_TOKEN`, `NODE_AUTH_TOKEN`, `PGPASSWORD`,
  `SSH_AUTH_SOCK` and friends;
- by shape: any name ending in `TOKEN`, `SECRET`, `PASSWORD`, `PASSPHRASE`,
  `CREDENTIAL(S)`, `API_KEY`, `ACCESS_KEY`, `PRIVATE_KEY`, `AUTH` or `PAT` at a
  word boundary (so `MY_SERVICE_TOKEN` goes, `TOKEN_URL` stays).

Everything else is preserved, because a test suite legitimately needs `PATH`,
`HOME`, `LANG` and its language's caches. The report prints how many variables
were withheld and their **names only** — never a value.

git is deliberately *not* isolated: it is our own code and may need the job's
credentials to fetch a base ref from a private repository. The comment token is
read once, in our own process, after the experiment has finished.

If a test genuinely needs one of these variables — an integration test against a
real service — name it explicitly:

```yaml
env:
  CORETEXA_VERIFY_ALLOW_ENV: MY_SERVICE_TOKEN,OTHER_TOKEN
```

This is per-name and opt-in. There is no switch that disables isolation wholesale.

### Least-privilege permissions

The Action needs nothing beyond reading the code it is analysing. Give it only
that, and only add `pull-requests: write` if you want the PR comment:

```yaml
permissions:
  contents: read
  pull-requests: write   # only if you pass github-token; omit otherwise

jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: earfman/coretexa-verify@v1
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

**With no `github-token` the Action posts nothing.** The verdict goes to the job
summary and the step outputs, which need no permissions at all. That is the
recommended configuration for public repositories taking pull requests from
forks:

```yaml
permissions:
  contents: read
```

### Never use `pull_request_target` to check out PR code

> **Warning**
> Do not run this Action on a `pull_request_target` event with the pull
> request's own code checked out.

`pull_request_target` runs with a **read-write token and access to repository
secrets**, in the context of the base repository. Checking out the PR's head in
that context and then executing it — which is precisely what this Action does —
hands a fork's author your secrets. That is true of any workflow that builds or
tests PR code under `pull_request_target`; this Action's token isolation reduces
the blast radius but cannot fix a workflow that is unsafe by construction.

Use the ordinary `pull_request` event, which runs with a read-only token and no
secrets for fork PRs:

```yaml
on:
  pull_request:
```

If you need the comment on fork PRs, run the analysis on `pull_request`, upload
the JSON output as an artifact, and post it from a separate
`workflow_run`-triggered workflow that never checks out the PR's code.

### What this Action does not do

- No telemetry. It talks to no network service except the GitHub API, and only
  when you hand it a token.
- It runs entirely on your runner. Nothing is uploaded anywhere.
- It never writes outside the repository except to a temporary report directory,
  and it restores the working tree in a `finally`.

Report a security issue by opening an issue at
<https://github.com/earfman/coretexa-verify/issues>. See [SECURITY.md](SECURITY.md).

---

## Safety

- The working tree is restored in a `finally`, by copying back the exact bytes we
  saved before touching anything.
- We use `git show <base>:<path>` rather than `git checkout <base> -- <path>` so
  the **index is never modified** — a staged revert would make a later
  `git checkout -- .` destructive.
- We refuse to start on a dirty working tree — and that check runs *before*
  the dependency install, so nothing the install generates can trigger it.
- The dependency install only ever runs commands derived from files the
  repository already committed. Nothing else is fetched.
- Every subprocess has a timeout, and a timeout is reported as a result rather
  than swallowed as a failure.
- Nothing is written outside the repository except a temporary report directory.

---

## Development

```bash
git clone https://github.com/earfman/coretexa-verify
cd coretexa-verify
PYTHONPATH=src python -m pytest tests -q     # 507 tests, no network required
```

Layout:

```
src/coretexa_verify/
  classify.py    SOURCE / TEST / OTHER, with a reason for every decision
  selection.py   changed test files -> runnable targets, incl. fixture mapping
  refine.py      narrowing to the tests/cases this PR actually added
  hunks.py       unified-diff surgery and the behavioural-inertness rule
  gitops.py      timeout-bounded git, revert/restore, and tree snapshots
  deps.py        test-dependency detection and the install it runs
  runners/       the detection registry and one module per toolchain:
                   python.py, javascript.py, golang.py, rust.py, java.py,
                   plus custom.py (an explicit --test-command) and
                   junit.py (shared JUnit XML reading)
  verify.py      the experiment and the verdict logic
  report.py      terminal, Markdown and JSON rendering
  cli.py         command line
  action_main.py GitHub Action entry point
```

## Licence

MIT. See [LICENSE](LICENSE).
