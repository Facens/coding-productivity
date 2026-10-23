"""Automatic identity resolution: lib/identity.py and its use in analyze.py."""

import sys
from types import SimpleNamespace as NS

import pytest

import analyze
from lib import anonymize, identity
from lib.config import Config
from lib.storage import DuckDBStorage

from .test_duplicate_work import commit
from .test_dedupe_analyze import score

KEY = b"k" * 32
JAN = ("2026-01-01", "2026-02-01")
FEB = ("2026-02-01", "2026-03-01")


def cfg(**kw):
    base = dict(REPOS=[], EXCLUDED_DEVELOPERS=[], IDENTITY_MERGES={},
                IDENTITY_NO_MERGE=[], IDENTITY_AUTO_MERGE=True, MIN_ACTIVE_COMMITS=1,
                ANONYMIZATION_ENABLED=False, PSEUDONYMIZATION_KEY=None)
    base.update(kw)
    return NS(**base)


def pairs(*rows):
    """(name, email) or (name, email, n) rows to resolver input."""
    return [(r[0], r[1], r[2] if len(r) > 2 else 1) for r in rows]


def groups(people):
    return sorted(sorted(p.emails) for p in people.persons)


# ── resolver ─────────────────────────────────────────────────────────────────

def test_same_name_two_emails_is_one_person():
    people = identity.resolve(pairs(("Anna Rossi", "a@x.com"), ("Anna Rossi", "anna@y.com"),
                                    ("Bruno", "b@x.com")))
    assert groups(people) == [["a@x.com", "anna@y.com"], ["b@x.com"]]
    assert people.key("a@x.com") == people.key("anna@y.com")
    assert [p.emails for p in people.auto_merged()] == [["a@x.com", "anna@y.com"]]


def test_name_match_ignores_case_and_padding():
    people = identity.resolve(pairs(("Anna Rossi", "a@x.com"), (" anna rossi ", "b@y.com")))
    assert len(people.persons) == 1


def test_one_email_two_names_is_one_person_and_chains():
    people = identity.resolve(pairs(("Anna", "a@x.com"), ("A. Rossi", "a@x.com"),
                                    ("A. Rossi", "a2@x.com"), ("Zed", "z@x.com")))
    assert groups(people) == [["a2@x.com", "a@x.com"], ["z@x.com"]]


def test_distinct_names_and_emails_stay_apart():
    people = identity.resolve(pairs(("Anna", "a@x.com"), ("Bruno", "b@x.com")))
    assert len(people.persons) == 2 and people.auto_merged() == []


def test_the_main_identity_is_the_one_with_most_commits():
    people = identity.resolve(pairs(("Anna", "old@x.com", 3), ("Anna", "new@x.com", 9)))
    assert people.key("old@x.com") == "new@x.com"
    assert people.persons[0].commits == 12


def test_generic_names_never_link_raw():
    # Two unrelated accounts both called "Admin" / "root" must not become one person.
    people = identity.resolve(pairs(("Admin", "a@x.com"), ("Admin", "b@y.com"),
                                    ("root", "r1@x.com"), ("Root", "r2@y.com")))
    assert len(people.persons) == 4
    assert all(p.non_person for p in people.persons)


def test_generic_names_never_link_hashed():
    def h(v):
        return anonymize.pseudonymize(v, KEY)
    people = identity.resolve(
        pairs((h("Root"), h("r1@x.com")), (h("Root"), h("r2@y.com")),
              (h("No-Reply"), h("n@x.com")), (h("no reply"), h("n2@x.com")),
              (h("Anna"), h("a@x.com")), (h("Anna"), h("a2@y.com"))),
        key=KEY)
    non = [p for p in people.persons if p.non_person]
    assert len(non) == 4                       # nothing merged, all excluded
    assert len(people.persons) == 5            # Anna's two emails are one person


def test_hashed_generics_are_unknown_without_the_key():
    h = anonymize.pseudonymize("Root", KEY)
    people = identity.resolve(pairs((h, "r1"), (h, "r2")))
    assert len(people.persons) == 1 and not people.persons[0].non_person


def test_generic_name_on_a_real_persons_email_does_not_exclude_it():
    people = identity.resolve(pairs(("root", "a@x.com", 1), ("Anna", "a@x.com", 5)))
    assert len(people.persons) == 1 and not people.persons[0].non_person
    assert people.non_person_emails() == []


def test_non_person_emails_listed_for_exclusion():
    people = identity.resolve(pairs(("Root", "root@x.com"), ("Anna", "a@x.com")))
    assert people.non_person_emails() == ["root@x.com"]


def test_hashed_names_and_emails_resolve_like_raw_ones():
    h = lambda v: anonymize.pseudonymize(v, KEY)  # noqa: E731
    people = identity.resolve(pairs((h("Anna Rossi"), h("a@x.com")),
                                    (h("anna rossi"), h("anna@y.com"))), key=KEY)
    assert len(people.persons) == 1


def test_auto_merge_off_keeps_emails_apart():
    people = identity.resolve(pairs(("Anna", "a@x.com"), ("Anna", "b@y.com")), auto=False)
    assert len(people.persons) == 2


def test_explicit_merges_apply_even_when_auto_is_off():
    people = identity.resolve(pairs(("Anna", "a@x.com"), ("Anna K", "b@y.com")),
                              merges={"B@Y.com": "a@x.com"}, auto=False)
    assert len(people.persons) == 1 and people.auto_merged() == []  # not an automatic merge


def test_explicit_merge_of_hashed_emails_from_raw_config():
    h = lambda v: anonymize.pseudonymize(v, KEY)  # noqa: E731
    people = identity.resolve(pairs(("n1", h("a@x.com")), ("n2", h("b@y.com"))),
                              merges={"b@y.com": "a@x.com"}, key=KEY)
    assert len(people.persons) == 1


def test_explicit_merge_with_an_email_not_in_the_data_is_ignored():
    people = identity.resolve(pairs(("Anna", "a@x.com")), merges={"ghost@x.com": "a@x.com"})
    assert len(people.persons) == 1


def test_no_merge_blocks_the_join_raw_and_hashed():
    rows = pairs(("John Smith", "j1@x.com"), ("John Smith", "j2@y.com"))
    people = identity.resolve(rows, no_merge=[("j1@x.com", "J2@y.com")])
    assert len(people.persons) == 2
    h = lambda v: anonymize.pseudonymize(v, KEY)  # noqa: E731
    hashed = pairs(("John Smith", h("j1@x.com")), ("John Smith", h("j2@y.com")))
    assert len(identity.resolve(hashed, no_merge=[("j1@x.com", "j2@y.com")], key=KEY).persons) == 2
    # The stored values themselves work too.
    assert len(identity.resolve(hashed, no_merge=[(h("j1@x.com"), h("j2@y.com"))]).persons) == 2


def test_no_merge_holds_across_a_chain_and_leaves_other_links():
    rows = pairs(("A", "e1"), ("A", "e2"), ("A", "e3"), ("B", "e2"))
    people = identity.resolve(rows, no_merge=[("e1", "e3")])
    # e1 and e3 never end up together; the third email goes with whoever links first.
    assert people.key("e1") != people.key("e3")


def test_no_merge_does_not_block_an_explicit_merge():
    rows = pairs(("A", "e1"), ("B", "e2"))
    people = identity.resolve(rows, merges={"e2": "e1"}, no_merge=[("e1", "e2")])
    assert len(people.persons) == 1


def test_emails_that_differ_by_case_are_one_node():
    people = identity.resolve(pairs(("Anna", "A@x.com"), ("Bea", "a@x.com")))
    assert len(people.persons) == 1
    assert people.key("A@x.com") == people.key("a@x.com")


def test_rows_without_an_email_are_ignored():
    people = identity.resolve(pairs(("Anna", None), ("Anna", ""), ("Bob", "b@x.com")))
    assert len(people.persons) == 1 and people.key(None) is None


def test_result_does_not_depend_on_row_order():
    rows = pairs(("A", "e1"), ("A", "e2"), ("B", "e2"), ("B", "e3"), ("C", "e9"))
    forward = groups(identity.resolve(rows))
    assert forward == groups(identity.resolve(list(reversed(rows))))


def test_report_lists_merges_and_how_to_undo(tmp_dir):
    people = identity.resolve(pairs(("Anna Rossi", "a@x.com", 4), ("Anna Rossi", "b@y.com", 2),
                                    ("Root", "root@x.com", 7)))
    text = "\n".join(identity.format_report(people))
    assert "1 automatic merge(s)" in text
    assert "Anna Rossi <a@x.com> (4 commits)" in text and "b@y.com" in text
    assert "IDENTITY_NO_MERGE=a@x.com:b@y.com" in text
    assert "Root <root@x.com> (7 commits)" in text and "non-person" in text
    assert identity.format_report(identity.resolve(pairs(("A", "a@x.com")))) == []


def test_report_undo_example_pairs_two_emails_that_really_differ():
    # The second most-used email differs from the first only by case: a no-op pair.
    people = identity.resolve(pairs(("Anna", "Anna@x.com", 5), ("Anna", "anna@x.com", 3),
                                    ("Anna", "b@y.com", 2)))
    text = "\n".join(identity.format_report(people))
    assert "IDENTITY_NO_MERGE=Anna@x.com:b@y.com" in text
    assert "Anna@x.com:anna@x.com" not in text


def test_report_under_anonymization_points_to_the_developers_skill():
    people = identity.resolve(pairs(("A", "a@x.com", 2), ("A", "b@y.com", 1)))
    text = "\n".join(identity.format_report(people, anonymized=True))
    assert "pseudonyms" in text and "/coding-productivity:developers" in text


# ── config ───────────────────────────────────────────────────────────────────

def _config(tmp_dir, body):
    path = tmp_dir / ".coding-productivity.env"
    path.write_text(body)
    path.chmod(0o600)
    return Config(path)


def test_config_defaults(tmp_dir):
    c = _config(tmp_dir, "PLATFORM=github\n")
    assert c.IDENTITY_AUTO_MERGE is True
    assert c.IDENTITY_NO_MERGE == []
    assert c.MIN_ACTIVE_COMMITS == 1


def test_config_parses_the_new_keys(tmp_dir):
    c = _config(tmp_dir, "IDENTITY_AUTO_MERGE=false\n"
                         "IDENTITY_NO_MERGE=a@x.com:b@y.com, a@x.com:c@z.com\n"
                         "MIN_ACTIVE_COMMITS=3\n")
    assert c.IDENTITY_AUTO_MERGE is False
    assert c.IDENTITY_NO_MERGE == [("a@x.com", "b@y.com"), ("a@x.com", "c@z.com")]
    assert c.MIN_ACTIVE_COMMITS == 3


@pytest.mark.parametrize("body, fragment", [
    ("MIN_ACTIVE_COMMITS=0\n", "MIN_ACTIVE_COMMITS"),
    ("MIN_ACTIVE_COMMITS=many\n", "MIN_ACTIVE_COMMITS"),
    ("IDENTITY_NO_MERGE=a@x.com\n", "IDENTITY_NO_MERGE"),
])
def test_config_validation_rejects_bad_values(tmp_dir, body, fragment):
    errors = _config(tmp_dir, body).validate()
    assert any(fragment in e for e in errors)


# ── analyze ──────────────────────────────────────────────────────────────────

def person_commit(sha, name, email, date="2026-01-10T10:00:00Z", adds=10):
    row = commit(sha, email=email, date=date, adds=adds)
    row["author_name"] = name
    return row


@pytest.fixture
def team(duckdb_storage):
    """Anna (two emails, same name), Bruno (one email, two names), Carla, a Root
    account, and two different people who are both called John Smith."""
    st = duckdb_storage
    rows = [
        person_commit("a1", "Anna Rossi", "anna@a.com"),
        person_commit("a2", "Anna Rossi", "anna@b.com"),
        person_commit("b1", "Bruno", "bruno@a.com"),
        person_commit("b2", "B. Verdi", "bruno@a.com"),
        person_commit("c1", "Carla", "carla@a.com"),
        person_commit("r1", "Root", "root@a.com", adds=500),
        person_commit("j1", "John Smith", "js1@a.com"),
        person_commit("j2", "John Smith", "js2@b.com"),
        person_commit("a3", "Anna Rossi", "anna@a.com", date="2026-02-05T10:00:00Z"),
    ]
    st.insert_batch("commits", rows)
    st.insert_batch("commit_scores", [
        score(r["commit_sha"], float(r["additions"]), 0.5) for r in rows])
    return st


def test_monthly_trends_counts_people_not_emails(team):
    jan, feb = analyze.monthly_trends(team, cfg(), "2026-01-01", "2026-03-01")
    # Anna(2 emails)=1, Bruno=1, Carla=1, John Smith (2 emails, same name)=1; Root excluded.
    assert jan["unique_authors"] == 4
    assert feb["unique_authors"] == 1
    assert jan["commits"] == 7            # r1 is excluded like a bot
    assert "active_developers" not in jan  # default: columns unchanged


def test_without_auto_merge_only_explicit_merges_and_non_persons_apply(team):
    jan = analyze.monthly_trends(team, cfg(IDENTITY_AUTO_MERGE=False), *JAN)[0]
    # anna@a.com, anna@b.com, bruno, carla, js1, js2 = 6 emails; Root excluded.
    assert jan["unique_authors"] == 6
    merged = analyze.monthly_trends(
        team, cfg(IDENTITY_AUTO_MERGE=False, IDENTITY_MERGES={"anna@b.com": "anna@a.com"}), *JAN)[0]
    assert merged["unique_authors"] == 5


def test_no_merge_keeps_two_john_smiths_apart(team):
    jan = analyze.monthly_trends(
        team, cfg(IDENTITY_NO_MERGE=[("js1@a.com", "js2@b.com")]), *JAN)[0]
    assert jan["unique_authors"] == 5


def test_period_comparison_counts_people(team):
    rows = analyze.period_comparison(team, cfg(), *JAN, *FEB)
    assert rows[0]["unique_authors"] == 4 and rows[1]["unique_authors"] == 1
    assert rows[2]["unique_authors"] == -75.0


class _Reversed:
    """Storage wrapper returning the rows of every multi-row query in reverse."""

    def __init__(self, inner):
        self._inner = inner

    def query(self, sql, params=None):
        return list(reversed(self._inner.query(sql, params)))

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_period_comparison_does_not_depend_on_row_order(team):
    rows = analyze.period_comparison(_Reversed(team), cfg(), *JAN, *FEB)
    assert [r["period"] for r in rows] == ["baseline", "comparison", "% change"]
    assert rows[0]["unique_authors"] == 4 and rows[1]["unique_authors"] == 1
    assert rows[0]["commits"] == 7 and rows[1]["commits"] == 1
    assert rows[2]["unique_authors"] == -75.0 and rows[2]["commits"] == -85.7


def test_author_table_has_one_row_per_person_and_no_root(team):
    rows = analyze.author_productivity(team, cfg(), "2026-01-01", "2026-03-01")
    by_commits = {r["author_name"]: r for r in rows}
    assert len(rows) == 4                                   # Root is not a row
    assert "Root" not in by_commits
    anna = by_commits["Anna Rossi"]
    assert anna["commits"] == 3 and anna["author_email"] == "anna@a.com"  # most commits
    assert anna["total_productivity"] == 30 and anna["avg_weighted_score"] == 0.5
    assert anna["top_category"] == "feature"
    assert by_commits["John Smith"]["commits"] == 2


def test_author_table_limit_and_order_apply_to_people(team):
    rows = analyze.author_productivity(team, cfg(), "2026-01-01", "2026-03-01", limit=2)
    assert [r["commits"] for r in rows] == [3, 2]


def test_author_table_without_scores_sums_lines_per_person(duckdb_storage):
    st = duckdb_storage
    st.insert_batch("commits", [person_commit("x1", "Anna", "a@a.com", adds=5),
                                person_commit("x2", "Anna", "b@b.com", adds=7)])
    rows = analyze.author_productivity(st, cfg(), *JAN)
    assert len(rows) == 1
    assert rows[0]["commits"] == 2 and rows[0]["total_lines_changed"] == 12
    assert rows[0]["author_name"] == "Anna"


def test_commits_without_an_email_still_count_as_commits(duckdb_storage):
    st = duckdb_storage
    row = person_commit("n1", "Ghost", "x@x.com")
    row["author_email"] = None
    st.insert_batch("commits", [row, person_commit("n2", "Anna", "a@a.com")])
    jan = analyze.monthly_trends(st, cfg(), *JAN)[0]
    assert jan["commits"] == 2 and jan["unique_authors"] == 1


def test_excluded_developers_stay_out_of_identity_resolution(team):
    jan = analyze.monthly_trends(team, cfg(EXCLUDED_DEVELOPERS=["carla@a.com"]), *JAN)[0]
    assert jan["unique_authors"] == 3 and jan["commits"] == 6


def test_null_email_commit_survives_the_non_person_exclusion(team):
    # The team fixture has a Root account, so the exclusion clause is active.
    row = person_commit("n1", "Ghost", "x@x.com")
    row["author_email"] = None
    team.insert_batch("commits", [row])
    jan = analyze.monthly_trends(team, cfg(), *JAN)[0]
    assert jan["commits"] == 8 and jan["unique_authors"] == 4


def test_null_email_commit_survives_excluded_developers(team):
    row = person_commit("n1", "Ghost", "x@x.com")
    row["author_email"] = None
    team.insert_batch("commits", [row])
    jan = analyze.monthly_trends(team, cfg(EXCLUDED_DEVELOPERS=["carla@a.com"]), *JAN)[0]
    assert jan["commits"] == 7 and jan["unique_authors"] == 3


def test_non_persons_are_excluded_from_every_query(team):
    cats = analyze.category_distribution(team, cfg(), *JAN)
    assert cats[0]["commit_count"] == 7
    eff = analyze.code_efficiency(team, cfg(), *JAN)
    assert eff[0]["total_additions"] == 70                    # r1's 500 lines are out


def test_hashed_dataset_end_to_end(duckdb_storage):
    h = lambda v: anonymize.pseudonymize(v, KEY)  # noqa: E731
    st = duckdb_storage
    st.insert_batch("commits", [
        person_commit("h1", h("Anna Rossi"), h("anna@a.com")),
        person_commit("h2", h("Anna Rossi"), h("anna@b.com")),
        person_commit("h3", h("Root"), h("root@a.com")),
        person_commit("h4", h("Bruno"), h("bruno@a.com")),
    ])
    config = cfg(ANONYMIZATION_ENABLED=True, PSEUDONYMIZATION_KEY=KEY)
    jan = analyze.monthly_trends(st, config, *JAN)[0]
    assert jan["unique_authors"] == 2 and jan["commits"] == 3


# ── MIN_ACTIVE_COMMITS ───────────────────────────────────────────────────────

@pytest.fixture
def busy(duckdb_storage):
    """January: Anna 3 commits (two emails), Bruno 1 commit, Carla 2 commits."""
    st = duckdb_storage
    rows = [person_commit("m1", "Anna", "a@a.com", adds=10),
            person_commit("m2", "Anna", "a@b.com", adds=20),
            person_commit("m3", "Anna", "a@a.com", adds=30),
            person_commit("m4", "Bruno", "b@a.com", adds=100),
            person_commit("m5", "Carla", "c@a.com", adds=10),
            person_commit("m6", "Carla", "c@a.com", adds=10)]
    st.insert_batch("commits", rows)
    st.insert_batch("commit_scores", [score(r["commit_sha"], float(r["additions"]), 0.5)
                                      for r in rows])
    return st


def test_min_active_commits_one_leaves_the_default_columns(busy):
    jan = analyze.monthly_trends(busy, cfg(), *JAN)[0]
    assert set(jan) == {"month", "commits", "unique_authors", "total_productivity",
                        "avg_weighted_score"}
    assert jan["unique_authors"] == 3


def test_min_active_commits_adds_the_two_columns(busy):
    jan = analyze.monthly_trends(busy, cfg(MIN_ACTIVE_COMMITS=2), *JAN)[0]
    assert jan["unique_authors"] == 3                       # the headline count is unchanged
    assert jan["active_developers"] == 2                    # Anna (3, over two emails), Carla (2)
    assert jan["productivity_per_active_developer"] == (60 + 20) / 2   # Bruno's 100 is out
    assert jan["total_productivity"] == 180


def test_min_active_commits_is_counted_per_month_and_per_person(busy):
    busy.insert_batch("commits", [person_commit("m7", "Carla", "c@a.com",
                                                date="2026-02-03T10:00:00Z")])
    rows = analyze.monthly_trends(busy, cfg(MIN_ACTIVE_COMMITS=2), "2026-01-01", "2026-03-01")
    assert rows[1]["unique_authors"] == 1 and rows[1]["active_developers"] == 0
    assert rows[1]["productivity_per_active_developer"] is None


def test_min_active_commits_without_scores_uses_lines(duckdb_storage):
    st = duckdb_storage
    st.insert_batch("commits", [person_commit("l1", "A", "a@a.com", adds=10),
                                person_commit("l2", "A", "a@a.com", adds=30),
                                person_commit("l3", "B", "b@a.com", adds=99)])
    jan = analyze.monthly_trends(st, cfg(MIN_ACTIVE_COMMITS=2), *JAN)[0]
    assert jan["active_developers"] == 1 and jan["lines_per_active_developer"] == 40


# ── CLI report ───────────────────────────────────────────────────────────────

def _cli_config(tmp_dir, storage, extra=""):
    path = tmp_dir / ".coding-productivity.env"
    path.write_text(f"STORAGE_BACKEND=duckdb\nDB_PATH={storage._db_path}\n{extra}")
    path.chmod(0o600)
    return path


def test_cli_reports_merges_and_non_persons(team, tmp_dir, capsys, monkeypatch):
    team.close()
    path = _cli_config(tmp_dir, team)
    monkeypatch.setattr(sys, "argv", ["analyze", "--config", str(path), "--query",
                                      "monthly_trends", "--since", JAN[0], "--until", JAN[1]])
    analyze.main()
    out = capsys.readouterr().out
    assert "2 automatic merge(s)" in out           # Anna and John Smith
    assert "IDENTITY_NO_MERGE=" in out
    assert "1 non-person account(s) excluded" in out and "Root" in out


def test_cli_report_goes_to_stderr_for_json(team, tmp_dir, capsys, monkeypatch):
    team.close()
    path = _cli_config(tmp_dir, team)
    monkeypatch.setattr(sys, "argv", ["analyze", "--config", str(path), "--query",
                                      "monthly_trends", "--since", JAN[0], "--until", JAN[1],
                                      "--format", "json"])
    analyze.main()
    out = capsys.readouterr()
    assert "automatic merge" in out.err
    assert out.out.lstrip().startswith("[")


def test_cli_without_auto_merge_reports_no_merges(team, tmp_dir, capsys, monkeypatch):
    team.close()
    path = _cli_config(tmp_dir, team, "IDENTITY_AUTO_MERGE=false\n")
    monkeypatch.setattr(sys, "argv", ["analyze", "--config", str(path), "--query",
                                      "all", "--since", JAN[0], "--until", JAN[1]])
    analyze.main()
    out = capsys.readouterr().out
    assert "automatic merge" not in out and "non-person" in out
