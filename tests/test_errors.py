"""Tests for recallloom error types."""

from __future__ import annotations

import pytest

from recallloom.scripts.core.errors import (
    ConfigContractError,
    EnvironmentContractError,
    LockBusyError,
    RecallLoomError,
    StorageResolutionError,
)


class TestRecallLoomError:
    def test_is_runtime_error(self):
        assert issubclass(RecallLoomError, RuntimeError)

    def test_message_is_preserved(self):
        err = RecallLoomError("boom")
        assert str(err) == "boom"

    def test_failure_reason_default_none(self):
        err = RecallLoomError("boom")
        assert err.failure_reason is None

    def test_failure_reason_set(self):
        err = RecallLoomError("boom", failure_reason="disk_full")
        assert err.failure_reason == "disk_full"


class TestStorageResolutionError:
    def test_is_recallloom_error(self):
        assert issubclass(StorageResolutionError, RecallLoomError)

    def test_inherits_failure_reason(self):
        err = StorageResolutionError("bad root", failure_reason="ambiguous")
        assert err.failure_reason == "ambiguous"


class TestConfigContractError:
    def test_is_recallloom_error(self):
        assert issubclass(ConfigContractError, RecallLoomError)


class TestEnvironmentContractError:
    def test_is_recallloom_error(self):
        assert issubclass(EnvironmentContractError, RecallLoomError)


class TestLockBusyError:
    def test_is_recallloom_error(self):
        assert issubclass(LockBusyError, RecallLoomError)


class TestExceptionHierarchy:
    def test_all_can_be_caught_as_runtime_error(self):
        for cls in (
            StorageResolutionError,
            ConfigContractError,
            EnvironmentContractError,
            LockBusyError,
        ):
            with pytest.raises(RuntimeError):
                raise cls("test")

    def test_all_can_be_caught_as_recallsloom_error(self):
        for cls in (
            StorageResolutionError,
            ConfigContractError,
            EnvironmentContractError,
            LockBusyError,
        ):
            with pytest.raises(RecallLoomError):
                raise cls("test")
