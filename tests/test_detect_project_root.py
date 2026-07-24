"""Tests for detect_project_root argument parser."""

from __future__ import annotations

import pytest

from recallloom.scripts.detect_project_root import build_parser


class TestBuildParser:
    def test_returns_argument_parser(self):
        parser = build_parser()
        assert parser.description is not None
        assert "RecallLoom" in parser.description

    def test_default_path_is_dot(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.path == "."

    def test_custom_path(self):
        parser = build_parser()
        args = parser.parse_args(["/some/dir"])
        assert args.path == "/some/dir"

    def test_json_flag_default_false(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.json is False

    def test_json_flag_enabled(self):
        parser = build_parser()
        args = parser.parse_args(["--json"])
        assert args.json is True

    def test_json_with_path(self):
        parser = build_parser()
        args = parser.parse_args(["/my/path", "--json"])
        assert args.path == "/my/path"
        assert args.json is True
