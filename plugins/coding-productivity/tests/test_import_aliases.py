"""scripts/import_aliases.py: alias CSV into IDENTITY_MERGES."""

import pytest

import import_aliases as ia
from lib.config import Config


def make_config(tmp_dir, merges=""):
    path = tmp_dir / ".coding-productivity.env"
    path.write_text(f"PLATFORM=github\nIDENTITY_MERGES={merges}\n")
    path.chmod(0o600)
    return Config(path)


def write_csv(tmp_dir, text):
    path = tmp_dir / "aliases.csv"
    path.write_text(text, encoding="utf-8")
    return path


def test_pairs_are_added_lowercased_and_keep_existing(tmp_dir):
    cfg = make_config(tmp_dir, "old@x.com:main@x.com")
    csv_path = write_csv(tmp_dir, "original_email,canonical_email\n"
                                  "Anna.R@Gmail.com , anna@corp.com\n"
                                  "b@y.com,b@corp.com\n")
    result = ia.import_aliases(cfg, csv_path)
    assert result.added == 2 and result.refused == []
    expected = {"old@x.com": "main@x.com", "anna.r@gmail.com": "anna@corp.com",
                "b@y.com": "b@corp.com"}
    assert cfg.IDENTITY_MERGES == expected
    assert Config(cfg._path).IDENTITY_MERGES == expected      # persisted to the env file


def test_equal_pairs_are_skipped(tmp_dir):
    cfg = make_config(tmp_dir)
    csv_path = write_csv(tmp_dir, "original_email,canonical_email\nA@x.com,a@X.com\nb@x.com,c@x.com\n")
    result = ia.import_aliases(cfg, csv_path)
    assert (result.added, result.equal) == (1, 1)
    assert cfg.IDENTITY_MERGES == {"b@x.com": "c@x.com"}


@pytest.mark.parametrize("original, canonical", [
    ("a,b@x.com", "c@x.com"), ("a@x.com", "c:d@x.com"), ("a@x.com", "c@x.com,d@x.com"),
    ("a:b@x.com", "c@x.com"),
])
def test_values_with_comma_or_colon_are_refused(tmp_dir, original, canonical):
    cfg = make_config(tmp_dir)
    csv_path = write_csv(tmp_dir, f'original_email,canonical_email\n"{original}","{canonical}"\n'
                                  "ok@x.com,main@x.com\n")
    result = ia.import_aliases(cfg, csv_path)
    assert result.refused == [(2, "email contains ',' or ':'")]
    assert result.added == 1
    assert cfg.IDENTITY_MERGES == {"ok@x.com": "main@x.com"}  # the setting stays well-formed


def test_empty_values_are_refused_and_blank_lines_ignored(tmp_dir):
    cfg = make_config(tmp_dir)
    csv_path = write_csv(tmp_dir, "original_email,canonical_email\n\na@x.com,\n,b@x.com\n")
    result = ia.import_aliases(cfg, csv_path)
    assert [r[1] for r in result.refused] == ["empty email", "empty email"]
    assert cfg.IDENTITY_MERGES == {}


def test_conflicting_alias_keeps_the_first_mapping(tmp_dir):
    cfg = make_config(tmp_dir, "a@x.com:first@x.com")
    csv_path = write_csv(tmp_dir, "original_email,canonical_email\n"
                                  "a@x.com,other@x.com\n"          # conflicts with the config
                                  "a@x.com,first@x.com\n"          # already there
                                  "b@x.com,one@x.com\nb@x.com,two@x.com\n")  # conflicts in the CSV
    result = ia.import_aliases(cfg, csv_path)
    assert (result.added, result.already, len(result.refused)) == (1, 1, 2)
    assert cfg.IDENTITY_MERGES == {"a@x.com": "first@x.com", "b@x.com": "one@x.com"}


def test_dry_run_does_not_write(tmp_dir):
    cfg = make_config(tmp_dir)
    csv_path = write_csv(tmp_dir, "original_email,canonical_email\na@x.com,b@x.com\n")
    result = ia.import_aliases(cfg, csv_path, dry_run=True)
    assert result.added == 1
    assert Config(cfg._path).IDENTITY_MERGES == {}


def test_header_any_order_case_and_bom(tmp_dir):
    cfg = make_config(tmp_dir)
    path = tmp_dir / "a.csv"
    path.write_bytes("﻿Canonical_Email, ORIGINAL_EMAIL ,note\nb@x.com,a@x.com,hr\n".encode("utf-8"))
    ia.import_aliases(cfg, path)
    assert cfg.IDENTITY_MERGES == {"a@x.com": "b@x.com"}


def test_missing_column_is_an_error(tmp_dir):
    cfg = make_config(tmp_dir)
    with pytest.raises(ValueError, match="canonical_email"):
        ia.import_aliases(cfg, write_csv(tmp_dir, "original_email,other\na@x.com,b@x.com\n"))


def test_cli_exit_codes_and_no_email_in_output(tmp_dir, monkeypatch, capsys):
    cfg = make_config(tmp_dir)
    good = write_csv(tmp_dir, "original_email,canonical_email\na@x.com,b@x.com\n")
    run = lambda *a: (monkeypatch.setattr("sys.argv", ["import_aliases.py", *a]), ia.main())[1]  # noqa: E731
    assert run("--config", str(cfg._path), "--csv", str(good)) == 0
    bad = write_csv(tmp_dir, "original_email,canonical_email\nsecret:one@x.com,b@x.com\n")
    assert run("--config", str(cfg._path), "--csv", str(bad)) == 1
    out = capsys.readouterr().out
    assert "refused CSV line 2" in out and "secret" not in out
    assert run("--config", str(cfg._path), "--csv", str(tmp_dir / "missing.csv")) == 2
