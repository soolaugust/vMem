import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _copy_include(src: Path, dst: Path, rel: str) -> None:
    source = src / rel
    target = dst / rel
    if source.is_dir():
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _package_tree(tmp_path: Path) -> Path:
    package_root = tmp_path / "pkg"
    includes = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["hatch"]["build"]["targets"]["wheel"]["only-include"]
    for rel in includes:
        _copy_include(ROOT, package_root, rel)
    return package_root


def _run_import(package_root: Path, statement: str) -> subprocess.CompletedProcess[str]:
    tmp_home = package_root.parent / "home"
    tmp_home.mkdir(exist_ok=True)
    env = {
        "PYTHONPATH": str(package_root),
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_home),
    }
    return subprocess.run(
        [sys.executable, "-S", "-c", statement],
        cwd=str(package_root.parent),
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
    )


def test_package_tree_imports_new_package_entrypoints(tmp_path: Path) -> None:
    package_root = _package_tree(tmp_path)

    result = _run_import(package_root, "import memory_os.store.core; import memory_os.store.mm; import memory_os.store.vfs; import memory_os.store.vfs_effects; import memory_os.vfs.backend_sqlite")
    assert result.returncode == 0, result.stderr

    result = _run_import(package_root, "from memory_os.store import api, mm_compat, vfs_compat; from memory_os.vfs import api as vfs_api; assert hasattr(vfs_compat, '_safe_add_column'); assert hasattr(vfs_compat, '_fts5_escape')")
    assert result.returncode == 0, result.stderr

    assert list(package_root.glob('*.py')) == []


def test_packaged_hooks_json_references_existing_files(tmp_path: Path) -> None:
    package_root = _package_tree(tmp_path)
    hooks_json = package_root / "hooks" / "hooks.json"
    text = hooks_json.read_text(encoding="utf-8")
    refs = sorted(set(ref.rstrip('\\') for ref in re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/([^\"\s]+)", text)))

    assert refs
    missing = [ref for ref in refs if not (package_root / ref).exists()]
    assert missing == []



def test_package_modules_do_not_import_root_shims() -> None:
    import re

    forbidden = [
        r"^\s*import\s+assertion_history\b",
        r"^\s*from\s+assertion_history\s+import\b",
        r"^\s*import\s+knowledge_vfs_init\b",
        r"^\s*from\s+knowledge_vfs_init\s+import\b",
        r"^\s*import\s+store_criu\b",
        r"^\s*from\s+store_criu\s+import\b",
        r"^\s*import\s+store_vfs\b",
        r"^\s*from\s+store_vfs\s+import\b",
        r"^\s*import\s+store_vfs_effects_new\b",
        r"^\s*from\s+store_vfs_effects_new\s+import\b",
        r"^\s*import\s+store_core\b",
        r"^\s*from\s+store_core\s+import\b",
        r"^\s*import\s+vfs_core\b",
        r"^\s*from\s+vfs_core\s+import\b",
        r"^\s*import\s+bm25\b",
        r"^\s*from\s+bm25\s+import\b",
        r"^\s*import\s+schema\b",
        r"^\s*from\s+schema\s+import\b",
        r"^\s*import\s+scorer\b",
        r"^\s*from\s+scorer\s+import\b",
        r"^\s*import\s+utils\b",
        r"^\s*from\s+utils\s+import\b",
    ]
    offenders = []
    for path in (ROOT / "memory_os").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            if re.search(pattern, text, re.MULTILINE):
                offenders.append(f"{path.relative_to(ROOT)}: {pattern}")
    assert offenders == []
