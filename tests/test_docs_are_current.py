"""The documentation is checked, not trusted.

Two of the three defects in the 1.3.2 review were documentation drift: the
README claimed Go/Rust/Java produce `INCONCLUSIVE` months after their runners
shipped, and the detection error recommended a flag that did not exist. Prose
rots silently, so the claims that *can* be checked mechanically are checked
here and the suite fails when they stop being true.
"""

import pathlib
import re

import pytest

from coretexa_verify.cli import build_parser
from coretexa_verify.verify import __version__

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()
CHANGELOG = (ROOT / "CHANGELOG.md").read_text()
SECURITY = (ROOT / "SECURITY.md").read_text()
ACTION = (ROOT / "action.yml").read_text()
ACTION_MAIN = (ROOT / "src" / "coretexa_verify" / "action_main.py").read_text()
PYPROJECT = (ROOT / "pyproject.toml").read_text()


# --------------------------------------------------------------------------
# versions agree
# --------------------------------------------------------------------------


def test_the_package_version_matches_the_code():
    assert f'version = "{__version__}"' in PYPROJECT


def test_the_changelog_leads_with_this_version():
    first = re.search(r"^## (\S+)", CHANGELOG, re.MULTILINE)
    assert first is not None, "CHANGELOG.md has no version sections"
    assert first.group(1) == __version__


def test_the_changelog_covers_every_shipped_version():
    versions = re.findall(r"^## (\d+\.\d+\.\d+)", CHANGELOG, re.MULTILINE)
    for expected in ("1.0.0", "1.1.0", "1.2.0", "1.2.1", "1.3.0", "1.3.1", "1.3.2"):
        assert expected in versions, expected


# --------------------------------------------------------------------------
# every flag the docs recommend exists
# --------------------------------------------------------------------------


def _cli_flags() -> set:
    flags = set()
    for action in build_parser()._actions:
        flags.update(action.option_strings)
    return flags


def _our_flags_in(text: str) -> set:
    """Flags the document is presenting as *ours*.

    Two shapes count, and nothing else: a flag alone in backticks (``--foo``),
    which is how prose refers to one of our options, and a flag on a line that
    invokes ``coretexa_verify``. Everything else in the README is a third-party
    command being quoted - ``pip install --no-input``, ``uv run --frozen`` - and
    a denylist of those would need updating every time an example changed.
    """
    found = set(re.findall(r"`(--[a-z][a-z0-9-]*)`", text))
    for line in text.splitlines():
        if "coretexa_verify" in line or "coretexa-verify --" in line:
            found.update(re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]+)", line))
    return found


#: Third-party flags the docs quote in prose while discussing another tool's
#: behaviour. Short and explicit on purpose: a *fabricated* flag is not in here,
#: so the check below still catches the defect it exists for.
FOREIGN_FLAGS = {
    "--disable-pip-version-check",  # pip
    "--no-input",  # pip
    "--frozen-lockfile",  # yarn classic
    "--immutable",  # yarn berry
}


@pytest.mark.parametrize("document", ["README.md", "SECURITY.md"])
def test_every_flag_the_docs_mention_is_real(document):
    """The exact defect: a message recommending `--test-command` when it did not exist."""
    text = (ROOT / document).read_text()
    unknown = sorted(_our_flags_in(text) - _cli_flags() - FOREIGN_FLAGS)
    assert unknown == [], f"{document} names flags the CLI does not have: {unknown}"


def test_the_flag_check_would_have_caught_the_defect():
    """Guard the guard: a fabricated flag must fail the check above."""
    assert _our_flags_in("pass `--not-a-real-flag` to proceed") - _cli_flags() == {
        "--not-a-real-flag"
    }


def test_the_detection_failure_message_only_recommends_real_flags():
    from coretexa_verify.runners import DetectionFailed

    message = str(DetectionFailed("/tmp/x", ["python"]))
    for flag in re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]+)", message):
        assert flag in _cli_flags(), f"the detection error recommends {flag}, which does not exist"


# --------------------------------------------------------------------------
# every Action input is wired end to end
# --------------------------------------------------------------------------


def _declared_inputs() -> list:
    block = ACTION.split("outputs:", 1)[0].split("inputs:", 1)[1]
    return re.findall(r"^  ([a-z][a-z0-9-]*):$", block, re.MULTILINE)


def test_every_action_input_is_passed_into_the_env_block():
    for name in _declared_inputs():
        assert f"inputs.{name} }}}}" in ACTION, f"input {name} is declared but never mapped to env"


def test_every_mapped_env_var_is_read_by_action_main():
    mapped = re.findall(r"^        (INPUT_[A-Z_]+):", ACTION, re.MULTILINE)
    assert mapped, "no INPUT_* env vars found in action.yml"
    for name in mapped:
        assert f'"{name}"' in ACTION_MAIN, f"{name} is mapped in action.yml but never read"


def test_the_readme_documents_the_new_inputs():
    for name in ("test-command", "junit-path"):
        assert name in README, f"the {name} input is undocumented"
        assert name in ACTION, f"the {name} input is not declared in action.yml"


# --------------------------------------------------------------------------
# language claims
# --------------------------------------------------------------------------


def test_the_readme_does_not_still_say_go_rust_java_are_unsupported():
    """The stale sentence, verbatim, must not come back."""
    stale = [
        "Go, Rust, Java, C++,\n  Ruby and .NET produce `INCONCLUSIVE` — no runner is detected",
        "anything outside\n  pytest/jest/vitest today",
        "Languages other than Python and JavaScript/TypeScript",
    ]
    for phrase in stale:
        assert phrase not in README, f"stale claim back in the README: {phrase!r}"


def test_the_readme_names_every_runner_module():
    modules = sorted(
        p.stem
        for p in (ROOT / "src" / "coretexa_verify" / "runners").glob("*.py")
        if p.stem not in ("__init__", "base")
    )
    assert modules, "no runner modules found"
    for module in modules:
        assert f"{module}.py" in README, f"runners/{module}.py is missing from the README layout"


def test_the_readme_test_count_is_the_real_one():
    """The one number that always rots. Asked of pytest, not of a regex."""
    import os
    import subprocess
    import sys

    m = re.search(r"pytest tests -q\s*#\s*(\d+) tests", README)
    assert m is not None, "the Development section no longer states a test count"
    claimed = int(m.group(1))

    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q", "-p", "no:randomly"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )
    found = re.search(r"(\d+) tests? collected", res.stdout)
    assert found is not None, res.stdout[-2000:]
    collected = int(found.group(1))
    assert claimed == collected, (
        f"README claims {claimed} tests, pytest collects {collected}. Update the "
        f"Development section."
    )


# --------------------------------------------------------------------------
# the security documentation exists and says the necessary things
# --------------------------------------------------------------------------


def test_the_readme_has_a_security_section():
    assert "\n## Security\n" in README


def test_the_security_docs_warn_about_pull_request_target():
    for text in (README, SECURITY):
        assert "pull_request_target" in text
        assert "secrets" in text


def test_the_security_docs_show_a_least_privilege_permissions_block():
    assert "permissions:\n  contents: read" in README
    assert "pull-requests: write" in README


def test_the_security_docs_say_no_token_means_no_comment():
    assert "no `github-token`" in README or "With no `github-token`" in README
    assert "job summary" in README


def test_the_security_docs_name_the_allowlist_variable():
    from coretexa_verify.gitops import ALLOWLIST_ENV_VAR

    for text in (README, SECURITY):
        assert ALLOWLIST_ENV_VAR in text
