"""Tests for isolated subscription-authenticated Codex, Claude, and Grok adapters."""

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from browser_use.llm.messages import ContentPartImageParam, ImageURL, UserMessage
from browser_use.llm.subscription_cli import (
	ChatSubscriptionCLI,
	SubscriptionCLIProcessResult,
	SubscriptionCLIProvider,
	_run_subscription_process,
	_serialize_messages,
	_subscription_environment,
	inspect_subscription_cli,
)


class StructuredAnswer(BaseModel):
	"""Validated payload used by subscription CLI parser tests."""

	status: str


def process_result(*, stdout: str = '', stderr: str = '', returncode: int = 0) -> SubscriptionCLIProcessResult:
	"""Build a bounded fake CLI process result."""
	return SubscriptionCLIProcessResult(returncode=returncode, stdout=stdout, stderr=stderr, duration_ms=1)


def test_subscription_environment_removes_all_supported_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Official CLIs must use stored subscriptions even when API keys exist in the parent."""
	for variable_name in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'XAI_API_KEY', 'GROK_API_KEY'):
		monkeypatch.setenv(variable_name, 'must-not-be-forwarded')

	environment = _subscription_environment()

	assert not {'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'XAI_API_KEY', 'GROK_API_KEY'} & environment.keys()


@pytest.mark.parametrize(
	('provider', 'expected_flags'),
	[
		(
			SubscriptionCLIProvider.CODEX,
			{'--ephemeral', '--sandbox', 'read-only', '--ignore-user-config', '--ignore-rules'},
		),
		(
			SubscriptionCLIProvider.CLAUDE,
			{'--print', '--safe-mode', '--no-session-persistence', '--permission-mode', 'dontAsk'},
		),
		(
			SubscriptionCLIProvider.GROK,
			{'--disable-web-search', '--no-memory', '--no-subagents', '--no-plan', '--verbatim'},
		),
	],
)
def test_cli_arguments_are_noninteractive_and_preserve_explicit_model(
	tmp_path: Path,
	provider: SubscriptionCLIProvider,
	expected_flags: set[str],
) -> None:
	"""Each CLI route must be isolated while forwarding unknown model identifiers verbatim."""
	llm = ChatSubscriptionCLI(provider_name=provider, model='future-subscription-model')
	arguments, _result_path, _prompt_path = llm._build_arguments(
		executable_path=f'/usr/bin/{provider.value}',
		temporary_directory=tmp_path,
		output_format=StructuredAnswer,
	)

	assert expected_flags <= set(arguments)
	assert arguments[arguments.index('--model') + 1] == 'future-subscription-model'
	if provider != SubscriptionCLIProvider.CODEX:
		assert arguments[arguments.index('--tools') + 1] == ''
	if provider == SubscriptionCLIProvider.CODEX:
		schema_path = Path(arguments[arguments.index('--output-schema') + 1])
		assert json.loads(schema_path.read_text(encoding='utf-8'))['additionalProperties'] is False


async def test_authentication_inspection_uses_sanitized_official_statuses(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Status checks expose only auth metadata, never account identities or tokens."""
	responses = {
		'codex': process_result(stdout='Logged in using ChatGPT'),
		'claude': process_result(
			stdout=json.dumps(
				{
					'loggedIn': True,
					'authMethod': 'claude.ai',
					'subscriptionType': 'max',
					'email': 'private@example.com',
				}
			)
		),
		'grok': process_result(stdout='You are not authenticated'),
	}

	async def fake_process(arguments: list[str], **_kwargs: object) -> SubscriptionCLIProcessResult:
		return responses[Path(arguments[0]).name]

	monkeypatch.setattr('browser_use.llm.subscription_cli.shutil.which', lambda name: f'/usr/bin/{name}')
	monkeypatch.setattr('browser_use.llm.subscription_cli._run_subscription_process', fake_process)

	codex = await inspect_subscription_cli(SubscriptionCLIProvider.CODEX)
	claude = await inspect_subscription_cli(SubscriptionCLIProvider.CLAUDE)
	grok = await inspect_subscription_cli(SubscriptionCLIProvider.GROK)

	assert codex.authenticated and codex.auth_method == 'chatgpt'
	assert claude.authenticated and claude.subscription_type == 'max'
	assert 'private@example.com' not in claude.model_dump_json()
	assert not grok.authenticated and grok.reason == 'Grok OAuth login is required'


@pytest.mark.parametrize('provider', list(SubscriptionCLIProvider))
def test_structured_completion_is_validated_for_every_provider(
	tmp_path: Path,
	provider: SubscriptionCLIProvider,
) -> None:
	"""Provider-specific envelopes converge on one Pydantic-validated completion contract."""
	llm = ChatSubscriptionCLI(provider_name=provider)
	result_path: Path | None = None
	if provider == SubscriptionCLIProvider.CODEX:
		result_path = tmp_path / 'result.json'
		result_path.write_text('{"status":"ok"}', encoding='utf-8')
		result = process_result()
	elif provider == SubscriptionCLIProvider.CLAUDE:
		result = process_result(stdout='{"is_error":false,"structured_output":{"status":"ok"}}')
	else:
		result = process_result(stdout='{"response":{"text":"{\\"status\\":\\"ok\\"}","provider":"grok"}}')

	completion = llm._parse_completion(
		result=result,
		result_path=result_path,
		output_format=StructuredAnswer,
	)

	assert completion == StructuredAnswer(status='ok')


def test_message_serializer_rejects_vision_input() -> None:
	"""Subscription coding CLIs cannot silently lose screenshots from an agent request."""
	message = UserMessage(content=[ContentPartImageParam(image_url=ImageURL(url='data:image/png;base64,AA=='))])
	with pytest.raises(ValueError, match='use_vision=False'):
		_serialize_messages([message])


def test_process_runner_requires_explicit_working_directory() -> None:
	"""The low-level runner contract keeps every invocation scoped to a temporary directory."""
	assert 'working_directory' in _run_subscription_process.__annotations__
