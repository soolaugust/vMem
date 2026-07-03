"""
test_session_replay.py — Session Replay (ftrace ring buffer) 测试

验证事件录制、回放、GC 功能。
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timezone, timedelta
from memory_os.store.vfs_compat import open_db, ensure_schema
from memory_os.store.episodes import (
    ensure_replay_schema, record_replay_event, replay_session, gc_replay_events
)


import tempfile

def _get_conn():
    """每次测试使用独立临时 DB，避免测试间状态污染。"""
    tmp = tempfile.mktemp(suffix=".db")
    conn = open_db(tmp)
    ensure_schema(conn)
    ensure_replay_schema(conn)
    return conn


class TestRecordReplayEvent:

    def test_basic_insert(self):
        conn = _get_conn()
        rowid = record_replay_event(conn, "sess1", "chunks_injected",
                                     data={"count": 3}, chunk_ids=["a", "b"])
        assert rowid > 0
        conn.close()

    def test_no_data(self):
        conn = _get_conn()
        rowid = record_replay_event(conn, "sess1", "query_received")
        assert rowid > 0
        conn.close()

    def test_data_truncated(self):
        conn = _get_conn()
        big_data = {"text": "x" * 5000}
        record_replay_event(conn, "sess1", "test", data=big_data)
        row = conn.execute("SELECT data FROM replay_events WHERE id=1").fetchone()
        assert len(row[0]) <= 2100  # 2000 + "..."
        conn.close()


class TestReplaySession:

    def test_returns_events_in_order(self):
        conn = _get_conn()
        record_replay_event(conn, "sess1", "query_received", data={"q": "test1"})
        record_replay_event(conn, "sess1", "chunks_injected", data={"count": 2})
        record_replay_event(conn, "sess1", "chunks_extracted", data={"count": 1})
        events = replay_session(conn, "sess1")
        assert len(events) == 3
        assert events[0]["event_type"] == "query_received"
        assert events[2]["event_type"] == "chunks_extracted"
        conn.close()

    def test_filter_by_event_type(self):
        conn = _get_conn()
        record_replay_event(conn, "sess2", "query_received")
        record_replay_event(conn, "sess2", "chunks_injected")
        record_replay_event(conn, "sess2", "chunks_extracted")
        events = replay_session(conn, "sess2", event_types=["chunks_injected"])
        assert len(events) == 1
        assert events[0]["event_type"] == "chunks_injected"
        conn.close()

    def test_nonexistent_session_empty(self):
        conn = _get_conn()
        events = replay_session(conn, "nonexistent_session")
        assert events == []
        conn.close()

    def test_chunk_ids_parsed(self):
        conn = _get_conn()
        record_replay_event(conn, "sess3", "chunks_injected",
                             chunk_ids=["id1", "id2", "id3"])
        events = replay_session(conn, "sess3")
        assert events[0]["chunk_ids"] == ["id1", "id2", "id3"]
        conn.close()

    def test_data_parsed_as_dict(self):
        conn = _get_conn()
        record_replay_event(conn, "sess4", "test", data={"key": "value", "n": 42})
        events = replay_session(conn, "sess4")
        assert events[0]["data"]["key"] == "value"
        assert events[0]["data"]["n"] == 42
        conn.close()

    def test_duration_ms_stored(self):
        conn = _get_conn()
        record_replay_event(conn, "sess5", "test", duration_ms=12.5)
        events = replay_session(conn, "sess5")
        assert events[0]["duration_ms"] == 12.5
        conn.close()


class TestGcReplayEvents:

    def test_gc_deletes_old_events(self):
        conn = _get_conn()
        old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        conn.execute(
            "INSERT INTO replay_events (session_id, event_type, timestamp) VALUES (?, ?, ?)",
            ("old_sess", "test", old_ts)
        )
        conn.commit()
        record_replay_event(conn, "new_sess", "test")
        deleted = gc_replay_events(conn, max_age_days=30)
        assert deleted == 1
        remaining = conn.execute("SELECT COUNT(*) FROM replay_events").fetchone()[0]
        assert remaining == 1
        conn.close()

    def test_gc_no_old_events(self):
        conn = _get_conn()
        record_replay_event(conn, "recent", "test")
        deleted = gc_replay_events(conn, max_age_days=30)
        assert deleted == 0
        conn.close()
