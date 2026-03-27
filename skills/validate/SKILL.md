---
name: validate
description: 'Compare plugin analysis results against a reference dataset to verify correctness. Use to validate a new instance against known-good results, or generate a reference snapshot.'
---

# Validation for coding-productivity

This skill compares analysis output from the plugin against a reference dataset to verify that results are correct. It can also generate a reference snapshot from current data. Useful after initial setup, after changing storage backends, or after re-extracting data.

**Important:** Use `AskUserQuestion` for every interactive prompt. Never assume answers or skip steps.

**No org-specific references.** Reference files are generated from each instance's own data. Do not bundle or distribute pre-built reference datasets tied to a specific organization.

## Step 1: Determine Mode

Ask the user:
> How would you like to validate?
>
> 1. **Generate a reference snapshot** from current data (snapshot current results as the baseline)
> 2. **Compare against an existing reference file** (verify current results match a known-good baseline)

## Step 2a: Generate Reference Snapshot

If the user chose to generate a reference file:

1. Locate the `.coding-productivity.env` config file in the project root.
2. Ask the user for the date range:
   > What date range should the reference cover?
   >
   > - **Since** (YYYY-MM-DD):
   > - **Until** (YYYY-MM-DD):

3. Determine the output path. Use today's date:
   ```
   .coding-productivity/reference-YYYY-MM-DD.json
   ```

4. Run the generation via Bash:
   ```
   python3.14 scripts/validate.py \
       --config .coding-productivity.env \
       --generate-reference .coding-productivity/reference-{today}.json \
       --since {since} --until {until}
   ```

5. Display the result:
   > Reference file saved to `.coding-productivity/reference-{today}.json`
   >
   > Contains:
   > - {N} monthly trend rows
   > - {N} author productivity rows
   > - {N} category distribution rows
   >
   > Use this file with option 2 to validate future runs.

## Step 2b: Compare Against Reference File

If the user chose to compare against an existing reference:

1. Ask for the reference file path:
   > Path to the reference file (JSON or CSV):
   >
   > Hint: generated references are saved under `.coding-productivity/reference-*.json`

2. Read the reference file to extract the period (if present). Ask the user to confirm or override:
   > The reference file covers **{since}** to **{until}**. Use the same date range?
   >
   > 1. Yes, use the same range
   > 2. No, let me specify different dates

3. Run the validation script via Bash:
   ```
   python3.14 scripts/validate.py \
       --config .coding-productivity.env \
       --reference {reference_path} \
       --since {since} --until {until} \
       --tolerance 0.01
   ```

4. Display the full output table to the user.

## Step 3: Interpret Results

After displaying the results table, explain the status codes:
- **PASS** -- exact match between reference and plugin output
- **OK** -- values differ but within the configured tolerance (default: +/-0.01 for scores)
- **FAIL** -- values differ beyond tolerance, or integer metrics do not match exactly

Display the summary line (e.g., "Validation: 12 PASS, 3 OK, 0 FAIL").

## Step 4: Handle Failures

If there are any FAIL results, walk the user through possible causes:

1. **Date range misalignment** -- the reference period and the validation period must match exactly. Even a one-day difference in `--since` or `--until` changes commit counts.
2. **Storage backend differences** -- DuckDB and BigQuery may produce slightly different floating-point rounding. If migrating between backends, increase `--tolerance` (e.g., `0.05`) to account for this.
3. **DuckDB vs BigQuery rounding** -- DuckDB uses IEEE 754 double precision; BigQuery uses FLOAT64 with different rounding for `ROUND()` at boundary values. Scores at exactly N.5 may round differently.
4. **Data changes** -- if new commits were extracted between generating the reference and running validation, counts will differ. Re-generate the reference after extraction to get a fresh baseline.
5. **Configuration changes** -- if `REPOS`, `EXCLUDED_DEVELOPERS`, or `IDENTITY_MERGES` changed, the filtered dataset is different. Ensure the same config was active for both the reference and the validation.

Ask the user:
> Would you like to:
>
> 1. Re-run with a higher tolerance (e.g., 0.05)
> 2. Generate a new reference file from current data
> 3. Done

## Step 5: Success

If all results are PASS or OK (exit code 0), display:

> All metrics validated successfully. The plugin output matches the reference dataset.

No further action needed.
