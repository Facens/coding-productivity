"""rebase: batch chunking, result classification and an end-to-end run on fakes."""

import json
from types import SimpleNamespace as NS

import pytest

import rebase
from lib import scoring, scoring_meta
from .helpers import FakeClient, make_response

FILES_A = [{"path": "src/auth.py", "score": 0.5, "category": "feature", "reasoning": "r"}]
FILES_B = [{"path": "src/login.py", "score": 0.4, "category": "bugfix", "reasoning": "r"}]


# ── Chunking by bytes ────────────────────────────────────────────────────────

def items(n, payload=100):
    return [(f"r{i}", {"messages": [{"role": "user", "content": "x" * payload}]}) for i in range(n)]


def test_chunking_respects_the_byte_budget():
    reqs = items(10)
    size = rebase.request_size(*reqs[0])
    chunks = list(rebase.chunk_requests(reqs, max_bytes=size * 3 + 1))
    assert [len(c) for c in chunks] == [3, 3, 3, 1]
    for c in chunks:
        assert sum(rebase.request_size(cid, p) for cid, p in c) <= size * 3 + 1


def test_chunking_respects_the_request_budget():
    chunks = list(rebase.chunk_requests(items(7), max_bytes=10**9, max_requests=3))
    assert [len(c) for c in chunks] == [3, 3, 1]


def test_chunking_preserves_order_and_loses_nothing():
    reqs = items(25, payload=50)
    flat = [cid for chunk in rebase.chunk_requests(reqs, max_bytes=2000) for cid, _ in chunk]
    assert flat == [cid for cid, _ in reqs]


def test_oversized_request_gets_its_own_chunk():
    reqs = items(2) + [("r2", {"messages": [{"role": "user", "content": "y" * 5000}]})] + items(2)[:1]
    chunks = list(rebase.chunk_requests(reqs, max_bytes=1000))
    assert any(len(c) == 1 and c[0][0] == "r2" for c in chunks)
    assert sum(len(c) for c in chunks) == 4


def test_chunking_empty_input():
    assert list(rebase.chunk_requests([])) == []


def test_size_estimate_counts_escaped_non_ascii_at_worst_case():
    ascii_size = rebase.request_size("r0", {"c": "a" * 10})
    assert rebase.request_size("r0", {"c": "é" * 10}) > ascii_size


def test_custom_ids_fit_the_api_limit():
    import re
    for i in (0, 9, 99_999, 10**9):
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rebase.custom_id_for(i))
        assert rebase.index_of(rebase.custom_id_for(i)) == i
    with pytest.raises(ValueError):
        rebase.index_of("abc")


# ── Result classification ────────────────────────────────────────────────────

def item(result):
    return NS(custom_id="r0", result=result)


def test_classify_succeeded_result_with_thinking_block():
    got = rebase.classify_item(item(NS(type="succeeded", message=make_response(FILES_A))))
    result, model, tokens, reason = got
    assert reason is None and result["files"][0]["path"] == "src/auth.py"
    assert model == "claude-haiku-5-5-20260301" and tokens == (100, 50)


@pytest.mark.parametrize("rtype", ["errored", "canceled", "expired"])
def test_classify_non_succeeded(rtype):
    assert rebase.classify_item(item(NS(type=rtype)))[0:2] == (None, None)
    assert rebase.classify_item(item(NS(type=rtype)))[3] == rtype


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_succeeded_but_refused_or_truncated_is_a_failure(stop):
    got = rebase.classify_item(item(NS(type="succeeded", message=make_response(FILES_A, stop_reason=stop))))
    assert got[0] is None and got[3] == stop


# ── End to end on fakes ──────────────────────────────────────────────────────

class FakeBatches:
    """Message Batches stand-in. Returns results in reverse order; one errors."""

    def __init__(self, error_ids=()):
        self.created = []
        self.error_ids = set(error_ids)

    def create(self, requests):
        self.created.append(requests)
        return NS(id=f"batch_{len(self.created)}")

    def retrieve(self, batch_id):
        return NS(processing_status="ended",
                  request_counts=NS(processing=0, succeeded=0, errored=0))

    def results(self, batch_id):
        reqs = self.created[int(batch_id.split("_")[1]) - 1]
        for req in reversed(reqs):  # arbitrary order: keyed by custom_id only
            cid = req["custom_id"]
            files = FILES_A if "auth" in req["params"]["messages"][0]["content"] else FILES_B
            if cid in self.error_ids:
                yield NS(custom_id=cid, result=NS(type="expired"))
            else:
                yield NS(custom_id=cid, result=NS(type="succeeded", message=make_response(files)))


class RebaseClient(FakeClient):
    def __init__(self, batches, sync_response):
        super().__init__([sync_response])
        self.messages.batches = batches


@pytest.fixture
def rebase_env(tmp_dir, duckdb_storage, sample_commits, sample_diffs, monkeypatch):
    # Building batch requests uses the SDK's request types; the SDK is an
    # optional dependency (installed when scoring is enabled).
    pytest.importorskip("anthropic")
    duckdb_storage.insert_batch("commits", sample_commits)
    duckdb_storage.insert_batch("diffs", sample_diffs)
    monkeypatch.chdir(tmp_dir)  # consent file lands in the temp dir
    config = NS(
        _path=str(tmp_dir / ".coding-productivity.env"), EXCLUDED_DEVELOPERS=[],
        SCORING_MODEL="claude-haiku-5-5", SCORING_EFFORT="medium",
        SCORING_MAX_TOKENS=16000, ANTHROPIC_API_KEY="k",
    )
    monkeypatch.setattr(rebase, "get_storage", lambda cfg: duckdb_storage)
    args = NS(activate_version=None, force=False, restart=False, no_activate=False,
              wait=0, poll_interval=1, workers=2, max_batch_bytes=rebase.MAX_BATCH_BYTES,
              max_batch_requests=rebase.MAX_BATCH_REQUESTS)

    def run(client, **overrides):
        monkeypatch.setattr(rebase, "_make_client", lambda key: client)
        for k, v in overrides.items():
            setattr(args, k, v)
        return rebase.run(config, args)

    return NS(storage=duckdb_storage, config=config, args=args, run=run, tmp=tmp_dir)


def test_rebase_writes_new_tables_then_activates(rebase_env, capsys):
    client = RebaseClient(FakeBatches(), make_response(FILES_A))
    assert rebase_env.run(client) == rebase.EXIT_DONE

    st = rebase_env.storage
    rows = st.query("SELECT * FROM commit_scores_v2 ORDER BY commit_sha")
    assert [r["commit_sha"] for r in rows] == ["abc123def456", "def456abc789"]
    assert all(r["scorer_model"] == "claude-haiku-5-5-20260301" for r in rows)
    assert all(len(r["prompt_version"]) == 12 for r in rows)
    assert st.count("file_scores_v2") == 2
    assert st.count("commit_scores") == 0  # old tables untouched

    active = scoring_meta.get_active(st)
    assert (active.version, active.commit_table, active.scorer_model) == (2, "commit_scores_v2", "claude-haiku-5-5")
    out = capsys.readouterr().out
    assert "| 2026-01 |" in out and "| total | 2 | 0 | 2 | 0 |" in out
    assert out.index("| total |") < out.index("Activated scoring series v2")  # report first


def test_batch_request_shape_and_index_map(rebase_env):
    batches = FakeBatches()
    rebase_env.run(RebaseClient(batches, make_response(FILES_A)))
    reqs = batches.created[0]
    assert [r["custom_id"] for r in reqs] == ["r0", "r1"]
    params = reqs[0]["params"]
    assert params["thinking"] == {"type": "adaptive"}
    assert params["output_config"] == {"effort": "medium"}
    assert params["max_tokens"] == 16000
    state = json.loads((rebase_env.tmp / ".coding-productivity" / "rebase_state.json").read_text())
    assert state["shas"] == ["abc123def456", "def456abc789"]  # custom_id r{i} -> shas[i]


def test_failed_batch_results_are_retried_synchronously(rebase_env):
    client = RebaseClient(FakeBatches(error_ids={"r1"}), make_response(FILES_B))
    assert rebase_env.run(client) == rebase.EXIT_DONE
    assert rebase_env.storage.count("commit_scores_v2") == 2
    assert len(client.messages.calls) == 1  # only the expired one went synchronous
    state = json.loads((rebase_env.tmp / ".coding-productivity" / "rebase_state.json").read_text())
    assert state["failed"] == {} and state["status"] == "activated"


def test_unrecoverable_commits_block_activation(rebase_env):
    refusal = make_response(FILES_B, stop_reason="refusal")
    client = RebaseClient(FakeBatches(error_ids={"r1"}), refusal)
    assert rebase_env.run(client) == rebase.EXIT_NOT_ACTIVATED
    assert scoring_meta.get_active(rebase_env.storage).version == 1  # not switched
    assert rebase_env.storage.count("commit_scores_v2") == 1


def test_pending_batches_exit_and_resume_without_resubmitting(rebase_env):
    class Pending(FakeBatches):
        ready = False

        def retrieve(self, batch_id):
            return NS(processing_status="ended" if self.ready else "in_progress",
                      request_counts=NS(processing=2, succeeded=0, errored=0))

    batches = Pending()
    client = RebaseClient(batches, make_response(FILES_A))
    assert rebase_env.run(client) == rebase.EXIT_PENDING
    assert len(batches.created) == 1
    batches.ready = True
    assert rebase_env.run(client) == rebase.EXIT_DONE
    assert len(batches.created) == 1  # resumed: nothing submitted twice
    assert scoring_meta.get_active(rebase_env.storage).version == 2


def test_resume_refuses_a_changed_instrument(rebase_env):
    class Pending(FakeBatches):
        def retrieve(self, batch_id):
            return NS(processing_status="in_progress",
                      request_counts=NS(processing=2, succeeded=0, errored=0))

    client = RebaseClient(Pending(), make_response(FILES_A))
    assert rebase_env.run(client) == rebase.EXIT_PENDING
    rebase_env.config.SCORING_MODEL = "claude-haiku-5-6"
    assert rebase_env.run(client) == rebase.EXIT_ERROR


def test_no_activate_builds_but_does_not_switch(rebase_env):
    client = RebaseClient(FakeBatches(), make_response(FILES_A))
    assert rebase_env.run(client, no_activate=True) == rebase.EXIT_NOT_ACTIVATED
    assert scoring_meta.get_active(rebase_env.storage).version == 1
    assert rebase_env.run(client, no_activate=False, activate_version=2) == rebase.EXIT_DONE
    assert scoring_meta.get_active(rebase_env.storage).version == 2


def test_second_rebase_uses_the_next_free_version(rebase_env):
    client = RebaseClient(FakeBatches(), make_response(FILES_A))
    rebase_env.run(client)
    rebase_env.run(client)
    assert scoring_meta.get_active(rebase_env.storage).version == 3
    assert rebase_env.storage.count("commit_scores_v2") == 2  # v2 left intact
