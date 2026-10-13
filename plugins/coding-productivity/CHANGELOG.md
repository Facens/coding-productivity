# Changelog

## 0.13.0

The scoring model is now a measuring instrument: one series = one model. Model upgrades used to change the score scale silently (the scorer auto-discovered "the latest Haiku" at run time; on 150 real commits Haiku 5.5 scored 0.74x Haiku 4.5, and the ratio differed by period), so trends that crossed a model change were inflated. Switching models now means a full rescore (rebase), never splicing.

### Added

- `calibrate` skill and `scripts/calibrate.py`: rescore a deterministic, month-stratified sample with the current and a candidate model (no writes to score tables), report mean weighted_score and total_productivity ratios, Pearson, Spearman and median absolute difference overall and per quarter, and give a verdict (COMPARABLE or REBASE REQUIRED) against the test-retest noise band. Logs tokens and estimates the cost of a full rebase.
- `rebase` skill and `scripts/rebase.py`: rescore all commits through the Message Batches API into versioned tables `commit_scores_v{N}` / `file_scores_v{N}` (chunked by bytes, resumable, `custom_id` index map in a state file), retry failures through the synchronous scorer, report per-month counts, and only then switch the active series. Old tables are never modified.
- Provenance columns `scorer_model` (the `response.model` the API returned) and `prompt_version` (12 hex of sha256 over system prompt, user-prompt template, model, thinking config, effort and max_tokens) on `commit_scores` and `file_scores`.
- `scoring_meta` table: append-only log of scoring series; the active series is the latest `active` row (the unversioned tables are the legacy series 1).
- Config: `SCORING_MODEL` (required when `SCORING_ENABLED=true`), `SCORING_EFFORT` (default `medium`), `SCORING_MAX_TOKENS` (default 16000).
- `analyze.py` refuses a date range that contains more than one distinct `scorer_model`, or NULL plus non-NULL unless `--allow-legacy-null` is passed.

### Changed

- The scorer no longer auto-discovers a model. It uses `SCORING_MODEL` and prints a WARNING (never a switch) when a newer Haiku exists, pointing at `calibrate`.
- Requests to non-`claude-haiku-4*` models send `thinking={"type": "adaptive"}` and `output_config={"effort": ...}` explicitly; Haiku 4.x gets neither. The answer is read from the first `text` block (5.x models return a thinking block first, which crashed `content[0].text`).
- `stop_reason` of `refusal` or `max_tokens` counts as a failed attempt and is retried; failures are logged per month at the end of a run.
- `score_commits.py` refuses to append scores from a model other than the active series' model.
- `analyze.py`, `score`, `report` and `analyze` skills read the active series' tables.
- `create_tables` migrates existing DuckDB and BigQuery tables by adding missing columns (old rows keep NULL). BigQuery queries now resolve unqualified table names against the configured dataset.
- Shared scoring logic moved to `scripts/lib/scoring.py`; the Anthropic SDK (`anthropic>=1.13`) is imported lazily.

### Upgrade notes

- Set `SCORING_MODEL` in `.coding-productivity.env`. For existing scores, use the model that produced them (they carry no recorded model); `/coding-productivity:setup` asks.
- Scores written before this version have NULL `scorer_model`. The first run after upgrading appends rows with a recorded model, so analysis of a range that spans both needs `--allow-legacy-null` (if the model is the same) or a rebase.

## 0.1.0 (Unreleased)

Initial release.

### Skills

- `setup` - Interactive configuration wizard
- `extract` - Run extraction pipeline (GitHub/GitLab)
- `score` - AI-powered commit scoring via Claude Haiku
- `analyze` - Interactive productivity analysis
- `report` - Generate executive summary markdown
- `connect` - Connect to existing BigQuery dataset (read-only)
- `developers` - Developer roster management (bots, dedup, exclusions)
- `validate` - Compare results against reference dataset
