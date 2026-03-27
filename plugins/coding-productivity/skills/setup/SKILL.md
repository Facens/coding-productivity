---
name: coding-productivity:setup
description: 'Configure coding-productivity for your project. Auto-detects CLI tokens, selects repos, and writes .coding-productivity.env.'
---

# coding-productivity Setup

## Interaction Method

Ask the user each question below using the platform's blocking question tool (e.g., `AskUserQuestion` in Claude Code, `request_user_input` in Codex). If no structured question tool is available, present each question as a numbered list and wait for a reply before proceeding. Never skip or auto-configure.

## Step 1: Check Existing Config

Read `.coding-productivity.env` in the current directory. If it exists, display current settings (mask tokens: show only last 4 chars) and ask:

```
Settings file already exists. What would you like to do?

1. Reconfigure - Run setup again from scratch
2. Modify a section - Change repos, storage, scoring, etc.
3. View current - Show the file contents, then stop
4. Cancel - Keep current settings
```

If "View current": read and display the file (mask tokens), then stop.
If "Cancel": stop.
If "Modify a section": jump to Re-run Flow below.

## Step 2: Detect Platform and Token

Auto-detect existing CLI authentication by running this via Bash:

```bash
gh auth token 2>/dev/null && echo "PLATFORM=github" || glab config get token 2>/dev/null && echo "PLATFORM=gitlab" || echo "NONE"
```

**If a token is detected**, ask:

```
Found existing CLI authentication ({platform}). How would you like to proceed?

1. Use detected token (Recommended) - No manual setup needed
2. Use a different platform or token - I'll provide one manually
```

If option 1: store the token and detected platform. Skip to Step 3.

**If no token detected**, or user chose option 2, ask:

```
Which Git platform do you use?

1. GitHub
2. GitLab
```

If GitLab, also ask for GitLab URL (for self-hosted instances).

Then ask the user to paste their API token. Validate it by running via Bash:

- **GitHub**: `curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer TOKEN" https://api.github.com/user/repos?per_page=1`
- **GitLab**: `curl -s -o /dev/null -w "%{http_code}" -H "PRIVATE-TOKEN: TOKEN" GITLAB_URL/api/v4/projects?membership=true&per_page=1`

A `200` means valid. On failure, explain:
- **GitHub**: needs `repo` scope. Create at: https://github.com/settings/tokens
- **GitLab**: needs `read_api` scope.

Retry until validation succeeds.

## Step 3: Repository Selection

Fetch the full list of accessible repositories using the validated token:

- **GitHub**: `curl -s -H "Authorization: Bearer TOKEN" "https://api.github.com/user/repos?per_page=100&type=all"` — extract `full_name`
- **GitLab**: `curl -s -H "PRIVATE-TOKEN: TOKEN" "GITLAB_URL/api/v4/projects?membership=true&per_page=100"` — extract `path_with_namespace`

Paginate if needed (follow Link headers).

Present as a multi-select:

```
Which repositories should be tracked? (comma-separated numbers, or "all")

0. Select all
1. org/repo-one
2. org/repo-two
...
```

## Step 4: Configure Options

Ask three quick questions:

**a. Storage:**

```
Storage backend?

1. DuckDB (Recommended) - Local file, zero cloud setup
2. BigQuery - Requires GCP project and service account
```

If BigQuery: ask for GCP Project ID, Dataset name, path to service account JSON.

**b. Anonymization:**

```
Anonymize developer identities?

1. Yes (Recommended) - Emails are hashed, real names stored separately
2. No - Emails stored as-is
```

If Yes: generate a key via Bash: `python3 -c "import secrets; print(secrets.token_hex(32))"`

**c. AI Scoring:**

```
Enable AI commit scoring? (uses Anthropic API, costs ~$0.001/commit)

1. No (default) - Raw metrics only
2. Yes - Score each commit's intellectual value with Claude Haiku
```

If Yes: ask for Anthropic API key, validate with curl.

## Step 5: Write Config and Confirm

Write `.coding-productivity.env` using the Write tool:

```
# coding-productivity configuration
# Created by /coding-productivity:setup
# NEVER commit this file to version control.

PLATFORM={platform}
GITHUB_TOKEN={github_token}
GITLAB_TOKEN={gitlab_token}
GITLAB_URL={gitlab_url}
GITLAB_CA_BUNDLE=

REPOS={repos_comma_separated}

STORAGE_BACKEND={storage_backend}
STORAGE_MODE=readwrite
DB_PATH=.coding-productivity/data.duckdb
GCP_PROJECT_ID={gcp_project_id}
BQ_DATASET={bq_dataset}
GOOGLE_APPLICATION_CREDENTIALS={google_creds_path}

SCORING_ENABLED={true_or_false}
ANTHROPIC_API_KEY={anthropic_key}

ANONYMIZATION_ENABLED={true_or_false}
PSEUDONYMIZATION_KEY={key_or_empty}

EXCLUDED_DEVELOPERS=
BOT_OVERRIDES=
IDENTITY_MERGES=
```

Leave keys blank when not applicable. Then run via Bash:

```bash
chmod 600 .coding-productivity.env
```

Add to `.gitignore` (create if needed):
```
.coding-productivity.env
.coding-productivity/
developer_mapping.json
```

Display summary (mask tokens) and warnings:

```
Saved to .coding-productivity.env

Platform:       {platform}
Token:          ****{last4}  [validated]
Repositories:   {count} selected
Storage:        {backend}
Anonymization:  {Enabled/Disabled}
AI Scoring:     {Enabled/Disabled}
```

If anonymization enabled:
> **Back up your pseudonymization key.** It is stored in `.coding-productivity.env` (the `PSEUDONYMIZATION_KEY` field). Losing it means existing hashes become permanently unresolvable. Copy this file to a safe location outside this project.

Display the path to the config file as a clickable reference.

Then:

```
Setup complete. What next?

1. Run /coding-productivity:extract - Start pulling commit data
2. Done for now
```

---

## Re-run Flow (Modify a Section)

When the user chose "Modify a section" in Step 1:

```
Which section would you like to modify?

1. Platform & Token
2. Repositories
3. Storage Backend
4. Anonymization
5. AI Scoring
6. Done - exit
```

For each section, re-run the corresponding step from above. Important edge cases:

- **Anonymization**: Check if data exists first (`count commits in storage`). If commits > 0, display as **locked**: "Cannot change while data exists. Delete .coding-productivity/data.duckdb and re-extract to change."
- **Storage backend switch**: Warn "Switching does not migrate data. You'll need to re-extract."
- **Repo removal**: "Removing a repo does not delete existing data. Analysis filters by active repos."

After each modification, save config, show updated summary, and return to section selection.
