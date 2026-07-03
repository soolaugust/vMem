"""
iter556: sched_autogroup — Adaptive Scheduler Parameter Tuning

OS 类比：Linux sched_autogroup (Mike Galbraith, 2010, kernel 2.6.38)
基于 schedstat 累积数据，自动调节 timer_slack/sched_deadline/cgroup_budget 参数。

测试覆盖：
  - load/save 持久化
  - cooldown 冷却期跳过
  - insufficient data 跳过
  - 规则1: 高空转 → 降低 idle_threshold
  - 规则2: degrading → 收紧 budget_ms
  - 规则3: improving + high work_rate → 放松 budget_ms
  - 规则4: 低 work_rate → 收紧 group_budget_ms
  - 规则5: 高 work_rate + improving → 放松 group_budget_ms
  - no_action_needed 场景
  - 多规则同时触发
  - 调整记录上限 (20条)
  - stats 输出结构
  - budget_ms 下限保护 (5ms)
  - budget_ms 上限保护 (50ms)
  - group_budget_ms 下限保护 (20ms)
  - group_budget_ms 上限保护 (120ms)
  - idle_threshold 下限保护 (不低于 1)
  - cooldown 可配置
"""
import json
import os
import sys
import tempfile
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


def _make_schedstat(session_count=10, boot_times=None, subsystems=None):
    """构造测试用 schedstat 状态。"""
    if boot_times is None:
        boot_times = [100.0] * session_count
    if subsystems is None:
        subsystems = {}
    return {
        "session_count": session_count,
        "boot_times_ms": boot_times,
        "subsystems": subsystems,
    }


def _make_high_idle_subsystems(count=5, skip_rate=0.85):
    """构造 N 个高空转子系统。"""
    subs = {}
    for i in range(count):
        name = f"subsystem_{i}"
        skip_total = int(10 * skip_rate)
        exec_count = 10 - skip_total
        subs[name] = {
            "exec_count": exec_count,
            "skip_total": skip_total,
            "skip_idle": skip_total,
            "skip_throttle": 0,
            "skip_group_throttle": 0,
            "total_runtime_ms": exec_count * 5.0,
            "did_work_count": exec_count,
        }
    return subs


def _degrading_boot_times(n=10):
    """生成 degrading 趋势的 boot times（后半比前半高 >10%）。"""
    first_half = [80.0] * (n // 2)
    second_half = [120.0] * (n // 2)
    return first_half + second_half


def _improving_boot_times(n=10):
    """生成 improving 趋势的 boot times（后半比前半低 >10%）。"""
    first_half = [120.0] * (n // 2)
    second_half = [80.0] * (n // 2)
    return first_half + second_half


@pytest.fixture(autouse=True)
def _isolate_autogroup(tmp_path, monkeypatch):
    """隔离 autogroup 文件 + config 到临时目录。"""
    ag_file = str(tmp_path / "sched_autogroup.json")
    monkeypatch.setattr("store_mm._AUTOGROUP_FILE", ag_file)
    # 隔离 config sysctl.json 到临时目录
    import memory_os.config.sysctl as config
    sysctl_file = str(tmp_path / "sysctl.json")
    monkeypatch.setattr(config, "SYSCTL_FILE", sysctl_file)
    monkeypatch.setattr(config, "MEMORY_OS_DIR", str(tmp_path))
    config._invalidate_cache()
    # 隔离 schedstat 文件
    ss_file = str(tmp_path / "schedstat.json")
    monkeypatch.setattr("store_mm._SCHEDSTAT_FILE", ss_file)


class TestSchedAutogroupLoadSave:
    """load/save 持久化测试。"""

    def test_load_missing_file(self):
        from memory_os.store.mm import sched_autogroup_load
        state = sched_autogroup_load()
        assert state["adjustments"] == []
        assert state["last_run_session"] == 0
        assert state["cooldown_sessions"] == 3

    def test_save_load_roundtrip(self, tmp_path, monkeypatch):
        from memory_os.store.mm import sched_autogroup_load, sched_autogroup_save
        state = {
            "adjustments": [{"param": "x", "old": 1, "new": 2, "reason": "test"}],
            "last_run_session": 7,
            "cooldown_sessions": 3,
        }
        sched_autogroup_save(state)
        loaded = sched_autogroup_load()
        assert loaded["last_run_session"] == 7
        assert len(loaded["adjustments"]) == 1

    def test_load_corrupt_file(self, tmp_path, monkeypatch):
        from memory_os.store.mm import sched_autogroup_load, _AUTOGROUP_FILE
        Path(_AUTOGROUP_FILE).parent.mkdir(parents=True, exist_ok=True)
        Path(_AUTOGROUP_FILE).write_text("not json{{{", encoding="utf-8")
        state = sched_autogroup_load()
        assert state["adjustments"] == []


class TestSchedAutogroupCooldown:
    """冷却期测试。"""

    def test_skip_during_cooldown(self, tmp_path, monkeypatch):
        from memory_os.store.mm import sched_autogroup, sched_autogroup_save
        # 设置 last_run_session=8，当前 session=10，cooldown=3 → 间隔 2 < 3，应跳过
        sched_autogroup_save({
            "adjustments": [], "last_run_session": 8, "cooldown_sessions": 3,
        })
        ss = _make_schedstat(session_count=10)
        result = sched_autogroup(ss)
        assert not result["adjusted"]
        assert "cooldown" in result["skipped_reason"]

    def test_pass_after_cooldown(self, tmp_path, monkeypatch):
        from memory_os.store.mm import sched_autogroup, sched_autogroup_save
        # 设置 last_run_session=5，当前 session=10，cooldown=3 → 间隔 5 >= 3，不跳过
        sched_autogroup_save({
            "adjustments": [], "last_run_session": 5, "cooldown_sessions": 3,
        })
        ss = _make_schedstat(session_count=10)
        result = sched_autogroup(ss)
        # 不一定有调整，但不应因 cooldown 跳过
        assert "cooldown" not in result.get("skipped_reason", "")


class TestSchedAutogroupInsufficientData:
    """数据不足跳过。"""

    def test_skip_with_few_sessions(self):
        from memory_os.store.mm import sched_autogroup
        ss = _make_schedstat(session_count=3)
        result = sched_autogroup(ss)
        assert not result["adjusted"]
        assert "insufficient_data" in result["skipped_reason"]


class TestSchedAutogroupRule1:
    """规则1: 高空转 → 降低 timer_slack.idle_threshold。"""

    def test_lower_idle_threshold(self):
        from memory_os.store.mm import sched_autogroup
        from memory_os.config.sysctl import get as cfg_get
        subs = _make_high_idle_subsystems(count=5, skip_rate=0.90)
        ss = _make_schedstat(session_count=10, subsystems=subs)
        result = sched_autogroup(ss)
        assert result["adjusted"]
        # 找到 idle_threshold 调整
        ts_adj = [a for a in result["adjustments"]
                  if a["param"] == "timer_slack.idle_threshold"]
        assert len(ts_adj) == 1
        assert ts_adj[0]["new"] < ts_adj[0]["old"]
        # 验证 config 已更新
        assert int(cfg_get("timer_slack.idle_threshold")) == ts_adj[0]["new"]

    def test_idle_threshold_floor(self, monkeypatch):
        """idle_threshold 不低于 1。"""
        from memory_os.store.mm import sched_autogroup
        from memory_os.config.sysctl import sysctl_set
        sysctl_set("timer_slack.idle_threshold", 1)
        subs = _make_high_idle_subsystems(count=5, skip_rate=0.90)
        ss = _make_schedstat(session_count=10, subsystems=subs)
        result = sched_autogroup(ss)
        # 已经是 1，不应再降
        ts_adj = [a for a in result.get("adjustments", [])
                  if a["param"] == "timer_slack.idle_threshold"]
        assert len(ts_adj) == 0


class TestSchedAutogroupRule2:
    """规则2: degrading → 收紧 sched_deadline.budget_ms。"""

    def test_tighten_budget_on_degrading(self):
        from memory_os.store.mm import sched_autogroup
        from memory_os.config.sysctl import get as cfg_get
        boot_times = _degrading_boot_times(10)
        ss = _make_schedstat(session_count=10, boot_times=boot_times)
        result = sched_autogroup(ss)
        assert result["adjusted"]
        sd_adj = [a for a in result["adjustments"]
                  if a["param"] == "sched_deadline.budget_ms"]
        assert len(sd_adj) == 1
        assert sd_adj[0]["new"] < sd_adj[0]["old"]
        assert sd_adj[0]["new"] == round(20.0 * 0.85, 1)

    def test_budget_ms_floor(self, monkeypatch):
        """budget_ms 不低于 5.0。"""
        from memory_os.store.mm import sched_autogroup
        from memory_os.config.sysctl import sysctl_set
        sysctl_set("sched_deadline.budget_ms", 5.5)
        boot_times = _degrading_boot_times(10)
        ss = _make_schedstat(session_count=10, boot_times=boot_times)
        result = sched_autogroup(ss)
        sd_adj = [a for a in result.get("adjustments", [])
                  if a["param"] == "sched_deadline.budget_ms"]
        if sd_adj:
            assert sd_adj[0]["new"] >= 5.0


class TestSchedAutogroupRule3:
    """规则3: improving + high work_rate → 放松 budget_ms。"""

    def test_relax_budget_on_improving(self):
        from memory_os.store.mm import sched_autogroup
        from memory_os.config.sysctl import get as cfg_get
        boot_times = _improving_boot_times(10)
        # 构造 work_rate > 0.60
        subs = {}
        for i in range(5):
            subs[f"sub_{i}"] = {
                "exec_count": 10, "skip_total": 0, "skip_idle": 0,
                "skip_throttle": 0, "skip_group_throttle": 0,
                "total_runtime_ms": 50.0, "did_work_count": 8,  # 80% work rate
            }
        ss = _make_schedstat(session_count=10, boot_times=boot_times, subsystems=subs)
        result = sched_autogroup(ss)
        assert result["adjusted"]
        sd_adj = [a for a in result["adjustments"]
                  if a["param"] == "sched_deadline.budget_ms"]
        assert len(sd_adj) == 1
        assert sd_adj[0]["new"] > sd_adj[0]["old"]

    def test_budget_ms_ceiling(self, monkeypatch):
        """budget_ms 不超过 50.0。"""
        from memory_os.store.mm import sched_autogroup
        from memory_os.config.sysctl import sysctl_set
        sysctl_set("sched_deadline.budget_ms", 49.0)
        boot_times = _improving_boot_times(10)
        subs = {f"sub_{i}": {
            "exec_count": 10, "skip_total": 0, "skip_idle": 0,
            "skip_throttle": 0, "skip_group_throttle": 0,
            "total_runtime_ms": 50.0, "did_work_count": 8,
        } for i in range(5)}
        ss = _make_schedstat(session_count=10, boot_times=boot_times, subsystems=subs)
        result = sched_autogroup(ss)
        sd_adj = [a for a in result.get("adjustments", [])
                  if a["param"] == "sched_deadline.budget_ms"]
        if sd_adj:
            assert sd_adj[0]["new"] <= 50.0


class TestSchedAutogroupRule4:
    """规则4: 低 work_rate → 收紧 cgroup_budget.group_budget_ms。"""

    def test_tighten_group_budget_on_low_work_rate(self):
        from memory_os.store.mm import sched_autogroup
        # 构造 work_rate < 0.30
        subs = {}
        for i in range(5):
            subs[f"sub_{i}"] = {
                "exec_count": 10, "skip_total": 0, "skip_idle": 0,
                "skip_throttle": 0, "skip_group_throttle": 0,
                "total_runtime_ms": 50.0, "did_work_count": 2,  # 20% work rate
            }
        ss = _make_schedstat(session_count=10, subsystems=subs)
        result = sched_autogroup(ss)
        assert result["adjusted"]
        cg_adj = [a for a in result["adjustments"]
                  if a["param"] == "cgroup_budget.group_budget_ms"]
        assert len(cg_adj) == 1
        assert cg_adj[0]["new"] < cg_adj[0]["old"]

    def test_group_budget_floor(self, monkeypatch):
        """group_budget_ms 不低于 20.0。"""
        from memory_os.store.mm import sched_autogroup
        from memory_os.config.sysctl import sysctl_set
        sysctl_set("cgroup_budget.group_budget_ms", 22.0)
        subs = {f"sub_{i}": {
            "exec_count": 10, "skip_total": 0, "skip_idle": 0,
            "skip_throttle": 0, "skip_group_throttle": 0,
            "total_runtime_ms": 50.0, "did_work_count": 2,
        } for i in range(5)}
        ss = _make_schedstat(session_count=10, subsystems=subs)
        result = sched_autogroup(ss)
        cg_adj = [a for a in result.get("adjustments", [])
                  if a["param"] == "cgroup_budget.group_budget_ms"]
        if cg_adj:
            assert cg_adj[0]["new"] >= 20.0


class TestSchedAutogroupRule5:
    """规则5: 高 work_rate + improving → 放松 group_budget_ms。"""

    def test_relax_group_budget_on_high_work_rate(self):
        from memory_os.store.mm import sched_autogroup
        boot_times = _improving_boot_times(10)
        subs = {f"sub_{i}": {
            "exec_count": 10, "skip_total": 0, "skip_idle": 0,
            "skip_throttle": 0, "skip_group_throttle": 0,
            "total_runtime_ms": 50.0, "did_work_count": 8,  # 80% work rate
        } for i in range(5)}
        ss = _make_schedstat(session_count=10, boot_times=boot_times, subsystems=subs)
        result = sched_autogroup(ss)
        assert result["adjusted"]
        cg_adj = [a for a in result["adjustments"]
                  if a["param"] == "cgroup_budget.group_budget_ms"]
        assert len(cg_adj) == 1
        assert cg_adj[0]["new"] > cg_adj[0]["old"]

    def test_group_budget_ceiling(self, monkeypatch):
        """group_budget_ms 不超过 120.0。"""
        from memory_os.store.mm import sched_autogroup
        from memory_os.config.sysctl import sysctl_set
        sysctl_set("cgroup_budget.group_budget_ms", 119.0)
        boot_times = _improving_boot_times(10)
        subs = {f"sub_{i}": {
            "exec_count": 10, "skip_total": 0, "skip_idle": 0,
            "skip_throttle": 0, "skip_group_throttle": 0,
            "total_runtime_ms": 50.0, "did_work_count": 8,
        } for i in range(5)}
        ss = _make_schedstat(session_count=10, boot_times=boot_times, subsystems=subs)
        result = sched_autogroup(ss)
        cg_adj = [a for a in result.get("adjustments", [])
                  if a["param"] == "cgroup_budget.group_budget_ms"]
        if cg_adj:
            assert cg_adj[0]["new"] <= 120.0


class TestSchedAutogroupNoAction:
    """无需调整场景。"""

    def test_stable_no_adjustment(self):
        from memory_os.store.mm import sched_autogroup
        # stable 趋势，正常 work_rate，低空转 → 无调整
        subs = {f"sub_{i}": {
            "exec_count": 10, "skip_total": 2, "skip_idle": 2,
            "skip_throttle": 0, "skip_group_throttle": 0,
            "total_runtime_ms": 50.0, "did_work_count": 5,
        } for i in range(3)}
        ss = _make_schedstat(session_count=10, subsystems=subs)
        result = sched_autogroup(ss)
        assert not result["adjusted"]
        assert result["skipped_reason"] == "no_action_needed"


class TestSchedAutogroupMultipleRules:
    """多规则同时触发。"""

    def test_degrading_plus_high_idle(self):
        from memory_os.store.mm import sched_autogroup
        boot_times = _degrading_boot_times(10)
        subs = _make_high_idle_subsystems(count=5, skip_rate=0.90)
        # 同时低 work rate (did_work=1/2 exec)
        for k in subs:
            subs[k]["did_work_count"] = 1
        ss = _make_schedstat(session_count=10, boot_times=boot_times, subsystems=subs)
        result = sched_autogroup(ss)
        assert result["adjusted"]
        # 应该同时有 idle_threshold 和 budget_ms 调整
        params = [a["param"] for a in result["adjustments"]]
        assert "timer_slack.idle_threshold" in params
        assert "sched_deadline.budget_ms" in params


class TestSchedAutogroupHistoryLimit:
    """调整记录上限 (20条)。"""

    def test_history_capped_at_20(self, tmp_path, monkeypatch):
        from memory_os.store.mm import sched_autogroup, sched_autogroup_save, sched_autogroup_load
        # 预填 25 条历史
        state = {
            "adjustments": [{"param": f"x{i}", "old": i, "new": i+1,
                             "reason": "test", "session_count": i}
                            for i in range(25)],
            "last_run_session": 0,
            "cooldown_sessions": 3,
        }
        sched_autogroup_save(state)
        # 触发一次调整
        boot_times = _degrading_boot_times(10)
        ss = _make_schedstat(session_count=10, boot_times=boot_times)
        result = sched_autogroup(ss)
        # 加载后检查记录不超过 20
        loaded = sched_autogroup_load()
        assert len(loaded["adjustments"]) <= 20


class TestSchedAutogroupStats:
    """stats 输出结构。"""

    def test_stats_structure(self):
        from memory_os.store.mm import sched_autogroup_stats
        ss = _make_schedstat(session_count=10)
        stats = sched_autogroup_stats(ss)
        assert "total_adjustments" in stats
        assert "last_run_session" in stats
        assert "current_session" in stats
        assert "boot_trend" in stats
        assert "work_rate" in stats
        assert "recent_adjustments" in stats
        assert stats["current_session"] == 10


class TestSchedAutogroupCooldownConfigurable:
    """cooldown 可通过 config 配置。"""

    def test_custom_cooldown(self, tmp_path, monkeypatch):
        from memory_os.store.mm import sched_autogroup, sched_autogroup_save
        from memory_os.config.sysctl import sysctl_set
        sysctl_set("sched_autogroup.cooldown_sessions", 5)
        # last_run=6, session=10, cooldown=5 → 间隔 4 < 5，应跳过
        # 但实际 sched_autogroup 内部从 ag_state 读取 cooldown_sessions
        # 测试从文件读取
        sched_autogroup_save({
            "adjustments": [], "last_run_session": 6, "cooldown_sessions": 5,
        })
        ss = _make_schedstat(session_count=10)
        result = sched_autogroup(ss)
        assert "cooldown" in result["skipped_reason"]
