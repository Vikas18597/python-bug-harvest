"""Dataset sources: download pinned Hugging Face parquet files and normalise rows."""

from __future__ import annotations

import json
import os
import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Source:
    name: str
    hf_repo: str
    revision: str
    licence: str
    synthetic: bool
    splits: dict[str, list[str]]


SOURCES: dict[str, Source] = {
    "swe-rebench": Source(
        name="swe-rebench",
        hf_repo="nebius/SWE-rebench",
        revision="89cdfbab4ab1bd8f5a658bb212d1b63624f4f881",
        licence="CC-BY-4.0",
        synthetic=False,
        splits={
            "filtered": ["data/filtered-00000-of-00001.parquet"],
            "test": ["data/test-00000-of-00002.parquet", "data/test-00001-of-00002.parquet"],
        },
    ),
    "swe-bench": Source(
        name="swe-bench",
        hf_repo="princeton-nlp/SWE-bench",
        revision="e48e2bd1e9fecd5bbd641e9414ac59da9f2e69f6",
        licence="MIT",
        synthetic=False,
        splits={
            "test": ["data/test-00000-of-00001.parquet"],
            "dev": ["data/dev-00000-of-00001.parquet"],
            "train": ["data/train-00000-of-00001.parquet"],
        },
    ),
    "swe-gym": Source(
        name="swe-gym",
        hf_repo="SWE-Gym/SWE-Gym",
        revision="bb94ed9e39bbeb96a7fcbfb533b80f25a7fd59cb",
        licence="MIT",
        synthetic=False,
        splits={"train": ["data/train-00000-of-00001.parquet"]},
    ),
}


def cache_dir() -> Path:
    return Path(os.environ.get("HARVEST_CACHE", Path.cwd() / ".cache" / "datasets"))


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last_err: Exception | None = None
    for _ in range(5):
        try:
            with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as fh:
                shutil.copyfileobj(resp, fh)
            tmp.replace(dest)
            return
        except OSError as err:  # network blips: retry
            last_err = err
    raise RuntimeError(f"download failed: {url}: {last_err}")


def parquet_files(source: Source, split: str) -> list[Path]:
    if split not in source.splits:
        raise ValueError(f"{source.name} has no split {split!r}; choose from {sorted(source.splits)}")
    out = []
    for rel in source.splits[split]:
        dest = cache_dir() / source.hf_repo.replace("/", "__") / source.revision / rel
        if not dest.exists():
            url = f"https://huggingface.co/datasets/{source.hf_repo}/resolve/{source.revision}/{rel}"
            print(f"downloading {url}", flush=True)
            _download(url, dest)
        out.append(dest)
    return out


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = value.strip()
        return list(json.loads(value)) if value else []
    return [str(v) for v in value]


def normalise(source: Source, row: dict) -> dict:
    """Map a raw dataset row onto the harvester's common candidate schema."""
    return {
        "instance_id": row["instance_id"],
        "repo": row["repo"],
        "base_commit": row["base_commit"],
        "environment_setup_commit": row.get("environment_setup_commit"),
        "created_at": str(row.get("created_at") or ""),
        "version": None if row.get("version") is None else str(row.get("version")),
        "problem_statement": row.get("problem_statement") or "",
        "hints_text": row.get("hints_text") or "",
        "patch": row["patch"],
        "test_patch": row["test_patch"],
        "FAIL_TO_PASS": _as_list(row.get("FAIL_TO_PASS")),
        "PASS_TO_PASS": _as_list(row.get("PASS_TO_PASS")),
        "install_config": row.get("install_config"),
        "requirements": row.get("requirements"),
        "repo_license": row.get("license_name"),
        "source": source.name,
        "source_dataset": source.hf_repo,
        "source_revision": source.revision,
        "source_licence": source.licence,
        "synthetic": source.synthetic,
    }


def load(source_name: str, split: str, instance_ids: set[str] | None = None) -> list[dict]:
    import pyarrow.parquet as pq

    source = SOURCES[source_name]
    rows: list[dict] = []
    for path in parquet_files(source, split):
        for row in pq.read_table(path).to_pylist():
            if instance_ids is None or row["instance_id"] in instance_ids:
                rows.append(normalise(source, row))
    return rows
