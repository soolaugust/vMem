"""Context Kernel — virtual-memory style governance for prompt context.

This module is the memory-os owned context manager.  Hooks should be thin
syscall adapters; policy and accounting live here so every context producer can
share the same page table, cgroup budgets, admission and reclaim semantics.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

DEFAULT_ROOT = Path.home() / ".claude" / "context-kernel"
DEFAULT_MAX_RESIDENT_BYTES = 80 * 1024
DEFAULT_READ_LIMIT_LINES = 2000
DEFAULT_GREP_HEAD_LIMIT = 80
PAGE_TABLE_FILE = "page_table.jsonl"
CGROUP_STATE_FILE = "cgroup_state.json"
WORKING_SET_MANIFEST_FILE = "working_set_manifest.json"
DEFAULT_THREAD_BUDGETS: dict[str, int] = {
    "main": 16_000,
    "code": 24_000,
    "evidence": 8_000,
    "tools": 4_000,
    "memory": 6_000,
    "agents": 8_000,
    "governance": 6_000,
}

ContextSource = Literal["Read", "Grep", "Bash", "Agent", "memory_os", "hook", "user", "assistant", "unknown"]
Decision = Literal["allow", "update", "block"]
ContextThread = Literal["main", "code", "evidence", "tools", "memory", "agents", "governance"]
ContextPageType = Literal["register", "claim", "evidence", "tool_result", "hook_payload", "memory", "agent_join", "decision", "unknown"]

HIGH_OUTPUT_BASH_RE = re.compile(
    r"\b(git\s+(diff|log|show)|pytest\b.*(-vv|\s-s\b)|python3?\s+-m\s+pytest.*(-vv|\s-s\b)|"
    r"feishu\s+(docx\s+read|bitable\s+(records|search))|cat|sed|awk|grep|rg)\b"
)


@dataclass(frozen=True)
class ContextPage:
    page_id: str
    source: ContextSource
    cgroup: str
    resident: bool
    size_bytes: int
    summary: str
    evidence_uri: str
    created_at: float
    last_accessed: float
    access_count: int = 0
    importance: float = 0.5
    metadata: dict[str, Any] | None = None
    thread: str = "tools"
    page_type: str = "unknown"
    semantic_key: str = ""
    hotness: float = 0.0
    dirty: bool = False
    dependencies: list[str] | None = None


@dataclass(frozen=True)
class WorkingSetManifest:
    manifest_id: str
    created_at: float
    registers: dict[str, Any]
    hot_pages: list[dict[str, Any]]
    cold_refs: list[dict[str, Any]]
    budgets: dict[str, int]
    usage: dict[str, int]
    pressure: str


@dataclass(frozen=True)
class TranscriptExtractionResult:
    transcript: str
    pages_created: int
    bytes_offloaded: int
    manifest: WorkingSetManifest


@dataclass(frozen=True)
class AdmissionResult:
    decision: Decision
    reason: str = ""
    updated_input: dict[str, Any] | None = None
    page: ContextPage | None = None


@dataclass(frozen=True)
class CgroupUsage:
    resident_bytes: int = 0
    swapped_bytes: int = 0
    pages: int = 0


def kernel_root() -> Path:
    return Path(os.environ.get("CONTEXT_KERNEL_DIR", str(DEFAULT_ROOT))).expanduser()


def max_resident_output_bytes() -> int:
    return int(os.environ.get("CONTEXT_KERNEL_MAX_OUTPUT_BYTES", str(DEFAULT_MAX_RESIDENT_BYTES)))


def default_read_limit_lines() -> int:
    return int(os.environ.get("CONTEXT_KERNEL_DEFAULT_READ_LIMIT_LINES", str(DEFAULT_READ_LIMIT_LINES)))


def max_grep_head_limit() -> int:
    return int(os.environ.get("CONTEXT_KERNEL_GREP_HEAD_LIMIT", str(DEFAULT_GREP_HEAD_LIMIT)))


def _page_table_path(root: Path | None = None) -> Path:
    return (root or kernel_root()) / PAGE_TABLE_FILE


def _cgroup_state_path(root: Path | None = None) -> Path:
    return (root or kernel_root()) / CGROUP_STATE_FILE


def _now() -> float:
    return time.time()


def _page_id(source: str, meta: dict[str, Any]) -> str:
    raw = json.dumps({"source": source, "meta": meta, "ts": _now()}, ensure_ascii=False, sort_keys=True)
    import zlib

    return f"ctx-{zlib.crc32(raw.encode('utf-8')) & 0xFFFFFFFF:08x}"


def _stable_page_id(source: str, semantic_key: str, evidence_uri: str = "") -> str:
    import zlib

    raw = json.dumps({"source": source, "semantic_key": semantic_key, "evidence_uri": evidence_uri}, ensure_ascii=False, sort_keys=True)
    return f"ctx-{zlib.crc32(raw.encode('utf-8')) & 0xFFFFFFFF:08x}"


def _hash_text(value: str) -> str:
    import zlib

    return f"{zlib.crc32(value.encode('utf-8', errors='replace')) & 0xFFFFFFFF:08x}"


def record_page(
    source: ContextSource,
    *,
    cgroup: str,
    size_bytes: int,
    summary: str,
    resident: bool = False,
    evidence_uri: str = "",
    metadata: dict[str, Any] | None = None,
    root: Path | None = None,
    thread: str = "tools",
    page_type: str = "unknown",
    semantic_key: str = "",
    importance: float = 0.5,
    hotness: float = 0.0,
    dirty: bool = False,
    dependencies: list[str] | None = None,
) -> ContextPage:
    root = root or kernel_root()
    root.mkdir(parents=True, exist_ok=True)
    meta = metadata or {}
    stable_key = semantic_key or str(meta.get("stable_key") or "")
    page = ContextPage(
        page_id=_stable_page_id(source, stable_key, evidence_uri) if stable_key else _page_id(source, meta),
        source=source,
        cgroup=cgroup,
        resident=resident,
        size_bytes=size_bytes,
        summary=summary,
        evidence_uri=evidence_uri,
        created_at=_now(),
        last_accessed=_now(),
        importance=importance,
        metadata=meta,
        thread=thread,
        page_type=page_type,
        semantic_key=semantic_key,
        hotness=hotness,
        dirty=dirty,
        dependencies=dependencies or [],
    )
    with _page_table_path(root).open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(page), ensure_ascii=False, sort_keys=True) + "\n")
    update_cgroup_usage(root=root)
    return page


def iter_pages(root: Path | None = None) -> list[ContextPage]:
    path = _page_table_path(root)
    if not path.exists():
        return []
    pages: list[ContextPage] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            data = json.loads(line)
            data.setdefault("thread", "tools")
            data.setdefault("page_type", "unknown")
            data.setdefault("semantic_key", "")
            data.setdefault("hotness", 0.0)
            data.setdefault("dirty", False)
            data.setdefault("dependencies", [])
            pages.append(ContextPage(**data))
        except Exception:
            continue
    return pages


def update_cgroup_usage(root: Path | None = None) -> dict[str, CgroupUsage]:
    root = root or kernel_root()
    usage: dict[str, CgroupUsage] = {}
    for page in iter_pages(root):
        old = usage.get(page.cgroup, CgroupUsage())
        usage[page.cgroup] = CgroupUsage(
            resident_bytes=old.resident_bytes + (page.size_bytes if page.resident else 0),
            swapped_bytes=old.swapped_bytes + (0 if page.resident else page.size_bytes),
            pages=old.pages + 1,
        )
    root.mkdir(parents=True, exist_ok=True)
    _cgroup_state_path(root).write_text(
        json.dumps({k: asdict(v) for k, v in usage.items()}, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return usage


def _estimate_read_slice(path: Path, offset: int, limit: int | None, max_bytes: int) -> tuple[int, int]:
    max_lines = default_read_limit_lines() if limit is None else max(limit, 0)
    start = max(offset, 0)
    total = 0
    seen = 0
    with path.open("rb") as f:
        for idx, line in enumerate(f, start=1):
            if idx <= start:
                continue
            if seen >= max_lines:
                break
            total += len(line)
            seen += 1
            if total > max_bytes:
                break
    return total, seen


def admit_read(tool_input: dict[str, Any], root: Path | None = None) -> AdmissionResult:
    file_path = str(tool_input.get("file_path") or "")
    if not file_path:
        return AdmissionResult("allow")
    path = Path(file_path).expanduser()
    try:
        if not path.is_file():
            return AdmissionResult("allow")
    except OSError:
        return AdmissionResult("allow")

    raw_limit = tool_input.get("limit")
    offset = int(tool_input.get("offset") or 0)
    limit = int(raw_limit) if isinstance(raw_limit, int) or (isinstance(raw_limit, str) and raw_limit.isdigit()) else None
    estimated, sampled = _estimate_read_slice(path, offset, limit, max_resident_output_bytes())
    if estimated <= max_resident_output_bytes():
        return AdmissionResult("allow")

    page = record_page(
        "Read",
        cgroup="tool_output",
        size_bytes=estimated,
        summary=f"Read output for {path.name} offset={offset} limit={limit or default_read_limit_lines()} paged out",
        resident=False,
        evidence_uri=str(path),
        metadata={"path": str(path), "offset": offset, "limit": limit, "sampled_lines": sampled},
        root=root,
    )
    if limit is None:
        reason = (
            f"[context_kernel] BLOCKED Read of {path.name}: default Read slice "
            f"({default_read_limit_lines()} lines from offset={offset}) is estimated {estimated // 1024}KB, "
            f"above resident output limit {max_resident_output_bytes() // 1024}KB. "
            "Use Grep/LSP or Read with a smaller explicit offset+limit targeted range."
        )
    else:
        reason = (
            f"[context_kernel] BLOCKED Read of {path.name}: requested slice offset={offset} limit={limit} "
            f"is estimated {estimated // 1024}KB, above resident output limit "
            f"{max_resident_output_bytes() // 1024}KB. Use smaller limit or narrower offset."
        )
    return AdmissionResult("block", reason=reason, page=page)


def admit_grep(tool_input: dict[str, Any]) -> AdmissionResult:
    if str(tool_input.get("output_mode") or "files_with_matches") != "content":
        return AdmissionResult("allow")
    head_limit = tool_input.get("head_limit")
    if head_limit is None or head_limit == 0:
        updated = {**tool_input, "head_limit": max_grep_head_limit()}
        return AdmissionResult("update", reason=f"[context_kernel] Clamped Grep content output head_limit to {max_grep_head_limit()}", updated_input=updated)
    try:
        parsed = int(head_limit)
    except Exception:
        return AdmissionResult("allow")
    if parsed > max_grep_head_limit():
        updated = {**tool_input, "head_limit": max_grep_head_limit()}
        return AdmissionResult("update", reason=f"[context_kernel] Clamped Grep content output head_limit from {parsed} to {max_grep_head_limit()}", updated_input=updated)
    return AdmissionResult("allow")


def split_shell_segments(command: str) -> list[str]:
    return [seg.strip() for seg in re.split(r"(?:&&|\|\||;|\n)", command) if seg.strip()]


def is_bounded_shell_segment(segment: str) -> bool:
    if not HIGH_OUTPUT_BASH_RE.search(segment):
        return True
    if re.search(r"\btee\b", segment):
        return False
    if re.search(r"(?:^|\s)1?>\s*\S+|&>\s*\S+", segment):
        return True
    if re.search(r"\|\s*(head|tail)\b[^|;]*$", segment):
        return True
    if re.match(r"\s*(head|tail)\s+-n\s+\d+", segment):
        return True
    if re.search(r"\b(grep|rg)\b.*(-m\s+\d+|--max-count\b)", segment):
        return True
    if re.search(r"\bgit\s+(diff|log|show)\b", segment):
        return bool(re.search(r"--stat\b|--name-only\b|--name-status\b|--oneline\b", segment))
    if re.search(r"\b(pytest|python3?\s+-m\s+pytest)\b", segment):
        return bool(re.search(r"--maxfail=\d+", segment))
    return False


def admit_bash(tool_input: dict[str, Any]) -> AdmissionResult:
    cmd = str(tool_input.get("command") or "")
    if not cmd:
        return AdmissionResult("allow")
    unbounded = [seg for seg in split_shell_segments(cmd) if not is_bounded_shell_segment(seg)]
    if not unbounded:
        return AdmissionResult("allow")
    reason = (
        "[context_kernel] BLOCKED shell command segment with potentially unbounded output: "
        f"{unbounded[0][:120]}. "
        "Each high-output segment must prove bounded output (head/tail/-m/--max-count/--stat/--oneline/--maxfail) "
        "or redirect full output to a file and print only a bounded summary."
    )
    record_page(
        "Bash",
        cgroup="tool_output",
        size_bytes=0,
        summary="blocked unbounded shell output before execution",
        resident=False,
        metadata={"command": cmd, "unbounded_segment": unbounded[0]},
    )
    return AdmissionResult("block", reason=reason)


def admit_tool(tool_name: str, tool_input: dict[str, Any], root: Path | None = None) -> AdmissionResult:
    if tool_name == "Read":
        return admit_read(tool_input, root=root)
    if tool_name == "Grep":
        return admit_grep(tool_input)
    if tool_name == "Bash":
        return admit_bash(tool_input)
    return AdmissionResult("allow")


# ── Full Context Kernel components ──────────────────────────────────────────

@dataclass(frozen=True)
class ContextAccount:
    prompt_bytes: int
    transcript_bytes: int
    static_bytes: int
    page_resident_bytes: int
    page_swapped_bytes: int
    projected_bytes: int
    pressure: str
    by_cgroup: dict[str, dict[str, int]]


@dataclass(frozen=True)
class ReclaimResult:
    target_bytes: int
    freed_bytes: int
    reclaimed_pages: list[str]
    remaining_resident_bytes: int


@dataclass(frozen=True)
class OomDecision:
    decision: str
    pressure: str
    reason: str
    account: ContextAccount
    reclaim: ReclaimResult | None = None
    recovery_context: str = ""


def pages_dir(root: Path | None = None) -> Path:
    path = (root or kernel_root()) / "pages"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _rewrite_pages(pages: list[ContextPage], root: Path | None = None) -> None:
    root = root or kernel_root()
    root.mkdir(parents=True, exist_ok=True)
    with _page_table_path(root).open("w", encoding="utf-8") as f:
        for page in pages:
            f.write(json.dumps(asdict(page), ensure_ascii=False, sort_keys=True) + "\n")
    update_cgroup_usage(root=root)


def swap_out_text(
    source: ContextSource,
    text: str,
    *,
    cgroup: str,
    summary: str = "",
    metadata: dict[str, Any] | None = None,
    root: Path | None = None,
    importance: float = 0.5,
    thread: str = "tools",
    page_type: str = "unknown",
    semantic_key: str = "",
    hotness: float = 0.0,
    dirty: bool = False,
    dependencies: list[str] | None = None,
) -> ContextPage:
    root = root or kernel_root()
    meta = metadata or {}
    stable_key = semantic_key or str(meta.get("stable_key") or "")
    page_id = _stable_page_id(source, stable_key, str(meta.get("transcript", ""))) if stable_key else _page_id(source, {**meta, "size": len(text)})
    page_path = pages_dir(root) / f"{page_id}.txt"
    page_path.write_text(text, encoding="utf-8", errors="replace")
    page = ContextPage(
        page_id=page_id,
        source=source,
        cgroup=cgroup,
        resident=False,
        size_bytes=len(text.encode("utf-8", errors="replace")),
        summary=summary or f"{source} output swapped out ({len(text)} chars)",
        evidence_uri=str(page_path),
        created_at=_now(),
        last_accessed=_now(),
        importance=importance,
        metadata=meta,
        thread=thread,
        page_type=page_type,
        semantic_key=semantic_key,
        hotness=hotness,
        dirty=dirty,
        dependencies=dependencies or [],
    )
    root.mkdir(parents=True, exist_ok=True)
    with _page_table_path(root).open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(page), ensure_ascii=False, sort_keys=True) + "\n")
    update_cgroup_usage(root=root)
    return page


def page_fault(page_id: str, *, offset: int = 0, limit: int = 80, root: Path | None = None) -> str:
    root = root or kernel_root()
    pages = iter_pages(root)
    out_pages: list[ContextPage] = []
    target: ContextPage | None = None
    for page in pages:
        if page.page_id == page_id:
            target = page
            out_pages.append(ContextPage(**{**asdict(page), "last_accessed": _now(), "access_count": page.access_count + 1}))
        else:
            out_pages.append(page)
    if target is None:
        raise KeyError(page_id)
    _rewrite_pages(out_pages, root)
    source = Path(target.evidence_uri)
    if not source.exists():
        return ""
    lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(offset, 0)
    end = start + max(limit, 0)
    return "\n".join(lines[start:end])


def account_context(
    *,
    prompt: str = "",
    transcript_path: Path | None = None,
    static_bytes: int = 180_000,
    transcript_tail_bytes: int = 4_000_000,
    root: Path | None = None,
) -> ContextAccount:
    root = root or kernel_root()
    prompt_bytes = len(prompt.encode("utf-8", errors="replace"))
    transcript_bytes = 0
    if transcript_path and transcript_path.exists():
        try:
            transcript_bytes = min(transcript_path.stat().st_size, transcript_tail_bytes)
        except OSError:
            transcript_bytes = 0
    usage = update_cgroup_usage(root=root)
    page_resident = sum(v.resident_bytes for v in usage.values())
    page_swapped = sum(v.swapped_bytes for v in usage.values())
    projected = prompt_bytes + transcript_bytes + static_bytes + page_resident
    warn = int(os.environ.get("CONTEXT_KERNEL_WARN_BYTES", str(700_000)))
    hard = int(os.environ.get("CONTEXT_KERNEL_HARD_BYTES", str(900_000)))
    pressure = "low"
    if projected >= hard:
        pressure = "critical"
    elif projected >= warn:
        pressure = "high"
    return ContextAccount(
        prompt_bytes=prompt_bytes,
        transcript_bytes=transcript_bytes,
        static_bytes=static_bytes,
        page_resident_bytes=page_resident,
        page_swapped_bytes=page_swapped,
        projected_bytes=projected,
        pressure=pressure,
        by_cgroup={k: asdict(v) for k, v in usage.items()},
    )


def reclaim(target_bytes: int, *, root: Path | None = None) -> ReclaimResult:
    root = root or kernel_root()
    pages = iter_pages(root)
    victims = sorted(
        [p for p in pages if p.resident and not (p.metadata or {}).get("pin")],
        key=lambda p: (p.importance, p.last_accessed, -p.size_bytes),
    )
    freed = 0
    reclaimed: list[str] = []
    victim_ids: set[str] = set()
    for page in victims:
        if freed >= target_bytes:
            break
        freed += page.size_bytes
        reclaimed.append(page.page_id)
        victim_ids.add(page.page_id)
    rewritten = [ContextPage(**{**asdict(p), "resident": False}) if p.page_id in victim_ids else p for p in pages]
    _rewrite_pages(rewritten, root)
    remaining = sum(p.size_bytes for p in rewritten if p.resident)
    return ReclaimResult(target_bytes=target_bytes, freed_bytes=freed, reclaimed_pages=reclaimed, remaining_resident_bytes=remaining)


def _working_set_manifest_path(root: Path | None = None) -> Path:
    return (root or kernel_root()) / WORKING_SET_MANIFEST_FILE


def _text_preview(value: str, limit: int = 240) -> str:
    normalized = " ".join(value.split())
    return normalized[:limit]


def _entry_text(entry: dict[str, Any]) -> str:
    message = entry.get("message")
    if isinstance(message, dict):
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        parts.append(str(item.get("text", "")))
                    elif item.get("type") in {"tool_result", "tool_use"}:
                        parts.append(json.dumps(item, ensure_ascii=False)[:1000])
            return "\n".join(parts)
    if "lastPrompt" in entry:
        return str(entry.get("lastPrompt") or "")
    attachment = entry.get("attachment")
    if isinstance(attachment, dict):
        return str(attachment.get("content") or attachment.get("stdout") or attachment.get("stderr") or "")
    return ""


def _classify_transcript_entry(entry: dict[str, Any]) -> tuple[str, str, str, str, str]:
    entry_type = str(entry.get("type") or "unknown")
    if entry_type == "user" or "lastPrompt" in entry:
        return "main", "register", "user", "user intent", "user-intent"
    if entry_type == "assistant":
        return "main", "decision", "assistant", "assistant decision", "assistant-decision"
    attachment = entry.get("attachment")
    if isinstance(attachment, dict):
        hook_name = str(attachment.get("hookName") or "")
        if hook_name.startswith("PostToolUse"):
            return "tools", "hook_payload", "hook", hook_name or "post tool hook", hook_name or "posttool"
        if "pytest" in json.dumps(attachment, ensure_ascii=False).lower():
            return "evidence", "evidence", "hook", "test evidence", "test-evidence"
        return "governance", "evidence", "hook", hook_name or "hook attachment", hook_name or "hook"
    return "evidence", "unknown", "unknown", entry_type, entry_type


def extract_transcript_pages(
    transcript_path: Path,
    *,
    root: Path | None = None,
    min_offload_chars: int = 4000,
    max_lines: int | None = None,
) -> TranscriptExtractionResult:
    root = root or kernel_root()
    created = 0
    offloaded = 0
    try:
        raw_lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        manifest = build_working_set_manifest(root=root)
        return TranscriptExtractionResult(str(transcript_path), 0, 0, manifest)
    base_index = max(0, len(raw_lines) - max_lines) if max_lines is not None else 0
    lines = raw_lines[base_index:] if max_lines is not None else raw_lines
    existing_ids = {page.page_id for page in iter_pages(root)}
    for relative_index, line in enumerate(lines):
        index = base_index + relative_index
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        text = _entry_text(entry)
        if not text:
            continue
        thread, page_type, source, summary_prefix, semantic_prefix = _classify_transcript_entry(entry)
        summary = f"{summary_prefix}: {_text_preview(text)}"
        stable_key = f"{semantic_prefix}:{index}:{_hash_text(line)}"
        stable_id = _stable_page_id(source, stable_key, str(transcript_path))
        if stable_id in existing_ids:
            continue
        metadata = {"transcript": str(transcript_path), "line_index": index, "entry_type": entry.get("type", ""), "stable_key": stable_key}
        if len(text) >= min_offload_chars or page_type in {"hook_payload", "evidence"}:
            page = swap_out_text(
                source if source in {"Read", "Grep", "Bash", "Agent", "memory_os", "hook", "user", "assistant", "unknown"} else "unknown",  # type: ignore[arg-type]
                text,
                cgroup="transcript",
                summary=summary,
                metadata=metadata,
                root=root,
                importance=0.9 if thread == "main" else 0.4,
                thread=thread,
                page_type=page_type,
                semantic_key=stable_key,
                hotness=0.9 if thread == "main" else 0.2,
            )
            offloaded += page.size_bytes
            created += 1
        elif page_type in {"register", "decision"}:
            record_page(
                source if source in {"Read", "Grep", "Bash", "Agent", "memory_os", "hook", "user", "assistant", "unknown"} else "unknown",  # type: ignore[arg-type]
                cgroup="transcript",
                size_bytes=len(text.encode("utf-8", errors="replace")),
                summary=summary,
                resident=True,
                evidence_uri=str(transcript_path),
                metadata=metadata,
                root=root,
                importance=0.9,
                thread=thread,
                page_type=page_type,
                semantic_key=stable_key,
                hotness=0.8,
            )
            created += 1
    manifest = build_working_set_manifest(root=root)
    return TranscriptExtractionResult(str(transcript_path), created, offloaded, manifest)


def build_working_set_manifest(
    *,
    root: Path | None = None,
    prompt: str = "",
    budgets: dict[str, int] | None = None,
    max_hot_pages: int = 32,
) -> WorkingSetManifest:
    root = root or kernel_root()
    budgets = budgets or DEFAULT_THREAD_BUDGETS
    pages = iter_pages(root)
    usage = {thread: 0 for thread in budgets}
    hot: list[ContextPage] = []
    cold: list[ContextPage] = []
    ranked = sorted(pages, key=lambda p: (p.thread == "main", p.dirty, p.hotness, p.importance, p.last_accessed), reverse=True)
    for page in ranked:
        thread = page.thread if page.thread in budgets else "tools"
        budget = budgets.get(thread, 0)
        page_cost = min(max(page.size_bytes, len(page.summary)), 4096)
        if len(hot) < max_hot_pages and usage.get(thread, 0) + page_cost <= budget and (page.resident or page.thread == "main" or page.hotness >= 0.5):
            usage[thread] = usage.get(thread, 0) + page_cost
            hot.append(page)
        else:
            cold.append(page)
    registers = {
        "prompt_preview": _text_preview(prompt),
        "page_count": len(pages),
        "hot_count": len(hot),
        "cold_count": len(cold),
    }
    pressure = "low"
    if any(usage.get(thread, 0) >= int(budget * 0.95) for thread, budget in budgets.items() if budget > 0):
        pressure = "high"
    manifest = WorkingSetManifest(
        manifest_id=f"wsm-{int(_now())}",
        created_at=_now(),
        registers=registers,
        hot_pages=[
            {
                "page_id": p.page_id,
                "thread": p.thread,
                "type": p.page_type,
                "semantic_key": p.semantic_key,
                "summary": p.summary,
                "evidence_uri": p.evidence_uri,
                "dependencies": p.dependencies or [],
            }
            for p in hot
        ],
        cold_refs=[
            {
                "page_id": p.page_id,
                "thread": p.thread,
                "type": p.page_type,
                "semantic_key": p.semantic_key,
                "summary": p.summary,
                "evidence_uri": p.evidence_uri,
            }
            for p in cold[:128]
        ],
        budgets=budgets,
        usage=usage,
        pressure=pressure,
    )
    root.mkdir(parents=True, exist_ok=True)
    _working_set_manifest_path(root).write_text(json.dumps(asdict(manifest), ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def manifest_context(manifest: WorkingSetManifest, *, max_chars: int = 6000) -> str:
    lines = [
        "[context_kernel:working_set_manifest]",
        f"manifest={manifest.manifest_id} pressure={manifest.pressure} hot={len(manifest.hot_pages)} cold={len(manifest.cold_refs)}",
    ]
    for page in manifest.hot_pages:
        lines.append(f"- [{page.get('thread')}/{page.get('type')}] {page.get('semantic_key')}: {page.get('summary')} (ref={page.get('page_id')})")
    if manifest.cold_refs:
        lines.append(f"cold_refs={len(manifest.cold_refs)} available via page_fault(page_id, range); raw cold pages are not resident.")
    text = "\n".join(lines)
    return text[:max_chars]


def build_recovery_context(account: ContextAccount, reclaim_result: ReclaimResult | None = None) -> str:
    lines = [
        "[context_kernel:OOM] Context pressure is critical; normal context growth is unsafe.",
        f"projected={account.projected_bytes} prompt={account.prompt_bytes} transcript={account.transcript_bytes} static={account.static_bytes} resident_pages={account.page_resident_bytes}",
    ]
    if reclaim_result:
        lines.append(f"reclaim freed={reclaim_result.freed_bytes} pages={','.join(reclaim_result.reclaimed_pages) or 'none'}")
    lines.append("Allowed recovery actions: /compact, targeted Read(offset+limit), Grep with bounded head_limit, or page_fault(page_id, range).")
    return "\n".join(lines)


def oom_check(
    *,
    prompt: str = "",
    transcript_path: Path | None = None,
    static_bytes: int = 180_000,
    root: Path | None = None,
    auto_reclaim: bool = True,
) -> OomDecision:
    root = root or kernel_root()
    account = account_context(prompt=prompt, transcript_path=transcript_path, static_bytes=static_bytes, root=root)
    if account.pressure != "critical":
        return OomDecision("allow", account.pressure, "within context budget", account)
    reclaim_result = None
    if auto_reclaim and account.page_resident_bytes > 0:
        hard = int(os.environ.get("CONTEXT_KERNEL_HARD_BYTES", str(900_000)))
        target = max(account.projected_bytes - hard, 0)
        reclaim_result = reclaim(target, root=root)
        account = account_context(prompt=prompt, transcript_path=transcript_path, static_bytes=static_bytes, root=root)
        if account.pressure != "critical":
            return OomDecision("allow_after_reclaim", account.pressure, "reclaimed resident context pages", account, reclaim_result)
    return OomDecision("recovery_only", account.pressure, "context remains over hard budget", account, reclaim_result, build_recovery_context(account, reclaim_result))
