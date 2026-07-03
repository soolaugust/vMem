"""
test_supersession.py — Contradiction Detection (KSM 超取代) 测试

验证 Jaccard 近似内容检测 + supersede_chunk 联动。

⚠️ 已漂移（2026-06-05 发现）：本测试引用的私有 API _tokenize_for_jaccard /
_detect_supersession 已在重构中移除，被公开 API supersede_chunk /
get_superseded_ids 取代（语义从"自动检测高重叠取代"改为"显式标记取代"）。
旧测试不再反映当前设计，整体 skip 以解除全量 collect 阻塞；保留为路标，
需按新 API 重写（不要删除——删除会丢失"此处应有 supersession 测试"的信息）。

这正是"缺自动验证触发器"的活体证据：370 测试无 CI/pre-commit，
重构删了被测函数而测试无人发现，漂移持续至全量 collect 才暴露。
"""
import sys, os
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytestmark = pytest.mark.skip(
    reason="API drifted: _tokenize_for_jaccard/_detect_supersession removed; "
           "rewrite against supersede_chunk/get_superseded_ids"
)

from datetime import datetime, timezone
from memory_os.store.vfs_compat import open_db, ensure_schema, insert_chunk
from memory_os.core.schema import MemoryChunk

# 漂移占位：旧私有 API 已移除。skip 模块下这些名字不会被调用，
# 但保留导入处的 import 失败防护——直接给 None 占位避免 collect 阶段 NameError。
_tokenize_for_jaccard = None
_detect_supersession = None


def _get_conn():
    db = os.environ.get("MEMORY_OS_DB", ":memory:")
    conn = open_db(db)
    ensure_schema(conn)
    return conn


def _make_chunk(summary, chunk_type="decision", importance=0.7, project="test_proj"):
    now = datetime.now(timezone.utc).isoformat()
    c = MemoryChunk(
        chunk_type=chunk_type, summary=summary, importance=importance,
        project=project, created_at=now, updated_at=now, last_accessed=now,
        content=summary + " extended content for testing purposes",
    )
    return c.to_dict()


class TestTokenizeForJaccard:

    def test_english_words(self):
        tokens = _tokenize_for_jaccard("hello world test")
        assert "hello" in tokens
        assert "world" in tokens

    def test_chinese_bigrams(self):
        tokens = _tokenize_for_jaccard("决策使用BM25检索")
        assert "决策" in tokens
        assert "使用" in tokens
        assert "bm25" in tokens

    def test_empty_string(self):
        assert _tokenize_for_jaccard("") == set()


class TestDetectSupersession:

    def test_high_overlap_supersedes(self):
        conn = _get_conn()
        old = _make_chunk("使用 BM25 检索算法进行全文搜索并排序结果")
        insert_chunk(conn, old)
        new = _make_chunk("使用 BM25 检索算法进行全文搜索并排序结果返回")
        insert_chunk(conn, new)
        superseded = _detect_supersession(conn, new)
        assert old["id"] in superseded
        conn.close()

    def test_low_overlap_no_supersession(self):
        conn = _get_conn()
        old = _make_chunk("使用 Redis 做缓存层加速查询")
        insert_chunk(conn, old)
        new = _make_chunk("SQLite WAL 模式适合写少读多的场景")
        insert_chunk(conn, new)
        superseded = _detect_supersession(conn, new)
        assert len(superseded) == 0
        conn.close()

    def test_different_project_no_supersession(self):
        conn = _get_conn()
        old = _make_chunk("使用 BM25 检索算法进行全文搜索", project="proj_a")
        insert_chunk(conn, old)
        new = _make_chunk("使用 BM25 检索算法进行全文搜索", project="proj_b")
        insert_chunk(conn, new)
        superseded = _detect_supersession(conn, new)
        assert len(superseded) == 0
        conn.close()

    def test_different_chunk_type_no_supersession(self):
        conn = _get_conn()
        old = _make_chunk("使用 BM25 检索算法进行全文搜索", chunk_type="decision")
        insert_chunk(conn, old)
        new = _make_chunk("使用 BM25 检索算法进行全文搜索", chunk_type="reasoning_chain")
        insert_chunk(conn, new)
        superseded = _detect_supersession(conn, new)
        assert len(superseded) == 0
        conn.close()

    def test_superseded_chunk_importance_halved(self):
        conn = _get_conn()
        old = _make_chunk("设计决策：采用 Ebbinghaus 遗忘曲线作为衰减模型进行排序评分", importance=0.8)
        insert_chunk(conn, old)
        new = _make_chunk("设计决策：采用 Ebbinghaus 遗忘曲线作为衰减模型进行排序评分计算", importance=0.8)
        insert_chunk(conn, new)
        _detect_supersession(conn, new)
        row = conn.execute(
            "SELECT importance, oom_adj FROM memory_chunks WHERE id=?", (old["id"],)
        ).fetchone()
        assert row[0] <= 0.5  # importance halved
        assert row[1] >= 200  # oom_adj bumped
        conn.close()

    def test_short_summary_skipped(self):
        """summary 太短（<3 tokens）不触发检测。"""
        conn = _get_conn()
        old = _make_chunk("ok")
        insert_chunk(conn, old)
        new = _make_chunk("ok")
        new["id"] = "different_id"
        insert_chunk(conn, new)
        superseded = _detect_supersession(conn, new)
        assert len(superseded) == 0
        conn.close()
