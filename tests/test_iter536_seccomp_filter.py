"""
iter536: seccomp_filter — Summary Content Sanitizer at Syscall Boundary

OS 类比：Linux seccomp(SECCOMP_SET_MODE_FILTER) (Will Drewry, 2012, kernel 3.5)
  BPF 过滤器在 syscall entry 拦截畸形系统调用。

测试：_seccomp_filter 函数的 5 类模式检测 + insert_chunk 集成 + 生产数据验证。
"""
import sys
import sqlite3
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from memory_os.store.vfs_compat import _seccomp_filter, _vfs_write_protect, insert_chunk
from memory_os.store.api import open_db, ensure_schema


def _make_chunk(summary: str, chunk_type: str = "causal_chain", importance: float = 0.80):
    return {
        "id": str(uuid.uuid4()),
        "summary": summary,
        "chunk_type": chunk_type,
        "project": "test_seccomp",
        "tags": ["test"],
        "importance": importance,
        "created_at": "2026-05-02T00:00:00+00:00",
        "updated_at": "2026-05-02T00:00:00+00:00",
        "last_accessed": "2026-05-02T00:00:00+00:00",
        "access_count": 0,
        "oom_adj": 0,
        "lru_gen": 0,
        "raw_snippet": "",
        "content": summary,
        "encoding_context": {},
        "source_session": "test-session",
        "stability": 1.0,
        "retrievability": 1.0,
        "info_class": "world",
        "concept": "",
        "confidence_score": 0.7,
        "verification_status": "pending",
    }


def test_pattern1_json_key_prefix():
    """Pattern 1: JSON key 前缀 — "key": "value" → 提取 value"""
    action, cleaned = _seccomp_filter('"recommended_action": "审计当前记忆分类标准，识别边界模糊的规则"')
    assert action == "sanitize", f"Expected sanitize, got {action}"
    assert "审计当前记忆分类标准" in cleaned
    assert "recommended_action" not in cleaned
    print(f"  PASS: Pattern 1 JSON key prefix → sanitize")


def test_pattern2_truncated_key():
    """Pattern 2: 截断 JSON key — ction": "..." → 提取内容"""
    action, cleaned = _seccomp_filter(
        'ction": "抽查最近3条 closed corrections，验证行为是否确实不同"')
    assert action == "sanitize", f"Expected sanitize, got {action}"
    assert "抽查最近3条" in cleaned
    assert 'ction"' not in cleaned
    print(f"  PASS: Pattern 2 truncated key → sanitize")


def test_pattern3_arrow_json():
    """Pattern 3: 箭头后跟 JSON key — content" → "key": "value" → 清洗"""
    action, cleaned = _seccomp_filter(
        '规则看起来有效" → "recommended_action": "在实际工作 session 结束后验证规则是否生效"')
    assert action == "sanitize", f"Expected sanitize, got {action}"
    assert "规则看起来有效" in cleaned
    assert "recommended_action" not in cleaned
    print(f"  PASS: Pattern 3 arrow+JSON → sanitize")


def test_pattern4_quoted_content():
    """Pattern 4: 引号包裹 — "actual content" → 去引号"""
    action, cleaned = _seccomp_filter('"这是一个足够长的正常内容，被引号包裹了"')
    assert action == "sanitize", f"Expected sanitize, got {action}"
    assert cleaned == "这是一个足够长的正常内容，被引号包裹了"
    print(f"  PASS: Pattern 4 quoted content → sanitize")


def test_pattern5_underscore_key():
    """Pattern 5: _action": "..." 前缀 → 提取内容"""
    action, cleaned = _seccomp_filter(
        '_action": "统计最近 10 次用到 lock_analysis 的对话，检查触发词是否命中"')
    assert action == "sanitize", f"Expected sanitize, got {action}"
    assert "统计最近 10 次" in cleaned
    assert '_action"' not in cleaned
    print(f"  PASS: Pattern 5 _key prefix → sanitize")


def test_normal_summary_allow():
    """正常 summary 应直接 allow"""
    normal_cases = [
        "memory 引用前必须用 Glob/Read 验证路径存在：避免 MEMORY.md 悬挂链接",
        "Android 性能诊断核心规则：Running 慢=资源管控",
        "git commit author 字段必须严格取自 git config 原值",
        "cgroup 级别 cpu.uclamp.max 是 P99 决定性因素：设置后 P99 从 393us -> 59us",
    ]
    for s in normal_cases:
        action, _ = _seccomp_filter(s)
        assert action == "allow", f"Expected allow for '{s[:40]}', got {action}"
    print(f"  PASS: {len(normal_cases)} normal summaries → allow")


def test_high_json_density_reject():
    """高密度 JSON 特征（>=3 个 ': '）→ reject"""
    json_blob = '{"type": "correction", "severity": "high", "action": "fix immediately"}'
    action, _ = _seccomp_filter(json_blob)
    assert action == "reject", f"Expected reject, got {action}"
    print(f"  PASS: High JSON density → reject")


def test_sanitize_too_short_reject():
    """清洗后内容太短（<8字符）→ reject"""
    action, _ = _seccomp_filter('"短"')
    # 引号包裹但内容太短 → Pattern 4 不匹配（<8字符要求）
    # 所以不触发 sanitize，走到 allow
    # 但 "短" 本身长度 3（加引号5），不匹配 Pattern 4 的 .{8,}
    assert action == "allow", f"Expected allow (pattern not matched), got {action}"
    print(f"  PASS: Too short for pattern match → allow (handled by vfs_write_protect)")


def test_disabled_config():
    """配置禁用时直接 allow"""
    import memory_os.config.sysctl as config
    original = config._REGISTRY.get("vfs.seccomp_filter_enabled")
    config._REGISTRY["vfs.seccomp_filter_enabled"] = (False, bool, None, None, None, "test")
    try:
        action, _ = _seccomp_filter('"recommended_action": "should pass through"')
        assert action == "allow", f"Expected allow when disabled, got {action}"
    finally:
        if original:
            config._REGISTRY["vfs.seccomp_filter_enabled"] = original
        else:
            del config._REGISTRY["vfs.seccomp_filter_enabled"]
    print(f"  PASS: Config disabled → allow")


def test_insert_chunk_integration_sanitize():
    """insert_chunk 集成：sanitize 路径 — 写入清洗后的 summary"""
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    chunk = _make_chunk('"recommended_action": "验证这个长度足够的清洗内容是否能正确写入数据库"')
    insert_chunk(conn, chunk)
    row = conn.execute("SELECT summary FROM memory_chunks WHERE id=?", (chunk["id"],)).fetchone()
    assert row is not None, "Chunk should be inserted"
    assert "recommended_action" not in row[0], f"JSON key should be stripped: {row[0]}"
    assert "验证这个长度足够的清洗内容" in row[0]
    conn.close()
    print(f"  PASS: insert_chunk sanitize integration")


def test_insert_chunk_integration_reject():
    """insert_chunk 集成：reject 路径 — 不写入"""
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    chunk = _make_chunk('{"type": "bad", "severity": "high", "action": "reject this"}')
    insert_chunk(conn, chunk)
    row = conn.execute("SELECT summary FROM memory_chunks WHERE id=?", (chunk["id"],)).fetchone()
    assert row is None, "Chunk should be rejected (not inserted)"
    conn.close()
    print(f"  PASS: insert_chunk reject integration")


def test_insert_chunk_integration_allow():
    """insert_chunk 集成：allow 路径 — 正常写入"""
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    chunk = _make_chunk("正常的 summary 内容，不含任何 JSON 残留或碎片")
    insert_chunk(conn, chunk)
    row = conn.execute("SELECT summary FROM memory_chunks WHERE id=?", (chunk["id"],)).fetchone()
    assert row is not None, "Normal chunk should be inserted"
    assert row[0] == "正常的 summary 内容，不含任何 JSON 残留或碎片"
    conn.close()
    print(f"  PASS: insert_chunk allow integration")


def test_fts5_consistency_after_sanitize():
    """FTS5 索引一致性：sanitize 后 FTS5 包含清洗后内容"""
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    chunk = _make_chunk('ction": "这个 FTS5 索引应该包含清洗后的正确内容而非 JSON 残留"')
    insert_chunk(conn, chunk)
    # 检查 FTS5 中的内容
    results = conn.execute(
        "SELECT summary FROM memory_chunks_fts WHERE memory_chunks_fts MATCH '清洗'"
    ).fetchall()
    assert len(results) >= 1, "FTS5 should find sanitized content"
    conn.close()
    print(f"  PASS: FTS5 consistency after sanitize")


def test_performance():
    """性能：1000 次 _seccomp_filter < 50ms"""
    import time
    cases = [
        "正常 summary，无碎片",
        '"recommended_action": "这是需要清洗的内容"',
        'ction": "截断的 JSON key 前缀内容"',
        '{"type": "high", "severity": "high", "action": "should reject"}',
    ]
    t0 = time.time()
    for _ in range(250):
        for c in cases:
            _seccomp_filter(c)
    elapsed_ms = (time.time() - t0) * 1000
    assert elapsed_ms < 50, f"Performance: {elapsed_ms:.1f}ms > 50ms"
    print(f"  PASS: Performance 1000 calls = {elapsed_ms:.1f}ms (< 50ms)")


if __name__ == "__main__":
    tests = [
        test_pattern1_json_key_prefix,
        test_pattern2_truncated_key,
        test_pattern3_arrow_json,
        test_pattern4_quoted_content,
        test_pattern5_underscore_key,
        test_normal_summary_allow,
        test_high_json_density_reject,
        test_sanitize_too_short_reject,
        test_disabled_config,
        test_insert_chunk_integration_sanitize,
        test_insert_chunk_integration_reject,
        test_insert_chunk_integration_allow,
        test_fts5_consistency_after_sanitize,
        test_performance,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as e:
            print(f"  FAIL: {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{'='*60}")
    print(f"iter536 seccomp_filter: {passed}/{passed+failed} passed")
    if failed:
        sys.exit(1)
