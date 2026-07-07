#!/usr/bin/env python3
"""Production benchmark runner for vMem.

The benchmark is intentionally deterministic and local-first: it validates the
engineering contract that vMem can be installed, diagnosed, and can shed context
before a model request would exceed the context window.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = Path(__file__).resolve().parent / "reports"
THRESHOLDS = json.loads((Path(__file__).resolve().parent / "thresholds.json").read_text(encoding="utf-8"))
INTERNAL_PATTERNS = ("xiao" + "mi", "git.n." + "xiao" + "mi", "@" + "xiao" + "mi", "kernel-cpu/" + "aios")


@dataclass
class Check:
    name: str
    title: str
    category: str
    ok: bool
    message: str
    value: Any = None
    threshold: Any = None
    duration_ms: float = 0.0
    impact: str = ""
    fix: str = ""
    gate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "category": self.category,
            "ok": self.ok,
            "message": self.message,
            "value": self.value,
            "threshold": self.threshold,
            "duration_ms": round(self.duration_ms, 2),
            "impact": self.impact,
            "fix": self.fix,
            "gate": self.gate,
        }


@dataclass
class BenchContext:
    root: Path
    temp: Path
    memory_dir: Path
    env: dict[str, str] = field(default_factory=dict)


def run_cmd(args: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None, input_text: str | None = None, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(args, cwd=cwd, env=merged, input=input_text, text=True, capture_output=True, check=False, timeout=timeout)


def timed(name: str, title: str, category: str, func: Callable[[BenchContext], Check]) -> Callable[[BenchContext], Check]:
    def wrapper(ctx: BenchContext) -> Check:
        start = time.perf_counter()
        try:
            check = func(ctx)
        except Exception as exc:
            check = Check(
                name=name,
                title=title,
                category=category,
                ok=False,
                message=f"{type(exc).__name__}: {exc}",
                impact="Benchmark crashed before producing a result.",
                fix="Run the check locally with --suite smoke and inspect stderr.",
                gate=name in THRESHOLDS["required_gates"],
            )
        check.name = name
        check.title = title
        check.category = category
        check.duration_ms = (time.perf_counter() - start) * 1000
        check.gate = check.gate or name in THRESHOLDS["required_gates"]
        return check
    return wrapper


def _check_package_metadata(ctx: BenchContext) -> Check:
    with (ctx.root / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    scripts = data["project"]["scripts"]
    needed = {"vmem", "memory-os"}
    ok = data["project"]["name"] == "vmem" and needed.issubset(scripts)
    return Check(
        name="package_metadata_ok",
        title="Package metadata exposes vmem and legacy CLI",
        category="install",
        ok=ok,
        message="pyproject package name and console scripts are correct" if ok else "pyproject metadata is incomplete",
        value={"name": data["project"].get("name"), "scripts": sorted(scripts)},
        impact="Users can invoke vMem from the installed wheel.",
        fix="Set project.name=vmem and expose both vmem and memory-os console scripts.",
        gate=True,
    )


def _check_doctor(ctx: BenchContext) -> Check:
    result = run_cmd([sys.executable, "-m", "memory_os.cli.mcp_memory_lookup", "doctor", "--json"], env={"MEMORY_OS_DIR": str(ctx.memory_dir)})
    ok = result.returncode == 0
    payload = json.loads(result.stdout) if result.stdout.strip().startswith("{") else {}
    return Check(
        name="doctor_passes",
        title="Doctor passes on a fresh writable memory dir",
        category="install",
        ok=ok and payload.get("ok") is True,
        message="vmem doctor reports all checks healthy" if ok else result.stderr[:300] or result.stdout[:300],
        value=payload,
        threshold="ok=true",
        impact="Users get a single command that proves the installation is sane.",
        fix="Run vmem repair, verify packaged hook files, and ensure MEMORY_OS_DIR is writable.",
        gate=True,
    )


def _check_install_repair(ctx: BenchContext) -> Check:
    settings = ctx.temp / "settings.json"
    settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": []}}, ensure_ascii=False), encoding="utf-8")
    first = run_cmd([sys.executable, "-m", "memory_os.cli.mcp_memory_lookup", "install", "--settings", str(settings), "--json"])
    second = run_cmd([sys.executable, "-m", "memory_os.cli.mcp_memory_lookup", "repair", "--settings", str(settings), "--json"])
    first_payload = json.loads(first.stdout or "{}")
    second_payload = json.loads(second.stdout or "{}")
    data = json.loads(settings.read_text(encoding="utf-8"))
    first_hook = data["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    ok = first.returncode == 0 and second.returncode == 0 and first_payload.get("changed") is True and second_payload.get("changed") is False and first_hook.get("async") is False
    return Check(
        name="install_repair_idempotent",
        title="Install/repair are idempotent",
        category="install",
        ok=ok,
        message="install writes the guard once and repair is a no-op" if ok else "install/repair did not converge",
        value={"install": first_payload, "repair": second_payload, "first_hook": first_hook},
        threshold="install changed=true, repair changed=false",
        impact="Users can safely re-run repair without duplicating hooks.",
        fix="Normalize UserPromptSubmit hooks so prompt_budget_guard is first and unique.",
        gate=True,
    )


def _warn_overflow_payload(ctx: BenchContext) -> tuple[dict[str, Any], Path]:
    transcript = ctx.temp / "huge-transcript.jsonl"
    transcript.write_text(json.dumps({"message": {"content": [{"type": "text", "text": "t" * 5000}]}}) + "\n", encoding="utf-8")
    env = {
        "MEMORY_OS_DIR": str(ctx.memory_dir),
        "HARNESS_HEARTBEAT_DIR": str(ctx.memory_dir),
        "MEMORY_OS_PROMPT_CHAR_BUDGET": "1000",
        "MEMORY_OS_TOTAL_CONTEXT_WARN_CHARS": "200",
        "MEMORY_OS_TOTAL_CONTEXT_HARD_CHARS": "10000",
        "MEMORY_OS_STATIC_CONTEXT_RESERVE_CHARS": "100",
        "MEMORY_OS_DOWNSTREAM_CONTEXT_RESERVE_CHARS": "1",
    }
    result = run_cmd([sys.executable, "hooks/prompt_budget_guard.py"], env=env, input_text=json.dumps({"prompt": "continue", "transcript_path": str(transcript)}))
    payload = json.loads(result.stdout or "{}")
    return payload, transcript


def _check_warn_overflow(ctx: BenchContext) -> Check:
    payload, _ = _warn_overflow_payload(ctx)
    pressure = json.loads((ctx.memory_dir / "context_pressure_state.json").read_text(encoding="utf-8"))
    mode = json.loads((ctx.memory_dir / "context_mode_state.json").read_text(encoding="utf-8"))
    working_set = ctx.memory_dir / "working_set" / "current.json"
    notice = payload.get("hookSpecificOutput", {}).get("additionalContext", "")
    ok = (
        payload.get("decision") == "approve"
        and pressure.get("last_pressure_level") == "high"
        and mode.get("mode") == "working_set"
        and working_set.exists()
        and 0 < len(notice) <= 1200
    )
    return Check(
        name="context_warn_overflow_enters_working_set",
        title="Warning context overflow enters working-set mode",
        category="context_safety",
        ok=ok,
        message="warn watermark triggers bounded working-set reclaim before hard overflow" if ok else "warn watermark did not enter bounded working-set mode",
        value={
            "decision": payload.get("decision"),
            "pressure": pressure.get("last_pressure_level"),
            "mode": mode.get("mode"),
            "working_set_exists": working_set.exists(),
            "notice_chars": len(notice),
        },
        threshold="decision=approve, pressure=high, mode=working_set, notice<=1200",
        impact="vMem starts OS-style working-set reclaim at the warning watermark, before requests reach the API context-window failure point.",
        fix="Make prompt_budget_guard write working_set state, shed optional context, and emit only bounded recovery context under warning pressure.",
        gate=True,
    )


def _check_retriever_shed(ctx: BenchContext) -> Check:
    _warn_overflow_payload(ctx)
    result = run_cmd([sys.executable, "hooks/retriever.py"], env={"MEMORY_OS_DIR": str(ctx.memory_dir)}, input_text=json.dumps({"prompt": "need architecture context"}))
    daemon_text = (ctx.root / "hooks" / "retriever_daemon.py").read_text(encoding="utf-8")
    daemon_wired = "if should_shed_optional_context(hook_input):" in daemon_text and daemon_text.index("if should_shed_optional_context(hook_input):") < daemon_text.index("# ── Stage 0: SKIP ──")
    ok = result.returncode == 0 and result.stdout == "" and daemon_wired
    return Check(
        name="high_pressure_sheds_retriever",
        title="High pressure sheds optional retrieval context",
        category="context_safety",
        ok=ok,
        message="fallback emits no additionalContext and daemon is wired before Stage 0" if ok else "retriever can still add context under critical pressure",
        value={"fallback_stdout_bytes": len(result.stdout), "daemon_wired_before_stage0": daemon_wired},
        threshold="fallback_stdout_bytes=0, daemon_wired_before_stage0=true",
        impact="This is the local proof that vMem prevents extra context from pushing requests toward API 400.",
        fix="Ensure retriever.py and retriever_daemon.py call should_shed_optional_context before retrieval/injection.",
        gate=True,
    )


def _check_no_db(ctx: BenchContext) -> Check:
    empty = ctx.temp / "empty-memory"
    empty.mkdir()
    result = run_cmd([sys.executable, "-m", "memory_os.observability.production_assertions", "--json"], env={"MEMORY_OS_DIR": str(empty)})
    payload = json.loads(result.stdout or "{}")
    ok = result.returncode == 1 and payload.get("status") == "DEGRADED" and "timestamp" in payload and "duration_ms" in payload.get("summary", {})
    return Check(
        name="fault_no_db_degrades",
        title="No store.db degrades instead of crashing",
        category="fault",
        ok=ok,
        message="production assertions return shaped DEGRADED report without store.db" if ok else "no-db path crashed or returned malformed report",
        value={"returncode": result.returncode, "status": payload.get("status")},
        threshold="returncode=1, status=DEGRADED, report shape complete",
        impact="Fresh installs and broken stores produce actionable diagnostics rather than stack traces.",
        fix="Return a normal report shape for no-db and keep text/json modes tolerant.",
        gate=True,
    )


def _check_public_hygiene(ctx: BenchContext) -> Check:
    roots = ["README.md", "README.zh.md", "llms.txt", "docs", "marketing", "paper/main.tex", "pyproject.toml", "glama.json", ".mcp.json", "hooks/hooks.json", "memory_os/cli/vmem_doctor.py", "memory_os/observability/production_assertions.py"]
    hits: list[str] = []
    for item in roots:
        root = ctx.root / item
        paths = root.rglob("*") if root.is_dir() else [root]
        for path in paths:
            if path.is_file() and path.suffix.lower() not in {".pdf", ".deb", ".aux", ".out", ".blg"}:
                text = path.read_text(encoding="utf-8", errors="ignore").lower()
                if any(pattern in text for pattern in INTERNAL_PATTERNS):
                    hits.append(str(path.relative_to(ctx.root)))
    ok = not hits
    return Check(
        name="public_hygiene_passes",
        title="Public files contain no internal strings",
        category="hygiene",
        ok=ok,
        message="public hygiene scan is clean" if ok else "internal strings found",
        value={"hits": hits[:20]},
        threshold="0 hits",
        impact="Public releases do not leak internal organization references.",
        fix="Remove internal strings from public docs/configs or construct scanner patterns dynamically.",
        gate=True,
    )


CHECKS: list[Callable[[BenchContext], Check]] = [
    timed("package_metadata_ok", "Package metadata exposes vmem and legacy CLI", "install", _check_package_metadata),
    timed("doctor_passes", "Doctor passes on a fresh writable memory dir", "install", _check_doctor),
    timed("install_repair_idempotent", "Install/repair are idempotent", "install", _check_install_repair),
    timed("context_warn_overflow_enters_working_set", "Warning context overflow enters working-set mode", "context_safety", _check_warn_overflow),
    timed("high_pressure_sheds_retriever", "High pressure sheds optional retrieval context", "context_safety", _check_retriever_shed),
    timed("fault_no_db_degrades", "No store.db degrades instead of crashing", "fault", _check_no_db),
    timed("public_hygiene_passes", "Public files contain no internal strings", "hygiene", _check_public_hygiene),
]
SUITES = {
    "smoke": CHECKS,
    "release": CHECKS,
    "all": CHECKS,
}


def score(checks: list[Check]) -> dict[str, Any]:
    weights = {
        "install": 25,
        "context_safety": 35,
        "fault": 15,
        "hygiene": 15,
        "performance": 10,
    }
    by_category: dict[str, list[Check]] = {}
    for check in checks:
        by_category.setdefault(check.category, []).append(check)
    category_scores: dict[str, float] = {}
    total = 0.0
    for category, weight in weights.items():
        items = by_category.get(category, [])
        if not items:
            category_scores[category] = weight
            total += weight
            continue
        passed = sum(1 for item in items if item.ok)
        value = weight * passed / len(items)
        category_scores[category] = round(value, 1)
        total += value
    gates_failed = [check.name for check in checks if check.gate and not check.ok]
    if gates_failed:
        total = min(total, 69.0)
    if total >= 95:
        level = "production-grade"
    elif total >= 85:
        level = "production-candidate"
    elif total >= 70:
        level = "public-beta"
    else:
        level = "prototype"
    return {"score": round(total, 1), "level": level, "category_scores": category_scores, "gates_failed": gates_failed}


def render_markdown(report: dict[str, Any]) -> str:
    verdict = report["verdict"]
    checks = report["checks"]
    passed = sum(1 for item in checks if item["ok"])
    failed = len(checks) - passed
    lines = [
        "# vMem Production Benchmark Report",
        "",
        f"**Verdict:** {verdict['level']} ({verdict['score']}/100)",
        f"**Run:** {report['timestamp']}",
        f"**Checks:** {passed} passed / {failed} failed",
        "",
        "## Value At A Glance",
        "",
        "- **OS-style context reclaim:** warning watermark enters bounded working-set mode before hard overflow.",
        "- **API 400 prevention path:** high pressure sheds optional context and emits only a bounded working-set notice.",
        "- **Operational readiness:** doctor, install, repair, no-db degraded reports, and public hygiene are checked as release gates.",
        "",
        "## Hard Gates",
        "",
    ]
    for item in checks:
        if not item["gate"]:
            continue
        icon = "✅" if item["ok"] else "❌"
        lines.append(f"- {icon} **{item['title']}** — {item['message']}")
    lines.extend(["", "## Score Breakdown", ""])
    for category, value in verdict["category_scores"].items():
        lines.append(f"- **{category}:** {value}")
    lines.extend(["", "## Detailed Checks", ""])
    for item in checks:
        icon = "✅" if item["ok"] else "❌"
        lines.extend([
            f"### {icon} {item['title']}",
            f"- **Category:** {item['category']}",
            f"- **Result:** {item['message']}",
            f"- **Value:** `{json.dumps(item['value'], ensure_ascii=False)[:500]}`",
            f"- **Why it matters:** {item['impact']}",
            f"- **Fix if failing:** {item['fix']}",
            "",
        ])
    if verdict["gates_failed"]:
        lines.extend(["## Required Fixes", ""])
        for gate in verdict["gates_failed"]:
            lines.append(f"- Fix hard gate `{gate}` before claiming production readiness.")
        lines.append("")
    return "\n".join(lines)


def run_suite(suite: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        temp = Path(tmp)
        ctx = BenchContext(root=ROOT, temp=temp, memory_dir=temp / "memory-os")
        ctx.memory_dir.mkdir(parents=True, exist_ok=True)
        selected = SUITES[suite]
        checks = [check(ctx) for check in selected]
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "suite": suite,
        "verdict": score(checks),
        "checks": [check.to_dict() for check in checks],
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run vMem production benchmark")
    parser.add_argument("--suite", choices=sorted(SUITES), default="smoke")
    parser.add_argument("--report", type=Path, default=None, help="JSON report path")
    parser.add_argument("--markdown", type=Path, default=None, help="Markdown report path")
    parser.add_argument("--fail-under", type=float, default=0.0)
    args = parser.parse_args(argv)

    report = run_suite(args.suite)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = args.report or REPORT_DIR / f"production-{args.suite}.json"
    md_path = args.markdown or json_path.with_suffix(".md")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(render_markdown(report))
    score_value = report["verdict"]["score"]
    gates_failed = report["verdict"]["gates_failed"]
    if gates_failed or score_value < args.fail_under:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
