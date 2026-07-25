"""Optional per-repo configuration.

Ships with defaults that work; a repository can override file classification by
adding ``.coretexa-verify.toml`` at its root::

    [classify]
    test_dir_patterns = ["^tests?$", "^checks$"]
    executable_test_patterns = ["test_*.py", "check_*.py"]
    force_test_globs = ["scenarios/**"]
    force_source_globs = ["tests/support/production_shim.py"]

Config is additive to nothing: a key you set replaces that default list
entirely, which keeps the behaviour predictable.
"""

from __future__ import annotations

import os

from .classify import ClassifierConfig

CONFIG_FILENAME = ".coretexa-verify.toml"

_FIELDS = (
    "test_dir_patterns",
    "executable_test_patterns",
    "other_patterns",
    "other_dir_prefixes",
    "force_test_globs",
    "force_source_globs",
)


def load_config(repo: str) -> tuple[ClassifierConfig, list[str]]:
    """Return ``(config, warnings)``. Missing or unreadable config is not fatal."""
    cfg = ClassifierConfig()
    warnings: list[str] = []
    path = os.path.join(repo, CONFIG_FILENAME)
    if not os.path.exists(path):
        return cfg, warnings

    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        warnings.append(f"{CONFIG_FILENAME} found but tomllib is unavailable; using defaults")
        return cfg, warnings

    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        warnings.append(f"could not read {CONFIG_FILENAME}: {exc}; using defaults")
        return cfg, warnings

    section = data.get("classify") or {}
    for key in _FIELDS:
        if key in section:
            value = section[key]
            if isinstance(value, list) and all(isinstance(v, str) for v in value):
                setattr(cfg, key, list(value))
            else:
                warnings.append(f"{CONFIG_FILENAME}: [classify].{key} must be a list of strings; ignored")
    unknown = set(section) - set(_FIELDS)
    if unknown:
        warnings.append(f"{CONFIG_FILENAME}: ignoring unknown key(s) {', '.join(sorted(unknown))}")
    return cfg, warnings
