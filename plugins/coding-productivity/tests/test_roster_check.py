"""roster_check.py: exclusion leaks, new people, merge candidates; synthetic identities."""

from types import SimpleNamespace as NS

import pytest

import roster_check
from lib import anonymize

from .test_duplicate_work import commit

SINCE, UNTIL = "2026-10-01", "2026-11-01"
OLD = "2026-08-10T10:00:00Z"
IN = "2026-10-10T10:00:00Z"
KEY = b"k" * 32

_n = [0]


def c(name, email, date=IN, project="org/repo"):
    _n[0] += 1
    rec = commit(f"sha{_n[0]}", project=project, email=email, date=date)
    rec["author_name"] = name
    return rec


def cfg(**kw):
    base = dict(REPOS=[], EXCLUDED_DEVELOPERS=[], IDENTITY_MERGES={},
                IDENTITY_NO_MERGE=[], IDENTITY_AUTO_MERGE=True, DATE_FIELD="committed_date",
                ANONYMIZATION_ENABLED=False, PSEUDONYMIZATION_KEY=None, BOT_OVERRIDES=[])
    base.update(kw)
    return NS(**base)


def check(storage, commits, config=None, mapping=None):
    storage.insert_batch("commits", commits)
    config = config or cfg()
    rows = roster_check.load_rows(storage, config, SINCE, UNTIL)
    return roster_check.run_check(rows, config, SINCE, UNTIL, mapping)


def emails_of(people):
    return sorted(e for p in people for e in p)


def test_leak_detected_when_excluded_person_commits_from_new_email(duckdb_storage):
    report = check(duckdb_storage, [
        c("Alice Doe", "alice@example.com", OLD),
        c("Alice Doe", "alice@example.com", OLD),
        c("Alice Doe", "123+alice@users.noreply.github.example", IN),
        c("Bob Roe", "bob@example.com", OLD),
    ], cfg(EXCLUDED_DEVELOPERS=["alice@example.com"]))
    assert len(report.leaks) == 1
    leak = report.leaks[0]
    assert leak.covered == ["alice@example.com"]
    assert [(u[0], u[2], u[3]) for u in leak.uncovered] == [
        ("123+alice@users.noreply.github.example", 1, 1)]
    assert report.leak_lines[-1] == "123+alice@users.noreply.github.example"
    text = "\n".join(roster_check.render(report))
    assert "TO EXCLUDE" in text and "1 commits (1 in period)" in text
    # The leaking person is not also asked to be confirmed as new.
    assert report.new == []


def test_fully_excluded_person_is_no_finding(duckdb_storage):
    report = check(duckdb_storage, [
        c("Alice Doe", "alice@example.com", OLD),
        c("Alice Doe", "alice2@example.com", IN),
    ], cfg(EXCLUDED_DEVELOPERS=["alice@example.com", "alice2@example.com"]))
    assert not report.has_findings


def test_new_person_in_period(duckdb_storage):
    report = check(duckdb_storage, [
        c("Bob Roe", "bob@example.com", OLD),
        c("Carol Poe", "carol@example.com", IN),
        c("Carol Poe", "carol@example.com", IN),
    ])
    assert [p.label for p in report.new] == ["Carol Poe <carol@example.com>"]
    assert report.new[0].period == 2
    assert "EXCLUDED_DEVELOPERS" in "\n".join(roster_check.render(report))


def test_existing_person_not_new_even_through_older_second_email(duckdb_storage):
    # Same name: the person's first commit is the old one on the other email.
    report = check(duckdb_storage, [
        c("Bob Roe", "bob@old.example.com", OLD),
        c("Bob Roe", "bob@example.com", IN),
    ])
    assert report.new == []


def test_new_person_before_period_or_after_is_not_new(duckdb_storage):
    report = check(duckdb_storage, [
        c("Dan Moe", "dan@example.com", "2026-11-05T10:00:00Z"),
        c("Eve Poe", "eve@example.com", OLD),
    ])
    assert report.new == []


def test_excluded_person_and_bot_are_not_new(duckdb_storage):
    report = check(duckdb_storage, [
        c("Alice Doe", "alice@example.com", IN),
        c("Build Helper", "helper@example.com", IN),
    ], cfg(EXCLUDED_DEVELOPERS=["alice@example.com"], ))
    assert [p.label for p in report.new] == ["Build Helper <helper@example.com>"]
    from lib import bots
    bots.BOT_NAMES.add("Build Helper")
    try:
        report = check_again(duckdb_storage)
    finally:
        bots.BOT_NAMES.discard("Build Helper")
    assert report.new == []


def check_again(storage):
    config = cfg(EXCLUDED_DEVELOPERS=["alice@example.com"])
    return roster_check.run_check(
        roster_check.load_rows(storage, config, SINCE, UNTIL), config, SINCE, UNTIL)


def test_merge_candidate_suggested(duckdb_storage):
    # Same email local part on different domains, different names: not auto-merged.
    report = check(duckdb_storage, [
        c("Frank Foe", "frank@example.com", OLD),
        c("Frank Foe", "frank@example.com", OLD),
        c("F. Foe", "frank@example.org", OLD),
    ])
    assert len(report.candidates) == 1
    assert report.candidates[0].reason == "same_local_part"
    assert "IDENTITY_MERGES=frank@example.org:frank@example.com" in report.merge_lines
    assert any(l.startswith("IDENTITY_NO_MERGE=") for l in report.merge_lines)


def test_merge_candidate_with_excluded_side_is_kept_and_points_to_exclusion(duckdb_storage):
    report = check(duckdb_storage, [
        c("gina-handle", "gina@example.com", OLD),
        c("Gina Hoe", "gina@example.org", OLD),
    ], cfg(EXCLUDED_DEVELOPERS=["gina@example.com"]))
    assert len(report.candidates) == 1
    assert report.candidates[0].excluded_side["email"] == "gina@example.com"
    assert not any(l.startswith("IDENTITY_MERGES=") for l in report.merge_lines)
    assert "add gina@example.org to EXCLUDED_DEVELOPERS" in "\n".join(roster_check.render(report))


def test_pair_with_both_sides_excluded_is_dropped(duckdb_storage):
    report = check(duckdb_storage, [
        c("Hal Ire", "hal@example.com", OLD), c("H. Ire", "hal@example.org", OLD),
    ], cfg(EXCLUDED_DEVELOPERS=["hal@example.com", "hal@example.org"]))
    assert report.candidates == []


def test_already_merged_pair_not_suggested(duckdb_storage):
    commits = [c("Frank Foe", "frank@example.com", OLD), c("F. Foe", "frank@example.org", OLD)]
    report = check(duckdb_storage, commits,
                   cfg(IDENTITY_MERGES={"frank@example.org": "frank@example.com"}))
    assert report.candidates == []


def test_no_merge_pair_not_suggested(duckdb_storage):
    report = check(duckdb_storage, [
        c("Frank Foe", "frank@example.com", OLD), c("F. Foe", "frank@example.org", OLD),
    ], cfg(IDENTITY_NO_MERGE=[("frank@example.com", "frank@example.org")]))
    assert report.candidates == []


def test_auto_merged_same_name_pair_not_suggested(duckdb_storage):
    report = check(duckdb_storage, [
        c("Ivy Jay", "ivy@example.com", OLD), c("Ivy Jay", "ivy@example.org", OLD),
    ])
    assert report.candidates == []


def test_clean_dataset_has_no_findings(duckdb_storage):
    report = check(duckdb_storage, [
        c("Bob Roe", "bob@example.com", OLD), c("Bob Roe", "bob@example.com", IN),
        c("Zed Kay", "zed@example.com", OLD),
    ])
    assert not report.has_findings
    assert roster_check.render(report)[-1] == "No findings."


def test_repos_filter_applies(duckdb_storage):
    report = check(duckdb_storage, [
        c("Kim Lee", "kim@example.com", IN, project="org/other"),
    ], cfg(REPOS=["org/repo"]))
    assert not report.has_findings


def test_anonymized_with_mapping_shows_real_values_and_matches_hashes(duckdb_storage):
    h = lambda v: anonymize.pseudonymize(v, KEY)
    alice, noreply = h("alice@example.com"), h("alice@users.noreply.example")
    name = h("Alice Doe")
    mapping = {alice: {"name": "Alice Doe", "email": "alice@example.com"},
               noreply: {"name": "Alice Doe", "email": "alice@users.noreply.example"}}
    commits = [c(name, alice, OLD), c(name, noreply, IN)]
    config = cfg(ANONYMIZATION_ENABLED=True, PSEUDONYMIZATION_KEY=KEY,
                 EXCLUDED_DEVELOPERS=[alice])
    report = check(duckdb_storage, commits, config, mapping)
    assert len(report.leaks) == 1
    assert report.leaks[0].uncovered[0][1] == "Alice Doe <alice@users.noreply.example>"
    assert report.leak_lines[-1] == noreply          # stored value: what analyze compares
    assert "hashes" not in "\n".join(roster_check.render(report))


def test_anonymized_without_mapping_prints_hashes_and_hint(duckdb_storage):
    h = lambda v: anonymize.pseudonymize(v, KEY)
    config = cfg(ANONYMIZATION_ENABLED=True, PSEUDONYMIZATION_KEY=KEY)
    report = check(duckdb_storage, [c(h("Carol Poe"), h("carol@example.com"), IN)], config)
    text = "\n".join(roster_check.render(report))
    assert h("carol@example.com") in text and "/coding-productivity:developers" in text


# ── CLI ──────────────────────────────────────────────────────────────────────

def write_env(tmp_dir, db, excluded=""):
    path = tmp_dir / ".coding-productivity.env"
    path.write_text(f"STORAGE_BACKEND=duckdb\nDB_PATH={db}\nEXCLUDED_DEVELOPERS={excluded}\n")
    path.chmod(0o600)
    return path


def seed(tmp_dir, commits):
    from lib.storage import DuckDBStorage
    db = tmp_dir / "cli.duckdb"
    st = DuckDBStorage(str(db))
    st.create_tables()
    st.insert_batch("commits", commits)
    st.close()
    return db


def test_cli_exit_code_zero_with_findings_and_output_written(tmp_dir, capsys):
    db = seed(tmp_dir, [c("Alice Doe", "alice@example.com", OLD),
                        c("Alice Doe", "alice2@example.com", IN)])
    env = write_env(tmp_dir, db, "alice@example.com")
    out = tmp_dir / "report.txt"
    code = roster_check.main(["--config", str(env), "--since", SINCE, "--until", UNTIL,
                              "--output", str(out)])
    assert code == 0
    printed = capsys.readouterr().out
    assert "TO EXCLUDE" in printed and out.read_text() == printed


def test_cli_clean_writes_no_file_and_removes_stale_one(tmp_dir, capsys):
    db = seed(tmp_dir, [c("Bob Roe", "bob@example.com", OLD)])
    env = write_env(tmp_dir, db)
    out = tmp_dir / "report.txt"
    out.write_text("stale")
    code = roster_check.main(["--config", str(env), "--since", SINCE, "--until", UNTIL,
                              "--output", str(out)])
    assert code == 0 and not out.exists()
    assert "No findings." in capsys.readouterr().out


def test_cli_errors_are_nonzero(tmp_dir, capsys):
    with pytest.raises(SystemExit) as e:
        roster_check.main(["--config", str(tmp_dir / "missing.env"),
                           "--since", SINCE, "--until", UNTIL])
    assert e.value.code not in (0, None)
    env = write_env(tmp_dir, tmp_dir / "empty.duckdb")
    with pytest.raises(SystemExit) as e:
        roster_check.main(["--config", str(env), "--since", "2026-13-01", "--until", UNTIL])
    assert e.value.code not in (0, None)
