"""
test_privacy_filter.py — privacy_filter 单元测试

验证 seccomp BPF 内容过滤器的 9 种 secret pattern 检测 + 脱敏。
"""
import time
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from memory_os.core.privacy_filter import scrub_secrets


class TestPatternDetection:
    """每种 secret pattern 独立检测。"""

    def test_aws_access_key(self):
        text = "key is AKIAIOSFODNN7EXAMPLE"
        result, log = scrub_secrets(text)
        assert "[REDACTED:aws_access_key]" in result
        assert any(r["type"] == "aws_access_key" for r in log)

    def test_aws_secret_key(self):
        text = "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY1"
        result, log = scrub_secrets(text)
        assert "[REDACTED:aws_secret_key]" in result

    def test_github_pat(self):
        text = "token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmn"
        result, log = scrub_secrets(text)
        assert "[REDACTED:github_token]" in result

    def test_github_fine_grained(self):
        text = "github_pat_11AAAAAAA0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        result, log = scrub_secrets(text)
        assert "[REDACTED:github_token]" in result

    def test_anthropic_key(self):
        text = "ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnopqrst"
        result, log = scrub_secrets(text)
        assert "[REDACTED:anthropic_key]" in result

    def test_openai_key(self):
        text = "openai_key = sk-proj-abcdefghijklmnopqrstuvwxyz1234"
        result, log = scrub_secrets(text)
        assert "[REDACTED:openai_key]" in result

    def test_bearer_token(self):
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abcdef"
        result, log = scrub_secrets(text)
        assert "[REDACTED:bearer_token]" in result

    def test_pem_private_key(self):
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn\n-----END RSA PRIVATE KEY-----"
        result, log = scrub_secrets(text)
        assert "[REDACTED:private_key]" in result
        assert "BEGIN" not in result

    def test_url_basic_auth(self):
        text = "connect to https://admin:supersecret123@db.example.com/prod"
        result, log = scrub_secrets(text)
        assert "[REDACTED:url_basic_auth]" in result
        assert "supersecret123" not in result

    def test_generic_api_key(self):
        text = 'api_key = "abcdefghijklmnopqrstuvwxyz1234"'
        result, log = scrub_secrets(text)
        assert "[REDACTED:generic_api_key]" in result


class TestEdgeCases:
    """边界情况。"""

    def test_empty_text(self):
        result, log = scrub_secrets("")
        assert result == ""
        assert log == []

    def test_none_text(self):
        result, log = scrub_secrets(None)
        assert result is None
        assert log == []

    def test_no_secrets(self):
        text = "这是一段普通的中文技术讨论，不包含任何密钥信息。BM25 检索效果很好。"
        result, log = scrub_secrets(text)
        assert result == text
        assert log == []

    def test_multiple_secrets(self):
        text = "aws: AKIAIOSFODNN7EXAMPLE, github: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmn"
        result, log = scrub_secrets(text)
        assert "[REDACTED:aws_access_key]" in result
        assert "[REDACTED:github_token]" in result
        assert len(log) == 2

    def test_redaction_log_structure(self):
        text = "key: AKIAIOSFODNN7EXAMPLE"
        _, log = scrub_secrets(text)
        assert len(log) == 1
        assert "type" in log[0]
        assert "offset" in log[0]
        assert "length" in log[0]


class TestPerformance:
    """性能基线：<1ms for <2000 chars。"""

    def test_fast_path_no_secrets(self):
        text = "普通文本" * 500  # 2000 chars
        start = time.perf_counter()
        for _ in range(1000):
            scrub_secrets(text)
        elapsed_ms = (time.perf_counter() - start) / 1000 * 1000
        assert elapsed_ms < 1.0, f"fast path took {elapsed_ms:.3f}ms (expect <1ms)"

    def test_with_secrets_performance(self):
        text = f"prefix AKIAIOSFODNN7EXAMPLE middle ghp_{'A' * 40} suffix" * 10
        start = time.perf_counter()
        for _ in range(100):
            scrub_secrets(text)
        elapsed_ms = (time.perf_counter() - start) / 100 * 1000
        assert elapsed_ms < 5.0, f"scrub took {elapsed_ms:.3f}ms (expect <5ms)"
