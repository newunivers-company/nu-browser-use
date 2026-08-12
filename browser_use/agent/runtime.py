"""Shared runtime configuration helpers for Python and Rust-backed agents."""

import warnings
from collections.abc import Mapping, Set
from typing import Any, TypeVar

from browser_use.llm.base import BaseChatModel

Context = TypeVar('Context')


def resolve_agent_context(
	context: Context | None,
	legacy_arguments: Mapping[str, Any],
	*,
	allowed_legacy_arguments: Set[str] = frozenset(),
) -> tuple[Context | None, dict[str, Any]]:
	"""Resolve the deprecated context alias and reject misspelled agent arguments.

	The returned mapping contains only explicitly allowed compatibility arguments.
	Keeping this validation shared prevents the Python and Rust-backed agents from
	silently diverging when their constructors evolve.
	"""
	remaining_arguments = dict(legacy_arguments)
	legacy_context = remaining_arguments.pop('custom_context', None)
	if legacy_context is not None:
		if context is not None:
			raise ValueError('Cannot specify both "context" and deprecated "custom_context".')
		warnings.warn(
			'custom_context is deprecated; pass context=... instead.',
			DeprecationWarning,
			stacklevel=3,
		)
		context = legacy_context

	unexpected_arguments = sorted(set(remaining_arguments) - set(allowed_legacy_arguments))
	if unexpected_arguments:
		unexpected = ', '.join(unexpected_arguments)
		raise TypeError(f'Unexpected Agent argument(s): {unexpected}')

	return context, {name: remaining_arguments[name] for name in allowed_legacy_arguments if name in remaining_arguments}


def resolve_default_llm(llm: BaseChatModel | None) -> BaseChatModel:
	"""Return the configured LLM, defaulting to the shared keyless runtime."""
	if llm is not None:
		return llm

	from browser_use.config import CONFIG

	if CONFIG.DEFAULT_LLM:
		from browser_use.llm.models import get_llm_by_name

		return get_llm_by_name(CONFIG.DEFAULT_LLM)

	from browser_use.runtime import resolve_runtime_llm

	return resolve_runtime_llm()


def resolve_llm_timeout(llm: BaseChatModel | Any) -> int:
	"""Return the model-family timeout used by both agent implementations."""
	model_name = str(getattr(llm, 'model', '') or '').lower()
	if 'gemini' in model_name:
		if '3-pro' in model_name:
			return 90
		return 75
	if 'groq' in model_name:
		return 30
	if any(model_family in model_name for model_family in ('o3', 'claude', 'sonnet', 'deepseek')):
		return 90
	return 75
