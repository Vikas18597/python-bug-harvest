"""Deterministic candidate selection from a source dataset."""

from __future__ import annotations

import json
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
    one_per_project: bool = True,
) -> list[dict]:
    excluded = {p.lower() for p in exclude_projects}
    pool = [
        r
        for r in rows
        if r["created_at"] >= created_from
        and r["repo"].split("/")[-1].lower() not in excluded
        and r["repo"].lower() not in excluded
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
    seen: set[str] = set()
    for r in pool:
        project = r["repo"].lower()
        if one_per_project and project in seen:
            continue
        seen.add(project)
        chosen.append(r)
        if len(chosen) >= limit:
            break
    return chosen


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
