"""
test_graph_export.py — Graph Relationship Export 测试

验证 export_subgraph_compact() 的关系图导出。
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tempfile
from datetime import datetime, timezone
from memory_os.store.vfs_compat import open_db, ensure_schema, insert_chunk
from memory_os.store.graph import (
    ensure_graph_schema, add_edge, EdgeType, export_subgraph_compact
)
from memory_os.core.schema import MemoryChunk


def _get_conn():
    tmp = tempfile.mktemp(suffix=".db")
    conn = open_db(tmp)
    ensure_schema(conn)
    ensure_graph_schema(conn)
    return conn


def _make_and_insert(conn, summary, chunk_id=None):
    now = datetime.now(timezone.utc).isoformat()
    c = MemoryChunk(
        chunk_type="decision", summary=summary, importance=0.7,
        project="test", created_at=now, updated_at=now,
        last_accessed=now, content=summary + " content",
    ).to_dict()
    if chunk_id:
        c["id"] = chunk_id
    insert_chunk(conn, c)
    return c["id"]


class TestExportSubgraphCompact:

    def test_basic_edge_export(self):
        conn = _get_conn()
        id_a = _make_and_insert(conn, "使用 SQLite WAL 模式", "chunk_a")
        id_b = _make_and_insert(conn, "避免锁竞争问题", "chunk_b")
        add_edge(conn, id_a, id_b, EdgeType.CAUSES, 0.8, "test")
        result = export_subgraph_compact(conn, [id_a, id_b])
        assert "[关系图]" in result
        assert "CAUSES" in result
        conn.close()

    def test_no_edges_returns_empty(self):
        conn = _get_conn()
        id_a = _make_and_insert(conn, "chunk a")
        id_b = _make_and_insert(conn, "chunk b")
        result = export_subgraph_compact(conn, [id_a, id_b])
        assert result == ""
        conn.close()

    def test_single_chunk_returns_empty(self):
        conn = _get_conn()
        id_a = _make_and_insert(conn, "chunk a")
        result = export_subgraph_compact(conn, [id_a])
        assert result == ""
        conn.close()

    def test_empty_ids_returns_empty(self):
        conn = _get_conn()
        result = export_subgraph_compact(conn, [])
        assert result == ""
        conn.close()

    def test_multiple_edges(self):
        conn = _get_conn()
        id_a = _make_and_insert(conn, "模块 A", "ca")
        id_b = _make_and_insert(conn, "模块 B", "cb")
        id_c = _make_and_insert(conn, "模块 C", "cc")
        add_edge(conn, id_a, id_b, EdgeType.CAUSES, 0.8, "test")
        add_edge(conn, id_b, id_c, EdgeType.REQUIRES, 0.7, "test")
        add_edge(conn, id_a, id_c, EdgeType.RELATED, 0.5, "test")
        result = export_subgraph_compact(conn, [id_a, id_b, id_c])
        assert result.count("-->") == 3
        conn.close()

    def test_max_edges_limit(self):
        conn = _get_conn()
        ids = []
        for i in range(5):
            ids.append(_make_and_insert(conn, f"chunk {i}", f"c{i}"))
        for i in range(4):
            add_edge(conn, ids[i], ids[i + 1], EdgeType.RELATED, 0.5, "test")
        result = export_subgraph_compact(conn, ids, max_edges=2)
        assert result.count("-->") == 2
        conn.close()

    def test_labels_from_summary(self):
        conn = _get_conn()
        id_a = _make_and_insert(conn, "SQLite WAL 决策选择持久化方案")
        id_b = _make_and_insert(conn, "BM25 检索选择全文搜索方案")
        add_edge(conn, id_a, id_b, EdgeType.REQUIRES, 0.7, "test")
        result = export_subgraph_compact(conn, [id_a, id_b])
        assert "SQLite" in result or "BM25" in result
        conn.close()
