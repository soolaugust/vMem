"""
test_content_hash.py — 内容寻址去重（借鉴 komi-learn content-addressing）

验证：
  T1: compute_content_hash 对相同语义内容产生相同指纹
  T2: 内容改一字 → 指纹不同
  T3: tags 顺序无关（内部 sorted）
  T4: 排除易变元数据（importance/access_count/stability/id/时间戳）→ 指纹不变
  T5: project 隔离 — 同内容不同 project 指纹不同（避免跨项目误并）
  T6: schema 列存在 + 索引可用
  T7: 快通道 SQL 语义 — content_hash 命中时 UPDATE 加固（stability*1.5, access+1）
       而非新建（直接验证逻辑，因 tmpfs 测试环境跳过 insert_chunk 内的快通道 gate）
"""
import sys
import os
from pathlib import Path
from datetime import datetime, timezone

_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT.parent))

import pytest
import memory_os.runtime.tmpfs_compat as tmpfs  # noqa: F401
from memory_os.core.schema import MemoryChunk, compute_content_hash
from memory_os.store.vfs_compat import open_db, ensure_schema, insert_chunk


@pytest.fixture(autouse=True)
def _close_conns_after_test():
    yield
    while _OPEN_CONNS:
        try:
            _OPEN_CONNS.pop().close()
        except Exception:
            pass


_OPEN_CONNS = []


def _get_conn():
    # 关闭上一个连接，避免共享 WAL 文件 DB 上多连接残留写锁（test-only 隔离）
    while _OPEN_CONNS:
        try:
            _OPEN_CONNS.pop().close()
        except Exception:
            pass
    db = os.environ.get("MEMORY_OS_DB", ":memory:")
    conn = open_db(db)
    ensure_schema(conn)
    conn.execute("PRAGMA busy_timeout=5000")
    _OPEN_CONNS.append(conn)
    return conn


class TestComputeContentHash:
    """T1-T5: 纯函数性质"""

    def test_same_content_same_hash(self):
        h1 = compute_content_hash("decision", "world", "proj", "选 BM25", "因为更快", ["a"])
        h2 = compute_content_hash("decision", "world", "proj", "选 BM25", "因为更快", ["a"])
        assert h1 == h2
        assert len(h1) == 32  # blake2b digest_size=16 → 32 hex chars

    def test_content_diff_hash_diff(self):
        h1 = compute_content_hash("decision", "world", "proj", "选 BM25", "因为更快", [])
        h2 = compute_content_hash("decision", "world", "proj", "选 BM25", "因为更慢", [])
        assert h1 != h2

    def test_tag_order_irrelevant(self):
        h1 = compute_content_hash("decision", "world", "proj", "s", "c", ["x", "y", "z"])
        h2 = compute_content_hash("decision", "world", "proj", "s", "c", ["z", "x", "y"])
        assert h1 == h2

    def test_tag_case_whitespace_normalized(self):
        h1 = compute_content_hash("decision", "world", "proj", "s", "c", ["Foo", " Bar "])
        h2 = compute_content_hash("decision", "world", "proj", "s", "c", ["foo", "bar"])
        assert h1 == h2

    def test_summary_case_normalized(self):
        h1 = compute_content_hash("decision", "world", "proj", "Hello", "World", [])
        h2 = compute_content_hash("decision", "world", "proj", "hello", "world", [])
        assert h1 == h2

    def test_project_isolation(self):
        h1 = compute_content_hash("decision", "world", "projA", "s", "c", [])
        h2 = compute_content_hash("decision", "world", "projB", "s", "c", [])
        assert h1 != h2

    def test_chunk_type_affects_hash(self):
        h1 = compute_content_hash("decision", "world", "proj", "s", "c", [])
        h2 = compute_content_hash("reasoning_chain", "world", "proj", "s", "c", [])
        assert h1 != h2


class TestMemoryChunkHash:
    """T4: dataclass 方法 — 排除易变元数据"""

    def test_excludes_volatile_metadata(self):
        now = datetime.now(timezone.utc).isoformat()
        c1 = MemoryChunk(chunk_type="decision", summary="s", content="c",
                         project="p", importance=0.5, stability=1.0)
        c2 = MemoryChunk(chunk_type="decision", summary="s", content="c",
                         project="p", importance=0.99, stability=42.0,
                         last_accessed=now)
        # importance/stability/last_accessed/id 不参与哈希
        assert c1.compute_content_hash() == c2.compute_content_hash()

    def test_finalize_sets_hash(self):
        c = MemoryChunk(chunk_type="decision", summary="s", content="c", project="p")
        assert c.content_hash == ""
        c.finalize()
        assert c.content_hash != ""
        assert c.content_hash == c.compute_content_hash()

    def test_to_from_dict_roundtrip(self):
        c = MemoryChunk(chunk_type="decision", summary="s", content="c", project="p").finalize()
        d = c.to_dict()
        assert d["content_hash"] == c.content_hash
        c2 = MemoryChunk.from_dict(d)
        assert c2.content_hash == c.content_hash


class TestSchemaColumn:
    """T6: 列与索引"""

    def test_column_exists(self):
        conn = _get_conn()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_chunks)").fetchall()}
        assert "content_hash" in cols

    def test_index_exists(self):
        conn = _get_conn()
        idx = {r[1] for r in conn.execute("PRAGMA index_list(memory_chunks)").fetchall()}
        assert "idx_mc_content_hash" in idx

    def test_insert_chunk_populates_hash(self):
        """insert_chunk 即使在 tmpfs 跳过快通道，也应填充 content_hash 列。"""
        conn = _get_conn()
        now = datetime.now(timezone.utc).isoformat()
        c = MemoryChunk(chunk_type="decision",
                        summary="内容寻址测试 unique summary 12345",
                        content="完整正文，足够长以通过密度 gate，验证 content_hash 列被写入数据库中",
                        project="ch_test_proj", importance=0.8,
                        created_at=now, updated_at=now, last_accessed=now)
        insert_chunk(conn, c.to_dict())
        row = conn.execute(
            "SELECT content_hash FROM memory_chunks WHERE summary=?",
            ("内容寻址测试 unique summary 12345",)
        ).fetchone()
        assert row is not None
        assert row[0] == c.compute_content_hash()


class TestFastPathLogic:
    """T7: 快通道 SQL 语义 — 命中加固而非新建（直接验证逻辑）"""

    def test_hit_reinforces_not_duplicates(self):
        conn = _get_conn()
        now = datetime.now(timezone.utc).isoformat()
        chash = compute_content_hash("decision", "world", "fp_proj", "summ", "body", [])
        # 预置一条已有 chunk
        conn.execute(
            "INSERT INTO memory_chunks (id, created_at, updated_at, project, "
            "source_session, chunk_type, info_class, content, summary, tags, "
            "importance, retrievability, last_accessed, access_count, stability, content_hash) "
            "VALUES ('fp1',?,?,'fp_proj','','decision','world','body','summ','[]',0.5,0.5,?,3,2.0,?)",
            (now, now, now, chash),
        )
        conn.commit()
        # 模拟快通道：相同 hash 命中 → 加固
        hit = conn.execute(
            "SELECT id FROM memory_chunks WHERE content_hash=? AND project=? LIMIT 1",
            (chash, "fp_proj"),
        ).fetchone()
        assert hit is not None and hit[0] == "fp1"
        conn.execute(
            "UPDATE memory_chunks SET importance=MAX(importance,?), "
            "stability=stability*1.5, access_count=access_count+1, "
            "last_accessed=?, updated_at=? WHERE id=?",
            (0.9, now, now, "fp1"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT importance, stability, access_count FROM memory_chunks WHERE id='fp1'"
        ).fetchone()
        assert abs(row[0] - 0.9) < 1e-9       # importance 提升到 0.9
        assert abs(row[1] - 3.0) < 1e-9       # stability 2.0 * 1.5
        assert row[2] == 4                     # access_count 3 + 1
        # 仍只有一条（未新建）
        cnt = conn.execute(
            "SELECT COUNT(*) FROM memory_chunks WHERE content_hash=? AND project='fp_proj'",
            (chash,)
        ).fetchone()[0]
        assert cnt == 1
