"""Typed keyless runtime selection shared by Agent, MCP, and evaluation entry points."""

from __future__ import annotations

import os
import shutil
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from browser_use.browser.profile import BrowserProfile
from browser_use.llm.base import BaseChatModel
from browser_use.llm.subscription_cli import ChatSubscriptionCLI, SubscriptionCLIProvider


class RuntimeLLMBackend(StrEnum):
	"""Keyless model backends available to the shared runtime."""

	AUTO = 'auto'
	SUBSCRIPTION = 'subscription'
	OLLAMA = 'ollama'
	OPENAI_LIKE = 'openai_like'


class RuntimeBrowserBackend(StrEnum):
	"""Browser transports that do not require Browser Use Cloud credentials."""

	LOCAL = 'local'
	CDP = 'cdp'


class RuntimeConfig(BaseModel):
	"""Validated environment-independent configuration for one keyless runtime."""

	model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

	llm_backend: RuntimeLLMBackend = RuntimeLLMBackend.AUTO
	subscription_provider: SubscriptionCLIProvider | None = None
	subscription_model: str = 'default'
	subscription_timeout_seconds: float = Field(default=180.0, ge=10, le=600)
	local_model: str | None = None
	local_base_url: str | None = None
	local_api_key: str = 'local-keyless-runtime'
	browser_backend: RuntimeBrowserBackend = RuntimeBrowserBackend.LOCAL
	executable_path: Path | None = None
	cdp_url: str | None = None
	headless: bool | None = None

	@model_validator(mode='after')
	def validate_runtime_contract(self) -> RuntimeConfig:
		"""Reject incomplete model and browser selections before launching processes."""
		if self.llm_backend in {RuntimeLLMBackend.OLLAMA, RuntimeLLMBackend.OPENAI_LIKE} and not self.local_model:
			raise ValueError(f'local_model is required for {self.llm_backend.value} runtime')
		if self.llm_backend == RuntimeLLMBackend.SUBSCRIPTION and self.subscription_provider is None:
			self.subscription_provider = SubscriptionCLIProvider.CODEX
		if self.browser_backend == RuntimeBrowserBackend.CDP and not self.cdp_url:
			raise ValueError('cdp_url is required for the cdp browser runtime')
		if self.browser_backend == RuntimeBrowserBackend.LOCAL and self.cdp_url:
			raise ValueError('cdp_url requires browser_backend=cdp')
		if self.browser_backend == RuntimeBrowserBackend.CDP and self.executable_path is not None:
			raise ValueError('executable_path cannot be combined with browser_backend=cdp')
		return self

	@classmethod
	def from_environment(cls) -> RuntimeConfig:
		"""Load the keyless runtime contract without reading any provider API-key variable."""
		subscription_provider = os.getenv('SUBSCRIPTION_LLM_PROVIDER')
		headless_value = os.getenv('BROWSER_USE_HEADLESS')
		cdp_url = os.getenv('BROWSER_USE_CDP_URL')
		return cls(
			llm_backend=RuntimeLLMBackend(os.getenv('BROWSER_USE_LLM_BACKEND', RuntimeLLMBackend.AUTO.value)),
			subscription_provider=(SubscriptionCLIProvider(subscription_provider) if subscription_provider else None),
			subscription_model=os.getenv('SUBSCRIPTION_LLM_MODEL', 'default'),
			subscription_timeout_seconds=float(os.getenv('SUBSCRIPTION_LLM_TIMEOUT_SECONDS', '180')),
			local_model=os.getenv('KEYLESS_LLM_MODEL'),
			local_base_url=os.getenv('KEYLESS_LLM_BASE_URL'),
			local_api_key=os.getenv('KEYLESS_LLM_API_KEY', 'local-keyless-runtime'),
			browser_backend=RuntimeBrowserBackend.CDP if cdp_url else RuntimeBrowserBackend.LOCAL,
			executable_path=(Path(path).expanduser() if (path := os.getenv('BROWSER_USE_EXECUTABLE_PATH')) else None),
			cdp_url=cdp_url,
			headless=_parse_optional_boolean(headless_value),
		)


def _parse_optional_boolean(value: str | None) -> bool | None:
	"""Parse an optional environment boolean with strict accepted spellings."""
	if value is None or not value.strip():
		return None
	normalized = value.strip().lower()
	if normalized in {'1', 'true', 'yes', 'on'}:
		return True
	if normalized in {'0', 'false', 'no', 'off'}:
		return False
	raise ValueError(f'invalid boolean value: {value!r}')


def _installed_subscription_provider() -> SubscriptionCLIProvider | None:
	"""Return the first installed official subscription CLI in stable preference order."""
	for provider in (
		SubscriptionCLIProvider.CODEX,
		SubscriptionCLIProvider.CLAUDE,
		SubscriptionCLIProvider.GROK,
	):
		if shutil.which(provider.value):
			return provider
	return None


def resolve_runtime_llm(config: RuntimeConfig | None = None, *, model_override: str | None = None) -> BaseChatModel:
	"""Create the explicitly configured keyless model without changing its model name."""
	runtime = config or RuntimeConfig.from_environment()
	backend = runtime.llm_backend
	if backend == RuntimeLLMBackend.AUTO:
		if runtime.subscription_provider is not None:
			backend = RuntimeLLMBackend.SUBSCRIPTION
		elif runtime.local_model:
			backend = (
				RuntimeLLMBackend.OPENAI_LIKE
				if runtime.local_base_url and runtime.local_base_url.rstrip('/').endswith('/v1')
				else RuntimeLLMBackend.OLLAMA
			)
		else:
			installed_provider = _installed_subscription_provider()
			if installed_provider is None:
				raise ValueError(
					'No keyless LLM runtime is available. Sign in with an official Codex, Claude, or Grok CLI, '
					'or configure KEYLESS_LLM_MODEL for an installed local model.'
				)
			runtime = runtime.model_copy(update={'subscription_provider': installed_provider})
			backend = RuntimeLLMBackend.SUBSCRIPTION

	if backend == RuntimeLLMBackend.SUBSCRIPTION:
		provider = runtime.subscription_provider or SubscriptionCLIProvider.CODEX
		return ChatSubscriptionCLI(
			provider_name=provider,
			model=model_override or runtime.subscription_model,
			timeout=runtime.subscription_timeout_seconds,
		)

	if backend == RuntimeLLMBackend.OLLAMA:
		from browser_use.llm.ollama.chat import ChatOllama

		local_model = model_override or runtime.local_model
		assert local_model is not None
		return ChatOllama(
			model=local_model,
			host=runtime.local_base_url or 'http://127.0.0.1:11434',
			timeout=120,
		)

	from browser_use.llm.openai.like import ChatOpenAILike

	local_model = model_override or runtime.local_model
	assert local_model is not None
	return ChatOpenAILike(
		model=local_model,
		base_url=runtime.local_base_url or 'http://127.0.0.1:8000/v1',
		api_key=runtime.local_api_key,
		timeout=120,
		max_retries=1,
		add_schema_to_system_prompt=True,
	)


def _detect_local_browser_executable() -> Path | None:
	"""Find a system Chromium executable while allowing BrowserSession's bundled fallback."""
	for executable_name in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'chrome'):
		if executable_path := shutil.which(executable_name):
			return Path(executable_path)
	return None


def resolve_runtime_browser_profile(config: RuntimeConfig | None = None) -> BrowserProfile:
	"""Build a local or caller-supplied CDP profile that never provisions Browser Use Cloud."""
	runtime = config or RuntimeConfig.from_environment()
	if runtime.browser_backend == RuntimeBrowserBackend.CDP:
		return BrowserProfile(cdp_url=runtime.cdp_url, is_local=False, use_cloud=False, headless=runtime.headless)
	executable_path = runtime.executable_path or _detect_local_browser_executable()
	return BrowserProfile(
		executable_path=executable_path,
		is_local=True,
		use_cloud=False,
		headless=runtime.headless,
	)
