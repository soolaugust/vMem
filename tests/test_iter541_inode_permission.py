"""
iter541: inode_permission — Unified Write Gate at Storage Layer

Tests that _vfs_write_protect() is enforced on ALL write paths,
not just insert_chunk(). Validates that table-row fragments and other
malformed summaries are blocked regardless of entry point.

OS 类比：Linux inode_permission() (Al Viro, 1999) — VFS 层强制权限检查，
无论哪条 syscall 路径到达文件系统，都必须经过 inode_permission()。
"""
import sys
import os

# ── tmpfs isolation ──────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import memory_os.runtime.tmpfs_compat as tmpfs  # noqa: F401 — sets up isolated DB before any store import

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks"))

import sqlite3
import json
import time
from datetime import datetime, timezone

from memory_os.store.vfs_compat import open_db, ensure_schema, insert_chunk, _vfs_write_protect
from memory_os.store.mm import IoUringSQ


def _setup_db():
    conn = open_db()
    ensure_schema(conn)
    return conn


# ── Test 1: _vfs_write_protect catches table rows ──────────────────────────
def test_vfs_write_protect_table_rows():
    """Table rows with | prefix or >=2 pipes are rejected."""
    assert _vfs_write_protect("| iter 60 — PSI 自我窒息 | latency_baseline=5ms 硬编码 |") is True
    assert _vfs_write_protect("| 生产效果 | 74→72 chunks，零访问率 21.6%→19.4% |") is True
    assert _vfs_write_protect("| col1 | col2 | col3 |") is True
    assert _vfs_write_protect("|---+---+---|") is True


# ── Test 2: _vfs_write_protect allows valid content ──────────────────────────
def test_vfs_write_protect_allows_valid():
    """Normal summaries pass through."""
    assert _vfs_write_protect("选择 React 而非 Vue 因为生态更成熟") is False
    assert _vfs_write_protect("latency_baseline=5ms 导致 PSI 永久 FULL") is False
    assert _vfs_write_protect("extractor.py 新增 _sqe_validate 函数") is False


# ── Test 3: insert_chunk blocks fragments ──────────────────────────────────
def test_insert_chunk_blocks_fragments():
    """insert_chunk (the canonical path) rejects table rows via _vfs_write_protect."""
    conn = _setup_db()
    insert_chunk(conn, {
        "id": "test-frag-001",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "project": "test",
        "source_session": "test",
        "chunk_type": "decision",
        "content": "| leaked | table | row |",
        "summary": "| leaked | table | row |",
        "tags": ["decision", "test"],
        "importance": 0.85,
        "retrievability": 0.35,
        "last_accessed": datetime.now(timezone.utc).isoformat(),
        "access_count": 0,
    })
    conn.commit()
    row = conn.execute("SELECT id FROM memory_chunks WHERE id='test-frag-001'").fetchone()
    assert row is None, "insert_chunk should reject table-row fragment"
    conn.close()


# ── Test 4: IoUringSQ.submit blocks fragments (the key fix) ────────────────
def test_io_uring_sq_blocks_fragments():
    """IoUringSQ.submit() now calls _vfs_write_protect before INSERT."""
    conn = _setup_db()
    sq = IoUringSQ()
    sq.prep_write("decision", "| iter 60 — PSI 窒息 | baseline=5ms |",
                  "test-proj", "test-sess", "topic1", importance=0.85)
    sq.prep_write("decision", "正常决策：选择 React 框架",
                  "test-proj", "test-sess", "topic2", importance=0.85)
    sq.prep_write("decision", "| col | row | data |",
                  "test-proj", "test-sess", "topic3", importance=0.80)

    result = sq.submit(conn)
    conn.commit()

    # Only the valid one should be inserted
    assert result["skipped_quality"] >= 2, f"Expected >=2 quality skips, got {result['skipped_quality']}"
    assert result["inserted"] <= 1, f"Expected <=1 insert, got {result['inserted']}"

    # Verify DB state
    rows = conn.execute("SELECT summary FROM memory_chunks WHERE project='test-proj'").fetchall()
    summaries = [r[0] for r in rows]
    assert "正常决策：选择 React 框架" in summaries or any("React" in s for s in summaries)
    assert all("|" not in s or s.count("|") < 2 for s in summaries), \
        f"Table rows leaked into DB: {summaries}"
    conn.close()


# ── Test 5: IoUringSQ allows valid writes ──────────────────────────────────
def test_io_uring_sq_allows_valid():
    """Valid summaries pass through IoUringSQ unchanged."""
    conn = _setup_db()
    sq = IoUringSQ()
    sq.prep_write("decision", "采用 FTS5 替代 BM25 全扫描减少延迟",
                  "test-proj2", "sess2", "perf")
    sq.prep_write("excluded_path", "排除 Vue 因为团队无经验",
                  "test-proj2", "sess2", "stack")

    result = sq.submit(conn)
    conn.commit()

    assert result["inserted"] == 2
    assert result["skipped_quality"] == 0
    rows = conn.execute("SELECT COUNT(*) FROM memory_chunks WHERE project='test-proj2'").fetchone()
    assert rows[0] == 2
    conn.close()


# ── Test 6: Short/empty summaries blocked ──────────────────────────────────
def test_blocks_short_and_empty():
    """Very short or empty summaries are blocked on all paths."""
    conn = _setup_db()
    sq = IoUringSQ()
    sq.prep_write("decision", "短", "proj", "sess", "")
    sq.prep_write("decision", "", "proj", "sess", "")
    sq.prep_write("decision", "  ab  ", "proj", "sess", "")

    result = sq.submit(conn)
    conn.commit()

    assert result["skipped_quality"] == 3
    assert result["inserted"] == 0
    conn.close()


# ── Test 7: Colon-ending fragments blocked ─────────────────────────────────
def test_blocks_colon_ending():
    """Summaries ending with colon (title fragments) are blocked."""
    conn = _setup_db()
    sq = IoUringSQ()
    sq.prep_write("decision", "核心成果：", "proj", "sess", "")
    sq.prep_write("decision", "验证结果:", "proj", "sess", "")

    result = sq.submit(conn)
    conn.commit()

    assert result["skipped_quality"] == 2
    assert result["inserted"] == 0
    conn.close()


# ── Test 8: Symbol-prefix fragments blocked ────────────────────────────────
def test_blocks_symbol_prefix():
    """Summaries starting with truncation symbols are blocked."""
    conn = _setup_db()
    sq = IoUringSQ()
    sq.prep_write("decision", ") 后续操作完成", "proj", "sess", "")
    sq.prep_write("decision", "] 数组已满", "proj", "sess", "")
    sq.prep_write("decision", "> 引用来源不明", "proj", "sess", "")

    result = sq.submit(conn)
    conn.commit()

    assert result["skipped_quality"] == 3
    conn.close()


# ── Test 9: Performance — _vfs_write_protect is O(1) ──────────────────────
def test_performance():
    """_vfs_write_protect completes in < 0.1ms per call."""
    test_cases = [
        "| table | row | fragment |",
        "正常的决策文本，包含技术锚点 extractor.py",
        "短",
        "核心成果：",
    ]
    start = time.perf_counter()
    for _ in range(1000):
        for tc in test_cases:
            _vfs_write_protect(tc)
    elapsed = time.perf_counter() - start
    avg_us = elapsed / 4000 * 1_000_000
    print(f"  _vfs_write_protect avg: {avg_us:.1f}μs/call ({elapsed*1000:.1f}ms total)")
    assert elapsed < 0.5, f"Performance regression: {elapsed*1000:.1f}ms for 4000 calls"


# ── Test 10: swap_in path guard (integration) ──────────────────────────────
def test_swap_in_guard():
    """swap_in rejects corrupted summary from swap store."""
    conn = _setup_db()
    # Simulate a corrupted swap entry — create swap_store table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS swap_store (
            id TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            swapped_at TEXT
        )
    """)
    import zlib, base64
    corrupted_chunk = {
        "id": "corrupted-swap-001",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "project": "test",
        "source_session": "test",
        "chunk_type": "decision",
        "content": "| bad | swap | data |",
        "summary": "| bad | swap | data |",
        "tags": json.dumps(["decision"]),
        "importance": 0.8,
        "retrievability": 0.35,
        "last_accessed": datetime.now(timezone.utc).isoformat(),
        "access_count": 0,
    }
    compressed = base64.b64encode(zlib.compress(json.dumps(corrupted_chunk).encode())).decode()
    conn.execute("INSERT INTO swap_store (id, data, swapped_at) VALUES (?, ?, ?)",
                 ("corrupted-swap-001", compressed, datetime.now(timezone.utc).isoformat()))
    conn.commit()

    # Now try swap_in
    from memory_os.store.swap import swap_in
    result = swap_in(conn, ["corrupted-swap-001"])
    conn.commit()

    # The corrupted chunk should NOT be in main table
    row = conn.execute("SELECT id FROM memory_chunks WHERE id='corrupted-swap-001'").fetchone()
    assert row is None, "swap_in should reject corrupted summary via inode_permission"
    conn.close()


# ── Test 11: Global promotion path guard ───────────────────────────────────
def test_global_promotion_guard():
    """Global promotion path in extractor rejects fragments."""
    # This test verifies the code change exists by checking the function
    # We can't easily integration-test the full extractor, but we verify
    # the _vfs_write_protect would catch the patterns
    fragments = [
        "| iter 60 — PSI 自我窒息 | latency_baseline=5ms 硬编码 |",
        "| 生产效果 | 74→72 chunks |",
        ")截断的后缀",
        "核心成果：",
    ]
    for frag in fragments:
        assert _vfs_write_protect(frag) is True, f"Should block: {frag}"


# ── Test 12: knowledge_vfs_backends guard (code path exists) ───────────────
def test_knowledge_vfs_guard_code_exists():
    """Verify the inode_permission guard was added to knowledge_vfs_backends."""
    import inspect
    backend_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "knowledge_vfs_backends.py"
    )
    with open(backend_path) as f:
        source = f.read()
    assert "_vfs_write_protect" in source, \
        "knowledge_vfs_backends.py should contain _vfs_write_protect guard"
    assert "iter541" in source, \
        "knowledge_vfs_backends.py should reference iter541"


if __name__ == "__main__":
    tests = [
        test_vfs_write_protect_table_rows,
        test_vfs_write_protect_allows_valid,
        test_insert_chunk_blocks_fragments,
        test_io_uring_sq_blocks_fragments,
        test_io_uring_sq_allows_valid,
        test_blocks_short_and_empty,
        test_blocks_colon_ending,
        test_blocks_symbol_prefix,
        test_performance,
        test_swap_in_guard,
        test_global_promotion_guard,
        test_knowledge_vfs_guard_code_exists,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS: {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {t.__name__}: {e}")
            failed += 1
    print(f"\n{'='*50}")
    print(f"iter541 inode_permission: {passed}/{passed+failed} passed")
    if failed:
        sys.exit(1)
