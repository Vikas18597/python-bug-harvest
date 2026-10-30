from pathlib import Path

from harvest import recipes, select, sources


def _cand(**kw):
    base = {
        "instance_id": "o__p-1", "repo": "o/p", "created_at": "2024-01-01", "FAIL_TO_PASS": ["t.py::a"],
        "PASS_TO_PASS": [], "test_patch": "diff --git a/t.py b/t.py\n", "install_config": {"python": "3.9"},
    }
    base.update(kw)
    return base


def test_select_filters_and_diversifies():
    rows = [
        _cand(instance_id="a__x-1", repo="a/x", install_config={"python": "3.12"}),
        _cand(instance_id="a__x-2", repo="a/x", install_config={"python": "3.12"}, created_at="2025-01-01"),
        _cand(instance_id="old-1", repo="b/old", created_at="2022-12-31"),
        _cand(instance_id="fa-1", repo="tiangolo/fastapi"),
        _cand(instance_id="nof2p-1", repo="c/nof2p", FAIL_TO_PASS=[]),
        _cand(instance_id="d__y-1", repo="d/y", install_config={"python": "3.9"}),
        _cand(instance_id="e__z-1", repo="e/z", install_config={"python": "3.11"}),
    ]
    chosen = select.select(rows, limit=10, created_from="2023", exclude_projects={"fastapi"})
    # one per project, newest first within a project, closest Python version to the target first
    assert [c["instance_id"] for c in chosen] == ["a__x-2", "e__z-1", "d__y-1"]
    assert len(select.select(rows, limit=1, created_from="2023", exclude_projects=set())) == 1


def test_candidate_file_rejects_revision_drift(tmp_path):
    path = tmp_path / "c.json"
    select.write_candidate_file(path, source="swe-rebench", split="filtered", criteria={}, chosen=[_cand()])
    assert select.read_candidate_file(path)["instance_ids"] == ["o__p-1"]
    path.write_text(path.read_text().replace(sources.SOURCES["swe-rebench"].revision, "deadbeef"))
    try:
        select.read_candidate_file(path)
    except ValueError as err:
        assert "deadbeef" in str(err)
    else:
        raise AssertionError("revision drift not detected")


def test_normalise_parses_json_string_lists():
    row = {"instance_id": "i", "repo": "o/p", "base_commit": "c", "patch": "", "test_patch": "",
           "FAIL_TO_PASS": '["a", "b"]', "PASS_TO_PASS": ["c"]}
    out = sources.normalise(sources.SOURCES["swe-bench"], row)
    assert out["FAIL_TO_PASS"] == ["a", "b"] and out["PASS_TO_PASS"] == ["c"]
    assert out["source_licence"] == "MIT" and out["synthetic"] is False


def test_uvify_and_frozen_requirements():
    assert recipes.uvify("pip install -e .[dev]") == "uv pip install -e .[dev]"
    assert recipes.uvify("cd x && python -m pip install foo") == "cd x && uv pip install foo"
    frozen = recipes.frozen_requirements(
        "foo==1.0\n-e git+https://github.com/o/my_proj.git@abc#egg=my_proj\nbar @ file:///croot/bar_1/work\n"
        "numpy @ file:///croot/np/work/dist/numpy-2.2.4-cp311-cp311-linux_x86_64.whl#sha256=ab\nmy-proj==0.1\n# c\nbaz==2\n",
        "o/my_proj",
    )
    # conda `@ file://` lines keep their pin when the wheel name reveals it, else become bare names
    assert frozen == "foo==1.0\nbar\nnumpy==2.2.4\nbaz==2\n"


def test_rebench_tiers(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("x\n")
    cfg = {"python": "3.9", "packages": "requirements.txt", "reqs_path": ["requirements.txt", "missing.txt"],
           "pip_packages": ["pytest==7.0"], "pre_install": ["apt-get update"], "install": "pip install -e .[dev]",
           "test_cmd": "pytest -rA", "env_vars": {"A": "1", "B": None}}
    latest, pinned_target, pinned = recipes.tiers_for(_cand(install_config=cfg, requirements="foo==1\n"), tmp_path, "3.13")
    assert (latest.python, pinned_target.python, pinned.python) == ("3.13", "3.13", "3.9")
    assert pinned_target.steps == pinned.steps
    cmds = [s.cmd for s in latest.steps]
    assert "uv pip install -r requirements.txt" in cmds and not any("missing.txt" in c for c in cmds)
    assert "uv pip install pytest" in cmds  # pins dropped on the target-Python tier
    assert cmds[-1] == "uv pip install -e .[dev]" and not latest.steps[-1].soft
    assert latest.steps[0].privileged and latest.env == {"A": "1"} and latest.test_cmd == "pytest -rA"
    pinned_cmds = [s.cmd for s in pinned.steps]
    assert any("--no-deps" in c for c in pinned_cmds) and "uv pip install pytest==7.0" in pinned_cmds


def test_rebench_tier_edge_cases(tmp_path: Path):
    cfg = {"python": "3.9", "packages": "numpy=1.2 scipy", "pip_packages": ["pytest pytest-cov==4"],
           "pre_install": ["apt-get install -y gcc libfoo-dev"]}
    latest, _, pinned = recipes.tiers_for(_cand(install_config=cfg, version="7.0"), tmp_path, "3.13")
    cmds = [s.cmd for s in latest.steps]
    # multi-package pip_packages entries are split; pins dropped on the target tier
    assert "uv pip install pytest pytest-cov" in cmds
    assert "uv pip install numpy scipy" in cmds
    # conda-style pins become pip pins on the pinned tier
    assert "uv pip install numpy==1.2 scipy" in [s.cmd for s in pinned.steps]
    # apt failures can be retried package by package
    assert latest.steps[0].retry_each == ["apt-get install -y gcc", "apt-get install -y libfoo-dev"]
    # shallow clones have no tags: the project install pretends the dataset version for setuptools-scm
    assert latest.steps[-1].env == {"SETUPTOOLS_SCM_PRETEND_VERSION": "7.0.0"}
    assert recipes.tiers_for(_cand(install_config=cfg, version="unknown"), tmp_path, "3.13")[0].steps[-1].env == {}


def test_auto_tiers_for_sources_without_specs(tmp_path: Path):
    (tmp_path / "requirements-dev.txt").write_text("x\n")
    tiers = recipes.tiers_for(_cand(install_config=None), tmp_path, "3.13")
    assert [t.python for t in tiers] == ["3.13", recipes.FALLBACK_PYTHON]
    assert "uv pip install -r requirements-dev.txt" in [s.cmd for s in tiers[0].steps]


def test_test_files(tmp_path: Path):
    for f in ("tests/test_a.py", "tests/test_b.py"):
        (tmp_path / f).parent.mkdir(exist_ok=True)
        (tmp_path / f).write_text("")
    cand = _cand(
        test_patch="diff --git a/tests/test_a.py b/tests/test_a.py\ndiff --git a/tests/data.json b/tests/data.json\n"
                   "diff --git a/tests/gone.py b/tests/gone.py\n",
        FAIL_TO_PASS=["tests/test_a.py::t"], PASS_TO_PASS=["tests/test_b.py::C::t[x y]"],
    )
    assert recipes.test_files(cand, tmp_path) == ["tests/test_a.py", "tests/test_b.py"]
