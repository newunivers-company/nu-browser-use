"""Tests for structured real-Chromium data-source probes."""

import asyncio
from datetime import date
from pathlib import Path
from typing import cast

import pytest
import yaml
from pydantic import HttpUrl

from browser_use.browser.profile import BrowserProfile
from browser_use.browser.views import BrowserStateSummary
from browser_use.dom.views import DOMSelectorMap, SerializedDOMState
from scripts.check_browser_data_sources import (
	BrowserDataSourceResult,
	BrowserGateMode,
	BrowserProbeOptions,
	BrowserRuntimeMode,
	BrowserSessionProtocol,
	BrowserSourceClassification,
	build_browser_profile,
	classify_browser_page,
	inspect_browser_data_source,
	probe_browser_catalog,
	select_browser_sources,
	summarize_browser_results,
)
from scripts.data_source_catalog import (
	BrowserDataSourceContract,
	DataSourceAccess,
	DataSourceCatalog,
	DataSourceCategory,
	DataSourceDefinition,
	DataSourceTestLevel,
)


class FakeDomNode:
	"""Minimal semantic selector node used by browser evidence tests."""

	def __init__(self, index: int, *, tag_name: str = 'button', text: str | None = None) -> None:
		self.tag_name = tag_name
		label = f'Interactive action number {index}' if text is None else text
		self.attributes = {'aria-label': label} if label else {}

	def get_meaningful_text_for_llm(self) -> str:
		"""Return deterministic model-visible node text."""
		return self.attributes.get('aria-label', '')


def make_source(
	source_id: str = 'browser_source',
	test_level: DataSourceTestLevel = DataSourceTestLevel.BEHAVIORAL,
	browser_contract: BrowserDataSourceContract | None = None,
) -> DataSourceDefinition:
	"""Build a minimal validated source for browser probe tests."""
	return DataSourceDefinition(
		id=source_id,
		name=source_id,
		category=DataSourceCategory.SOCIAL_MEDIA,
		url=HttpUrl(f'https://{source_id}.example.com/'),
		access=DataSourceAccess.PUBLIC_STATIC,
		test_level=test_level,
		expected_http_statuses=[200],
		description='Browser probe test source.',
		browser_contract=browser_contract or BrowserDataSourceContract(),
	)


def make_state(
	*,
	title: str = 'Interactive page',
	selector_count: int = 1,
	url: str = 'https://browser_source.example.com/content',
	tag_name: str = 'button',
	node_text: str | None = None,
) -> BrowserStateSummary:
	"""Build model-visible browser state without starting Chromium."""
	selector_map = cast(
		DOMSelectorMap,
		{index: FakeDomNode(index, tag_name=tag_name, text=node_text) for index in range(selector_count)},
	)
	return BrowserStateSummary(
		dom_state=SerializedDOMState(_root=None, selector_map=selector_map),
		url=url,
		title=title,
		tabs=[],
	)


class FakeBrowserSession:
	"""In-memory BrowserSessionProtocol implementation for isolated tests."""

	def __init__(
		self,
		state: BrowserStateSummary,
		*,
		state_failure: Exception | None = None,
		kill_failure: Exception | None = None,
	) -> None:
		self.state = state
		self.state_failure = state_failure
		self.kill_failure = kill_failure
		self.started = False
		self.killed = False

	async def start(self) -> None:
		"""Record a successful fake launch."""
		self.started = True

	async def navigate_to(self, url: str, new_tab: bool = False) -> None:
		"""Accept navigation without external I/O."""

	async def get_browser_state_summary(
		self,
		include_screenshot: bool = True,
		cached: bool = False,
		include_recent_events: bool = False,
	) -> BrowserStateSummary:
		"""Return configured state or raise a configured failure."""
		if self.state_failure is not None:
			raise self.state_failure
		return self.state

	async def get_current_page_url(self) -> str:
		"""Return the fallback state URL."""
		return self.state.url

	async def get_current_page_title(self) -> str:
		"""Return the fallback state title."""
		return self.state.title

	async def kill(self) -> None:
		"""Record deterministic shutdown."""
		self.killed = True
		if self.kill_failure is not None:
			raise self.kill_failure


class FakeSessionFactory:
	"""Capture probe profiles while returning one configured fake session."""

	def __init__(self, session: FakeBrowserSession) -> None:
		self.session = session
		self.profiles: list[BrowserProfile] = []

	def __call__(self, profile: BrowserProfile) -> BrowserSessionProtocol:
		"""Return the fake session and retain its validated profile."""
		self.profiles.append(profile)
		return self.session


class SequencedSessionFactory:
	"""Return a fresh configured session for each whole-source attempt."""

	def __init__(self, sessions: list[FakeBrowserSession]) -> None:
		self.sessions = iter(sessions)

	def __call__(self, profile: BrowserProfile) -> BrowserSessionProtocol:
		"""Return the next isolated fake browser session."""
		return next(self.sessions)


class DelayedInteractiveSession(FakeBrowserSession):
	"""Return a non-interactive shell once before the final rendered DOM."""

	def __init__(self) -> None:
		super().__init__(make_state(selector_count=2))
		self.state_calls = 0

	async def get_browser_state_summary(
		self,
		include_screenshot: bool = True,
		cached: bool = False,
		include_recent_events: bool = False,
	) -> BrowserStateSummary:
		"""Simulate a JavaScript page that renders controls after its first capture."""
		self.state_calls += 1
		if self.state_calls == 1:
			return make_state(title='Loading shell', selector_count=0)
		return self.state


class RecoveredNonInteractiveSession(FakeBrowserSession):
	"""Fail one capture before returning a valid non-interactive page state."""

	def __init__(self) -> None:
		super().__init__(make_state(title='Rendered document', selector_count=0))
		self.state_calls = 0

	async def get_browser_state_summary(
		self,
		include_screenshot: bool = True,
		cached: bool = False,
		include_recent_events: bool = False,
	) -> BrowserStateSummary:
		"""Return a transient error followed by a successful empty-selector state."""
		self.state_calls += 1
		if self.state_calls == 1:
			raise ConnectionError('transient state capture failure')
		return self.state


class UnstableDomSession(FakeBrowserSession):
	"""Return materially different selector sets on every state capture."""

	def __init__(self) -> None:
		super().__init__(make_state(selector_count=1))
		self.state_calls = 0

	async def get_browser_state_summary(
		self,
		include_screenshot: bool = True,
		cached: bool = False,
		include_recent_events: bool = False,
	) -> BrowserStateSummary:
		"""Alternate between small and large rendered states."""
		selector_counts = [1, 20, 1]
		selector_count = selector_counts[min(self.state_calls, len(selector_counts) - 1)]
		self.state_calls += 1
		return make_state(selector_count=selector_count)


class StartFailureSession(FakeBrowserSession):
	"""Fail the shared runtime preflight before any source navigation."""

	async def start(self) -> None:
		"""Raise a deterministic local browser launch failure."""
		raise RuntimeError('sandbox unavailable')


async def test_browser_probe_reports_interactive_behavioral_source() -> None:
	"""Behavioral sources pass only with reachable model-actionable state."""
	session = FakeBrowserSession(make_state(selector_count=2))
	factory = FakeSessionFactory(session)
	result = await inspect_browser_data_source(
		make_source(),
		BrowserProbeOptions(disable_sandbox=True),
		asyncio.Semaphore(1),
		session_factory=factory,
	)

	assert result.ok is True
	assert result.reachable is True
	assert result.actionable is True
	assert result.classification == BrowserSourceClassification.INTERACTIVE
	assert result.selector_count == 2
	assert session.started is True
	assert session.killed is True
	assert factory.profiles[0].chromium_sandbox is False
	assert factory.profiles[0].enable_default_extensions is False


async def test_browser_probe_preserves_failure_and_still_kills_session() -> None:
	"""State extraction failures must fail the contract and still terminate Chromium."""
	session = FakeBrowserSession(make_state(), state_failure=TimeoutError('state timeout'))
	result = await inspect_browser_data_source(
		make_source(),
		BrowserProbeOptions(state_retry_delay_seconds=0),
		asyncio.Semaphore(1),
		session_factory=FakeSessionFactory(session),
	)

	assert result.ok is False
	assert result.classification == BrowserSourceClassification.ERROR
	assert result.error is not None and 'state:TimeoutError' in result.error
	assert session.killed is True


async def test_browser_probe_retries_initial_non_interactive_state() -> None:
	"""Dynamic behavioral pages may become actionable after an initial empty DOM."""
	session = DelayedInteractiveSession()
	result = await inspect_browser_data_source(
		make_source(),
		BrowserProbeOptions(state_retry_delay_seconds=0),
		asyncio.Semaphore(1),
		session_factory=FakeSessionFactory(session),
	)

	assert result.ok is True
	assert result.selector_count == 2
	assert result.dom_stable is True
	assert result.stable_state_captures == 2
	assert session.state_calls == 3
	assert session.killed is True


async def test_browser_probe_clears_transient_capture_error_after_success() -> None:
	"""A later valid state must not remain classified as an earlier capture failure."""
	session = RecoveredNonInteractiveSession()
	result = await inspect_browser_data_source(
		make_source(test_level=DataSourceTestLevel.AVAILABILITY),
		BrowserProbeOptions(state_retry_delay_seconds=0),
		asyncio.Semaphore(1),
		session_factory=FakeSessionFactory(session),
	)

	assert result.ok is True
	assert result.classification == BrowserSourceClassification.NON_INTERACTIVE
	assert result.state_capture_errors == []
	assert result.error is None
	assert session.state_calls == 3


async def test_browser_probe_rejects_empty_root_selector_false_positive() -> None:
	"""A generic app root without text or controls must not count as actionable content."""
	result = await inspect_browser_data_source(
		make_source(),
		BrowserProbeOptions(state_retry_delay_seconds=0),
		asyncio.Semaphore(1),
		session_factory=FakeSessionFactory(make_state_session := FakeBrowserSession(make_state(tag_name='div', node_text=''))),
	)

	assert make_state_session.killed is True
	assert result.reachable is True
	assert result.content_available is False
	assert result.meaningful_selector_count == 0
	assert result.interactive_element_count == 0
	assert result.classification == BrowserSourceClassification.NON_INTERACTIVE
	assert result.ok is False


async def test_browser_probe_requires_target_path_contract() -> None:
	"""A rendered but off-target path must not pass source fidelity checks."""
	source = make_source(
		browser_contract=BrowserDataSourceContract(allowed_final_path_prefixes=['/expected']),
	)
	result = await inspect_browser_data_source(
		source,
		BrowserProbeOptions(state_retry_delay_seconds=0),
		asyncio.Semaphore(1),
		session_factory=FakeSessionFactory(
			FakeBrowserSession(make_state(selector_count=3, url='https://browser_source.example.com/other'))
		),
	)

	assert result.target_matched is False
	assert result.classification == BrowserSourceClassification.TARGET_MISMATCH
	assert result.actionable is False
	assert result.target_errors and 'final path' in result.target_errors[0]


async def test_browser_probe_requires_consecutive_stable_dom_states() -> None:
	"""Materially changing DOM snapshots remain unstable after the bounded capture window."""
	result = await inspect_browser_data_source(
		make_source(),
		BrowserProbeOptions(state_retry_delay_seconds=0, state_stability_tolerance=0),
		asyncio.Semaphore(1),
		session_factory=FakeSessionFactory(UnstableDomSession()),
	)

	assert result.state_captures == 3
	assert result.stable_state_captures == 1
	assert result.dom_stable is False
	assert result.classification == BrowserSourceClassification.UNSTABLE
	assert result.ok is False


async def test_browser_probe_separates_cleanup_failure_from_source_result() -> None:
	"""Cleanup failures are infrastructure evidence, not website classification errors."""
	source = make_source()
	result = await inspect_browser_data_source(
		source,
		BrowserProbeOptions(),
		asyncio.Semaphore(1),
		session_factory=FakeSessionFactory(
			FakeBrowserSession(make_state(selector_count=2), kill_failure=TimeoutError('kill timed out'))
		),
	)
	catalog = DataSourceCatalog(version=1, last_reviewed=date(2026, 8, 12), sources=[source])
	summary = summarize_browser_results(catalog, [result], strict=False)

	assert result.classification == BrowserSourceClassification.INTERACTIVE
	assert result.ok is True
	assert result.error is None
	assert result.cleanup_error is not None and result.cleanup_error.startswith('kill:TimeoutError')
	assert summary.infrastructure_failures == 1
	assert summary.gate_failures == 1


async def test_browser_catalog_retries_failed_source_in_fresh_session(tmp_path: Path) -> None:
	"""Transient CDP failures receive a bounded whole-browser retry with retained evidence."""
	source = make_source()
	catalog = DataSourceCatalog(
		version=1,
		last_reviewed=date(2026, 8, 12),
		sources=[source],
	)
	catalog_path = tmp_path / 'catalog.yaml'
	catalog_path.write_text(yaml.safe_dump(catalog.model_dump(mode='json')), encoding='utf-8')
	failed_session = FakeBrowserSession(
		make_state(url='https://browser_source.example.com/content'),
		state_failure=ConnectionError('CDP closed'),
	)
	passed_session = FakeBrowserSession(make_state(selector_count=3, url='https://browser_source.example.com/content'))

	summary = await probe_browser_catalog(
		BrowserProbeOptions(
			catalog_path=catalog_path,
			state_attempts=1,
			required_stable_states=1,
			state_retry_delay_seconds=0,
			source_attempts=2,
			source_retry_delay_seconds=0,
			preflight=False,
		),
		session_factory=SequencedSessionFactory([failed_session, passed_session]),
	)

	result = summary.results[0]
	assert summary.gate_failures == 0
	assert result.ok is True
	assert result.attempts == 2
	assert result.previous_failures and 'state:ConnectionError' in result.previous_failures[0]
	assert failed_session.killed is True
	assert passed_session.killed is True


def test_browser_page_classification_distinguishes_reachability_states() -> None:
	"""Keep runtime, anti-bot, login, sparse, interactive, and page errors distinct."""
	assert (
		classify_browser_page(
			final_url=None,
			title=None,
			selector_count=0,
			state_error=None,
			error='start:RuntimeError: Chromium crashed',
		)
		== BrowserSourceClassification.BROWSER_UNAVAILABLE
	)
	assert (
		classify_browser_page(
			final_url='https://example.com/',
			title='Just a moment...',
			selector_count=5,
			state_error=None,
			error=None,
		)
		== BrowserSourceClassification.ANTI_BOT
	)
	assert (
		classify_browser_page(
			final_url='https://example.com/login',
			title='Sign in',
			selector_count=10,
			state_error=None,
			error=None,
		)
		== BrowserSourceClassification.LOGIN_REQUIRED
	)
	assert (
		classify_browser_page(final_url='https://example.com/', title='Shell', selector_count=0, state_error=None, error=None)
		== BrowserSourceClassification.NON_INTERACTIVE
	)
	assert (
		classify_browser_page(final_url='https://example.com/', title='Content', selector_count=3, state_error=None, error=None)
		== BrowserSourceClassification.INTERACTIVE
	)
	assert (
		classify_browser_page(
			final_url='https://example.com/', title='Content', selector_count=3, state_error='capture failed', error=None
		)
		== BrowserSourceClassification.ERROR
	)


def test_browser_summary_gates_behavioral_failures_by_default() -> None:
	"""Availability-only failures remain informational unless strict mode is enabled."""
	behavioral_source = make_source('behavioral_source', DataSourceTestLevel.BEHAVIORAL)
	availability_source = make_source('availability_source', DataSourceTestLevel.AVAILABILITY)
	catalog = DataSourceCatalog(
		version=1,
		last_reviewed=date(2026, 8, 12),
		sources=[behavioral_source, availability_source],
	)
	results = [
		BrowserDataSourceResult(
			source_id=source.id,
			category=source.category,
			test_level=source.test_level,
			classification=BrowserSourceClassification.ERROR,
			reachable=False,
			target_matched=False,
			content_available=False,
			actionable=False,
			dom_stable=False,
			ok=False,
			requested_url=str(source.url),
			elapsed_ms=1,
			error='unreachable',
		)
		for source in catalog.sources
	]

	assert summarize_browser_results(catalog, results, strict=False).gate_failures == 1
	assert summarize_browser_results(catalog, results, strict=True).gate_failures == 2
	assert summarize_browser_results(catalog, results, strict=True).infrastructure_failures == 0


def test_browser_summary_separates_reachability_and_actionability_gates() -> None:
	"""An availability anti-bot page may be reachable without passing actionability."""
	source = make_source(test_level=DataSourceTestLevel.AVAILABILITY)
	catalog = DataSourceCatalog(version=1, last_reviewed=date(2026, 8, 12), sources=[source])
	result = BrowserDataSourceResult(
		source_id=source.id,
		category=source.category,
		test_level=source.test_level,
		classification=BrowserSourceClassification.ANTI_BOT,
		reachable=True,
		target_matched=True,
		content_available=True,
		actionable=False,
		dom_stable=True,
		ok=True,
		requested_url=str(source.url),
		final_url=str(source.url),
		title='Just a moment...',
		elapsed_ms=1,
	)

	assert (
		summarize_browser_results(
			catalog,
			[result],
			strict=True,
			gate_mode=BrowserGateMode.REACHABILITY,
		).gate_failures
		== 0
	)
	assert (
		summarize_browser_results(
			catalog,
			[result],
			strict=True,
			gate_mode=BrowserGateMode.ACTIONABILITY,
		).gate_failures
		== 1
	)


def test_browser_profile_supports_explicit_executable_and_cloud_modes(tmp_path: Path) -> None:
	"""Runtime options produce mutually exclusive local and Browser Use Cloud profiles."""
	executable = tmp_path / 'chrome'
	executable.touch(mode=0o755)
	local_options = BrowserProbeOptions(executable_path=executable)
	local_profile = build_browser_profile(local_options, tmp_path / 'local-profile')
	cloud_options = BrowserProbeOptions(
		use_cloud=True,
		cloud_proxy_country_code='us',
		cloud_timeout_minutes=15,
	)
	cloud_profile = build_browser_profile(cloud_options, tmp_path / 'unused-cloud-profile')

	assert local_profile.executable_path == executable
	assert local_profile.chromium_sandbox is True
	assert local_profile.use_cloud is False
	assert cloud_profile.use_cloud is True
	assert cloud_profile.cloud_browser_params is not None
	assert cloud_profile.cloud_browser_params.proxy_country_code == 'us'
	with pytest.raises(ValueError, match='cannot be combined'):
		BrowserProbeOptions(use_cloud=True, executable_path=executable)


async def test_browser_preflight_failure_stops_source_scheduling(tmp_path: Path) -> None:
	"""One shared launch failure should classify infrastructure without navigating every source."""
	sources = [make_source(), make_source('second_source')]
	catalog = DataSourceCatalog(version=1, last_reviewed=date(2026, 8, 12), sources=sources)
	catalog_path = tmp_path / 'catalog.yaml'
	catalog_path.write_text(yaml.safe_dump(catalog.model_dump(mode='json')), encoding='utf-8')
	session = StartFailureSession(make_state())
	summary = await probe_browser_catalog(
		BrowserProbeOptions(catalog_path=catalog_path),
		session_factory=FakeSessionFactory(session),
	)

	assert summary.preflight is not None and summary.preflight.ok is False
	assert summary.total == 2
	assert summary.infrastructure_failures == 1
	assert summary.results[0].classification == BrowserSourceClassification.BROWSER_UNAVAILABLE
	assert summary.results[0].browser_mode == BrowserRuntimeMode.LOCAL_DEFAULT
	assert session.killed is True


def test_select_browser_sources_validates_ids_and_combines_filters(tmp_path: Path) -> None:
	"""Apply source/category/level filters without silently accepting unknown IDs."""
	behavioral_source = make_source('behavioral_source', DataSourceTestLevel.BEHAVIORAL)
	availability_source = make_source('availability_source', DataSourceTestLevel.AVAILABILITY)
	catalog = DataSourceCatalog(
		version=1,
		last_reviewed=date(2026, 8, 12),
		sources=[behavioral_source, availability_source],
	)
	options = BrowserProbeOptions(
		catalog_path=tmp_path / 'unused.yaml',
		source_ids={'behavioral_source'},
		categories={DataSourceCategory.SOCIAL_MEDIA},
		test_levels={DataSourceTestLevel.BEHAVIORAL},
	)

	assert [source.id for source in select_browser_sources(catalog, options)] == ['behavioral_source']

	unknown_options = options.model_copy(update={'source_ids': {'missing_source'}})
	try:
		select_browser_sources(catalog, unknown_options)
	except ValueError as error:
		assert 'missing_source' in str(error)
	else:
		raise AssertionError('Unknown source ID should fail validation')
