"""Command line interface.

Two shapes:

    python -m coretexa_verify --repo <path> --base <ref> --head <ref>
    python -m coretexa_verify --pr https://github.com/owner/repo/pull/123

The PR mode uses plain git over HTTPS (``git fetch origin pull/N/head``) so it
works where the GitHub REST API is unreachable.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import gitops
from .config import load_config
from .models import Verdict
from .report import one_line, render_markdown, render_text, to_json
from .verify import VerifyOptions, __version__, verify

#: Exit code 0 unless the user asked us to gate on a verdict.
EXIT_OK = 0
EXIT_VERDICT = 1
EXIT_USAGE = 2

FAIL_ON_CHOICES = ("never", "no-gate", "no-gate-or-inconclusive", "not-gate-holds")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="coretexa-verify",
        description=(
            "Mutation-test a pull request's diff: revert only its source changes and "
            "check whether the PR's own tests notice."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m coretexa_verify --repo . --base origin/main --head HEAD\n"
            "  python -m coretexa_verify --pr https://github.com/psf/requests/pull/1234 --json\n"
        ),
    )
    src = p.add_argument_group("what to analyse")
    src.add_argument("--repo", default=None, help="path to a git repository (default: cwd)")
    src.add_argument("--base", default=None, help="base ref (default: origin/HEAD, then main/master)")
    src.add_argument("--head", default="HEAD", help="head ref (default: HEAD)")
    src.add_argument("--pr", default=None, help="GitHub pull request URL; clones/fetches it for you")
    src.add_argument(
        "--workdir",
        default=os.path.join(os.path.expanduser("~"), ".cache", "coretexa-verify"),
        help="where --pr clones repositories (default: ~/.cache/coretexa-verify)",
    )
    src.add_argument("--clone-depth", type=int, default=100, help="fetch depth for --pr (default: 100)")

    run = p.add_argument_group("running")
    run.add_argument("--timeout", type=int, default=900, help="per test-run timeout in seconds (default: 900)")
    run.add_argument(
        "--runner-arg",
        action="append",
        default=[],
        dest="runner_args",
        metavar="ARG",
        help="extra argument passed through to the test runner (repeatable)",
    )
    run.add_argument(
        "--no-checkout",
        action="store_true",
        help="refuse to move the checkout; require it to already be at --head",
    )
    run.add_argument(
        "--max-targets", type=int, default=50,
        help="refuse to run if selection widens beyond this many targets (default: 50)",
    )
    run.add_argument(
        "--localize", choices=("auto", "always", "never"), default="auto",
        help=("per-hunk localisation: 'auto' (default) drills down only when the whole-PR "
              "revert merely broke the build, 'always' also drills down when the gate holds, "
              "'never' reports only the whole-PR result"),
    )
    run.add_argument(
        "--max-hunks", type=int, default=40,
        help="skip localisation if the diff has more behavioural hunks than this (default: 40)",
    )
    run.add_argument(
        "--no-refine", action="store_true",
        help="run whole test files instead of narrowing to the tests this PR added",
    )

    cls = p.add_argument_group("classification overrides")
    cls.add_argument("--test-glob", action="append", default=[], metavar="GLOB",
                     help="force paths matching GLOB to be treated as TEST (repeatable)")
    cls.add_argument("--source-glob", action="append", default=[], metavar="GLOB",
                     help="force paths matching GLOB to be treated as SOURCE (repeatable)")

    out = p.add_argument_group("output")
    out.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of text")
    out.add_argument("--markdown", action="store_true", help="emit Markdown (the CI summary format)")
    out.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    out.add_argument(
        "--fail-on", choices=FAIL_ON_CHOICES, default="never",
        help="exit non-zero on the given verdict class (default: never)",
    )
    out.add_argument("--version", action="version", version=f"coretexa-verify {__version__}")
    return p


def should_fail(verdict: Verdict, fail_on: str) -> bool:
    if fail_on == "never":
        return False
    if fail_on == "no-gate":
        return verdict is Verdict.NO_GATE
    if fail_on == "no-gate-or-inconclusive":
        return verdict in (Verdict.NO_GATE, Verdict.INCONCLUSIVE)
    if fail_on == "not-gate-holds":
        return verdict not in (Verdict.GATE_HOLDS, Verdict.GATE_HOLDS_BUILD)
    return False


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.pr and args.repo:
        print("error: pass either --pr or --repo, not both", file=sys.stderr)
        return EXIT_USAGE

    head = args.head
    try:
        if args.pr:
            pr = gitops.parse_pr_url(args.pr)
            print(f"fetching {pr.owner}/{pr.repo} pull/{pr.number}/head ...", file=sys.stderr)
            repo = gitops.fetch_pull_request(pr, args.workdir, depth=args.clone_depth)
            head = pr.local_branch
        else:
            repo = os.path.abspath(args.repo or os.getcwd())
    except gitops.GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    classifier, cfg_warnings = load_config(repo)
    classifier.force_test_globs.extend(args.test_glob)
    classifier.force_source_globs.extend(args.source_glob)

    report = verify(
        VerifyOptions(
            repo=repo,
            base=args.base,
            head=head,
            timeout=args.timeout,
            classifier=classifier,
            extra_runner_args=args.runner_args,
            allow_checkout=not args.no_checkout,
            max_targets=args.max_targets,
            localize=args.localize,
            max_hunks=args.max_hunks,
            refine_selection=not args.no_refine,
        )
    )
    report.warnings = cfg_warnings + report.warnings

    if args.json:
        print(to_json(report))
    elif args.markdown:
        print(render_markdown(report))
    else:
        color = not args.no_color and sys.stdout.isatty()
        print(render_text(report, color=color))

    if should_fail(report.verdict, args.fail_on):
        print(f"\nfailing because --fail-on={args.fail_on}: {one_line(report)}", file=sys.stderr)
        return EXIT_VERDICT
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
