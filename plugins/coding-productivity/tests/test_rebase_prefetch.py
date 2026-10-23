"""Rebase reads commits and diffs in bulk, not one query per commit."""

import rebase


def test_prefetch_returns_each_commit_with_its_diffs(duckdb_storage, monkeypatch):
    monkeypatch.setattr(rebase, "FETCH_CHUNK", 2)  # several chunks
    duckdb_storage.insert_batch("commits", [
        {"commit_sha": s, "title": f"t-{s}", "author_email": "a@x"} for s in ("c1", "c2", "c3")
    ])
    duckdb_storage.insert_batch("diffs", [
        {"commit_sha": "c1", "file_path": "a.py", "diff": "+a", "additions": 1, "deletions": 0},
        {"commit_sha": "c1", "file_path": "b.py", "diff": "+b", "additions": 1, "deletions": 0},
        {"commit_sha": "c3", "file_path": "c.py", "diff": "+c", "additions": 1, "deletions": 0},
    ])
    data = rebase._prefetch(duckdb_storage, ["c1", "c2", "c3", "c1", "missing"])
    assert data["c1"][0]["title"] == "t-c1"
    assert sorted(d["file_path"] for d in data["c1"][1]) == ["a.py", "b.py"]
    assert data["c2"][1] == []
    assert [d["file_path"] for d in data["c3"][1]] == ["c.py"]
    assert data["missing"] == ({"commit_sha": "missing"}, [])


def test_prefetch_issues_two_queries_per_chunk(monkeypatch):
    calls = []

    class Fake:
        dialect = "duckdb"

        def query(self, sql, params=None):
            calls.append(sql)
            return []

    monkeypatch.setattr(rebase, "FETCH_CHUNK", 250)
    rebase._prefetch(Fake(), [f"s{i}" for i in range(600)])
    assert len(calls) == 6  # 3 chunks x (commits, diffs)
