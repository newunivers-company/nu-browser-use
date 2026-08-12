"""Regression tests for MCP and core Agent LLM policy alignment."""

from browser_use.llm.openai.chat import ChatOpenAI
from browser_use.llm.subscription_cli import ChatSubscriptionCLI, SubscriptionCLIProvider
from browser_use.mcp.runtime import resolve_mcp_llm


def test_mcp_defaults_to_shared_keyless_runtime(monkeypatch) -> None:
	"""Generated legacy placeholders must not activate Browser Use Cloud authentication."""
	monkeypatch.delenv('DEFAULT_LLM', raising=False)
	monkeypatch.setenv('SUBSCRIPTION_LLM_PROVIDER', 'codex')

	llm = resolve_mcp_llm({'model': 'gpt-4.1-mini', 'api_key': 'your-openai-api-key-here'})

	assert isinstance(llm, ChatSubscriptionCLI)
	assert llm.provider_name == SubscriptionCLIProvider.CODEX


def test_mcp_preserves_explicit_openai_model_name() -> None:
	"""Explicit legacy OpenAI configuration remains backwards compatible."""
	llm = resolve_mcp_llm(
		{
			'model': 'future-model-name-not-known-to-browser-use',
			'api_key': 'test-openai-key',
			'temperature': 0.4,
		}
	)

	assert isinstance(llm, ChatOpenAI)
	assert llm.model == 'future-model-name-not-known-to-browser-use'
	assert llm.temperature == 0.4


def test_mcp_legacy_openai_key_without_model_uses_compatible_default() -> None:
	"""A legacy key-only MCP config must keep the historical OpenAI fallback."""
	llm = resolve_mcp_llm({'api_key': 'test-openai-key'})

	assert isinstance(llm, ChatOpenAI)
	assert llm.model == 'gpt-4o'


def test_mcp_keyless_override_preserves_model_name(monkeypatch) -> None:
	"""MCP forwards future model IDs to the selected keyless runtime without an allowlist."""
	monkeypatch.setenv('SUBSCRIPTION_LLM_PROVIDER', 'claude')
	model_name = 'provider/future-model-version'

	llm = resolve_mcp_llm({}, model_override=model_name)

	assert isinstance(llm, ChatSubscriptionCLI)
	assert llm.provider_name == SubscriptionCLIProvider.CLAUDE
	assert llm.model == model_name
