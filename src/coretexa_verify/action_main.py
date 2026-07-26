"""GitHub Action entry point.

Runs on the *user's* runner against the checkout the workflow already made. We
never execute anyone's code on our own infrastructure, and this module talks to
no network service except the GitHub API the user explicitly handed us a token
for. No telemetry, no phoning home.

Responsibilities beyond running the experiment:

* make sure the base ref is actually present (``actions/checkout`` defaults to a
  shallow, single-branch clone, which has no merge base),
* write the verdict to ``$GITHUB_STEP_SUMMARY``,
* set the ``verdict`` / ``details`` / ``json`` outputs,
* optionally maintain exactly one PR comment, updating it in place.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid

from . import gitops
from .config import load_config
from .models import Verdict
from .report import render_markdown, render_text, to_json
from .verify import VerifyOptions, verify

COMMENT_MARKER = "<!-- coretexa-verify: do not edit, this comment is updated in place -->"


# --------------------------------------------------------------------------
# small env helpers
# --------------------------------------------------------------------------


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    raw = env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def set_output(name: str, value: str) -> None:
    path = env("GITHUB_OUTPUT")
    if not path:
        print(f"::set-output-unavailable:: {name}={value}", file=sys.stderr)
        return
    delim = f"ghadelim_{uuid.uuid4().hex}"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{name}<<{delim}\n{value}\n{delim}\n")


def write_summary(markdown: str) -> None:
    path = env("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(markdown + "\n")


# --------------------------------------------------------------------------
# git plumbing for shallow Action checkouts
# --------------------------------------------------------------------------


def ensure_base_available(repo: str, base_ref: str) -> tuple[str, list[str]]:
    """Make ``base_ref`` resolvable locally. Returns (ref to use, warnings)."""
    warnings: list[str] = []
    for candidate in (f"origin/{base_ref}", base_ref):
        if gitops.git(repo, "rev-parse", "--verify", f"{candidate}^{{commit}}").returncode == 0:
            if gitops.git(repo, "merge-base", candidate, "HEAD").returncode == 0:
                return candidate, warnings

    fetch = gitops.git(
        repo, "fetch", "--no-tags", "--depth=200", "origin",
        f"+refs/heads/{base_ref}:refs/remotes/origin/{base_ref}", timeout=300,
    )
    if fetch.returncode != 0:
        warnings.append(f"could not fetch base ref {base_ref}: {fetch.stderr.strip()}")
    if gitops.git(repo, "merge-base", f"origin/{base_ref}", "HEAD").returncode != 0:
        deep = gitops.git(repo, "fetch", "--no-tags", "--unshallow", "origin", timeout=600)
        if deep.returncode != 0:
            warnings.append(
                "no merge base found and the clone could not be deepened; "
                "set `fetch-depth: 0` on actions/checkout"
            )
    return f"origin/{base_ref}", warnings


# --------------------------------------------------------------------------
# PR comment (idempotent)
# --------------------------------------------------------------------------


def pr_number_from_event() -> int | None:
    path = env("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            event = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    for key in ("pull_request", "issue"):
        node = event.get(key)
        if isinstance(node, dict) and isinstance(node.get("number"), int):
            return node["number"]
    number = event.get("number")
    return number if isinstance(number, int) else None


def _api(url: str, token: str, method: str = "GET", payload: dict | None = None) -> object:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "coretexa-verify")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode() or "null")


def upsert_comment(body: str, token: str) -> str:
    """Create the comment, or update the one we made last time. Never spam."""
    repo = env("GITHUB_REPOSITORY")
    number = pr_number_from_event()
    if not repo or not number:
        return "no pull request context; comment skipped"
    api = env("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    marked = f"{COMMENT_MARKER}\n{body}"

    try:
        existing_id = None
        for page in range(1, 6):
            url = f"{api}/repos/{repo}/issues/{number}/comments?per_page=100&page={page}"
            comments = _api(url, token)
            if not isinstance(comments, list) or not comments:
                break
            for comment in comments:
                if COMMENT_MARKER in (comment.get("body") or ""):
                    existing_id = comment.get("id")
            if len(comments) < 100:
                break

        if existing_id is not None:
            _api(f"{api}/repos/{repo}/issues/comments/{existing_id}", token, "PATCH", {"body": marked})
            return f"updated comment {existing_id}"
        created = _api(f"{api}/repos/{repo}/issues/{number}/comments", token, "POST", {"body": marked})
        cid = created.get("id") if isinstance(created, dict) else "?"
        return f"created comment {cid}"
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        return f"comment failed: {exc}"


# --------------------------------------------------------------------------


def should_fail(verdict: Verdict, fail_on: str) -> bool:
    from .cli import should_fail as _impl

    return _impl(verdict, fail_on)


def main() -> int:
    repo = os.path.abspath(env("INPUT_WORKING_DIRECTORY") or env("GITHUB_WORKSPACE") or os.getcwd())
    base_input = env("INPUT_BASE_REF") or env("GITHUB_BASE_REF")
    head_input = env("INPUT_HEAD_REF") or "HEAD"
    fail_on = env("INPUT_FAIL_ON", "never") or "never"
    timeout = int(env("INPUT_TIMEOUT", "900") or "900")
    token = env("INPUT_GITHUB_TOKEN")
    do_comment = env_bool("INPUT_COMMENT", True)
    localize = env("INPUT_LOCALIZE", "auto") or "auto"
    install_deps = env_bool("INPUT_INSTALL_DEPS", True)
    install_command = env("INPUT_INSTALL_COMMAND")
    install_timeout = int(env("INPUT_INSTALL_TIMEOUT", "600") or "600")

    warnings: list[str] = []
    if not base_input:
        print("::error::no base ref: set the `base-ref` input or run on a pull_request event")
        return 1
    base_ref, fetch_warnings = ensure_base_available(repo, base_input)
    warnings.extend(fetch_warnings)

    classifier, cfg_warnings = load_config(repo)
    warnings.extend(cfg_warnings)
    for glob in env("INPUT_TEST_GLOBS").split():
        classifier.force_test_globs.append(glob)
    for glob in env("INPUT_SOURCE_GLOBS").split():
        classifier.force_source_globs.append(glob)

    report = verify(
        VerifyOptions(
            repo=repo,
            base=base_ref,
            head=head_input,
            timeout=timeout,
            classifier=classifier,
            localize=localize,
            install_deps=install_deps,
            install_command=install_command,
            install_timeout=install_timeout,
            # The runner's checkout is already at the PR head; moving it would
            # fight with whatever else the workflow does.
            allow_checkout=False,
        )
    )
    report.warnings = warnings + report.warnings

    print(render_text(report, color=False))

    markdown = render_markdown(report)
    write_summary(markdown)
    set_output("verdict", report.verdict.value)
    set_output("details", report.headline)
    set_output("json", to_json(report, indent=None))

    if token and do_comment:
        print(f"::notice::{upsert_comment(markdown, token)}")

    if should_fail(report.verdict, fail_on):
        print(f"::error::{report.verdict.value}: {report.headline}")
        return 1
    if report.verdict is Verdict.NO_GATE:
        print(f"::warning::{report.headline}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
