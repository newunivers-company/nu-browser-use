"""Run browser-agent evaluations through deterministic, replay, local, or cloud routes."""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import logging
import os
import signal
import sys
import tempfile
import time
import warnings
from pathlib import Path
from typing import Any, Never, cast

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

load_dotenv()

from browser_use import Agent, AgentHistoryList, BrowserSession, ChatBrowserUse
from browser_use.agent.views import ActionResult
from browser_use.llm.base import BaseChatModel
from browser_use.llm.google.chat import ChatGoogle
from browser_use.llm.messages import BaseMessage, UserMessage
from browser_use.llm.subscription_cli import ChatSubscriptionCLI, SubscriptionCLIProvider, inspect_subscription_cli
from tests.ci.evaluation_models import (
	EvaluationBrowserOptions,
	EvaluationMode,
	EvaluationReasonCode,
	EvaluationResult,
	EvaluationSummary,
	LocalEvaluationProvider,
)
from tests.ci.keyless_evaluation import (
	KeylessRunnerOptions,
	build_evaluation_browser_profile,
	close_browser_session,
	load_evaluation_task,
	run_keyless_task,
)

DEFAULT_TASK_DIR = Path(__file__).resolve().parents[1] / 'agent_tasks'
DEFAULT_MIN_PASS_RATE = 0.60
DEFAULT_MIN_EXECUTED_TASKS = 3
KEYLESS_EVALUATION_MODES = frozenset(
	{
		EvaluationMode.HYBRID,
		EvaluationMode.DETERMINISTIC,
		EvaluationMode.REPLAY,
		EvaluationMode.LOCAL,
		EvaluationMode.SUBSCRIPTION,
	}
)


class SubscriptionProbe(BaseModel):
	"""Small structured response proving a subscription CLI can serve model calls."""

	status: str = Field(pattern='^ok$')


class EvaluationRunOptions(EvaluationBrowserOptions):
	"""Validated CLI and environment inputs for one evaluation run."""

	model_config = ConfigDict(extra='forbid')

	mode: EvaluationMode = EvaluationMode.AUTO
	task_dir: Path = DEFAULT_TASK_DIR
	task_path: Path | None = None
	output_path: Path | None = None
	history_dir: Path | None = None
	max_parallel: int = Field(default=1, ge=1, le=10)
	browser_preflight: bool = True
	browser_launch_timeout_seconds: float = Field(default=30.0, ge=1, le=120)
	browser_shutdown_timeout_seconds: float = Field(default=15.0, ge=1, le=120)
	required_stable_states: int = Field(default=2, ge=1, le=5)
	state_stability_tolerance: float = Field(default=0.2, ge=0, le=1)
	state_retry_delay_seconds: float = Field(default=1.0, ge=0, le=10)
	local_provider: LocalEvaluationProvider = LocalEvaluationProvider.AUTO
	local_model: str | None = None
	local_base_url: str | None = None
	local_api_key: str = 'local-keyless-evaluation'
	subscription_provider: SubscriptionCLIProvider | None = None
	subscription_model: str = 'default'
	subscription_judge_provider: SubscriptionCLIProvider | None = None
	subscription_judge_model: str = 'default'
	subscription_timeout_seconds: float = Field(default=180.0, ge=10, le=600)
	preflight_timeout_seconds: float = Field(default=20.0, ge=1, le=120)
	task_timeout_seconds: float = Field(default=150.0, ge=30, le=900)
	subprocess_attempts: int = Field(default=2, ge=1, le=3)
	minimum_pass_rate: float | None = Field(default=None, ge=0, le=1)
	minimum_executed_tasks: int | None = Field(default=None, ge=0)


class DeterministicReplayLLM:
	"""Prevent saved-history replay from silently making an external model call."""

	model = 'deterministic-replay-no-llm'
	_verified_api_keys = True

	@property
	def provider(self) -> str:
		"""Identify this guard as an offline replay provider."""
		return 'keyless-replay'

	@property
	def name(self) -> str:
		"""Return the stable diagnostic model name."""
		return self.model

	async def ainvoke(
		self,
		messages: list[BaseMessage],
		output_format: type[BaseModel] | None = None,
		**kwargs: Any,
	) -> Never:
		"""Reject AI-dependent history steps so replay cannot report a false pass."""
		del messages, output_format, kwargs
		raise RuntimeError('saved history requires an AI step that is unavailable in keyless replay mode')


def resolve_evaluation_mode(options: EvaluationRunOptions) -> EvaluationMode:
	"""Select deterministic-first hybrid execution unless the caller requests an exact route."""
	if options.mode != EvaluationMode.AUTO:
		return options.mode
	return EvaluationMode.HYBRID


def create_local_evaluation_llm(options: EvaluationRunOptions) -> BaseChatModel:
	"""Create a configured keyless local model adapter or fail with actionable context."""
	if not options.local_model:
		raise ValueError('KEYLESS_LLM_MODEL or --local-model is required for local mode')

	provider = options.local_provider
	if provider == LocalEvaluationProvider.AUTO:
		provider = (
			LocalEvaluationProvider.OPENAI_LIKE
			if options.local_base_url and options.local_base_url.rstrip('/').endswith('/v1')
			else LocalEvaluationProvider.OLLAMA
		)

	if provider == LocalEvaluationProvider.OLLAMA:
		from browser_use.llm.ollama.chat import ChatOllama

		return ChatOllama(
			model=options.local_model,
			host=options.local_base_url or 'http://127.0.0.1:11434',
			timeout=120,
		)

	from browser_use.llm.openai.like import ChatOpenAILike

	return ChatOpenAILike(
		model=options.local_model,
		base_url=options.local_base_url or 'http://127.0.0.1:8000/v1',
		api_key=options.local_api_key,
		timeout=120,
		max_retries=1,
		add_schema_to_system_prompt=True,
	)


def _subscription_provider(options: EvaluationRunOptions, *, judge: bool) -> SubscriptionCLIProvider:
	"""Resolve explicit agent/judge subscription providers with a Codex-first default."""
	if judge and options.subscription_judge_provider is not None:
		return options.subscription_judge_provider
	return options.subscription_provider or SubscriptionCLIProvider.CODEX


def create_subscription_evaluation_llm(options: EvaluationRunOptions, *, judge: bool = False) -> BaseChatModel:
	"""Create a keyless adapter backed only by an official CLI subscription session."""
	provider = _subscription_provider(options, judge=judge)
	model = options.subscription_judge_model if judge else options.subscription_model
	return ChatSubscriptionCLI(provider_name=provider, model=model, timeout=options.subscription_timeout_seconds)


def provider_preflight_errors(options: EvaluationRunOptions, mode: EvaluationMode) -> list[str]:
	"""Return missing provider requirements before any browser subprocess is launched."""
	errors: list[str] = []
	if mode == EvaluationMode.CLOUD:
		if not os.getenv('BROWSER_USE_API_KEY'):
			errors.append('BROWSER_USE_API_KEY is unavailable')
		if not os.getenv('GOOGLE_API_KEY'):
			errors.append('GOOGLE_API_KEY is unavailable')
	elif mode == EvaluationMode.LOCAL and not options.local_model:
		errors.append('KEYLESS_LLM_MODEL or --local-model is required')
	return errors


async def provider_connectivity_errors(options: EvaluationRunOptions, mode: EvaluationMode) -> list[str]:
	"""Probe configured model routes once before launching browser subprocesses."""
	if mode == EvaluationMode.SUBSCRIPTION:
		routes: dict[tuple[SubscriptionCLIProvider, str], set[str]] = {}
		for role, provider, model in (
			('agent', _subscription_provider(options, judge=False), options.subscription_model),
			('judge', _subscription_provider(options, judge=True), options.subscription_judge_model),
		):
			routes.setdefault((provider, model), set()).add(role)
		errors: list[str] = []
		for (provider, model), roles in sorted(routes.items(), key=lambda route: (route[0][0].value, route[0][1])):
			role = '/'.join(sorted(roles))
			status = await inspect_subscription_cli(
				provider,
				timeout_seconds=min(15.0, options.preflight_timeout_seconds),
			)
			if not status.authenticated:
				errors.append(f'{role} {provider.value} subscription unavailable: {status.reason}')
				continue
			try:
				llm = ChatSubscriptionCLI(
					provider_name=provider,
					model=model,
					timeout=options.subscription_timeout_seconds,
				)
				response = await asyncio.wait_for(
					llm.ainvoke(
						[UserMessage(content='Return JSON with status set to ok.')],
						output_format=SubscriptionProbe,
					),
					timeout=options.preflight_timeout_seconds,
				)
				if response.completion.status != 'ok':
					errors.append(f'{role} {provider.value} subscription preflight returned an invalid status')
			except Exception as error:
				errors.append(f'{role} {provider.value} subscription preflight failed: {type(error).__name__}: {error}')
		return errors

	if mode != EvaluationMode.LOCAL or not options.local_model:
		return []
	try:
		local_llm = create_local_evaluation_llm(options)
		response = await asyncio.wait_for(
			local_llm.ainvoke([UserMessage(content='Reply with exactly OK to confirm local evaluation availability.')]),
			timeout=options.preflight_timeout_seconds,
		)
		if not str(response.completion).strip():
			return ['local model preflight returned an empty response']
	except Exception as error:
		return [f'local model preflight failed: {type(error).__name__}: {error}']
	return []


async def browser_runtime_preflight_error(options: EvaluationRunOptions) -> str | None:
	"""Launch and close the selected browser once before scheduling task subprocesses."""
	if not options.browser_preflight:
		return None
	if options.use_cloud_browser and not os.getenv('BROWSER_USE_API_KEY'):
		return 'BROWSER_USE_API_KEY is required for the cloud browser runtime'
	if options.executable_path is not None:
		executable_path = options.executable_path.expanduser()
		if not executable_path.is_file() or not os.access(executable_path, os.X_OK):
			return f'Browser executable is missing or not executable: {executable_path}'

	session: BrowserSession | None = None
	with tempfile.TemporaryDirectory(prefix='nu-evaluation-browser-preflight-') as profile_directory:
		try:
			profile = build_evaluation_browser_profile(options, Path(profile_directory))
			session = BrowserSession(browser_profile=profile)
			await asyncio.wait_for(session.start(), timeout=options.browser_launch_timeout_seconds)
		except Exception as error:
			diagnostic = ' '.join(str(error).splitlines())[:2000]
			return f'Browser preflight failed: {type(error).__name__}: {diagnostic}'
		finally:
			await close_browser_session(session, options.browser_shutdown_timeout_seconds)
	return None


def _keyless_options(options: EvaluationRunOptions) -> KeylessRunnerOptions:
	"""Translate evaluation settings into deterministic browser resource controls."""
	return KeylessRunnerOptions(
		disable_sandbox=options.disable_sandbox,
		executable_path=options.executable_path,
		use_cloud_browser=options.use_cloud_browser,
		cloud_profile_id=options.cloud_profile_id,
		cloud_proxy_country_code=options.cloud_proxy_country_code,
		cloud_timeout_minutes=options.cloud_timeout_minutes,
		minimum_page_load_wait_seconds=options.minimum_page_load_wait_seconds,
		network_idle_wait_seconds=options.network_idle_wait_seconds,
		launch_timeout_seconds=options.browser_launch_timeout_seconds,
		shutdown_timeout_seconds=options.browser_shutdown_timeout_seconds,
		required_stable_states=options.required_stable_states,
		state_stability_tolerance=options.state_stability_tolerance,
		state_retry_delay_seconds=options.state_retry_delay_seconds,
	)


async def run_replay_task(task_path: Path, options: EvaluationRunOptions) -> EvaluationResult:
	"""Replay saved AgentHistory when configured, otherwise replay the declarative trace."""
	try:
		task_definition = load_evaluation_task(task_path)
	except Exception as error:
		return EvaluationResult(
			file=task_path.name,
			status='failed',
			explanation=f'Invalid task: {type(error).__name__}: {error}',
			mode=EvaluationMode.REPLAY,
			reason_code=EvaluationReasonCode.INVALID_TASK,
		)

	if task_definition.replay_history_path is not None:
		task_directory = task_path.resolve().parent
		history_path = (task_directory / task_definition.replay_history_path).resolve()
		if not history_path.is_relative_to(task_directory):
			return EvaluationResult(
				file=task_path.name,
				status='failed',
				explanation='replay_history_path must remain inside the task directory',
				mode=EvaluationMode.REPLAY,
				reason_code=EvaluationReasonCode.INVALID_TASK,
				source_id=task_definition.source_id,
			)
		if not history_path.is_file():
			return EvaluationResult(
				file=task_path.name,
				status='skipped',
				explanation=f'Saved replay history is unavailable: {history_path.name}',
				mode=EvaluationMode.REPLAY,
				reason_code=EvaluationReasonCode.REPLAY_UNAVAILABLE,
				source_id=task_definition.source_id,
			)
		return await _run_saved_history_replay(task_path, history_path, options)

	result = await run_keyless_task(task_path, _keyless_options(options))
	explanation = f'Declarative contract replay: {result.explanation}'
	return result.model_copy(update={'mode': EvaluationMode.REPLAY, 'explanation': explanation})


async def _run_saved_history_replay(
	task_path: Path,
	history_path: Path,
	options: EvaluationRunOptions,
) -> EvaluationResult:
	"""Execute a checked-in AgentHistory through the library's native rerun path."""
	started_at = time.monotonic()
	task_definition = load_evaluation_task(task_path)
	session: BrowserSession | None = None
	try:
		with tempfile.TemporaryDirectory(prefix=f'nu-history-replay-{task_definition.source_id}-') as profile_directory:
			profile = build_evaluation_browser_profile(options, Path(profile_directory))
			session = BrowserSession(browser_profile=profile)
			replay_llm = cast(BaseChatModel, DeterministicReplayLLM())
			agent = Agent(task=task_definition.task, llm=replay_llm, browser_session=session)
			results: list[ActionResult] = await agent.load_and_rerun(
				history_file=history_path,
				max_retries=2,
				skip_failures=False,
				delay_between_actions=0.5,
				max_step_interval=2.0,
				summary_llm=replay_llm,
				ai_step_llm=replay_llm,
			)

		errors = [result.error for result in results if result.error]
		final_result = results[-1] if results else None
		passed = bool(final_result and final_result.is_done and final_result.success and not errors)
		trace = [
			{
				'index': index,
				'is_done': result.is_done,
				'success': result.success,
				'error': result.error,
				'extracted_content': (result.extracted_content or '')[:500],
			}
			for index, result in enumerate(results)
		]
		return EvaluationResult(
			file=task_path.name,
			status='passed' if passed else 'failed',
			explanation=(
				f'Saved AgentHistory replay passed with {len(results) - 1} action results'
				if passed
				else f'Saved AgentHistory replay failed: {errors[0] if errors else "completion was unsuccessful"}'
			),
			mode=EvaluationMode.REPLAY,
			reason_code=EvaluationReasonCode.COMPLETED if passed else EvaluationReasonCode.AGENT_FAILED,
			source_id=task_definition.source_id,
			duration_ms=round((time.monotonic() - started_at) * 1000),
			output={
				'history_file': history_path.name,
				'action_result_count': max(0, len(results) - 1),
				'error_count': len(errors),
			},
			trace=trace,
		)
	except Exception as error:
		return EvaluationResult(
			file=task_path.name,
			status='failed',
			explanation=f'Saved AgentHistory replay failed: {type(error).__name__}: {error}',
			mode=EvaluationMode.REPLAY,
			reason_code=EvaluationReasonCode.AGENT_FAILED,
			source_id=task_definition.source_id,
			duration_ms=round((time.monotonic() - started_at) * 1000),
		)
	finally:
		await close_browser_session(session, options.browser_shutdown_timeout_seconds)


async def run_model_task(task_path: Path, options: EvaluationRunOptions, mode: EvaluationMode) -> EvaluationResult:
	"""Run one real agent using either local inference or the preferred cloud models."""
	started_at = time.monotonic()
	session: BrowserSession | None = None
	profile_directory = tempfile.TemporaryDirectory(prefix='nu-model-evaluation-')
	try:
		task_definition = load_evaluation_task(task_path)
		if mode == EvaluationMode.CLOUD:
			browser_use_api_key = os.getenv('BROWSER_USE_API_KEY')
			google_api_key = os.getenv('GOOGLE_API_KEY')
			if not browser_use_api_key:
				return EvaluationResult(
					file=task_path.name,
					status='skipped',
					explanation='BROWSER_USE_API_KEY is unavailable',
					mode=mode,
					reason_code=EvaluationReasonCode.PROVIDER_UNAVAILABLE,
					source_id=task_definition.source_id,
				)
			if not google_api_key:
				return EvaluationResult(
					file=task_path.name,
					status='skipped',
					explanation='GOOGLE_API_KEY is unavailable',
					mode=mode,
					reason_code=EvaluationReasonCode.JUDGE_UNAVAILABLE,
					source_id=task_definition.source_id,
				)
			agent_llm: BaseChatModel = ChatBrowserUse(api_key=browser_use_api_key)
			judge_llm: BaseChatModel = ChatGoogle(model='gemini-3.1-flash-lite', api_key=google_api_key)
		elif mode == EvaluationMode.LOCAL:
			agent_llm = create_local_evaluation_llm(options)
			judge_llm = create_local_evaluation_llm(options)
		else:
			agent_llm = create_subscription_evaluation_llm(options)
			judge_llm = create_subscription_evaluation_llm(options, judge=True)

		logging.getLogger().setLevel(logging.CRITICAL)
		for logger_name in ['browser_use', 'telemetry', 'message_manager']:
			logging.getLogger(logger_name).setLevel(logging.CRITICAL)
		warnings.filterwarnings('ignore')

		profile = build_evaluation_browser_profile(options, Path(profile_directory.name))
		session = BrowserSession(browser_profile=profile)
		await asyncio.wait_for(session.start(), timeout=options.browser_launch_timeout_seconds)
		criteria = '\n- '.join(task_definition.judge_context)
		agent = Agent(
			task=task_definition.task,
			llm=agent_llm,
			browser_session=session,
			use_vision=False if mode == EvaluationMode.SUBSCRIPTION else True,
			judge_llm=judge_llm,
			ground_truth=f'- {criteria}',
			require_live_evidence=True,
			extend_system_message=(
				'After a navigation or click changes the page, verify that the live URL and title are complete before '
				'extracting them. Use the wait action and inspect the next browser state when content is still loading. '
				'Every factual value in the final answer must come from a successful observation or extraction in the current '
				'browser run; never fill missing page content from memory or inference. If extraction reports that requested '
				'content is unavailable, do not call done: scroll, open the matching result or detail view, and retry extraction. '
				'Only report successful completion after the required live-page evidence has been obtained.'
			),
		)
		history: AgentHistoryList = await agent.run(max_steps=task_definition.max_steps)
		agent_output = history.final_result() or ''

		if options.history_dir is not None:
			options.history_dir.mkdir(parents=True, exist_ok=True)
			agent.save_history(options.history_dir / f'{task_path.stem}.json')

		agent_success = history.is_done() and history.is_successful() is True
		judge_validated = history.is_validated() is True
		history_errors = [error for error in history.errors() if error]
		action_results = history.action_results()
		last_result_metadata = action_results[-1].metadata if action_results else None
		live_evidence_gate = (last_result_metadata or {}).get('live_evidence_gate')
		passed = agent_success and judge_validated and not history_errors and bool(agent_output)
		judgement = history.judgement() or {}
		failure_reasons: list[str] = []
		if not agent_success:
			failure_reasons.append('agent did not report successful completion')
		if not agent_output:
			failure_reasons.append('agent produced no final output')
		if not judge_validated:
			failure_reasons.append(str(judgement.get('failure_reason') or 'trace judge did not validate the run'))
		if history_errors:
			failure_reasons.append(f'{len(history_errors)} execution error(s) were recorded')
		explanation = (
			str(judgement.get('reasoning') or 'Agent completion and trace judge validation passed')
			if passed
			else '; '.join(failure_reasons)
		)
		return EvaluationResult(
			file=task_path.name,
			status='passed' if passed else 'failed',
			explanation=explanation,
			mode=mode,
			reason_code=(EvaluationReasonCode.COMPLETED if passed else EvaluationReasonCode.ASSERTION_FAILED),
			source_id=task_definition.source_id,
			duration_ms=round((time.monotonic() - started_at) * 1000),
			output={
				'final_result': agent_output,
				'steps': history.number_of_steps(),
				'agent_provider': agent_llm.provider,
				'agent_model': agent_llm.name,
				'judge_provider': judge_llm.provider,
				'judge_model': judge_llm.name,
				'agent_success': agent_success,
				'judge_validated': judge_validated,
				'judgement': judgement,
				'actions': history.action_names(),
				'urls': history.urls(),
				'error_count': len(history_errors),
				'live_evidence_gate': live_evidence_gate,
			},
		)
	except ValueError as error:
		return EvaluationResult(
			file=task_path.name,
			status='skipped',
			explanation=str(error),
			mode=mode,
			reason_code=EvaluationReasonCode.PROVIDER_UNAVAILABLE,
			duration_ms=round((time.monotonic() - started_at) * 1000),
		)
	except Exception as error:
		return EvaluationResult(
			file=task_path.name,
			status='failed',
			explanation=f'Task failed with error: {type(error).__name__}: {error}',
			mode=mode,
			reason_code=EvaluationReasonCode.AGENT_FAILED,
			duration_ms=round((time.monotonic() - started_at) * 1000),
		)
	finally:
		await close_browser_session(session, options.browser_shutdown_timeout_seconds)
		profile_directory.cleanup()


async def run_single_task(task_path: Path, options: EvaluationRunOptions, mode: EvaluationMode) -> EvaluationResult:
	"""Dispatch one task to the selected evaluation route."""
	browser_error = await browser_runtime_preflight_error(options)
	if browser_error:
		return EvaluationResult(
			file=task_path.name,
			status='skipped',
			explanation=browser_error,
			mode=mode,
			reason_code=EvaluationReasonCode.BROWSER_UNAVAILABLE,
			source_id=_task_source_id(task_path),
		)
	if mode == EvaluationMode.DETERMINISTIC:
		return await run_keyless_task(task_path, _keyless_options(options))
	if mode == EvaluationMode.HYBRID:
		return await run_hybrid_task(task_path, options)
	if mode == EvaluationMode.REPLAY:
		return await run_replay_task(task_path, options)
	return await run_model_task(task_path, options, mode)


async def run_hybrid_task(task_path: Path, options: EvaluationRunOptions) -> EvaluationResult:
	"""Run the verified contract first and escalate only a real failure to an available model route."""
	deterministic_result = await run_keyless_task(task_path, _keyless_options(options))
	deterministic_output = dict(deterministic_result.output)
	if deterministic_result.success:
		deterministic_output['execution_route'] = 'deterministic'
		return deterministic_result.model_copy(update={'mode': EvaluationMode.HYBRID, 'output': deterministic_output})

	fallback_mode = (
		EvaluationMode.LOCAL if options.local_model and options.subscription_provider is None else EvaluationMode.SUBSCRIPTION
	)
	fallback_errors = provider_preflight_errors(options, fallback_mode)
	if not fallback_errors:
		fallback_errors = await provider_connectivity_errors(options, fallback_mode)
	if fallback_errors:
		deterministic_output.update(
			{
				'execution_route': 'deterministic_failed',
				'autonomous_fallback': fallback_mode.value,
				'autonomous_fallback_errors': fallback_errors,
			}
		)
		return deterministic_result.model_copy(
			update={
				'mode': EvaluationMode.HYBRID,
				'explanation': f'{deterministic_result.explanation}; autonomous fallback unavailable: {"; ".join(fallback_errors)}',
				'output': deterministic_output,
			}
		)

	model_result = await run_model_task(task_path, options, fallback_mode)
	model_output = dict(model_result.output)
	model_output.update(
		{
			'execution_route': 'autonomous_fallback',
			'deterministic_failure': deterministic_result.explanation,
			'deterministic_reason_code': deterministic_result.reason_code.value,
		}
	)
	return model_result.model_copy(update={'mode': EvaluationMode.HYBRID, 'output': model_output})


def _subprocess_arguments(task_path: Path, options: EvaluationRunOptions, mode: EvaluationMode) -> list[str]:
	"""Build explicit child arguments so every task receives identical validated settings."""
	arguments = [sys.executable, __file__, '--task', str(task_path), '--mode', mode.value, '--skip-browser-preflight']
	if options.disable_sandbox:
		arguments.append('--disable-sandbox')
	if options.executable_path is not None:
		arguments.extend(['--executable-path', str(options.executable_path)])
	if options.use_cloud_browser:
		arguments.append('--use-cloud-browser')
	if options.cloud_profile_id:
		arguments.extend(['--cloud-profile-id', options.cloud_profile_id])
	if options.cloud_proxy_country_code:
		arguments.extend(['--cloud-proxy-country-code', options.cloud_proxy_country_code])
	if options.cloud_timeout_minutes is not None:
		arguments.extend(['--cloud-timeout-minutes', str(options.cloud_timeout_minutes)])
	arguments.extend(
		[
			'--minimum-page-load-wait-seconds',
			str(options.minimum_page_load_wait_seconds),
			'--network-idle-wait-seconds',
			str(options.network_idle_wait_seconds),
			'--browser-launch-timeout-seconds',
			str(options.browser_launch_timeout_seconds),
			'--browser-shutdown-timeout-seconds',
			str(options.browser_shutdown_timeout_seconds),
			'--required-stable-states',
			str(options.required_stable_states),
			'--state-stability-tolerance',
			str(options.state_stability_tolerance),
			'--state-retry-delay-seconds',
			str(options.state_retry_delay_seconds),
		]
	)
	if options.local_provider != LocalEvaluationProvider.AUTO:
		arguments.extend(['--local-provider', options.local_provider.value])
	if options.local_model:
		arguments.extend(['--local-model', options.local_model])
	if options.local_base_url:
		arguments.extend(['--local-base-url', options.local_base_url])
	if options.subscription_provider is not None:
		arguments.extend(['--subscription-provider', options.subscription_provider.value])
	if options.subscription_model != 'default':
		arguments.extend(['--subscription-model', options.subscription_model])
	if options.subscription_judge_provider is not None:
		arguments.extend(['--subscription-judge-provider', options.subscription_judge_provider.value])
	if options.subscription_judge_model != 'default':
		arguments.extend(['--subscription-judge-model', options.subscription_judge_model])
	arguments.extend(['--subscription-timeout-seconds', str(options.subscription_timeout_seconds)])
	if options.history_dir:
		arguments.extend(['--history-dir', str(options.history_dir)])
	return arguments


def _subprocess_environment(options: EvaluationRunOptions, mode: EvaluationMode) -> dict[str, str]:
	"""Build a child environment that cannot accidentally activate cloud auth in keyless local modes."""
	environment = os.environ.copy()
	environment['PYTHONPATH'] = os.pathsep.join(sys.path)
	if mode in KEYLESS_EVALUATION_MODES and not options.use_cloud_browser:
		environment.pop('BROWSER_USE_API_KEY', None)
	return environment


def _configure_process_environment(options: EvaluationRunOptions, mode: EvaluationMode) -> None:
	"""Remove unused cloud credentials from direct keyless evaluation processes."""
	if mode in KEYLESS_EVALUATION_MODES and not options.use_cloud_browser:
		os.environ.pop('BROWSER_USE_API_KEY', None)


async def run_task_subprocess(
	task_path: Path,
	semaphore: asyncio.Semaphore,
	options: EvaluationRunOptions,
	mode: EvaluationMode,
) -> EvaluationResult:
	"""Run one task in a bounded isolated process and parse its structured result."""
	async with semaphore:
		environment = _subprocess_environment(options, mode)
		for attempt in range(1, options.subprocess_attempts + 1):
			process = await asyncio.create_subprocess_exec(
				*_subprocess_arguments(task_path, options, mode),
				stdout=asyncio.subprocess.PIPE,
				stderr=asyncio.subprocess.PIPE,
				env=environment,
				start_new_session=os.name == 'posix',
			)
			try:
				stdout, stderr = await asyncio.wait_for(
					process.communicate(),
					timeout=options.task_timeout_seconds,
				)
			except TimeoutError:
				await _terminate_subprocess_tree(process)
				print(
					f'[SUBPROCESS {task_path.name}] timed out after {options.task_timeout_seconds:.0f}s '
					f'(attempt {attempt}/{options.subprocess_attempts})'
				)
				if attempt < options.subprocess_attempts:
					continue
				return EvaluationResult(
					file=task_path.name,
					status='failed',
					explanation=(
						f'Subprocess timed out after {options.task_timeout_seconds:.0f}s '
						f'on {options.subprocess_attempts} attempts'
					),
					mode=mode,
					reason_code=EvaluationReasonCode.AGENT_FAILED,
					attempts=attempt,
				)

			stdout_text = stdout.decode(errors='replace').strip()
			stderr_text = stderr.decode(errors='replace').strip()
			if stderr_text:
				print(f'[SUBPROCESS {task_path.name}] {stderr_text[:1000]}')
			if process.returncode != 0:
				return EvaluationResult(
					file=task_path.name,
					status='failed',
					explanation=f'Subprocess failed (code {process.returncode}): {stderr_text[:300]}',
					mode=mode,
					reason_code=EvaluationReasonCode.AGENT_FAILED,
					attempts=attempt,
				)
			for line in reversed(stdout_text.splitlines()):
				candidate = line.strip()
				if candidate.startswith('{') and candidate.endswith('}'):
					try:
						result = EvaluationResult.model_validate_json(candidate)
						return result.model_copy(update={'attempts': max(result.attempts, attempt)})
					except ValueError:
						continue
			return EvaluationResult(
				file=task_path.name,
				status='failed',
				explanation=f'No EvaluationResult JSON found in subprocess output: {stdout_text[:300]}',
				mode=mode,
				reason_code=EvaluationReasonCode.AGENT_FAILED,
				attempts=attempt,
			)

		raise AssertionError('subprocess attempt loop completed without a result')


async def _terminate_subprocess_tree(process: asyncio.subprocess.Process) -> None:
	"""Stop a timed-out evaluator and the Chromium process group it owns."""
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


def discover_task_paths(options: EvaluationRunOptions) -> list[Path]:
	"""Return either the requested task or every YAML task in stable order."""
	if options.task_path is not None:
		return [options.task_path]
	return [Path(path) for path in sorted(glob.glob(str(options.task_dir / '*.yaml')))]


def _task_source_id(task_path: Path) -> str | None:
	"""Return a task source ID without allowing one invalid task to break a preflight summary."""
	try:
		return load_evaluation_task(task_path).source_id
	except Exception:
		return None


def summarize_results(mode: EvaluationMode, results: list[EvaluationResult]) -> EvaluationSummary:
	"""Build aggregate counts without treating unavailable providers as successes."""
	return EvaluationSummary(
		mode=mode,
		passed=sum(result.status == 'passed' for result in results),
		failed=sum(result.status == 'failed' for result in results),
		skipped=sum(result.status == 'skipped' for result in results),
		total=len(results),
		results=results,
	)


async def run_all_tasks(options: EvaluationRunOptions, mode: EvaluationMode) -> EvaluationSummary:
	"""Run every selected task with bounded subprocess concurrency."""
	task_paths = discover_task_paths(options)
	if not task_paths:
		return EvaluationSummary(mode=mode, passed=0, failed=0, skipped=0, total=0)
	browser_error = await browser_runtime_preflight_error(options)
	if browser_error:
		results = [
			EvaluationResult(
				file=task_path.name,
				status='skipped',
				explanation=browser_error,
				mode=mode,
				reason_code=EvaluationReasonCode.BROWSER_UNAVAILABLE,
				source_id=_task_source_id(task_path),
			)
			for task_path in task_paths
		]
		return summarize_results(mode, results)
	preflight_errors = provider_preflight_errors(options, mode)
	if not preflight_errors:
		preflight_errors = await provider_connectivity_errors(options, mode)
	if preflight_errors:
		results = []
		for task_path in task_paths:
			results.append(
				EvaluationResult(
					file=task_path.name,
					status='skipped',
					explanation='; '.join(preflight_errors),
					mode=mode,
					reason_code=EvaluationReasonCode.PROVIDER_UNAVAILABLE,
					source_id=_task_source_id(task_path),
				)
			)
		return summarize_results(mode, results)

	semaphore = asyncio.Semaphore(options.max_parallel)
	results = await asyncio.gather(*(run_task_subprocess(task_path, semaphore, options, mode) for task_path in task_paths))
	return summarize_results(mode, list(results))


def print_summary(summary: EvaluationSummary) -> None:
	"""Print human and GitHub Actions compatible evaluation output."""
	print(f'\nEvaluation mode: {summary.mode.value}')
	print('Task                              | Status  | Reason code            | Reason')
	print('----------------------------------+---------+------------------------+------------------------------')
	icons = {'passed': 'PASS', 'failed': 'FAIL', 'skipped': 'SKIP'}
	for result in summary.results:
		print(f'{result.file:33} | {icons[result.status]:7} | {result.reason_code.value:22} | {result.explanation[:120]}')
	print(
		f'\n{summary.passed}/{summary.executed} executed tasks passed '
		f'({summary.skipped} skipped, {summary.total} total, pass rate {summary.pass_rate:.1%})'
	)
	print(f'PASSED={summary.passed}')
	print(f'FAILED={summary.failed}')
	print(f'SKIPPED={summary.skipped}')
	print(f'EXECUTED={summary.executed}')
	print(f'PASS_RATE={summary.pass_rate:.6f}')
	print(f'TOTAL={summary.total}')
	detailed_results = [
		{
			'task': result.file.removesuffix('.yaml'),
			'status': result.status,
			'success': result.success,
			'reason': result.explanation,
			'reason_code': result.reason_code.value,
			'mode': result.mode.value,
			'duration_ms': result.duration_ms,
		}
		for result in summary.results
	]
	print('DETAILED_RESULTS=' + json.dumps(detailed_results, ensure_ascii=False))


def persist_evaluation_evidence(output_path: Path | None, payload: BaseModel) -> None:
	"""Persist either one task result or an aggregate summary with identical JSON formatting."""
	if output_path is None:
		return
	output_path.parent.mkdir(parents=True, exist_ok=True)
	output_path.write_text(payload.model_dump_json(indent=2) + '\n', encoding='utf-8')


def parse_options() -> EvaluationRunOptions:
	"""Parse CLI inputs and merge explicit local/subscription environment defaults."""
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument('task_dir', nargs='?', type=Path, default=DEFAULT_TASK_DIR)
	parser.add_argument('--task', type=Path, dest='task_path')
	parser.add_argument('--mode', choices=[mode.value for mode in EvaluationMode], default=EvaluationMode.AUTO.value)
	parser.add_argument('--output', type=Path, dest='output_path')
	parser.add_argument('--history-dir', type=Path)
	parser.add_argument('--max-parallel', type=int, default=int(os.getenv('EVALUATION_MAX_PARALLEL', '1')))
	parser.add_argument('--disable-sandbox', action='store_true')
	parser.add_argument('--executable-path', type=Path)
	parser.add_argument('--use-cloud-browser', action='store_true')
	parser.add_argument('--cloud-profile-id')
	parser.add_argument('--cloud-proxy-country-code')
	parser.add_argument('--cloud-timeout-minutes', type=int)
	parser.add_argument('--skip-browser-preflight', action='store_true')
	parser.add_argument('--minimum-page-load-wait-seconds', type=float, default=1.0)
	parser.add_argument('--network-idle-wait-seconds', type=float, default=1.0)
	parser.add_argument('--browser-launch-timeout-seconds', type=float, default=30.0)
	parser.add_argument('--browser-shutdown-timeout-seconds', type=float, default=15.0)
	parser.add_argument('--required-stable-states', type=int, default=2)
	parser.add_argument('--state-stability-tolerance', type=float, default=0.2)
	parser.add_argument('--state-retry-delay-seconds', type=float, default=1.0)
	parser.add_argument(
		'--local-provider',
		choices=[provider.value for provider in LocalEvaluationProvider],
		default=os.getenv('KEYLESS_LLM_PROVIDER', LocalEvaluationProvider.AUTO.value),
	)
	parser.add_argument('--local-model', default=os.getenv('KEYLESS_LLM_MODEL'))
	parser.add_argument('--local-base-url', default=os.getenv('KEYLESS_LLM_BASE_URL'))
	parser.add_argument(
		'--subscription-provider',
		choices=[provider.value for provider in SubscriptionCLIProvider],
		default=os.getenv('SUBSCRIPTION_LLM_PROVIDER'),
	)
	parser.add_argument('--subscription-model', default=os.getenv('SUBSCRIPTION_LLM_MODEL', 'default'))
	parser.add_argument(
		'--subscription-judge-provider',
		choices=[provider.value for provider in SubscriptionCLIProvider],
		default=os.getenv('SUBSCRIPTION_JUDGE_PROVIDER'),
	)
	parser.add_argument('--subscription-judge-model', default=os.getenv('SUBSCRIPTION_JUDGE_MODEL', 'default'))
	parser.add_argument('--subscription-timeout-seconds', type=float, default=180.0)
	parser.add_argument('--preflight-timeout-seconds', type=float, default=20.0)
	parser.add_argument('--task-timeout-seconds', type=float, default=150.0)
	parser.add_argument('--subprocess-attempts', type=int, default=2)
	parser.add_argument('--minimum-pass-rate', type=float)
	parser.add_argument('--minimum-executed-tasks', type=int)
	arguments = parser.parse_args()
	return EvaluationRunOptions(
		mode=EvaluationMode(arguments.mode),
		task_dir=arguments.task_dir,
		task_path=arguments.task_path,
		output_path=arguments.output_path,
		history_dir=arguments.history_dir,
		max_parallel=arguments.max_parallel,
		disable_sandbox=arguments.disable_sandbox,
		executable_path=arguments.executable_path,
		use_cloud_browser=arguments.use_cloud_browser,
		cloud_profile_id=arguments.cloud_profile_id,
		cloud_proxy_country_code=arguments.cloud_proxy_country_code,
		cloud_timeout_minutes=arguments.cloud_timeout_minutes,
		browser_preflight=not arguments.skip_browser_preflight,
		minimum_page_load_wait_seconds=arguments.minimum_page_load_wait_seconds,
		network_idle_wait_seconds=arguments.network_idle_wait_seconds,
		browser_launch_timeout_seconds=arguments.browser_launch_timeout_seconds,
		browser_shutdown_timeout_seconds=arguments.browser_shutdown_timeout_seconds,
		required_stable_states=arguments.required_stable_states,
		state_stability_tolerance=arguments.state_stability_tolerance,
		state_retry_delay_seconds=arguments.state_retry_delay_seconds,
		local_provider=LocalEvaluationProvider(arguments.local_provider),
		local_model=arguments.local_model,
		local_base_url=arguments.local_base_url,
		subscription_provider=(
			SubscriptionCLIProvider(arguments.subscription_provider) if arguments.subscription_provider else None
		),
		subscription_model=arguments.subscription_model,
		subscription_judge_provider=(
			SubscriptionCLIProvider(arguments.subscription_judge_provider) if arguments.subscription_judge_provider else None
		),
		subscription_judge_model=arguments.subscription_judge_model,
		subscription_timeout_seconds=arguments.subscription_timeout_seconds,
		preflight_timeout_seconds=arguments.preflight_timeout_seconds,
		task_timeout_seconds=arguments.task_timeout_seconds,
		subprocess_attempts=arguments.subprocess_attempts,
		minimum_pass_rate=arguments.minimum_pass_rate,
		minimum_executed_tasks=arguments.minimum_executed_tasks,
	)


def quality_gate_thresholds(options: EvaluationRunOptions, summary: EvaluationSummary) -> tuple[float, int]:
	"""Use strict keyless gates while preserving gradual model-quality thresholds."""
	strict_modes = {EvaluationMode.HYBRID, EvaluationMode.DETERMINISTIC, EvaluationMode.REPLAY}
	default_pass_rate = 1.0 if summary.mode in strict_modes else DEFAULT_MIN_PASS_RATE
	default_executed = summary.total if summary.mode in strict_modes else DEFAULT_MIN_EXECUTED_TASKS
	pass_rate = options.minimum_pass_rate
	if pass_rate is None:
		pass_rate = float(os.getenv('EVALUATION_MIN_PASS_RATE', str(default_pass_rate)))
	executed = options.minimum_executed_tasks
	if executed is None:
		executed = int(os.getenv('EVALUATION_MIN_EXECUTED_TASKS', str(default_executed)))
	return pass_rate, executed


def main() -> int:
	"""Run selected evaluations, persist evidence, and enforce mode-specific quality gates."""
	options = parse_options()
	mode = resolve_evaluation_mode(options)
	_configure_process_environment(options, mode)
	if options.task_path is not None:
		result = asyncio.run(run_single_task(options.task_path, options, mode))
		persist_evaluation_evidence(options.output_path, result)
		print(result.model_dump_json(), flush=True)
		return 0 if result.success else 1

	summary = asyncio.run(run_all_tasks(options, mode))
	print_summary(summary)
	persist_evaluation_evidence(options.output_path, summary)

	minimum_pass_rate, minimum_executed_tasks = quality_gate_thresholds(options, summary)
	gate_errors = summary.quality_gate_errors(
		minimum_pass_rate=minimum_pass_rate,
		minimum_executed_tasks=minimum_executed_tasks,
	)
	if gate_errors:
		print('\nEVALUATION QUALITY GATE FAILED:')
		for error in gate_errors:
			print(f'- {error}')
		return 1
	return 0


if __name__ == '__main__':
	raise SystemExit(main())
