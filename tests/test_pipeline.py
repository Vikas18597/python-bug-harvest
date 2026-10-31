import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from harvest import aggregate, kaggle_upload, verify

BUGGY = "def add(a, b):\n    return a - b\n\n\ndef neg(a):\n    return -a\n"
BUGGY_TEST = "from calc import add, neg\n\n\ndef test_neg():\n    assert neg(1) == -1\n"
NEW_TEST = BUGGY_TEST + "\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def toy_candidate(tmp_path: Path) -> dict:
    repo = tmp_path / "upstream"
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text(BUGGY)
    (repo / "tests" / "test_calc.py").write_text(BUGGY_TEST)
    (repo / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n'
        '[project]\nname = "calc"\nversion = "0"\n[tool.setuptools]\npy-modules = ["calc"]\n'
    )
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "base")
    patches = {}
    for name, rel, text in (("test", "tests/test_calc.py", NEW_TEST), ("gold", "calc.py", BUGGY.replace("a - b", "a + b")),
                            ("wrong", "calc.py", BUGGY.replace("a - b", "a * b"))):
        (repo / rel).write_text(text)
        patches[name] = _git(repo, "diff") + "\n"
        _git(repo, "checkout", "--", ".")
    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    return {
        "instance_id": "toy__calc-1", "repo": "toy/calc", "repo_url": repo.as_uri(), "base_commit": _git(repo, "rev-parse", "HEAD"),
        "created_at": "2024-01-01", "patch": patches["gold"], "test_patch": patches["test"], "_wrong": patches["wrong"], "problem_statement": "add() subtracts",
        "FAIL_TO_PASS": ["tests/test_calc.py::test_add"], "PASS_TO_PASS": ["tests/test_calc.py::test_neg"],
        "install_config": {"python": py, "packages": "pytest", "install": "pip install -e ."},
        "requirements": "", "source": "toy", "_py": py,
    }


needs_tools = pytest.mark.skipif(
    not (shutil.which("uv") and shutil.which("bash") and shutil.which("git")) or sys.platform == "win32",
    reason="end-to-end verification needs uv, bash and git on a POSIX host",
)


@needs_tools
def test_verify_end_to_end(toy_candidate, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # a relative output dir must still work (commands run inside the clone)
    result = verify.verify(toy_candidate, Path("out"), target_python=toy_candidate["_py"])
    assert result["verified"], result
    out = tmp_path / "out" / "toy__calc-1"
    task = json.loads((out / "task.json").read_text())
    assert task["FAIL_TO_PASS"] == ["tests/test_calc.py::test_add"] and task["python_version"].startswith(toy_candidate["_py"])
    listing = subprocess.run(["tar", "-tzf", str(out / task["snapshot"])], capture_output=True, text=True, check=True).stdout
    assert "toy__calc-1/calc.py" in listing


@needs_tools
def test_verify_rejects_wrong_gold_patch(toy_candidate, tmp_path):
    toy_candidate["patch"] = toy_candidate["_wrong"]
    result = verify.verify(toy_candidate, tmp_path / "out", target_python=toy_candidate["_py"])
    assert not result["verified"] and result["reason"] == "f2p_fails_with_fix"
    assert len(result["attempts"]) == 2  # target-Python tier, then pinned fallback tier
    assert not (tmp_path / "out" / "toy__calc-1" / "task.json").exists()


def _fake_result(root: Path, iid: str, verified: bool, reason=None, size=1000):
    d = root / iid
    d.mkdir(parents=True)
    res = {"instance_id": iid, "repo": "o/" + iid, "verified": verified, "reason": reason, "wall_seconds": 10.0,
           "attempts": [{"tier": "py3.13-recipe", "python": "3.13", "reason": reason}]}
    if verified:
        res.update(python_version="3.13.1", tier="py3.13-recipe", snapshot={"file": f"{iid}.tar.gz", "bytes": size})
        (d / f"{iid}.tar.gz").write_bytes(b"x" * size)
        (d / "task.json").write_text(json.dumps({"instance_id": iid}))
    (d / "result.json").write_text(json.dumps(res))


def test_aggregate_layout_and_summary(tmp_path):
    res = tmp_path / "results"
    _fake_result(res, "a-1", True, size=2000)
    _fake_result(res, "b-1", False, "env_install_failed")
    _fake_result(res, "c-1", False, "env_install_failed")
    summary = aggregate.aggregate(res, tmp_path / "ds", candidates_total=4)
    ds = tmp_path / "ds"
    assert [json.loads(line)["instance_id"] for line in (ds / "harvest.jsonl").read_text().splitlines()] == ["a-1"]
    assert (ds / "snapshots" / "a-1.tar.gz").stat().st_size == 2000
    assert len((ds / "rejections.jsonl").read_text().splitlines()) == 2
    assert summary["verified"] == 1 and summary["missing_results"] == 1
    assert summary["rejection_reasons"] == {"env_install_failed": 2}
    assert "CC-BY-4.0" in (ds / "README.md").read_text()


def test_aggregate_rejects_untrusted_result_paths(tmp_path):
    res = tmp_path / "results"
    _fake_result(res, "a-1", True)
    # a result.json planted deeper than results/<iid>/ is ignored
    deep = res / "a-1" / "x"
    deep.mkdir()
    (deep / "result.json").write_text(json.dumps({"instance_id": "a-1", "verified": True}))
    # a snapshot name that escapes the snapshots dir is refused, nothing is copied out of place
    _fake_result(res, "evil-1", True)
    (res / "evil-1" / "payload.py").write_text("stolen = True\n")
    bad = json.loads((res / "evil-1" / "result.json").read_text())
    bad["snapshot"]["file"] = "../../payload.py"
    (res / "evil-1" / "result.json").write_text(json.dumps(bad))
    _fake_result(res, "renamed-1", True)
    other = json.loads((res / "renamed-1" / "result.json").read_text())
    other["instance_id"] = "../renamed-1"
    (res / "renamed-1" / "result.json").write_text(json.dumps(other))

    summary = aggregate.aggregate(res, tmp_path / "ds")
    ds = tmp_path / "ds"
    assert [json.loads(line)["instance_id"] for line in (ds / "harvest.jsonl").read_text().splitlines()] == ["a-1"]
    assert sorted(p.name for p in (ds / "snapshots").iterdir()) == ["a-1.tar.gz"]
    assert not (tmp_path / "payload.py").exists()
    assert summary["results"] == 3 and summary["rejection_reasons"] == {"invalid_result": 2}
    rejected = {json.loads(line)["instance_id"] for line in (ds / "rejections.jsonl").read_text().splitlines()}
    assert rejected == {"evil-1", "renamed-1"}


@pytest.mark.parametrize("frozen_ok", [False, True])
def test_exported_recipe_lists_only_succeeded_commands(tmp_path, monkeypatch, frozen_ok):
    """The pinned tier wins; the row's recipe holds only what succeeded and the pins it read."""
    cand = {
        "instance_id": "o__p-1", "repo": "o/p", "repo_url": "https://example.invalid/o/p.git", "base_commit": "abc",
        "created_at": "2024-01-01", "patch": "gold\n", "test_patch": "test\n", "problem_statement": "x",
        "FAIL_TO_PASS": ["t.py::test_bug"], "PASS_TO_PASS": ["t.py::test_ok"], "source": "toy",
        "requirements": "good==1.0\nbad==2.0\np==0.1\n",
        "install_config": {"python": "3.12", "packages": "pytest", "pip_packages": ["missing-pkg"],
                           "pre_install": ["apt-get install -y libfoo libbar"], "install": "pip install -e ."},
    }
    frozen_cmd = 'uv pip install --no-deps -r "$HARVEST_FROZEN"'
    failing = {"uv pip install missing-pkg"} | (set() if frozen_ok else {frozen_cmd, "uv pip install --no-deps bad==2.0"})
    state = {"gold": False}

    def fake_sh(cmd, cwd, env, log, timeout):
        if cmd.startswith("git archive"):
            argv = shlex.split(cmd)
            Path(argv[argv.index("-o") + 1]).write_bytes(b"tar")
        if "test_patch.diff" in cmd:
            state["gold"] = False
        if "gold_patch.diff" in cmd:
            state["gold"] = True
        if cmd.startswith("python -c"):
            return 0, "3.13.1\n"
        if cmd.startswith("pytest"):
            fixed = state["gold"] and not env["VIRTUAL_ENV"].endswith("recipe")
            return 0, f"PASSED t.py::test_ok\n{'PASSED' if fixed else 'FAILED'} t.py::test_bug\n"
        return (1 if cmd in failing else 0), ""

    monkeypatch.setattr(verify, "sh", fake_sh)
    monkeypatch.setattr(verify, "_privileged", lambda cmd: None)
    result = verify.verify(cand, tmp_path / "out", target_python="3.13")
    assert result["verified"] and result["tier"] == "py3.13-pinned", result
    task = json.loads((tmp_path / "out" / "o__p-1" / "task.json").read_text())
    skipped_apt = {"cmd": "apt-get install -y libfoo libbar", "status": "skipped"}
    failed_pkg = {"cmd": "uv pip install missing-pkg", "status": "failed"}
    if frozen_ok:
        assert task["install_commands"] == ["uv pip install setuptools wheel pytest", frozen_cmd, "uv pip install -e ."]
        assert task["install_omitted"] == [skipped_apt, failed_pkg]
        assert task["frozen_requirements"] == "good==1.0\nbad==2.0\n"
    else:
        assert task["install_commands"] == [
            "uv pip install setuptools wheel pytest", "uv pip install --no-deps good==1.0", "uv pip install -e .",
        ]
        assert task["install_omitted"] == [
            skipped_apt, {"cmd": "uv pip install --no-deps bad==2.0", "status": "failed"}, failed_pkg,
        ]
        assert task["frozen_requirements"] is None

def test_kaggle_upload_is_noop_without_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("KAGGLE_USERNAME", raising=False)
    monkeypatch.setenv("KAGGLE_KEY", "")
    monkeypatch.delenv("KAGGLE_API_TOKEN", raising=False)

    def runner(*a, **k):
        raise AssertionError("kaggle CLI must not be invoked without credentials")

    assert kaggle_upload.upload(tmp_path, "slug", "t", "m", runner=runner) == "skipped"
    assert "Kaggle upload skipped" in capsys.readouterr().out
    assert not (tmp_path / "dataset-metadata.json").exists()


def test_kaggle_upload_creates_private_dataset(tmp_path, monkeypatch):
    monkeypatch.setenv("KAGGLE_USERNAME", "someone")
    monkeypatch.setenv("KAGGLE_KEY", "k")
    monkeypatch.delenv("KAGGLE_API_TOKEN", raising=False)
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1 if cmd[2] == "status" else 0)

    assert kaggle_upload.upload(tmp_path, "slug", "t", "m", runner=runner) == "created"
    assert calls[-1][:3] == ["kaggle", "datasets", "create"] and "--public" not in calls[-1]
    assert json.loads((tmp_path / "dataset-metadata.json").read_text())["id"] == "someone/slug"
