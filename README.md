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

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      # Install YOUR project's test dependencies exactly as your CI does.
      # coretexa-verify runs your tests with the interpreter on PATH.
      - run: pip install -e ".[dev]"

      - uses: earfman/coretexa-verify@v1
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
          # fail-on: no-gate     # uncomment to make NO_GATE block the merge
```

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
`--test-glob`, `--source-glob`, `--fail-on`, `--runner-arg`.

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

Changed test files are handed to the runner directly. But two refinements make
the result mean what you think it means, and both are **verified by asking the
runner to collect them** — nothing is ever guessed:

- **Only the tests this PR added.** The diff's changed line numbers are
  intersected with the test file's AST, so a neighbouring legacy test that hits
  the network cannot turn a good run into `INCONCLUSIVE`.
- **Only the fixture cases this PR added.** For YAML/JSON fixtures, the top-level
  keys the PR added are matched against collected parametrised test ids. On
  sqlfluff this turned a 2,313-case, multi-minute suite into the 10 cases the PR
  is actually about.

If a changed fixture cannot be mapped to a consuming test module, the enclosing
test directory is run instead **and the report says so** — a silent widening of
scope would change what the verdict means.

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

`--localize always` runs this even when the gate holds; `--localize never`
reports only the whole-PR result.

### Language support

Detection is a small registry keyed on repository markers. Adding a language is
one detector function plus one entry in `REGISTRY`. The chosen command and the
reason for choosing it are always printed.

| marker | runner |
|---|---|
| `uv.lock` + `uv` on PATH | `uv run --frozen pytest` |
| `.venv/bin/python` | `.venv/bin/python -m pytest` |
| `pyproject.toml`, `setup.py`, `setup.cfg`, `tox.ini`, `pytest.ini`, `conftest.py` | `python -m pytest` |
| `package.json` naming `vitest` | `npx vitest run` |
| `package.json` naming `jest` | `npx jest` |
| `package.json` with some other `scripts.test` | `npm test` (exit-code-only; the reduced confidence is stated in the report) |

If detection fails, the verdict is `INCONCLUSIVE` with the reason. We never guess
a command.

### Test-file classification

Configurable, with defaults that work: paths containing `test/`, `tests/`,
`spec/`, `__tests__/`, `unit_tests/`, `*testsuite/`; files matching `test_*.py`,
`*_test.py`, `*Test.py`, `conftest.py`, `*.test.[jt]sx?`, `*.spec.[jt]sx?`.
Fixture and snapshot data under a test directory counts as `TEST`.

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
- No runner detected: Go, Rust, Java, C++, Ruby, .NET — anything outside
  pytest/jest/vitest today.
- A changed fixture that cannot be mapped to a consumer and has no enclosing test
  directory.
- Selection that widens past `--max-targets` (default 50).
- Any run that times out; the timeout is reported, never swallowed.
- A dirty working tree — we refuse to start rather than risk not restoring it.
- A shallow clone with no merge base (use `fetch-depth: 0`).

### PR shapes where it can mislead

- **Tests split across files.** If the PR changes `src/a.py` and adds
  `tests/test_b.py` that only covers something else, we run the changed test file
  and may report `NO_GATE` when the real gate is an existing untouched test. The
  question we answer is about the PR's *own* tests, by design — but the headline
  can read as a broader claim than it is.
- **Fixture mapping via literal search.** We look for test modules containing a
  literal reference to the fixture's path, directory or stem. A test suite that
  discovers fixtures purely dynamically will fall through to the
  directory-fallback (reported) or fail to select (`INCONCLUSIVE`).
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
- **Non-hermetic tests.** A test that writes into the repository, caches to disk,
  or depends on run order can make the second run differ from the first for
  reasons that have nothing to do with the revert.
- **Generated code and lockfiles** are classified `SOURCE` and reverted. For a PR
  that regenerates a large artifact, the revert is real but the result is rarely
  interesting.
- **Renames and whole-file additions are skipped during localisation**, because
  there is no meaningful intermediate state to revert one hunk of.

---

## Safety

- The working tree is restored in a `finally`, by copying back the exact bytes we
  saved before touching anything.
- We use `git show <base>:<path>` rather than `git checkout <base> -- <path>` so
  the **index is never modified** — a staged revert would make a later
  `git checkout -- .` destructive.
- We refuse to start on a dirty working tree.
- Every subprocess has a timeout, and a timeout is reported as a result rather
  than swallowed as a failure.
- Nothing is written outside the repository except a temporary report directory.

---

## Development

```bash
git clone https://github.com/earfman/coretexa-verify
cd coretexa-verify
PYTHONPATH=src python -m pytest tests -q     # 115 tests, no network required
```

Layout:

```
src/coretexa_verify/
  classify.py    SOURCE / TEST / OTHER, with a reason for every decision
  selection.py   changed test files -> runnable targets, incl. fixture mapping
  refine.py      narrowing to the tests/cases this PR actually added
  hunks.py       unified-diff surgery and the behavioural-inertness rule
  gitops.py      timeout-bounded git, and the revert/restore machinery
  runners/       the detection registry: python.py, javascript.py
  verify.py      the experiment and the verdict logic
  report.py      terminal, Markdown and JSON rendering
  cli.py         command line
  action_main.py GitHub Action entry point
```

## Licence

MIT. See [LICENSE](LICENSE).
