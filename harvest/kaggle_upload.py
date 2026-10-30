"""Upload the harvested dataset to a PRIVATE Kaggle dataset. No-op when credentials are absent."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SKIP_MESSAGE = (
    "Kaggle upload skipped: KAGGLE_USERNAME / KAGGLE_KEY are not set "
    "(add them as repository secrets to enable the upload)."
)


def upload(data_dir: Path, slug: str, title: str, message: str, runner=subprocess.run) -> str:
    """Create or version `<username>/<slug>` from `data_dir`. Returns "skipped", "created" or "versioned"."""
    user = os.environ.get("KAGGLE_USERNAME", "").strip()
    key = os.environ.get("KAGGLE_KEY", "").strip()
    if not user or not key:
        print(SKIP_MESSAGE, flush=True)
        return "skipped"

    dataset_id = f"{user}/{slug}"
    meta = {
        "title": title,
        "id": dataset_id,
        "licenses": [{"name": "other"}],
        "description": (data_dir / "README.md").read_text(encoding="utf-8") if (data_dir / "README.md").exists() else "",
    }
    (data_dir / "dataset-metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    exists = runner(["kaggle", "datasets", "status", dataset_id], capture_output=True, text=True).returncode == 0
    if exists:
        cmd = ["kaggle", "datasets", "version", "-p", str(data_dir), "-m", message, "--dir-mode", "zip"]
        action = "versioned"
    else:
        # `create` without --public makes the dataset private.
        cmd = ["kaggle", "datasets", "create", "-p", str(data_dir), "--dir-mode", "zip"]
        action = "created"
    print(f"Kaggle upload: {action} private dataset {dataset_id}", flush=True)
    runner(cmd, check=True)
    return action
