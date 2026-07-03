"""
jsonl_import.py — 从 Claude Code .jsonl transcript 导入 session 到 replay_events

OS 类比：strace -o trace.log + strace replay — 从 trace 文件重建进程执行历史。

导入后可在 Viewer 中回放历史 session 的 tool calls 和消息流。
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory_os.store.vfs_compat import open_db
from memory_os.store.episodes import ensure_replay_schema, record_replay_event

_DEFAULT_DB = os.path.expanduser("~/.claude/memory-os/store.db")


def parse_jsonl(filepath: str) -> list:
    """
    解析 Claude Code .jsonl transcript，提取有意义的事件。

    事件类型映射：
      user message → query_received
      assistant message with tool_use → tool_call
      assistant text → response
      system → system_event
    """
    events = []
    session_id = Path(filepath).stem[:16]

    with open(filepath, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line.strip())
            except json.JSONDecodeError:
                continue

            msg_type = d.get("type")
            if msg_type not in ("user", "assistant", "system"):
                continue
            if d.get("isSnapshotUpdate"):
                continue

            message = d.get("message") or d.get("snapshot", {}).get("message", {})
            if not message:
                continue

            role = message.get("role", msg_type)
            content = message.get("content", "")
            timestamp = d.get("timestamp") or datetime.now(timezone.utc).isoformat()

            if role == "user":
                text = _extract_text(content)
                if text and len(text) > 5:
                    events.append({
                        "session_id": session_id,
                        "event_type": "query_received",
                        "timestamp": timestamp,
                        "data": {"role": "user", "text": text[:200]},
                    })

            elif role == "assistant":
                # Check for tool_use blocks
                tool_uses = _extract_tool_uses(content)
                if tool_uses:
                    for tool in tool_uses:
                        events.append({
                            "session_id": session_id,
                            "event_type": "tool_call",
                            "timestamp": timestamp,
                            "data": {
                                "tool": tool.get("name", "unknown"),
                                "input_preview": json.dumps(tool.get("input", {}), ensure_ascii=False)[:150],
                            },
                        })
                else:
                    text = _extract_text(content)
                    if text and len(text) > 10:
                        events.append({
                            "session_id": session_id,
                            "event_type": "response",
                            "timestamp": timestamp,
                            "data": {"text": text[:200]},
                        })

    return events


def _extract_text(content) -> str:
    """从 content (str or list) 中提取纯文本。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
            elif isinstance(block, str):
                texts.append(block)
        return " ".join(texts).strip()
    return ""


def _extract_tool_uses(content) -> list:
    """从 content list 中提取 tool_use blocks。"""
    if not isinstance(content, list):
        return []
    tools = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tools.append(block)
    return tools


def import_jsonl(filepath: str, db_path: str = None) -> dict:
    """
    导入 .jsonl 文件到 replay_events。

    Returns:
        {"session_id": str, "events_imported": int, "filepath": str}
    """
    db_path = db_path or os.environ.get("MEMORY_OS_DB", _DEFAULT_DB)
    events = parse_jsonl(filepath)

    if not events:
        return {"session_id": "", "events_imported": 0, "filepath": filepath}

    conn = open_db(db_path)
    ensure_replay_schema(conn)

    session_id = events[0]["session_id"]
    imported = 0
    for ev in events:
        try:
            record_replay_event(
                conn, ev["session_id"], ev["event_type"],
                data=ev.get("data"), duration_ms=0,
            )
            imported += 1
        except Exception:
            pass

    conn.close()
    return {"session_id": session_id, "events_imported": imported, "filepath": filepath}


def import_directory(dirpath: str, db_path: str = None) -> list:
    """导入目录下所有 .jsonl 文件。"""
    results = []
    p = Path(dirpath)
    for f in sorted(p.glob("*.jsonl")):
        if f.stat().st_size > 50_000_000:  # skip >50MB files
            continue
        r = import_jsonl(str(f), db_path)
        if r["events_imported"] > 0:
            results.append(r)
    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Import Claude Code JSONL transcripts")
    parser.add_argument("path", help="JSONL file or directory")
    parser.add_argument("--db", default=None)
    args = parser.parse_args()

    path = Path(args.path)
    if path.is_dir():
        results = import_directory(str(path), args.db)
        for r in results:
            print(f"  {r['session_id']}: {r['events_imported']} events <- {Path(r['filepath']).name}")
        print(f"\nTotal: {len(results)} sessions imported")
    elif path.is_file():
        r = import_jsonl(str(path), args.db)
        print(f"Session {r['session_id']}: {r['events_imported']} events imported")
    else:
        print(f"Error: {path} not found")
        sys.exit(1)
