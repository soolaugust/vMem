#!/usr/bin/env python3
"""Sync project file memories into memory-os chunks.

This is a compatibility bridge for environments that still emit
~/.claude/projects/*/memory/*.md files while memory-os is the knowledge source
of truth. It imports each markdown memory as an ACTIVE chunk so /clear recovery
can use memory_lookup instead of depending on file-memory injection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import sys
MEMORY_OS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MEMORY_OS_DIR))

from memory_os.core.utils import resolve_project_id  # noqa: E402

DB_PATH = Path.home() / ".claude" / "memory-os" / "store.db"
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def parse_memory_file(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8", errors="replace")
    metadata: dict[str, object] = {}
    body = raw
    match = FRONTMATTER_RE.match(raw)
    if match:
        frontmatter, body = match.groups()
        metadata = parse_frontmatter(frontmatter)
    name = str(metadata.get("name") or path.stem)
    description = str(metadata.get("description") or extract_summary(body, path.stem))
    chunk_type = "reference"
    meta = metadata.get("metadata")
    if isinstance(meta, dict) and isinstance(meta.get("type"), str):
        chunk_type = meta["type"]
    return {
        "name": name,
        "summary": description,
        "content": body.strip() or raw.strip(),
        "chunk_type": normalize_chunk_type(chunk_type),
        "tags": ["memory-md", name, path.name],
    }


def parse_frontmatter(text: str) -> dict:
    result: dict[str, object] = {}
    current_parent: str | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith("  ") and current_parent:
            key, sep, value = line.strip().partition(":")
            if sep:
                parent = result.setdefault(current_parent, {})
                if isinstance(parent, dict):
                    parent[key.strip()] = value.strip()
            continue
        key, sep, value = line.partition(":")
        if sep:
            key = key.strip()
            value = value.strip()
            if value:
                result[key] = value
                current_parent = None
            else:
                result[key] = {}
                current_parent = key
    return result


def normalize_chunk_type(value: str) -> str:
    mapping = {
        "user": "design_constraint",
        "feedback": "design_constraint",
        "project": "decision",
        "reference": "reference",
    }
    return mapping.get(value, value or "reference")


def extract_summary(body: str, fallback: str) -> str:
    for line in body.splitlines():
        stripped = line.strip().strip("# ")
        if len(stripped) >= 10:
            return stripped[:160]
    return fallback[:160]


def chunk_id(project: str, path: Path, content: str) -> str:
    digest = hashlib.sha256(f"{project}\n{path}\n{content}".encode()).hexdigest()[:24]
    return f"memory-md:{digest}"


def sync_file(conn: sqlite3.Connection, path: Path, project: str, dry_run: bool) -> tuple[str, str]:
    item = parse_memory_file(path)
    cid = chunk_id(project, path, item["content"])
    now = datetime.now(timezone.utc).isoformat()
    tags = json.dumps(item["tags"], ensure_ascii=False)
    existing = conn.execute("SELECT id FROM memory_chunks WHERE id=?", (cid,)).fetchone()
    if dry_run:
        return ("exists" if existing else "would_insert", cid)
    conn.execute(
        """INSERT OR REPLACE INTO memory_chunks
           (id, created_at, updated_at, project, source_session, chunk_type,
            content, summary, tags, importance, retrievability, last_accessed,
            source_type, source_reliability, chunk_state, access_count, apply_count)
           VALUES (?, COALESCE((SELECT created_at FROM memory_chunks WHERE id=?), ?), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE',
                   COALESCE((SELECT access_count FROM memory_chunks WHERE id=?), 0),
                   COALESCE((SELECT apply_count FROM memory_chunks WHERE id=?), 0))""",
        (
            cid, cid, now, now, project, "memory-md-sync", item["chunk_type"],
            item["content"], item["summary"], tags, 0.9, 0.8, now, "memory-md", 0.95, cid, cid,
        ),
    )
    rowid = conn.execute("SELECT rowid FROM memory_chunks WHERE id=?", (cid,)).fetchone()[0]
    conn.execute("DELETE FROM memory_chunks_fts WHERE rowid_ref=?", (str(rowid),))
    conn.execute(
        "INSERT INTO memory_chunks_fts(rowid_ref, summary, content) VALUES (?, ?, ?)",
        (str(rowid), item["summary"], item["content"]),
    )
    return ("updated" if existing else "inserted", cid)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("memory_dir", nargs="?", default=str(Path.home() / ".claude" / "projects"),
                        help="project memory root or specific memory directory")
    parser.add_argument("--project-root", default=os.getcwd(), help="repo root used to resolve memory-os project id")
    parser.add_argument("--project", help="explicit memory-os project id")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = Path(args.memory_dir).expanduser()
    if root.name == "memory":
        files = sorted(p for p in root.glob("*.md") if p.name != "MEMORY.md")
    else:
        files = sorted(p for p in root.glob("*/memory/*.md") if p.name != "MEMORY.md")
    project = args.project or resolve_project_id(str(Path(args.project_root).expanduser()))

    conn = sqlite3.connect(Path(args.db).expanduser())
    required = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = {"memory_chunks", "memory_chunks_fts"} - required
    if missing:
        raise SystemExit(f"memory-os DB missing tables: {sorted(missing)}")
    results = []
    for path in files:
        status, cid = sync_file(conn, path, project, args.dry_run)
        results.append({"file": str(path), "status": status, "chunk_id": cid})
    if not args.dry_run:
        conn.commit()
    print(json.dumps({"project": project, "count": len(results), "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
