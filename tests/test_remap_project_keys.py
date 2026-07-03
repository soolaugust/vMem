"""
test_remap_project_keys.py — 存量 project key 迁移脚本

  T1: 显式 --map 映射生效
  T2: protected key(global/__semantic__/空) 永不迁移
  T3: dry-run 不改库
  T4: 实际迁移 UPDATE 正确、行数对
  T5: 已是正确 key 不迁(old==new 跳过)
  T6: 未匹配映射的 git:/gitroot:/abspath: 不动(除非显式 --map)
"""
import sys
import os
import sqlite3
from pathlib import Path

_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT.parent))

import memory_os.runtime.tmpfs_compat as tmpfs  # noqa: F401
from tools import remap_project_keys as R


def _make_db(path, chunks):
    """chunks: [(id, project), ...]"""
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE memory_chunks(id TEXT PRIMARY KEY, project TEXT, content TEXT)")
    for cid, proj in chunks:
        c.execute("INSERT INTO memory_chunks VALUES(?,?,?)", (cid, proj, "body"))
    c.commit()
    c.close()


def _projects(path):
    c = sqlite3.connect(path)
    rows = dict(c.execute("SELECT project, COUNT(*) FROM memory_chunks GROUP BY project").fetchall())
    c.close()
    return rows


class TestExplicitMap:

    def test_explicit_map_applied(self, tmp_path):
        p = str(tmp_path / "db.db")
        _make_db(p, [("a", "claude-workspace"), ("b", "claude-workspace"), ("c", "git:keep")])
        stats = R.apply_remap(p, {"claude-workspace": "gitroot:NEW"}, dry_run=False)
        assert stats["total_rows"] == 2
        proj = _projects(p)
        assert proj.get("gitroot:NEW") == 2
        assert "claude-workspace" not in proj
        assert proj.get("git:keep") == 1  # 未映射的不动

    def test_explicit_map_git_key(self, tmp_path):
        # 计划关键风险点:活跃 git: key 也能被显式迁到 gitroot:
        p = str(tmp_path / "db.db")
        _make_db(p, [("a", "git:a0ab"), ("b", "git:a0ab")])
        R.apply_remap(p, {"git:a0ab": "gitroot:ea65"}, dry_run=False)
        proj = _projects(p)
        assert proj.get("gitroot:ea65") == 2
        assert "git:a0ab" not in proj


class TestProtected:

    def test_global_never_migrated(self, tmp_path):
        p = str(tmp_path / "db.db")
        _make_db(p, [("a", "global"), ("b", "__semantic__")])
        # 即使显式 map global 也应被 protected 拦截
        stats = R.apply_remap(p, {"global": "gitroot:X"}, dry_run=False)
        assert "global" not in stats["mappings"]
        proj = _projects(p)
        assert proj.get("global") == 1
        assert proj.get("__semantic__") == 1


class TestDryRun:

    def test_dry_run_no_change(self, tmp_path):
        p = str(tmp_path / "db.db")
        _make_db(p, [("a", "claude-workspace")])
        stats = R.apply_remap(p, {"claude-workspace": "gitroot:NEW"}, dry_run=True)
        assert stats["total_rows"] == 1  # 报告会迁 1 行
        proj = _projects(p)
        assert proj.get("claude-workspace") == 1  # 但实际没改
        assert "gitroot:NEW" not in proj


class TestNoOp:

    def test_same_key_skipped(self, tmp_path):
        p = str(tmp_path / "db.db")
        _make_db(p, [("a", "gitroot:X")])
        stats = R.apply_remap(p, {"gitroot:X": "gitroot:X"}, dry_run=False)
        assert stats["mappings"] == {}  # old==new 不迁

    def test_unmapped_prefixed_keys_untouched(self, tmp_path):
        p = str(tmp_path / "db.db")
        _make_db(p, [("a", "git:foo"), ("b", "gitroot:bar"), ("c", "abspath:baz")])
        stats = R.apply_remap(p, {}, dry_run=False)  # 无显式映射
        assert stats["mappings"] == {}  # 带前缀的 key 不自动迁(只有 dirname 孤儿才自动反解)
        proj = _projects(p)
        assert proj.get("git:foo") == 1
        assert proj.get("gitroot:bar") == 1
        assert proj.get("abspath:baz") == 1
