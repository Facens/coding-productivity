---
title: Unicode characters in SKILL.md prevent slash command resolution in Claude Code
date: 2026-04-03
category: integration-issues
module: claude-code-plugins
problem_type: integration_issue
component: tooling
symptoms:
  - "Slash command /plugin-name:skill-name returns 'Unknown skill' despite SKILL.md existing"
  - "Skill appears in system skills list but cannot be invoked via slash command"
  - "Skill tool (programmatic invocation) works, only slash command resolution fails"
  - "/reload-plugins reports '0 skills' loaded"
root_cause: config_error
resolution_type: config_change
severity: high
tags:
  - claude-code
  - plugins
  - skill-md
  - unicode
  - slash-commands
  - frontmatter
---

# Unicode characters in SKILL.md prevent slash command resolution in Claude Code

## Problem

A Claude Code plugin skill with a valid SKILL.md file (correct frontmatter, correct directory structure) could not be invoked via its slash command (`/coding-productivity:run`). The error "Unknown skill" was returned despite the skill appearing in the system skills list and being invocable programmatically via the Skill tool.

## Symptoms

- `/coding-productivity:run` returned "Unknown skill"
- All other 8 skills in the same plugin worked as slash commands
- `/reload-plugins` reported "0 skills" in its output
- The skill appeared in the system reminder's skills list (loaded for context injection)
- `Skill("coding-productivity:run")` tool invocation worked correctly
- SKILL.md frontmatter was valid YAML with correct `name`, `description`, and `argument-hint` fields
- File permissions, encoding (UTF-8), and directory structure were identical to working skills

## What Didn't Work

- **Clearing old cached versions** — Removed stale 0.1.0-0.9.0 caches, kept only 0.10.0. Skill still not invocable.
- **Bumping plugin version** — Updated marketplace plugin.json from 0.1.0 to 0.10.0 (which was also stale). This was a real issue but didn't fix the slash command.
- **Checking for reserved words** — "run" is not a reserved Claude Code command name.
- **Hex dump comparison** — Compared byte-level content of working vs broken SKILL.md. Both had clean UTF-8 with no BOM or hidden characters in frontmatter.

## Solution

Replace all non-ASCII Unicode characters in the SKILL.md body with ASCII equivalents:

**Before:**
```markdown
Run extract -> score (if enabled) -> analyze -> report in sequence.

- `January 2026` -> `--since 2026-01-01 --until 2026-02-01`

Scoring: Skipped -- not enabled
```

The file contained 13 Unicode arrow characters (U+2192, `e2 86 92` in UTF-8) and em-dashes (U+2014, `e2 80 94`).

**After:**
```markdown
Run extract -> score (if enabled) -> analyze -> report in sequence.

- `January 2026` -> `--since 2026-01-01 --until 2026-02-01`

Scoring: Skipped -- not enabled
```

All `->` replaced with `->`, all `--` replaced with `--`.

After this change and a version bump + `/reload-plugins`, the slash command worked immediately.

## Why This Works

Claude Code's slash command router (distinct from the skill context loader) likely processes SKILL.md files through a parser that chokes on multi-byte UTF-8 sequences in the file body. The skill context loader is more tolerant — it successfully reads the file and injects it into the system prompt — but the slash command resolution path fails silently, registering the skill as "0 skills" in the reload count while still making it available for programmatic `Skill` tool invocation.

The frontmatter itself was pure ASCII (the Unicode was only in the markdown body), which explains why the YAML parsing succeeded and the skill appeared in the skills list. The slash command router apparently validates or hashes the entire file content, not just the frontmatter.

## Prevention

- **Use only ASCII in SKILL.md files.** Avoid Unicode arrows (`->`), em-dashes (`--`), curly quotes, or other non-ASCII characters anywhere in the file — not just frontmatter.
- **Test slash command invocation after creating new skills.** Don't assume that appearing in the system skills list means the slash command works.
- **If a skill loads for context but not for slash commands**, check for non-ASCII characters with: `grep -P '[\x80-\xFF]' skills/*/SKILL.md`

## Related Issues

- This may be related to Claude Code's `js-yaml` parser or the skill file hashing mechanism in the plugin loader.
- The `file` command classified the broken SKILL.md as "Unicode text" vs working ones as "Python script text executable, Unicode text" — the file-type classification difference may be a useful diagnostic signal.
