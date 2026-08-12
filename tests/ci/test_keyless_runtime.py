"""Tests for shared API-key-free Agent, MCP, and browser runtime selection."""

from pathlib import Path

import pytest

from browser_use import Agent
from browser_use.agent.runtime import resolve_default_llm
from browser_use.llm.ollama.chat import ChatOllama
from browser_use.llm.subscription_cli import ChatSubscriptionCLI, SubscriptionCLIProvider
from browser_use.runtime import (
	RuntimeBrowserBackend,
	RuntimeConfig,
	RuntimeLLMBackend,
	resolve_runtime_browser_profile,
	resolve_runtime_llm,
)


def test_runtime_environment_ignores_browser_use_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
	"""The shared default route must not inspect or activate Browser Use Cloud credentials."""
	monkeypatch.setenv('BROWSER_USE_API_KEY', 'unused-cloud-key')
	monkeypatch.setenv('SUBSCRIPTION_LLM_PROVIDER', 'claude')
	monkeypatch.setenv('SUBSCRIPTION_LLM_MODEL', 'future-subscription-model')

	llm = resolve_runtime_llm(RuntimeConfig.from_environment())

	assert isinstance(llm, ChatSubscriptionCLI)
	assert llm.provider_name == SubscriptionCLIProvider.CLAUDE
	assert llm.model == 'future-subscription-model'


def test_auto_runtime_selects_installed_subscription_cli(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Auto mode uses a stable installed CLI preference without reading credential files."""
	monkeypatch.delenv('DEFAULT_LLM', raising=False)
	monkeypatch.delenv('SUBSCRIPTION_LLM_PROVIDER', raising=False)
	monkeypatch.delenv('KEYLESS_LLM_MODEL', raising=False)
	monkeypatch.setattr('browser_use.runtime.shutil.which', lambda name: f'/usr/bin/{name}' if name == 'codex' else None)

	llm = resolve_default_llm(None)

	assert isinstance(llm, ChatSubscriptionCLI)
	assert llm.provider_name == SubscriptionCLIProvider.CODEX


def test_auto_runtime_requires_an_available_keyless_backend(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Missing subscription and local runtimes fail before an Agent tries to call a cloud model."""
	monkeypatch.delenv('SUBSCRIPTION_LLM_PROVIDER', raising=False)
	monkeypatch.delenv('KEYLESS_LLM_MODEL', raising=False)
	monkeypatch.setattr('browser_use.runtime.shutil.which', lambda _name: None)

	with pytest.raises(ValueError, match='No keyless LLM runtime is available'):
		resolve_runtime_llm(RuntimeConfig())


def test_local_runtime_preserves_explicit_model_name() -> None:
	"""Local model routing passes unknown and future model names through verbatim."""
	llm = resolve_runtime_llm(
		RuntimeConfig(
			llm_backend=RuntimeLLMBackend.OLLAMA,
			local_model='future-local-model:custom-tag',
			local_base_url='http://127.0.0.1:11434',
		)
	)

	assert isinstance(llm, ChatOllama)
	assert llm.model == 'future-local-model:custom-tag'


def test_runtime_builds_only_local_or_generic_cdp_browser_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Default browser resolution cannot provision Browser Use Cloud."""
	monkeypatch.setattr('browser_use.runtime._detect_local_browser_executable', lambda: Path('/usr/bin/google-chrome'))

	local_profile = resolve_runtime_browser_profile(RuntimeConfig())
	cdp_profile = resolve_runtime_browser_profile(
		RuntimeConfig(browser_backend=RuntimeBrowserBackend.CDP, cdp_url='http://browser.internal:9222')
	)

	assert local_profile.executable_path == Path('/usr/bin/google-chrome')
	assert local_profile.is_local is True
	assert local_profile.use_cloud is False
	assert cdp_profile.cdp_url == 'http://browser.internal:9222'
	assert cdp_profile.use_cloud is False


def test_subscription_runtime_automatically_disables_agent_vision() -> None:
	"""Text-only official CLI adapters do not require every caller to remember use_vision=False."""
	agent = Agent(
		task='Inspect the current page.',
		llm=ChatSubscriptionCLI(provider_name=SubscriptionCLIProvider.CODEX, executable='/bin/true'),
		directly_open_url=False,
	)

	assert agent.settings.use_vision is False
	assert agent.settings.require_live_evidence is True
	assert 'screenshot' not in agent.tools.registry.registry.actions
