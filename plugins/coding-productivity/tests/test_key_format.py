"""PSEUDONYMIZATION_KEY_FORMAT: text (default) or hex key bytes."""

import hashlib
import hmac
import json

import pytest

from lib import anonymize, identity
from lib.config import Config
from lib.dedup import apply_retroactive_merges

from .test_duplicate_work import commit

HEX_KEY = "00112233445566778899aabbccddeeff" * 2  # 64 hex characters


def expected(value, key_bytes):
    """HMAC-SHA256 of the normalized value, first 12 hex characters."""
    normalized = value.strip().lower().encode("utf-8")
    return hmac.new(key_bytes, normalized, hashlib.sha256).hexdigest()[:12]


def make_config(tmp_dir, body):
    path = tmp_dir / ".coding-productivity.env"
    path.write_text(body)
    path.chmod(0o600)
    return Config(path)


def hex_config(tmp_dir):
    return make_config(tmp_dir, f"ANONYMIZATION_ENABLED=true\nPSEUDONYMIZATION_KEY={HEX_KEY}\n"
                                "PSEUDONYMIZATION_KEY_FORMAT=hex\n")


# ── the key bytes ────────────────────────────────────────────────────────────

def test_text_is_the_default_and_unchanged(tmp_dir):
    cfg = make_config(tmp_dir, f"PSEUDONYMIZATION_KEY={HEX_KEY}\n")
    assert cfg.PSEUDONYMIZATION_KEY_FORMAT == "text"
    assert cfg.PSEUDONYMIZATION_KEY == HEX_KEY.encode("utf-8")  # as in every earlier version
    assert len(cfg.PSEUDONYMIZATION_KEY) == 64


def test_hex_decodes_the_key(tmp_dir):
    cfg = hex_config(tmp_dir)
    assert cfg.PSEUDONYMIZATION_KEY == bytes.fromhex(HEX_KEY)
    assert len(cfg.PSEUDONYMIZATION_KEY) == 32


def test_format_is_case_insensitive_and_missing_key_is_none(tmp_dir):
    assert make_config(tmp_dir, "PSEUDONYMIZATION_KEY_FORMAT= HEX \n").PSEUDONYMIZATION_KEY_FORMAT == "hex"
    assert make_config(tmp_dir, "PSEUDONYMIZATION_KEY_FORMAT=hex\n").PSEUDONYMIZATION_KEY is None


@pytest.mark.parametrize("bad", ["not-hex-at-all", "abc", "zz" * 32])
def test_invalid_hex_fails_clearly_without_echoing_the_key(tmp_dir, bad):
    cfg = make_config(tmp_dir, f"PSEUDONYMIZATION_KEY={bad}\nPSEUDONYMIZATION_KEY_FORMAT=hex\n")
    with pytest.raises(ValueError, match="not valid hex") as exc:
        cfg.PSEUDONYMIZATION_KEY
    assert bad not in str(exc.value)
    errors = cfg.validate()
    assert any("not valid hex" in e for e in errors)
    assert not any(bad in e for e in errors)


def test_unknown_format_is_a_validation_error(tmp_dir):
    errors = make_config(tmp_dir, f"PSEUDONYMIZATION_KEY={HEX_KEY}\n"
                                  "PSEUDONYMIZATION_KEY_FORMAT=base64\n").validate()
    assert any("PSEUDONYMIZATION_KEY_FORMAT" in e for e in errors)
    errors = make_config(tmp_dir, "PSEUDONYMIZATION_KEY_FORMAT=base64\n").validate()
    assert any("PSEUDONYMIZATION_KEY_FORMAT" in e for e in errors)  # even with no key set


def test_valid_hex_config_has_no_key_error(tmp_dir):
    assert not any("PSEUDONYMIZATION" in e for e in hex_config(tmp_dir).validate())


# ── hashing with the hex key ─────────────────────────────────────────────────

def test_known_email_hashes_to_hmac_of_the_decoded_key(tmp_dir):
    key = hex_config(tmp_dir).PSEUDONYMIZATION_KEY
    for email in ("Jane.Doe@Example.com", "  jane.doe@example.com "):
        assert anonymize.pseudonymize(email, key) == expected("jane.doe@example.com",
                                                              bytes.fromhex(HEX_KEY))
    text_key = HEX_KEY.encode("utf-8")
    assert anonymize.pseudonymize("jane.doe@example.com", key) != expected(
        "jane.doe@example.com", text_key)  # the two formats really differ


def test_extraction_hashing_uses_the_decoded_key(tmp_dir):
    key = hex_config(tmp_dir).PSEUDONYMIZATION_KEY
    rec = {"author_name": "Jane Doe", "author_email": "jane@x.com"}
    anonymize.hash_record(rec, ["author_name", "author_email"], key)
    raw = bytes.fromhex(HEX_KEY)
    assert rec == {"author_name": expected("Jane Doe", raw),
                   "author_email": expected("jane@x.com", raw)}


def test_identity_matching_uses_the_decoded_key(tmp_dir):
    key = hex_config(tmp_dir).PSEUDONYMIZATION_KEY
    raw = bytes.fromhex(HEX_KEY)
    h = lambda v: expected(v, raw)  # noqa: E731

    # generic names are recognized by their hashes
    people = identity.resolve(
        [(h("Root"), h("r1@x.com"), 1), (h("Root"), h("r2@y.com"), 1),
         (h("Anna"), h("a@x.com"), 1)], key=key)
    assert sorted(p.non_person for p in people.persons) == [False, True, True]

    # IDENTITY_MERGES with real emails match the stored pseudonyms
    people = identity.resolve([("n1", h("a@x.com"), 1), ("n2", h("b@y.com"), 1)],
                              merges={"b@y.com": "a@x.com"}, key=key, auto=False)
    assert len(people.persons) == 1

    # IDENTITY_NO_MERGE with real emails blocks the automatic join
    rows = [(h("Anna"), h("a@x.com"), 1), (h("Anna"), h("b@y.com"), 1)]
    assert len(identity.resolve(rows, key=key).persons) == 1
    assert len(identity.resolve(rows, no_merge=[("a@x.com", "b@y.com")], key=key).persons) == 2


def test_the_text_key_does_not_match_hex_hashed_data(tmp_dir):
    """Why the setting exists: the same 64 characters give a different key."""
    raw = bytes.fromhex(HEX_KEY)
    rows = [("n1", expected("a@x.com", raw), 1), ("n2", expected("b@y.com", raw), 1)]
    merges = {"b@y.com": "a@x.com"}
    assert len(identity.resolve(rows, merges=merges, key=raw, auto=False).persons) == 1
    assert len(identity.resolve(rows, merges=merges, key=HEX_KEY.encode(), auto=False).persons) == 2


def test_retroactive_merge_and_mapping_use_the_decoded_key(tmp_dir, duckdb_storage):
    key = hex_config(tmp_dir).PSEUDONYMIZATION_KEY
    raw = bytes.fromhex(HEX_KEY)
    alias_hash = expected("old@x.com", raw)
    row = commit("c1", email=alias_hash)
    duckdb_storage.insert_batch("commits", [row])
    mapping = tmp_dir / "developer_mapping.json"
    mapping.write_text(json.dumps({alias_hash: {"name": "Old", "email": "old@x.com"}}))

    apply_retroactive_merges(duckdb_storage, mapping, key, {"old@x.com": "new@x.com"})

    new_hash = expected("new@x.com", raw)
    assert duckdb_storage.query("SELECT author_email AS e FROM commits")[0]["e"] == new_hash
    assert json.loads(mapping.read_text())[new_hash]["email"] == "new@x.com"
