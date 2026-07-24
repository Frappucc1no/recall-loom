"""Tests for path redaction, secret detection, and privacy helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from recallloom.scripts.core.output.privacy import (
    _EMAIL_PATTERN,
    _OPENAI_TOKEN_PATTERN,
    _COMMON_TOKEN_PATTERN,
    _BEARER_TOKEN_PATTERN,
    _SECRET_ASSIGNMENT_PATTERN,
    _URL_PATTERN,
    _field_looks_pathlike,
    _field_looks_sensitive,
    contains_local_absolute_path,
    private_json_paths_enabled,
    public_project_path,
    public_project_root_label,
    redact_public_text,
    publicize_text_paths,
)


# --- private_json_paths_enabled ---

class TestPrivateJsonPathsEnabled:
    def test_always_false(self):
        assert private_json_paths_enabled() is False

    def test_always_false_with_env(self):
        assert private_json_paths_enabled(env={"RECALLLOOM_DEBUG_JSON_PATHS": "1"}) is False


# --- public_project_root_label ---

class TestPublicProjectRootLabel:
    def test_returns_name(self, tmp_path):
        project = tmp_path / "my-project"
        project.mkdir()
        label = public_project_root_label(project)
        assert label == "my-project"

    def test_returns_dot_for_root(self, tmp_path):
        label = public_project_root_label(tmp_path)
        assert label == tmp_path.name or label == "."


# --- public_project_path ---

class TestPublicProjectPath:
    def test_none_returns_none(self, tmp_path):
        assert public_project_path(None, project_root=tmp_path) is None

    def test_relative_path_resolves(self, tmp_path):
        result = public_project_path("src/main.py", project_root=tmp_path)
        assert result == "src/main.py"

    def test_absolute_path_returns_name_when_outside_root(self, tmp_path):
        result = public_project_path("/etc/passwd", project_root=tmp_path)
        assert result is not None
        assert "passwd" in result


# --- contains_local_absolute_path ---

class TestContainsLocalAbsolutePath:
    def test_posix_absolute(self):
        assert contains_local_absolute_path("see /etc/hosts") is True

    def test_windows_drive(self):
        assert contains_local_absolute_path("see C:\\Users\\test") is True

    def test_none_returns_false(self):
        assert contains_local_absolute_path(None) is False

    def test_empty_string(self):
        assert contains_local_absolute_path("") is False

    def test_no_path(self):
        assert contains_local_absolute_path("no paths here") is False


# --- redact_public_text secret detection ---

class TestRedactPublicTextSecretDetection:
    def test_github_pat_token(self):
        result = redact_public_text("token: ghp_ABCDEFGHIJKLMNOP1234567890")
        assert "ghp_" not in result
        assert "redacted" in result

    def test_aws_access_key(self):
        result = redact_public_text("key: AKIAIOSFODNN7EXAMPLE")
        assert "AKIA" not in result
        assert "redacted" in result

    def test_openai_token(self):
        result = redact_public_text("api sk-projABCDEF1234567890abcdef")
        assert "sk-" not in result
        assert "redacted" in result

    def test_secret_assignment(self):
        result = redact_public_text('api_key = "supersecretvalue"')
        assert "credential=redacted" in result

    def test_bearer_token(self):
        result = redact_public_text("Authorization: Bearer abc123.def456")
        assert "bearer redacted" in result.lower()

    def test_email(self):
        result = redact_public_text("contact user@example.com for info")
        assert "user@example.com" not in result
        assert "redacted-email" in result

    def test_url_redacted(self):
        result = redact_public_text("see https://evil.com/secret")
        assert "https://" not in result
        assert "redacted-url" in result

    def test_slack_token(self):
        result = redact_public_text("token: xoxb-1234567890-abcdef-ghijklmnop")
        assert "xoxb-" not in result
        assert "redacted" in result

    def test_none_returns_none(self):
        assert redact_public_text(None) is None

    def test_empty_string(self):
        assert redact_public_text("") == ""

    def test_no_secrets_passes_through(self):
        text = "This is a normal sentence with no secrets."
        assert redact_public_text(text) == text


# --- publicize_text_paths ---

class TestPublicizeTextPaths:
    def test_none_returns_none(self):
        assert publicize_text_paths(None, project_root="/foo") is None

    def test_empty_string(self):
        assert publicize_text_paths("", project_root="/foo") == ""

    def test_private_returns_original(self):
        assert publicize_text_paths("text", project_root="/foo", private=True) == "text"


# --- _field_looks_pathlike ---

class TestFieldLooksPathlike:
    def test_known_path_field(self):
        assert _field_looks_pathlike("project_root") is True

    def test_suffix_file(self):
        assert _field_looks_pathlike("my_file") is True

    def test_suffix_dir(self):
        assert _field_looks_pathlike("my_dir") is True

    def test_suffix_path(self):
        assert _field_looks_pathlike("target_path") is True

    def test_suffix_root(self):
        assert _field_looks_pathlike("storage_root") is True

    def test_non_path_field(self):
        assert _field_looks_pathlike("name") is False

    def test_excluded_field(self):
        assert _field_looks_pathlike("latest_file") is False

    def test_none_returns_false(self):
        assert _field_looks_pathlike(None) is False


# --- _field_looks_sensitive ---

class TestFieldLooksSensitive:
    def test_token_field(self):
        assert _field_looks_sensitive("token") is True

    def test_secret_field(self):
        assert _field_looks_sensitive("secret") is True

    def test_password_field(self):
        assert _field_looks_sensitive("password") is True

    def test_api_key_field(self):
        assert _field_looks_sensitive("api_key") is True

    def test_credential_field(self):
        assert _field_looks_sensitive("credential") is True

    def test_normal_field(self):
        assert _field_looks_sensitive("name") is False

    def test_none_returns_false(self):
        assert _field_looks_sensitive(None) is False

    def test_case_insensitive(self):
        assert _field_looks_sensitive("TOKEN") is True

    def test_hyphenated(self):
        assert _field_looks_sensitive("api-key") is True


# --- Regex patterns ---

class TestRegexPatterns:
    def test_secret_assignment_matches(self):
        assert _SECRET_ASSIGNMENT_PATTERN.search('api_key = "abc123"')
        assert _SECRET_ASSIGNMENT_PATTERN.search("token: mytoken")
        assert _SECRET_ASSIGNMENT_PATTERN.search('password="hunter2"')

    def test_secret_assignment_no_match(self):
        assert _SECRET_ASSIGNMENT_PATTERN.search("name = alice") is None

    def test_common_token_ghp(self):
        assert _COMMON_TOKEN_PATTERN.search("ghp_ABCDEFGHIJKLMNOP1234567890")

    def test_common_token_aws(self):
        assert _COMMON_TOKEN_PATTERN.search("AKIAIOSFODNN7EXAMPLE")

    def test_common_token_slack(self):
        assert _COMMON_TOKEN_PATTERN.search("xoxb-1234567890-abcdef-ghijklmnop")

    def test_openai_token(self):
        assert _OPENAI_TOKEN_PATTERN.search("sk-projABCDEF1234567890abcdef")

    def test_bearer_pattern(self):
        assert _BEARER_TOKEN_PATTERN.search("Bearer abc123.def456")

    def test_email_pattern(self):
        assert _EMAIL_PATTERN.search("user@example.com")

    def test_url_pattern(self):
        assert _URL_PATTERN.search("https://example.com/path?q=1")
        assert _URL_PATTERN.search("http://example.com")
