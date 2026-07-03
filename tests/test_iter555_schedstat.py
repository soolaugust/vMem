"""
iter555: schedstat — Unified Scheduler Statistics Accumulator
OS 类比：Linux SCHEDSTAT (Mike Galbraith, 2004, kernel 2.6.7, kernel/sched/stats.c)

测试覆盖：
1. load 缺失文件返回空状态
2. load 损坏文件容错
3. save/load roundtrip
4. record_skip 各原因正确计数
5. record_exec 累积 runtime + did_work
6. record_session boot time 环形缓冲区
7. record_session 环形缓冲区溢出截断
8. report 空状态不崩溃
9. report boot_time_trend 判定（improving/stable/degrading）
10. report skip_breakdown 累加正确
11. report top_idle 排序
12. report top_slow 排序
13. report effective_work_rate 计算
14. blame 格式化输出
15. 多子系统独立统计
16. _schedstat_empty_entry 结构完整
"""
import sys
import json
import os
import tempfile
from pathlib import Path

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestSchedstat:
    """iter555: schedstat 核心逻辑测试"""

    def setup_method(self):
        """每个测试用例前清理环境"""
        self._tmpdir = tempfile.mkdtemp()
        self._mock_file = os.path.join(self._tmpdir, "schedstat_state.json")
        import memory_os.store.mm as store_mm
        self._orig_file = store_mm._SCHEDSTAT_FILE
        store_mm._SCHEDSTAT_FILE = self._mock_file

    def teardown_method(self):
        """清理临时文件"""
        import memory_os.store.mm as store_mm
        store_mm._SCHEDSTAT_FILE = self._orig_file
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_load_missing_file(self):
        """缺失文件返回空状态"""
        from memory_os.store.mm import schedstat_load
        state = schedstat_load()
        assert state == {"subsystems": {}, "session_count": 0, "boot_times_ms": []}

    def test_load_corrupt_file(self):
        """损坏文件返回空状态"""
        from memory_os.store.mm import schedstat_load
        with open(self._mock_file, "w") as f:
            f.write("not json{{{")
        state = schedstat_load()
        assert state == {"subsystems": {}, "session_count": 0, "boot_times_ms": []}

    def test_save_load_roundtrip(self):
        """save → load 数据完整性"""
        from memory_os.store.mm import schedstat_load, schedstat_save, schedstat_record_exec
        state = schedstat_load()
        state = schedstat_record_exec(state, "watchdog", 5.0, True)
        state = schedstat_record_exec(state, "damon_scan", 12.0, False)
        schedstat_save(state)
        loaded = schedstat_load()
        assert loaded["subsystems"]["watchdog"]["exec_count"] == 1
        assert loaded["subsystems"]["watchdog"]["total_runtime_ms"] == 5.0
        assert loaded["subsystems"]["watchdog"]["did_work_count"] == 1
        assert loaded["subsystems"]["damon_scan"]["exec_count"] == 1
        assert loaded["subsystems"]["damon_scan"]["did_work_count"] == 0

    def test_record_skip_idle(self):
        """timer_slack 空转跳过正确记录"""
        from memory_os.store.mm import schedstat_record_skip
        state = {"subsystems": {}, "session_count": 0, "boot_times_ms": []}
        state = schedstat_record_skip(state, "shrink_dcache", "idle")
        state = schedstat_record_skip(state, "shrink_dcache", "idle")
        entry = state["subsystems"]["shrink_dcache"]
        assert entry["skip_idle"] == 2
        assert entry["skip_total"] == 2
        assert entry["skip_throttle"] == 0
        assert entry["skip_group_throttle"] == 0

    def test_record_skip_throttle(self):
        """sched_deadline throttle 正确记录"""
        from memory_os.store.mm import schedstat_record_skip
        state = {"subsystems": {}, "session_count": 0, "boot_times_ms": []}
        state = schedstat_record_skip(state, "sleep_consolidation", "throttle")
        entry = state["subsystems"]["sleep_consolidation"]
        assert entry["skip_throttle"] == 1
        assert entry["skip_total"] == 1

    def test_record_skip_group_throttle(self):
        """cgroup_budget 分组 throttle 正确记录"""
        from memory_os.store.mm import schedstat_record_skip
        state = {"subsystems": {}, "session_count": 0, "boot_times_ms": []}
        state = schedstat_record_skip(state, "oom_reaper", "group_throttle")
        state = schedstat_record_skip(state, "oom_reaper", "group_throttle")
        state = schedstat_record_skip(state, "oom_reaper", "idle")
        entry = state["subsystems"]["oom_reaper"]
        assert entry["skip_group_throttle"] == 2
        assert entry["skip_idle"] == 1
        assert entry["skip_total"] == 3

    def test_record_exec_accumulates(self):
        """exec 累积 runtime 和 did_work"""
        from memory_os.store.mm import schedstat_record_exec
        state = {"subsystems": {}, "session_count": 0, "boot_times_ms": []}
        state = schedstat_record_exec(state, "watchdog", 5.0, True)
        state = schedstat_record_exec(state, "watchdog", 3.0, True)
        state = schedstat_record_exec(state, "watchdog", 0.5, False)
        entry = state["subsystems"]["watchdog"]
        assert entry["exec_count"] == 3
        assert entry["total_runtime_ms"] == 8.5
        assert entry["did_work_count"] == 2

    def test_record_session_boot_time(self):
        """session boot time 正确追加"""
        from memory_os.store.mm import schedstat_record_session
        state = {"subsystems": {}, "session_count": 0, "boot_times_ms": []}
        state = schedstat_record_session(state, 69.0, max_history=5)
        state = schedstat_record_session(state, 42.0, max_history=5)
        assert state["session_count"] == 2
        assert state["boot_times_ms"] == [69.0, 42.0]

    def test_record_session_ring_buffer_overflow(self):
        """环形缓冲区超出 max_history 时截断"""
        from memory_os.store.mm import schedstat_record_session
        state = {"subsystems": {}, "session_count": 0, "boot_times_ms": []}
        for i in range(10):
            state = schedstat_record_session(state, float(i * 10), max_history=5)
        assert state["session_count"] == 10
        assert len(state["boot_times_ms"]) == 5
        # 保留最近 5 个
        assert state["boot_times_ms"] == [50.0, 60.0, 70.0, 80.0, 90.0]

    def test_report_empty_state(self):
        """空状态 report 不崩溃"""
        from memory_os.store.mm import schedstat_report
        state = {"subsystems": {}, "session_count": 0, "boot_times_ms": []}
        report = schedstat_report(state)
        assert report["session_count"] == 0
        assert report["boot_time_avg_ms"] == 0.0
        assert report["boot_time_trend"] == "stable"
        assert report["effective_work_rate"] == 0.0
        assert report["top_idle"] == []
        assert report["top_slow"] == []

    def test_report_trend_improving(self):
        """boot time 下降趋势 → improving"""
        from memory_os.store.mm import schedstat_report
        # 前半高，后半低
        state = {"subsystems": {}, "session_count": 8,
                 "boot_times_ms": [100.0, 95.0, 90.0, 85.0, 60.0, 55.0, 50.0, 45.0]}
        report = schedstat_report(state)
        assert report["boot_time_trend"] == "improving"

    def test_report_trend_degrading(self):
        """boot time 上升趋势 → degrading"""
        from memory_os.store.mm import schedstat_report
        state = {"subsystems": {}, "session_count": 8,
                 "boot_times_ms": [40.0, 45.0, 50.0, 55.0, 80.0, 85.0, 90.0, 95.0]}
        report = schedstat_report(state)
        assert report["boot_time_trend"] == "degrading"

    def test_report_trend_stable(self):
        """boot time 持平 → stable"""
        from memory_os.store.mm import schedstat_report
        state = {"subsystems": {}, "session_count": 4,
                 "boot_times_ms": [70.0, 72.0, 68.0, 71.0]}
        report = schedstat_report(state)
        assert report["boot_time_trend"] == "stable"

    def test_report_skip_breakdown(self):
        """skip_breakdown 各原因累加正确"""
        from memory_os.store.mm import schedstat_record_skip, schedstat_report
        state = {"subsystems": {}, "session_count": 0, "boot_times_ms": []}
        state = schedstat_record_skip(state, "a", "idle")
        state = schedstat_record_skip(state, "a", "idle")
        state = schedstat_record_skip(state, "b", "throttle")
        state = schedstat_record_skip(state, "c", "group_throttle")
        report = schedstat_report(state)
        assert report["skip_breakdown"] == {"idle": 2, "throttle": 1, "group_throttle": 1}

    def test_report_top_idle_sorted(self):
        """top_idle 按 skip_rate 降序"""
        from memory_os.store.mm import schedstat_record_skip, schedstat_record_exec, schedstat_report
        state = {"subsystems": {}, "session_count": 0, "boot_times_ms": []}
        # a: 5 skip, 1 exec → skip_rate = 5/6 = 0.833
        for _ in range(5):
            state = schedstat_record_skip(state, "subsys_a", "idle")
        state = schedstat_record_exec(state, "subsys_a", 1.0, False)
        # b: 2 skip, 3 exec → skip_rate = 2/5 = 0.4
        for _ in range(2):
            state = schedstat_record_skip(state, "subsys_b", "idle")
        for _ in range(3):
            state = schedstat_record_exec(state, "subsys_b", 2.0, True)
        report = schedstat_report(state)
        assert len(report["top_idle"]) == 2
        assert report["top_idle"][0]["name"] == "subsys_a"
        assert report["top_idle"][0]["skip_rate"] == 0.833
        assert report["top_idle"][1]["name"] == "subsys_b"

    def test_report_top_slow_sorted(self):
        """top_slow 按 avg_runtime_ms 降序"""
        from memory_os.store.mm import schedstat_record_exec, schedstat_report
        state = {"subsystems": {}, "session_count": 0, "boot_times_ms": []}
        # fast: 2 exec, total 4ms → avg 2ms
        state = schedstat_record_exec(state, "fast", 2.0, True)
        state = schedstat_record_exec(state, "fast", 2.0, True)
        # slow: 2 exec, total 40ms → avg 20ms
        state = schedstat_record_exec(state, "slow", 20.0, True)
        state = schedstat_record_exec(state, "slow", 20.0, True)
        report = schedstat_report(state)
        assert report["top_slow"][0]["name"] == "slow"
        assert report["top_slow"][0]["avg_runtime_ms"] == 20.0
        assert report["top_slow"][1]["name"] == "fast"

    def test_report_effective_work_rate(self):
        """全局有效工作率计算"""
        from memory_os.store.mm import schedstat_record_exec, schedstat_report
        state = {"subsystems": {}, "session_count": 0, "boot_times_ms": []}
        state = schedstat_record_exec(state, "a", 5.0, True)
        state = schedstat_record_exec(state, "a", 5.0, True)
        state = schedstat_record_exec(state, "a", 5.0, False)
        state = schedstat_record_exec(state, "b", 3.0, True)
        # 4 exec, 3 did_work → work_rate = 0.75
        report = schedstat_report(state)
        assert report["effective_work_rate"] == 0.75

    def test_blame_format(self):
        """blame 输出包含关键字段"""
        from memory_os.store.mm import (schedstat_record_exec, schedstat_record_skip,
                              schedstat_record_session, schedstat_blame)
        state = {"subsystems": {}, "session_count": 0, "boot_times_ms": []}
        state = schedstat_record_exec(state, "watchdog", 5.0, True)
        state = schedstat_record_skip(state, "damon", "idle")
        state = schedstat_record_session(state, 69.0, max_history=5)
        blame = schedstat_blame(state)
        assert "sessions=1" in blame
        assert "avg_boot=69.0ms" in blame
        assert "trend=" in blame
        assert "work_rate=" in blame

    def test_multi_subsystem_independent(self):
        """多子系统统计互不干扰"""
        from memory_os.store.mm import schedstat_record_exec, schedstat_record_skip
        state = {"subsystems": {}, "session_count": 0, "boot_times_ms": []}
        state = schedstat_record_exec(state, "alpha", 10.0, True)
        state = schedstat_record_skip(state, "beta", "idle")
        state = schedstat_record_exec(state, "gamma", 3.0, False)
        assert state["subsystems"]["alpha"]["exec_count"] == 1
        assert state["subsystems"]["alpha"]["skip_total"] == 0
        assert state["subsystems"]["beta"]["skip_idle"] == 1
        assert state["subsystems"]["beta"]["exec_count"] == 0
        assert state["subsystems"]["gamma"]["did_work_count"] == 0

    def test_empty_entry_structure(self):
        """_schedstat_empty_entry 包含所有必需字段"""
        from memory_os.store.mm import _schedstat_empty_entry
        entry = _schedstat_empty_entry()
        expected_keys = {"exec_count", "skip_total", "skip_idle", "skip_throttle",
                         "skip_group_throttle", "total_runtime_ms", "did_work_count"}
        assert set(entry.keys()) == expected_keys
        assert all(v == 0 or v == 0.0 for v in entry.values())
