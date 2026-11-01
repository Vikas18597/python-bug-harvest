"""Upload the harvested dataset to a PRIVATE Kaggle dataset. No-op when credentials are absent.

Auth (https://github.com/Kaggle/kaggle-cli/blob/main/docs/README.md#authentication): either the single
`KAGGLE_API_TOKEN` (kaggle>=1.8.3) or the legacy `KAGGLE_USERNAME` + `KAGGLE_KEY` pair.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

SKIP_MESSAGE = (
    "Kaggle upload skipped: neither KAGGLE_API_TOKEN nor KAGGLE_USERNAME + KAGGLE_KEY are set "
    "(add them as repository secrets to enable the upload)."
)
SKIP_NO_USERNAME = (
    "Kaggle upload skipped: KAGGLE_API_TOKEN is set but the dataset owner is unknown; "
    "set KAGGLE_USERNAME (could not resolve it from the token via `kaggle config view`)."
)


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _username_from_cli(runner) -> str:
    """Ask the CLI (which introspects the token) for the username. Output is parsed, never printed."""
    try:
        proc = runner(["kaggle", "config", "view"], capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    m = re.search(r"^\W*username:\s*(\S+)\s*$", proc.stdout or "", re.MULTILINE)
    return m.group(1) if m and m.group(1).lower() != "none" else ""


def resolve_owner(runner=subprocess.run) -> tuple[str | None, str | None]:
    """Return (owner, None) when upload can proceed, else (None, skip_message)."""
    user, key, token = _env("KAGGLE_USERNAME"), _env("KAGGLE_KEY"), _env("KAGGLE_API_TOKEN")
    if not token and not (user and key):
        return None, SKIP_MESSAGE
    if user:
        return user, None
    owner = _username_from_cli(runner)
    return (owner, None) if owner else (None, SKIP_NO_USERNAME)


def upload(data_dir: Path, slug: str, title: str, message: str, runner=subprocess.run) -> str:
    """Create or version `<username>/<slug>` from `data_dir`. Returns "skipped", "created" or "versioned"."""
    owner, skip = resolve_owner(runner)
    if skip:
        print(skip, flush=True)
        return "skipped"

    dataset_id = f"{owner}/{slug}"
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
    print(f"Kaggle upload: uploading private dataset {dataset_id} ...", flush=True)
    runner(cmd, check=True)
    # Claim success only now: check=True raises (and fails the job) if the CLI call failed.
    print(f"Kaggle upload: {action} private dataset {dataset_id}", flush=True)
    return action
