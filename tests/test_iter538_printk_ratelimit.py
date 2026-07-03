"""
iter538: printk_ratelimit — dmesg Ring Buffer Deduplication Tests

OS 类比：Linux printk_ratelimit() / __ratelimit() (Alan Cox, 1999)
  防止同一子系统的重复消息填满 dmesg ring buffer。

验证：
  1. _ratelimit_key 提取消息结构骨架
  2. _printk_ratelimit 窗口内去重
  3. 不同子系统不交叉抑制
  4. ERR/WARN 级别永不抑制
  5. INFO/DEBUG 级别在窗口内被去重
  6. 窗口过期后允许再次写入
  7. LRU 淘汰防止内存泄漏
  8. 去重后 ring buffer 实际减少写入
  9. 移除子系统内部双写（kfree_rcu/put_page/numa_balancing/page_idle）
  10. config tunable dmesg.ratelimit_interval_s 生效
"""
import sys
import os
import time
import sqlite3
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 使用临时目录隔离测试 DB，避免锁定生产 DB
_tmpdir = tempfile.mkdtemp(prefix="test538_")
_test_db = os.path.join(_tmpdir, "test.db")
os.environ["MEMORY_OS_DB"] = _test_db


def _fresh_conn():
    """创建独立临时 DB 并初始化 schema。"""
    import tempfile as _tf
    _td = _tf.mkdtemp(prefix="t538_")
    _db = os.path.join(_td, "t.db")
    conn = sqlite3.connect(_db)
    # 最小 schema: memory_chunks + dmesg + 必要表
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memory_chunks (
            id TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT, project TEXT,
            source_session TEXT, chunk_type TEXT, content TEXT, summary TEXT,
            tags TEXT DEFAULT '', importance REAL DEFAULT 0.5,
            retrievability REAL DEFAULT 1.0, last_accessed TEXT,
            feishu_url TEXT DEFAULT '', access_count INTEGER DEFAULT 0,
            oom_adj INTEGER DEFAULT 0, lru_gen INTEGER DEFAULT 0,
            confidence_score REAL DEFAULT 0.5, evidence_chain TEXT DEFAULT '',
            verification_status TEXT DEFAULT 'unverified',
            info_class TEXT DEFAULT '', stability REAL DEFAULT 0.5,
            emotional_weight REAL DEFAULT 0.0, emotional_valence REAL DEFAULT 0.0,
            depth_of_processing REAL DEFAULT 0.0, source_type TEXT DEFAULT '',
            source_reliability REAL DEFAULT 0.5, encode_context TEXT DEFAULT '',
            raw_snippet TEXT DEFAULT '', encoding_context TEXT DEFAULT '',
            original_ec_count INTEGER DEFAULT 0, spaced_access_count INTEGER DEFAULT 0,
            hypermnesia_last_boost TEXT DEFAULT '', access_source TEXT DEFAULT '',
            row_version INTEGER DEFAULT 0, chunk_state TEXT DEFAULT 'active',
            boundary_proximity REAL DEFAULT 0.0, session_type_history TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS memory_chunks_fts (
            rowid_ref TEXT, summary TEXT, content TEXT
        );
        CREATE TABLE IF NOT EXISTS dmesg (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL, level TEXT NOT NULL,
            subsystem TEXT NOT NULL, message TEXT NOT NULL,
            session_id TEXT DEFAULT '', project TEXT DEFAULT '',
            extra TEXT
        );
        CREATE TABLE IF NOT EXISTS recall_traces (
            id TEXT PRIMARY KEY, timestamp TEXT NOT NULL,
            session_id TEXT NOT NULL, project TEXT NOT NULL,
            prompt_hash TEXT NOT NULL, candidates_count INTEGER,
            top_k_json TEXT, injected INTEGER DEFAULT 0,
            reason TEXT, duration_ms REAL DEFAULT 0,
            ftrace_json TEXT, user_feedback TEXT,
            feedback_ts TEXT, agent_id TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS chunk_pins (
            chunk_id TEXT, project TEXT, pin_type TEXT,
            PRIMARY KEY (chunk_id, project)
        );
        CREATE TABLE IF NOT EXISTS checkpoints (
            id TEXT PRIMARY KEY, project TEXT, session_id TEXT,
            timestamp TEXT, data TEXT
        );
        CREATE TABLE IF NOT EXISTS fts_schema_version (version INTEGER);
    """)
    conn.commit()
    return conn


def _clear_cache():
    """清理 ratelimit 缓存。"""
    from memory_os.store.proc import _ratelimit_cache
    _ratelimit_cache.clear()


def test_ratelimit_key_structured():
    """结构化消息（field=value）提取相同 key。"""
    from memory_os.store.proc import _ratelimit_key
    k1 = _ratelimit_key("freed=27 dead=27 skip_prot=0 12.4ms")
    k2 = _ratelimit_key("freed=0 dead=0 skip_prot=3 0.1ms")
    assert k1 == k2, f"Same structure should produce same key: {k1} vs {k2}"


def test_ratelimit_key_unstructured():
    """非结构化消息用前 40 字符作 key。"""
    from memory_os.store.proc import _ratelimit_key
    k1 = _ratelimit_key("session started successfully with no errors at all")
    k2 = _ratelimit_key("session started successfully with no errors at all!!!")
    # 前 40 字符相同
    assert k1 == k2


def test_ratelimit_key_different_structure():
    """不同字段结构产生不同 key。"""
    from memory_os.store.proc import _ratelimit_key
    k1 = _ratelimit_key("freed=27 dead=27")
    k2 = _ratelimit_key("promoted=2 demoted=0")
    assert k1 != k2


def test_ratelimit_first_call_allowed():
    """首次调用永不抑制。"""
    from memory_os.store.proc import _printk_ratelimit
    _clear_cache()
    assert _printk_ratelimit("sub1", "freed=5 dead=5") is False


def test_ratelimit_second_call_suppressed():
    """同窗口内第二次同 key 调用被抑制。"""
    from memory_os.store.proc import _printk_ratelimit
    _clear_cache()
    _printk_ratelimit("sub1", "freed=5 dead=5")
    assert _printk_ratelimit("sub1", "freed=3 dead=3") is True


def test_ratelimit_different_subsystem_not_suppressed():
    """不同子系统不交叉抑制。"""
    from memory_os.store.proc import _printk_ratelimit
    _clear_cache()
    _printk_ratelimit("kfree_rcu", "freed=5 dead=5")
    assert _printk_ratelimit("put_page", "freed=5 dead=5") is False


def test_ratelimit_window_expiry():
    """窗口过期后允许再次写入。"""
    from memory_os.store.proc import _printk_ratelimit, _ratelimit_cache
    _clear_cache()
    _printk_ratelimit("sub_expire", "freed=1 dead=1")
    # 手动修改时间戳模拟过期
    for key in _ratelimit_cache:
        _ratelimit_cache[key]["ts"] -= 60  # 后退 60 秒
    assert _printk_ratelimit("sub_expire", "freed=2 dead=2") is False


def test_err_level_never_suppressed():
    """ERR 级别 dmesg_log 永不被抑制。"""
    from memory_os.store.proc import DMESG_ERR, dmesg_log
    _clear_cache()
    conn = _fresh_conn()
    dmesg_log(conn, DMESG_ERR, "test_err538", "critical error 1")
    dmesg_log(conn, DMESG_ERR, "test_err538", "critical error 1")
    dmesg_log(conn, DMESG_ERR, "test_err538", "critical error 1")
    count = conn.execute("SELECT COUNT(*) FROM dmesg WHERE subsystem='test_err538'").fetchone()[0]
    assert count == 3, f"ERR should never be suppressed, got {count}"


def test_warn_level_never_suppressed():
    """WARN 级别 dmesg_log 永不被抑制。"""
    from memory_os.store.proc import DMESG_WARN, dmesg_log
    _clear_cache()
    conn = _fresh_conn()
    dmesg_log(conn, DMESG_WARN, "test_warn538", "warning 1")
    dmesg_log(conn, DMESG_WARN, "test_warn538", "warning 1")
    count = conn.execute("SELECT COUNT(*) FROM dmesg WHERE subsystem='test_warn538'").fetchone()[0]
    assert count == 2, f"WARN should never be suppressed, got {count}"


def test_info_level_suppressed():
    """INFO 级别重复消息在窗口内被抑制。"""
    from memory_os.store.proc import DMESG_INFO, dmesg_log
    _clear_cache()
    conn = _fresh_conn()
    dmesg_log(conn, DMESG_INFO, "test_info538", "freed=5 dead=5 skip_prot=0")
    dmesg_log(conn, DMESG_INFO, "test_info538", "freed=3 dead=3 skip_prot=1")
    dmesg_log(conn, DMESG_INFO, "test_info538", "freed=1 dead=1 skip_prot=0")
    count = conn.execute("SELECT COUNT(*) FROM dmesg WHERE subsystem='test_info538'").fetchone()[0]
    assert count == 1, f"Second+ INFO should be suppressed, got {count}"


def test_debug_level_suppressed():
    """DEBUG 级别重复消息在窗口内被抑制。"""
    from memory_os.store.proc import DMESG_DEBUG, dmesg_log
    _clear_cache()
    conn = _fresh_conn()
    dmesg_log(conn, DMESG_DEBUG, "test_dbg538", "scan: total=9 hot=1")
    dmesg_log(conn, DMESG_DEBUG, "test_dbg538", "scan: total=8 hot=2")
    count = conn.execute("SELECT COUNT(*) FROM dmesg WHERE subsystem='test_dbg538'").fetchone()[0]
    assert count == 1, f"Second DEBUG should be suppressed, got {count}"


def test_lru_eviction():
    """缓存达到上限时 LRU 淘汰最旧 entry。"""
    from memory_os.store.proc import _printk_ratelimit, _ratelimit_cache, _RATELIMIT_CACHE_MAX
    _clear_cache()
    # 填满缓存
    for i in range(_RATELIMIT_CACHE_MAX):
        _printk_ratelimit(f"sub_{i}", f"msg_{i}")
    assert len(_ratelimit_cache) == _RATELIMIT_CACHE_MAX
    # 再加一个 — 应该淘汰最旧的
    _printk_ratelimit("sub_new", "new_msg")
    assert len(_ratelimit_cache) == _RATELIMIT_CACHE_MAX


def test_ring_buffer_savings():
    """验证 ratelimit 实际减少 ring buffer 写入数。"""
    from memory_os.store.proc import DMESG_INFO, dmesg_log
    _clear_cache()
    conn = _fresh_conn()
    # 模拟 10 个 session 的 kfree_rcu 日志
    for i in range(10):
        dmesg_log(conn, DMESG_INFO, "test_savings538",
                  f"freed={i} dead={i} skip_prot=0")
    count = conn.execute("SELECT COUNT(*) FROM dmesg WHERE subsystem='test_savings538'").fetchone()[0]
    assert count == 1, f"10 calls should produce 1 entry, got {count}"


def test_kfree_rcu_no_internal_dmesg():
    """kfree_rcu 不再内部写 dmesg（由 loader 负责）。"""
    _clear_cache()
    conn = _fresh_conn()
    # 插入一个 dead chunk
    conn.execute("""
        INSERT INTO memory_chunks (id, created_at, updated_at, project, source_session,
            chunk_type, content, summary, importance, access_count, oom_adj)
        VALUES ('dead538', datetime('now'), datetime('now'), 'test_proj', 'sess',
            'decision', 'test', 'test dead', 0.1, 0, 0)
    """)
    conn.commit()
    from memory_os.store.mm import kfree_rcu
    result = kfree_rcu(conn)
    # kfree_rcu should NOT write to dmesg internally
    count = conn.execute("SELECT COUNT(*) FROM dmesg WHERE subsystem='kfree_rcu'").fetchone()[0]
    assert count == 0, f"kfree_rcu should not write dmesg internally, got {count}"
    assert result["freed"] >= 1


def test_put_page_no_internal_dmesg():
    """put_page 不再内部写 dmesg（由 loader 负责）。"""
    _clear_cache()
    conn = _fresh_conn()
    # 插入 UE chunk (importance=0, access>0)
    conn.execute("""
        INSERT INTO memory_chunks (id, created_at, updated_at, project, source_session,
            chunk_type, content, summary, importance, access_count, oom_adj)
        VALUES ('ue538', datetime('now'), datetime('now'), 'test_proj', 'sess',
            'decision', 'test', 'ue test', 0.0, 5, 0)
    """)
    conn.commit()
    from memory_os.store.mm import put_page
    result = put_page(conn, "test_proj")
    count = conn.execute("SELECT COUNT(*) FROM dmesg WHERE subsystem='put_page'").fetchone()[0]
    assert count == 0, f"put_page should not write dmesg internally, got {count}"


def test_config_tunable_exists():
    """config 中 dmesg.ratelimit_interval_s 存在且默认 30。"""
    from memory_os.config.sysctl import get
    val = get("dmesg.ratelimit_interval_s")
    assert val == 30, f"Default should be 30, got {val}"


def test_config_disable_ratelimit():
    """interval=0 时禁用去重。"""
    from memory_os.store.proc import _printk_ratelimit, _ratelimit_cache
    from memory_os.config.sysctl import sysctl_set
    _clear_cache()
    # 暂时设为 0
    sysctl_set("dmesg.ratelimit_interval_s", 0)
    r1 = _printk_ratelimit("test_disable", "freed=1 dead=1")
    r2 = _printk_ratelimit("test_disable", "freed=2 dead=2")
    # 恢复
    sysctl_set("dmesg.ratelimit_interval_s", 30)
    assert r1 is False
    assert r2 is False, "With interval=0, nothing should be suppressed"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed}/{passed + failed} passed")
    if failed:
        sys.exit(1)
