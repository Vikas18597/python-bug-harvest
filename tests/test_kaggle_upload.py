import json
import subprocess

import pytest

from harvest import kaggle_upload

FAKE_TOKEN = "KGAT_fake_test_value"  # not a real credential


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for name in ("KAGGLE_API_TOKEN", "KAGGLE_USERNAME", "KAGGLE_KEY"):
        monkeypatch.delenv(name, raising=False)


class FakeRunner:
    def __init__(self, config_stdout="", config_rc=0):
        self.calls = []
        self.config_stdout = config_stdout
        self.config_rc = config_rc

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[:3] == ["kaggle", "config", "view"]:
            return subprocess.CompletedProcess(cmd, self.config_rc, self.config_stdout, "")
        if cmd[:3] == ["kaggle", "datasets", "status"]:
            return subprocess.CompletedProcess(cmd, 1, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


def _upload(tmp_path, runner):
    return kaggle_upload.upload(tmp_path, "slug", "Title", "msg", runner=runner)


def test_no_credentials_skips_without_calling_cli(tmp_path, capsys):
    runner = FakeRunner()
    assert _upload(tmp_path, runner) == "skipped"
    assert runner.calls == []
    assert "KAGGLE_API_TOKEN" in capsys.readouterr().out


def test_username_without_key_or_token_skips(tmp_path, monkeypatch):
    monkeypatch.setenv("KAGGLE_USERNAME", "alice")
    runner = FakeRunner()
    assert _upload(tmp_path, runner) == "skipped"
    assert runner.calls == []


def test_legacy_pair_creates_private_dataset(tmp_path, monkeypatch):
    monkeypatch.setenv("KAGGLE_USERNAME", "alice")
    monkeypatch.setenv("KAGGLE_KEY", "fake-key")
    runner = FakeRunner()
    assert _upload(tmp_path, runner) == "created"
    assert json.loads((tmp_path / "dataset-metadata.json").read_text())["id"] == "alice/slug"
    assert "--public" not in runner.calls[-1]


def test_token_with_username_uses_username(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KAGGLE_API_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("KAGGLE_USERNAME", "alice")
    runner = FakeRunner()
    assert _upload(tmp_path, runner) == "created"
    assert ["kaggle", "config", "view"] not in runner.calls
    assert json.loads((tmp_path / "dataset-metadata.json").read_text())["id"] == "alice/slug"
    assert FAKE_TOKEN not in capsys.readouterr().out


def test_token_only_resolves_username_from_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KAGGLE_API_TOKEN", FAKE_TOKEN)
    runner = FakeRunner(config_stdout="Configuration values from env\n- username: bob\n- path: None\n")
    assert _upload(tmp_path, runner) == "created"
    assert json.loads((tmp_path / "dataset-metadata.json").read_text())["id"] == "bob/slug"
    assert FAKE_TOKEN not in capsys.readouterr().out


@pytest.mark.parametrize(
    "stdout,rc", [("- username: None\n", 0), ("", 1), ("- path: None\n", 0)]
)
def test_token_only_unresolved_username_skips(tmp_path, monkeypatch, capsys, stdout, rc):
    monkeypatch.setenv("KAGGLE_API_TOKEN", FAKE_TOKEN)
    runner = FakeRunner(config_stdout=stdout, config_rc=rc)
    assert _upload(tmp_path, runner) == "skipped"
    out = capsys.readouterr().out
    assert "KAGGLE_USERNAME" in out and FAKE_TOKEN not in out
    assert not (tmp_path / "dataset-metadata.json").exists()


def test_token_only_cli_missing_skips(tmp_path, monkeypatch):
    monkeypatch.setenv("KAGGLE_API_TOKEN", FAKE_TOKEN)

    def runner(cmd, **kwargs):
        raise FileNotFoundError("kaggle")

    assert _upload(tmp_path, runner) == "skipped"


def test_success_logged_only_after_cli_call_succeeds(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KAGGLE_USERNAME", "alice")
    monkeypatch.setenv("KAGGLE_KEY", "fake-key")
    assert _upload(tmp_path, FakeRunner()) == "created"
    assert "created private dataset alice/slug" in capsys.readouterr().out

    def failing_runner(cmd, check=False, **kwargs):
        if cmd[:3] == ["kaggle", "datasets", "status"]:
            return subprocess.CompletedProcess(cmd, 1, "", "")
        if check:
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 1, "", "")

    with pytest.raises(subprocess.CalledProcessError):
        _upload(tmp_path, failing_runner)
    assert "created private dataset" not in capsys.readouterr().out
