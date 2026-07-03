from pathlib import Path


def test_decision_else_branch_keeps_quality_gate() -> None:
    source = (Path(__file__).resolve().parents[2] / "hooks" / "extractor.py").read_text(encoding="utf-8")
    marker = "# iterNNNN: decision 统一 SNR gate"
    assert marker in source
    branch = source[source.index(marker):source.index("        for summary in excluded:", source.index(marker))]

    assert "if not _is_quality_decision(summary):" in branch
    assert "snr_filter: decision dropped" in branch
    assert branch.index("if not _is_quality_decision(summary):") < branch.index("_write_chunk(\"decision\"")
