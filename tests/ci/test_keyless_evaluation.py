"""Tests for API-key-free deterministic, replay, and local evaluation routing."""

import asyncio
import json
import os
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
	_configure_process_environment,
	_keyless_options,
	_subprocess_arguments,
	_subprocess_environment,
	_terminate_subprocess_tree,
	browser_runtime_preflight_error,
	create_subscription_evaluation_llm,
	persist_evaluation_evidence,
	provider_connectivity_errors,
	provider_preflight_errors,
	resolve_evaluation_mode,
	run_hybrid_task,
	run_model_task,
	run_replay_task,
)
from tests.ci.evaluation_models import (
	EvaluationMode,
	EvaluationReasonCode,
	EvaluationResult,
	KeylessSnapshot,
	LocalEvaluationProvider,
)
from tests.ci.keyless_evaluation import (
	KeylessRunnerOptions,
	_capture_state,
	build_evaluation_browser_profile,
	load_evaluation_task,
	run_keyless_task,
)


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


def test_evaluation_browser_runtime_supports_explicit_local_and_cloud_profiles(tmp_path: Path) -> None:
	"""Every evaluation mode can select a sandboxed system browser or Browser Use Cloud."""
	local_options = EvaluationRunOptions(executable_path=Path('/usr/bin/google-chrome'))
	local_profile = build_evaluation_browser_profile(local_options, tmp_path / 'profile')
	assert local_profile.executable_path == Path('/usr/bin/google-chrome')
	assert local_profile.chromium_sandbox is True

	cloud_options = EvaluationRunOptions(
		use_cloud_browser=True,
		cloud_profile_id='profile-id',
		cloud_proxy_country_code='us',
		cloud_timeout_minutes=15,
	)
	cloud_profile = build_evaluation_browser_profile(cloud_options, None)
	assert cloud_profile.use_cloud is True
	assert cloud_profile.cloud_browser_params is not None
	assert cloud_profile.cloud_browser_params.profile_id == 'profile-id'

	with pytest.raises(ValidationError, match='cannot be combined'):
		EvaluationRunOptions(use_cloud_browser=True, executable_path=Path('/usr/bin/google-chrome'))


def test_browser_runtime_options_are_forwarded_to_keyless_subprocesses() -> None:
	"""Parent preflight and child task processes must use the same validated browser settings."""
	options = EvaluationRunOptions(
		executable_path=Path('/usr/bin/google-chrome'),
		minimum_page_load_wait_seconds=1.5,
		network_idle_wait_seconds=2.0,
		required_stable_states=3,
	)
	keyless_options = _keyless_options(options)
	arguments = _subprocess_arguments(Path('task.yaml'), options, EvaluationMode.DETERMINISTIC)

	assert keyless_options.executable_path == options.executable_path
	assert keyless_options.required_stable_states == 3
	assert arguments[arguments.index('--executable-path') + 1] == '/usr/bin/google-chrome'
	assert arguments[arguments.index('--required-stable-states') + 1] == '3'
	assert '--skip-browser-preflight' in arguments


async def test_failed_browser_start_is_cleaned_before_keyless_result(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""A launch failure must not leave the evaluator event loop or browser session alive."""
	task_path = tmp_path / 'task.yaml'
	write_task(task_path, 'https://localhost/')

	class FailingSession:
		def __init__(self, **_kwargs: object) -> None:
			self.killed = False
			created_sessions.append(self)

		async def start(self) -> None:
			raise RuntimeError('launch failed')

		async def kill(self) -> None:
			self.killed = True

		async def reset(self) -> None:
			self.killed = True

	created_sessions: list[FailingSession] = []
	monkeypatch.setattr('tests.ci.keyless_evaluation.BrowserSession', FailingSession)
	result = await run_keyless_task(task_path, KeylessRunnerOptions(attempts=1))

	assert result.status == 'skipped'
	assert result.reason_code == EvaluationReasonCode.BROWSER_UNAVAILABLE
	assert created_sessions and created_sessions[0].killed is True


async def test_browser_preflight_rejects_missing_executable_before_launch(tmp_path: Path) -> None:
	"""Invalid explicit executables fail once before any task subprocess is scheduled."""
	error = await browser_runtime_preflight_error(EvaluationRunOptions(executable_path=tmp_path / 'missing-chrome'))
	assert error is not None and 'missing or not executable' in error


async def test_final_capture_waits_for_consecutive_url_title_and_dom_stability(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Extraction must use the settled page rather than the first transitional browser state."""
	task_path = tmp_path / 'task.yaml'
	write_task(task_path, 'https://localhost/')
	task = load_evaluation_task(task_path)
	state_values = [
		{'url': 'https://localhost/loading', 'title': 'Loading', 'selectors': 1, 'body': 'x'},
		{'url': 'https://localhost/result', 'title': 'Ready', 'selectors': 3, 'body': 'browser use'},
		{'url': 'https://localhost/result', 'title': 'Ready', 'selectors': 3, 'body': 'browser use'},
	]

	class FakeSession:
		def __init__(self) -> None:
			self.capture_index = -1

		async def get_browser_state_summary(self, **_kwargs: object) -> SimpleNamespace:
			self.capture_index += 1
			value = state_values[self.capture_index]
			return SimpleNamespace(
				url=value['url'],
				title=value['title'],
				dom_state=SimpleNamespace(selector_map=dict.fromkeys(range(int(value['selectors'])))),
			)

	session = FakeSession()

	async def fake_evaluate(received_session: FakeSession, expression: str) -> object:
		value = state_values[received_session.capture_index]
		if expression == 'location.href':
			return value['url']
		if expression == 'document.title':
			return value['title']
		return value['body']

	monkeypatch.setattr('tests.ci.keyless_evaluation._evaluate_value', fake_evaluate)
	url, title, selector_count, body_text = await _capture_state(
		session,  # type: ignore[arg-type]
		task,
		KeylessRunnerOptions(attempts=1, state_retry_delay_seconds=0),
	)

	assert session.capture_index == 2
	assert (url, title, selector_count, body_text) == ('https://localhost/result', 'Ready', 3, 'browser use')


def test_snapshot_hash_rejects_unreviewed_evidence_change() -> None:
	"""Checked-in fallback values cannot change without an explicit hash update."""
	with pytest.raises(ValidationError, match='snapshot sha256'):
		KeylessSnapshot(
			reviewed_at=date(2026, 8, 12),
			source_url='https://example.com',
			output={'rule': 'changed'},
			sha256='0' * 64,
		)


def test_auto_mode_selects_deterministic_first_hybrid_routing(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Auto routing never activates a remote API merely because a credential exists in the shell."""
	monkeypatch.delenv('BROWSER_USE_API_KEY', raising=False)
	monkeypatch.delenv('GOOGLE_API_KEY', raising=False)
	assert resolve_evaluation_mode(EvaluationRunOptions()) == EvaluationMode.HYBRID
	assert resolve_evaluation_mode(EvaluationRunOptions(local_model='user-model')) == EvaluationMode.HYBRID

	monkeypatch.setenv('BROWSER_USE_API_KEY', 'browser-key')
	monkeypatch.setenv('GOOGLE_API_KEY', 'judge-key')
	assert resolve_evaluation_mode(EvaluationRunOptions(local_model='user-model')) == EvaluationMode.HYBRID

	options = EvaluationRunOptions(
		local_model='user-model',
		subscription_provider=SubscriptionCLIProvider.CLAUDE,
	)
	assert resolve_evaluation_mode(options) == EvaluationMode.HYBRID
	assert resolve_evaluation_mode(EvaluationRunOptions(mode=EvaluationMode.SUBSCRIPTION)) == EvaluationMode.SUBSCRIPTION


async def test_hybrid_route_stops_after_successful_deterministic_contract(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""A verified contract avoids all model preflight and inference work."""
	task_path = tmp_path / 'task.yaml'
	write_task(task_path, 'https://localhost/')
	deterministic = EvaluationResult(
		file=task_path.name,
		status='passed',
		explanation='contract passed',
		mode=EvaluationMode.DETERMINISTIC,
	)

	async def fake_keyless(*_args: object, **_kwargs: object) -> EvaluationResult:
		return deterministic

	monkeypatch.setattr('tests.ci.evaluate_tasks.run_keyless_task', fake_keyless)
	result = await run_hybrid_task(task_path, EvaluationRunOptions())

	assert result.status == 'passed'
	assert result.mode == EvaluationMode.HYBRID
	assert result.output['execution_route'] == 'deterministic'


async def test_hybrid_route_escalates_only_failed_contract(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""A deterministic failure retains its evidence while an available subscription model retries autonomously."""
	task_path = tmp_path / 'task.yaml'
	write_task(task_path, 'https://localhost/')
	deterministic = EvaluationResult(
		file=task_path.name,
		status='failed',
		explanation='selector contract changed',
		mode=EvaluationMode.DETERMINISTIC,
		reason_code=EvaluationReasonCode.ASSERTION_FAILED,
	)
	autonomous = EvaluationResult(
		file=task_path.name,
		status='passed',
		explanation='autonomous recovery passed',
		mode=EvaluationMode.SUBSCRIPTION,
	)

	async def fake_keyless(*_args: object, **_kwargs: object) -> EvaluationResult:
		return deterministic

	async def fake_connectivity(*_args: object, **_kwargs: object) -> list[str]:
		return []

	async def fake_model(*_args: object, **_kwargs: object) -> EvaluationResult:
		return autonomous

	monkeypatch.setattr('tests.ci.evaluate_tasks.run_keyless_task', fake_keyless)
	monkeypatch.setattr('tests.ci.evaluate_tasks.provider_connectivity_errors', fake_connectivity)
	monkeypatch.setattr('tests.ci.evaluate_tasks.run_model_task', fake_model)
	result = await run_hybrid_task(
		task_path,
		EvaluationRunOptions(subscription_provider=SubscriptionCLIProvider.CODEX),
	)

	assert result.status == 'passed'
	assert result.mode == EvaluationMode.HYBRID
	assert result.output['execution_route'] == 'autonomous_fallback'
	assert result.output['deterministic_failure'] == 'selector contract changed'


def test_single_task_json_evidence_is_persisted(tmp_path: Path) -> None:
	"""The --task and aggregate code paths share the same explicit output writer."""
	output_path = tmp_path / 'single-result.json'
	result = EvaluationResult(
		file='task.yaml',
		status='passed',
		explanation='completed',
		mode=EvaluationMode.HYBRID,
	)

	persist_evaluation_evidence(output_path, result)

	assert json.loads(output_path.read_text(encoding='utf-8'))['file'] == 'task.yaml'


def test_keyless_local_subprocess_environment_removes_browser_use_key(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Keyless local modes must not inherit a cloud credential from the parent shell."""
	monkeypatch.setenv('BROWSER_USE_API_KEY', 'must-not-reach-child')
	options = EvaluationRunOptions(executable_path=Path('/usr/bin/google-chrome'))

	for mode in (
		EvaluationMode.DETERMINISTIC,
		EvaluationMode.REPLAY,
		EvaluationMode.LOCAL,
		EvaluationMode.SUBSCRIPTION,
	):
		assert 'BROWSER_USE_API_KEY' not in _subprocess_environment(options, mode)


def test_direct_keyless_process_environment_removes_browser_use_key(monkeypatch: pytest.MonkeyPatch) -> None:
	"""A direct single-task run must remove a dotenv- or shell-provided cloud credential too."""
	monkeypatch.setenv('BROWSER_USE_API_KEY', 'must-not-remain-in-process')

	_configure_process_environment(EvaluationRunOptions(), EvaluationMode.DETERMINISTIC)

	assert 'BROWSER_USE_API_KEY' not in os.environ


def test_cloud_browser_subprocess_environment_retains_required_key(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Explicit cloud browser and cloud model routes retain their required Browser Use credential."""
	monkeypatch.setenv('BROWSER_USE_API_KEY', 'browser-key')
	_configure_process_environment(EvaluationRunOptions(), EvaluationMode.CLOUD)

	cloud_model_environment = _subprocess_environment(EvaluationRunOptions(), EvaluationMode.CLOUD)
	cloud_browser_environment = _subprocess_environment(
		EvaluationRunOptions(use_cloud_browser=True),
		EvaluationMode.SUBSCRIPTION,
	)

	assert cloud_model_environment['BROWSER_USE_API_KEY'] == 'browser-key'
	assert cloud_browser_environment['BROWSER_USE_API_KEY'] == 'browser-key'
	assert os.environ['BROWSER_USE_API_KEY'] == 'browser-key'


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


async def test_model_evaluation_requires_trace_judge_validation(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Agent self-reported success cannot pass when the trace judge rejects its evidence."""
	task_path = tmp_path / 'task.yaml'
	write_task(task_path, 'https://localhost/')

	class FakeModel:
		provider = 'subscription-test'
		name = 'test-model'

	class FakeSession:
		def __init__(self, **_kwargs: object) -> None:
			pass

		async def start(self) -> None:
			pass

		async def kill(self) -> None:
			pass

		async def reset(self) -> None:
			pass

	class RejectedHistory:
		def final_result(self) -> str:
			return 'browser use'

		def number_of_steps(self) -> int:
			return 3

		def is_done(self) -> bool:
			return True

		def is_successful(self) -> bool:
			return True

		def is_validated(self) -> bool:
			return False

		def judgement(self) -> dict[str, object]:
			return {'verdict': False, 'failure_reason': 'destination title was not verified'}

		def errors(self) -> list[None]:
			return [None]

		def action_names(self) -> list[str]:
			return ['navigate', 'click', 'done']

		def urls(self) -> list[str]:
			return ['https://localhost/', 'https://example.com/', 'https://localhost/']

		def action_results(self) -> list[SimpleNamespace]:
			return [SimpleNamespace(metadata=None)]

	class FakeAgent:
		async def run(self, **_kwargs: object) -> RejectedHistory:
			return RejectedHistory()

		def save_history(self, _path: Path) -> None:
			pass

	monkeypatch.setattr('tests.ci.evaluate_tasks.create_subscription_evaluation_llm', lambda *_args, **_kwargs: FakeModel())
	monkeypatch.setattr('tests.ci.evaluate_tasks.BrowserSession', FakeSession)
	monkeypatch.setattr('tests.ci.evaluate_tasks.Agent', lambda **_kwargs: FakeAgent())

	result = await run_model_task(
		task_path,
		EvaluationRunOptions(mode=EvaluationMode.SUBSCRIPTION),
		EvaluationMode.SUBSCRIPTION,
	)

	assert result.status == 'failed'
	assert result.reason_code == EvaluationReasonCode.ASSERTION_FAILED
	assert result.output['agent_success'] is True
	assert result.output['judge_validated'] is False
	assert 'destination title was not verified' in result.explanation


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
