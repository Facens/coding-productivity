---
name: connect
description: 'Connect to an existing BigQuery dataset for read-only analysis. Use when you have access to a pre-populated dataset and want to explore results without running extraction.'
---

# Connect to an Existing BigQuery Dataset

This skill connects the plugin to a pre-populated BigQuery dataset in **read-only mode**, allowing analysis without extraction.

**Important:** Use `AskUserQuestion` for every interactive prompt. Never assume answers or skip steps.

## Locate the Project Root

The project root is the directory containing `.coding-productivity.env` (or the current working directory for fresh setups). All paths below are relative to this root.

---

## Step 1: Collect Connection Details

Ask the user for the following, one at a time:

1. **GCP Project ID**
   > What is the GCP project ID? (e.g., `my-project-123`)

2. **BigQuery Dataset Name**
   > What is the BigQuery dataset name? (e.g., `coding_productivity`)

3. **Service Account JSON Path**
   > Provide the path to the service account JSON key file.

   Validate the file exists using the Read tool. If it does not exist, show the error and ask again.

---

## Step 2: Ensure google-cloud-bigquery Is Installed

Check whether the Python venv exists. If `scripts/.venv` does not exist, run via Bash:
```
python3.14 scripts/setup_env.py
```

Then install the BigQuery client library in the venv if not already present:
```
scripts/.venv/bin/pip install google-cloud-bigquery
```

---

## Step 3: Validate the Connection

Run via Bash:
```
scripts/.venv/bin/python3.14 -c "
import os, sys
os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = '<SERVICE_ACCOUNT_PATH>'
from google.cloud import bigquery
client = bigquery.Client(project='<GCP_PROJECT_ID>')
try:
    rows = list(client.query('SELECT COUNT(*) AS cnt FROM \`<GCP_PROJECT_ID>.<BQ_DATASET>.commits\`').result())
    print(f'COMMIT_COUNT={rows[0][\"cnt\"]}')
except Exception as e:
    print(f'ERROR={e}', file=sys.stderr)
    sys.exit(1)
"
```

Replace `<SERVICE_ACCOUNT_PATH>`, `<GCP_PROJECT_ID>`, and `<BQ_DATASET>` with the user's values.

### On Validation Failure

Display the error clearly, then suggest:
- **Permission denied**: Check that the service account has `BigQuery Data Viewer` and `BigQuery Job User` roles on the project.
- **Dataset not found**: Verify the dataset name is correct and exists in the specified project.
- **Invalid credentials**: Confirm the JSON key file is a valid Google Cloud service account key.

Ask the user to correct the issue and try again. Do not proceed until validation succeeds.

---

## Step 4: Show Dataset Summary

On successful connection, fetch a summary. Run via Bash:
```
scripts/.venv/bin/python3.14 -c "
import os
os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = '<SERVICE_ACCOUNT_PATH>'
from google.cloud import bigquery
client = bigquery.Client(project='<GCP_PROJECT_ID>')
ds = '<GCP_PROJECT_ID>.<BQ_DATASET>'

rows = list(client.query(f'SELECT COUNT(*) AS cnt, MIN(committed_date) AS earliest, MAX(committed_date) AS latest, COUNT(DISTINCT author_email) AS authors FROM \`{ds}.commits\`').result())
r = rows[0]
print(f'Commits:       {r[\"cnt\"]}')
print(f'Date range:    {r[\"earliest\"]} to {r[\"latest\"]}')
print(f'Unique authors: {r[\"authors\"]}')
"
```

Display the result:
```
=== BigQuery Dataset Connected (Read-Only) ===

Project:        <GCP_PROJECT_ID>
Dataset:        <BQ_DATASET>
Commits:        12,345
Date range:     2023-01-15 to 2025-03-20
Unique authors: 42

===================================================
```

---

## Step 5: Save Configuration

Write (or update) `.coding-productivity.env` with these values:

- `STORAGE_BACKEND=bigquery`
- `STORAGE_MODE=readonly`
- `GCP_PROJECT_ID=<user value>`
- `BQ_DATASET=<user value>`
- `GOOGLE_APPLICATION_CREDENTIALS=<user value>`

If `.coding-productivity.env` already exists, preserve all other keys. Only overwrite the storage-related keys listed above.

After writing, set permissions via Bash:
```
chmod 600 .coding-productivity.env
```

---

## Step 6: Offer Next Step

Ask:
> Connection established. Would you like to explore the data now?
>
> 1. Yes, run /coding-productivity:analyze
> 2. No, I will do it later

If "Yes", tell the user to run `/coding-productivity:analyze`.
