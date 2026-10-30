"""Build environment-install recipes ("tiers") for a candidate.

A tier is an ordered list of shell steps run inside a fresh uv virtualenv at the repo root.
Steps marked ``soft`` may fail without aborting the tier; the test run is the real judge.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TEST_CMD = "pytest --no-header -rA --tb=line --color=no -p no:cacheprovider -W ignore::DeprecationWarning"
FALLBACK_PYTHON = "3.11"
_SPECIAL_PACKAGES = {"", "requirements.txt", "environment.yml", "pip", "pytest"}


@dataclass
class Step:
    cmd: str
    soft: bool = False
    privileged: bool = False


@dataclass
class Tier:
    name: str
    python: str
    steps: list[Step] = field(default_factory=list)
    test_cmd: str = DEFAULT_TEST_CMD
    env: dict[str, str] = field(default_factory=dict)


_PIP = re.compile(r"(^|&&\s*|;\s*|\|\|\s*)(?:python3? -m )?pip3? install\b")


def uvify(cmd: str) -> str:
    """Route `pip install` through `uv pip install`, which targets the active VIRTUAL_ENV."""
    return _PIP.sub(lambda m: f"{m.group(1)}uv pip install", cmd)


def _strip_pin(spec: str) -> str:
    return re.split(r"[<>=!~;\[ ]", spec.strip(), maxsplit=1)[0]


def frozen_requirements(text: str | None, repo: str) -> str:
    """Drop editable/local lines and the project itself from a `pip freeze` dump."""
    project = repo.split("/")[-1].lower().replace("_", "-")
    keep = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith(("#", "-e ", "--editable")) or " @ file://" in s or "git+" in s:
            continue
        if _strip_pin(s).lower().replace("_", "-") == project:
            continue
        keep.append(s)
    return "\n".join(keep) + ("\n" if keep else "")


def _env_yml_deps(path: Path) -> list[str]:
    """Best-effort extraction of package names from a conda environment.yml (no YAML dependency)."""
    names: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^\s*-\s+([A-Za-z0-9_.\-\[\]]+)\s*([<>=!~].*)?$", line)
        if not m:
            continue
        name = m.group(1)
        if name.lower() in {"python", "pip", "conda", "defaults", "conda-forge"}:
            continue
        names.append(name)
    return names


def _common_head(cfg: dict) -> list[Step]:
    steps = [Step(p, soft=True, privileged=True) for p in (cfg.get("pre_install") or [])]
    steps.append(Step("uv pip install setuptools wheel pytest"))
    return steps


def rebench_tiers(cand: dict, repo_dir: Path, target_python: str) -> list[Tier]:
    """Two tiers for rows that ship an install_config (SWE-rebench)."""
    cfg = cand.get("install_config") or {}
    test_cmd = cfg.get("test_cmd") or DEFAULT_TEST_CMD
    env = {k: str(v) for k, v in (cfg.get("env_vars") or {}).items() if v is not None}
    install = uvify(cfg.get("install") or "pip install -e .")
    packages = (cfg.get("packages") or "").strip()
    pip_packages = [p for p in (cfg.get("pip_packages") or []) if p]

    # Tier 1: the target Python with the project's own (unpinned) dependency declarations.
    latest = _common_head(cfg)
    if packages == "requirements.txt":
        for rel in cfg.get("reqs_path") or []:
            if (repo_dir / rel).is_file():
                latest.append(Step(f"uv pip install -r {shlex.quote(rel)}", soft=True))
    elif packages == "environment.yml":
        for rel in cfg.get("env_yml_path") or ["environment.yml"]:
            if (repo_dir / rel).is_file():
                deps = _env_yml_deps(repo_dir / rel)
                if deps:
                    latest.append(Step("uv pip install " + " ".join(shlex.quote(d) for d in deps), soft=True))
    elif packages not in _SPECIAL_PACKAGES:
        latest.append(Step("uv pip install " + " ".join(shlex.quote(_strip_pin(p)) for p in packages.split()), soft=True))
    if pip_packages:
        latest.append(Step("uv pip install " + " ".join(shlex.quote(_strip_pin(p)) for p in pip_packages), soft=True))
    latest.append(Step(install))

    # Tier 2: the dataset's own Python version and pinned `pip freeze`.
    pinned = _common_head(cfg)
    if frozen_requirements(cand.get("requirements"), cand["repo"]).strip():
        pinned.append(Step('uv pip install --no-deps -r "$HARVEST_FROZEN"', soft=True))
    if packages not in _SPECIAL_PACKAGES:
        pinned.append(Step("uv pip install " + " ".join(shlex.quote(p) for p in packages.split()), soft=True))
    if pip_packages:
        pinned.append(Step("uv pip install " + " ".join(shlex.quote(p) for p in pip_packages), soft=True))
    pinned.append(Step(install))

    dataset_python = str(cfg.get("python") or FALLBACK_PYTHON)
    return [
        Tier(f"py{target_python}-recipe", target_python, latest, test_cmd, env),
        Tier(f"py{dataset_python}-pinned", dataset_python, pinned, test_cmd, env),
    ]


def auto_tiers(repo_dir: Path, target_python: str) -> list[Tier]:
    """Heuristic tiers for sources without install specs (SWE-bench, SWE-Gym)."""

    def steps() -> list[Step]:
        out = [Step("uv pip install setuptools wheel pytest")]
        patterns = ["requirements*.txt", "*requirements*.txt", "requirements/*.txt"]
        seen: set[str] = set()
        for pattern in patterns:
            for p in sorted(repo_dir.glob(pattern)):
                rel = p.relative_to(repo_dir).as_posix()
                if rel not in seen:
                    seen.add(rel)
                    out.append(Step(f"uv pip install -r {shlex.quote(rel)}", soft=True))
        out.append(Step('uv pip install -e ".[test,tests,testing,dev]" || uv pip install -e .'))
        return out

    return [
        Tier(f"py{target_python}-auto", target_python, steps()),
        Tier(f"py{FALLBACK_PYTHON}-auto", FALLBACK_PYTHON, steps()),
    ]


def tiers_for(cand: dict, repo_dir: Path, target_python: str) -> list[Tier]:
    if cand.get("install_config"):
        return rebench_tiers(cand, repo_dir, target_python)
    return auto_tiers(repo_dir, target_python)


def test_files(cand: dict, repo_dir: Path) -> list[str]:
    """Test files to run: Python files touched by the test patch plus files named in the test lists."""
    files: list[str] = []
    for path in re.findall(r"^diff --git a/.* b/(.*)$", cand["test_patch"], flags=re.M):
        if path.endswith(".py"):
            files.append(path)
    for test_id in cand["FAIL_TO_PASS"] + cand["PASS_TO_PASS"]:
        head = test_id.split("::", 1)[0].split(" ", 1)[0]
        if head.endswith(".py"):
            files.append(head)
    out: list[str] = []
    for f in files:
        if f not in out and (repo_dir / f).is_file():
            out.append(f)
    return out
