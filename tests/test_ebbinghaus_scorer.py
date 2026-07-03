"""
test_ebbinghaus_scorer.py — Ebbinghaus 遗忘曲线评分测试

验证 R = e^(-t/S) 衰减、stability 传递、feature flag gating。
"""
import math
import time
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timezone, timedelta


class TestEbbinghausRetention:

    def test_basic_formula(self):
        from memory_os.core.scorer import ebbinghaus_retention
        r = ebbinghaus_retention(7.0, 7.0)
        assert abs(r - math.exp(-1)) < 0.001

    def test_high_stability_slow_decay(self):
        from memory_os.core.scorer import ebbinghaus_retention
        r = ebbinghaus_retention(365.0, 7.0)
        assert r > 0.98

    def test_low_stability_fast_decay(self):
        from memory_os.core.scorer import ebbinghaus_retention
        r = ebbinghaus_retention(1.0, 7.0)
        assert r < 0.01

    def test_zero_age_full_retention(self):
        from memory_os.core.scorer import ebbinghaus_retention
        r = ebbinghaus_retention(7.0, 0.0)
        assert abs(r - 1.0) < 0.001

    def test_zero_stability_clamped(self):
        from memory_os.core.scorer import ebbinghaus_retention
        r = ebbinghaus_retention(0.0, 7.0)
        assert 0 <= r <= 1

    def test_negative_stability_clamped(self):
        from memory_os.core.scorer import ebbinghaus_retention
        r = ebbinghaus_retention(-5.0, 7.0)
        assert 0 <= r <= 1


class TestImportanceWithDecayEbbinghaus:

    def _past_iso(self, days):
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def test_ebbinghaus_disabled_uses_linear(self):
        """ebbinghaus_enabled=False 时应使用传统线性衰减。"""
        import memory_os.core.scorer as scorer
        old_val = scorer._EBBINGHAUS_ENABLED
        scorer._EBBINGHAUS_ENABLED = False
        try:
            # stability 无论多高都不影响（被忽略）
            r1 = scorer.importance_with_decay(1.0, self._past_iso(30), stability=365.0)
            r2 = scorer.importance_with_decay(1.0, self._past_iso(30), stability=1.0)
            assert abs(r1 - r2) < 0.01
        finally:
            scorer._EBBINGHAUS_ENABLED = old_val

    def test_ebbinghaus_enabled_uses_stability(self):
        """ebbinghaus_enabled=True 时 stability 影响衰减速度。"""
        import memory_os.core.scorer as scorer
        old_val = scorer._EBBINGHAUS_ENABLED
        scorer._EBBINGHAUS_ENABLED = True
        try:
            r_high = scorer.importance_with_decay(1.0, self._past_iso(30), stability=365.0)
            r_low = scorer.importance_with_decay(1.0, self._past_iso(30), stability=7.0)
            assert r_high > r_low
        finally:
            scorer._EBBINGHAUS_ENABLED = old_val

    def test_floor_still_applies(self):
        """最低下限 0.3 仍然生效。"""
        import memory_os.core.scorer as scorer
        old_val = scorer._EBBINGHAUS_ENABLED
        scorer._EBBINGHAUS_ENABLED = True
        try:
            r = scorer.importance_with_decay(1.0, self._past_iso(1000), stability=1.0)
            assert r >= scorer._IMP_FLOOR
        finally:
            scorer._EBBINGHAUS_ENABLED = old_val

    def test_stability_cap_applied(self):
        """stability 超过 cap 被截断。"""
        import memory_os.core.scorer as scorer
        old_val = scorer._EBBINGHAUS_ENABLED
        old_cap = scorer._EBBINGHAUS_CAP
        scorer._EBBINGHAUS_ENABLED = True
        scorer._EBBINGHAUS_CAP = 100.0
        try:
            r_capped = scorer.importance_with_decay(1.0, self._past_iso(30), stability=9999.0)
            r_at_cap = scorer.importance_with_decay(1.0, self._past_iso(30), stability=100.0)
            assert abs(r_capped - r_at_cap) < 0.01
        finally:
            scorer._EBBINGHAUS_ENABLED = old_val
            scorer._EBBINGHAUS_CAP = old_cap


class TestRetrievalScoreStability:

    def _past_iso(self, days):
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def test_stability_param_accepted(self):
        """retrieval_score 接受 stability 参数不报错。"""
        import memory_os.core.scorer as scorer
        s = scorer.retrieval_score(
            relevance=0.5, importance=0.8,
            last_accessed=self._past_iso(7),
            stability=30.0,
        )
        assert isinstance(s, float)

    def test_stability_affects_score_when_enabled(self):
        """stability 不同导致评分不同（Ebbinghaus 启用时）。"""
        import memory_os.core.scorer as scorer
        old_val = scorer._EBBINGHAUS_ENABLED
        scorer._EBBINGHAUS_ENABLED = True
        try:
            s_high = scorer.retrieval_score(
                relevance=0.5, importance=0.8,
                last_accessed=self._past_iso(30),
                stability=365.0,
            )
            s_low = scorer.retrieval_score(
                relevance=0.5, importance=0.8,
                last_accessed=self._past_iso(30),
                stability=7.0,
            )
            assert s_high > s_low
        finally:
            scorer._EBBINGHAUS_ENABLED = old_val


class TestPerformance:

    def _past_iso(self, days):
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def test_ebbinghaus_retention_perf(self):
        from memory_os.core.scorer import ebbinghaus_retention
        start = time.perf_counter()
        for _ in range(10000):
            ebbinghaus_retention(30.0, 15.0)
        elapsed_ms = (time.perf_counter() - start) * 1000
        per_call_us = elapsed_ms / 10000 * 1000
        assert per_call_us < 10, f"ebbinghaus_retention took {per_call_us:.1f}us/call"
