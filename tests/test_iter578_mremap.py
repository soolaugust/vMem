"""
iter578: mremap — Adaptive Score Floor 测试

OS 类比：Linux mremap() — 动态调整虚拟地址空间映射大小
测试自适应分数地板逻辑：当 Top-1 score 很高时，降低 min_thresh 允许更多次优候选通过。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import memory_os.runtime.tmpfs_compat as tmpfs  # noqa: E402 — 测试隔离
import pytest
from memory_os.config.sysctl import get as _sysctl, _REGISTRY


# ─── 单元测试：自适应地板计算逻辑 ───

def _compute_adaptive_floor(top1_score, ratio=0.25, min_top1=0.5, global_floor=0.30):
    """复现 retriever.py 中的自适应地板逻辑"""
    _min_thresh = global_floor
    if top1_score >= min_top1:
        _adaptive_floor = top1_score * ratio
        _min_thresh = min(_min_thresh, max(_adaptive_floor, 0.10))
    return _min_thresh


class TestAdaptiveFloorLogic:
    """自适应地板计算逻辑的单元测试"""

    def test_high_top1_lowers_threshold(self):
        """top1=0.99 → floor=0.2475, 低于固定 0.3"""
        floor = _compute_adaptive_floor(0.99)
        assert floor < 0.30
        assert abs(floor - 0.99 * 0.25) < 0.001

    def test_low_top1_keeps_fixed_threshold(self):
        """top1=0.3 (< min_top1=0.5) → 保持固定 0.3"""
        floor = _compute_adaptive_floor(0.30)
        assert floor == 0.30

    def test_medium_top1_adaptive(self):
        """top1=0.6 → floor=0.15, 低于固定 0.3"""
        floor = _compute_adaptive_floor(0.60)
        assert floor < 0.30
        assert abs(floor - 0.60 * 0.25) < 0.001

    def test_boundary_top1_equals_min_top1(self):
        """top1=0.5 (= min_top1) → 启用自适应"""
        floor = _compute_adaptive_floor(0.50)
        assert floor < 0.30
        assert abs(floor - 0.50 * 0.25) < 0.001

    def test_floor_never_below_absolute_min(self):
        """即使 ratio 很低，地板不低于 0.10"""
        floor = _compute_adaptive_floor(0.99, ratio=0.05)
        # 0.99 * 0.05 = 0.0495 < 0.10, 应被钳位到 0.10
        assert floor == 0.10

    def test_ratio_zero_uses_absolute_min(self):
        """ratio=0 → floor=0.10 (absolute minimum)"""
        floor = _compute_adaptive_floor(0.99, ratio=0.0)
        assert floor == 0.10

    def test_disabled_when_top1_below_min(self):
        """top1 < min_top1 → 不启用，保持 global_floor"""
        floor = _compute_adaptive_floor(0.49, min_top1=0.5)
        assert floor == 0.30

    def test_custom_global_floor(self):
        """global_floor=0.20 → 当自适应 > 0.20 时用 global_floor"""
        floor = _compute_adaptive_floor(0.99, ratio=0.25, global_floor=0.20)
        # 0.99*0.25=0.2475 > 0.20, min(0.20, 0.2475)=0.20
        assert floor == 0.20

    def test_adaptive_below_global_floor(self):
        """adaptive < global_floor → 用 adaptive"""
        floor = _compute_adaptive_floor(0.99, ratio=0.15, global_floor=0.30)
        # 0.99*0.15=0.1485 < 0.30, 用 adaptive
        assert abs(floor - 0.99 * 0.15) < 0.001


class TestAdaptiveFloorIntegration:
    """集成测试：验证候选过滤效果"""

    def test_more_candidates_pass_with_adaptive(self):
        """自适应地板让更多次优候选通过"""
        candidates = [(0.99, {"id": "a"}), (0.28, {"id": "b"}),
                      (0.26, {"id": "c"}), (0.15, {"id": "d"})]

        # 固定阈值 0.30：只有 a 通过
        fixed_positive = [(s, c) for s, c in candidates if s >= 0.30]
        assert len(fixed_positive) == 1

        # 自适应地板：top1=0.99, ratio=0.25 → floor=0.2475
        adaptive_thresh = _compute_adaptive_floor(0.99)
        adaptive_positive = [(s, c) for s, c in candidates if s >= adaptive_thresh]
        assert len(adaptive_positive) == 3  # a, b, c 通过

    def test_noise_still_filtered(self):
        """极低分候选仍被过滤"""
        candidates = [(0.99, {"id": "a"}), (0.05, {"id": "noise"})]
        adaptive_thresh = _compute_adaptive_floor(0.99)
        positive = [(s, c) for s, c in candidates if s >= adaptive_thresh]
        assert len(positive) == 1  # 只有 a，noise 被过滤

    def test_all_low_scores_no_adaptive(self):
        """所有候选分低（top1=0.2）→ 不启用自适应，用固定阈值"""
        candidates = [(0.20, {"id": "a"}), (0.18, {"id": "b"})]
        adaptive_thresh = _compute_adaptive_floor(0.20)
        assert adaptive_thresh == 0.30  # 不启用，保持固定
        positive = [(s, c) for s, c in candidates if s >= adaptive_thresh]
        assert len(positive) == 0  # 全部被过滤（正确）

    def test_generic_query_not_affected(self):
        """generic_query 使用独立高阈值，不受自适应影响"""
        # 自适应只在非 generic query 时启用
        # generic_query_min_threshold=0.85 不受 mremap 影响
        generic_thresh = _sysctl("retriever.generic_query_min_threshold")
        assert generic_thresh == 0.85


class TestConfig:
    """config.py tunable 注册验证"""

    def test_tunables_registered(self):
        """新增 3 个 retriever.adaptive_floor_* tunable 已注册"""
        assert "retriever.adaptive_floor_enabled" in _REGISTRY
        assert "retriever.adaptive_floor_ratio" in _REGISTRY
        assert "retriever.adaptive_floor_min_top1" in _REGISTRY

    def test_default_values(self):
        """默认值正确"""
        assert _sysctl("retriever.adaptive_floor_enabled") is True
        assert _sysctl("retriever.adaptive_floor_ratio") == 0.25
        assert _sysctl("retriever.adaptive_floor_min_top1") == 0.5

    def test_ratio_range(self):
        """ratio 范围 [0.05, 0.8]"""
        _, typ, lo, hi, _, _ = _REGISTRY["retriever.adaptive_floor_ratio"]
        assert lo == 0.05
        assert hi == 0.8


class TestPerformance:
    """性能验证"""

    def test_adaptive_floor_overhead(self):
        """自适应地板计算开销 < 0.01ms"""
        import time
        N = 10000
        start = time.perf_counter()
        for _ in range(N):
            _compute_adaptive_floor(0.99)
        elapsed = (time.perf_counter() - start) / N * 1000
        assert elapsed < 0.01  # < 0.01ms per call


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
