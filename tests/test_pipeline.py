import json
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


def test_kaggle_upload_is_noop_without_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("KAGGLE_USERNAME", raising=False)
    monkeypatch.setenv("KAGGLE_KEY", "")

    def runner(*a, **k):
        raise AssertionError("kaggle CLI must not be invoked without credentials")

    assert kaggle_upload.upload(tmp_path, "slug", "t", "m", runner=runner) == "skipped"
    assert "Kaggle upload skipped" in capsys.readouterr().out
    assert not (tmp_path / "dataset-metadata.json").exists()


def test_kaggle_upload_creates_private_dataset(tmp_path, monkeypatch):
    monkeypatch.setenv("KAGGLE_USERNAME", "someone")
    monkeypatch.setenv("KAGGLE_KEY", "k")
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1 if cmd[2] == "status" else 0)

    assert kaggle_upload.upload(tmp_path, "slug", "t", "m", runner=runner) == "created"
    assert calls[-1][:3] == ["kaggle", "datasets", "create"] and "--public" not in calls[-1]
    assert json.loads((tmp_path / "dataset-metadata.json").read_text())["id"] == "someone/slug"
