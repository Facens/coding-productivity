---
name: coding-productivity:setup
description: 'Interactive configuration wizard for coding-productivity. Use when setting up the plugin for the first time, adding/removing repositories, changing storage backend, or toggling AI scoring. Re-runnable — safely modifies existing config without data loss.'
---

# Setup Wizard for coding-productivity

This skill runs an interactive configuration wizard that creates or modifies `.coding-productivity.env`. It is safe to re-run at any time.

**Important:** Use `AskUserQuestion` for every interactive prompt. Never assume answers or skip steps.

## Locate the Project Root

The project root is the directory containing `.coding-productivity.env` (for re-runs) or the current working directory (for fresh setup). All paths below are relative to this root.

## Detect Fresh vs. Re-run

1. Use the Read tool to attempt reading `.coding-productivity.env` in the project root.
2. If the file exists and contains config values, this is a **re-run** — jump to the Re-run Flow.
3. If the file does not exist or is empty, this is a **fresh setup** — proceed with the Fresh Setup Flow.

---

## Fresh Setup Flow

Execute these steps in order. Use `AskUserQuestion` for each prompt.

### Step 1: Platform

Ask the user:
> Which Git platform do you use?
>
> 1. GitHub
> 2. GitLab

Save the choice. If GitLab, also ask:
- **GitLab URL** (e.g., `https://gitlab.example.com`) — required for self-hosted instances
- **Custom CA bundle path** (optional) — only if they have a self-hosted GitLab with a custom certificate

### Step 2: API Token

**MANDATORY FIRST ACTION — run this before showing any prompt to the user.**

First, find the plugin's scripts directory:
```
find ~/.claude/plugins -path "*/coding-productivity/scripts/detect_token.py" 2>/dev/null | head -1
```

Then run the token detection script using the found path:
```
python3 /path/to/detect_token.py github
```
or for GitLab:
```
python3 /path/to/detect_token.py gitlab --url GITLAB_URL
```

Use the Bash tool for both commands. Check the exit code of the second command.

**If exit code is 0** (token found — printed to stdout):
- Store the token value
- Ask the user via AskUserQuestion:
  - Question: "Found an existing CLI authentication. Use this token?"
  - Option 1: "Yes, use the detected token (Recommended)"
  - Option 2: "No, I'll provide a different token"
- If the user picks option 1, use the detected token and skip to Step 3.

**If exit code is 1** (no token found), or user picked option 2, ask:
> Please paste your GitHub/GitLab API token.

**Validate the token immediately** by calling the platform API:

- **GitHub**: Run via Bash:
  ```
  curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer TOKEN" https://api.github.com/user/repos?per_page=1
  ```
  A `200` response means the token is valid. Any other code means failure.

- **GitLab**: Run via Bash:
  ```
  curl -s -o /dev/null -w "%{http_code}" -H "PRIVATE-TOKEN: TOKEN" GITLAB_URL/api/v4/projects?membership=true&per_page=1
  ```
  A `200` response means the token is valid.

**On failure**, display the error and explain the required scopes:
- **GitHub**: Needs `repo` scope (classic token) or a fine-grained token with Contents + Pull Requests read access. Create one at: `https://github.com/settings/tokens`
- **GitLab**: Needs `read_api` scope. Link: `GITLAB_URL/-/user_settings/personal_access_tokens`

Ask the user to try again. Do not proceed until validation succeeds.

### Step 3: Repository Selection

After token validation, fetch the full list of accessible repositories:

- **GitHub**: `curl -s -H "Authorization: Bearer TOKEN" "https://api.github.com/user/repos?per_page=100&type=all"` — paginate if needed, extract `full_name` from each repo.
- **GitLab**: `curl -s -H "PRIVATE-TOKEN: TOKEN" "GITLAB_URL/api/v4/projects?membership=true&per_page=100"` — paginate if needed, extract `path_with_namespace` from each repo.

Present the list to the user as a multi-select with a "Select all" option at the top:

> Which repositories should be tracked? (comma-separated numbers, or "all")
>
> 0. Select all
> 1. org/repo-one
> 2. org/repo-two
> ...

Save the selected repos as a comma-separated list for the `REPOS` config key.

### Step 4: Storage Backend

Ask:
> Which storage backend?
>
> 1. DuckDB (Recommended, default) — local file, zero setup
> 2. BigQuery — cloud-based, for large-scale analysis

If **DuckDB** (default): set `STORAGE_BACKEND=duckdb` and `DB_PATH=.coding-productivity/data.duckdb`.

If **BigQuery**: ask for:
- GCP Project ID
- BigQuery Dataset name
- Path to service account JSON file (validate the file exists using Read tool)

Set `STORAGE_BACKEND=bigquery` and the corresponding `GCP_PROJECT_ID`, `BQ_DATASET`, `GOOGLE_APPLICATION_CREDENTIALS`.

### Step 5: Anonymization

Ask:
> Enable developer identity anonymization?
>
> 1. Yes (Recommended, default) — developer emails are hashed; real identities stored in a separate mapping file
> 2. No — developer emails stored as-is in the dataset

If **Yes**:
- Generate a pseudonymization salt via Bash: `python3.14 -c "import secrets; print(secrets.token_hex(32))"`
- Save the output as `PSEUDONYMIZATION_KEY`
- Set `ANONYMIZATION_ENABLED=true`
- Display this warning prominently (use bold/caps):

> **WARNING: Your pseudonymization key has been generated. BACK IT UP. Losing it means existing hashes become permanently unresolvable. Store a copy somewhere safe outside this project.**

If **No**: set `ANONYMIZATION_ENABLED=false`.

### Step 6: AI Scoring

Ask:
> Enable AI-powered commit scoring? This uses the Anthropic API to evaluate commit quality and assign scores.
>
> 1. No (default) — skip AI scoring
> 2. Yes — enable AI-powered commit quality scoring

If **Yes**:
- Ask for the Anthropic API key
- Validate by calling: `curl -s -o /dev/null -w "%{http_code}" -H "x-api-key: KEY" -H "anthropic-version: 2023-06-01" https://api.anthropic.com/v1/models`
- A `200` response means valid. On failure, show the error and ask to retry.
- Set `SCORING_ENABLED=true` and `ANTHROPIC_API_KEY`

If **No**: set `SCORING_ENABLED=false`.

### Step 7: Summary & Confirmation

Display a formatted summary of ALL configuration:

```
=== coding-productivity Configuration Summary ===

Platform:       GitHub
Token:          ghp_****...****(last 4)   [validated]
Repositories:   3 selected
                - org/repo-one
                - org/repo-two
                - org/repo-three
Storage:        DuckDB (.coding-productivity/data.duckdb)
Anonymization:  Enabled (key generated)
AI Scoring:     Disabled

===================================================
```

**Always mask tokens** — show only the last 4 characters.

Ask:
> Does this look correct? Save configuration?
>
> 1. Yes, save it
> 2. No, let me start over

If "No", restart from Step 1.

### Step 8: Write Configuration

Write the config to `.coding-productivity.env` using the Write tool. Use the following template, filling in the collected values:

```
# coding-productivity configuration
# Created by /coding-productivity:setup
# Keep this file private — it contains API tokens and the pseudonymization key.
# NEVER commit this file to version control.

# ─── Platform & Authentication ───────────────────────────────────────
PLATFORM={platform}
GITHUB_TOKEN={github_token}
GITLAB_TOKEN={gitlab_token}
GITLAB_URL={gitlab_url}
GITLAB_CA_BUNDLE={gitlab_ca_bundle}

# ─── Repositories ────────────────────────────────────────────────────
REPOS={repos_comma_separated}

# ─── Storage ─────────────────────────────────────────────────────────
STORAGE_BACKEND={storage_backend}
STORAGE_MODE=readwrite
DB_PATH={db_path}
GCP_PROJECT_ID={gcp_project_id}
BQ_DATASET={bq_dataset}
GOOGLE_APPLICATION_CREDENTIALS={google_creds_path}

# ─── AI Scoring (optional) ───────────────────────────────────────────
SCORING_ENABLED={scoring_enabled}
ANTHROPIC_API_KEY={anthropic_api_key}

# ─── Anonymization ───────────────────────────────────────────────────
ANONYMIZATION_ENABLED={anonymization_enabled}
PSEUDONYMIZATION_KEY={pseudonymization_key}

# ─── Developer Management ────────────────────────────────────────────
EXCLUDED_DEVELOPERS=
BOT_OVERRIDES=
IDENTITY_MERGES=
```

Leave keys blank (not omitted) when they are not applicable to the chosen configuration.

After writing, set permissions via Bash:
```
chmod 600 .coding-productivity.env
```

### Step 9: Update .gitignore

Read `.gitignore` in the project root (create it if it does not exist). Ensure these entries are present — add any that are missing:

```
.coding-productivity.env
.coding-productivity/
developer_mapping.json
```

Do not duplicate entries that already exist.

### Step 10: Bootstrap Python Environment

Run via Bash:
```
python3.14 scripts/setup_env.py
```

Report the output to the user. If it fails, show the error and suggest troubleshooting steps.

### Step 11: Offer Next Step

Display the security warnings:

> **Security reminder:** Your config file contains API tokens. Do not place this project in a cloud-synced directory if security matters.

If anonymization is enabled, also display:

> **Anonymization notice:** The pseudonymization key and developer mapping file together can deanonymize the dataset. Store backups of these files separately.

Then ask:
> Setup complete. Would you like to extract data now?
>
> 1. Yes, run /coding-productivity:extract
> 2. No, I will do it later

If "Yes", tell the user to run `/coding-productivity:extract`.

---

## Re-run Flow

When `.coding-productivity.env` already exists:

### Step 1: Load and Display Current Config

Read `.coding-productivity.env` and parse all values. Display a summary identical to the format in Fresh Setup Step 7, with tokens masked.

### Step 2: Section Selection

Ask:
> Which section would you like to modify?
>
> 1. Platform & Authentication
> 2. Repositories
> 3. Storage Backend
> 4. Anonymization
> 5. AI Scoring
> 6. Everything looks good — exit

### Step 3: Handle Each Section

#### Platform & Authentication (option 1)
- Show current platform and token (masked)
- Run the same flow as Fresh Setup Steps 1-2
- After changing, also re-run Repository Selection (Step 3 of fresh) since repos depend on the token

#### Repositories (option 2)
- Show currently configured repos
- Fetch the full repo list using the existing token
- Present the multi-select as in Fresh Setup Step 3
- Pre-select currently configured repos
- **If repos are removed**, inform the user:
  > "Removing a repo does not delete existing data. Analysis will simply filter by active repos."
- **If repos are added**, inform the user:
  > "New repos will be included on the next run of /coding-productivity:extract."

#### Storage Backend (option 3)
- Show current backend
- If changing from one backend to another, display a warning:
  > **Warning:** Switching backend does not migrate data. Your existing data remains as a backup in the previous backend. You will need to re-extract.
- Run the same flow as Fresh Setup Step 4

#### Anonymization (option 4)
- **Check if data exists first.** Run via Bash:
  ```
  python3.14 -c "
  from scripts.lib.config import Config
  from scripts.lib.storage import Storage
  cfg = Config()
  st = Storage(cfg)
  print(st.count('commits'))
  "
  ```
- **If commit count > 0**, display as READ-ONLY:
  > **Anonymization: Locked**
  > Anonymization settings cannot be changed while data exists ({count} commits in storage). To change anonymization settings, delete the data file (`.coding-productivity/data.duckdb` for DuckDB) and re-extract.
  >
  > Current setting: {Enabled/Disabled}
  > Key: {masked}

  Do NOT allow changes. Return to section selection.

- **If no data exists**, run the same flow as Fresh Setup Step 5. If changing from disabled to enabled, generate a new key. If changing from enabled to disabled, warn that the existing key will be removed from config (but not deleted from backups).

#### AI Scoring (option 5)
- Show current setting
- Run the same flow as Fresh Setup Step 6

### Step 4: Save and Confirm

After modifying any section:
1. Write the updated config to `.coding-productivity.env` (preserving all other values)
2. Set `chmod 600`
3. Show the updated summary
4. Return to section selection (Step 2) so the user can modify additional sections or exit

### Exiting Re-run

When the user selects "Everything looks good", display:
> Configuration saved. Run `/coding-productivity:extract` to pull data with the updated settings.
