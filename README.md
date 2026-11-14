# python-bug-harvest

Generic harvester that rebuilds public Python bug datasets into runnable, test-verified tasks.
It is a general-purpose tool and is not tied to any particular downstream project.

For each candidate bug it produces:

- a **snapshot tarball** of the project at the bug's `base_commit`,
- the **issue text**, **gold (reference) patch**, **test patch**, and the `FAIL_TO_PASS` / `PASS_TO_PASS` test lists,
- **proof that the task works**: with the test patch applied, the empty patch leaves every `FAIL_TO_PASS` test
  failing, and the gold patch makes every `FAIL_TO_PASS` test pass while every `PASS_TO_PASS` test still passes.

Rejected candidates are logged with the reason they failed.

## Sources and licences

| Source | Hugging Face dataset (pinned revision) | Licence | Used |
|---|---|---|---|
| SWE-rebench | [`nebius/SWE-rebench`](https://huggingface.co/datasets/nebius/SWE-rebench) @ `89cdfbab4ab1bd8f5a658bb212d1b63624f4f881` | CC-BY-4.0 | primary (trial) |
| SWE-bench | [`princeton-nlp/SWE-bench`](https://huggingface.co/datasets/princeton-nlp/SWE-bench) @ `e48e2bd1e9fecd5bbd641e9414ac59da9f2e69f6` | MIT | supported |
| SWE-Gym | [`SWE-Gym/SWE-Gym`](https://huggingface.co/datasets/SWE-Gym/SWE-Gym) @ `bb94ed9e39bbeb96a7fcbfb533b80f25a7fd59cb` | MIT | supported |

Attribution: task metadata (issue text, patches, test lists, install recipes) comes from the datasets above;
SWE-rebench content is used under [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/)
(Badertdinov et al., *SWE-rebench: An Automated Pipeline for Task Collection and Decontaminated Evaluation of Software Engineering Agents*, 2025, [arXiv:2505.20411](https://arxiv.org/abs/2505.20411)). Every output row records `source`, `source_dataset`,
`source_revision` and `source_licence`. Snapshot tarballs contain third-party source code under each project's
own licence (`repo_license` on the row, when the source provides it). The synthetic SWE-smith dataset is not
used; if added later it must be a separate source tagged `synthetic: true`.

Revisions are pinned in [`harvest/sources.py`](harvest/sources.py); a candidate file that names a different
revision is refused.

## How it works

1. **Select** (`python -m harvest select`) – deterministically picks candidates from a source split and writes a
   small JSON candidate list (committed under [`candidates/`](candidates/)). `--exclude-ids` skips bugs listed in
   earlier candidate files (see [Later batches](#later-batches)).
2. **Plan** (`python -m harvest plan`) – loads the pinned dataset, materialises the candidate rows and a
   GitHub Actions job matrix (`--per-job N` candidates per job; Actions allows at most 256 jobs per matrix).
3. **Verify** (`python -m harvest verify`) – for each candidate:
   - shallow-fetches `https://github.com/<repo>` at `base_commit`;
   - builds a fresh [uv](https://docs.astral.sh/uv/) virtualenv and installs the project, trying *tiers* in order:
     1. `py3.13-recipe` – the target Python (default 3.13) with the project's own, unpinned dependency files
        and the source's install command;
     2. `py3.13-pinned` – the target Python with the source dataset's pinned `pip freeze`;
     3. `py<X>-pinned` – the Python version and pinned `pip freeze` recorded by the source dataset
        (tier 2 is skipped when X already is the target).

     SWE-rebench ships an `install_config` per row (Python version, apt pre-install commands, requirements files,
     install command, test command) plus a pinned `requirements` freeze; both tiers are built from it. Sources
     without install specs (SWE-bench, SWE-Gym) use a heuristic recipe (`requirements*.txt` +
     `pip install -e .[test,tests,testing,dev]`) on the target Python, then on Python 3.11.
     A dependency step that fails as a whole is retried one requirement (or apt package) at a time, and because
     shallow clones have no tags, the project install sets `SETUPTOOLS_SCM_PRETEND_VERSION` from the dataset's
     `version` field;
   - applies the test patch, runs the test command on the touched test files, applies the gold patch, runs again,
     and checks the verification contract above. The first tier that satisfies it wins;
   - writes a `git archive` snapshot of `base_commit` for verified bugs.
4. **Aggregate** (`python -m harvest aggregate`) – merges per-candidate results into the output layout below.
5. **Kaggle upload** (`python -m harvest kaggle-upload`) – optional, see below.

## Output format

```
dataset/
  harvest.jsonl          one SWE-bench-style row per verified bug
  snapshots/<instance_id>.tgz
  rejections.jsonl       one row per rejected candidate: reason + every tier attempt
  summary.json, summary.md
  README.md              attribution for the published dataset
```

`harvest.jsonl` fields: `instance_id`, `repo`, `base_commit`, `environment_setup_commit`, `created_at`,
`version`, `problem_statement`, `hints_text`, `patch` (gold), `test_patch`, `FAIL_TO_PASS`, `PASS_TO_PASS`
(lists), `source`, `source_dataset`, `source_revision`, `source_licence`, `repo_license`, `synthetic`, plus the
reproduction recipe that passed: `python_version` (exact interpreter used), `install_tier`, `install_commands`
(only the commands that succeeded, in order; a dependency step that failed as a whole contributes the individual
pieces whose retry succeeded), `install_omitted` (privileged steps skipped for lack of root/sudo and soft steps or
pieces that failed, as `{"cmd", "status": "skipped" | "failed"}`; not part of the recipe), `frozen_requirements`
(the filtered pinned freeze when an install command reads `$HARVEST_FROZEN` – write it to a file and point
`HARVEST_FROZEN` at it – else `null`), `install_env`, `test_cmd`, `snapshot`, `snapshot_sha256`.

Snapshots are gzipped `git archive` tarballs (no `.git` directory, no submodule contents), with every path prefixed
by `<instance_id>/`. They are named `<instance_id>.tgz` (see [Kaggle upload](#kaggle-upload)), and the `snapshot` field
always holds the file name. Results from older runs, whose snapshots were named `.tar.gz`, are renamed by aggregate,
and it deletes the `.tar.gz` snapshots an older run left in its output folder.

Rejection reasons: `clone_failed`, `python_unavailable`, `env_install_failed`, `test_patch_apply_failed`,
`gold_patch_apply_failed`, `tests_timeout`, `no_tests_collected`, `f2p_passes_without_fix`,
`f2p_fails_with_fix`, `p2p_fails_with_fix`, `snapshot_failed`, `harness_error`, plus `invalid_result` (set by
aggregate for a `result.json` whose instance id or snapshot name does not match its directory). The reason is the one from the
last tier tried; `rejections.jsonl` keeps every attempt and the per-candidate `harvest.log` has full command output.

## Running on GitHub Actions

[`.github/workflows/harvest.yml`](.github/workflows/harvest.yml) runs plan → verify (matrix, one ubuntu job per
shard, in parallel) → aggregate. Trigger it with **Run workflow** (`workflow_dispatch`; inputs: candidate file,
candidates per job, max parallel jobs, target Python, Kaggle slug) or by pushing a `harvest-run/**` branch.
Outputs are uploaded as short-retention (7 day) artifacts: `harvest-dataset` (the layout above) and
`result-<shard>` (per-candidate logs).

### Kaggle upload

The aggregate job uploads `dataset/` to a **private** Kaggle dataset `<KAGGLE_USERNAME>/<slug>` (created on first
run, new version afterwards) when Kaggle credentials are set as repository secrets: either `KAGGLE_API_TOKEN` (the
new single token, needs `kaggle>=1.8.3`, see the
[Kaggle CLI auth docs](https://github.com/Kaggle/kaggle-cli/blob/main/docs/README.md#authentication)) or the legacy
`KAGGLE_USERNAME` + `KAGGLE_KEY` pair. With the token, also set `KAGGLE_USERNAME` (the dataset owner); if it is
missing the step tries to read the username from `kaggle config view` and otherwise skips. Without credentials
the step is a no-op and logs `Kaggle upload skipped: ...`. Credentials are only read from the environment and are
never written to the repository.

With credentials, the step installs `kaggle==2.2.5` plus `six` (also the `kaggle` extra in `pyproject.toml`).
`six` is listed explicitly because kaggle 1.8.4+ no longer declares it although the CLI still imports it. The step
logs `Kaggle upload: created|versioned private dataset ...` only after the `kaggle datasets` call succeeds and
Kaggle has processed the upload: it polls `kaggle datasets status` until the dataset, or its new version, is ready.
A failed call, status `error`, or nothing ready within 30 minutes (`--ready-timeout`) fails the job. A new version
is recognised by the version number that `kaggle datasets status --format json` reports; with a CLI that reports
none, versioning an existing slug fails before anything is uploaded.

Kaggle unpacks uploaded archives, including archives inside them: the `snapshots.zip` that `--dir-mode zip` builds
and every `.tar.gz` in it. Some source trees, for example one with a directory named `__main__` or a path containing
`&`, then make processing fail without an error: the CLI reports success but the dataset never appears. Kaggle keeps
`.tgz` files as they are, so snapshots use that name, and the upload refuses a `snapshots/` folder with `.tar.gz`
files. A slug whose processing failed can be uploaded again.

## Running locally

Verification needs Linux (or WSL/macOS) with `git`, `bash` and [`uv`](https://docs.astral.sh/uv/); apt
pre-install steps run only when root or passwordless `sudo` is available.

```sh
pip install -e ".[dev]"
python -m harvest run --candidates candidates/trial-20.json --only casbin__pycasbin-346 --out out --keep-work
# or step by step
python -m harvest plan --candidates candidates/trial-20.json --out out/plan
python -m harvest verify --plan out/plan/candidates.jsonl --ids casbin__pycasbin-346 --out out/results
python -m harvest aggregate --results out/results --plan out/plan/candidates.jsonl --out out/dataset
```

Downloaded parquet files are cached in `.cache/datasets` (override with `HARVEST_CACHE`); work directories go to
the system temp dir (override with `HARVEST_WORK`). Outputs and caches are git-ignored.

Unit tests: `pytest` (end-to-end verification tests run when `uv` and `bash` are available).

## Trial candidate list

[`candidates/trial-20.json`](candidates/trial-20.json) was generated with

```sh
python -m harvest select --source swe-rebench --split filtered --limit 20 --created-from 2023 \
  --exclude fastapi,rich,requests,httpx --out candidates/trial-20.json
```

i.e. 20 different projects, bugs created 2023 or later, preferring rows whose recorded Python is closest to 3.13
and whose install recipe is simplest.

Trial result (Actions, `ubuntu-24.04`, one candidate per job): **20 / 20 verified**. Python used was 3.13 for 16 bugs
and 3.12 for 4. Wall time per bug: mean 38 s, median 24 s, max 113 s. Snapshots average 4.1 MB (82 MB in total).
The run used 22 jobs: about 17 runner-minutes, or 29 minutes with per-job rounding.

## Bulk candidate list

[`candidates/bulk-500.json`](candidates/bulk-500.json) uses the same filters with a larger limit:

```sh
python -m harvest select --source swe-rebench --split filtered --limit 500 --created-from 2023 \
  --exclude fastapi,rich,requests,httpx --out candidates/bulk-500.json
```

That gives 500 bugs from 500 different projects; its first 20 entries are the trial list. The excluded projects
are reserved for a held-out evaluation set. Run it with `per_job=2` (250 matrix jobs, under the 256 cap):

```sh
gh workflow run harvest.yml --ref main -f candidates=candidates/bulk-500.json -f per_job=2
```

## Later batches

With one bug per project, a larger `--limit` returns the earlier list again before adding anything new. To pick a
new batch without repeats, pass the earlier candidate files to `--exclude-ids` (comma-separated):

```sh
python -m harvest select --source swe-rebench --split filtered --limit 500 --created-from 2023 \
  --exclude fastapi,rich,requests,httpx --exclude-ids candidates/bulk-500.json --out candidates/batch-2.json
```

Bugs in those files are skipped, whatever source or pinned revision they came from, and they count toward
`--per-project N` (default 1): the most bugs any one project may have across the earlier files and the new list.
A bug listed in two files counts once. So the default picks only projects no earlier file used, including
projects whose earlier bug was rejected; `--per-project 2` also allows one more bug from each project. Every
project gets its first bug before any project gets its second, so new projects still come first. The new
candidate file records `per_project` and `exclude_ids_from` in its criteria when they are used; runs without these
options write the same criteria as before.

## Batch 2 candidate lists

Batch 2 keeps one bug per project and skips every project in the earlier lists. SWE-rebench `filtered` has only
274 such projects from 2023 on, so the window starts in 2022:

```sh
python -m harvest select --source swe-rebench --split filtered --limit 500 --created-from 2022 \
  --exclude fastapi,rich,requests,httpx --exclude-ids candidates/trial-20.json,candidates/bulk-500.json \
  --out candidates/batch-2.json        # 469 bugs from 469 projects: every one left
python -m harvest select --source swe-rebench --split filtered --limit 20 --created-from 2022 \
  --exclude fastapi,rich,requests,httpx --exclude-ids candidates/trial-20.json,candidates/bulk-500.json \
  --out candidates/batch-2-trial-20.json   # the first 20 of batch-2.json
python -m harvest select --source swe-gym --split train --limit 500 --created-from 2022 \
  --exclude fastapi,rich,requests,httpx \
  --exclude-ids candidates/trial-20.json,candidates/bulk-500.json,candidates/batch-2.json \
  --out candidates/batch-2-swe-gym.json    # 6 bugs
python -m harvest select --source swe-bench --split test --limit 500 --created-from 2022 \
  --exclude fastapi,rich,requests,httpx \
  --exclude-ids candidates/trial-20.json,candidates/bulk-500.json,candidates/batch-2.json,candidates/batch-2-swe-gym.json \
  --out candidates/batch-2-swe-bench.json  # 6 bugs
```

SWE-Gym and SWE-bench each come from about a dozen large projects, so one bug per project adds only 12; most of
`batch-2.json` was recorded on Python 3.9, because batch 1 took the newer ones. Run the trial first, then the full
lists (`per_job=2` gives 235 jobs for `batch-2.json`). To keep batch 1 intact, upload each batch to its own
dataset: `kaggle-upload` versions an existing slug, so reusing `python-bug-harvest` would replace batch 1's files
in the latest version.

```sh
gh workflow run harvest.yml --ref main -f candidates=candidates/batch-2-trial-20.json -f per_job=1 \
  -f kaggle_slug=python-bug-harvest-b2-trial
gh workflow run harvest.yml --ref main -f candidates=candidates/batch-2.json -f per_job=2 \
  -f kaggle_slug=python-bug-harvest-b2
```

## Licence

MIT – see [LICENSE](LICENSE). This licence covers the harvester code only, not the harvested data.
