"""Parse pytest output into {test_id: status} maps and judge FAIL_TO_PASS / PASS_TO_PASS."""

from __future__ import annotations

import re

STATUSES = ("PASSED", "FAILED", "ERROR", "SKIPPED", "XFAIL", "XPASS")
PASSING = {"PASSED", "XFAIL", "XPASS"}

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_VERBOSE = re.compile(r"^(\S+::\S.*?) (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s+\[\s*\d+%\])?\s*$")


def parse_pytest_log(text: str) -> dict[str, str]:
    """Return a map of test id -> status from `pytest -rA` summaries or `-v` progress lines.

    Each summary line is registered under its full id and under its first whitespace-delimited
    token, because SWE-bench-style datasets record ids produced by `line.split()[1]`.
    """
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = _ANSI.sub("", raw).rstrip()
        m = _VERBOSE.match(line)
        if m:
            result.setdefault(m.group(1), m.group(2))
            continue
        for status in STATUSES:
            if not line.startswith(status + " "):
                continue
            rest = line[len(status) + 1 :]
            if status == "SKIPPED" or not rest or rest.startswith("["):
                break
            full = rest.split(" - ", 1)[0].strip()
            result[full] = status
            result.setdefault(rest.split()[0], status)
            break
    return result


def passed(statuses: dict[str, str], test_id: str) -> bool:
    return statuses.get(test_id) in PASSING


def judge(f2p: list[str], p2p: list[str], before: dict[str, str], after: dict[str, str]) -> dict:
    """Apply the verification contract and return a verdict with per-list evidence."""
    f2p_pass_before = [t for t in f2p if passed(before, t)]
    f2p_fail_after = [t for t in f2p if not passed(after, t)]
    p2p_fail_after = [t for t in p2p if not passed(after, t)]
    if not after:
        reason = "no_tests_collected"
    elif f2p_pass_before:
        reason = "f2p_passes_without_fix"
    elif f2p_fail_after:
        reason = "f2p_fails_with_fix"
    elif p2p_fail_after:
        reason = "p2p_fails_with_fix"
    else:
        reason = None
    return {
        "verified": reason is None,
        "reason": reason,
        "f2p_pass_before": f2p_pass_before,
        "f2p_fail_after": f2p_fail_after,
        "p2p_fail_after": p2p_fail_after,
        "f2p_status_after": {t: after.get(t, "MISSING") for t in f2p},
    }
