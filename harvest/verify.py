"""Rebuild one candidate into a verified task: clone, install, run tests before/after the gold patch."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import recipes, testlog

DEFAULT_TIMEOUTS = {"clone": 900, "step": 1200, "tests": 1200}
TASK_FIELDS = (
    "instance_id", "repo", "base_commit", "environment_setup_commit", "created_at", "version",
    "problem_statement", "hints_text", "patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS",
    "source", "source_dataset", "source_revision", "source_licence", "repo_license", "synthetic",
)


class Log:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(path, "a", encoding="utf-8", errors="replace")

    def write(self, text: str) -> None:
        self.fh.write(text if text.endswith("\n") else text + "\n")
        self.fh.flush()

    def close(self) -> None:
        self.fh.close()


def _bash() -> str | None:
    return shutil.which("bash")


def sh(cmd: str, cwd: Path, env: dict, log: Log, timeout: int) -> tuple[int, str]:
    """Run a shell command, tee its output into the log, kill the whole process group on timeout."""
    log.write(f"$ {cmd}")
    start = time.monotonic()
    kwargs: dict = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    argv = [_bash(), "-c", cmd] if _bash() else cmd
    proc = subprocess.Popen(
        argv, cwd=cwd, env=env, shell=not _bash(), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", **kwargs,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        out, _ = proc.communicate()
        rc = -9
        out = (out or "") + f"\n[harvest] TIMEOUT after {timeout}s\n"
    log.write(out or "")
    log.write(f"[harvest] exit={rc} in {time.monotonic() - start:.1f}s")
    return rc, out or ""


def _venv_bin(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin")


def _privileged(cmd: str) -> str | None:
    if not sys.platform.startswith("linux"):
        return None
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return f"DEBIAN_FRONTEND=noninteractive bash -c {shlex.quote(cmd)}"
    if shutil.which("sudo") and subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0:
        return f"sudo -E env DEBIAN_FRONTEND=noninteractive bash -c {shlex.quote(cmd)}"
    return None


def _apply(patch: str, repo: Path, env: dict, log: Log, name: str, work: Path) -> bool:
    pf = work / f"{name}.diff"
    pf.write_text(patch, encoding="utf-8")
    q = shlex.quote(str(pf))
    for cmd in (
        f"git apply -v --whitespace=nowarn {q}",
        f"patch --batch --fuzz=5 -p1 --dry-run -i {q} && patch --batch --fuzz=5 -p1 -i {q}",
    ):
        rc, _ = sh(cmd, repo, env, log, 300)
        if rc == 0:
            return True
    return False


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def clone(cand: dict, repo: Path, env: dict, log: Log, timeout: int) -> bool:
    repo.mkdir(parents=True, exist_ok=True)
    url = cand.get("repo_url") or f"https://github.com/{cand['repo']}.git"
    for cmd in (
        f"git init -q && git remote add origin {shlex.quote(url)}",
        f"git -c protocol.version=2 fetch -q --depth 1 origin {cand['base_commit']}",
        "git -c advice.detachedHead=false checkout -q FETCH_HEAD",
    ):
        rc, _ = sh(cmd, repo, env, log, timeout)
        if rc != 0:
            return False
    sh("git submodule update --init --recursive --depth 1", repo, env, log, timeout)
    return True


def _reset(cand: dict, repo: Path, env: dict, log: Log) -> None:
    sh(f"git reset -q --hard {cand['base_commit']} && git clean -qffdx", repo, env, log, 300)


def _retry_pieces(step: recipes.Step, repo: Path, env: dict) -> list[str]:
    if step.retry_each:
        return list(step.retry_each)
    if step.retry_lines_of:
        path = Path(step.retry_lines_of.replace("$HARVEST_FROZEN", env.get("HARVEST_FROZEN", "")))
        path = path if path.is_absolute() else repo / path
        if path.is_file():
            lines = recipes.requirement_lines(path.read_text(encoding="utf-8", errors="replace"))
            return [f"{step.retry_line_prefix} {line if line.startswith('-') else shlex.quote(line)}" for line in lines]
    return []


def run_tier(cand: dict, tier: recipes.Tier, repo: Path, work: Path, base_env: dict, log: Log, timeouts: dict) -> dict:
    attempt: dict = {"tier": tier.name, "python": tier.python, "reason": None, "env": tier.env}
    venv = work / f"venv-{tier.name}"
    shutil.rmtree(venv, ignore_errors=True)
    frozen = work / "frozen-requirements.txt"
    frozen.write_text(recipes.frozen_requirements(cand.get("requirements"), cand["repo"]), encoding="utf-8")
    env = dict(base_env)
    env.update(tier.env)
    env.update({
        "VIRTUAL_ENV": str(venv),
        "PATH": str(_venv_bin(venv)) + os.pathsep + base_env.get("PATH", ""),
        "HARVEST_FROZEN": str(frozen),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "UV_HTTP_TIMEOUT": "180",
    })
    env.pop("PYTHONPATH", None)
    _reset(cand, repo, env, log)

    t0 = time.monotonic()
    log.write(f"\n===== tier {tier.name} (python {tier.python}) =====")
    rc, _ = sh(f"uv venv -q --seed --python {shlex.quote(tier.python)} {shlex.quote(str(venv))}", repo, env, log, timeouts["step"])
    if rc != 0:
        attempt["reason"] = "python_unavailable"
        return attempt
    rc, out = sh("python -c 'import sys; print(sys.version.split()[0])'", repo, env, log, 60)
    attempt["python_full"] = out.strip().splitlines()[-1] if rc == 0 and out.strip() else tier.python
    steps_log = []
    for step in tier.steps:
        cmd = step.cmd
        if step.privileged:
            wrapped = _privileged(cmd)
            if wrapped is None:
                log.write(f"[harvest] skipping privileged step (no root/sudo on this host): {cmd}")
                steps_log.append({"cmd": cmd, "rc": None, "soft": step.soft})
                continue
            cmd = wrapped
        step_env = {**env, **step.env}
        rc, _ = sh(cmd, repo, step_env, log, timeouts["step"])
        entry = {"cmd": step.cmd, "rc": rc, "soft": step.soft}
        if step.env:
            entry["env"] = step.env
        if rc != 0:
            retried = _retry_pieces(step, repo, step_env)
            if retried:
                log.write(f"[harvest] step failed as a whole; retrying {len(retried)} piece(s) one by one")
                results = []
                for piece in retried:
                    piece_cmd = _privileged(piece) if step.privileged else piece
                    results.append(sh(piece_cmd, repo, step_env, log, timeouts["step"])[0] if piece_cmd else 1)
                entry["retried"] = {"ok": sum(r == 0 for r in results), "total": len(results)}
        steps_log.append(entry)
        if rc != 0 and not step.soft:
            attempt.update(reason="env_install_failed", install_steps=steps_log, install_seconds=round(time.monotonic() - t0, 1))
            return attempt
    attempt["install_steps"] = steps_log
    attempt["install_seconds"] = round(time.monotonic() - t0, 1)
    sh("uv pip freeze", repo, env, log, 300)

    if not _apply(cand["test_patch"], repo, env, log, "test_patch", work):
        attempt["reason"] = "test_patch_apply_failed"
        return attempt
    files = recipes.test_files(cand, repo)
    test_cmd = tier.test_cmd + "".join(" " + shlex.quote(f) for f in files)
    attempt["test_cmd"] = test_cmd

    t1 = time.monotonic()
    log.write("----- tests: test patch only (expect FAIL_TO_PASS to fail) -----")
    rc_b, out_b = sh(test_cmd, repo, env, log, timeouts["tests"])
    (work / f"{tier.name}.before.log").write_text(out_b, encoding="utf-8")
    if rc_b == -9:
        attempt.update(reason="tests_timeout", test_seconds=round(time.monotonic() - t1, 1))
        return attempt
    if not _apply(cand["patch"], repo, env, log, "gold_patch", work):
        attempt["reason"] = "gold_patch_apply_failed"
        return attempt
    log.write("----- tests: test patch + gold patch (expect all to pass) -----")
    rc_a, out_a = sh(test_cmd, repo, env, log, timeouts["tests"])
    (work / f"{tier.name}.after.log").write_text(out_a, encoding="utf-8")
    attempt["test_seconds"] = round(time.monotonic() - t1, 1)
    if rc_a == -9:
        attempt["reason"] = "tests_timeout"
        return attempt

    verdict = testlog.judge(
        cand["FAIL_TO_PASS"], cand["PASS_TO_PASS"],
        testlog.parse_pytest_log(out_b), testlog.parse_pytest_log(out_a),
    )
    attempt["reason"] = verdict.pop("reason")
    attempt["verdict"] = verdict
    return attempt


def verify(cand: dict, out_root: Path, *, target_python: str = "3.13", timeouts: dict | None = None,
           keep_work: bool = False) -> dict:
    timeouts = {**DEFAULT_TIMEOUTS, **(timeouts or {})}
    iid = cand["instance_id"]
    out = Path(out_root).resolve() / iid
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    log = Log(out / "harvest.log")
    work = Path(tempfile.mkdtemp(prefix=f"harvest-{iid[:40]}-", dir=os.environ.get("HARVEST_WORK")))
    repo = work / "repo"
    base_env = dict(os.environ)
    base_env["GIT_TERMINAL_PROMPT"] = "0"
    start = time.monotonic()
    result: dict = {
        "instance_id": iid, "repo": cand["repo"], "source": cand["source"], "created_at": cand["created_at"],
        "verified": False, "reason": None, "attempts": [], "target_python": target_python,
        "runner": {"platform": sys.platform, "github_run_id": os.environ.get("GITHUB_RUN_ID")},
    }
    try:
        log.write(f"[harvest] {iid} @ {cand['repo']}#{cand['base_commit']}")
        if not clone(cand, repo, base_env, log, timeouts["clone"]):
            result["reason"] = "clone_failed"
        else:
            for tier in recipes.tiers_for(cand, repo, target_python):
                attempt = run_tier(cand, tier, repo, work, base_env, log, timeouts)
                result["attempts"].append(attempt)
                for name in (f"{tier.name}.before.log", f"{tier.name}.after.log"):
                    if (work / name).exists():
                        shutil.copy(work / name, out / name)
                if attempt["reason"] is None:
                    result.update(verified=True, tier=tier.name, python_version=attempt.get("python_full"),
                                  test_cmd=attempt["test_cmd"])
                    break
                if attempt["reason"] in ("test_patch_apply_failed", "gold_patch_apply_failed"):
                    break
            if not result["verified"]:
                result["reason"] = result["attempts"][-1]["reason"] if result["attempts"] else "no_tiers"
            else:
                snap = out / f"{iid}.tar.gz"
                rc, _ = sh(
                    f"git archive --format=tar.gz --prefix={shlex.quote(iid)}/ -o {shlex.quote(str(snap))} {cand['base_commit']}",
                    repo, base_env, log, 600,
                )
                if rc != 0:
                    result.update(verified=False, reason="snapshot_failed")
                else:
                    result["snapshot"] = {"file": snap.name, "bytes": snap.stat().st_size, "sha256": _sha256(snap)}
    except Exception as err:  # keep the batch going; record as a harness error
        log.write(f"[harvest] harness error: {err!r}")
        result.update(verified=False, reason="harness_error", error=repr(err))
    finally:
        result["wall_seconds"] = round(time.monotonic() - start, 1)
        log.close()
        if not keep_work:
            shutil.rmtree(work, ignore_errors=True)

    (out / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    if result["verified"]:
        attempt = result["attempts"][-1]
        row = {k: cand.get(k) for k in TASK_FIELDS}
        row.update(
            python_version=result["python_version"],
            install_tier=result["tier"],
            install_commands=[s["cmd"] for s in attempt.get("install_steps", [])],
            install_env={**attempt.get("env", {}), **{k: v for s in attempt.get("install_steps", []) for k, v in s.get("env", {}).items()}},
            test_cmd=result["test_cmd"],
            snapshot=result["snapshot"]["file"],
            snapshot_sha256=result["snapshot"]["sha256"],
        )
        (out / "task.json").write_text(json.dumps(row), encoding="utf-8")
    status = "VERIFIED" if result["verified"] else f"REJECTED ({result['reason']})"
    print(f"[harvest] {iid}: {status} in {result['wall_seconds']}s", flush=True)
    return result
