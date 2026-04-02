"""Unit tests for SessionReporter.

All I/O is contained within tmp_path (pytest fixture) — no real DB, no network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from news_trade.services.session_reporter import SessionReporter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_report(**kwargs: object) -> dict:
    defaults: dict[str, object] = {
        "session_start": "2026-04-01T09:00:00+00:00",
        "session_end": "2026-04-01T10:00:00+00:00",
        "duration_seconds": 3600.0,
        "version": "0.1.0",
        "commit": "abc1234",
        "cycles_run": 5,
        "system_halted": False,
        "orders_placed": [],
        "signals": {"total": 3, "approved": 2, "rejected": 1},
        "open_stage1_positions": [],
        "errors": [],
    }
    return {**defaults, **kwargs}


def _write_session_file(directory: Path, ts: str, report: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"session_{ts}.json"
    path.write_text(json.dumps(report))
    return path


# ---------------------------------------------------------------------------
# Tests — find_latest / load_latest
# ---------------------------------------------------------------------------


class TestFindLatest:
    def test_returns_none_when_dir_empty(self, tmp_path: Path) -> None:
        reporter = SessionReporter(sessions_dir=tmp_path)
        assert reporter.find_latest() is None

    def test_returns_none_when_dir_missing(self, tmp_path: Path) -> None:
        reporter = SessionReporter(sessions_dir=tmp_path / "nonexistent")
        assert reporter.find_latest() is None

    def test_returns_most_recent_by_filename(self, tmp_path: Path) -> None:
        _write_session_file(tmp_path, "20260401_090000", _make_report())
        _write_session_file(tmp_path, "20260402_090000", _make_report())
        latest = _write_session_file(tmp_path, "20260403_090000", _make_report())

        reporter = SessionReporter(sessions_dir=tmp_path)
        assert reporter.find_latest() == latest

    def test_ignores_non_session_files(self, tmp_path: Path) -> None:
        (tmp_path / "other.json").write_text("{}")
        path = _write_session_file(tmp_path, "20260401_090000", _make_report())

        reporter = SessionReporter(sessions_dir=tmp_path)
        assert reporter.find_latest() == path


class TestLoadLatest:
    def test_returns_none_when_no_sessions(self, tmp_path: Path) -> None:
        reporter = SessionReporter(sessions_dir=tmp_path)
        assert reporter.load_latest() is None

    def test_returns_parsed_dict(self, tmp_path: Path) -> None:
        report = _make_report(cycles_run=7)
        _write_session_file(tmp_path, "20260401_090000", report)

        reporter = SessionReporter(sessions_dir=tmp_path)
        result = reporter.load_latest()

        assert result is not None
        assert result["cycles_run"] == 7


# ---------------------------------------------------------------------------
# Tests — load (specific file)
# ---------------------------------------------------------------------------


class TestLoad:
    def test_parses_json(self, tmp_path: Path) -> None:
        report = _make_report(commit="deadbeef")
        path = _write_session_file(tmp_path, "20260401_090000", report)

        reporter = SessionReporter(sessions_dir=tmp_path)
        result = reporter.load(path)

        assert result["commit"] == "deadbeef"

    def test_raises_on_missing_file(self, tmp_path: Path) -> None:
        reporter = SessionReporter(sessions_dir=tmp_path)
        with pytest.raises(FileNotFoundError):
            reporter.load(tmp_path / "missing.json")


# ---------------------------------------------------------------------------
# Tests — write
# ---------------------------------------------------------------------------


class TestWrite:
    def _make_settings(self) -> MagicMock:
        settings = MagicMock()
        settings.database_url = "sqlite:///:memory:"
        return settings

    def test_creates_file_with_correct_name(self, tmp_path: Path) -> None:
        reporter = SessionReporter(sessions_dir=tmp_path)
        session_start = datetime(2026, 4, 1, 9, 0, 0, tzinfo=UTC)

        with (
            patch("news_trade.services.session_reporter.build_engine"),
            patch("news_trade.services.session_reporter.Session") as mock_session_cls,
        ):
            # Make the Session context manager return something iterable
            mock_db = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_db.execute.return_value.scalars.return_value.all.return_value = []

            out_path = reporter.write(
                settings=self._make_settings(),
                session_start=session_start,
                cycle_count=3,
                errors=[],
                last_state={},  # type: ignore[arg-type]
                git_hash="abc1234",
            )

        assert out_path == tmp_path / "session_20260401_090000.json"
        assert out_path.exists()

    def test_report_contains_expected_keys(self, tmp_path: Path) -> None:
        reporter = SessionReporter(sessions_dir=tmp_path)
        session_start = datetime(2026, 4, 1, 9, 0, 0, tzinfo=UTC)

        with patch("news_trade.services.session_reporter.build_engine"), \
             patch("news_trade.services.session_reporter.Session") as mock_session_cls:
            mock_db = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_db.execute.return_value.scalars.return_value.all.return_value = []

            out_path = reporter.write(
                settings=self._make_settings(),
                session_start=session_start,
                cycle_count=2,
                errors=["some error"],
                last_state={"system_halted": True},  # type: ignore[typeddict-item]
                git_hash="deadbeef",
            )

        data = json.loads(out_path.read_text())
        assert data["cycles_run"] == 2
        assert data["commit"] == "deadbeef"
        assert data["system_halted"] is True
        assert data["errors"] == ["some error"]
        assert "session_start" in data
        assert "session_end" in data
        assert "duration_seconds" in data

    def test_writes_partial_record_on_db_failure(self, tmp_path: Path) -> None:
        reporter = SessionReporter(sessions_dir=tmp_path)
        session_start = datetime(2026, 4, 1, 9, 0, 0, tzinfo=UTC)

        with patch(
            "news_trade.services.session_reporter.build_engine",
            side_effect=RuntimeError("db down"),
        ):
            out_path = reporter.write(
                settings=self._make_settings(),
                session_start=session_start,
                cycle_count=1,
                errors=[],
                last_state={},  # type: ignore[arg-type]
                git_hash="abc1234",
            )

        data = json.loads(out_path.read_text())
        assert "error" in data
        assert data["cycles_run"] == 1

    def test_creates_sessions_dir_if_missing(self, tmp_path: Path) -> None:
        sessions_dir = tmp_path / "nested" / "sessions"
        reporter = SessionReporter(sessions_dir=sessions_dir)
        session_start = datetime(2026, 4, 1, 9, 0, 0, tzinfo=UTC)

        with patch("news_trade.services.session_reporter.build_engine"), \
             patch("news_trade.services.session_reporter.Session") as mock_session_cls:
            mock_db = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_db.execute.return_value.scalars.return_value.all.return_value = []

            reporter.write(
                settings=self._make_settings(),
                session_start=session_start,
                cycle_count=0,
                errors=[],
                last_state={},  # type: ignore[arg-type]
                git_hash="abc1234",
            )

        assert sessions_dir.is_dir()


# ---------------------------------------------------------------------------
# Tests — log_startup_summary
# ---------------------------------------------------------------------------


class TestLogStartupSummary:
    def test_emits_warning_on_system_halt(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        reporter = SessionReporter()
        report = _make_report(system_halted=True)

        with caplog.at_level("WARNING", logger="news_trade.services.session_reporter"):
            reporter.log_startup_summary(report, current_commit="abc1234")

        assert any("SYSTEM HALT" in r.message for r in caplog.records)

    def test_emits_warning_for_errors(self, caplog: pytest.LogCaptureFixture) -> None:
        reporter = SessionReporter()
        report = _make_report(errors=["timeout on AAPL", "DB write failed"])

        with caplog.at_level("WARNING", logger="news_trade.services.session_reporter"):
            reporter.log_startup_summary(report, current_commit="abc1234")

        messages = [r.message for r in caplog.records]
        assert any("2 error" in m for m in messages)

    def test_logs_version_change(self, caplog: pytest.LogCaptureFixture) -> None:
        reporter = SessionReporter()
        report = _make_report(commit="oldcommit")

        with caplog.at_level("INFO", logger="news_trade.services.session_reporter"):
            reporter.log_startup_summary(report, current_commit="newcommit")

        messages = [r.message for r in caplog.records]
        assert any("oldcommit" in m and "newcommit" in m for m in messages)

    def test_no_version_change_log_when_same_commit(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        reporter = SessionReporter()
        report = _make_report(commit="abc1234")

        with caplog.at_level("INFO", logger="news_trade.services.session_reporter"):
            reporter.log_startup_summary(report, current_commit="abc1234")

        messages = [r.message for r in caplog.records]
        assert not any("Version change" in m for m in messages)

    def test_no_halt_warning_when_clean(self, caplog: pytest.LogCaptureFixture) -> None:
        reporter = SessionReporter()
        report = _make_report(system_halted=False, errors=[])

        with caplog.at_level("WARNING", logger="news_trade.services.session_reporter"):
            reporter.log_startup_summary(report, current_commit="abc1234")

        assert not any(r.levelname == "WARNING" for r in caplog.records)
