"""Deterministic candidate selection from a source dataset."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from . import sources


def _py_key(version: str | None) -> tuple[int, ...]:
    if not version:
        return (0,)
    try:
        return tuple(int(p) for p in version.split(".")[:2])
    except ValueError:
        return (0,)


def _recipe_penalty(cand: dict) -> int:
    """Lower is better: prefer simple pip-installable recipes likely to work on a new Python."""
    cfg = cand.get("install_config") or {}
    penalty = 0
    if (cfg.get("packages") or "") == "environment.yml":
        penalty += 2
    pre = [p for p in (cfg.get("pre_install") or []) if p not in ("apt-get update", "apt-get install -y gcc")]
    if pre:
        penalty += 1
    return penalty


def select(
    rows: list[dict],
    *,
    limit: int,
    created_from: str,
    exclude_projects: set[str],
    per_project: int = 1,
    previous: Iterable[tuple[str, str]] = (),
) -> list[dict]:
    """Pick up to ``limit`` candidates, at most ``per_project`` per project.

    ``previous`` holds ``(instance_id, repo)`` pairs chosen by earlier selections: those bugs are skipped and
    count toward their project's ``per_project`` cap, so a later batch adds new bugs without repeats. Every
    project gets its first bug before any project gets its second, and so on.
    """
    if per_project < 1:
        raise ValueError(f"per_project must be at least 1, got {per_project}")
    excluded = {p.lower() for p in exclude_projects}
    previous_repos = dict(previous)
    counts = Counter(repo.lower() for repo in previous_repos.values())
    pool = [
        r
        for r in rows
        if r["created_at"] >= created_from
        and r["repo"].split("/")[-1].lower() not in excluded
        and r["repo"].lower() not in excluded
        and r["instance_id"] not in previous_repos
        and r["FAIL_TO_PASS"]
        and r["test_patch"].strip()
    ]
    pool.sort(
        key=lambda r: (
            tuple(-x for x in _py_key((r.get("install_config") or {}).get("python"))),
            _recipe_penalty(r),
            [-ord(c) for c in r["created_at"]],
            r["instance_id"],
        )
    )
    chosen: list[dict] = []
    picked: set[str] = set()
    for cap in range(1, per_project + 1):
        for r in pool:
            if len(chosen) >= limit:
                return chosen
            project = r["repo"].lower()
            if r["instance_id"] in picked or counts[project] >= cap:
                continue
            counts[project] += 1
            picked.add(r["instance_id"])
            chosen.append(r)
    return chosen


def read_previous_candidates(paths: Iterable[Path]) -> list[tuple[str, str]]:
    """``(instance_id, repo)`` pairs from earlier candidate files, for ``select(previous=...)``.

    The pinned revision is not checked: an older list still names bugs that must not be picked again.
    """
    pairs: list[tuple[str, str]] = []
    for path in paths:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        ids, projects = (doc.get("instance_ids"), doc.get("projects")) if isinstance(doc, dict) else (None, None)
        if (not isinstance(ids, list) or not isinstance(projects, list) or len(ids) != len(projects)
                or not all(isinstance(v, str) and v for v in (*ids, *projects))):
            raise ValueError(f"{path} is not a candidate file: it needs instance_ids and projects lists "
                             "of non-empty strings, of equal length")
        pairs.extend(zip(ids, projects))
    return pairs


def write_candidate_file(path: Path, *, source: str, split: str, criteria: dict, chosen: list[dict]) -> None:
    src = sources.SOURCES[source]
    doc = {
        "source": source,
        "dataset": src.hf_repo,
        "revision": src.revision,
        "split": split,
        "criteria": criteria,
        "instance_ids": [c["instance_id"] for c in chosen],
        "projects": [c["repo"] for c in chosen],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def read_candidate_file(path: Path) -> dict:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    src = sources.SOURCES[doc["source"]]
    if doc["revision"] != src.revision:
        raise ValueError(
            f"{path} pins {doc['dataset']}@{doc['revision']} but the harvester pins {src.revision}"
        )
    return doc
