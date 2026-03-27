---
name: developers
description: 'Manage the developer roster — review detected bots, find and merge duplicate identities, exclude developers from analysis. Use after extraction to clean up identity data.'
---

# Developer Management

This skill provides an interactive workflow for cleaning up developer identity data after extraction. It detects bots, finds duplicate identities, and manages exclusions.

**Important:** Use `AskUserQuestion` for every interactive prompt. Never assume answers or skip steps.

## Locate the Project Root

The project root is the directory containing `.coding-productivity.env`. All paths below are relative to this root.

## Step 1: Load Config, Storage, and Mapping

Run via Bash:

```
python3.14 -c "
import json, sys
from pathlib import Path
from scripts.lib.config import Config
from scripts.lib.storage import Storage

cfg = Config()
st = Storage(cfg)

# Check for data
commit_count = st.count('commits')
if commit_count == 0:
    print('ERROR: No commit data found. Run /coding-productivity:extract first.')
    sys.exit(1)

anon_enabled = cfg.ANONYMIZATION_ENABLED
mapping_path = Path('.coding-productivity/developer_mapping.json')

# Load reverse mapping if anonymization is enabled
mapping = {}
if anon_enabled:
    if mapping_path.is_file():
        mapping = json.loads(mapping_path.read_text())
    else:
        print('WARNING: MAPPING_MISSING')

print(json.dumps({
    'anon_enabled': anon_enabled,
    'mapping_missing': anon_enabled and not mapping_path.is_file(),
    'commit_count': commit_count
}))
"
```

- If the output contains `MAPPING_MISSING`, warn the user:
  > The developer mapping file (`.coding-productivity/developer_mapping.json`) is missing. Without it, hashed identities cannot be resolved to real names. Would you like to re-extract data to regenerate it?
  >
  > 1. Yes, I will run /coding-productivity:extract
  > 2. No, continue with hashed identities

  If they choose to re-extract, exit the skill and tell them to run `/coding-productivity:extract`. If they continue, proceed but display hashes instead of names.

- If `commit_count` is 0, tell the user to run `/coding-productivity:extract` first and exit.

## Step 2: Query the Developer Roster

Run via Bash to get unique developer identities with commit counts:

```
python3.14 -c "
import json
from pathlib import Path
from scripts.lib.config import Config
from scripts.lib.storage import Storage

cfg = Config()
st = Storage(cfg)

rows = st.query('''
    SELECT author_email,
           author_name,
           COUNT(*) as commit_count
    FROM commits
    GROUP BY author_email, author_name
    ORDER BY commit_count DESC
''')

# Load config lists for status
excluded = [e.lower() for e in cfg.EXCLUDED_DEVELOPERS]
bot_overrides = cfg.BOT_OVERRIDES
merges = cfg.IDENTITY_MERGES

# Load mapping for deanonymization
anon = cfg.ANONYMIZATION_ENABLED
mapping = {}
if anon:
    mp = Path('.coding-productivity/developer_mapping.json')
    if mp.is_file():
        mapping = json.loads(mp.read_text())

print(json.dumps({
    'developers': rows,
    'excluded': excluded,
    'bot_overrides': bot_overrides,
    'merges': merges,
    'mapping': mapping,
    'anon_enabled': anon
}))
"
```

## Step 3: Build and Display the Roster Table

Using the data from Step 2, build a numbered table. For each developer:

1. **Resolve names**: If anonymization is enabled and a mapping exists, look up each `author_email` and `author_name` hash in the mapping to get the real name and email. If no mapping entry exists, display the hash as-is.
2. **Determine status**:
   - If the email (real or hashed) appears in `EXCLUDED_DEVELOPERS`: status = `Excluded`
   - If the name or email matches a bot (check against `bots.BOT_NAMES`, `bots.BOT_EMAILS`, or `BOT_OVERRIDES`): status = `Bot`
   - Otherwise: status = `Active`
3. **Group emails**: If the same resolved name appears with multiple emails, group them into one row showing all emails.

Display the roster as:

```
=== Developer Roster (N developers, M total commits) ===

 #  Name                  Email(s)                          Status     Commits
 1  Jane Smith            jane@work.com, jane@personal.com  Active         482
 2  dependabot[bot]       dependabot@github.com             Bot            201
 3  John Doe              john@company.com                  Excluded       156
 ...
```

## Step 4: Offer Actions

Ask via `AskUserQuestion`:

> What would you like to do?
>
> 1. Review bots — detect and confirm bot accounts
> 2. Find duplicates — detect and merge duplicate identities
> 3. Exclude developer — remove a developer from analysis
> 4. Include developer — re-include a previously excluded developer
> 5. Done — exit developer management

---

### Action 1: Review Bots

Run bot detection via Bash:

```
python3.14 -c "
import json
from pathlib import Path
from scripts.lib.config import Config
from scripts.lib.storage import Storage
from scripts.lib.bots import detect_bots, load_overrides

cfg = Config()
st = Storage(cfg)
load_overrides(cfg.BOT_OVERRIDES)

rows = st.query('''
    SELECT DISTINCT author_name as name, author_email as email
    FROM commits
''')

# Resolve hashes if anonymized
anon = cfg.ANONYMIZATION_ENABLED
mapping = {}
if anon:
    mp = Path('.coding-productivity/developer_mapping.json')
    if mp.is_file():
        mapping = json.loads(mp.read_text())
    resolved = []
    for r in rows:
        info = mapping.get(r['email'], mapping.get(r['name'], {}))
        resolved.append({
            'name': info.get('name', r['name']),
            'email': info.get('email', r['email']),
            'hash_name': r['name'],
            'hash_email': r['email']
        })
    rows = resolved

detected = detect_bots(rows)
print(json.dumps(detected))
"
```

Display the detected bots in a numbered list. For each detected bot, ask the user via `AskUserQuestion`:

> Detected bot: **{name}** ({email})
> Confirm as bot? (y/n/done)
>
> - **y** — Confirm: add to `BOT_OVERRIDES` in config
> - **n** — Override: this is a real developer, skip it
> - **done** — Stop reviewing, keep remaining as-is

For each confirmed bot, add the name to `BOT_OVERRIDES` via Bash:

```
python3.14 -c "
from scripts.lib.config import Config
cfg = Config()
existing = cfg.BOT_OVERRIDES
existing.append('BOT_NAME_HERE')
cfg.update('BOT_OVERRIDES', ','.join(existing))
print('Added to BOT_OVERRIDES')
"
```

After review is complete, return to Step 2 to refresh and redisplay the roster, then show the action menu again.

---

### Action 2: Find Duplicates

Run duplicate detection via Bash:

```
python3.14 -c "
import json
from pathlib import Path
from scripts.lib.config import Config
from scripts.lib.storage import Storage
from scripts.lib.dedup import find_duplicates

cfg = Config()
st = Storage(cfg)

rows = st.query('''
    SELECT author_name as name,
           author_email as email,
           COUNT(*) as commit_count
    FROM commits
    GROUP BY author_name, author_email
''')

# Resolve hashes if anonymized
anon = cfg.ANONYMIZATION_ENABLED
mapping = {}
if anon:
    mp = Path('.coding-productivity/developer_mapping.json')
    if mp.is_file():
        mapping = json.loads(mp.read_text())
    resolved = []
    for r in rows:
        info = mapping.get(r['email'], mapping.get(r['name'], {}))
        resolved.append({
            'name': info.get('name', r['name']),
            'email': info.get('email', r['email']),
            'commit_count': r['commit_count'],
            'hash_email': r['email']
        })
    rows = resolved

dupes = find_duplicates(rows)
print(json.dumps([(a, b, reason) for a, b, reason in dupes]))
"
```

If no duplicates found, inform the user and return to the action menu.

If duplicates are found, display each pair:

```
Duplicate pair #1 (reason: exact_name_match):
  A: Jane Smith <jane@work.com>       (142 commits)
  B: Jane Smith <jane@personal.com>   (38 commits)
```

For each pair, ask via `AskUserQuestion`:

> Merge these identities?
>
> 1. Yes, keep **A** as canonical ({email_a})
> 2. Yes, keep **B** as canonical ({email_b})
> 3. No, these are different people
> 4. Done reviewing duplicates

For each confirmed merge, run via Bash:

```
python3.14 -c "
from pathlib import Path
from scripts.lib.config import Config
from scripts.lib.storage import Storage
from scripts.lib.dedup import merge_identities, apply_retroactive_merges
from scripts.lib.anonymize import load_key

cfg = Config()
st = Storage(cfg)

canonical_email = 'CANONICAL_EMAIL'
alias_emails = ['ALIAS_EMAIL']

merge_identities(canonical_email, alias_emails, cfg)

if cfg.ANONYMIZATION_ENABLED and cfg.PSEUDONYMIZATION_KEY:
    key = load_key(cfg.PSEUDONYMIZATION_KEY.decode('utf-8'))
    mapping_path = Path('.coding-productivity/developer_mapping.json')
    affected = apply_retroactive_merges(st, mapping_path, key, {'ALIAS_EMAIL': 'CANONICAL_EMAIL'})
    print(f'Retroactive merge applied: {affected} rows updated')
else:
    print('Merge registered (no retroactive hash rewrite needed — anonymization disabled)')
"
```

After all pairs are reviewed, return to Step 2 to refresh and redisplay the roster, then show the action menu again.

---

### Action 3: Exclude Developer

Display the current roster (Active developers only) as a numbered list. Ask via `AskUserQuestion`:

> Which developer should be excluded from analysis? (enter number)

After selection, add the developer's email to `EXCLUDED_DEVELOPERS` via Bash:

```
python3.14 -c "
from scripts.lib.config import Config
cfg = Config()
existing = cfg.EXCLUDED_DEVELOPERS
existing.append('DEVELOPER_EMAIL_HERE')
cfg.update('EXCLUDED_DEVELOPERS', ','.join(existing))
print('Developer excluded')
"
```

Use the hashed email if anonymization is enabled, or the real email if not.

After the change, return to Step 2 to refresh and redisplay the roster, then show the action menu again.

---

### Action 4: Include Developer

Run via Bash to get the current excluded list:

```
python3.14 -c "
import json
from scripts.lib.config import Config
cfg = Config()
print(json.dumps(cfg.EXCLUDED_DEVELOPERS))
"
```

If the excluded list is empty, inform the user and return to the action menu.

Otherwise, display the excluded developers as a numbered list (resolve hashes to names via the mapping if anonymization is enabled). Ask via `AskUserQuestion`:

> Which developer should be re-included? (enter number)

After selection, remove the email from `EXCLUDED_DEVELOPERS` via Bash:

```
python3.14 -c "
from scripts.lib.config import Config
cfg = Config()
excluded = cfg.EXCLUDED_DEVELOPERS
excluded.remove('DEVELOPER_EMAIL_HERE')
cfg.update('EXCLUDED_DEVELOPERS', ','.join(excluded))
print('Developer re-included')
"
```

After the change, return to Step 2 to refresh and redisplay the roster, then show the action menu again.

---

### Action 5: Done

Display a summary of changes made during this session (how many bots confirmed, identities merged, developers excluded/included). Then exit.

## Persistence

All changes are persisted to `.coding-productivity.env` via `config.update()`. The keys modified by this skill are:

- `BOT_OVERRIDES` — comma-separated list of confirmed bot names
- `IDENTITY_MERGES` — comma-separated `alias:canonical` email pairs
- `EXCLUDED_DEVELOPERS` — comma-separated list of excluded developer emails

No manual file editing is needed. The `config.update()` method handles atomic writes.
