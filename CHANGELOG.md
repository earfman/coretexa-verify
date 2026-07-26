# Changelog

All notable changes to coretexa-verify. Newest first.

## 1.3.3

### Fixed

- **Repeat runs on the same checkout no longer degrade to `INCONCLUSIVE`.** A
  build or test run routinely rewrites a tracked lockfile - `go mod download`
  refreshes `go.work.sum`, cargo touches `Cargo.lock`. The cleanliness gate
  correctly refuses to run against a dirty tree, so leaving that dirt behind
  meant the *second* pull request verified in a given clone aborted for a reason
  that had nothing to do with it. Tracked files dirtied during the run are now
  restored at the end. The gate itself is unchanged: a tree that was already
  dirty when you invoked the tool is still refused, and a user's uncommitted
  work is never touched. Found while scouting knadh/koanf, where three of four
  PRs in a sweep died on a `go.work.sum` we had modified ourselves.

## 1.3.2

- **Security: token isolation.** No credential in the job's environment reaches
  any subprocess that runs repository-controlled code — the dependency
  installer, the build step, the test runs and the collection pass all get a
  sanitised environment. git is exempt (it is ours and may need credentials to
  fetch a base ref); the PR-comment token is read in our own process after the
  experiment ends. Opt-in per-name escape hatch: `CORETEXA_VERIFY_ALLOW_ENV`.
- `--test-command` / Action input `test-command`: an explicit command that
  replaces runner detection entirely, for toolchains no detector covers.
  `--junit-path` / `junit-path` makes its results as precise as a detected
  runner's; without it, classification is exit-code plus a declared heuristic.
- The "no test runner could be detected" message now names the markers it looked
  for and recommends a flag that exists.
- README: `Security` section (least-privilege permissions, `pull_request_target`
  warning, token isolation), corrected language and test-count claims.
- Added `CHANGELOG.md` and `SECURITY.md`.

## 1.3.1

- An identifier rename coupled to a behaviour change no longer reads as a
  build-only gate: rename-only hunks are inert, and their rename is kept applied
  when a sibling hunk is reverted (gatus#1719 `GATE_HOLDS_BUILD` → `GATE_HOLDS`).
- A head-run failure is re-checked with source *and* tests at the merge base.
  Failures that reproduce there are excluded as pre-existing; failures in tests
  that do not exist at base are reported as the PR's own new tests failing
  (superfile#1519, #1560 → real verdicts; #1534 still `INCONCLUSIVE`, with a cause).
- Hunks in files no detected runner can execute — frontend assets, dependency
  manifests, lock files — get the new status `unreachable`, are never run, and
  leave the behavioural-change denominator (gatus#1725 "19 of 34" → "2 of 17").

## 1.3.0

- Go (`go test -json`), Rust (`cargo test`) and JVM (Maven/Gradle, experimental)
  runners.
- Rust inline `#[cfg(test)]` blocks: one file is both halves of the experiment,
  so source hunks are reverted around the PR's own inline tests.
- Per-runner executable-file-extension filter, so a polyglot repository never
  hands a `.rs` integration test to pytest.

## 1.2.1

- A skipped test is not a passing test: no verdict may rest on a run in which
  nothing executed, and every headline says how many tests were skipped.

## 1.2.0

- Proof-carrying selection: a fixture-to-test mapping counts only when the
  collected node ids name the fixture or a case key the PR added.
- Auto-discovery harness detection, for suites that parametrise over a fixture
  directory without ever naming the file (sqlfluff#8221).
- Targeted probe: an unproven mapping that would produce `NO_GATE` is tested by
  reverting the fixture alone; no change in outcome downgrades to `INCONCLUSIVE`.
- Monorepo support: tests are run from the workspace package that owns them.

## 1.1.0

- Per-hunk localisation when the whole-PR revert only breaks the build.
- Behavioural-inertness rule: comment/docstring/formatting-only hunks are
  excluded, proven by docstring-stripped AST comparison for Python.
- Dependency-install detection and reporting, with a fallback chain.

## 1.0.0

- First release. Reverts a PR's source changes, re-runs the PR's own tests, and
  reports `NO_GATE` / `GATE_HOLDS` / `GATE_HOLDS_BUILD` / `NO_NEW_TESTS` /
  `INCONCLUSIVE`. Python (pytest) and JavaScript (jest/vitest/`npm test`).
