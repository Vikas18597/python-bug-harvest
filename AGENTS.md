# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Public, generic tool: commit only harvester code/docs; never commit harvested outputs, snapshots, parquet caches or credentials (see `.gitignore`).
- Layout and pipeline: `README.md` (How it works). Entry point `python -m harvest <select|plan|verify|aggregate|kaggle-upload|run>` (`harvest/cli.py`).
- Dataset revisions are pinned in `harvest/sources.py`; candidate files in `candidates/` must match them.
- Tests: `pytest`. End-to-end verify tests in `tests/test_pipeline.py` need Linux + `uv` + `bash` (they skip on Windows; CI runs them).
- Real harvest runs happen on Actions (`.github/workflows/harvest.yml`): push a `harvest-run/**` branch or use workflow_dispatch. Install tiers live in `harvest/recipes.py`.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
