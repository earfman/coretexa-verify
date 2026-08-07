# coretexa-verify

**Can this pull request's own new tests actually fail?**

Revert the PR's source changes. Keep its new tests. Run them again. If they still pass,
those tests do not gate the code the PR adds — and the green check is telling you nothing
about the change it was supposed to be checking.

That's the whole mechanism. It runs on your runner, needs no account, and sends nothing
anywhere.

---

## The case that makes the point

`yorukot/superfile#1545` added a directory-size cache and, alongside it, request tracking
to stop stale results being applied to the wrong path. The cache was well tested. The
request tracking was not tested at all — five new symbols, every one of them revertible
with all 67 tests still green.

CodeRabbit reviewed that pull request three times. It posted design opinions on exactly
that code. It never noticed the code had no tests.

This Action does not review design, and it will never tell you a change is *correct*. It
answers the narrow, mechanically decidable question underneath: **do these tests fail when
you take the change away?**

---

## Quick start

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

Non-blocking by default — it only fails the job if you opt in with `fail-on`. The verdict
goes to the job summary and the step outputs.

**Languages:** Python, JavaScript/TypeScript, Go, Rust. Java is experimental. Anything
else, point it at your own command with `--test-command`.

---

## The verdicts

| verdict | meaning |
|---|---|
| `NO_GATE` | Source changes reverted, the PR's new tests still passed. They do not gate this change. |
| `GATE_HOLDS` | Reverting made the new tests fail. They do gate it. |
| `GATE_HOLDS_BUILD` | Reverting broke the build, so the tests could not run. Weaker evidence, but not nothing. |
| `NO_NEW_TESTS` | The PR changes behaviour and adds no tests at all. |
| `INCONCLUSIVE` | No trustworthy answer was reachable. It says so instead of guessing. |

A `NO_GATE` is worth ninety seconds of attention. It usually means either the tests live
somewhere else in the suite, or the change is bigger than the tests that arrived with it.

---

## What this cannot tell you

This section is deliberately not at the bottom.

**It measures observability, not correctness.** Whether tests detect a change is
independent of whether the change is right.

- `GATE_HOLDS` does **not** mean the PR is correct. A thoroughly-tested wrong answer
  returns `GATE_HOLDS`. We have watched exactly that happen: run against
  `SollanSystems/loop-engineer#80` — a PR we had separately established was broken, by
  hand — this Action returned `GATE_HOLDS`. It would have cleared it. That is the tool
  working as designed, and it is still a real limit, so it is documented here rather than
  left for you to find.
- `NO_GATE` does **not** mean the PR is broken. Plenty of correct changes ship with their
  coverage elsewhere.
- It does not read intent, judge design, or assess whether a test is meaningful.

The one claim it refutes outright is a claim *about verification*: a PR whose description
promises comprehensive tests, returning `NO_GATE` or `NO_NEW_TESTS`.

Full detail — the PR shapes that reliably produce `INCONCLUSIVE`, and the repository
layouts where the dependency install cannot help — is in [`docs/limits.md`](docs/limits.md).

---

## Track record

Field-tested against real pull requests in other people's repositories, not against
fixtures.

- **Sweep 1**, 16 PRs — surfaced one **false** `NO_GATE` on `sqlfluff/sqlfluff#8221`,
  fixed in 1.2.0 by proof-carrying selection.
- **Sweep 2**, 27 PRs — zero wrong verdicts.
- **Sweep 3**, 6 PRs — surfaced one **false** `NO_GATE` on `yorukot/superfile#1619`, where
  the selected test was in a package that does not import the changed code. Fixed in 1.3.5
  by asking `go list -deps -test` whether the tests could reach the revert at all.

Two false `NO_GATE`s in 49 runs, both found by running against strangers' code rather than
fixtures, and both closed in the release that followed. That is the number worth having;
a tool that had never been wrong in the field is a tool that had never been used there.

Worked examples of every verdict, with the real pull requests that produced them, are in
[`docs/examples.md`](docs/examples.md).

---

## Inputs

| input | default | meaning |
|---|---|---|
| `base-ref` | | base branch to compare against |
| `head-ref` | | HEAD ref to analyse |
| `fail-on` | `never` | `never`, `no-gate`, `no-gate-or-inconclusive`, `not-gate-holds` |
| `timeout` | `900` | per test-run timeout, in seconds |
| `localize` | `auto` | `auto`, `always`, `never` |
| `localize-budget` | | time budget for the localisation loop |
| `install-deps` | `true` | install the project's declared test dependencies |
| `install-command` | | override the dependency install |
| `test-command` | | run this instead of a detected runner |
| `junit-path` | | read results from JUnit XML at this path |
| `github-token` | | post the verdict as a PR comment; omit and nothing is posted |

Outputs, the command line, test selection, localisation, dependency install and
test-file classification are documented in
[`docs/how-it-works.md`](docs/how-it-works.md).

---

## Command line

The Action is a thin wrapper. The same engine runs locally:

```bash
pipx install git+https://github.com/earfman/coretexa-verify
coretexa-verify --pr https://github.com/owner/name/pull/1234 --markdown
```

`--pr` takes a pull request URL and clones or fetches it for you. To analyse a checkout
you already have, point `--repo` at its path and give it `--base` and `--head` instead.

Useful flags: `--json` and `--markdown` for machine-readable output, `--test-command` and
`--junit-path` when detection cannot work it out, `--no-install-deps` in an environment
you have already prepared.

---

## Security

This Action runs a pull request's own test suite, its own build step and its own
dependency installer. All three are code the PR author wrote. Treat it exactly as you
would any other workflow that executes untrusted code.

### Token isolation

**No credential in the job's environment is passed to any subprocess that runs
repository-controlled code.** Before the installer, the build step, the test runs and the
collection pass are launched, the environment is copied and every credential-shaped
variable is removed:

- by exact name: `INPUT_GITHUB_TOKEN`, `GITHUB_TOKEN`, `GH_TOKEN`,
  `ACTIONS_RUNTIME_TOKEN`, `ACTIONS_ID_TOKEN_REQUEST_TOKEN`, `AWS_SECRET_ACCESS_KEY`,
  `NPM_TOKEN`, `NODE_AUTH_TOKEN`, `PGPASSWORD`, `SSH_AUTH_SOCK` and friends;
- by shape: any name ending in `TOKEN`, `SECRET`, `PASSWORD`, `PASSPHRASE`,
  `CREDENTIAL(S)`, `API_KEY`, `ACCESS_KEY`, `PRIVATE_KEY`, `AUTH` or `PAT` at a word
  boundary (so `MY_SERVICE_TOKEN` goes, `TOKEN_URL` stays).

Everything else is preserved, because a test suite legitimately needs `PATH`, `HOME`,
`LANG` and its language's caches. The report prints how many variables were withheld and
their **names only** — never a value.

git is deliberately *not* isolated: it is our own code and may need the job's credentials
to fetch a base ref from a private repository. The comment token is read once, in our own
process, after the experiment has finished.

If a test genuinely needs one of these variables — an integration test against a real
service — name it explicitly:

```yaml
env:
  CORETEXA_VERIFY_ALLOW_ENV: MY_SERVICE_TOKEN,OTHER_TOKEN
```

Per-name and opt-in. There is no switch that disables isolation wholesale.

### Least-privilege permissions

The Action needs nothing beyond reading the code it is analysing. Give it only that, and
add `pull-requests: write` only if you want the PR comment:

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

**With no `github-token` the Action posts nothing.** The verdict goes to the job summary
and the step outputs, which need no permissions at all. That is the recommended
configuration for public repositories taking pull requests from forks:

```yaml
permissions:
  contents: read
```

### Never use `pull_request_target` to check out PR code

> **Warning**
> Do not run this Action on a `pull_request_target` event with the pull request's own code
> checked out.

`pull_request_target` runs with a **read-write token and access to repository secrets**, in
the context of the base repository. Checking out the PR's head in that context and then
executing it — which is precisely what this Action does — hands both to code a stranger
controls. Use `pull_request`, which is what the Quick start above does.

More, including what this Action deliberately does not do, is in
[`SECURITY.md`](SECURITY.md).

---

## Development

```bash
git clone https://github.com/earfman/coretexa-verify
cd coretexa-verify
PYTHONPATH=src python -m pytest tests -q     # 526 tests, no network required
```

Zero third-party dependencies, pure standard library, Python 3.9+. There is no supply
chain to compromise because there is no supply chain.

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

Release history is in [`CHANGELOG.md`](CHANGELOG.md).

## Licence

MIT.
