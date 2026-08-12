"""Tests for structured real-Chromium data-source probes."""

import asyncio
from datetime import date
from pathlib import Path
from typing import cast

import yaml
from pydantic import HttpUrl

from browser_use.browser.profile import BrowserProfile
from browser_use.browser.views import BrowserStateSummary
from browser_use.dom.views import DOMSelectorMap, SerializedDOMState
from scripts.check_browser_data_sources import (
	BrowserDataSourceResult,
	BrowserProbeOptions,
	BrowserSessionProtocol,
	BrowserSourceClassification,
	classify_browser_page,
	inspect_browser_data_source,
	probe_browser_catalog,
	select_browser_sources,
	summarize_browser_results,
)
from scripts.data_source_catalog import (
	DataSourceAccess,
	DataSourceCatalog,
	DataSourceCategory,
	DataSourceDefinition,
	DataSourceTestLevel,
)


def make_source(
	source_id: str = 'browser_source',
	test_level: DataSourceTestLevel = DataSourceTestLevel.BEHAVIORAL,
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
	)


def make_state(*, title: str = 'Interactive page', selector_count: int = 1) -> BrowserStateSummary:
	"""Build model-visible browser state without starting Chromium."""
	selector_map = cast(DOMSelectorMap, {index: object() for index in range(selector_count)})
	return BrowserStateSummary(
		dom_state=SerializedDOMState(_root=None, selector_map=selector_map),
		url='https://browser_source.example.com/content',
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
	assert session.state_calls == 2
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
	failed_session = FakeBrowserSession(make_state(), state_failure=ConnectionError('CDP closed'))
	passed_session = FakeBrowserSession(make_state(selector_count=3))

	summary = await probe_browser_catalog(
		BrowserProbeOptions(
			catalog_path=catalog_path,
			state_attempts=1,
			state_retry_delay_seconds=0,
			source_attempts=2,
			source_retry_delay_seconds=0,
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
	common = {'state_error': None, 'error': None}
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
		classify_browser_page(final_url='https://example.com/', title='Just a moment...', selector_count=5, **common)
		== BrowserSourceClassification.ANTI_BOT
	)
	assert (
		classify_browser_page(final_url='https://example.com/login', title='Sign in', selector_count=10, **common)
		== BrowserSourceClassification.LOGIN_REQUIRED
	)
	assert (
		classify_browser_page(final_url='https://example.com/', title='Shell', selector_count=0, **common)
		== BrowserSourceClassification.NON_INTERACTIVE
	)
	assert (
		classify_browser_page(final_url='https://example.com/', title='Content', selector_count=3, **common)
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
			actionable=False,
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
