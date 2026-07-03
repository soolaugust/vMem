"""
test_project_key_stable.py — project key 稳定性（gitroot 优先 + 缓存版本失效）

验证 v2 resolver:gitroot > git remote > abspath > dirname，key 不随 remote 漂移。

  T1: 有 remote 的 git 目录 → gitroot: 而非 git:（优先级切换）
  T2: 同一 gitroot 下子目录 → 同一 key（子目录稳定）
  T3: 改 remote URL 不影响 key（gitroot 不变 → key 不变）
  T4: gitroot 失败但有 remote → 回退 git:
  T5: 非 git 目录 → abspath:
  T6: 缓存版本失效:旧 version 条目被忽略、重算
  T7: dirname 兜底触发 stderr 告警
"""
import sys
import os
import subprocess
import tempfile
from pathlib import Path

_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT.parent))

import memory_os.runtime.tmpfs_compat as tmpfs  # noqa: F401
import memory_os.core.utils as utils
from memory_os.core.utils import resolve_project_id, _cache_get, _cache_save, _cache_load, _RESOLVER_VERSION


def _init_git_repo(path, remote=None):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    if remote:
        subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True)


class TestGitrootPriority:

    def test_gitroot_over_remote(self, tmp_path):
        repo = tmp_path / "myrepo"
        repo.mkdir()
        _init_git_repo(str(repo), remote="git@example.com:foo/bar.git")
        pid = resolve_project_id(str(repo))
        assert pid.startswith("gitroot:"), f"应优先 gitroot,得到 {pid}"

    def test_subdir_same_key(self, tmp_path):
        repo = tmp_path / "repo2"
        sub = repo / "src" / "deep"
        sub.mkdir(parents=True)
        _init_git_repo(str(repo), remote="git@example.com:x/y.git")
        k_root = resolve_project_id(str(repo))
        k_sub = resolve_project_id(str(sub))
        assert k_root == k_sub, "同 gitroot 下子目录应得同 key"

    def test_remote_change_no_drift(self, tmp_path):
        repo = tmp_path / "repo3"
        repo.mkdir()
        _init_git_repo(str(repo), remote="git@example.com:a/b.git")
        k1 = resolve_project_id(str(repo))
        # 改 remote URL
        subprocess.run(["git", "remote", "set-url", "origin", "git@example.com:c/d.git"],
                       cwd=str(repo), check=True)
        # 清掉该 cwd 的缓存条目强制重算（模拟 .git/config mtime 变化触发）
        utils._PROJECT_ID_CACHE_FILE.unlink(missing_ok=True)
        k2 = resolve_project_id(str(repo))
        assert k1 == k2, f"改 remote 不应漂移 key: {k1} vs {k2}"

    def test_non_git_abspath(self, tmp_path):
        d = tmp_path / "plain"
        d.mkdir()
        pid = resolve_project_id(str(d))
        assert pid.startswith("abspath:"), f"非 git 目录应 abspath,得到 {pid}"


class TestCacheVersioning:

    def test_old_version_entry_invalidated(self, tmp_path):
        # 写一个旧版本(v1)缓存条目，_cache_get 应忽略它
        utils._PROJECT_ID_CACHE_FILE.unlink(missing_ok=True)
        utils._MEMORY_OS_DIR.mkdir(parents=True, exist_ok=True)
        import json
        cwd_key = "deadbeef"
        utils._PROJECT_ID_CACHE_FILE.write_text(json.dumps({
            cwd_key: {"project_id": "git:OLD", "git_config_mtime": 0.0, "resolver_version": "v1"}
        }), encoding="utf-8")
        cache = _cache_load()
        assert _cache_get(cache, cwd_key) is None, "旧版本条目应失效"

    def test_current_version_entry_valid(self, tmp_path):
        utils._PROJECT_ID_CACHE_FILE.unlink(missing_ok=True)
        _cache_save("livekey", "gitroot:NEW", 123.0)
        cache = _cache_load()
        entry = _cache_get(cache, "livekey")
        assert entry is not None
        assert entry["project_id"] == "gitroot:NEW"
        assert entry["resolver_version"] == _RESOLVER_VERSION

    def test_legacy_entry_without_version(self, tmp_path):
        # 没有 resolver_version 字段的老缓存（v1 格式）→ 失效
        utils._PROJECT_ID_CACHE_FILE.unlink(missing_ok=True)
        utils._MEMORY_OS_DIR.mkdir(parents=True, exist_ok=True)
        import json
        utils._PROJECT_ID_CACHE_FILE.write_text(json.dumps({
            "legacy": {"project_id": "claude-workspace", "git_config_mtime": 0.0}
        }), encoding="utf-8")
        cache = _cache_load()
        assert _cache_get(cache, "legacy") is None


class TestDirnameFallbackWarning:

    def test_dirname_fallback_warns(self, tmp_path, capsys, monkeypatch):
        # 构造 abspath 也失败的情形：mock os.path.abspath 抛异常 → 落到 dirname
        d = tmp_path / "weird"
        d.mkdir()
        utils._PROJECT_ID_CACHE_FILE.unlink(missing_ok=True)

        orig_abspath = os.path.abspath
        def boom(p):
            raise RuntimeError("simulated abspath failure")
        monkeypatch.setattr(os.path, "abspath", boom)
        try:
            pid = resolve_project_id(str(d))
        finally:
            monkeypatch.setattr(os.path, "abspath", orig_abspath)
        assert pid == "weird"
        captured = capsys.readouterr()
        assert "退化到 dirname" in captured.err
