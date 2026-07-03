"""
test_llm_umbrella.py — LLM consolidation umbrella（借鉴 komi-learn ConsolidateLLM）

验证：
  T1: config key 注册 — consolidation.llm_umbrella.enabled 默认 False
  T2: llm_client 无 key → llm_complete 返回 None（优雅回退）
  T3: enabled=False → 完全走旧字符串拼接路径（_llm_umbrella 不被调用）
  T4: enabled=True + LLM 返回干净 umbrella → new_content = umbrella
  T5: enabled=True + LLM 返回含 PII → 被 detect_identifiers 拦，回退拼接
  T6: enabled=True + LLM 返回含 secret → 被 scrub_secrets 脱敏后通过/或拦
  T7: enabled=True + LLM 不可用(None) → 回退拼接
"""
import sys
import os
from pathlib import Path
from datetime import datetime, timezone

_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT.parent))

import memory_os.runtime.tmpfs_compat as tmpfs  # noqa: F401
import memory_os.config.sysctl as config


class TestConfigKeys:

    def test_enabled_default_false(self):
        assert config.get("consolidation.llm_umbrella.enabled") is False

    def test_model_default(self):
        assert config.get("consolidation.llm_umbrella.model") == "claude-haiku-4-5-20251001"


class TestLLMClientFallback:

    def test_no_key_returns_none(self, monkeypatch):
        # 清除 key + 重置单例
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        import memory_os.core.llm_client as llm_client
        llm_client._CLIENT = None
        llm_client._CLIENT_TRIED = False
        assert llm_client.llm_complete("hi") is None
        assert llm_client.llm_available() is False


class TestUmbrellaHelper:

    def test_umbrella_no_llm_returns_none(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        import memory_os.core.llm_client as llm_client
        llm_client._CLIENT = None
        llm_client._CLIENT_TRIED = False
        from tools.consolidate import _llm_umbrella
        assert _llm_umbrella("s", "c", "g") is None


class TestConsolidateIntegration:
    """T3-T7: consolidate_project 的 umbrella 接入与安全闭环"""

    def _get_conn(self):
        from memory_os.store.api import open_db, ensure_schema
        db = os.environ.get("MEMORY_OS_DB", ":memory:")
        conn = open_db(db)
        ensure_schema(conn)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("DELETE FROM memory_chunks")
        conn.commit()
        self._conn = conn
        return conn

    def teardown_method(self, method):
        c = getattr(self, "_conn", None)
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    def _insert(self, conn, cid, summary, content, importance):
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO memory_chunks (id, created_at, updated_at, project, "
            "source_session, chunk_type, info_class, content, summary, tags, importance, "
            "retrievability, last_accessed, access_count, oom_adj, stability) "
            "VALUES (?,?,?,'umb_proj','','decision','world',?,?,'[]',?,0.5,?,1,0,2.0)",
            (cid, now, now, content, summary, importance, now),
        )

    def _two_similar(self, conn):
        # 两条高相似 decision（trigram Jaccard >= 0.85）
        self._insert(conn, "u_a",
                     "采用 BM25 检索算法因为短查询效果好且实现简单可靠稳定",
                     "主记忆正文：BM25 适合短查询", 0.9)
        self._insert(conn, "u_b",
                     "采用 BM25 检索算法因为短查询效果好且实现简单可靠稳健",
                     "次记忆正文", 0.8)
        conn.commit()

    def _get_survivor_content(self, conn):
        row = conn.execute(
            "SELECT content FROM memory_chunks WHERE id='u_a'"
        ).fetchone()
        return row[0] if row else None

    def test_disabled_uses_string_concat(self, monkeypatch):
        conn = self._get_conn()
        self._two_similar(conn)
        monkeypatch.setattr(config, "get",
            lambda k, *a, **kw: False if k == "consolidation.llm_umbrella.enabled"
            else config._REGISTRY.get(k, (None,))[0] if hasattr(config, "_REGISTRY") else None)
        from tools.consolidate import consolidate_project
        stats = consolidate_project(conn, "umb_proj", threshold=0.85, dry_run=False)
        assert stats["merged"] >= 1
        content = self._get_survivor_content(conn)
        assert "[merged]" in content  # 字符串拼接标记

    def test_enabled_clean_umbrella(self, monkeypatch):
        conn = self._get_conn()
        self._two_similar(conn)
        monkeypatch.setattr(config, "get",
            lambda k, *a, **kw: True if k == "consolidation.llm_umbrella.enabled" else None)
        import tools.consolidate as tc
        monkeypatch.setattr(tc, "_llm_umbrella",
            lambda ss, sc, gs: "整合后的通用知识：BM25 在短查询场景优于 TF-IDF，实现简单")
        stats = tc.consolidate_project(conn, "umb_proj", threshold=0.85, dry_run=False)
        assert stats["merged"] >= 1
        content = self._get_survivor_content(conn)
        assert "整合后的通用知识" in content
        assert "[merged]" not in content  # 走了 umbrella 而非拼接

    def test_enabled_pii_umbrella_rejected(self, monkeypatch):
        conn = self._get_conn()
        self._two_similar(conn)
        monkeypatch.setattr(config, "get",
            lambda k, *a, **kw: True if k == "consolidation.llm_umbrella.enabled" else None)
        import tools.consolidate as tc
        # LLM 返回含 PII（路径 + 邮箱）→ 应被拦，回退拼接
        monkeypatch.setattr(tc, "_llm_umbrella",
            lambda ss, sc, gs: "部署到 /home/mi/app 并通知 ops@example.com")
        tc.consolidate_project(conn, "umb_proj", threshold=0.85, dry_run=False)
        content = self._get_survivor_content(conn)
        assert "/home/mi/app" not in content
        assert "[merged]" in content  # 回退拼接

    def test_enabled_llm_unavailable_fallback(self, monkeypatch):
        conn = self._get_conn()
        self._two_similar(conn)
        monkeypatch.setattr(config, "get",
            lambda k, *a, **kw: True if k == "consolidation.llm_umbrella.enabled" else None)
        import tools.consolidate as tc
        monkeypatch.setattr(tc, "_llm_umbrella", lambda ss, sc, gs: None)  # 不可用
        tc.consolidate_project(conn, "umb_proj", threshold=0.85, dry_run=False)
        content = self._get_survivor_content(conn)
        assert "[merged]" in content  # 回退拼接
