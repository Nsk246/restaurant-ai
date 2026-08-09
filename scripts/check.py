#!/usr/bin/env python3
"""Pre-package checks: the mistakes that have actually cost us hours.

Parses rather than greps, so a comment explaining a bug is not mistaken for
the bug.
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "services" / "voice" / "app"
TESTS = ROOT / "services" / "voice" / "tests"

failures: list[str] = []


def say(label: str, ok: bool, detail: str = "") -> None:
    print(f"{label:<52} {'ok' if ok else 'FAIL'}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


def fixed_depth_parents() -> list[str]:
    """`parents[3]` on /srv/app/main.py raises IndexError at import and the
    container dies before serving anything. This has bitten twice: once in
    the portal lookup, once in the settings env path. Walk ancestors instead.
    """
    hits = []
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "parents"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, int)
            ):
                hits.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return hits


def literal_codes() -> list[str]:
    """Tests suffix dish names per run, so a hardcoded generated code is
    stale the moment it is written."""
    return [
        f"{p.relative_to(ROOT)}:{i + 1}"
        for p in TESTS.rglob("*.py")
        for i, line in enumerate(p.read_text().splitlines())
        if "WHERE code='" in line
    ]


def model_pinned_off() -> bool:
    """The suite must never call a live model: slow, costs money per run, and
    the result depends on what the model felt like returning."""
    return 'os.environ["GEMINI_API_KEY"] = ""' in (TESTS / "conftest.py").read_text()


def env_file_absolute() -> bool:
    """A bare relative ".env" resolves against the working directory, and the
    service is launched from services/voice."""
    return 'env_file=".env"' not in (APP / "config.py").read_text()


hits = fixed_depth_parents()
say("no fixed-depth parents[] indexing", not hits, " ".join(hits))

codes = literal_codes()
say("tests query no literal codes", not codes, " ".join(codes))

say("test suite pinned off the live model", model_pinned_off())
say("settings do not use a relative .env", env_file_absolute())

print()
if failures:
    print(f"{len(failures)} check(s) failed")
    sys.exit(1)
print("all checks passed")
