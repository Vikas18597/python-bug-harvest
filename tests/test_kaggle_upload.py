import json
import subprocess

import pytest

from harvest import cli, kaggle_upload

FAKE_TOKEN = "KGAT_fake_test_value"  # not a real credential


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for name in ("KAGGLE_API_TOKEN", "KAGGLE_USERNAME", "KAGGLE_KEY"):
        monkeypatch.delenv(name, raising=False)


class FakeRunner:
    """Fake `kaggle` CLI. `existing` is the (status, version) before the upload, None if there is no dataset.

    `states` scripts what `datasets status` reports after the upload, one entry per call (the last one repeats):
    None means not visible (the CLI exits 1, as on Kaggle's 403), else (status, version). By default the upload is
    ready at once, as version 1 or as the next version.
    """

    def __init__(self, config_stdout="", config_rc=0, existing=None, states=None, json_format=True):
        self.calls = []
        self.config_stdout = config_stdout
        self.config_rc = config_rc
        self.state = existing
        self.states = list(states) if states is not None else None
        self.json_format = json_format
        self.uploaded = False

    def _status(self, cmd):
        if "--format" in cmd and not self.json_format:
            return subprocess.CompletedProcess(cmd, 2, "", "unrecognized arguments: --format")
        state = self.state
        if self.uploaded:
            if self.states is None:
                state = ("ready", self.state[1] + 1 if self.state else 1)
            else:
                state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if state is None:
            return subprocess.CompletedProcess(cmd, 1, "", "403 Client Error: Forbidden")
        status, version = state
        if "--format" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"status": status, "current_version_number": version}), "")
        return subprocess.CompletedProcess(cmd, 0, status, "")

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[:3] == ["kaggle", "config", "view"]:
            return subprocess.CompletedProcess(cmd, self.config_rc, self.config_stdout, "")
        if cmd[:3] == ["kaggle", "datasets", "status"]:
            return self._status(cmd)
        if cmd[:3] in (["kaggle", "datasets", "create"], ["kaggle", "datasets", "version"]):
            self.uploaded = True
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def uploads(self):
        return [c for c in self.calls if c[2] in ("create", "version")]


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _upload(tmp_path, runner, clock=None, timeout=300, interval=30):
    clock = clock or FakeClock()
    return kaggle_upload.upload(tmp_path, "slug", "Title", "msg", runner=runner, ready_timeout=timeout,
                                poll_interval=interval, sleep=clock.sleep, clock=clock)


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("KAGGLE_USERNAME", "alice")
    monkeypatch.setenv("KAGGLE_KEY", "fake-key")


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


def test_success_is_logged_after_cli_call(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KAGGLE_USERNAME", "alice")
    monkeypatch.setenv("KAGGLE_KEY", "fake-key")
    assert _upload(tmp_path, FakeRunner()) == "created"
    assert "created private dataset alice/slug" in capsys.readouterr().out


def test_failed_cli_call_raises_and_does_not_log_success(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KAGGLE_USERNAME", "alice")
    monkeypatch.setenv("KAGGLE_KEY", "fake-key")

    def runner_where_create_fails(cmd, check=False, **kwargs):
        if cmd[:3] == ["kaggle", "datasets", "status"]:
            return subprocess.CompletedProcess(cmd, 1, "", "")  # dataset does not exist yet
        raise subprocess.CalledProcessError(1, cmd)  # what check=True does on a non-zero exit

    with pytest.raises(subprocess.CalledProcessError):
        _upload(tmp_path, runner_where_create_fails)
    assert "created private dataset" not in capsys.readouterr().out


def test_create_waits_until_dataset_is_ready(tmp_path, creds, capsys):
    clock = FakeClock()
    runner = FakeRunner(states=[None, None, ("ready", 1)])
    assert _upload(tmp_path, runner, clock) == "created"
    assert clock.sleeps == [30, 30]
    assert "created private dataset alice/slug (version 1 ready)" in capsys.readouterr().out


def test_dataset_that_never_appears_times_out(tmp_path, creds, capsys):
    clock = FakeClock()
    runner = FakeRunner(states=[None])  # Kaggle accepted the files, then processing failed: 403 forever
    with pytest.raises(kaggle_upload.KaggleUploadError) as err:
        _upload(tmp_path, runner, clock, timeout=300, interval=30)
    message = str(err.value)
    assert "alice/slug was not ready after 300s" in message and "not visible" in message
    assert "can be uploaded again" in message
    assert clock.now == 300 and len(runner.uploads()) == 1
    assert "created private dataset" not in capsys.readouterr().out


def test_processing_error_fails_without_waiting(tmp_path, creds):
    clock = FakeClock()
    runner = FakeRunner(states=[("error", 1)])
    with pytest.raises(kaggle_upload.KaggleUploadError, match="status: error"):
        _upload(tmp_path, runner, clock)
    assert clock.sleeps == []


def test_version_waits_for_the_new_version(tmp_path, creds, capsys):
    clock = FakeClock()
    runner = FakeRunner(existing=("ready", 3), states=[("ready", 3), ("pending", 4), ("ready", 4)])
    assert _upload(tmp_path, runner, clock) == "versioned"
    assert runner.uploads()[0][:3] == ["kaggle", "datasets", "version"]
    assert clock.sleeps == [30, 30]
    assert "versioned private dataset alice/slug (version 4 ready)" in capsys.readouterr().out


def test_version_that_never_lands_times_out(tmp_path, creds):
    runner = FakeRunner(existing=("ready", 3), states=[("ready", 3)])  # only the old version stays visible
    with pytest.raises(kaggle_upload.KaggleUploadError, match="a version newer than 3 of alice/slug was not ready"):
        _upload(tmp_path, runner)


def test_cli_without_format_option_falls_back_to_plain_status(tmp_path, creds):
    runner = FakeRunner(states=[None, ("ready", None)], json_format=False)
    assert _upload(tmp_path, runner) == "created"
    assert ["kaggle", "datasets", "status", "alice/slug"] in runner.calls


@pytest.mark.parametrize("stdout,expected", [
    ('{"status": "ready", "current_version_number": 2}', ("ready", 2)),
    ('Warning: newer CLI available\n{"status": "PENDING", "current_version_number": 5}\n', ("pending", 5)),
    ("ready", ("ready", None)),
    ("Warning: newer CLI available\nerror\n", ("error", None)),
    ('{"status": "ready", "current_version_number": "2"}', ("ready", None)),
    ("", (None, None)),
    ("something unexpected", (None, None)),
])
def test_parse_status(stdout, expected):
    assert kaggle_upload._parse_status(stdout) == expected


def test_legacy_snapshot_names_are_refused_before_upload(tmp_path, creds):
    (tmp_path / "snapshots").mkdir()
    (tmp_path / "snapshots" / "a-1.tar.gz").write_bytes(b"x")
    runner = FakeRunner()
    with pytest.raises(kaggle_upload.KaggleUploadError, match="harvest aggregate"):
        _upload(tmp_path, runner)
    assert runner.uploads() == []


def test_cli_reports_upload_failure(tmp_path, monkeypatch, capsys):
    def failing_upload(*args, **kwargs):
        raise kaggle_upload.KaggleUploadError("Kaggle upload: alice/slug was not ready")

    monkeypatch.setattr(kaggle_upload, "upload", failing_upload)
    assert cli.main(["kaggle-upload", "--dir", str(tmp_path), "--ready-timeout", "5"]) == 1
    assert "alice/slug was not ready" in capsys.readouterr().err
