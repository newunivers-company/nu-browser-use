"""Use authenticated Codex, Claude, and Grok subscriptions through their official CLIs."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import tempfile
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar, overload

from pydantic import BaseModel, ConfigDict, Field

from browser_use.llm.base import BaseChatModel
from browser_use.llm.exceptions import ModelProviderError
from browser_use.llm.messages import AssistantMessage, BaseMessage, ContentPartImageParam
from browser_use.llm.schema import SchemaOptimizer
from browser_use.llm.views import ChatInvokeCompletion

T = TypeVar('T', bound=BaseModel)

_API_KEY_ENVIRONMENT_VARIABLES = (
	'ANTHROPIC_API_KEY',
	'BROWSER_USE_API_KEY',
	'GROK_API_KEY',
	'OPENAI_API_KEY',
	'XAI_API_KEY',
)
_BACKEND_SYSTEM_PROMPT = (
	'Act only as a text chat-model backend for Browser Use. Do not inspect files, run commands, use tools, '
	'or modify the environment. Follow the supplied messages and return only the requested final response.'
)


class SubscriptionCLIProvider(StrEnum):
	"""Official subscription-authenticated CLI providers supported by the adapter."""

	CODEX = 'codex'
	CLAUDE = 'claude'
	GROK = 'grok'


class SubscriptionCLIStatus(BaseModel):
	"""Sanitized CLI installation and subscription authentication status."""

	model_config = ConfigDict(extra='forbid')

	provider: SubscriptionCLIProvider
	executable: str | None = None
	installed: bool
	authenticated: bool
	auth_method: str | None = None
	subscription_type: str | None = None
	reason: str


class SubscriptionCLIProcessResult(BaseModel):
	"""Bounded process result used internally without exposing credential material."""

	model_config = ConfigDict(extra='forbid')

	returncode: int
	stdout: str = Field(max_length=2_000_000)
	stderr: str = Field(max_length=2_000_000)
	duration_ms: int = Field(ge=0)


class ClaudeCLIEnvelope(BaseModel):
	"""Relevant fields from Claude Code's JSON result envelope."""

	model_config = ConfigDict(extra='allow')

	is_error: bool = False
	result: str | None = None
	structured_output: dict[str, Any] | None = None


def _subscription_environment() -> dict[str, str]:
	"""Build an environment that forces CLIs to use their stored subscription sessions."""
	environment = os.environ.copy()
	for variable_name in _API_KEY_ENVIRONMENT_VARIABLES:
		environment.pop(variable_name, None)
	environment['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'] = '1'
	return environment


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
	"""Terminate a CLI and any child process it may have started."""
	if process.returncode is not None:
		return
	try:
		if os.name == 'posix':
			os.killpg(os.getpgid(process.pid), signal.SIGTERM)
		else:
			process.terminate()
	except ProcessLookupError:
		return

	try:
		await asyncio.wait_for(process.wait(), timeout=5)
		return
	except TimeoutError:
		pass

	try:
		if os.name == 'posix':
			os.killpg(os.getpgid(process.pid), signal.SIGKILL)
		else:
			process.kill()
	except ProcessLookupError:
		return
	await process.wait()


async def _run_subscription_process(
	arguments: list[str],
	*,
	input_text: str | None = None,
	timeout_seconds: float,
	working_directory: Path,
) -> SubscriptionCLIProcessResult:
	"""Run one subscription CLI call with bounded time and process-tree cleanup."""
	started_at = time.monotonic()
	process = await asyncio.create_subprocess_exec(
		*arguments,
		stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
		stdout=asyncio.subprocess.PIPE,
		stderr=asyncio.subprocess.PIPE,
		cwd=working_directory,
		env=_subscription_environment(),
		start_new_session=os.name == 'posix',
	)
	try:
		stdout, stderr = await asyncio.wait_for(
			process.communicate(input_text.encode('utf-8') if input_text is not None else None),
			timeout=timeout_seconds,
		)
	except TimeoutError:
		await _terminate_process_tree(process)
		raise TimeoutError(f'subscription CLI exceeded its {timeout_seconds:.0f}s deadline') from None
	except asyncio.CancelledError:
		await _terminate_process_tree(process)
		raise
	return SubscriptionCLIProcessResult(
		returncode=process.returncode or 0,
		stdout=stdout.decode('utf-8', errors='replace').strip(),
		stderr=stderr.decode('utf-8', errors='replace').strip(),
		duration_ms=round((time.monotonic() - started_at) * 1000),
	)


def _bounded_error(result: SubscriptionCLIProcessResult) -> str:
	"""Return a short diagnostic without forwarding a complete CLI transcript."""
	diagnostic = result.stderr or result.stdout or 'no diagnostic output'
	diagnostic = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[redacted-email]', diagnostic)
	return diagnostic[-1000:]


def _serialize_messages(messages: list[BaseMessage]) -> str:
	"""Serialize Browser Use messages while rejecting unsupported screenshot input."""
	sections: list[str] = []
	for message in messages:
		content = getattr(message, 'content', None)
		if isinstance(content, list) and any(isinstance(part, ContentPartImageParam) for part in content):
			raise ValueError('subscription CLI adapters are text-only; configure Agent(use_vision=False)')
		text = message.text
		if isinstance(message, AssistantMessage) and message.tool_calls:
			tool_calls = [tool_call.model_dump(mode='json') for tool_call in message.tool_calls]
			text = f'{text}\n<tool_calls>{json.dumps(tool_calls, ensure_ascii=False)}</tool_calls>'
		sections.append(f'<message role="{message.role}">\n{text}\n</message>')
	return f'{_BACKEND_SYSTEM_PROMPT}\n\nThe conversation follows in chronological order.\n\n' + '\n\n'.join(sections)


def _load_json_value(value: str) -> Any:
	"""Parse a CLI JSON string, accepting a single fenced JSON response."""
	cleaned = value.strip()
	if cleaned.startswith('```'):
		cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned, flags=re.IGNORECASE)
		cleaned = re.sub(r'\s*```$', '', cleaned)
	return json.loads(cleaned)


def _extract_generic_envelope(payload: Any) -> Any:
	"""Extract a final response from Grok-style JSON envelopes without a version allowlist."""
	if not isinstance(payload, dict):
		return payload
	for key in ('structured_output', 'result', 'response', 'output', 'content', 'message', 'text'):
		value = payload.get(key)
		if value is not None:
			if isinstance(value, dict) and key == 'message':
				value = value.get('content', value)
			return _extract_generic_envelope(value)
	return payload


def _remove_json_schema_formats(value: Any) -> None:
	"""Remove advisory formats rejected by Codex while preserving Pydantic validation after inference."""
	if isinstance(value, dict):
		value.pop('format', None)
		for nested_value in value.values():
			_remove_json_schema_formats(nested_value)
	elif isinstance(value, list):
		for item in value:
			_remove_json_schema_formats(item)


def _provider_compatible_schema(
	provider: SubscriptionCLIProvider,
	output_format: type[BaseModel] | None,
) -> dict[str, Any] | None:
	"""Return a strict schema accepted by the selected official subscription CLI."""
	if output_format is None:
		return None
	schema = SchemaOptimizer.create_optimized_json_schema(output_format)
	if provider == SubscriptionCLIProvider.CODEX:
		_remove_json_schema_formats(schema)
	return schema


async def inspect_subscription_cli(
	provider: SubscriptionCLIProvider,
	*,
	executable: str | None = None,
	timeout_seconds: float = 15,
) -> SubscriptionCLIStatus:
	"""Inspect one CLI through its own status command without reading credential files."""
	executable_path = executable or shutil.which(provider.value)
	if executable_path is None:
		return SubscriptionCLIStatus(
			provider=provider,
			installed=False,
			authenticated=False,
			reason=f'{provider.value} CLI is not installed',
		)

	with tempfile.TemporaryDirectory(prefix=f'browser-use-{provider.value}-auth-') as temporary_directory:
		working_directory = Path(temporary_directory)
		if provider == SubscriptionCLIProvider.CODEX:
			arguments = [executable_path, 'login', 'status']
		elif provider == SubscriptionCLIProvider.CLAUDE:
			arguments = [executable_path, 'auth', 'status']
		else:
			arguments = [executable_path, 'models']
		try:
			result = await _run_subscription_process(
				arguments,
				timeout_seconds=timeout_seconds,
				working_directory=working_directory,
			)
		except Exception as error:
			return SubscriptionCLIStatus(
				provider=provider,
				executable=executable_path,
				installed=True,
				authenticated=False,
				reason=f'authentication status failed: {type(error).__name__}: {error}',
			)

	if provider == SubscriptionCLIProvider.CODEX:
		status_text = f'{result.stdout}\n{result.stderr}'.casefold()
		authenticated = result.returncode == 0 and 'logged in using chatgpt' in status_text
		return SubscriptionCLIStatus(
			provider=provider,
			executable=executable_path,
			installed=True,
			authenticated=authenticated,
			auth_method='chatgpt' if authenticated else None,
			reason='ChatGPT subscription session is available' if authenticated else 'ChatGPT subscription login is required',
		)

	if provider == SubscriptionCLIProvider.CLAUDE:
		try:
			payload = _load_json_value(result.stdout)
		except (json.JSONDecodeError, TypeError):
			payload = {}
		authenticated = bool(
			result.returncode == 0
			and isinstance(payload, dict)
			and payload.get('loggedIn')
			and payload.get('authMethod') == 'claude.ai'
		)
		return SubscriptionCLIStatus(
			provider=provider,
			executable=executable_path,
			installed=True,
			authenticated=authenticated,
			auth_method='claude.ai' if authenticated else None,
			subscription_type=str(payload.get('subscriptionType')) if authenticated else None,
			reason='Claude subscription session is available' if authenticated else 'Claude subscription login is required',
		)

	authenticated = result.returncode == 0 and 'not authenticated' not in result.stdout.casefold()
	return SubscriptionCLIStatus(
		provider=provider,
		executable=executable_path,
		installed=True,
		authenticated=authenticated,
		auth_method='oauth' if authenticated else None,
		reason='Grok subscription session is available' if authenticated else 'Grok OAuth login is required',
	)


@dataclass
class ChatSubscriptionCLI(BaseChatModel):
	"""BaseChatModel adapter over official subscription-authenticated model CLIs.

	The adapter never reads or copies credential files. API key environment variables are removed
	from child processes so the official CLI must use its own stored subscription session.
	"""

	provider_name: SubscriptionCLIProvider
	model: str = 'default'
	executable: str | None = None
	timeout: float = 180

	_verified_api_keys = True

	def __post_init__(self) -> None:
		"""Normalize validated scalar configuration without changing explicit model names."""
		self.provider_name = SubscriptionCLIProvider(self.provider_name)
		if self.timeout <= 0:
			raise ValueError('timeout must be greater than zero')

	@property
	def provider(self) -> str:
		"""Return a distinct provider identifier for logs and routing."""
		return f'subscription-{self.provider_name.value}'

	@property
	def name(self) -> str:
		"""Return the explicit model name or the CLI-owned default label."""
		return self.model

	@property
	def supports_vision(self) -> bool:
		"""Declare that official subscription CLI transports currently accept text only."""
		return False

	def _resolve_executable(self) -> str:
		"""Resolve the configured CLI executable or raise an actionable error."""
		executable_path = self.executable or shutil.which(self.provider_name.value)
		if executable_path is None:
			raise ModelProviderError(message=f'{self.provider_name.value} CLI is not installed', model=self.name)
		return executable_path

	def _build_arguments(
		self,
		*,
		executable_path: str,
		temporary_directory: Path,
		output_format: type[BaseModel] | None,
	) -> tuple[list[str], Path | None, Path | None]:
		"""Build provider-specific safe, non-interactive CLI arguments."""
		schema = _provider_compatible_schema(self.provider_name, output_format)
		if self.provider_name == SubscriptionCLIProvider.CODEX:
			result_path = temporary_directory / 'final-response.txt'
			arguments = [
				executable_path,
				'exec',
				'--ephemeral',
				'--sandbox',
				'read-only',
				'--skip-git-repo-check',
				'--ignore-user-config',
				'--ignore-rules',
				'--color',
				'never',
				'-C',
				str(temporary_directory),
				'--output-last-message',
				str(result_path),
			]
			if self.model != 'default':
				arguments.extend(['--model', self.model])
			if schema is not None:
				schema_path = temporary_directory / 'output-schema.json'
				schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding='utf-8')
				arguments.extend(['--output-schema', str(schema_path)])
			arguments.append('-')
			return arguments, result_path, None

		if self.provider_name == SubscriptionCLIProvider.CLAUDE:
			arguments = [
				executable_path,
				'--print',
				'--safe-mode',
				'--no-session-persistence',
				'--permission-mode',
				'dontAsk',
				'--tools',
				'',
				'--disable-slash-commands',
				'--no-chrome',
				'--output-format',
				'json',
				'--system-prompt',
				_BACKEND_SYSTEM_PROMPT,
			]
			if self.model != 'default':
				arguments.extend(['--model', self.model])
			if schema is not None:
				arguments.extend(['--json-schema', json.dumps(schema, ensure_ascii=False)])
			return arguments, None, None

		prompt_path = temporary_directory / 'prompt.txt'
		arguments = [
			executable_path,
			'--cwd',
			str(temporary_directory),
			'--disable-web-search',
			'--no-memory',
			'--no-subagents',
			'--no-plan',
			'--verbatim',
			'--permission-mode',
			'dontAsk',
			'--tools',
			'',
			'--output-format',
			'json',
			'--system-prompt-override',
			_BACKEND_SYSTEM_PROMPT,
			'--prompt-file',
			str(prompt_path),
		]
		if self.model != 'default':
			arguments.extend(['--model', self.model])
		if schema is not None:
			arguments.extend(['--json-schema', json.dumps(schema, ensure_ascii=False)])
		return arguments, None, prompt_path

	@overload
	def _parse_completion(
		self,
		*,
		result: SubscriptionCLIProcessResult,
		result_path: Path | None,
		output_format: None,
	) -> str: ...

	@overload
	def _parse_completion(
		self,
		*,
		result: SubscriptionCLIProcessResult,
		result_path: Path | None,
		output_format: type[T],
	) -> T: ...

	def _parse_completion(
		self,
		*,
		result: SubscriptionCLIProcessResult,
		result_path: Path | None,
		output_format: type[T] | None,
	) -> T | str:
		"""Parse provider output and validate requested Pydantic structured output."""
		if result.returncode != 0:
			raise ModelProviderError(
				message=f'{self.provider_name.value} CLI exited with code {result.returncode}: {_bounded_error(result)}',
				model=self.name,
			)

		if self.provider_name == SubscriptionCLIProvider.CODEX:
			if result_path is None or not result_path.is_file():
				raise ModelProviderError(message='Codex CLI did not produce a final response file', model=self.name)
			completion: Any = result_path.read_text(encoding='utf-8').strip()
		elif self.provider_name == SubscriptionCLIProvider.CLAUDE:
			try:
				envelope = ClaudeCLIEnvelope.model_validate(_load_json_value(result.stdout))
			except Exception as error:
				raise ModelProviderError(message=f'Invalid Claude CLI JSON envelope: {error}', model=self.name) from error
			if envelope.is_error:
				raise ModelProviderError(message='Claude CLI returned an error result', model=self.name)
			completion = envelope.structured_output if envelope.structured_output is not None else envelope.result
		else:
			try:
				completion = _extract_generic_envelope(_load_json_value(result.stdout))
			except Exception as error:
				raise ModelProviderError(message=f'Invalid Grok CLI JSON envelope: {error}', model=self.name) from error

		if completion is None:
			raise ModelProviderError(message=f'{self.provider_name.value} CLI returned no completion', model=self.name)
		if output_format is None:
			return completion if isinstance(completion, str) else json.dumps(completion, ensure_ascii=False)
		try:
			payload = _load_json_value(completion) if isinstance(completion, str) else completion
			return output_format.model_validate(payload)
		except Exception as error:
			raise ModelProviderError(
				message=f'{self.provider_name.value} CLI returned invalid structured output: {error}',
				model=self.name,
			) from error

	@overload
	async def ainvoke(
		self,
		messages: list[BaseMessage],
		output_format: None = None,
		**kwargs: Any,
	) -> ChatInvokeCompletion[str]: ...

	@overload
	async def ainvoke(
		self,
		messages: list[BaseMessage],
		output_format: type[T],
		**kwargs: Any,
	) -> ChatInvokeCompletion[T]: ...

	async def ainvoke(
		self,
		messages: list[BaseMessage],
		output_format: type[T] | None = None,
		**kwargs: Any,
	) -> ChatInvokeCompletion[T] | ChatInvokeCompletion[str]:
		"""Invoke the authenticated CLI as a text-only structured chat backend."""
		del kwargs
		try:
			prompt = _serialize_messages(messages)
			executable_path = self._resolve_executable()
			with tempfile.TemporaryDirectory(prefix=f'browser-use-{self.provider_name.value}-llm-') as directory:
				temporary_directory = Path(directory)
				arguments, result_path, prompt_path = self._build_arguments(
					executable_path=executable_path,
					temporary_directory=temporary_directory,
					output_format=output_format,
				)
				input_text = prompt
				if prompt_path is not None:
					prompt_path.write_text(prompt, encoding='utf-8')
					input_text = None
				result = await _run_subscription_process(
					arguments,
					input_text=input_text,
					timeout_seconds=self.timeout,
					working_directory=temporary_directory,
				)
				if output_format is None:
					text_completion = self._parse_completion(
						result=result,
						result_path=result_path,
						output_format=None,
					)
					return ChatInvokeCompletion(completion=text_completion, usage=None)
				structured_completion = self._parse_completion(
					result=result,
					result_path=result_path,
					output_format=output_format,
				)
				return ChatInvokeCompletion(completion=structured_completion, usage=None)
		except ModelProviderError:
			raise
		except Exception as error:
			raise ModelProviderError(message=str(error), model=self.name) from error
