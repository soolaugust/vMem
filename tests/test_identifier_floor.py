"""
test_identifier_floor.py — 安全分级降级（借鉴 komi-learn safety_floor）

验证：
  T1: detect_identifiers 命中各类 PII/标识符（邮箱/手机/路径/IP/localhost/internal host）
  T2: 纯通用知识不命中
  T3: can_promote_to_semantic — 含 PII→False，通用→True
  T4: generalize_for_semantic 无 LLM 且含标识符 → (原文, False)
  T5: generalize 重写后干净 → (改写文本, True)
  T6: generalize 重写后仍泄露（komi 关键二次检测）→ (原文, False)
  T7: semantic_consolidator 集成 — 含 PII 的候选被过滤，不进 __semantic__
  T8: 不回归 scrub_secrets（性能/行为）
"""
import sys
import os
import time
from pathlib import Path
from datetime import datetime, timezone

_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT.parent))

import memory_os.runtime.tmpfs_compat as tmpfs  # noqa: F401
from memory_os.core.privacy_filter import (
    detect_identifiers, can_promote_to_semantic, generalize_for_semantic,
    scrub_secrets,
)


class TestDetectIdentifiers:

    def test_email(self):
        assert "email" in detect_identifiers("联系 zhidao.su@example.com 确认")

    def test_cn_phone(self):
        assert "cn_phone" in detect_identifiers("电话 13812345678 找我")

    def test_user_path(self):
        assert "user_path" in detect_identifiers("配置在 /home/mi/ssd/codes 下")

    def test_private_ip(self):
        assert "private_ip" in detect_identifiers("服务跑在 192.168.1.100")

    def test_localhost_port(self):
        assert "localhost_port" in detect_identifiers("MetaBot 在 localhost:9100")

    def test_internal_host(self):
        assert "internal_host" in detect_identifiers("登录 mi@build01.internal 执行")

    def test_clean_general_knowledge(self):
        assert detect_identifiers("BM25 检索比 TF-IDF 更适合短查询，因为文档长度归一化") == []

    def test_clean_returns_empty_fast(self):
        # 无关键字符 → 快速返回
        assert detect_identifiers("一段不含任何标识符的通用技术决策说明") == []


class TestCanPromote:

    def test_pii_cannot_promote(self):
        assert can_promote_to_semantic("部署到 /home/mi/app 并发邮件 a@b.com") is False

    def test_general_can_promote(self):
        assert can_promote_to_semantic("确定性操作应写成代码，AI 只做概率性分析") is True


class TestGeneralize:

    def test_no_llm_with_identifiers_not_promotable(self):
        text = "在 192.168.1.5 上重启 /home/mi/svc"
        out, promotable = generalize_for_semantic(text, llm_complete=None)
        assert out == text
        assert promotable is False

    def test_no_llm_clean_promotable(self):
        text = "缓存命中率低时应提高写入门槛"
        out, promotable = generalize_for_semantic(text, llm_complete=None)
        assert out == text
        assert promotable is True

    def test_llm_rewrite_clean(self):
        def fake_llm(prompt, system=None, max_tokens=512):
            return "在内网服务器上重启对应服务"  # 已去除标识符
        text = "在 192.168.1.5 上重启 /home/mi/svc"
        out, promotable = generalize_for_semantic(text, llm_complete=fake_llm)
        assert promotable is True
        assert detect_identifiers(out) == []

    def test_llm_rewrite_still_leaks_rejected(self):
        # komi 关键点：重写后二次检测仍命中 → 放弃，退回原文
        def bad_llm(prompt, system=None, max_tokens=512):
            return "重启 /home/mi/svc 服务"  # 仍含 user_path
        text = "在 192.168.1.5 上重启 /home/mi/svc"
        out, promotable = generalize_for_semantic(text, llm_complete=bad_llm)
        assert out == text
        assert promotable is False

    def test_llm_returns_none_fallback(self):
        def none_llm(prompt, system=None, max_tokens=512):
            return None
        text = "在 192.168.1.5 上部署"
        out, promotable = generalize_for_semantic(text, llm_complete=none_llm)
        assert out == text
        assert promotable is False


class TestNoRegressionScrubSecrets:

    def test_scrub_still_works(self):
        out, log = scrub_secrets("key=AKIAIOSFODNN7EXAMPLE rest")
        assert "[REDACTED:aws_access_key]" in out
        assert len(log) >= 1

    def test_scrub_fast_path_clean(self):
        out, log = scrub_secrets("普通文本无 secret")
        assert out == "普通文本无 secret"
        assert log == []

    def test_perf_under_budget(self):
        text = "一段中等长度文本 " * 50 + "13812345678 a@b.com /home/mi/x"
        t0 = time.perf_counter()
        for _ in range(200):
            detect_identifiers(text)
        elapsed_ms = (time.perf_counter() - t0) * 1000 / 200
        assert elapsed_ms < 1.0  # 单次 <1ms


class TestSemanticConsolidatorIntegration:

    def _get_conn(self):
        from memory_os.store.vfs_compat import open_db, ensure_schema
        db = os.environ.get("MEMORY_OS_DB", ":memory:")
        conn = open_db(db)
        ensure_schema(conn)
        conn.execute("PRAGMA busy_timeout=5000")  # 共享文件 DB：等待其它连接释放锁
        # 隔离：清空 chunk 表，避免共享 tmpfs DB 中其它测试残留干扰统计
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

    def _insert(self, conn, cid, project, summary, content, importance=0.8):
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO memory_chunks (id, created_at, updated_at, project, "
            "source_session, chunk_type, info_class, content, summary, tags, importance, "
            "retrievability, last_accessed, access_count, oom_adj, stability) "
            "VALUES (?,?,?,?,'','decision','world',?,?,'[]',?,0.5,?,5,0,2.0)",
            (cid, now, now, project, content, summary, importance, now),
        )

    def test_pii_chunk_blocked_from_semantic(self):
        conn = self._get_conn()
        # 两个不同 project 的相似 chunk，但都含 PII → 应全被过滤，0 cluster
        self._insert(conn, "pii_a", "projA",
                     "部署服务到 /home/mi/app 重启流程说明文档",
                     "在 /home/mi/app 下执行重启，联系 ops@example.com")
        self._insert(conn, "pii_b", "projB",
                     "部署服务到 /home/mi/app 重启流程说明步骤",
                     "于 /home/mi/app 目录执行部署，邮件 ops@example.com")
        conn.commit()
        from tools.semantic_consolidator import run_consolidation
        stats = run_consolidation(conn, sim_threshold=0.4, min_importance=0.65, dry_run=True)
        assert stats["pii_blocked"] >= 2
        assert stats["candidates"] == 0  # 含 PII 的候选被滤掉
        assert stats["created"] == 0

    def test_clean_chunks_still_promote(self):
        conn = self._get_conn()
        self._insert(conn, "cl_a", "projC",
                     "确定性操作写成代码而非依赖模型概率推理通用原则一",
                     "正则数值比较 API 调用应代码化以保证可复现")
        self._insert(conn, "cl_b", "projD",
                     "确定性操作写成代码而非依赖模型概率推理通用原则二",
                     "if-else 与确定性流程应固化为代码而非每次让模型决策")
        conn.commit()
        from tools.semantic_consolidator import run_consolidation
        stats = run_consolidation(conn, sim_threshold=0.3, min_importance=0.65, dry_run=True)
        assert stats["pii_blocked"] == 0
        assert stats["candidates"] >= 2
