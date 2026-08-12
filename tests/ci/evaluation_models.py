"""Validated models shared by cloud, local, replay, and keyless evaluations."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvaluationBrowserOptions(BaseModel):
	"""Validated browser runtime shared by every evaluation mode."""

	model_config = ConfigDict(extra='forbid')

	disable_sandbox: bool = False
	executable_path: Path | None = None
	use_cloud_browser: bool = False
	cloud_profile_id: str | None = None
	cloud_proxy_country_code: str | None = None
	cloud_timeout_minutes: int | None = Field(default=None, ge=1, le=240)
	minimum_page_load_wait_seconds: float = Field(default=1.0, ge=0, le=10)
	network_idle_wait_seconds: float = Field(default=1.0, ge=0, le=10)

	@model_validator(mode='after')
	def validate_browser_runtime(self) -> EvaluationBrowserOptions:
		"""Reject local/cloud combinations that cannot represent one browser runtime."""
		if self.use_cloud_browser and self.executable_path is not None:
			raise ValueError('executable_path cannot be combined with use_cloud_browser')
		if self.use_cloud_browser and self.disable_sandbox:
			raise ValueError('disable_sandbox is a local-browser option and cannot be combined with use_cloud_browser')
		if not self.use_cloud_browser and any(
			value is not None for value in (self.cloud_profile_id, self.cloud_proxy_country_code, self.cloud_timeout_minutes)
		):
			raise ValueError('cloud browser options require use_cloud_browser=True')
		return self


class EvaluationMode(StrEnum):
	"""Supported levels of browser-agent evaluation."""

	AUTO = 'auto'
	HYBRID = 'hybrid'
	DETERMINISTIC = 'deterministic'
	REPLAY = 'replay'
	LOCAL = 'local'
	SUBSCRIPTION = 'subscription'
	CLOUD = 'cloud'


class LocalEvaluationProvider(StrEnum):
	"""Keyless local model protocols supported by the evaluation runner."""

	AUTO = 'auto'
	OLLAMA = 'ollama'
	OPENAI_LIKE = 'openai_like'


class EvaluationReasonCode(StrEnum):
	"""Machine-readable reason for an evaluation outcome."""

	COMPLETED = 'completed'
	ASSERTION_FAILED = 'assertion_failed'
	AGENT_FAILED = 'agent_failed'
	BROWSER_UNAVAILABLE = 'browser_unavailable'
	SOURCE_BLOCKED = 'source_blocked'
	PROVIDER_UNAVAILABLE = 'provider_unavailable'
	JUDGE_UNAVAILABLE = 'judge_unavailable'
	REPLAY_UNAVAILABLE = 'replay_unavailable'
	SNAPSHOT_FALLBACK = 'snapshot_fallback'
	INVALID_TASK = 'invalid_task'


class KeylessActionType(StrEnum):
	"""Safe declarative actions accepted by keyless contracts."""

	NAVIGATE = 'navigate'
	CLICK = 'click'
	FILL = 'fill'
	SUBMIT = 'submit'
	WAIT = 'wait'


class KeylessAction(BaseModel):
	"""One bounded browser action without executable code from task YAML."""

	model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

	action: KeylessActionType
	url: str | None = None
	selectors: list[str] = Field(default_factory=list)
	value: str | None = None
	optional: bool = False
	wait_after_seconds: float = Field(default=1.0, ge=0, le=10)
	fallback_url: str | None = None

	@model_validator(mode='after')
	def validate_action_inputs(self) -> KeylessAction:
		"""Reject ambiguous or unsafe declarative action definitions."""
		if self.action == KeylessActionType.NAVIGATE and not self.url:
			raise ValueError('navigate actions require url')
		if self.action in {KeylessActionType.CLICK, KeylessActionType.FILL, KeylessActionType.SUBMIT} and not self.selectors:
			raise ValueError(f'{self.action.value} actions require selectors')
		if self.action == KeylessActionType.FILL and self.value is None:
			raise ValueError('fill actions require value')
		return self


class KeylessField(BaseModel):
	"""A scalar value extracted from the first matching page element."""

	model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

	name: str = Field(pattern=r'^[a-z][a-z0-9_]*$')
	selectors: list[str] = Field(default_factory=list)
	root_selectors: list[str] = Field(default_factory=list)
	attribute: Literal['text', 'href', 'value', 'title', 'url'] = 'text'
	pattern: str | None = None
	required: bool = True


class KeylessCollection(BaseModel):
	"""A repeated structured extraction rooted at page item selectors."""

	model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

	name: str = Field(pattern=r'^[a-z][a-z0-9_]*$')
	root_selectors: list[str] = Field(min_length=1)
	fields: list[KeylessField] = Field(min_length=1)
	min_items: int = Field(default=1, ge=1, le=20)
	limit: int = Field(default=3, ge=1, le=20)


class KeylessSnapshot(BaseModel):
	"""Small reviewed evidence fallback for a live page that is consistently bot-blocked."""

	model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

	reviewed_at: date
	source_url: str
	output: dict[str, str] = Field(min_length=1)
	sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

	@model_validator(mode='after')
	def validate_evidence_hash(self) -> KeylessSnapshot:
		"""Detect accidental or unreviewed modification of checked-in fallback values."""
		canonical = json.dumps(self.output, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
		if hashlib.sha256(canonical).hexdigest() != self.sha256:
			raise ValueError('snapshot sha256 does not match its canonical output')
		return self


class KeylessContract(BaseModel):
	"""Machine-verifiable browser contract used when remote LLM keys are absent."""

	model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

	start_url: str
	expected_domains: list[str] = Field(min_length=1)
	actions: list[KeylessAction] = Field(default_factory=list)
	fields: list[KeylessField] = Field(default_factory=list)
	collections: list[KeylessCollection] = Field(default_factory=list)
	required_text_patterns: list[str] = Field(default_factory=list)
	min_selector_count: int = Field(default=1, ge=0)
	state_attempts: int = Field(default=3, ge=1, le=5)
	wait_after_navigation_seconds: float = Field(default=1.0, ge=0, le=10)
	blocked_snapshot: KeylessSnapshot | None = None

	@model_validator(mode='after')
	def require_evidence_definition(self) -> KeylessContract:
		"""Require every contract to extract or assert task-specific evidence."""
		if not self.fields and not self.collections and not self.required_text_patterns:
			raise ValueError('keyless contracts require fields, collections, or required_text_patterns')
		return self


class EvaluationTask(BaseModel):
	"""Validated browser-agent task with an API-key-free execution contract."""

	model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

	name: str
	source_id: str = Field(pattern=r'^[a-z][a-z0-9_]*$')
	task: str
	judge_context: list[str] = Field(min_length=1)
	max_steps: int = Field(default=15, ge=1, le=500)
	keyless: KeylessContract
	replay_history_path: Path | None = None


class ValidatorResult(BaseModel):
	"""One deterministic assertion and its bounded evidence."""

	name: str
	passed: bool
	detail: str


class EvaluationResult(BaseModel):
	"""Outcome of one evaluation task across every supported execution mode."""

	file: str
	status: Literal['passed', 'failed', 'skipped']
	explanation: str
	mode: EvaluationMode = EvaluationMode.HYBRID
	reason_code: EvaluationReasonCode = EvaluationReasonCode.COMPLETED
	source_id: str | None = None
	duration_ms: int = Field(default=0, ge=0)
	attempts: int = Field(default=1, ge=1)
	output: dict[str, object] = Field(default_factory=dict)
	validators: list[ValidatorResult] = Field(default_factory=list)
	trace: list[dict[str, object]] = Field(default_factory=list)

	@property
	def success(self) -> bool:
		"""Return whether the task was actually executed and passed."""
		return self.status == 'passed'


class EvaluationSummary(BaseModel):
	"""Aggregate evaluation score and quality-gate inputs."""

	mode: EvaluationMode = EvaluationMode.HYBRID
	passed: int
	failed: int
	skipped: int
	total: int
	results: list[EvaluationResult] = Field(default_factory=list)

	@property
	def executed(self) -> int:
		"""Return the number of tasks that produced a pass/fail judgement."""
		return self.passed + self.failed

	@property
	def pass_rate(self) -> float:
		"""Return pass rate over executed tasks only."""
		return self.passed / self.executed if self.executed else 0.0

	def quality_gate_errors(self, *, minimum_pass_rate: float, minimum_executed_tasks: int) -> list[str]:
		"""Describe every quality-gate requirement not met by this summary."""
		errors: list[str] = []
		if self.executed < minimum_executed_tasks:
			errors.append(f'only {self.executed} tasks executed; at least {minimum_executed_tasks} are required')
		if self.pass_rate < minimum_pass_rate:
			errors.append(f'pass rate {self.pass_rate:.1%} is below the required {minimum_pass_rate:.1%}')
		return errors
