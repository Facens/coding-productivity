"""Tests for lib/bots.py — bot detection heuristics."""

from lib.bots import is_bot, detect_bots, load_overrides


class TestIsBot:
    def test_dependabot(self):
        assert is_bot("dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com")

    def test_github_actions(self):
        assert is_bot("github-actions[bot]", "action@github.com")

    def test_renovate(self):
        assert is_bot("renovate[bot]", "bot@renovateapp.com")

    def test_generic_noreply_pattern(self):
        assert is_bot("SomeService", "notifications@noreply.github.com")

    def test_bot_suffix_in_name(self):
        assert is_bot("my-custom[bot]", "custom@bot.internal")

    def test_regular_developer_not_bot(self):
        assert not is_bot("John Doe", "john@example.com")

    def test_regular_email_not_bot(self):
        assert not is_bot("Jane Smith", "jane.smith@company.com")

    def test_empty_strings(self):
        assert not is_bot("", "")

    def test_gitlab_ci_bot(self):
        assert is_bot("GitLab CI", "gitlab@localhost")


class TestDetectBots:
    def test_filters_bots_from_list(self):
        devs = [
            {"name": "dependabot[bot]", "email": "dep@noreply.github.com"},
            {"name": "Alice", "email": "alice@company.com"},
            {"name": "github-actions[bot]", "email": "action@github.com"},
            {"name": "Bob", "email": "bob@company.com"},
        ]
        bots = detect_bots(devs)
        bot_names = {b["name"] for b in bots}
        assert "dependabot[bot]" in bot_names
        assert "github-actions[bot]" in bot_names
        assert "Alice" not in bot_names
        assert "Bob" not in bot_names

    def test_empty_list(self):
        assert detect_bots([]) == []


class TestLoadOverrides:
    def test_adds_custom_bot_names(self):
        load_overrides(["CustomBot", "InternalCI"])
        assert is_bot("CustomBot", "custom@internal.com")
        assert is_bot("InternalCI", "ci@internal.com")

    def test_empty_overrides(self):
        load_overrides([])  # should not raise
