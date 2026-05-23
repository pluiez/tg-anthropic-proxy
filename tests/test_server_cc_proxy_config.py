from server.config import cc_proxy_base_url, configured_anthropic_base_url


def test_configured_anthropic_base_url_defaults_to_anthropic(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)

    assert configured_anthropic_base_url() == "https://api.anthropic.com"


def test_configured_anthropic_base_url_uses_real_upstream(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://idealab.alibaba-inc.com/api/ ")

    assert configured_anthropic_base_url() == "https://idealab.alibaba-inc.com/api"


def test_cc_proxy_base_url_uses_host_and_port(monkeypatch) -> None:
    monkeypatch.setenv("CC_PROXY_HOST", "127.0.0.1")
    monkeypatch.setenv("CC_PROXY_PORT", "8790")

    assert cc_proxy_base_url() == "http://127.0.0.1:8790"


def test_cc_proxy_base_url_allows_scheme_in_host(monkeypatch) -> None:
    monkeypatch.setenv("CC_PROXY_HOST", "http://proxy.local")
    monkeypatch.setenv("CC_PROXY_PORT", "8080")

    assert cc_proxy_base_url() == "http://proxy.local:8080"
