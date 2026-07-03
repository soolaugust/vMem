"""
test_chunk_aggregation.py — Chunk Aggregation (hugepages) 测试

验证 cluster 发现、聚合写入、原始 chunk 降级。
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tempfile
from datetime import datetime, timezone
from memory_os.store.vfs_compat import open_db, ensure_schema, insert_chunk
from memory_os.store.graph import ensure_graph_schema, add_edge, EdgeType
from memory_os.store.aggregate import find_clusters, aggregate_cluster, aggregate_related_chunks
from memory_os.core.schema import MemoryChunk


def _get_conn():
    tmp = tempfile.mktemp(suffix=".db")
    conn = open_db(tmp)
    ensure_schema(conn)
    ensure_graph_schema(conn)
    return conn


def _insert(conn, summary, project="test", importance=0.7):
    now = datetime.now(timezone.utc).isoformat()
    c = MemoryChunk(
        chunk_type="decision", summary=summary, importance=importance,
        project=project, created_at=now, updated_at=now,
        last_accessed=now, content=summary + " extended content",
    ).to_dict()
    insert_chunk(conn, c)
    return c["id"]


class TestFindClusters:

    def test_basic_cluster(self):
        conn = _get_conn()
        ids = [_insert(conn, f"决策{i}：使用方案{i}进行系统设计") for i in range(4)]
        add_edge(conn, ids[0], ids[1], EdgeType.RELATED, 0.5, "test")
        add_edge(conn, ids[1], ids[2], EdgeType.RELATED, 0.5, "test")
        add_edge(conn, ids[2], ids[3], EdgeType.RELATED, 0.5, "test")
        clusters = find_clusters(conn, "test", min_cluster_size=3)
        assert len(clusters) >= 1
        assert len(clusters[0]["chunk_ids"]) >= 3
        conn.close()

    def test_no_edges_no_clusters(self):
        conn = _get_conn()
        for i in range(5):
            _insert(conn, f"isolated chunk {i}")
        clusters = find_clusters(conn, "test")
        assert len(clusters) == 0
        conn.close()

    def test_small_group_filtered(self):
        conn = _get_conn()
        id_a = _insert(conn, "chunk a small group test")
        id_b = _insert(conn, "chunk b small group test")
        add_edge(conn, id_a, id_b, EdgeType.RELATED, 0.5, "test")
        clusters = find_clusters(conn, "test", min_cluster_size=3)
        assert len(clusters) == 0
        conn.close()

    def test_contradicts_excluded(self):
        conn = _get_conn()
        ids = [_insert(conn, f"决策{i}：矛盾测试方案{i}") for i in range(3)]
        add_edge(conn, ids[0], ids[1], EdgeType.CONTRADICTS, 0.9, "test")
        add_edge(conn, ids[1], ids[2], EdgeType.CONTRADICTS, 0.9, "test")
        clusters = find_clusters(conn, "test", min_cluster_size=3)
        assert len(clusters) == 0
        conn.close()


class TestAggregateCluster:

    def test_creates_composite_chunk(self):
        conn = _get_conn()
        ids = [_insert(conn, f"方案{i}：详细的系统设计决策描述内容") for i in range(3)]
        add_edge(conn, ids[0], ids[1], EdgeType.RELATED, 0.5, "test")
        add_edge(conn, ids[1], ids[2], EdgeType.RELATED, 0.5, "test")
        clusters = find_clusters(conn, "test", min_cluster_size=3)
        assert len(clusters) >= 1
        cid = aggregate_cluster(conn, clusters[0], "test")
        assert cid is not None
        row = conn.execute(
            "SELECT chunk_type, summary FROM memory_chunks WHERE id=?", (cid,)
        ).fetchone()
        assert row[0] == "composite"
        assert "聚合×" in row[1]
        conn.close()

    def test_original_chunks_demoted(self):
        conn = _get_conn()
        ids = [_insert(conn, f"降级测试{i}：决策方案内容描述，包含完整的技术细节和理由说明") for i in range(3)]
        add_edge(conn, ids[0], ids[1], EdgeType.RELATED, 0.5, "test")
        add_edge(conn, ids[1], ids[2], EdgeType.RELATED, 0.5, "test")
        clusters = find_clusters(conn, "test", min_cluster_size=3)
        aggregate_cluster(conn, clusters[0], "test")
        for cid in ids:
            row = conn.execute(
                "SELECT oom_adj FROM memory_chunks WHERE id=?", (cid,)
            ).fetchone()
            if row:
                assert row[0] >= 100
        conn.close()


class TestAggregateRelatedChunks:

    def test_end_to_end(self):
        conn = _get_conn()
        ids = [_insert(conn, f"E2E测试{i}：系统架构决策方案详情") for i in range(4)]
        add_edge(conn, ids[0], ids[1], EdgeType.CAUSES, 0.7, "test")
        add_edge(conn, ids[1], ids[2], EdgeType.REQUIRES, 0.6, "test")
        add_edge(conn, ids[2], ids[3], EdgeType.RELATED, 0.5, "test")
        results = aggregate_related_chunks(conn, "test", min_cluster_size=3)
        assert len(results) >= 1
        conn.close()

    def test_empty_project(self):
        conn = _get_conn()
        results = aggregate_related_chunks(conn, "nonexistent")
        assert results == []
        conn.close()
