"""Rendering: a block a human acts on in two seconds, and Markdown for CI."""

from __future__ import annotations

import json

from .models import Kind, Report, Verdict

VERDICT_BLURB: dict[Verdict, str] = {
    Verdict.NO_GATE: "This PR's tests would pass without the fix.",
    Verdict.GATE_HOLDS: "The PR's tests fail without the fix. Healthy.",
    Verdict.GATE_HOLDS_BUILD: "Without the fix the tests stop building rather than assert-failing.",
    Verdict.NO_NEW_TESTS: "Source changed; no test was added or modified.",
    Verdict.INCONCLUSIVE: "No verdict could be established.",
}

_MARK = {
    Verdict.NO_GATE: "!!",
    Verdict.GATE_HOLDS: "ok",
    Verdict.GATE_HOLDS_BUILD: "ok",
    Verdict.NO_NEW_TESTS: "--",
    Verdict.INCONCLUSIVE: "??",
}


def to_json(report: Report, indent: int = 2) -> str:
    return json.dumps(report.to_dict(), indent=indent, sort_keys=False)


def render_text(report: Report, color: bool = False) -> str:
    w = 78
    lines: list[str] = []
    lines.append("=" * w)
    lines.append(f"[{_MARK[report.verdict]}] {report.verdict.value}")
    lines.append("=" * w)
    lines.append(_wrap(report.headline, w))
    lines.append("")
    lines.append(f"repo      : {report.repo}")
    lines.append(f"head      : {report.head_ref} @ {report.head_sha}")
    lines.append(f"base      : {report.base_ref} @ {report.base_sha} (merge base)")
    if report.runner:
        lines.append(f"runner    : {report.runner.id} ({report.runner.language})")
        lines.append(f"            {report.runner.reason}")
    inst = report.install
    if inst is not None:
        lines.append(f"deps      : {inst.status} ({inst.source}) - {inst.summary()}")
        for cmd in inst.commands:
            lines.append(f"            $ {cmd}")
        if inst.evidence:
            lines.append(_wrap(f"            evidence: {inst.evidence}", w, subsequent="              "))
        if inst.artefacts:
            lines.append(f"            created (left in place): {', '.join(inst.artefacts[:6])}")
        if len(inst.attempts) > 1:
            lines.append("            attempts (in order):")
            for a in inst.attempts:
                cmd = (a.get("commands") or [""])[0]
                lines.append(f"              {a.get('status')}  {a.get('detector')}: $ {cmd}")
        if inst.failed:
            for line in (inst.stderr_tail or inst.stdout_tail).strip().splitlines()[-8:]:
                lines.append(f"            | {line}")
    if report.build is not None:
        b = report.build
        lines.append(
            f"build     : {b.status} - re-run before every test run ({b.runs} run(s), "
            f"{b.failures} failure(s))"
        )
        lines.append(f"            $ {b.command_str}")
        lines.append(_wrap(f"            reason: {b.reason}", w, subsequent="              "))
    if report.workspace_package:
        lines.append(f"workspace : tests run from {report.workspace_package}/")
    if report.build_artifact_risk:
        lines.append(_wrap(f"artefacts : {report.build_artifact_risk}", w, subsequent="            "))

    if report.changed_files:
        lines.append("")
        lines.append("changed files")
        for f in report.changed_files:
            tag = f.kind.value.ljust(6)
            extra = " [runnable test]" if f.executable_test else ""
            lines.append(f"  {f.status} {tag} {f.path}{extra}")
            lines.append(f"           reason: {f.reason}")

    if report.selection:
        lines.append("")
        lines.append("test selection")
        for s in report.selection:
            arrow = ", ".join(s.targets) if s.targets else "(nothing)"
            lines.append(f"  {s.source_file}")
            lines.append(f"    -> {arrow}   [{s.method}]")
            if s.detail:
                lines.append(f"       {s.detail}")
            lines.append(
                f"       proof: {s.proof}" if s.proof else "       proof: NONE (mapping is a guess)"
            )

    for label, run in (
        ("run at head", report.head_run),
        ("run with source reverted", report.reverted_run),
        ("run with only the fixture reverted (probe)", report.probe_run),
    ):
        if run is None:
            continue
        lines.append("")
        lines.append(f"{label}: {run.outcome.value} - {run.summary()} in {run.duration_s}s")
        lines.append(f"  $ {run.command_str}")
        if run.note:
            lines.append(f"  note: {run.note}")
        for name in run.failing_ids[:10]:
            lines.append(f"  FAIL  {name}")
        for name in run.erroring_ids[:10]:
            lines.append(f"  ERROR {name}")
        hidden = len(run.failing_ids) + len(run.erroring_ids) - 20
        if hidden > 0:
            lines.append(f"  ... {hidden} more")

    if report.probe_note:
        lines.append("")
        lines.append(_wrap(f"fixture probe: {report.probe_note}", w, subsequent="  "))

    if report.hunk_results:
        lines.append("")
        lines.append("per-hunk localisation (each hunk reverted on its own)")
        for h in report.hunk_results:
            mark = {"gated": "GATED    ", "ungated": "UNGATED  ", "unknown": "UNKNOWN  "}[h.status]
            lines.append(f"  {mark} {h.label}")
            lines.append(f"            {h.outcome.value}: {h.summary}")
            for fid in h.failing_ids[:3]:
                lines.append(f"            -> {fid}")
    if report.inert_hunks:
        lines.append("")
        lines.append("hunks excluded as behaviourally inert")
        for note in report.inert_hunks:
            lines.append(f"  {note}")

    if report.reverted_files:
        lines.append("")
        lines.append("reverted to base: " + ", ".join(report.reverted_files))
    other = [f.path for f in report.changed_files if f.kind is Kind.OTHER]
    if other:
        lines.append("not reverted (docs/metadata): " + ", ".join(other))

    if report.warnings:
        lines.append("")
        lines.append("warnings")
        for warn in report.warnings:
            lines.append(_wrap(f"  - {warn}", w, subsequent="    "))

    if not report.tree_restored:
        lines.append("")
        lines.append("*** WARNING: the working tree may not have been fully restored. ***")

    lines.append("=" * w)
    text = "\n".join(lines)
    if color:
        text = _colorize(text, report.verdict)
    return text


def render_markdown(report: Report) -> str:
    """Job-summary / PR-comment body. Idempotent marker goes in by the caller."""
    icon = {
        Verdict.NO_GATE: "🚨",
        Verdict.GATE_HOLDS: "✅",
        Verdict.GATE_HOLDS_BUILD: "✅",
        Verdict.NO_NEW_TESTS: "➖",
        Verdict.INCONCLUSIVE: "❔",
    }[report.verdict]

    out: list[str] = []
    out.append(f"## {icon} `{report.verdict.value}`")
    out.append("")
    out.append(f"**{report.headline}**")
    out.append("")
    out.append("| | |")
    out.append("|---|---|")
    out.append(f"| head | `{report.head_sha[:12]}` ({report.head_ref}) |")
    out.append(f"| base (merge base) | `{report.base_sha[:12]}` ({report.base_ref}) |")
    if report.runner:
        out.append(f"| runner | `{report.runner.id}` — {report.runner.reason} |")
    if report.install is not None:
        inst = report.install
        cmds = ", ".join(f"`{c}`" for c in inst.commands) or "_none_"
        out.append(
            f"| dependency install | {cmds} — {inst.summary()} "
            f"({inst.source}: {inst.evidence or 'n/a'}) |"
        )
    if report.build is not None:
        b = report.build
        out.append(
            f"| build | `{b.command_str}` — re-run before every test run "
            f"({b.runs} run(s), {b.failures} failure(s)) |"
        )
    if report.workspace_package:
        out.append(f"| workspace package | `{report.workspace_package}` |")
    if report.test_targets:
        out.append(f"| tests run | {', '.join(f'`{t}`' for t in report.test_targets)} |")
    if report.reverted_files:
        out.append(f"| source reverted | {', '.join(f'`{f}`' for f in report.reverted_files)} |")
    out.append("")

    if report.head_run or report.reverted_run:
        out.append("<details><summary>Runs</summary>")
        out.append("")
        for label, run in (
            ("At head", report.head_run),
            ("With source reverted to base", report.reverted_run),
            ("With only the fixture reverted (mapping probe)", report.probe_run),
        ):
            if run is None:
                continue
            out.append(f"**{label}** — `{run.outcome.value}`: {run.summary()} ({run.duration_s}s)")
            out.append("")
            out.append("```")
            out.append(run.command_str)
            out.append("```")
            if run.note:
                out.append(f"> {run.note}")
            failures = run.failing_ids[:10] + run.erroring_ids[:10]
            if failures:
                out.append("")
                for f in failures:
                    out.append(f"- `{f}`")
            out.append("")
        out.append("</details>")
        out.append("")

    if report.hunk_results:
        ungated = [h for h in report.hunk_results if h.status == "ungated"]
        unknown = [h for h in report.hunk_results if h.status == "unknown"]
        extra = f", {len(unknown)} not evaluable" if unknown else ""
        out.append(
            f"<details{' open' if ungated else ''}><summary>Per-hunk localisation "
            f"({len(ungated)} of {len(report.hunk_results)} behavioural change(s) ungated"
            f"{extra})</summary>"
        )
        out.append("")
        out.append("| hunk | reverted alone | result |")
        out.append("|---|---|---|")
        flags = {
            "ungated": "🚨 **not detected**",
            "gated": "detected",
            "unknown": "❔ **not evaluated** (the runner itself failed)",
        }
        for h in report.hunk_results:
            out.append(f"| `{h.label}` | {flags[h.status]} | {h.outcome.value}: {h.summary} |")
        if ungated:
            out.append("")
            for h in ungated:
                out.append(f"<b>Ungated change in <code>{h.path}</code> ({h.header})</b>")
                out.append("")
                out.append("```diff")
                out.append(h.preview)
                out.append("```")
        out.append("")
        out.append("</details>")
        out.append("")

    if report.selection:
        out.append("<details><summary>How the tests were selected</summary>")
        out.append("")
        for s in report.selection:
            targets = ", ".join(f"`{t}`" for t in s.targets) or "_nothing_"
            out.append(f"- `{s.source_file}` → {targets} _({s.method}{'; ' + s.detail if s.detail else ''})_")
            out.append(f"  - proof: {s.proof or '**none — this mapping is a guess**'}")
        out.append("")
        out.append("</details>")
        out.append("")

    if report.warnings:
        out.append("<details><summary>Warnings</summary>")
        out.append("")
        for warn in report.warnings:
            out.append(f"- {warn}")
        out.append("")
        out.append("</details>")
        out.append("")

    out.append(
        "<sub>coretexa-verify reverts only the PR's source files to their base content, "
        "re-runs the PR's own tests, and reports whether anything noticed. "
        f"v{report.tool_version}</sub>"
    )
    return "\n".join(out)


def one_line(report: Report) -> str:
    return f"{report.verdict.value}: {report.headline}"


def _wrap(text: str, width: int, subsequent: str = "") -> str:
    import textwrap

    return "\n".join(
        textwrap.wrap(text, width=width, subsequent_indent=subsequent) or [""]
    )


_COLORS = {
    Verdict.NO_GATE: "\033[1;31m",
    Verdict.GATE_HOLDS: "\033[1;32m",
    Verdict.GATE_HOLDS_BUILD: "\033[1;32m",
    Verdict.NO_NEW_TESTS: "\033[1;33m",
    Verdict.INCONCLUSIVE: "\033[1;33m",
}


def _colorize(text: str, verdict: Verdict) -> str:
    """Colour the verdict banner line only; leave the data plain."""
    colour = _COLORS[verdict]
    out = []
    for line in text.split("\n"):
        if line.startswith("[") and verdict.value in line:
            out.append(f"{colour}{line}\033[0m")
        else:
            out.append(line)
    return "\n".join(out)
