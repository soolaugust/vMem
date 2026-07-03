"""
iter530: put_page — Unified Final Release + Bitmap Scrub

测试 put_page() 函数，验证：
1. Phase 1: imp=0 + acc>0 zombie 被强制删除（UE Force Kill）
2. Phase 2: oom_adj=1000 + imp<0.3 直接删除 / imp>0.3 降级
3. Phase 3: page_idle bitmap stale entries 被清理
4. 保护机制：mlock / task_state 豁免
5. 空 DB / 正常 DB 安全处理
"""
import sys
import os
import json
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone, timedelta

# tmpfs 测试隔离
_tmpdir = tempfile.mkdtemp(prefix="test_iter530_")
os.environ["MEMORY_OS_DIR"] = _tmpdir
os.environ["MEMORY_OS_DB"] = os.path.join(_tmpdir, "store.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from memory_os.store.core import open_db, ensure_schema, insert_chunk, OOM_ADJ_MAX
from memory_os.store.mm import put_page, _page_idle_load, _page_idle_save, _PAGE_IDLE_FILE
import unittest


PROJECT = "test_put_page"


def _make_chunk(conn, summary, importance=0.5, access_count=0,
                oom_adj=0, chunk_type="decision", project=PROJECT):
    """Helper: insert a test chunk."""
    chunk_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO memory_chunks
           (id, summary, content, chunk_type, source_session, project,
            importance, access_count, oom_adj, created_at, last_accessed, lru_gen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
        (chunk_id, summary, summary, chunk_type, "test", project,
         importance, access_count, oom_adj, now, now)
    )
    # FTS5 同步
    try:
        conn.execute(
            "INSERT INTO memory_chunks_fts(rowid, summary, content) "
            "SELECT rowid, summary, content FROM memory_chunks WHERE id = ?",
            (chunk_id,)
        )
    except Exception:
        pass
    conn.commit()
    return chunk_id


class TestPutPagePhase1UEForceKill(unittest.TestCase):
    """Phase 1: UE (Uncorrectable Error) chunks — imp=0 无条件删除"""

    def setUp(self):
        self.conn = open_db()
        ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_ue_zombie_killed(self):
        """imp=0 + access>0 的 zombie 应被强制删除"""
        cid = _make_chunk(self.conn, "UE zombie with access", importance=0.0,
                          access_count=5, oom_adj=500)
        result = put_page(self.conn, PROJECT)
        self.assertGreaterEqual(result["ue_killed"], 1)
        row = self.conn.execute("SELECT id FROM memory_chunks WHERE id=?", (cid,)).fetchone()
        self.assertIsNone(row, "imp=0 zombie should be deleted regardless of access_count")

    def test_ue_zero_access_killed(self):
        """imp=0 + access=0 也应被删除"""
        cid = _make_chunk(self.conn, "UE no access", importance=0.0, access_count=0)
        result = put_page(self.conn, PROJECT)
        self.assertGreaterEqual(result["ue_killed"], 1)
        row = self.conn.execute("SELECT id FROM memory_chunks WHERE id=?", (cid,)).fetchone()
        self.assertIsNone(row)

    def test_ue_mlock_protected(self):
        """imp=0 但 mlock (oom_adj<=-500) 不被删除"""
        cid = _make_chunk(self.conn, "UE but mlock", importance=0.0,
                          access_count=2, oom_adj=-500)
        result = put_page(self.conn, PROJECT)
        row = self.conn.execute("SELECT id FROM memory_chunks WHERE id=?", (cid,)).fetchone()
        self.assertIsNotNone(row, "mlock chunk should be protected even with imp=0")

    def test_ue_task_state_protected(self):
        """imp=0 但 task_state 类型不删"""
        cid = _make_chunk(self.conn, "UE task_state", importance=0.0,
                          chunk_type="task_state")
        result = put_page(self.conn, PROJECT)
        row = self.conn.execute("SELECT id FROM memory_chunks WHERE id=?", (cid,)).fetchone()
        self.assertIsNotNone(row, "task_state should be protected")


class TestPutPagePhase2OOMMaxReap(unittest.TestCase):
    """Phase 2: OOM_ADJ_MAX chunks — 低 imp 删除, 高 imp 降级"""

    def setUp(self):
        self.conn = open_db()
        ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_oom_max_low_imp_deleted(self):
        """oom_adj=1000 + imp<0.3 → 直接删除"""
        cid = _make_chunk(self.conn, "OOM MAX low imp", importance=0.2,
                          oom_adj=OOM_ADJ_MAX, access_count=3)
        result = put_page(self.conn, PROJECT)
        self.assertGreaterEqual(result["oom_max_reaped"], 1)
        row = self.conn.execute("SELECT id FROM memory_chunks WHERE id=?", (cid,)).fetchone()
        self.assertIsNone(row, "oom_adj=MAX + imp<0.3 should be deleted")

    def test_oom_max_high_imp_demoted(self):
        """oom_adj=1000 + imp=0.58 → 降级到 0.58*0.4=0.232"""
        cid = _make_chunk(self.conn, "OOM MAX high imp", importance=0.58,
                          oom_adj=OOM_ADJ_MAX, access_count=6)
        result = put_page(self.conn, PROJECT)
        self.assertGreaterEqual(result["oom_max_demoted"], 1)
        row = self.conn.execute("SELECT importance FROM memory_chunks WHERE id=?",
                                (cid,)).fetchone()
        self.assertIsNotNone(row)
        self.assertLess(row[0], 0.3, "imp should be demoted to ~0.232")

    def test_oom_max_mlock_protected(self):
        """oom_adj=-500 (mlock) 不受 OOM_MAX reap 影响"""
        cid = _make_chunk(self.conn, "mlock chunk", importance=0.5,
                          oom_adj=-500, access_count=0)
        result = put_page(self.conn, PROJECT)
        row = self.conn.execute("SELECT importance FROM memory_chunks WHERE id=?",
                                (cid,)).fetchone()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row[0], 0.5, places=2)

    def test_normal_oom_adj_untouched(self):
        """oom_adj < 1000 的 chunk 不受 Phase 2 影响"""
        cid = _make_chunk(self.conn, "normal chunk", importance=0.5,
                          oom_adj=500, access_count=0)
        result = put_page(self.conn, PROJECT)
        row = self.conn.execute("SELECT importance FROM memory_chunks WHERE id=?",
                                (cid,)).fetchone()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row[0], 0.5, places=2)


class TestPutPagePhase3BitmapScrub(unittest.TestCase):
    """Phase 3: page_idle bitmap stale entries 清理"""

    def setUp(self):
        self.conn = open_db()
        ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_stale_entries_removed(self):
        """bitmap 中引用已删除 chunk 的条目应被清理"""
        # 创建一个 live chunk
        live_id = _make_chunk(self.conn, "live chunk", importance=0.5)
        # 构造 bitmap: 1 个 live + 2 个 stale
        bitmap = {
            PROJECT: {
                live_id: 3,
                "deleted-chunk-1": 10,
                "deleted-chunk-2": 5,
            }
        }
        _page_idle_save(bitmap)

        result = put_page(self.conn, PROJECT)
        self.assertEqual(result["bitmap_stale_removed"], 2)

        # 验证清理后
        new_bitmap = _page_idle_load()
        self.assertIn(live_id, new_bitmap.get(PROJECT, {}))
        self.assertNotIn("deleted-chunk-1", new_bitmap.get(PROJECT, {}))

    def test_empty_project_removed(self):
        """bitmap 中某 project 全部 stale → project entry 被移除"""
        bitmap = {
            "dead_project": {
                "ghost-1": 5,
                "ghost-2": 3,
            }
        }
        _page_idle_save(bitmap)

        result = put_page(self.conn, PROJECT)
        self.assertEqual(result["bitmap_stale_removed"], 2)

        new_bitmap = _page_idle_load()
        self.assertNotIn("dead_project", new_bitmap)

    def test_no_bitmap_file(self):
        """bitmap 文件不存在时安全处理"""
        if _PAGE_IDLE_FILE.exists():
            _PAGE_IDLE_FILE.unlink()
        result = put_page(self.conn, PROJECT)
        self.assertEqual(result["bitmap_stale_removed"], 0)


class TestPutPageIntegration(unittest.TestCase):
    """集成测试"""

    def setUp(self):
        self.conn = open_db()
        ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_empty_db(self):
        """空 DB 不崩溃"""
        result = put_page(self.conn, PROJECT)
        self.assertEqual(result["ue_killed"], 0)
        self.assertEqual(result["oom_max_reaped"], 0)
        self.assertEqual(result["oom_max_demoted"], 0)

    def test_all_three_phases_combined(self):
        """三个 Phase 在同一次调用中全部执行"""
        # Phase 1 target: UE zombie
        ue_id = _make_chunk(self.conn, "UE combined", importance=0.0, access_count=3)
        # Phase 2 target: OOM_MAX
        oom_id = _make_chunk(self.conn, "OOM MAX combined", importance=0.15,
                             oom_adj=OOM_ADJ_MAX)
        # Phase 3 target: stale bitmap
        live_id = _make_chunk(self.conn, "live combined", importance=0.8)
        bitmap = {PROJECT: {live_id: 1, "stale-combined": 7}}
        _page_idle_save(bitmap)

        result = put_page(self.conn, PROJECT)
        self.assertGreaterEqual(result["ue_killed"], 1)
        self.assertGreaterEqual(result["oom_max_reaped"], 1)
        self.assertEqual(result["bitmap_stale_removed"], 1)

        # live chunk 应幸存
        row = self.conn.execute("SELECT id FROM memory_chunks WHERE id=?",
                                (live_id,)).fetchone()
        self.assertIsNotNone(row, "normal live chunk should survive")

    def test_performance(self):
        """性能：100 chunks + 50 bitmap entries < 100ms"""
        import time
        for i in range(100):
            _make_chunk(self.conn, f"perf chunk {i}", importance=0.5 + i*0.004)
        bitmap = {PROJECT: {f"stale-{i}": 3 for i in range(50)}}
        _page_idle_save(bitmap)

        t0 = time.time()
        result = put_page(self.conn, PROJECT)
        elapsed = (time.time() - t0) * 1000
        self.assertLess(elapsed, 100, f"put_page too slow: {elapsed:.1f}ms")
        self.assertEqual(result["bitmap_stale_removed"], 50)


if __name__ == "__main__":
    unittest.main()
