"""Inspect catalogued data sources with isolated real Chromium sessions."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession
from browser_use.browser.views import BrowserStateSummary
from scripts.data_source_catalog import (
	DEFAULT_DATA_SOURCE_CATALOG_PATH,
	DataSourceCatalog,
	DataSourceCategory,
	DataSourceDefinition,
	DataSourceTestLevel,
	load_data_source_catalog,
)

ANTI_BOT_TITLE_MARKERS = (
	'access denied',
	'attention required',
	'just a moment',
	'request blocked',
)
LOGIN_URL_MARKERS = ('/auth', '/login', '/signin', '/sign-in')
LOGIN_TITLE_MARKERS = ('log in', 'login', 'sign in')


class BrowserSourceClassification(StrEnum):
	"""Observed browser-level usefulness of a catalogued source."""

	INTERACTIVE = 'interactive'
	NON_INTERACTIVE = 'non_interactive'
	ANTI_BOT = 'anti_bot'
	LOGIN_REQUIRED = 'login_required'
	BROWSER_UNAVAILABLE = 'browser_unavailable'
	ERROR = 'error'


class BrowserProbeOptions(BaseModel):
	"""Validated command inputs for a real-browser catalog probe."""

	model_config = ConfigDict(extra='forbid')

	catalog_path: Path = DEFAULT_DATA_SOURCE_CATALOG_PATH
	source_ids: set[str] = Field(default_factory=set)
	categories: set[DataSourceCategory] = Field(default_factory=set)
	test_levels: set[DataSourceTestLevel] = Field(default_factory=set)
	concurrency: int = Field(default=1, ge=1, le=8)
	source_attempts: int = Field(default=2, ge=1, le=3)
	source_retry_delay_seconds: float = Field(default=2.0, ge=0, le=30)
	launch_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
	navigation_timeout_seconds: float = Field(default=45.0, gt=0, le=180)
	state_timeout_seconds: float = Field(default=45.0, ge=35.0, le=180)
	state_attempts: int = Field(default=3, ge=1, le=5)
	state_retry_delay_seconds: float = Field(default=1.5, ge=0, le=10)
	shutdown_timeout_seconds: float = Field(default=45.0, ge=35.0, le=180)
	enable_extensions: bool = False
	disable_sandbox: bool = False
	strict: bool = False
	json_output: bool = False
	output_path: Path | None = None


class BrowserDataSourceResult(BaseModel):
	"""Structured Chromium reachability and actionability result for one source."""

	source_id: str
	category: DataSourceCategory
	test_level: DataSourceTestLevel
	classification: BrowserSourceClassification
	reachable: bool
	actionable: bool
	ok: bool
	requested_url: str
	final_url: str | None = None
	title: str | None = None
	selector_count: int = Field(default=0, ge=0)
	elapsed_ms: int = Field(ge=0)
	state_error: str | None = None
	browser_error: str | None = None
	navigation_error: str | None = None
	state_capture_errors: list[str] = Field(default_factory=list)
	cleanup_error: str | None = None
	error: str | None = None
	attempts: int = Field(default=1, ge=1)
	previous_failures: list[str] = Field(default_factory=list)


class BrowserProbeSummary(BaseModel):
	"""Aggregate counts and per-source evidence for a real-browser probe."""

	generated_at: datetime
	total: int = Field(ge=0)
	passed: int = Field(ge=0)
	failed: int = Field(ge=0)
	reachable: int = Field(ge=0)
	actionable: int = Field(ge=0)
	infrastructure_failures: int = Field(ge=0)
	gate_failures: int = Field(ge=0)
	classifications: dict[BrowserSourceClassification, int]
	results: list[BrowserDataSourceResult]


class BrowserSessionProtocol(Protocol):
	"""Browser session surface required by the data-source probe."""

	async def start(self) -> None:
		"""Start the browser session."""
		...

	async def navigate_to(self, url: str, new_tab: bool = False) -> None:
		"""Navigate the active page to ``url``."""
		...

	async def get_browser_state_summary(
		self,
		include_screenshot: bool = True,
		cached: bool = False,
		include_recent_events: bool = False,
	) -> BrowserStateSummary:
		"""Return model-visible browser state."""
		...

	async def get_current_page_url(self) -> str:
		"""Return the active page URL."""
		...

	async def get_current_page_title(self) -> str:
		"""Return the active page title."""
		...

	async def kill(self) -> None:
		"""Terminate the browser session and its local process."""
		...


BrowserSessionFactory = Callable[[BrowserProfile], BrowserSessionProtocol]


def create_browser_session(profile: BrowserProfile) -> BrowserSessionProtocol:
	"""Construct the production BrowserSession behind a testable factory boundary."""
	return BrowserSession(browser_profile=profile)


def classify_browser_page(
	*,
	final_url: str | None,
	title: str | None,
	selector_count: int,
	state_error: str | None,
	error: str | None,
) -> BrowserSourceClassification:
	"""Classify a loaded page without conflating reachability with actionability."""
	if error and error.startswith('start:') and not final_url:
		return BrowserSourceClassification.BROWSER_UNAVAILABLE
	if state_error or error or not final_url or final_url == 'about:blank' or not title:
		return BrowserSourceClassification.ERROR

	normalized_title = title.casefold()
	if any(marker in normalized_title for marker in ANTI_BOT_TITLE_MARKERS):
		return BrowserSourceClassification.ANTI_BOT

	parsed_url = urlsplit(final_url)
	normalized_path = parsed_url.path.casefold()
	if any(marker in normalized_path for marker in LOGIN_URL_MARKERS) or any(
		marker in normalized_title for marker in LOGIN_TITLE_MARKERS
	):
		return BrowserSourceClassification.LOGIN_REQUIRED

	if selector_count == 0:
		return BrowserSourceClassification.NON_INTERACTIVE
	return BrowserSourceClassification.INTERACTIVE


def summarize_browser_results(
	catalog: DataSourceCatalog,
	results: list[BrowserDataSourceResult],
	*,
	strict: bool,
) -> BrowserProbeSummary:
	"""Calculate contract pass counts and browser quality-gate failures."""
	failed_results = [result for result in results if not result.ok]
	gate_failure_source_ids = {
		result.source_id
		for result in failed_results
		if strict or catalog.by_id[result.source_id].test_level == DataSourceTestLevel.BEHAVIORAL
	}
	gate_failure_source_ids.update(result.source_id for result in results if result.cleanup_error or result.browser_error)
	classifications = Counter(result.classification for result in results)
	return BrowserProbeSummary(
		generated_at=datetime.now(timezone.utc),
		total=len(results),
		passed=len(results) - len(failed_results),
		failed=len(failed_results),
		reachable=sum(result.reachable for result in results),
		actionable=sum(result.actionable for result in results),
		infrastructure_failures=sum(bool(result.browser_error or result.cleanup_error) for result in results),
		gate_failures=len(gate_failure_source_ids),
		classifications=dict(classifications),
		results=results,
	)


async def inspect_browser_data_source(
	source: DataSourceDefinition,
	options: BrowserProbeOptions,
	semaphore: asyncio.Semaphore,
	*,
	session_factory: BrowserSessionFactory = create_browser_session,
) -> BrowserDataSourceResult:
	"""Inspect one source in an isolated profile using bounded Chromium resources."""
	async with semaphore:
		started_at = time.monotonic()
		final_url: str | None = None
		title: str | None = None
		selector_count = 0
		state_error: str | None = None
		browser_error: str | None = None
		navigation_error: str | None = None
		state_capture_errors: list[str] = []
		cleanup_error: str | None = None

		with tempfile.TemporaryDirectory(prefix=f'nu-browser-probe-{source.id}-') as profile_directory:
			profile = BrowserProfile(
				headless=True,
				user_data_dir=Path(profile_directory),
				keep_alive=False,
				enable_default_extensions=options.enable_extensions,
				chromium_sandbox=not options.disable_sandbox,
			)
			session = session_factory(profile)
			try:
				await asyncio.wait_for(session.start(), timeout=options.launch_timeout_seconds)
				try:
					await asyncio.wait_for(
						session.navigate_to(str(source.url)),
						timeout=options.navigation_timeout_seconds,
					)
				except Exception as error:
					navigation_error = f'navigate:{type(error).__name__}: {error}'

				for attempt in range(options.state_attempts):
					try:
						state = await asyncio.wait_for(
							session.get_browser_state_summary(include_screenshot=False),
							timeout=options.state_timeout_seconds,
						)
						final_url = state.url
						title = state.title
						selector_count = len(state.dom_state.selector_map)
						state_error = state.state_error
						state_capture_errors.clear()
						if selector_count > 0 and state_error is None:
							break
					except Exception as error:
						state_capture_errors.append(f'{type(error).__name__}: {error}')
					if attempt + 1 < options.state_attempts:
						await asyncio.sleep(options.state_retry_delay_seconds)

				if state_capture_errors:
					try:
						final_url = await session.get_current_page_url()
						title = await session.get_current_page_title()
					except Exception:
						pass
			except Exception as error:
				browser_error = f'start:{type(error).__name__}: {error}'
			finally:
				try:
					await asyncio.wait_for(session.kill(), timeout=options.shutdown_timeout_seconds)
				except Exception as error:
					cleanup_error = f'kill:{type(error).__name__}: {error}'

		source_errors = [
			error
			for error in (
				browser_error,
				navigation_error,
				f'state:{" | ".join(state_capture_errors)}' if state_capture_errors else None,
			)
			if error
		]
		error_text = ' | '.join(source_errors) or None
		classification = classify_browser_page(
			final_url=final_url,
			title=title,
			selector_count=selector_count,
			state_error=state_error,
			error=error_text,
		)
		reachable = bool(final_url and final_url != 'about:blank' and title and not state_error)
		actionable = classification == BrowserSourceClassification.INTERACTIVE
		ok = bool(reachable and error_text is None and (source.test_level == DataSourceTestLevel.AVAILABILITY or actionable))
		return BrowserDataSourceResult(
			source_id=source.id,
			category=source.category,
			test_level=source.test_level,
			classification=classification,
			reachable=reachable,
			actionable=actionable,
			ok=ok,
			requested_url=str(source.url),
			final_url=final_url,
			title=title,
			selector_count=selector_count,
			elapsed_ms=round((time.monotonic() - started_at) * 1000),
			state_error=state_error,
			browser_error=browser_error,
			navigation_error=navigation_error,
			state_capture_errors=state_capture_errors,
			cleanup_error=cleanup_error,
			error=error_text,
		)


def select_browser_sources(
	catalog: DataSourceCatalog,
	options: BrowserProbeOptions,
) -> list[DataSourceDefinition]:
	"""Validate source filters and return catalog entries in stable order."""
	unknown_source_ids = options.source_ids - catalog.by_id.keys()
	if unknown_source_ids:
		raise ValueError(f'Unknown data source IDs: {", ".join(sorted(unknown_source_ids))}')
	return [
		source
		for source in catalog.sources
		if (not options.source_ids or source.id in options.source_ids)
		and (not options.categories or source.category in options.categories)
		and (not options.test_levels or source.test_level in options.test_levels)
	]


async def probe_browser_catalog(
	options: BrowserProbeOptions,
	*,
	session_factory: BrowserSessionFactory = create_browser_session,
) -> BrowserProbeSummary:
	"""Load, filter, and inspect the configured catalog with real browser sessions."""
	catalog = load_data_source_catalog(options.catalog_path)
	sources = select_browser_sources(catalog, options)
	semaphore = asyncio.Semaphore(options.concurrency)
	results = await asyncio.gather(
		*(
			inspect_browser_data_source(
				source,
				options,
				semaphore,
				session_factory=session_factory,
			)
			for source in sources
		)
	)
	results = list(results)
	for attempt_number in range(2, options.source_attempts + 1):
		failed_indices = [
			index for index, result in enumerate(results) if not result.ok or result.cleanup_error or result.browser_error
		]
		if not failed_indices:
			break
		if options.source_retry_delay_seconds:
			await asyncio.sleep(options.source_retry_delay_seconds)
		retry_semaphore = asyncio.Semaphore(1)
		retry_results = await asyncio.gather(
			*(
				inspect_browser_data_source(
					sources[index],
					options,
					retry_semaphore,
					session_factory=session_factory,
				)
				for index in failed_indices
			)
		)
		for index, retry_result in zip(failed_indices, retry_results, strict=True):
			previous_result = results[index]
			previous_failure = (
				previous_result.cleanup_error
				or previous_result.error
				or previous_result.state_error
				or previous_result.classification.value
			)
			results[index] = retry_result.model_copy(
				update={
					'attempts': attempt_number,
					'previous_failures': [*previous_result.previous_failures, previous_failure],
				}
			)
	return summarize_browser_results(catalog, results, strict=options.strict)


def print_browser_probe_summary(summary: BrowserProbeSummary) -> None:
	"""Print a compact browser reachability and actionability table."""
	print(
		f'Checked {summary.total} data sources with Chromium: {summary.passed} passed, '
		f'{summary.failed} failed, {summary.reachable} reachable, {summary.actionable} actionable, '
		f'{summary.infrastructure_failures} browser-runtime failures'
	)
	for result in summary.results:
		status = 'PASS' if result.ok else 'FAIL'
		detail = result.cleanup_error or result.error or result.state_error or result.title or 'no page evidence'
		print(
			f'{status:4}  {result.category.value:14}  {result.test_level.value:12}  '
			f'{result.source_id:28}  {result.classification.value:15}  '
			f'{result.selector_count:4} selectors  attempt {result.attempts}  {result.elapsed_ms}ms  {detail[:100]}'
		)
	if summary.gate_failures:
		print(f'Quality gate failures: {summary.gate_failures}')
	if summary.infrastructure_failures:
		print('Browser-runtime failures happened before source navigation and must not be interpreted as data-source failures.')


def parse_options() -> BrowserProbeOptions:
	"""Parse CLI arguments and return a validated browser probe options model."""
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument('--catalog', type=Path, default=DEFAULT_DATA_SOURCE_CATALOG_PATH)
	parser.add_argument('--source', action='append', default=[], help='Limit the run to one or more source IDs.')
	parser.add_argument(
		'--category',
		action='append',
		choices=[category.value for category in DataSourceCategory],
		default=[],
	)
	parser.add_argument(
		'--test-level',
		action='append',
		choices=[test_level.value for test_level in DataSourceTestLevel],
		default=[],
	)
	parser.add_argument('--concurrency', type=int, default=1)
	parser.add_argument('--source-attempts', type=int, default=2)
	parser.add_argument('--source-retry-delay-seconds', type=float, default=2.0)
	parser.add_argument('--launch-timeout-seconds', type=float, default=30.0)
	parser.add_argument('--navigation-timeout-seconds', type=float, default=45.0)
	parser.add_argument('--state-timeout-seconds', type=float, default=45.0)
	parser.add_argument('--state-attempts', type=int, default=3)
	parser.add_argument('--state-retry-delay-seconds', type=float, default=1.5)
	parser.add_argument('--shutdown-timeout-seconds', type=float, default=45.0)
	parser.add_argument('--enable-extensions', action='store_true')
	parser.add_argument(
		'--disable-sandbox',
		action='store_true',
		help='Disable the Chromium sandbox only in a trusted container or CI host.',
	)
	parser.add_argument('--strict', action='store_true', help='Gate on availability-only sources too.')
	parser.add_argument('--json', action='store_true', dest='json_output')
	parser.add_argument('--output', type=Path, dest='output_path')
	arguments = parser.parse_args()
	return BrowserProbeOptions(
		catalog_path=arguments.catalog,
		source_ids=set(arguments.source),
		categories={DataSourceCategory(category) for category in arguments.category},
		test_levels={DataSourceTestLevel(test_level) for test_level in arguments.test_level},
		concurrency=arguments.concurrency,
		source_attempts=arguments.source_attempts,
		source_retry_delay_seconds=arguments.source_retry_delay_seconds,
		launch_timeout_seconds=arguments.launch_timeout_seconds,
		navigation_timeout_seconds=arguments.navigation_timeout_seconds,
		state_timeout_seconds=arguments.state_timeout_seconds,
		state_attempts=arguments.state_attempts,
		state_retry_delay_seconds=arguments.state_retry_delay_seconds,
		shutdown_timeout_seconds=arguments.shutdown_timeout_seconds,
		enable_extensions=arguments.enable_extensions,
		disable_sandbox=arguments.disable_sandbox,
		strict=arguments.strict,
		json_output=arguments.json_output,
		output_path=arguments.output_path,
	)


def main() -> int:
	"""Run the real-browser probe command and return its quality-gate exit code."""
	options = parse_options()
	summary = asyncio.run(probe_browser_catalog(options))
	serialized_summary = summary.model_dump_json(indent=2)
	if options.output_path is not None:
		options.output_path.parent.mkdir(parents=True, exist_ok=True)
		options.output_path.write_text(serialized_summary + '\n', encoding='utf-8')
	if options.json_output:
		print(json.dumps(summary.model_dump(mode='json'), ensure_ascii=False, indent=2))
	else:
		print_browser_probe_summary(summary)
	return 1 if summary.gate_failures else 0


if __name__ == '__main__':
	raise SystemExit(main())
