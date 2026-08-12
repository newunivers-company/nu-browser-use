"""Inspect catalogued data sources with isolated real Chromium sessions."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from browser_use.browser.cloud.views import CloudBrowserParams
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
INTERACTIVE_TAG_NAMES = {
	'a',
	'button',
	'input',
	'option',
	'select',
	'summary',
	'textarea',
}
INTERACTIVE_ROLES = {
	'button',
	'checkbox',
	'combobox',
	'link',
	'menuitem',
	'option',
	'radio',
	'search',
	'slider',
	'spinbutton',
	'switch',
	'tab',
	'textbox',
}
MEANINGFUL_ATTRIBUTE_NAMES = {'alt', 'aria-label', 'href', 'placeholder', 'title', 'value'}
DOM_TAG_PATTERN = re.compile(r'<[^>]*>')
DOM_INDEX_PATTERN = re.compile(r'\[\d+\]')


class BrowserSourceClassification(StrEnum):
	"""Observed browser-level usefulness of a catalogued source."""

	INTERACTIVE = 'interactive'
	NON_INTERACTIVE = 'non_interactive'
	ANTI_BOT = 'anti_bot'
	LOGIN_REQUIRED = 'login_required'
	TARGET_MISMATCH = 'target_mismatch'
	UNSTABLE = 'unstable'
	BROWSER_UNAVAILABLE = 'browser_unavailable'
	ERROR = 'error'


class BrowserGateMode(StrEnum):
	"""Evidence level required by the browser quality gate."""

	CATALOG = 'catalog'
	REACHABILITY = 'reachability'
	CONTENT = 'content'
	ACTIONABILITY = 'actionability'


class BrowserRuntimeMode(StrEnum):
	"""Browser infrastructure selected for a probe run."""

	LOCAL_DEFAULT = 'local_default'
	LOCAL_EXECUTABLE = 'local_executable'
	CLOUD = 'cloud'


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
	required_stable_states: int = Field(default=2, ge=1, le=5)
	state_stability_tolerance: float = Field(default=0.2, ge=0.0, le=1.0)
	state_retry_delay_seconds: float = Field(default=1.5, ge=0, le=10)
	shutdown_timeout_seconds: float = Field(default=45.0, ge=35.0, le=180)
	enable_extensions: bool = False
	disable_sandbox: bool = False
	executable_path: Path | None = None
	use_cloud: bool = False
	cloud_profile_id: str | None = None
	cloud_proxy_country_code: str | None = None
	cloud_timeout_minutes: int | None = Field(default=None, ge=1, le=240)
	preflight: bool = True
	gate_mode: BrowserGateMode = BrowserGateMode.CATALOG
	strict: bool = False
	json_output: bool = False
	output_path: Path | None = None

	@model_validator(mode='after')
	def validate_browser_runtime(self) -> BrowserProbeOptions:
		"""Reject incompatible local/cloud settings and impossible stability windows."""
		if self.required_stable_states > self.state_attempts:
			raise ValueError('required_stable_states must not exceed state_attempts')
		if self.use_cloud and self.executable_path is not None:
			raise ValueError('executable_path cannot be combined with use_cloud')
		if self.use_cloud and self.disable_sandbox:
			raise ValueError('disable_sandbox is a local-browser option and cannot be combined with use_cloud')
		if not self.use_cloud and any(
			value is not None for value in (self.cloud_profile_id, self.cloud_proxy_country_code, self.cloud_timeout_minutes)
		):
			raise ValueError('cloud options require use_cloud=True')
		return self


class BrowserDomEvidence(BaseModel):
	"""Semantic evidence extracted from one model-visible DOM state."""

	selector_count: int = Field(ge=0)
	meaningful_selector_count: int = Field(ge=0)
	interactive_element_count: int = Field(ge=0)
	visible_text_chars: int = Field(ge=0)
	matched_content_markers: list[str] = Field(default_factory=list)
	missing_content_markers: list[str] = Field(default_factory=list)


class BrowserRuntimePreflight(BaseModel):
	"""One browser launch/cleanup check performed before source navigation."""

	mode: BrowserRuntimeMode
	ok: bool
	elapsed_ms: int = Field(ge=0)
	error: str | None = None
	cleanup_error: str | None = None


class BrowserDataSourceResult(BaseModel):
	"""Structured Chromium reachability and actionability result for one source."""

	source_id: str
	category: DataSourceCategory
	test_level: DataSourceTestLevel
	classification: BrowserSourceClassification
	reachable: bool
	target_matched: bool
	content_available: bool
	actionable: bool
	dom_stable: bool
	ok: bool
	requested_url: str
	final_url: str | None = None
	title: str | None = None
	selector_count: int = Field(default=0, ge=0)
	meaningful_selector_count: int = Field(default=0, ge=0)
	interactive_element_count: int = Field(default=0, ge=0)
	visible_text_chars: int = Field(default=0, ge=0)
	state_captures: int = Field(default=0, ge=0)
	stable_state_captures: int = Field(default=0, ge=0)
	matched_content_markers: list[str] = Field(default_factory=list)
	missing_content_markers: list[str] = Field(default_factory=list)
	target_errors: list[str] = Field(default_factory=list)
	contract_failures: list[str] = Field(default_factory=list)
	browser_mode: BrowserRuntimeMode = BrowserRuntimeMode.LOCAL_DEFAULT
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
	target_matched: int = Field(ge=0)
	content_available: int = Field(ge=0)
	actionable: int = Field(ge=0)
	stable: int = Field(ge=0)
	infrastructure_failures: int = Field(ge=0)
	gate_failures: int = Field(ge=0)
	gate_mode: BrowserGateMode
	preflight: BrowserRuntimePreflight | None = None
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


def browser_runtime_mode(options: BrowserProbeOptions) -> BrowserRuntimeMode:
	"""Return the explicit browser infrastructure mode for structured evidence."""
	if options.use_cloud:
		return BrowserRuntimeMode.CLOUD
	if options.executable_path is not None:
		return BrowserRuntimeMode.LOCAL_EXECUTABLE
	return BrowserRuntimeMode.LOCAL_DEFAULT


def build_browser_profile(options: BrowserProbeOptions, profile_directory: Path) -> BrowserProfile:
	"""Build one isolated local or cloud BrowserProfile from validated probe options."""
	if options.use_cloud:
		cloud_parameters = CloudBrowserParams(
			cloud_profile_id=options.cloud_profile_id,
			cloud_proxy_country_code=options.cloud_proxy_country_code,
			cloud_timeout=options.cloud_timeout_minutes,
		)
		return BrowserProfile(
			headless=True,
			keep_alive=False,
			enable_default_extensions=options.enable_extensions,
			use_cloud=True,
			cloud_browser_params=cloud_parameters,
		)
	return BrowserProfile(
		headless=True,
		keep_alive=False,
		enable_default_extensions=options.enable_extensions,
		user_data_dir=profile_directory,
		executable_path=options.executable_path,
		chromium_sandbox=not options.disable_sandbox,
	)


def _node_semantic_text(node: object) -> str:
	"""Read the same concise semantic text the Browser Use model sees for a DOM node."""
	text_reader = getattr(node, 'get_meaningful_text_for_llm', None)
	if callable(text_reader):
		try:
			return ' '.join(str(text_reader()).split())
		except Exception:
			return ''
	attributes = getattr(node, 'attributes', {})
	if isinstance(attributes, dict):
		for attribute_name in ('aria-label', 'title', 'placeholder', 'alt', 'value'):
			if value := attributes.get(attribute_name):
				return ' '.join(str(value).split())
	return ''


def extract_browser_dom_evidence(state: BrowserStateSummary, source: DataSourceDefinition) -> BrowserDomEvidence:
	"""Measure meaningful content and controls rather than counting generic DOM roots."""
	selector_map = state.dom_state.selector_map
	meaningful_selector_count = 0
	interactive_element_count = 0
	for node in selector_map.values():
		tag_name = str(getattr(node, 'tag_name', '') or '').casefold()
		attributes = getattr(node, 'attributes', {})
		attributes = attributes if isinstance(attributes, dict) else {}
		role = str(attributes.get('role', '') or '').casefold()
		semantic_text = _node_semantic_text(node)
		has_meaningful_attribute = any(attributes.get(name) for name in MEANINGFUL_ATTRIBUTE_NAMES)
		is_interactive = (
			tag_name in INTERACTIVE_TAG_NAMES
			or tag_name.endswith('button')
			or role in INTERACTIVE_ROLES
			or bool(attributes.get('href'))
		)
		if semantic_text or has_meaningful_attribute or is_interactive:
			meaningful_selector_count += 1
		if is_interactive:
			interactive_element_count += 1

	try:
		dom_representation = state.dom_state.llm_representation()
		if getattr(state.dom_state, '_root', None) is None and selector_map:
			dom_representation = ' '.join(_node_semantic_text(node) for node in selector_map.values())
	except Exception:
		dom_representation = ' '.join(_node_semantic_text(node) for node in selector_map.values())
	visible_text = DOM_INDEX_PATTERN.sub(' ', DOM_TAG_PATTERN.sub(' ', dom_representation))
	visible_text = ' '.join(visible_text.split())
	normalized_content = dom_representation.casefold()
	required_markers = source.browser_contract.required_content_markers
	matched_markers = [marker for marker in required_markers if marker.casefold() in normalized_content]
	missing_markers = [marker for marker in required_markers if marker.casefold() not in normalized_content]
	return BrowserDomEvidence(
		selector_count=len(selector_map),
		meaningful_selector_count=meaningful_selector_count,
		interactive_element_count=interactive_element_count,
		visible_text_chars=len(visible_text),
		matched_content_markers=matched_markers,
		missing_content_markers=missing_markers,
	)


def browser_states_are_stable(
	previous_state: BrowserStateSummary,
	previous_evidence: BrowserDomEvidence,
	current_state: BrowserStateSummary,
	current_evidence: BrowserDomEvidence,
	*,
	tolerance: float,
) -> bool:
	"""Accept consecutive states whose URL, title, and semantic evidence have settled."""
	if previous_state.url != current_state.url or previous_state.title != current_state.title:
		return False

	def within_tolerance(previous: int, current: int, minimum_delta: int) -> bool:
		allowed_delta = max(minimum_delta, math.ceil(max(previous, current) * tolerance))
		return abs(previous - current) <= allowed_delta

	return (
		within_tolerance(previous_evidence.meaningful_selector_count, current_evidence.meaningful_selector_count, 2)
		and within_tolerance(previous_evidence.interactive_element_count, current_evidence.interactive_element_count, 2)
		and within_tolerance(previous_evidence.visible_text_chars, current_evidence.visible_text_chars, 100)
		and previous_evidence.missing_content_markers == current_evidence.missing_content_markers
	)


def evaluate_browser_target(
	source: DataSourceDefinition,
	final_url: str | None,
	title: str | None,
) -> list[str]:
	"""Return source-contract violations for final origin, path, and title."""
	if final_url is None:
		return ['no final URL was captured']
	requested = urlsplit(str(source.url))
	final = urlsplit(final_url)
	errors: list[str] = []
	if source.browser_contract.require_same_origin and (requested.scheme.casefold(), requested.netloc.casefold()) != (
		final.scheme.casefold(),
		final.netloc.casefold(),
	):
		errors.append(f'final origin {final.scheme}://{final.netloc} differs from requested origin')
	allowed_paths = source.browser_contract.allowed_final_path_prefixes
	if allowed_paths and not any(
		final.path == prefix or final.path.startswith(f'{prefix.rstrip("/")}/') for prefix in allowed_paths
	):
		errors.append(f'final path {final.path or "/"} does not match {allowed_paths}')
	normalized_title = (title or '').casefold()
	missing_title_markers = [
		marker for marker in source.browser_contract.expected_title_markers if marker.casefold() not in normalized_title
	]
	if missing_title_markers:
		errors.append(f'missing title markers: {", ".join(missing_title_markers)}')
	return errors


def classify_browser_page(
	*,
	final_url: str | None,
	title: str | None,
	selector_count: int,
	state_error: str | None,
	error: str | None,
	meaningful_selector_count: int | None = None,
	interactive_element_count: int | None = None,
	minimum_interactive_elements: int = 1,
	target_matched: bool = True,
	content_available: bool = True,
	dom_stable: bool = True,
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

	if not target_matched:
		return BrowserSourceClassification.TARGET_MISMATCH
	meaningful_count = selector_count if meaningful_selector_count is None else meaningful_selector_count
	interactive_count = selector_count if interactive_element_count is None else interactive_element_count
	if not content_available or meaningful_count == 0 or interactive_count < minimum_interactive_elements:
		return BrowserSourceClassification.NON_INTERACTIVE
	if not dom_stable:
		return BrowserSourceClassification.UNSTABLE
	return BrowserSourceClassification.INTERACTIVE


def browser_result_meets_gate(result: BrowserDataSourceResult, gate_mode: BrowserGateMode) -> bool:
	"""Evaluate one source against an explicit reachability, content, or actionability gate."""
	if result.browser_error or result.cleanup_error or result.error:
		return False
	if gate_mode == BrowserGateMode.CATALOG:
		return result.ok
	if gate_mode == BrowserGateMode.REACHABILITY:
		return result.reachable and result.target_matched
	if gate_mode == BrowserGateMode.CONTENT:
		return result.reachable and result.target_matched and result.content_available
	return result.reachable and result.target_matched and result.content_available and result.dom_stable and result.actionable


def summarize_browser_results(
	catalog: DataSourceCatalog,
	results: list[BrowserDataSourceResult],
	*,
	strict: bool,
	gate_mode: BrowserGateMode = BrowserGateMode.CATALOG,
	preflight: BrowserRuntimePreflight | None = None,
) -> BrowserProbeSummary:
	"""Calculate contract pass counts and browser quality-gate failures."""
	failed_results = [result for result in results if not result.ok]
	gate_failure_source_ids = {
		result.source_id
		for result in results
		if (strict or catalog.by_id[result.source_id].test_level == DataSourceTestLevel.BEHAVIORAL)
		and not browser_result_meets_gate(result, gate_mode)
	}
	gate_failure_source_ids.update(result.source_id for result in results if result.cleanup_error or result.browser_error)
	classifications = Counter(result.classification for result in results)
	infrastructure_failures = (
		1
		if preflight is not None and not preflight.ok
		else sum(bool(result.browser_error or result.cleanup_error) for result in results)
	)
	return BrowserProbeSummary(
		generated_at=datetime.now(timezone.utc),
		total=len(results),
		passed=len(results) - len(failed_results),
		failed=len(failed_results),
		reachable=sum(result.reachable for result in results),
		target_matched=sum(result.target_matched for result in results),
		content_available=sum(result.content_available for result in results),
		actionable=sum(result.actionable for result in results),
		stable=sum(result.dom_stable for result in results),
		infrastructure_failures=infrastructure_failures,
		gate_failures=len(gate_failure_source_ids),
		gate_mode=gate_mode,
		preflight=preflight,
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
		dom_evidence = BrowserDomEvidence(
			selector_count=0,
			meaningful_selector_count=0,
			interactive_element_count=0,
			visible_text_chars=0,
		)
		state_error: str | None = None
		browser_error: str | None = None
		navigation_error: str | None = None
		state_capture_errors: list[str] = []
		cleanup_error: str | None = None
		state_captures = 0
		stable_state_captures = 0
		previous_state: BrowserStateSummary | None = None
		previous_evidence: BrowserDomEvidence | None = None

		with tempfile.TemporaryDirectory(prefix=f'nu-browser-probe-{source.id}-') as profile_directory:
			profile = build_browser_profile(options, Path(profile_directory))
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
						dom_evidence = extract_browser_dom_evidence(state, source)
						state_error = state.state_error
						state_captures += 1
						state_capture_errors.clear()
						if state_error is None:
							if (
								previous_state is not None
								and previous_evidence is not None
								and browser_states_are_stable(
									previous_state,
									previous_evidence,
									state,
									dom_evidence,
									tolerance=options.state_stability_tolerance,
								)
							):
								stable_state_captures += 1
							else:
								stable_state_captures = 1
						else:
							stable_state_captures = 0
						previous_state = state
						previous_evidence = dom_evidence
						if stable_state_captures >= options.required_stable_states:
							break
					except Exception as error:
						state_capture_errors.append(f'{type(error).__name__}: {error}')
						previous_state = None
						previous_evidence = None
						stable_state_captures = 0
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
		target_errors = evaluate_browser_target(source, final_url, title)
		target_matched = not target_errors
		content_failures: list[str] = []
		contract = source.browser_contract
		if dom_evidence.visible_text_chars < contract.minimum_visible_text_chars:
			content_failures.append(
				f'visible text {dom_evidence.visible_text_chars} is below required {contract.minimum_visible_text_chars}'
			)
		if dom_evidence.meaningful_selector_count < contract.minimum_meaningful_elements:
			content_failures.append(
				f'meaningful elements {dom_evidence.meaningful_selector_count} '
				f'is below required {contract.minimum_meaningful_elements}'
			)
		if dom_evidence.missing_content_markers:
			content_failures.append(f'missing content markers: {", ".join(dom_evidence.missing_content_markers)}')
		content_available = bool(not content_failures and not state_error and error_text is None)
		dom_stable = stable_state_captures >= options.required_stable_states
		classification = classify_browser_page(
			final_url=final_url,
			title=title,
			selector_count=dom_evidence.selector_count,
			state_error=state_error,
			error=error_text,
			meaningful_selector_count=dom_evidence.meaningful_selector_count,
			interactive_element_count=dom_evidence.interactive_element_count,
			minimum_interactive_elements=contract.minimum_interactive_elements,
			target_matched=target_matched,
			content_available=content_available,
			dom_stable=dom_stable,
		)
		reachable = bool(final_url and final_url != 'about:blank' and title and not state_error)
		actionable = classification == BrowserSourceClassification.INTERACTIVE
		contract_failures: list[str] = []
		if not reachable:
			contract_failures.append('browser did not capture a reachable page')
		contract_failures.extend(target_errors)
		if source.test_level == DataSourceTestLevel.BEHAVIORAL:
			contract_failures.extend(content_failures)
			if not dom_stable:
				contract_failures.append(f'DOM did not stabilize for {options.required_stable_states} consecutive captures')
			if not actionable:
				contract_failures.append(f'page classification is {classification.value}, not interactive')
		if error_text:
			contract_failures.append(error_text)
		ok = not contract_failures
		return BrowserDataSourceResult(
			source_id=source.id,
			category=source.category,
			test_level=source.test_level,
			classification=classification,
			reachable=reachable,
			target_matched=target_matched,
			content_available=content_available,
			actionable=actionable,
			dom_stable=dom_stable,
			ok=ok,
			requested_url=str(source.url),
			final_url=final_url,
			title=title,
			selector_count=dom_evidence.selector_count,
			meaningful_selector_count=dom_evidence.meaningful_selector_count,
			interactive_element_count=dom_evidence.interactive_element_count,
			visible_text_chars=dom_evidence.visible_text_chars,
			state_captures=state_captures,
			stable_state_captures=stable_state_captures,
			matched_content_markers=dom_evidence.matched_content_markers,
			missing_content_markers=dom_evidence.missing_content_markers,
			target_errors=target_errors,
			contract_failures=list(dict.fromkeys(contract_failures)),
			browser_mode=browser_runtime_mode(options),
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
	sources = [
		source
		for source in catalog.sources
		if (not options.source_ids or source.id in options.source_ids)
		and (not options.categories or source.category in options.categories)
		and (not options.test_levels or source.test_level in options.test_levels)
	]
	if not sources:
		raise ValueError('Browser probe filters selected no data sources')
	return sources


async def preflight_browser_runtime(
	options: BrowserProbeOptions,
	*,
	session_factory: BrowserSessionFactory = create_browser_session,
) -> BrowserRuntimePreflight:
	"""Launch and cleanly stop one browser before scheduling source-level work."""
	started_at = time.monotonic()
	mode = browser_runtime_mode(options)
	if options.executable_path is not None:
		executable_path = options.executable_path.expanduser()
		if not executable_path.is_file() or not os.access(executable_path, os.X_OK):
			return BrowserRuntimePreflight(
				mode=mode,
				ok=False,
				elapsed_ms=round((time.monotonic() - started_at) * 1000),
				error=f'Browser executable is missing or not executable: {executable_path}',
			)

	error_text: str | None = None
	cleanup_error: str | None = None
	with tempfile.TemporaryDirectory(prefix='nu-browser-probe-preflight-') as profile_directory:
		profile = build_browser_profile(options, Path(profile_directory))
		session = session_factory(profile)
		try:
			await asyncio.wait_for(session.start(), timeout=options.launch_timeout_seconds)
		except Exception as error:
			error_text = f'preflight:{type(error).__name__}: {error}'
		finally:
			try:
				await asyncio.wait_for(session.kill(), timeout=options.shutdown_timeout_seconds)
			except Exception as error:
				cleanup_error = f'preflight-kill:{type(error).__name__}: {error}'
	return BrowserRuntimePreflight(
		mode=mode,
		ok=error_text is None and cleanup_error is None,
		elapsed_ms=round((time.monotonic() - started_at) * 1000),
		error=error_text,
		cleanup_error=cleanup_error,
	)


def build_preflight_failure_result(
	source: DataSourceDefinition,
	options: BrowserProbeOptions,
	preflight: BrowserRuntimePreflight,
) -> BrowserDataSourceResult:
	"""Represent one shared browser preflight failure without misclassifying the website."""
	error = preflight.error or preflight.cleanup_error or 'browser preflight failed'
	return BrowserDataSourceResult(
		source_id=source.id,
		category=source.category,
		test_level=source.test_level,
		classification=BrowserSourceClassification.BROWSER_UNAVAILABLE,
		reachable=False,
		target_matched=False,
		content_available=False,
		actionable=False,
		dom_stable=False,
		ok=False,
		requested_url=str(source.url),
		browser_mode=browser_runtime_mode(options),
		elapsed_ms=preflight.elapsed_ms,
		browser_error=error,
		error=error,
		contract_failures=[error],
	)


async def probe_browser_catalog(
	options: BrowserProbeOptions,
	*,
	session_factory: BrowserSessionFactory = create_browser_session,
) -> BrowserProbeSummary:
	"""Load, filter, and inspect the configured catalog with real browser sessions."""
	catalog = load_data_source_catalog(options.catalog_path)
	sources = select_browser_sources(catalog, options)
	preflight = await preflight_browser_runtime(options, session_factory=session_factory) if options.preflight else None
	if preflight is not None and not preflight.ok:
		results = [build_preflight_failure_result(source, options, preflight) for source in sources]
		return summarize_browser_results(
			catalog,
			results,
			strict=options.strict,
			gate_mode=options.gate_mode,
			preflight=preflight,
		)
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
			index for index, result in enumerate(results) if not browser_result_meets_gate(result, options.gate_mode)
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
				or '; '.join(previous_result.contract_failures)
				or previous_result.classification.value
			)
			results[index] = retry_result.model_copy(
				update={
					'attempts': attempt_number,
					'previous_failures': [*previous_result.previous_failures, previous_failure],
				}
			)
	return summarize_browser_results(
		catalog,
		results,
		strict=options.strict,
		gate_mode=options.gate_mode,
		preflight=preflight,
	)


def print_browser_probe_summary(summary: BrowserProbeSummary) -> None:
	"""Print a compact browser reachability and actionability table."""
	print(
		f'Checked {summary.total} data sources with Chromium: {summary.passed} passed, '
		f'{summary.failed} failed, {summary.reachable} reachable, {summary.target_matched} target-matched, '
		f'{summary.content_available} with content, {summary.actionable} actionable, {summary.stable} stable, '
		f'{summary.infrastructure_failures} browser-runtime failures'
	)
	if summary.preflight is not None:
		print(
			f'Browser preflight: {"PASS" if summary.preflight.ok else "FAIL"} '
			f'({summary.preflight.mode.value}, {summary.preflight.elapsed_ms}ms)'
		)
	for result in summary.results:
		status = 'PASS' if result.ok else 'FAIL'
		detail = result.cleanup_error or result.error or result.state_error or result.title or 'no page evidence'
		print(
			f'{status:4}  {result.category.value:14}  {result.test_level.value:12}  '
			f'{result.source_id:28}  {result.classification.value:15}  '
			f'{result.meaningful_selector_count:4}/{result.selector_count:4} meaningful/selectors  '
			f'{result.stable_state_captures}/{result.state_captures} stable/captures  '
			f'attempt {result.attempts}  {result.elapsed_ms}ms  {detail[:100]}'
		)
	if summary.gate_failures:
		print(f'{summary.gate_mode.value} quality gate failures: {summary.gate_failures}')
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
	parser.add_argument('--required-stable-states', type=int, default=2)
	parser.add_argument('--state-stability-tolerance', type=float, default=0.2)
	parser.add_argument('--state-retry-delay-seconds', type=float, default=1.5)
	parser.add_argument('--shutdown-timeout-seconds', type=float, default=45.0)
	parser.add_argument('--enable-extensions', action='store_true')
	parser.add_argument(
		'--disable-sandbox',
		action='store_true',
		help='Disable the Chromium sandbox only in a trusted container or CI host.',
	)
	parser.add_argument('--executable-path', type=Path, help='Use an explicit local Chrome/Chromium executable.')
	parser.add_argument(
		'--use-cloud',
		action='store_true',
		help='Provision Browser Use Cloud browsers; requires BROWSER_USE_API_KEY.',
	)
	parser.add_argument('--cloud-profile-id')
	parser.add_argument('--cloud-proxy-country-code')
	parser.add_argument('--cloud-timeout-minutes', type=int)
	parser.add_argument('--skip-preflight', action='store_true', help='Skip the shared browser launch preflight.')
	parser.add_argument(
		'--gate-mode',
		choices=[mode.value for mode in BrowserGateMode],
		default=BrowserGateMode.CATALOG.value,
		help='Require catalog, reachability, content, or actionability evidence.',
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
		required_stable_states=arguments.required_stable_states,
		state_stability_tolerance=arguments.state_stability_tolerance,
		state_retry_delay_seconds=arguments.state_retry_delay_seconds,
		shutdown_timeout_seconds=arguments.shutdown_timeout_seconds,
		enable_extensions=arguments.enable_extensions,
		disable_sandbox=arguments.disable_sandbox,
		executable_path=arguments.executable_path,
		use_cloud=arguments.use_cloud,
		cloud_profile_id=arguments.cloud_profile_id,
		cloud_proxy_country_code=arguments.cloud_proxy_country_code,
		cloud_timeout_minutes=arguments.cloud_timeout_minutes,
		preflight=not arguments.skip_preflight,
		gate_mode=BrowserGateMode(arguments.gate_mode),
		strict=arguments.strict,
		json_output=arguments.json_output,
		output_path=arguments.output_path,
	)


def main() -> int:
	"""Run the real-browser probe command and return its quality-gate exit code."""
	try:
		options = parse_options()
		summary = asyncio.run(probe_browser_catalog(options))
	except ValueError as error:
		print(f'Browser probe configuration error: {error}')
		return 2
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
