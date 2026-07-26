# Security

## Reporting a vulnerability

Open an issue at <https://github.com/earfman/coretexa-verify/issues>. There is no
private disclosure channel; if a report would be dangerous to publish, open an
issue saying so without details and a maintainer will arrange somewhere else to
continue.

Supported version: the latest release. Fixes are not backported.

## Threat model

coretexa-verify executes a pull request's own test suite, its own build step and
its own dependency installer, on your runner. **That code is written by the PR
author and is not trusted.** Everything below follows from that.

The tool itself has no runtime dependencies, makes no network requests of its
own, and sends no telemetry. The only network call it can make is to the GitHub
API, only to post one PR comment, and only when you hand it a token.

## Token isolation

Every subprocess that runs repository-controlled code is launched with a
sanitised copy of the environment. Two rules remove a variable:

1. **Exact name** — `INPUT_GITHUB_TOKEN`, `GITHUB_TOKEN`, `GH_TOKEN`,
   `GH_ENTERPRISE_TOKEN`, `ACTIONS_RUNTIME_TOKEN`,
   `ACTIONS_ID_TOKEN_REQUEST_TOKEN`, `ACTIONS_ID_TOKEN_REQUEST_URL`,
   `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `NPM_TOKEN`, `NODE_AUTH_TOKEN`,
   `TWINE_PASSWORD`, `CODECOV_TOKEN`, `SSH_AUTH_SOCK`, `PGPASSWORD`,
   `PGPASSFILE`, `MYSQL_PWD`, `NETRC`.
2. **Shape** — any name ending, at a word boundary, in `TOKEN`, `SECRET(S)`,
   `PASSWORD`, `PASSWD`, `PASSPHRASE`, `CREDENTIAL(S)`, `API_KEY`, `APIKEY`,
   `ACCESS_KEY`, `PRIVATE_KEY`, `AUTH` or `PAT`, case-insensitively. The
   boundary is deliberate: `MY_SERVICE_TOKEN` is removed, `TOKEN_URL` is not.

Nothing else is removed. A test suite legitimately depends on `PATH`, `HOME`,
`LANG`, `CI` and its language's cache variables, and stripping more than
necessary would break real repositories without improving safety.

Design notes:

- The isolation lives in one place, `gitops.run(..., isolate=True)`, so no call
  site builds its own environment and none can drift. A test asserts that every
  call site in `runners/` and `deps.py` passes the flag.
- The guarantee is tested against a **real subprocess** that prints its own
  environment, launched through `Runner.execute`, `Runner.run_build` and
  `deps.run_plans` — not against a mock.
- `os.environ` in our own process is never modified. That is why the Action can
  still read its token afterwards, and it is read only after the experiment has
  finished.
- git is **not** isolated. It is our own code, not the repository's, and it may
  need the job's credentials to fetch a base ref from a private repository.
- The report prints how many variables were withheld, and their names. Never
  their values, on any output path — there is a test for that too.

Escape hatch, per-name and opt-in:

```yaml
env:
  CORETEXA_VERIFY_ALLOW_ENV: MY_SERVICE_TOKEN,OTHER_TOKEN
```

There is no setting that disables isolation wholesale.

## Workflow configuration is your responsibility

Token isolation reduces the blast radius of running untrusted code. It cannot
rescue a workflow that is unsafe by construction.

**Do not run this Action on `pull_request_target` with the pull request's code
checked out.** That event runs with a read-write token and access to repository
secrets in the base repository's context; executing a fork's code there hands
that fork's author your secrets, whatever this Action does with its environment.
Use `pull_request`, which gives fork PRs a read-only token and no secrets.

Recommended permissions:

```yaml
permissions:
  contents: read          # everything the analysis needs
  pull-requests: write    # only if you pass github-token
```

With no `github-token`, the Action writes only the job summary and its step
outputs, and needs no write permission at all.
