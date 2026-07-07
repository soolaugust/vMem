# Context Kernel Plan: Multi-Threaded vmem for LLM Context

Date: 2026-07-05
Status: implementation in progress

## Problem

A Claude Code session transcript is currently an append-only linear address space. It mixes user intent, assistant decisions, tool payloads, hook stdout, subagent output, test logs, and memory injection. When any producer emits large low-value text, request assembly can exceed the model context window and block the user with API 400. Manual `/compact` is an emergency operator action, not a memory-management strategy.

## First principles

Only the current working set must be resident in the model context. Everything else should be addressable, summarized, and demand-paged.

The context window should be treated like RAM:

- Current task registers are always hot.
- Evidence and tool logs are page cache, not stack.
- Subagent results are join handles, not inline transcripts.
- Hook payloads are DMA buffers and must not become resident by default.
- Long-term memory is disk-backed VFS, demand-paged through relevance scoring.

## Design

### Context Kernel

The Context Kernel owns deterministic policy and accounting:

- page table: semantic pages with summaries and evidence pointers
- thread registry: main/evidence/tools/memory/agents/governance lanes
- scheduler: selects hot pages per turn
- reclaimer/OOM killer: shrinks low-value pages before API submission
- page fault API: loads exact evidence ranges on demand

Hooks are syscall adapters. They should not own policy.

### Context Threads

Each semantic lane has a budget and loading policy:

| Thread | Purpose | Default policy |
| --- | --- | --- |
| main | current goal, next step, unresolved blockers | always hot |
| code | active files, patches, diffs | hot while editing |
| evidence | tests, logs, trace output | summary only |
| tools | raw tool/hook payloads | metadata only |
| memory | durable knowledge | top-k with pressure shedding |
| agents | subagent results | join handles only |
| governance | rules, budget, risk gates | hot when active |

### Semantic Page Table

A page is not a chunk of arbitrary text; it is a semantic unit:

```json
{
  "page_id": "ctx-...",
  "thread": "evidence",
  "type": "tool_result",
  "semantic_key": "pytest prompt_budget_guard 2026-07-05",
  "summary": "prompt_budget_guard regression passed",
  "evidence_uri": "/tmp/.../pytest.output",
  "tokens_estimate": 120,
  "importance": 0.82,
  "hotness": 0.5,
  "dirty": false,
  "dependencies": ["claim:context-oom-root-cause"]
}
```

### Claim/evidence separation

Compaction must preserve claims, evidence, and dependency edges rather than chat chronology.

A root-cause claim should survive even if raw logs are evicted:

```json
{
  "claim_id": "context-oom-root-cause-20260705",
  "claim": "API 400 was caused by PostToolUse hooks echoing large Edit payloads into transcript",
  "status": "active",
  "evidence_refs": ["rss_snapshot", "transcript_stats", "largest_attachment"],
  "fix_refs": ["posttool_observers.js", "prompt_budget_guard.py"],
  "verification_refs": ["prompt_budget_guard tests"]
}
```

### Happens-before DAG

Context recovery should restore causality:

```text
400 observed
  -> trace_id recorded
  -> transcript RSS measured
  -> PostToolUse attachment bloat identified
  -> observer input truncation applied
  -> prompt-time transcript reclaim applied
  -> tests passed
```

### OOM policy

When projected context exceeds hard budget, reclaim order is:

1. hook raw payloads
2. tool stdout/stderr already persisted elsewhere
3. passed test logs
4. stale subagent transcripts
5. stale retrieved memories with low apply signal
6. superseded decisions

Never reclaim by default:

- latest user correction
- current goal/next step
- unresolved blockers
- active safety gates
- active root-cause claims

## Implementation Plan

### Phase 1 — Page primitives

- Extend `memory_os.runtime.context_kernel` with semantic page fields: thread, type, semantic_key, hotness, dirty, dependencies.
- Keep backward compatibility with existing JSONL page table.
- Add working set manifest data model.

### Phase 2 — Transcript extractor

- Add deterministic extraction from Claude Code JSONL:
  - user intent pages
  - assistant decision pages
  - tool result pages
  - hook attachment pages
  - subagent/join pages
- Large payloads are offloaded to page files with summaries.

### Phase 3 — Scheduler

- Build `working_set_manifest.json` from page table and current prompt.
- Enforce per-thread budgets.
- Emit only registers + hot pages + cold refs.

### Phase 4 — UserPromptSubmit integration

- `prompt_budget_guard.py` calls Context Kernel before emergency notice.
- Hard pressure triggers automatic transcript reclaim and manifest rebuild.
- User input remains approved; no manual compact required.

### Phase 5 — Page fault API

- Add CLI/API to load exact evidence ranges by page id.
- Teach recovery notices to point to page ids instead of raw transcript.

### Phase 6 — Observability

- Production assertions:
  - prompt-time reclaim runs when transcript exceeds hard budget
  - working set manifest exists and is fresh
  - raw hook payload resident bytes trend down
  - 400 OOM events decrease after reclaim

## Success Criteria

- User prompt is never blocked only because transcript has large hook/tool payloads.
- New PostToolUse payloads are bounded before observer fanout.
- Existing oversized transcript is automatically reclaimed on next prompt.
- Working set manifest is generated under pressure.
- Claims and evidence refs survive reclaim.
- Tests cover page table, transcript extraction, manifest scheduling, and prompt guard integration.
