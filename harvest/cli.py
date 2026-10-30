"""Command-line entry point: `python -m harvest <command>`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import aggregate, kaggle_upload, select, sources, verify


def _load_plan(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def cmd_select(a: argparse.Namespace) -> int:
    rows = sources.load(a.source, a.split)
    exclude = {p.strip() for p in a.exclude.split(",") if p.strip()}
    chosen = select.select(rows, limit=a.limit, created_from=a.created_from, exclude_projects=exclude)
    criteria = {"limit": a.limit, "created_from": a.created_from, "exclude_projects": sorted(exclude),
                "one_per_project": True,
                "order": "dataset python version desc, simpler recipe, created_at desc, instance_id"}
    select.write_candidate_file(Path(a.out), source=a.source, split=a.split, criteria=criteria, chosen=chosen)
    print(f"wrote {len(chosen)} candidates from {len({c['repo'] for c in chosen})} projects to {a.out}")
    return 0


def make_plan(candidates: Path, out: Path, per_job: int, only: set[str] | None = None) -> list[dict]:
    doc = select.read_candidate_file(candidates)
    ids = [i for i in doc["instance_ids"] if not only or i in only]
    rows = {r["instance_id"]: r for r in sources.load(doc["source"], doc["split"], set(ids))}
    missing = [i for i in ids if i not in rows]
    if missing:
        raise SystemExit(f"instance ids not found in {doc['dataset']}@{doc['revision']}: {missing}")
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "candidates.jsonl", "w", encoding="utf-8") as fh:
        for i in ids:
            fh.write(json.dumps(rows[i]) + "\n")
    shards = [{"shard": n, "ids": ",".join(ids[k:k + per_job])} for n, k in enumerate(range(0, len(ids), per_job))]
    (out / "matrix.json").write_text(json.dumps(shards), encoding="utf-8")
    return shards


def cmd_plan(a: argparse.Namespace) -> int:
    shards = make_plan(Path(a.candidates), Path(a.out), a.per_job)
    if len(shards) > 256:
        raise SystemExit(f"{len(shards)} shards exceeds the GitHub Actions 256-job matrix limit; raise --per-job")
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"matrix={json.dumps(shards)}\n")
    print(f"planned {len(shards)} job(s) in {a.out}")
    return 0


def _timeouts(a: argparse.Namespace) -> dict:
    return {"tests": a.test_timeout, "step": a.step_timeout}


def cmd_verify(a: argparse.Namespace) -> int:
    plan = _load_plan(Path(a.plan))
    wanted = [i for i in a.ids.split(",") if i] if a.ids else [c["instance_id"] for c in plan]
    by_id = {c["instance_id"]: c for c in plan}
    for iid in wanted:
        verify.verify(by_id[iid], Path(a.out), target_python=a.python, timeouts=_timeouts(a), keep_work=a.keep_work)
    return 0


def cmd_aggregate(a: argparse.Namespace) -> int:
    total = len(_load_plan(Path(a.plan))) if a.plan else None
    summary = aggregate.aggregate(Path(a.results), Path(a.out), candidates_total=total)
    print(aggregate.render_markdown(summary))
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write("## Harvest summary\n\n" + aggregate.render_markdown(summary))
    return 0


def cmd_kaggle(a: argparse.Namespace) -> int:
    kaggle_upload.upload(Path(a.dir), a.slug, a.title, a.message)
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    """Local all-in-one: plan -> verify sequentially -> aggregate."""
    if os.name == "nt":
        print("warning: verification is designed for Linux (apt pre-install steps, POSIX tests); "
              "results on Windows may differ from CI.", file=sys.stderr)
    out = Path(a.out)
    only = {i for i in a.only.split(",") if i} if a.only else None
    make_plan(Path(a.candidates), out / "plan", per_job=1, only=only)
    for cand in _load_plan(out / "plan" / "candidates.jsonl"):
        verify.verify(cand, out / "results", target_python=a.python, timeouts=_timeouts(a), keep_work=a.keep_work)
    summary = aggregate.aggregate(out / "results", out / "dataset",
                                  candidates_total=len(_load_plan(out / "plan" / "candidates.jsonl")))
    print(aggregate.render_markdown(summary))
    return 0


def _add_verify_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--python", default="3.13", help="target Python version tried first (default 3.13)")
    p.add_argument("--test-timeout", type=int, default=verify.DEFAULT_TIMEOUTS["tests"])
    p.add_argument("--step-timeout", type=int, default=verify.DEFAULT_TIMEOUTS["step"])
    p.add_argument("--keep-work", action="store_true", help="keep the cloned repo and venvs for debugging")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="harvest", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("select", help="pick a deterministic candidate list from a source dataset")
    p.add_argument("--source", default="swe-rebench", choices=sorted(sources.SOURCES))
    p.add_argument("--split", default="filtered")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--created-from", default="2023")
    p.add_argument("--exclude", default="", help="comma-separated project names to skip")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_select)

    p = sub.add_parser("plan", help="materialise candidate rows and the CI job matrix")
    p.add_argument("--candidates", required=True)
    p.add_argument("--out", default="out/plan")
    p.add_argument("--per-job", type=int, default=1, help="candidates per CI job")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("verify", help="rebuild and verify candidates from a plan")
    p.add_argument("--plan", default="out/plan/candidates.jsonl")
    p.add_argument("--ids", default="", help="comma-separated instance ids (default: all in plan)")
    p.add_argument("--out", default="out/results")
    _add_verify_opts(p)
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("aggregate", help="merge results into harvest.jsonl + snapshots + summary")
    p.add_argument("--results", default="out/results")
    p.add_argument("--plan", default="", help="plan candidates.jsonl, to count missing results")
    p.add_argument("--out", default="out/dataset")
    p.set_defaults(fn=cmd_aggregate)

    p = sub.add_parser("kaggle-upload", help="upload to a private Kaggle dataset (no-op without credentials)")
    p.add_argument("--dir", default="out/dataset")
    p.add_argument("--slug", default="python-bug-harvest")
    p.add_argument("--title", default="python-bug-harvest")
    p.add_argument("--message", default="harvest update")
    p.set_defaults(fn=cmd_kaggle)

    p = sub.add_parser("run", help="local end-to-end run (plan, verify, aggregate)")
    p.add_argument("--candidates", required=True)
    p.add_argument("--only", default="", help="comma-separated subset of instance ids")
    p.add_argument("--out", default="out")
    _add_verify_opts(p)
    p.set_defaults(fn=cmd_run)

    a = ap.parse_args(argv)
    return a.fn(a)
