"""DATE_FIELD: which commit date buckets and filters the analysis."""

from types import SimpleNamespace as NS

import pytest

import analyze
from lib.config import Config

from .test_duplicate_work import commit


def cfg(field=None):
    return NS(REPOS=[], EXCLUDED_DEVELOPERS=[], DATE_FIELD=field, MIN_ACTIVE_COMMITS=1)


def rebased(sha, authored, committed, email="dev@example.com"):
    """A commit written on one date and committed (rebased, cherry-picked) later."""
    row = commit(sha, email=email, date=authored)
    row["committed_date"] = committed
    return row


@pytest.fixture
def store(duckdb_storage):
    duckdb_storage.insert_batch("commits", [
        # authored in January, committed in February
        rebased("a1", "2026-01-31T23:00:00Z", "2026-02-02T09:00:00Z"),
        # same month both ways
        rebased("a2", "2026-02-10T10:00:00Z", "2026-02-10T10:00:00Z"),
    ])
    return duckdb_storage


def months(rows):
    return {str(r["month"])[:7]: r["commits"] for r in rows}


def test_committed_date_is_the_default(store):
    rows = analyze.monthly_trends(store, cfg(), "2026-01-01", "2026-03-01")
    assert months(rows) == {"2026-02": 2}


def test_authored_date_can_be_selected(store):
    rows = analyze.monthly_trends(store, cfg("authored_date"), "2026-01-01", "2026-03-01")
    assert months(rows) == {"2026-01": 1, "2026-02": 1}


def test_since_until_filter_uses_the_same_field(store):
    feb = ("2026-02-01", "2026-03-01")
    assert months(analyze.monthly_trends(store, cfg(), *feb)) == {"2026-02": 2}
    assert months(analyze.monthly_trends(store, cfg("authored_date"), *feb)) == {"2026-02": 1}
    jan = ("2026-01-01", "2026-02-01")
    assert analyze.monthly_trends(store, cfg(), *jan) == []
    assert months(analyze.monthly_trends(store, cfg("authored_date"), *jan)) == {"2026-01": 1}


def test_people_counts_follow_the_field(store):
    rows = analyze.monthly_trends(store, cfg("authored_date"), "2026-01-01", "2026-02-01")
    assert rows[0]["unique_authors"] == 1
    assert analyze.author_productivity(store, cfg(), "2026-01-01", "2026-02-01") == []
    assert analyze.author_productivity(
        store, cfg("authored_date"), "2026-01-01", "2026-02-01")[0]["commits"] == 1


def test_period_comparison_and_efficiency_follow_the_field(store):
    def periods(c):
        rows = analyze.period_comparison(store, c, "2026-01-01", "2026-02-01",
                                         "2026-02-01", "2026-03-01")
        return rows[0]["commits"], rows[1]["commits"]
    assert periods(cfg()) == (0, 2)
    assert periods(cfg("authored_date")) == (1, 1)
    eff = analyze.code_efficiency(store, cfg(), "2026-01-01", "2026-03-01")
    assert [str(r["month"])[:7] for r in eff] == ["2026-02"]


def test_commit_without_committed_date_falls_back_to_authored(duckdb_storage):
    row = rebased("n1", "2026-03-05T10:00:00Z", None)
    duckdb_storage.insert_batch("commits", [row])
    rows = analyze.monthly_trends(duckdb_storage, cfg(), "2026-03-01", "2026-04-01")
    assert months(rows) == {"2026-03": 1}


def _config(tmp_dir, body):
    path = tmp_dir / ".coding-productivity.env"
    path.write_text(body)
    path.chmod(0o600)
    return Config(path)


def test_config_default_and_values(tmp_dir):
    assert _config(tmp_dir, "PLATFORM=github\n").DATE_FIELD == "committed_date"
    assert _config(tmp_dir, "DATE_FIELD= Authored_Date \n").DATE_FIELD == "authored_date"


def test_config_rejects_unknown_date_field(tmp_dir):
    errors = _config(tmp_dir, "DATE_FIELD=created_at\n").validate()
    assert any("DATE_FIELD" in e for e in errors)
    assert not any("DATE_FIELD" in e for e in _config(tmp_dir, "DATE_FIELD=authored_date\n").validate())
