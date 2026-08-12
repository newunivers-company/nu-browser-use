"""Tests for browser-agent evaluation scoring and quality gates."""

from tests.ci.evaluate_tasks import EvaluationResult, EvaluationRunOptions, EvaluationSummary, quality_gate_thresholds
from tests.ci.evaluation_models import EvaluationMode


def test_skipped_result_is_not_successful() -> None:
	"""Missing credentials must never inflate the passed task count."""
	result = EvaluationResult(file='task.yaml', status='skipped', explanation='missing secret')

	assert result.success is False


def test_quality_gate_requires_actual_executions() -> None:
	"""An all-skipped evaluation run must fail the minimum execution gate."""
	summary = EvaluationSummary(passed=0, failed=0, skipped=5, total=5)

	errors = summary.quality_gate_errors(minimum_pass_rate=0.6, minimum_executed_tasks=3)

	assert any('only 0 tasks executed' in error for error in errors)
	assert any('pass rate 0.0%' in error for error in errors)


def test_quality_gate_uses_executed_task_pass_rate() -> None:
	"""Skipped tasks are visible but excluded from the behavioral pass-rate denominator."""
	summary = EvaluationSummary(passed=3, failed=1, skipped=1, total=5)

	assert summary.executed == 4
	assert summary.pass_rate == 0.75
	assert summary.quality_gate_errors(minimum_pass_rate=0.6, minimum_executed_tasks=3) == []


def test_keyless_quality_gate_requires_every_task_to_pass() -> None:
	"""Deterministic and replay lanes are mandatory coverage, not probabilistic model evaluations."""
	summary = EvaluationSummary(mode=EvaluationMode.DETERMINISTIC, passed=16, failed=1, skipped=0, total=17)
	minimum_pass_rate, minimum_executed_tasks = quality_gate_thresholds(EvaluationRunOptions(), summary)

	assert minimum_pass_rate == 1.0
	assert minimum_executed_tasks == 17
	assert summary.quality_gate_errors(
		minimum_pass_rate=minimum_pass_rate,
		minimum_executed_tasks=minimum_executed_tasks,
	)
