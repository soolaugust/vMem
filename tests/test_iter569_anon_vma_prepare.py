"""
iter569: anon_vma_prepare — Entity Map Backfill for Orphan Import Chunks

OS 类比：Linux anon_vma_prepare() (Andrea Arcangeli, 2004, mm/rmap.c)
  为迁移页面建立 rmap 基础设施，使其对 page reclaim scanner 可见。

测试覆盖：
  1. orphan_backfilled — 无 entity_map 的 chunk 成功回填
  2. existing_entity_preserved — 已有 entity_map 的 chunk 不重复处理
  3. ghost_skipped — importance=0 的 ghost chunk 不处理
  4. min_entities_filter — 实体过少的 chunk 被跳过
  5. max_backfill_cap — 单次处理上限生效
  6. disabled — 配置关闭时直接返回
  7. entity_extraction_backtick — 反引号内容正确提取
  8. entity_extraction_bracket — 方括号 topic 标签正确提取
  9. entity_extraction_hierarchy — ">" 层级路径叶子节点正确提取
  10. entity_extraction_chinese — 中文双字词正确提取
  11. idempotent — 第二次运行不创建重复条目
  12. project_scoped — project 过滤器生效
  13. global_included — global 层 chunk 被包含
  14. multiple_orphans — 多个孤儿 chunk 同时回填
  15. content_used — content[:200] 参与实体提取
  16. performance — 100 orphan chunks < 200ms
"""
import os
import sys
import sqlite3
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("MEMORY_OS_TESTING", "1")

from memory_os.store.mm import anon_vma_prepare, _anon_vma_extract_entities


def _make_db():
    """创建内存 DB 并初始化 schema。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE memory_chunks (
            id TEXT PRIMARY KEY,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            project TEXT DEFAULT 'global',
            chunk_type TEXT DEFAULT 'decision',
            content TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            importance REAL DEFAULT 0.5,
            access_count INTEGER DEFAULT 0,
            oom_adj INTEGER DEFAULT 0,
            last_accessed TEXT DEFAULT '',
            lru_gen INTEGER DEFAULT 0,
            tags TEXT DEFAULT '[]'
        )
    """)
    conn.execute("""
        CREATE TABLE entity_map (
            entity_name TEXT,
            chunk_id TEXT,
            project TEXT,
            updated_at TEXT,
            PRIMARY KEY (entity_name, chunk_id)
        )
    """)
    conn.execute("""
        CREATE TABLE chunk_pins (
            chunk_id TEXT,
            project TEXT,
            pin_type TEXT,
            PRIMARY KEY (chunk_id, project)
        )
    """)
    return conn


def _insert_chunk(conn, chunk_id, summary, content="", project="global",
                  importance=0.5, access_count=0):
    conn.execute(
        """INSERT INTO memory_chunks (id, summary, content, project, importance, access_count)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (chunk_id, summary, content, project, importance, access_count)
    )
    conn.commit()


def _insert_entity(conn, entity_name, chunk_id, project="global"):
    conn.execute(
        """INSERT OR IGNORE INTO entity_map (entity_name, chunk_id, project, updated_at)
           VALUES (?, ?, ?, datetime('now'))""",
        (entity_name, chunk_id, project)
    )
    conn.commit()


def _entity_count(conn, chunk_id):
    return conn.execute(
        "SELECT COUNT(*) FROM entity_map WHERE chunk_id=?", (chunk_id,)
    ).fetchone()[0]


# ── 1. orphan_backfilled ──
def test_orphan_backfilled():
    """无 entity_map 的 chunk 成功回填。"""
    conn = _make_db()
    _insert_chunk(conn, "import-aaa", "[capabilities] Patch 发送工作流 > 邮件标签规则",
                  content="git send-email 的 Cc/Reviewed-by 标签使用规范")

    result = anon_vma_prepare(conn)

    assert result["backfilled"] == 1
    assert result["entities_created"] > 0
    assert _entity_count(conn, "import-aaa") > 0


# ── 2. existing_entity_preserved ──
def test_existing_entity_preserved():
    """已有 entity_map 的 chunk 不重复处理。"""
    conn = _make_db()
    _insert_chunk(conn, "native-bbb", "Android 性能诊断核心规则")
    _insert_entity(conn, "android", "native-bbb")

    result = anon_vma_prepare(conn)

    assert result["orphans_found"] == 0
    assert result["backfilled"] == 0
    # 原有条目不受影响
    assert _entity_count(conn, "native-bbb") == 1


# ── 3. ghost_skipped ──
def test_ghost_skipped():
    """importance=0 的 ghost chunk 不处理。"""
    conn = _make_db()
    _insert_chunk(conn, "ghost-ccc", "[merged→xxx] old content",
                  importance=0.0)

    result = anon_vma_prepare(conn)

    assert result["orphans_found"] == 0
    assert result["backfilled"] == 0


# ── 4. min_entities_filter ──
def test_min_entities_filter():
    """实体过少的 chunk 被跳过（summary 太短无法建立有效连接）。"""
    conn = _make_db()
    # 极短 summary，提取的实体不足 min_entities(3)
    _insert_chunk(conn, "import-ddd", "ab")

    result = anon_vma_prepare(conn)

    assert result["skipped_low_entity"] == 1
    assert result["backfilled"] == 0


# ── 5. max_backfill_cap ──
def test_max_backfill_cap():
    """单次处理上限生效。"""
    conn = _make_db()
    # 插入 40 个 orphan chunk（默认 max_backfill=30）
    for i in range(40):
        _insert_chunk(conn, f"import-cap-{i:03d}",
                      f"[capabilities] 功能模块 {i} > 详细描述说明文档内容",
                      content=f"详细的技术内容 content_value_{i}")

    result = anon_vma_prepare(conn)

    assert result["backfilled"] <= 30
    assert result["orphans_found"] >= 30


# ── 6. disabled ──
def test_disabled():
    """配置关闭时直接返回。"""
    conn = _make_db()
    _insert_chunk(conn, "import-dis", "[capabilities] 功能模块 > 详细说明")

    # Monkey-patch config
    import memory_os.config.sysctl as config
    original = config._REGISTRY.get("anon_vma_prepare.enabled")
    config._REGISTRY["anon_vma_prepare.enabled"] = (False, bool, None, None, None, "test")
    try:
        result = anon_vma_prepare(conn)
        assert result["backfilled"] == 0
        assert result["duration_ms"] == 0.0
    finally:
        if original:
            config._REGISTRY["anon_vma_prepare.enabled"] = original
        else:
            del config._REGISTRY["anon_vma_prepare.enabled"]


# ── 7. entity_extraction_backtick ──
def test_entity_extraction_backtick():
    """反引号内容正确提取。"""
    entities = _anon_vma_extract_entities("使用 `git send-email` 发送 patch")
    assert "git send-email" in entities


# ── 8. entity_extraction_bracket ──
def test_entity_extraction_bracket():
    """方括号 topic 标签正确提取。"""
    entities = _anon_vma_extract_entities("[capabilities] 元能力复用协议 > 方法")
    assert "capabilities" in entities


# ── 9. entity_extraction_hierarchy ──
def test_entity_extraction_hierarchy():
    """'>' 层级路径叶子节点正确提取。"""
    entities = _anon_vma_extract_entities("[sched_ext] Proxy Execution > 活跃问题")
    assert "proxy" in entities
    assert "execution" in entities


# ── 10. entity_extraction_chinese ──
def test_entity_extraction_chinese():
    """中文双字词正确提取。"""
    entities = _anon_vma_extract_entities("推理与问题解决原则")
    assert "推理" in entities
    assert "问题" in entities
    assert "解决" in entities


# ── 11. idempotent ──
def test_idempotent():
    """第二次运行不创建重复条目。"""
    conn = _make_db()
    _insert_chunk(conn, "import-idem", "[capabilities] Patch 发送工作流 > 邮件标签规则",
                  content="git send-email 规范")

    r1 = anon_vma_prepare(conn)
    assert r1["backfilled"] == 1
    count_after_first = conn.execute("SELECT COUNT(*) FROM entity_map").fetchone()[0]

    r2 = anon_vma_prepare(conn)
    assert r2["backfilled"] == 0
    assert r2["orphans_found"] == 0
    count_after_second = conn.execute("SELECT COUNT(*) FROM entity_map").fetchone()[0]
    assert count_after_second == count_after_first


# ── 12. project_scoped ──
def test_project_scoped():
    """project 过滤器生效。"""
    conn = _make_db()
    _insert_chunk(conn, "import-pa", "[capabilities] 模块A > 方法详细说明",
                  project="proj-a")
    _insert_chunk(conn, "import-pb", "[capabilities] 模块B > 方法详细说明",
                  project="proj-b")

    result = anon_vma_prepare(conn, project="proj-a")

    # proj-a 的 chunk 被处理
    assert _entity_count(conn, "import-pa") > 0
    # proj-b 不在 scope 内，不处理
    assert _entity_count(conn, "import-pb") == 0


# ── 13. global_included ──
def test_global_included():
    """global 层 chunk 被包含在 project scope 中。"""
    conn = _make_db()
    _insert_chunk(conn, "import-gl", "[capabilities] 全局功能 > 核心方法说明",
                  project="global")

    result = anon_vma_prepare(conn, project="some-project")

    assert result["backfilled"] == 1
    assert _entity_count(conn, "import-gl") > 0


# ── 14. multiple_orphans ──
def test_multiple_orphans():
    """多个孤儿 chunk 同时回填。"""
    conn = _make_db()
    _insert_chunk(conn, "import-m1", "[sched_ext] DSQ 概述 > 关键 API 文档",
                  content="dispatch_queue 调度队列管理")
    _insert_chunk(conn, "import-m2", "[pe_analysis] on_cpu 并发协议 > 当前状态",
                  content="proxy execution 并发同步")
    _insert_chunk(conn, "import-m3", "[kernel_process] Patch 格式规范 > Commit Message",
                  content="git commit 消息格式要求")

    result = anon_vma_prepare(conn)

    assert result["backfilled"] == 3
    for cid in ["import-m1", "import-m2", "import-m3"]:
        assert _entity_count(conn, cid) > 0


# ── 15. content_used ──
def test_content_used():
    """content[:200] 参与实体提取。"""
    conn = _make_db()
    # summary 很短但 content 有丰富实体
    _insert_chunk(conn, "import-ct", "简短摘要",
                  content="使用 `cgroup_budget_throttle` 函数控制 CPU 预算")

    result = anon_vma_prepare(conn)

    assert result["backfilled"] == 1
    # 从 content 中提取的实体应该存在
    entities = set(
        r[0] for r in conn.execute(
            "SELECT entity_name FROM entity_map WHERE chunk_id=?",
            ("import-ct",)
        ).fetchall()
    )
    assert "cgroup_budget_throttle" in entities


# ── 16. performance ──
def test_performance():
    """100 orphan chunks 处理 < 200ms。"""
    conn = _make_db()
    for i in range(100):
        _insert_chunk(
            conn, f"import-perf-{i:03d}",
            f"[capabilities] 功能模块 {i} > 详细技术描述文档",
            content=f"这是功能模块 {i} 的详细技术实现内容，包括 `function_{i}` 的参数和返回值"
        )

    # Patch max_backfill to allow all 100
    import memory_os.config.sysctl as config
    orig = config._REGISTRY.get("anon_vma_prepare.max_backfill")
    config._REGISTRY["anon_vma_prepare.max_backfill"] = (100, int, 5, 200, None, "test")
    try:
        t0 = time.time()
        result = anon_vma_prepare(conn)
        elapsed_ms = (time.time() - t0) * 1000

        assert result["backfilled"] == 100
        assert elapsed_ms < 200, f"Too slow: {elapsed_ms:.1f}ms"
    finally:
        if orig:
            config._REGISTRY["anon_vma_prepare.max_backfill"] = orig


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
