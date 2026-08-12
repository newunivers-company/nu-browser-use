"""Typed LLM configuration and resolution for the MCP server."""

import os
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, SecretStr

from browser_use.agent.runtime import resolve_default_llm
from browser_use.llm.base import BaseChatModel


class MCPConfiguredLLM(BaseModel):
	"""Validated legacy MCP LLM settings loaded from browser-use configuration."""

	model_config = ConfigDict(extra='ignore')

	model: str | None = None
	model_provider: str | None = None
	api_key: SecretStr | None = None
	base_url: str | None = None
	temperature: float | None = None
	region: str | None = None
	aws_sso_auth: bool = False

	def usable_api_key(self) -> str | None:
		"""Return a configured API key while rejecting generated placeholders."""
		if self.api_key is None:
			return None
		api_key = self.api_key.get_secret_value().strip()
		if not api_key or api_key == 'your-openai-api-key-here':
			return None
		return api_key


def resolve_mcp_llm(llm_config: Mapping[str, Any], model_override: str | None = None) -> BaseChatModel:
	"""Resolve MCP agent models with the same default policy as the core Agent.

	Explicit legacy OpenAI and Bedrock configuration remains supported. When no
	provider is explicitly configured, the core runtime selects ``ChatBrowserUse``
	or the model named by ``DEFAULT_LLM``.
	"""
	configured_llm = MCPConfiguredLLM.model_validate(dict(llm_config))
	model_provider = (configured_llm.model_provider or os.getenv('MODEL_PROVIDER') or '').lower()

	if model_provider == 'bedrock':
		from browser_use.llm import ChatAWSBedrock

		return ChatAWSBedrock(
			model=model_override or configured_llm.model or 'us.anthropic.claude-sonnet-4-20250514-v1:0',
			aws_region=configured_llm.region or os.getenv('REGION') or 'us-east-1',
			aws_sso_auth=configured_llm.aws_sso_auth,
		)

	api_key = configured_llm.usable_api_key()
	if api_key:
		from browser_use.llm.openai.chat import ChatOpenAI

		# Preserve the legacy MCP behavior for configurations that only supplied
		# OPENAI_API_KEY. Explicit model names, including future model IDs, still win.
		model_name = model_override or configured_llm.model or 'gpt-4o'
		return ChatOpenAI(
			model=model_name,
			api_key=api_key,
			temperature=configured_llm.temperature,
			base_url=configured_llm.base_url,
		)

	if model_override:
		browser_use_api_key = os.getenv('BROWSER_USE_API_KEY')
		if browser_use_api_key:
			from browser_use import ChatBrowserUse

			return ChatBrowserUse(model=model_override, api_key=browser_use_api_key)

		raise ValueError(
			'A model override requires BROWSER_USE_API_KEY or explicit provider credentials. '
			'Use a provider-prefixed Browser Use gateway model ID when using BROWSER_USE_API_KEY.'
		)

	return resolve_default_llm(None)
