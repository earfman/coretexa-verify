# Limits and known issues

What this tool cannot tell you, the pull-request shapes that reliably produce `INCONCLUSIVE`, and the repository layouts where the dependency install cannot help. Read this before trusting a verdict in either direction.

[← back to the README](../README.md)

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

---

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
