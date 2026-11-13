"""Upload the harvested dataset to a PRIVATE Kaggle dataset. No-op when credentials are absent.

Auth (https://github.com/Kaggle/kaggle-cli/blob/main/docs/README.md#authentication): either the single
`KAGGLE_API_TOKEN` (kaggle>=1.8.3) or the legacy `KAGGLE_USERNAME` + `KAGGLE_KEY` pair.

Kaggle accepts the files before it processes them, and a dataset whose processing fails never becomes visible:
`kaggle datasets status` keeps failing as if it did not exist. So after the CLI call the upload polls the status
until the dataset (or its new version) is ready, and fails otherwise.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from .aggregate import LEGACY_SNAPSHOT_SUFFIX

DEFAULT_READY_TIMEOUT = 30 * 60  # seconds to wait for Kaggle to process an upload
DEFAULT_POLL_INTERVAL = 30  # seconds between status checks

SKIP_MESSAGE = (
    "Kaggle upload skipped: neither KAGGLE_API_TOKEN nor KAGGLE_USERNAME + KAGGLE_KEY are set "
    "(add them as repository secrets to enable the upload)."
)
SKIP_NO_USERNAME = (
    "Kaggle upload skipped: KAGGLE_API_TOKEN is set but the dataset owner is unknown; "
    "set KAGGLE_USERNAME (could not resolve it from the token via `kaggle config view`)."
)


class KaggleUploadError(RuntimeError):
    """The upload did not end in a ready Kaggle dataset."""


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


def _parse_status(stdout: str) -> tuple[str | None, int | None]:
    """Parse `kaggle datasets status` output: JSON from `--format json`, else the bare status word."""
    text = stdout or ""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
        except ValueError:
            data = None
        if isinstance(data, dict):
            status = str(data.get("status") or "").strip().lower() or None
            version = data.get("current_version_number")
            return status, version if isinstance(version, int) and not isinstance(version, bool) else None
    lines = [line.strip().lower() for line in text.splitlines() if line.strip()]
    return (lines[-1], None) if lines and re.fullmatch(r"[a-z_]+", lines[-1]) else (None, None)


def dataset_state(dataset_id: str, runner=subprocess.run) -> tuple[str | None, int | None]:
    """Return (status, current version number) of `owner/slug`; status is None while the dataset is not visible."""
    base = ["kaggle", "datasets", "status", dataset_id]
    proc = runner([*base, "--format", "json"], capture_output=True, text=True)
    if proc.returncode == 2:  # argparse usage error: a CLI without --format
        proc = runner(base, capture_output=True, text=True)
    return _parse_status(proc.stdout) if proc.returncode == 0 else (None, None)


def wait_until_ready(dataset_id: str, *, after_version: int | None = None, runner=subprocess.run,
                     timeout: float = DEFAULT_READY_TIMEOUT, interval: float = DEFAULT_POLL_INTERVAL,
                     sleep=time.sleep, clock=time.monotonic) -> int | None:
    """Poll until `dataset_id` is ready and, after a new version, newer than `after_version`. Returns its version.

    Raises KaggleUploadError when Kaggle reports `error` or nothing ready shows up within `timeout` seconds.
    """
    deadline = clock() + timeout
    while True:
        status, version = dataset_state(dataset_id, runner)
        newer = after_version is None or (version is not None and version > after_version)
        if status == "ready" and newer:
            return version
        if status == "error":
            raise KaggleUploadError(f"Kaggle upload: Kaggle failed to process {dataset_id} (status: error).")
        if clock() >= deadline:
            seen = ("not visible (`kaggle datasets status` fails)" if status is None
                    else f"{status}, version {version}")
            what = f"a version newer than {after_version} of {dataset_id}" if after_version is not None else dataset_id
            raise KaggleUploadError(
                f"Kaggle upload: {what} was not ready after {timeout:.0f}s (last status: {seen}). Kaggle accepted "
                "the files but did not finish processing them; a dataset whose processing failed stays invisible. "
                "The same slug can be uploaded again."
            )
        sleep(interval)


def upload(data_dir: Path, slug: str, title: str, message: str, runner=subprocess.run, *,
           ready_timeout: float = DEFAULT_READY_TIMEOUT, poll_interval: float = DEFAULT_POLL_INTERVAL,
           sleep=time.sleep, clock=time.monotonic) -> str:
    """Create or version `<username>/<slug>` from `data_dir` and wait until Kaggle has processed it.

    Returns "skipped", "created" or "versioned"; raises KaggleUploadError if the dataset does not become ready.
    """
    owner, skip = resolve_owner(runner)
    if skip:
        print(skip, flush=True)
        return "skipped"

    packed = sorted(p.name for p in (data_dir / "snapshots").glob(f"*{LEGACY_SNAPSHOT_SUFFIX}"))
    if packed:
        raise KaggleUploadError(
            f"Kaggle upload: {len(packed)} snapshot(s) are named {LEGACY_SNAPSHOT_SUFFIX} (e.g. {packed[0]}); Kaggle "
            "would unpack them and can then fail without an error. Re-run `python -m harvest aggregate`, which "
            "names them .tgz."
        )

    dataset_id = f"{owner}/{slug}"
    meta = {
        "title": title,
        "id": dataset_id,
        "licenses": [{"name": "other"}],
        "description": (data_dir / "README.md").read_text(encoding="utf-8") if (data_dir / "README.md").exists() else "",
    }
    (data_dir / "dataset-metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    status, before = dataset_state(dataset_id, runner)
    exists = status is not None
    if exists:
        cmd = ["kaggle", "datasets", "version", "-p", str(data_dir), "-m", message, "--dir-mode", "zip"]
        action = "versioned"
        if before is None:
            print("Kaggle upload: the CLI reports no version numbers; only the dataset status is checked.", flush=True)
    else:
        # `create` without --public makes the dataset private.
        cmd = ["kaggle", "datasets", "create", "-p", str(data_dir), "--dir-mode", "zip"]
        action = "created"
    print(f"Kaggle upload: uploading private dataset {dataset_id} ...", flush=True)
    runner(cmd, check=True)
    # The CLI exits 0 once Kaggle has the files, even if processing them fails later; claim success only when ready.
    print(f"Kaggle upload: waiting up to {ready_timeout:.0f}s for Kaggle to process {dataset_id} ...", flush=True)
    version = wait_until_ready(dataset_id, after_version=before if exists else None, runner=runner,
                               timeout=ready_timeout, interval=poll_interval, sleep=sleep, clock=clock)
    shown = f"version {version}" if version is not None else "latest version"
    print(f"Kaggle upload: {action} private dataset {dataset_id} ({shown} ready)", flush=True)
    return action
