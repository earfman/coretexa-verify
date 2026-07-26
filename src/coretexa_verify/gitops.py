"""Thin, timeout-bounded wrapper around git, plus the revert/restore machinery.

Three rules govern this module:

1. Every subprocess has a timeout and the timeout is reported, never swallowed.
2. The user's working tree is put back exactly as we found it, on every path out
   of the function, including exceptions.
3. **No subprocess that executes repository-controlled code inherits a secret.**

Rule 3 is the one worth spelling out. This tool runs a pull request's own test
suite, its own build step and its own dependency installer - all of which are
code the PR author wrote - and it runs them inside a GitHub Action that may
have been handed a token so it can post a comment. Passing ``os.environ``
straight through would hand that token, and every other credential the job
happens to hold, to the code under examination. ``run(..., isolate=True)``
strips them first; see :func:`sanitized_environ`.

The token is read exactly once, in :mod:`coretexa_verify.action_main`, *after*
the experiment is over, out of that process's own environment - never out of a
child's.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from .models import ChangedFile, Kind


class GitError(RuntimeError):
    pass


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


# --------------------------------------------------------------------------
# secret isolation
# --------------------------------------------------------------------------

#: Names removed by exact match. These carry no ``TOKEN``-shaped suffix in
#: every spelling a CI system uses, so listing them is the only reliable way.
SECRET_ENV_NAMES = frozenset(
    {
        "INPUT_GITHUB_TOKEN",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "ACTIONS_RUNTIME_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "NPM_TOKEN",
        "NODE_AUTH_TOKEN",
        "TWINE_PASSWORD",
        "CODECOV_TOKEN",
        "SSH_AUTH_SOCK",
        # Legacy spellings with no separator before the secret word, which the
        # pattern below deliberately requires.
        "PGPASSWORD",
        "PGPASSFILE",
        "MYSQL_PWD",
        "NETRC",
    }
)

#: Names removed by shape. Anchored at the end so ``TOKEN_URL`` survives while
#: ``MY_SERVICE_TOKEN`` does not, and case-insensitive so lowercase spellings
#: (``npm_token``) are caught too.
SECRET_ENV_PATTERN = re.compile(
    r"(?:^|_)(TOKEN|SECRET|SECRETS|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|CREDENTIALS|"
    r"API_KEY|APIKEY|ACCESS_KEY|PRIVATE_KEY|AUTH|PAT)$",
    re.IGNORECASE,
)

#: Escape hatch, comma-separated. A repository whose test suite genuinely needs
#: one of these (an integration test against a real service) can name it here
#: and it will be passed through. Deliberately opt-in and per-name: there is no
#: "disable isolation entirely" switch.
ALLOWLIST_ENV_VAR = "CORETEXA_VERIFY_ALLOW_ENV"


def allowlisted_env_names(base: "dict[str, str] | None" = None) -> frozenset:
    """Names the user has explicitly asked us to keep, upper-cased."""
    source = os.environ if base is None else base
    raw = source.get(ALLOWLIST_ENV_VAR) or ""
    return frozenset(part.strip() for part in raw.replace(";", ",").split(",") if part.strip())


def is_secret_env_name(name: str, allowed: "frozenset | None" = None) -> bool:
    """Would passing this variable to repository-controlled code leak a secret?"""
    if allowed and name in allowed:
        return False
    if name in SECRET_ENV_NAMES:
        return True
    return bool(SECRET_ENV_PATTERN.search(name))


def sanitized_environ(base: "dict[str, str] | None" = None) -> dict:
    """A copy of the environment with every credential-shaped variable removed.

    This is what every subprocess that executes repository-controlled code gets
    - the dependency installer, the build step, the test run and the collection
    pass. Everything else about the environment is preserved, because a test
    suite legitimately depends on ``PATH``, ``HOME``, ``LANG``, ``CI``,
    language-specific caches and so on; removing more than we must would break
    real repositories for no security gain.
    """
    source = dict(os.environ if base is None else base)
    allowed = allowlisted_env_names(source)
    return {k: v for k, v in source.items() if not is_secret_env_name(k, allowed)}


def redacted_env_names(base: "dict[str, str] | None" = None) -> list[str]:
    """Which variables :func:`sanitized_environ` would drop. For the report."""
    source = dict(os.environ if base is None else base)
    allowed = allowlisted_env_names(source)
    return sorted(k for k in source if is_secret_env_name(k, allowed))


def run(
    argv: list[str],
    cwd: str,
    timeout: int = 120,
    env: dict[str, str] | None = None,
    isolate: bool = False,
) -> CommandResult:
    """Run a command, never raising on non-zero exit; timeouts are data, not exceptions.

    ``isolate=True`` means "this command is repository-controlled": it gets
    :func:`sanitized_environ` as its base instead of ``os.environ``, so no
    token or credential in the job's environment can reach it. Every call site
    that runs a test suite, a build, an installer or a collection sets it. git
    itself does not, because git is ours and may legitimately need the job's
    credentials to fetch a base ref from a private repository.
    """
    full_env = sanitized_environ() if isolate else dict(os.environ)
    if env:
        full_env.update(env)
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=full_env,
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            argv=argv,
            returncode=None,  # type: ignore[arg-type]
            stdout=_decode(exc.stdout),
            stderr=_decode(exc.stderr),
            timed_out=True,
        )
    except FileNotFoundError as exc:
        return CommandResult(argv=argv, returncode=127, stdout="", stderr=str(exc))
    return CommandResult(
        argv=argv, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
    )


def _decode(blob: object) -> str:
    if blob is None:
        return ""
    if isinstance(blob, bytes):
        return blob.decode("utf-8", "replace")
    return str(blob)


def git(repo: str, *args: str, timeout: int = 120) -> CommandResult:
    return run(["git", *args], cwd=repo, timeout=timeout)


def git_bytes(repo: str, *args: str, timeout: int = 120) -> bytes | None:
    """Run git and return raw stdout, or None on failure. Used for blob content."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, timeout=timeout
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def show_blob(repo: str, ref: str, path: str) -> bytes | None:
    """Content of ``path`` at ``ref``, or None if it does not exist there.

    Preferred over ``git checkout <ref> -- <path>`` because it does not touch
    the index; mutating the index would make restoration ambiguous and would
    make a later ``git checkout -- .`` actively destructive.
    """
    return git_bytes(repo, "show", f"{ref}:{path}")


def git_ok(repo: str, *args: str, timeout: int = 120) -> str:
    res = git(repo, *args, timeout=timeout)
    if res.timed_out:
        raise GitError(f"`git {' '.join(args)}` timed out after {timeout}s")
    if res.returncode != 0:
        raise GitError(f"`git {' '.join(args)}` failed ({res.returncode}): {res.stderr.strip()}")
    return res.stdout.strip()


def is_git_repo(path: str) -> bool:
    res = git(path, "rev-parse", "--git-dir")
    return res.returncode == 0


def rev_parse(repo: str, ref: str) -> str:
    return git_ok(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")


def current_ref(repo: str) -> str:
    """Branch name if we are on one, otherwise the raw SHA (detached HEAD)."""
    res = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip()
    return git_ok(repo, "rev-parse", "HEAD")


def is_clean(repo: str) -> bool:
    return git_ok(repo, "status", "--porcelain", "--untracked-files=no") == ""


def dirty_paths(repo: str) -> list[str]:
    # NB: do not strip the output. Porcelain v1 status codes are two columns
    # wide and " M mod.py" (worktree-modified, unstaged) starts with a space;
    # stripping it first and then slicing [3:] eats the first character of the
    # path, which is how this used to report "od.py".
    res = git(repo, "status", "--porcelain", "--untracked-files=no")
    if res.returncode != 0:
        raise GitError(f"`git status` failed ({res.returncode}): {res.stderr.strip()}")
    paths = []
    for line in res.stdout.splitlines():
        if len(line) <= 3:
            continue
        path = line[3:]
        # Renames are reported as "ORIG -> DEST"; the destination is the one
        # that exists in the tree we would have to restore.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path)
    return paths


def untracked_paths(repo: str) -> list[str]:
    """Untracked, non-ignored paths, with directories collapsed to ``dir/``.

    ``--untracked-files=normal`` (rather than ``all``) keeps this bounded: a
    freshly installed ``node_modules`` is one entry, not forty thousand.
    """
    res = git(repo, "status", "--porcelain", "--untracked-files=normal")
    if res.returncode != 0:
        return []
    return [line[3:] for line in res.stdout.splitlines() if line.startswith("?? ")]


@dataclass
class TreeState:
    """A snapshot of what git thinks is modified and what is merely lying around.

    Taken once before and once after the dependency install so that anything
    the install generates - ``*.egg-info/``, ``build/``, a regenerated
    ``_version.py`` - can be attributed to the install rather than to the user
    or to us. See the artefact policy in :mod:`coretexa_verify.verify`.
    """

    tracked_dirty: frozenset
    untracked: frozenset

    @classmethod
    def capture(cls, repo: str) -> "TreeState":
        try:
            tracked = frozenset(dirty_paths(repo))
        except GitError:  # pragma: no cover - defensive
            tracked = frozenset()
        return cls(tracked_dirty=tracked, untracked=frozenset(untracked_paths(repo)))

    def new_tracked_since(self, earlier: "TreeState") -> list[str]:
        return sorted(self.tracked_dirty - earlier.tracked_dirty)

    def new_untracked_since(self, earlier: "TreeState") -> list[str]:
        return sorted(self.untracked - earlier.untracked)


def merge_base(repo: str, base: str, head: str, deepen_rounds: int = 3) -> str:
    """Find the fork point, deepening a shallow clone if we have to.

    Comparing against the merge base rather than the tip of the base branch is
    what makes the result about *this PR* instead of about everything that
    landed on main since the PR was opened.
    """
    res = git(repo, "merge-base", base, head)
    if res.returncode == 0:
        return res.stdout.strip()
    if is_shallow(repo):
        for _ in range(deepen_rounds):
            deep = git(repo, "fetch", "--deepen", "100", timeout=300)
            if deep.returncode != 0 and not deep.timed_out:
                break
            res = git(repo, "merge-base", base, head)
            if res.returncode == 0:
                return res.stdout.strip()
    raise GitError(
        f"no merge base between {base!r} and {head!r}"
        + (" (shallow clone: try `git fetch --unshallow`)" if is_shallow(repo) else "")
    )


def is_shallow(repo: str) -> bool:
    res = git(repo, "rev-parse", "--is-shallow-repository")
    return res.returncode == 0 and res.stdout.strip() == "true"


_RENAME = re.compile(r"^R\d*$")


def changed_files(repo: str, base: str, head: str) -> list[tuple[str, str, str | None]]:
    """Return ``(status, path, old_path)`` for every file the PR touches.

    ``path`` is always the head-side path; ``old_path`` is set for renames.
    """
    out = git_ok(repo, "diff", "--name-status", "-M", f"{base}", f"{head}")
    result: list[tuple[str, str, str | None]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0].strip()
        if _RENAME.match(status) and len(parts) >= 3:
            result.append(("R", parts[2], parts[1]))
        else:
            result.append((status[0], parts[-1], None))
    return result


# --------------------------------------------------------------------------
# Revert / restore
# --------------------------------------------------------------------------


class TreeMutator:
    """Reverts a set of files to their base content and always puts them back.

    We back up the exact bytes rather than relying on ``git checkout HEAD --``
    so that restoration is correct even if the checkout is at a detached commit,
    a merge commit, or has content git would consider unchanged.
    """

    def __init__(self, repo: str, base_sha: str):
        self.repo = repo
        self.base_sha = base_sha
        self._backup_dir: str | None = None
        self._backed_up: list[tuple[str, str | None]] = []  # (relpath, backup file or None if absent)
        self.reverted: list[str] = []
        self.errors: list[str] = []

    def __enter__(self) -> "TreeMutator":
        self._backup_dir = tempfile.mkdtemp(prefix="coretexa-verify-backup-")
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.restore()
        return False

    def _backup(self, rel: str) -> None:
        assert self._backup_dir is not None
        abs_path = os.path.join(self.repo, rel)
        if os.path.exists(abs_path):
            dest = os.path.join(self._backup_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(abs_path, dest)
            self._backed_up.append((rel, dest))
        else:
            self._backed_up.append((rel, None))

    def revert(self, files: list[ChangedFile], kinds: tuple = (Kind.SOURCE,)) -> None:
        """Restore files of the given kinds to their base-commit content, in place.

        ``kinds`` defaults to SOURCE, which is the experiment. The fixture probe
        passes TEST instead, to revert a fixture on its own and see whether the
        selected tests notice - that is the only way to *prove* a fixture is
        actually consumed by the tests we chose.
        """
        for f in files:
            if f.kind not in kinds:
                continue
            targets = [f.path] if f.old_path is None else [f.path, f.old_path]
            for rel in targets:
                self._backup(rel)
            if f.status == "A":
                # Added at head: reverting means the file should not exist.
                self._remove(f.path)
                self.reverted.append(f.path)
            elif f.status == "R":
                self._remove(f.path)
                if f.old_path:
                    self._checkout_base(f.old_path)
                self.reverted.append(f.path)
            else:  # M or D
                self._checkout_base(f.path)
                self.reverted.append(f.path)

    def _remove(self, rel: str) -> None:
        abs_path = os.path.join(self.repo, rel)
        try:
            if os.path.exists(abs_path):
                os.remove(abs_path)
        except OSError as exc:  # pragma: no cover - filesystem edge case
            self.errors.append(f"could not remove {rel}: {exc}")

    def write(self, rel: str, content: bytes) -> None:
        """Overwrite a file, backing up its current bytes first."""
        self._backup(rel)
        self._write_now(rel, content)

    def _write_now(self, rel: str, content: bytes) -> None:
        abs_path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(abs_path) or self.repo, exist_ok=True)
        with open(abs_path, "wb") as fh:
            fh.write(content)

    def _checkout_base(self, rel: str) -> None:
        content = show_blob(self.repo, self.base_sha, rel)
        if content is None:
            # The path does not exist at base. Reverting means deleting it.
            self._remove(rel)
        else:
            self._write_now(rel, content)

    def restore(self) -> None:
        """Put every touched file back, byte for byte. Safe to call twice."""
        if self._backup_dir is None:
            return
        for rel, backup in reversed(self._backed_up):
            abs_path = os.path.join(self.repo, rel)
            try:
                if backup is None:
                    if os.path.exists(abs_path):
                        os.remove(abs_path)
                else:
                    os.makedirs(os.path.dirname(abs_path) or self.repo, exist_ok=True)
                    shutil.copy2(backup, abs_path)
            except OSError as exc:  # pragma: no cover
                self.errors.append(f"could not restore {rel}: {exc}")
        self._backed_up = []
        shutil.rmtree(self._backup_dir, ignore_errors=True)
        self._backup_dir = None


# --------------------------------------------------------------------------
# PR URL convenience mode (plain git over HTTPS; no api.github.com)
# --------------------------------------------------------------------------

PR_URL_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
)


@dataclass
class PullRequestRef:
    owner: str
    repo: str
    number: int

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}.git"

    @property
    def local_branch(self) -> str:
        return f"coretexa-pr{self.number}"

    @property
    def refspec(self) -> str:
        return f"pull/{self.number}/head:{self.local_branch}"


def parse_pr_url(url: str) -> PullRequestRef:
    m = PR_URL_RE.match(url.strip())
    if not m:
        raise GitError(f"not a GitHub pull request URL: {url!r}")
    return PullRequestRef(m["owner"], m["repo"].removesuffix(".git"), int(m["number"]))


def fetch_pull_request(
    pr: PullRequestRef,
    workdir: str,
    depth: int = 100,
    timeout: int = 600,
) -> str:
    """Clone (or reuse) the repo and fetch ``pull/N/head``. Returns the repo path.

    Deliberately uses only plain git over HTTPS so the tool works in networks
    where the GitHub REST API is unreachable.
    """
    repo_path = os.path.join(workdir, pr.repo)
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        os.makedirs(workdir, exist_ok=True)
        res = run(
            ["git", "clone", "--depth", str(depth), pr.clone_url, repo_path],
            cwd=workdir,
            timeout=timeout,
        )
        if res.timed_out:
            raise GitError(f"clone of {pr.clone_url} timed out after {timeout}s")
        if res.returncode != 0:
            raise GitError(f"clone of {pr.clone_url} failed: {res.stderr.strip()}")
    res = git(repo_path, "fetch", "--depth", str(depth), "origin", pr.refspec, "--force", timeout=timeout)
    if res.timed_out:
        raise GitError(f"fetch of {pr.refspec} timed out after {timeout}s")
    if res.returncode != 0:
        raise GitError(f"fetch of {pr.refspec} failed: {res.stderr.strip()}")
    return repo_path


def default_base_ref(repo: str) -> str:
    """Best guess at the base branch when the user did not name one."""
    res = git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip()
    for candidate in ("origin/main", "origin/master", "main", "master"):
        if git(repo, "rev-parse", "--verify", f"{candidate}^{{commit}}").returncode == 0:
            return candidate
    raise GitError("could not determine a base ref; pass --base explicitly")
