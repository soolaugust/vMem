from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from memory_os.runtime.context.kernel_compat import (
    ContextPage,
    account_context,
    iter_pages,
    oom_check,
    page_fault,
    reclaim,
    swap_out_text,
    update_cgroup_usage,
    _rewrite_pages,
)


def test_pager_swap_out_and_page_fault(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_KERNEL_DIR", str(tmp_path / "kernel"))
    page = swap_out_text("Bash", "a\nb\nc\nd", cgroup="tool_output", summary="log output")

    assert page.resident is False
    assert Path(page.evidence_uri).exists()
    assert page_fault(page.page_id, offset=1, limit=2) == "b\nc"
    updated = iter_pages(tmp_path / "kernel")[0]
    assert updated.access_count == 1


def test_accountant_counts_transcript_and_cgroups(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "kernel"
    monkeypatch.setenv("CONTEXT_KERNEL_DIR", str(root))
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("x" * 100, encoding="utf-8")
    swap_out_text("Read", "y" * 200, cgroup="tool_output", summary="read page")

    account = account_context(prompt="hello", transcript_path=transcript, static_bytes=10)

    assert account.prompt_bytes == 5
    assert account.transcript_bytes == 100
    assert account.static_bytes == 10
    assert account.page_swapped_bytes == 200
    assert account.by_cgroup["tool_output"]["swapped_bytes"] == 200


def test_reclaimer_swaps_resident_pages_by_importance(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "kernel"
    monkeypatch.setenv("CONTEXT_KERNEL_DIR", str(root))
    low = swap_out_text("Agent", "low" * 100, cgroup="subagent", summary="low", importance=0.1)
    high = swap_out_text("Agent", "high" * 100, cgroup="subagent", summary="high", importance=0.9)
    pages = [ContextPage(**{**asdict(p), "resident": True}) for p in iter_pages(root)]
    _rewrite_pages(pages, root)

    result = reclaim(200, root=root)

    assert low.page_id in result.reclaimed_pages
    assert high.page_id not in result.reclaimed_pages
    usage = update_cgroup_usage(root)
    assert usage["subagent"].resident_bytes == high.size_bytes


def test_oom_policy_recovers_after_reclaim(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "kernel"
    monkeypatch.setenv("CONTEXT_KERNEL_DIR", str(root))
    monkeypatch.setenv("CONTEXT_KERNEL_HARD_BYTES", "500")
    monkeypatch.setenv("CONTEXT_KERNEL_WARN_BYTES", "300")
    page = swap_out_text("Agent", "x" * 600, cgroup="subagent", summary="resident", importance=0.1)
    _rewrite_pages([ContextPage(**{**asdict(page), "resident": True})], root)

    decision = oom_check(prompt="small", static_bytes=0, root=root)

    assert decision.decision == "allow_after_reclaim"
    assert decision.reclaim is not None
    assert decision.reclaim.freed_bytes >= 500


def test_oom_policy_returns_recovery_only_when_transcript_too_large(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "kernel"
    monkeypatch.setenv("CONTEXT_KERNEL_DIR", str(root))
    monkeypatch.setenv("CONTEXT_KERNEL_HARD_BYTES", "500")
    monkeypatch.setenv("CONTEXT_KERNEL_WARN_BYTES", "300")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("z" * 1000, encoding="utf-8")

    decision = oom_check(prompt="small", transcript_path=transcript, static_bytes=0, root=root)

    assert decision.decision == "recovery_only"
    assert "Allowed recovery actions" in decision.recovery_context
