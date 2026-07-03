#!/usr/bin/env python3
"""
iter560: cfs_bandwidth — Per-Chunk Retrieval Frequency Throttle

OS 类比：Linux CFS Bandwidth Control (Paul Turner, Google, 2011, kernel 3.2,
kernel/sched/fair.c, cfs_bandwidth.c)
  每个 cgroup 分配 quota/period 带宽上限；超额 task 被 throttled。
  类比：recall_count > quota 的 chunk 被乘法降权，防止单 chunk 垄断 Top-K。

测试覆盖：
  T1. 基础行为：under-quota 返回 1.0，over-quota 返回 throttle
  T2. 渐进压制：overflow 越多 throttle 越重（decay 指数衰减）
  T3. 边界条件：recall_count=0, =quota, =quota+1
  T4. 配置 tunables 验证
  T5. scorer.retrieval_score 集成
  T6. 与 saturation_penalty/bandwidth_throttle 互补性
  T7. 生产场景：垄断 chunk 模拟
  T8. 禁用场景
  T9. 性能测试
  T10. 不同参数组合
"""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import memory_os.runtime.tmpfs_compat as tmpfs  # noqa: E402 — 测试隔离

from memory_os.core.scorer import (
    cfs_bandwidth_throttle,
    saturation_penalty,
    bandwidth_throttle,
    retrieval_score,
)
from memory_os.config.sysctl import get as sysctl

_pass = _fail = 0


def test(name, cond, msg=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  PASS  {name}")
    else:
        _fail += 1
        print(f"  FAIL  {name} — {msg}")


def main():
    global _pass, _fail

    # ── T1: 基础行为 ──
    print("\n[T1] 基础行为：under-quota → 1.0，over-quota → throttle")
    quota = sysctl("cfs_bandwidth.quota")
    throttle_factor = sysctl("cfs_bandwidth.throttle_factor")
    overflow_decay = sysctl("cfs_bandwidth.overflow_decay")
    print(f"  defaults: quota={quota} factor={throttle_factor} decay={overflow_decay}")

    # Under quota
    test("rc=0 → 1.0",
         cfs_bandwidth_throttle(0) == 1.0)
    test("rc=1 → 1.0",
         cfs_bandwidth_throttle(1) == 1.0)
    test("rc=quota → 1.0",
         cfs_bandwidth_throttle(quota) == 1.0,
         f"got {cfs_bandwidth_throttle(quota)}")

    # Over quota
    over1 = cfs_bandwidth_throttle(quota + 1)
    expected1 = throttle_factor * (overflow_decay ** 1)
    test("rc=quota+1 → factor*decay^1",
         abs(over1 - expected1) < 1e-10,
         f"got={over1:.6f} expected={expected1:.6f}")
    test("rc=quota+1 < 1.0",
         over1 < 1.0)

    # ── T2: 渐进压制 ──
    print("\n[T2] 渐进压制：overflow 越多 throttle 越重")
    prev = 1.0
    throttles = []
    for i in range(1, 15):
        t = cfs_bandwidth_throttle(quota + i)
        throttles.append(t)
        test(f"rc=quota+{i} < rc=quota+{i-1}",
             t < prev,
             f"got={t:.6f} prev={prev:.6f}")
        prev = t

    # Very high overflow should approach 0
    t_high = cfs_bandwidth_throttle(quota + 30)
    test("rc=quota+30 < 0.05",
         t_high < 0.05,
         f"got={t_high:.6f}")

    # ── T3: 边界条件 ──
    print("\n[T3] 边界条件")
    test("rc=0 exact 1.0",
         cfs_bandwidth_throttle(0) == 1.0)
    test("rc=-1 → 1.0 (defensive)",
         cfs_bandwidth_throttle(-1) == 1.0)
    test("rc=quota exact 1.0",
         cfs_bandwidth_throttle(quota) == 1.0)
    test("rc=quota+1 < 1.0",
         cfs_bandwidth_throttle(quota + 1) < 1.0)

    # ── T4: 配置 tunables ──
    print("\n[T4] 配置 tunables 验证")
    test("cfs_bandwidth.enabled exists",
         sysctl("cfs_bandwidth.enabled") is not None)
    test("cfs_bandwidth.quota default=8",
         sysctl("cfs_bandwidth.quota") == 8)
    test("cfs_bandwidth.throttle_factor default=0.50",
         abs(sysctl("cfs_bandwidth.throttle_factor") - 0.50) < 1e-6)
    test("cfs_bandwidth.overflow_decay default=0.85",
         abs(sysctl("cfs_bandwidth.overflow_decay") - 0.85) < 1e-6)

    # ── T5: scorer.retrieval_score 集成 ──
    print("\n[T5] retrieval_score 集成：cfs_bandwidth 影响最终分数")
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()

    # Base score with low recall count (no throttle)
    score_normal = retrieval_score(
        relevance=1.0, importance=0.95,
        last_accessed=now_iso, access_count=5,
        created_at=now_iso, recall_count=2,
    )
    # Same but with high recall count (throttle active)
    score_throttled = retrieval_score(
        relevance=1.0, importance=0.95,
        last_accessed=now_iso, access_count=5,
        created_at=now_iso, recall_count=quota + 10,
    )
    test("high recall_count reduces score",
         score_throttled < score_normal,
         f"normal={score_normal:.4f} throttled={score_throttled:.4f}")
    # Should be significantly reduced
    ratio = score_throttled / score_normal if score_normal > 0 else 0
    test("throttle ratio < 0.5 for +10 overflow",
         ratio < 0.5,
         f"ratio={ratio:.4f}")

    # ── T6: 与 saturation_penalty/bandwidth_throttle 互补性 ──
    print("\n[T6] 互补性：cfs_bandwidth 比 saturation_penalty 强得多")
    # saturation_penalty for monopoly chunk (recall_count=30)
    sp = saturation_penalty(30)
    # cfs_bandwidth for same (quota=8, overflow=22)
    cbw = cfs_bandwidth_throttle(30)
    print(f"  saturation_penalty(30) = {sp:.4f} (additive, max 0.25)")
    print(f"  cfs_bandwidth_throttle(30) = {cbw:.6f} (multiplicative)")
    test("cfs_bandwidth much stronger than saturation_penalty",
         cbw < 0.10,  # Should be < 10% of original score
         f"cbw={cbw:.6f}")
    test("saturation_penalty still capped at 0.25",
         sp <= 0.25 + 1e-10)

    # bandwidth_throttle (iter527) comparison
    bw = bandwidth_throttle(30, window=30)
    print(f"  bandwidth_throttle(30, window=30) = {bw:.4f}")
    # cfs_bandwidth provides finer granularity
    cbw_9 = cfs_bandwidth_throttle(9)  # just over quota
    cbw_15 = cfs_bandwidth_throttle(15)  # moderate
    cbw_30 = cfs_bandwidth_throttle(30)  # heavy
    test("progressive: 9 > 15 > 30",
         cbw_9 > cbw_15 > cbw_30,
         f"9={cbw_9:.4f} 15={cbw_15:.4f} 30={cbw_30:.4f}")

    # ── T7: 生产场景 — 垄断 chunk 模拟 ──
    print("\n[T7] 生产场景：垄断 chunk (rc=30, imp=0.95) vs 新 chunk (rc=0, imp=0.70)")
    # Monopoly chunk: high everything
    score_monopoly = retrieval_score(
        relevance=1.0, importance=0.95,
        last_accessed=now_iso, access_count=89,
        created_at=now_iso, recall_count=30,
    )
    # Fresh chunk: lower but zero recall
    score_fresh = retrieval_score(
        relevance=0.8, importance=0.70,
        last_accessed=now_iso, access_count=0,
        created_at=now_iso, recall_count=0,
    )
    print(f"  monopoly score: {score_monopoly:.4f}")
    print(f"  fresh score: {score_fresh:.4f}")
    # With cfs_bandwidth, fresh should be competitive
    gap = score_monopoly / score_fresh if score_fresh > 0 else float('inf')
    print(f"  gap ratio: {gap:.2f}x")
    test("monopoly/fresh gap < 3x",
         gap < 3.0,
         f"gap={gap:.2f}x (was ~6x without cfs_bandwidth)")

    # ── T8: 不同参数组合 ──
    print("\n[T8] 不同参数组合")
    # Strict: low quota, low factor
    t_strict = cfs_bandwidth_throttle(10, quota=5, throttle_factor=0.30, overflow_decay=0.70)
    expected_strict = 0.30 * (0.70 ** 5)
    test("strict params (q=5,f=0.3,d=0.7) at rc=10",
         abs(t_strict - expected_strict) < 1e-10,
         f"got={t_strict:.6f} expected={expected_strict:.6f}")

    # Lenient: high quota, high factor
    t_lenient = cfs_bandwidth_throttle(10, quota=10, throttle_factor=0.80, overflow_decay=0.95)
    test("lenient params at rc=10 (at quota) → 1.0",
         t_lenient == 1.0)
    t_lenient2 = cfs_bandwidth_throttle(12, quota=10, throttle_factor=0.80, overflow_decay=0.95)
    expected_lenient = 0.80 * (0.95 ** 2)
    test("lenient params at rc=12",
         abs(t_lenient2 - expected_lenient) < 1e-10,
         f"got={t_lenient2:.6f} expected={expected_lenient:.6f}")

    # ── T9: 性能测试 ──
    print("\n[T9] 性能")
    N = 100_000
    t0 = time.time()
    for i in range(N):
        cfs_bandwidth_throttle(15)
    elapsed = (time.time() - t0) * 1000
    per_call = elapsed / N * 1000  # microseconds
    print(f"  {N} calls: {elapsed:.1f}ms, {per_call:.3f}us/call")
    test("performance < 0.5us/call",
         per_call < 0.5,
         f"got {per_call:.3f}us/call")

    # ── T10: 数学正确性 ──
    print("\n[T10] 数学正确性")
    for overflow in range(0, 20):
        rc = quota + overflow
        expected = 1.0 if overflow == 0 else throttle_factor * (overflow_decay ** overflow)
        actual = cfs_bandwidth_throttle(rc)
        test(f"rc={rc} math",
             abs(actual - expected) < 1e-10,
             f"actual={actual:.10f} expected={expected:.10f}")

    # ── Summary ──
    print(f"\n{'='*50}")
    print(f"iter560 cfs_bandwidth: {_pass} PASS, {_fail} FAIL")
    return _fail == 0


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
