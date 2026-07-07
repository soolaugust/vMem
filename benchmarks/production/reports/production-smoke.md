# vMem Production Benchmark Report

**Verdict:** production-grade (100.0/100)
**Run:** 2026-07-04T03:59:37.490062+00:00
**Checks:** 7 passed / 0 failed

## Value At A Glance

- **OS-style context reclaim:** warning watermark enters bounded working-set mode before hard overflow.
- **API 400 prevention path:** high pressure sheds optional context and emits only a bounded working-set notice.
- **Operational readiness:** doctor, install, repair, no-db degraded reports, and public hygiene are checked as release gates.

## Hard Gates

- ✅ **Package metadata exposes vmem and legacy CLI** — pyproject package name and console scripts are correct
- ✅ **Doctor passes on a fresh writable memory dir** — vmem doctor reports all checks healthy
- ✅ **Install/repair are idempotent** — install writes the guard once and repair is a no-op
- ✅ **Warning context overflow enters working-set mode** — warn watermark triggers bounded working-set reclaim before hard overflow
- ✅ **High pressure sheds optional retrieval context** — fallback emits no additionalContext and daemon is wired before Stage 0
- ✅ **No store.db degrades instead of crashing** — production assertions return shaped DEGRADED report without store.db
- ✅ **Public files contain no internal strings** — public hygiene scan is clean

## Score Breakdown

- **install:** 25.0
- **context_safety:** 35.0
- **fault:** 15.0
- **hygiene:** 15.0
- **performance:** 10

## Detailed Checks

### ✅ Package metadata exposes vmem and legacy CLI
- **Category:** install
- **Result:** pyproject package name and console scripts are correct
- **Value:** `{"name": "vmem", "scripts": ["memory-os", "vmem"]}`
- **Why it matters:** Users can invoke vMem from the installed wheel.
- **Fix if failing:** Set project.name=vmem and expose both vmem and memory-os console scripts.

### ✅ Doctor passes on a fresh writable memory dir
- **Category:** install
- **Result:** vmem doctor reports all checks healthy
- **Value:** `{"ok": true, "checks": [{"name": "required_files", "ok": true, "message": "required hook files present"}, {"name": "hooks", "ok": true, "message": "hook order ok: prompt_budget_guard before retriever"}, {"name": "retriever_pressure", "ok": true, "message": "retriever fallback and daemon consume pressure state"}, {"name": "memory_dir", "ok": true, "message": "memory dir writable: /tmp/claude-1000/claude-1000/tmp7j1yoz13/memory-os"}, {"name": "public_hygiene", "ok": true, "message": "no public int`
- **Why it matters:** Users get a single command that proves the installation is sane.
- **Fix if failing:** Run vmem repair, verify packaged hook files, and ensure MEMORY_OS_DIR is writable.

### ✅ Install/repair are idempotent
- **Category:** install
- **Result:** install writes the guard once and repair is a no-op
- **Value:** `{"install": {"ok": true, "changed": true, "settings_path": "/tmp/claude-1000/claude-1000/tmp7j1yoz13/settings.json", "guard_index": 0, "duplicates_removed": 0, "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/prompt_budget_guard.py\"", "action": "install"}, "repair": {"ok": true, "changed": false, "settings_path": "/tmp/claude-1000/claude-1000/tmp7j1yoz13/settings.json", "guard_index": 0, "duplicates_removed": 0, "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/prompt_budget_guard.py\"", "acti`
- **Why it matters:** Users can safely re-run repair without duplicating hooks.
- **Fix if failing:** Normalize UserPromptSubmit hooks so prompt_budget_guard is first and unique.

### ✅ Warning context overflow enters working-set mode
- **Category:** context_safety
- **Result:** warn watermark triggers bounded working-set reclaim before hard overflow
- **Value:** `{"decision": "approve", "pressure": "high", "mode": "working_set", "working_set_exists": true, "notice_chars": 239}`
- **Why it matters:** vMem starts OS-style working-set reclaim at the warning watermark, before requests reach the API context-window failure point.
- **Fix if failing:** Make prompt_budget_guard write working_set state, shed optional context, and emit only bounded recovery context under warning pressure.

### ✅ High pressure sheds optional retrieval context
- **Category:** context_safety
- **Result:** fallback emits no additionalContext and daemon is wired before Stage 0
- **Value:** `{"fallback_stdout_bytes": 0, "daemon_wired_before_stage0": true}`
- **Why it matters:** This is the local proof that vMem prevents extra context from pushing requests toward API 400.
- **Fix if failing:** Ensure retriever.py and retriever_daemon.py call should_shed_optional_context before retrieval/injection.

### ✅ No store.db degrades instead of crashing
- **Category:** fault
- **Result:** production assertions return shaped DEGRADED report without store.db
- **Value:** `{"returncode": 1, "status": "DEGRADED"}`
- **Why it matters:** Fresh installs and broken stores produce actionable diagnostics rather than stack traces.
- **Fix if failing:** Return a normal report shape for no-db and keep text/json modes tolerant.

### ✅ Public files contain no internal strings
- **Category:** hygiene
- **Result:** public hygiene scan is clean
- **Value:** `{"hits": []}`
- **Why it matters:** Public releases do not leak internal organization references.
- **Fix if failing:** Remove internal strings from public docs/configs or construct scanner patterns dynamically.
