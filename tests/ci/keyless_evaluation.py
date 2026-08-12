"""API-key-free live browser contract execution for agent evaluation tasks."""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field

from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession
from tests.ci.evaluation_models import (
	EvaluationMode,
	EvaluationReasonCode,
	EvaluationResult,
	EvaluationTask,
	KeylessAction,
	KeylessActionType,
	KeylessCollection,
	KeylessField,
	ValidatorResult,
)

ANTI_BOT_TITLE_MARKERS = ('access denied', 'attention required', 'just a moment', 'request blocked')


class KeylessRunnerOptions(BaseModel):
	"""Validated resource and retry controls for deterministic browser evaluations."""

	model_config = ConfigDict(extra='forbid')

	disable_sandbox: bool = False
	launch_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
	navigation_timeout_seconds: float = Field(default=45.0, gt=0, le=180)
	state_timeout_seconds: float = Field(default=45.0, ge=35.0, le=180)
	shutdown_timeout_seconds: float = Field(default=45.0, ge=35.0, le=180)
	attempts: int = Field(default=2, ge=1, le=3)
	retry_delay_seconds: float = Field(default=2.0, ge=0, le=30)


def load_evaluation_task(task_path: str | Path) -> EvaluationTask:
	"""Load one YAML task and validate its declarative keyless contract."""
	path = Path(task_path)
	return EvaluationTask.model_validate(yaml.safe_load(path.read_text(encoding='utf-8')))


async def _evaluate_value(session: BrowserSession, expression: str) -> object:
	"""Evaluate bounded browser JavaScript and return its JSON-compatible value."""
	cdp_session = await session.get_or_create_cdp_session()
	result = await cdp_session.cdp_client.send.Runtime.evaluate(
		params={'expression': expression, 'returnByValue': True, 'awaitPromise': True},
		session_id=cdp_session.session_id,
	)
	if result.get('exceptionDetails'):
		raise RuntimeError(f'JavaScript evaluation failed: {result["exceptionDetails"].get("text", "unknown error")}')
	return result.get('result', {}).get('value')


async def _run_action(session: BrowserSession, action: KeylessAction) -> tuple[bool, str]:
	"""Execute one allow-listed declarative action without evaluating YAML-provided code."""
	if action.action == KeylessActionType.WAIT:
		await asyncio.sleep(action.wait_after_seconds)
		return True, f'waited {action.wait_after_seconds:.1f}s'
	if action.action == KeylessActionType.NAVIGATE:
		assert action.url is not None
		await session.navigate_to(action.url)
		await asyncio.sleep(action.wait_after_seconds)
		return True, f'navigated to {action.url}'

	selectors_json = json.dumps(action.selectors)
	if action.action == KeylessActionType.FILL:
		value_json = json.dumps(action.value)
		expression = f"""
(() => {{
  const selectors = {selectors_json};
  const element = selectors.map(selector => document.querySelector(selector)).find(Boolean);
  if (!element) return false;
  element.focus();
  const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
  if (setter) setter.call(element, {value_json}); else element.value = {value_json};
  element.dispatchEvent(new Event('input', {{bubbles: true}}));
  element.dispatchEvent(new Event('change', {{bubbles: true}}));
  return true;
}})()
"""
	else:
		expression = f"""
(() => {{
  const selectors = {selectors_json};
  const element = selectors.map(selector => document.querySelector(selector)).find(Boolean);
  if (!element) return false;
  if ({json.dumps(action.action == KeylessActionType.SUBMIT)}) {{
    const form = element.closest('form');
    if (form?.requestSubmit) form.requestSubmit(); else element.click();
  }} else {{
    element.click();
  }}
  return true;
}})()
"""

	succeeded = bool(await _evaluate_value(session, expression))
	if not succeeded and action.fallback_url:
		await session.navigate_to(action.fallback_url)
		await asyncio.sleep(action.wait_after_seconds)
		return True, f'{action.action.value} selector unavailable; navigated to fallback URL'
	if succeeded:
		await asyncio.sleep(action.wait_after_seconds)
	return succeeded, f'{action.action.value} matched={succeeded}'


def _apply_pattern(value: object, pattern: str | None) -> object:
	"""Extract a regex match from scalar or list evidence when configured."""
	if pattern is None:
		return value
	match = re.search(pattern, str(value), flags=re.IGNORECASE | re.MULTILINE)
	if not match:
		return ''
	return match.group(1) if match.lastindex else match.group(0)


async def _extract_field(session: BrowserSession, field: KeylessField, *, root_index: int | None = None) -> object:
	"""Extract one scalar field from the document or a selected collection root."""
	if field.attribute == 'title':
		return await session.get_current_page_title()
	if field.attribute == 'url':
		return await session.get_current_page_url()

	selectors_json = json.dumps(field.selectors)
	root_selectors_json = json.dumps(field.root_selectors)
	root_index_json = json.dumps(root_index)
	attribute_json = json.dumps(field.attribute)
	expression = f"""
(() => {{
  const rootSelectors = {root_selectors_json};
  const rootIndex = {root_index_json};
  let root = document;
  if (rootSelectors.length) {{
    const roots = rootSelectors.map(selector => [...document.querySelectorAll(selector)]).find(items => items.length) || [];
    root = roots[rootIndex === null ? 0 : rootIndex];
  }}
  if (!root) return '';
  const selectors = {selectors_json};
  const element = selectors.length ? selectors.map(selector => root.querySelector(selector)).find(Boolean) : root;
  if (!element) return '';
  const attribute = {attribute_json};
  if (attribute === 'href') return element.href || element.getAttribute('href') || '';
  if (attribute === 'value') return element.value || element.getAttribute('value') || '';
  return (element.innerText || element.textContent || '').trim();
}})()
"""
	return _apply_pattern(await _evaluate_value(session, expression), field.pattern)


async def _extract_collection(session: BrowserSession, collection: KeylessCollection) -> list[dict[str, object]]:
	"""Extract a bounded collection using the first selector that produces items."""
	root_selectors_json = json.dumps(collection.root_selectors)
	count_expression = f"""
(() => {{
  const selectors = {root_selectors_json};
  const roots = selectors.map(selector => [...document.querySelectorAll(selector)]).find(items => items.length) || [];
  return roots.length;
}})()
"""
	root_count_value = await _evaluate_value(session, count_expression)
	root_count = min(int(root_count_value) if isinstance(root_count_value, int | float | str) else 0, collection.limit)
	items: list[dict[str, object]] = []
	for index in range(root_count):
		item: dict[str, object] = {}
		for field in collection.fields:
			rooted_field = field.model_copy(update={'root_selectors': collection.root_selectors})
			item[field.name] = await _extract_field(session, rooted_field, root_index=index)
		items.append(item)
	return items


def _is_nonempty(value: object) -> bool:
	"""Return whether extracted evidence contains a usable scalar or collection value."""
	if isinstance(value, str):
		return bool(value.strip())
	if isinstance(value, list):
		return bool(value)
	return value is not None


async def _capture_state(
	session: BrowserSession,
	task: EvaluationTask,
	state_timeout_seconds: float,
) -> tuple[str, str, int, str]:
	"""Capture final URL, title, selector count, and bounded page text with retries."""
	contract = task.keyless
	last_error: Exception | None = None
	for attempt in range(contract.state_attempts):
		try:
			state = await asyncio.wait_for(
				session.get_browser_state_summary(include_screenshot=False),
				timeout=state_timeout_seconds,
			)
			direct_url = str(await _evaluate_value(session, 'location.href') or state.url)
			direct_title = str(await _evaluate_value(session, 'document.title') or state.title)
			body_text = str(await _evaluate_value(session, "document.body ? document.body.innerText.slice(0, 500000) : ''") or '')
			if direct_title and direct_url and direct_url != 'about:blank':
				return direct_url, direct_title, len(state.dom_state.selector_map), body_text
		except Exception as error:
			last_error = error
		if attempt + 1 < contract.state_attempts:
			await asyncio.sleep(1.5)
	if last_error:
		raise last_error
	raise RuntimeError('page state remained empty after all capture attempts')


async def _run_keyless_attempt(
	task: EvaluationTask,
	task_file: str,
	options: KeylessRunnerOptions,
) -> EvaluationResult:
	"""Execute a task once inside an isolated Chromium profile."""
	started_at = time.monotonic()
	trace: list[dict[str, object]] = []
	validators: list[ValidatorResult] = []
	session: BrowserSession | None = None

	with tempfile.TemporaryDirectory(prefix=f'nu-keyless-eval-{task.source_id}-') as profile_directory:
		try:
			profile = BrowserProfile(
				headless=True,
				user_data_dir=Path(profile_directory),
				keep_alive=False,
				enable_default_extensions=False,
				chromium_sandbox=not options.disable_sandbox,
			)
			session = BrowserSession(browser_profile=profile)
			await asyncio.wait_for(session.start(), timeout=options.launch_timeout_seconds)
		except Exception as error:
			return EvaluationResult(
				file=task_file,
				status='skipped',
				explanation=f'Browser unavailable before navigation: {type(error).__name__}: {error}',
				mode=EvaluationMode.DETERMINISTIC,
				reason_code=EvaluationReasonCode.BROWSER_UNAVAILABLE,
				source_id=task.source_id,
				duration_ms=round((time.monotonic() - started_at) * 1000),
			)

		try:
			await asyncio.wait_for(
				session.navigate_to(task.keyless.start_url),
				timeout=options.navigation_timeout_seconds,
			)
			await asyncio.sleep(task.keyless.wait_after_navigation_seconds)
			trace.append({'action': 'navigate', 'url': task.keyless.start_url, 'succeeded': True})

			for action in task.keyless.actions:
				try:
					succeeded, detail = await asyncio.wait_for(
						_run_action(session, action),
						timeout=options.navigation_timeout_seconds,
					)
				except Exception as error:
					succeeded = False
					detail = f'{type(error).__name__}: {error}'
				trace.append({'action': action.action.value, 'succeeded': succeeded, 'detail': detail})
				if not succeeded and not action.optional:
					raise RuntimeError(f'required action failed: {detail}')

			final_url, title, selector_count, body_text = await _capture_state(
				session,
				task,
				options.state_timeout_seconds,
			)
			hostname = (urlsplit(final_url).hostname or '').casefold()
			domain_passed = any(
				hostname == domain.casefold() or hostname.endswith(f'.{domain.casefold()}')
				for domain in task.keyless.expected_domains
			)
			validators.append(
				ValidatorResult(name='expected_domain', passed=domain_passed, detail=f'final host: {hostname or "missing"}')
			)
			validators.append(
				ValidatorResult(
					name='selector_count',
					passed=selector_count >= task.keyless.min_selector_count,
					detail=f'{selector_count} >= {task.keyless.min_selector_count}',
				)
			)
			blocked = any(marker in title.casefold() for marker in ANTI_BOT_TITLE_MARKERS)
			if blocked and task.keyless.blocked_snapshot is not None:
				snapshot = task.keyless.blocked_snapshot
				snapshot_url_matches = snapshot.source_url == task.keyless.start_url
				validators.append(
					ValidatorResult(
						name='snapshot_source_url',
						passed=snapshot_url_matches,
						detail=f'reviewed {snapshot.reviewed_at.isoformat()}',
					)
				)
				return EvaluationResult(
					file=task_file,
					status='passed' if domain_passed and snapshot_url_matches else 'failed',
					explanation=(
						f'Live source returned {title!r}; used hash-verified snapshot reviewed {snapshot.reviewed_at.isoformat()}'
					),
					mode=EvaluationMode.DETERMINISTIC,
					reason_code=EvaluationReasonCode.SNAPSHOT_FALLBACK,
					source_id=task.source_id,
					duration_ms=round((time.monotonic() - started_at) * 1000),
					output={
						'url': final_url,
						'title': title,
						'selector_count': selector_count,
						'evidence_origin': 'hash_verified_snapshot',
						**snapshot.output,
					},
					validators=validators,
					trace=trace,
				)

			for pattern in task.keyless.required_text_patterns:
				matched = re.search(pattern, body_text, flags=re.IGNORECASE | re.MULTILINE) is not None
				validators.append(ValidatorResult(name='page_text_pattern', passed=matched, detail=f'pattern={pattern!r}'))

			output: dict[str, object] = {
				'url': final_url,
				'title': title,
				'selector_count': selector_count,
			}
			for field in task.keyless.fields:
				value = await _extract_field(session, field)
				output[field.name] = value
				if field.required:
					validators.append(
						ValidatorResult(
							name=f'field:{field.name}',
							passed=_is_nonempty(value),
							detail='non-empty' if _is_nonempty(value) else 'empty',
						)
					)

			for collection in task.keyless.collections:
				items = await _extract_collection(session, collection)
				output[collection.name] = items
				validators.append(
					ValidatorResult(
						name=f'collection:{collection.name}',
						passed=len(items) >= collection.min_items,
						detail=f'{len(items)} >= {collection.min_items}',
					)
				)
				for index, item in enumerate(items):
					for field in collection.fields:
						if field.required:
							value = item.get(field.name)
							validators.append(
								ValidatorResult(
									name=f'collection:{collection.name}[{index}].{field.name}',
									passed=_is_nonempty(value),
									detail='non-empty' if _is_nonempty(value) else 'empty',
								)
							)

			if blocked:
				return EvaluationResult(
					file=task_file,
					status='failed',
					explanation=f'Source returned an anti-bot page: {title}',
					mode=EvaluationMode.DETERMINISTIC,
					reason_code=EvaluationReasonCode.SOURCE_BLOCKED,
					source_id=task.source_id,
					duration_ms=round((time.monotonic() - started_at) * 1000),
					output=output,
					validators=validators,
					trace=trace,
				)

			failed_validators = [validator for validator in validators if not validator.passed]
			status: Literal['passed', 'failed'] = 'failed' if failed_validators else 'passed'
			explanation = (
				'; '.join(f'{validator.name}: {validator.detail}' for validator in failed_validators)
				if failed_validators
				else f'{len(validators)} deterministic validators passed'
			)
			return EvaluationResult(
				file=task_file,
				status=status,
				explanation=explanation,
				mode=EvaluationMode.DETERMINISTIC,
				reason_code=(EvaluationReasonCode.ASSERTION_FAILED if failed_validators else EvaluationReasonCode.COMPLETED),
				source_id=task.source_id,
				duration_ms=round((time.monotonic() - started_at) * 1000),
				output=output,
				validators=validators,
				trace=trace,
			)
		except Exception as error:
			return EvaluationResult(
				file=task_file,
				status='failed',
				explanation=f'Keyless task failed: {type(error).__name__}: {error}',
				mode=EvaluationMode.DETERMINISTIC,
				reason_code=EvaluationReasonCode.AGENT_FAILED,
				source_id=task.source_id,
				duration_ms=round((time.monotonic() - started_at) * 1000),
				validators=validators,
				trace=trace,
			)
		finally:
			if session is not None:
				try:
					await asyncio.wait_for(session.kill(), timeout=options.shutdown_timeout_seconds)
				except Exception:
					pass


async def run_keyless_task(
	task_path: str | Path,
	options: KeylessRunnerOptions,
) -> EvaluationResult:
	"""Run a deterministic browser contract with bounded fresh-profile retries."""
	path = Path(task_path)
	try:
		task = load_evaluation_task(path)
	except Exception as error:
		return EvaluationResult(
			file=path.name,
			status='failed',
			explanation=f'Invalid task: {type(error).__name__}: {error}',
			mode=EvaluationMode.DETERMINISTIC,
			reason_code=EvaluationReasonCode.INVALID_TASK,
		)

	previous_explanations: list[str] = []
	last_result: EvaluationResult | None = None
	for attempt in range(1, options.attempts + 1):
		result = await _run_keyless_attempt(task, path.name, options)
		result = result.model_copy(update={'attempts': attempt})
		last_result = result
		if result.success:
			if previous_explanations:
				result = result.model_copy(
					update={'explanation': f'{result.explanation}; recovered after: {" | ".join(previous_explanations)}'}
				)
			return result
		previous_explanations.append(result.explanation)
		if attempt < options.attempts and options.retry_delay_seconds:
			await asyncio.sleep(options.retry_delay_seconds)
	assert last_result is not None
	return last_result
