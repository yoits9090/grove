"""Public durable SQLite logical-history API tests."""
from pathlib import Path

import pytest

from grove import SQLiteHistory, SQLiteTreeStore, Snapshot
from grove.errors import StorageCorruptionError
import grove.history as history_module


def test_capture_revisions_and_open_are_detached(tmp_path: Path):
    with SQLiteTreeStore(tmp_path / "live.db") as source:
        source.create("before")
        history = SQLiteHistory(source, tmp_path / "history")
        first = history.capture()
        source.create("after")
        second = history.capture()

        assert isinstance(first, Snapshot)
        assert first.revision == 1
        assert second.revision == 2
        assert history.revisions() == (1, 2)
        assert history.snapshot(1) == first
        assert first.open().exists("/before")
        assert not first.open().exists("/after")
        detached = second.open()
        detached.rename("/after", "changed")
        assert second.open().exists("/after")
        with pytest.raises(KeyError):
            history.snapshot(99)


def test_capture_is_idempotent_for_a_revision(tmp_path: Path):
    with SQLiteTreeStore(tmp_path / "live.db") as source:
        source.create("node")
        history = SQLiteHistory(source, tmp_path / "history")
        first = history.capture()
        assert history.capture() == first
        assert history.revisions() == (1,)


def test_capture_publishes_atomically_and_cleans_temporary_file(tmp_path: Path, monkeypatch):
    with SQLiteTreeStore(tmp_path / "live.db") as source:
        source.create("first")
        history = SQLiteHistory(source, tmp_path / "history")
        first = history.capture()
        source.create("second")

        original_replace = history_module.os.replace
        def fail_final_replace(source_path, destination_path):
            # sqlite_store.backup uses its own os module; this hook therefore
            # targets only history's final revision publication.
            if Path(destination_path).name.startswith("snapshot-"):
                raise OSError("simulated publication failure")
            return original_replace(source_path, destination_path)
        monkeypatch.setattr(history_module.os, "replace", fail_final_replace)
        with pytest.raises(OSError, match="publication"):
            history.capture()

        assert history.revisions() == (first.revision,)
        assert first.path.exists()
        assert not list((tmp_path / "history").glob(".*.capture.db"))


def test_corrupt_artifact_fails_closed_on_lookup_and_open(tmp_path: Path):
    with SQLiteTreeStore(tmp_path / "live.db") as source:
        source.create("node")
        history = SQLiteHistory(source, tmp_path / "history")
        snapshot = history.capture()
        with snapshot.path.open("r+b") as artifact:
            artifact.seek(0)
            artifact.write(b"not a sqlite database")
        with pytest.raises(StorageCorruptionError):
            history.snapshot(snapshot.revision)
        with pytest.raises(StorageCorruptionError):
            snapshot.open()


def test_invalid_revision_arguments(tmp_path: Path):
    with SQLiteTreeStore(tmp_path / "live.db") as source:
        history = SQLiteHistory(source, tmp_path / "history")
        with pytest.raises(ValueError):
            history.snapshot(-1)
        with pytest.raises(ValueError):
            history.snapshot(True)
