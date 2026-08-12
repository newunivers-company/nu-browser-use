"""Tests for API-key-free deterministic, replay, and local evaluation routing."""

import asyncio
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError
from pytest_httpserver import HTTPServer

from browser_use.llm.subscription_cli import (
	ChatSubscriptionCLI,
	SubscriptionCLIProvider,
	SubscriptionCLIStatus,
)
from tests.ci.evaluate_tasks import (
	EvaluationRunOptions,
	_terminate_subprocess_tree,
	create_subscription_evaluation_llm,
	provider_connectivity_errors,
	provider_preflight_errors,
	resolve_evaluation_mode,
	run_replay_task,
)
from tests.ci.evaluation_models import (
	EvaluationMode,
	EvaluationReasonCode,
	EvaluationResult,
	KeylessSnapshot,
	LocalEvaluationProvider,
)
from tests.ci.keyless_evaluation import KeylessRunnerOptions, load_evaluation_task, run_keyless_task


def write_task(path: Path, start_url: str) -> None:
	"""Write one deterministic test task against the local HTTP fixture."""
	payload = {
		'name': 'Keyless local interaction',
		'source_id': 'keyless_local',
		'task': 'Enter a query and read the first result.',
		'judge_context': ['The result must contain the submitted query.'],
		'max_steps': 5,
		'keyless': {
			'start_url': start_url,
			'expected_domains': ['localhost'],
			'actions': [
				{'action': 'fill', 'selectors': ['#query'], 'value': 'browser use', 'wait_after_seconds': 0},
				{'action': 'click', 'selectors': ['#submit'], 'wait_after_seconds': 0},
			],
			'collections': [
				{
					'name': 'results',
					'root_selectors': ['.result'],
					'min_items': 1,
					'limit': 1,
					'fields': [{'name': 'title', 'selectors': []}],
				}
			],
			'required_text_patterns': ['browser use'],
			'min_selector_count': 1,
			'wait_after_navigation_seconds': 0,
		},
	}
	path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding='utf-8')


async def test_keyless_runner_executes_actions_and_extracts_structured_evidence(
	tmp_path: Path,
	httpserver: HTTPServer,
) -> None:
	"""A real Chromium run must validate interactions without an LLM or remote API key."""
	httpserver.expect_request('/').respond_with_data(
		"""
<!doctype html><html><head><title>Keyless fixture</title></head><body>
<input id="query"><button id="submit" onclick="document.querySelector('.result').textContent = document.getElementById('query').value">Go</button>
<article class="result">waiting</article>
</body></html>
""",
		content_type='text/html',
	)
	task_path = tmp_path / 'keyless.yaml'
	write_task(task_path, httpserver.url_for('/'))

	result = await run_keyless_task(
		task_path,
		KeylessRunnerOptions(disable_sandbox=True, attempts=1, retry_delay_seconds=0),
	)

	assert result.status == 'passed', result.model_dump()
	assert result.reason_code == EvaluationReasonCode.COMPLETED
	assert result.output['results'] == [{'title': 'browser use'}]
	assert [entry['action'] for entry in result.trace] == ['navigate', 'fill', 'click']


def test_snapshot_hash_rejects_unreviewed_evidence_change() -> None:
	"""Checked-in fallback values cannot change without an explicit hash update."""
	with pytest.raises(ValidationError, match='snapshot sha256'):
		KeylessSnapshot(
			reviewed_at=date(2026, 8, 12),
			source_url='https://example.com',
			output={'rule': 'changed'},
			sha256='0' * 64,
		)


def test_auto_mode_prefers_cloud_then_local_then_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Auto routing uses configured providers but never invents or replaces a model name."""
	monkeypatch.delenv('BROWSER_USE_API_KEY', raising=False)
	monkeypatch.delenv('GOOGLE_API_KEY', raising=False)
	assert resolve_evaluation_mode(EvaluationRunOptions()) == EvaluationMode.DETERMINISTIC
	assert resolve_evaluation_mode(EvaluationRunOptions(local_model='user-model')) == EvaluationMode.LOCAL

	monkeypatch.setenv('BROWSER_USE_API_KEY', 'browser-key')
	monkeypatch.setenv('GOOGLE_API_KEY', 'judge-key')
	assert resolve_evaluation_mode(EvaluationRunOptions(local_model='user-model')) == EvaluationMode.CLOUD

	options = EvaluationRunOptions(
		local_model='user-model',
		subscription_provider=SubscriptionCLIProvider.CLAUDE,
	)
	assert resolve_evaluation_mode(options) == EvaluationMode.SUBSCRIPTION


def test_subscription_route_preserves_agent_and_judge_provider_models() -> None:
	"""Agent and independent judge routes retain every explicit subscription selection."""
	options = EvaluationRunOptions(
		mode=EvaluationMode.SUBSCRIPTION,
		subscription_provider=SubscriptionCLIProvider.CODEX,
		subscription_model='codex-future-model',
		subscription_judge_provider=SubscriptionCLIProvider.CLAUDE,
		subscription_judge_model='claude-future-model',
	)
	agent_llm = create_subscription_evaluation_llm(options)
	judge_llm = create_subscription_evaluation_llm(options, judge=True)

	assert isinstance(agent_llm, ChatSubscriptionCLI)
	assert (agent_llm.provider, agent_llm.name) == ('subscription-codex', 'codex-future-model')
	assert (judge_llm.provider, judge_llm.name) == ('subscription-claude', 'claude-future-model')


def test_provider_preflight_reports_all_missing_cloud_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Cloud mode reports both credentials once before launching task subprocesses."""
	monkeypatch.delenv('BROWSER_USE_API_KEY', raising=False)
	monkeypatch.delenv('GOOGLE_API_KEY', raising=False)
	errors = provider_preflight_errors(EvaluationRunOptions(mode=EvaluationMode.CLOUD), EvaluationMode.CLOUD)
	assert errors == ['BROWSER_USE_API_KEY is unavailable', 'GOOGLE_API_KEY is unavailable']


async def test_local_connectivity_is_probed_once_before_browser_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
	"""An unreachable configured local model must fail preflight without launching 17 browsers."""

	class UnavailableLocalModel:
		async def ainvoke(self, *_args: object, **_kwargs: object) -> None:
			raise ConnectionError('local endpoint refused the connection')

	monkeypatch.setattr('tests.ci.evaluate_tasks.create_local_evaluation_llm', lambda _options: UnavailableLocalModel())
	errors = await provider_connectivity_errors(
		EvaluationRunOptions(mode=EvaluationMode.LOCAL, local_model='configured-model'),
		EvaluationMode.LOCAL,
	)
	assert len(errors) == 1
	assert 'ConnectionError' in errors[0]


async def test_identical_subscription_agent_and_judge_routes_are_probed_once(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""One subscription/model pair should consume one preflight call even when it fills both roles."""
	inspect_calls = 0
	inference_calls = 0

	async def fake_inspect(provider: SubscriptionCLIProvider, **_kwargs: object) -> SubscriptionCLIStatus:
		nonlocal inspect_calls
		inspect_calls += 1
		return SubscriptionCLIStatus(
			provider=provider,
			executable=f'/usr/bin/{provider.value}',
			installed=True,
			authenticated=True,
			auth_method='subscription',
			reason='available',
		)

	class AvailableSubscriptionModel:
		async def ainvoke(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
			nonlocal inference_calls
			inference_calls += 1
			return SimpleNamespace(completion=SimpleNamespace(status='ok'))

	monkeypatch.setattr('tests.ci.evaluate_tasks.inspect_subscription_cli', fake_inspect)
	monkeypatch.setattr('tests.ci.evaluate_tasks.ChatSubscriptionCLI', lambda **_kwargs: AvailableSubscriptionModel())
	options = EvaluationRunOptions(
		mode=EvaluationMode.SUBSCRIPTION,
		subscription_provider=SubscriptionCLIProvider.CODEX,
		subscription_judge_provider=SubscriptionCLIProvider.CODEX,
	)

	errors = await provider_connectivity_errors(options, EvaluationMode.SUBSCRIPTION)

	assert errors == []
	assert inspect_calls == 1
	assert inference_calls == 1


async def test_replay_mode_reuses_validated_declarative_trace(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Replay routing must preserve evidence while identifying the replay coverage tier."""
	task_path = tmp_path / 'task.yaml'
	write_task(task_path, 'https://localhost/')
	original = EvaluationResult(
		file=task_path.name,
		status='passed',
		explanation='contract passed',
		mode=EvaluationMode.DETERMINISTIC,
		reason_code=EvaluationReasonCode.COMPLETED,
	)

	async def fake_run_keyless_task(*_args: object, **_kwargs: object) -> EvaluationResult:
		return original

	monkeypatch.setattr('tests.ci.evaluate_tasks.run_keyless_task', fake_run_keyless_task)
	result = await run_replay_task(task_path, EvaluationRunOptions())
	assert result.mode == EvaluationMode.REPLAY
	assert result.explanation == 'Declarative contract replay: contract passed'


async def test_replay_mode_prefers_checked_in_native_agent_history(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""A configured history must route through Agent.rerun_history instead of the contract fallback."""
	task_path = tmp_path / 'task.yaml'
	write_task(task_path, 'https://localhost/')
	payload = yaml.safe_load(task_path.read_text(encoding='utf-8'))
	payload['replay_history_path'] = 'history.json'
	task_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding='utf-8')
	history_path = tmp_path / 'history.json'
	history_path.write_text('{}', encoding='utf-8')
	expected = EvaluationResult(
		file=task_path.name,
		status='passed',
		explanation='native replay passed',
		mode=EvaluationMode.REPLAY,
		reason_code=EvaluationReasonCode.COMPLETED,
	)

	async def fake_saved_replay(
		received_task_path: Path,
		received_history_path: Path,
		_options: EvaluationRunOptions,
	) -> EvaluationResult:
		assert received_task_path == task_path
		assert received_history_path == history_path
		return expected

	monkeypatch.setattr('tests.ci.evaluate_tasks._run_saved_history_replay', fake_saved_replay)
	result = await run_replay_task(task_path, EvaluationRunOptions())
	assert result == expected


async def test_replay_history_cannot_escape_task_directory(tmp_path: Path) -> None:
	"""Task YAML cannot use replay history to read arbitrary files outside its directory."""
	task_directory = tmp_path / 'tasks'
	task_directory.mkdir()
	task_path = task_directory / 'task.yaml'
	write_task(task_path, 'https://localhost/')
	payload = yaml.safe_load(task_path.read_text(encoding='utf-8'))
	payload['replay_history_path'] = '../outside.json'
	task_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding='utf-8')
	(tmp_path / 'outside.json').write_text('{}', encoding='utf-8')

	result = await run_replay_task(task_path, EvaluationRunOptions())
	assert result.status == 'failed'
	assert result.reason_code == EvaluationReasonCode.INVALID_TASK


def test_local_provider_configuration_preserves_explicit_model_name() -> None:
	"""Local configuration retains new or unknown user model identifiers verbatim."""
	options = EvaluationRunOptions(
		mode=EvaluationMode.LOCAL,
		local_provider=LocalEvaluationProvider.OPENAI_LIKE,
		local_model='future-browser-model',
		local_base_url='http://127.0.0.1:8000/v1',
	)
	assert options.local_model == 'future-browser-model'
	assert load_evaluation_task(Path('tests/agent_tasks/quotes_to_scrape.yaml')).source_id == 'quotes_to_scrape'


async def test_timed_out_evaluator_process_tree_is_terminated() -> None:
	"""A hung evaluator must not leave its isolated process group running."""
	process = await asyncio.create_subprocess_exec(
		sys.executable,
		'-c',
		'import time; time.sleep(30)',
		start_new_session=True,
	)
	await _terminate_subprocess_tree(process)
	assert process.returncode is not None
