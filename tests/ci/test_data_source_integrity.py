"""Integrity tests for every file-backed test data source in the repository."""

import json
from pathlib import Path
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from scripts.data_source_catalog import DataSourceCategory, DataSourceTestLevel, load_data_source_catalog
from tests.ci.evaluation_models import EvaluationTask

TESTS_DIR = Path(__file__).resolve().parents[1]
AGENT_TASKS_DIR = TESTS_DIR / 'agent_tasks'
MIND2WEB_DATASET_PATH = TESTS_DIR / 'mind2web_data' / 'processed.json'
BROWSER_FIXTURES_DIR = Path(__file__).resolve().parent / 'browser'
DATA_SOURCE_CATALOG_PATH = TESTS_DIR / 'data_sources.yaml'
HIGH_DIFFICULTY_BROWSER_SOURCE_IDS = {
	'amazon',
	'discord_discovery',
	'langsmith_hub',
	'mastodon_explore',
	'product_hunt',
	'stack_overflow_hot',
	'tiktok_explore',
	'youtube_trending',
}


class Mind2WebTask(BaseModel):
	"""Validated task and action sequence from the bundled Mind2Web subset."""

	model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

	website: str = Field(min_length=1)
	id: UUID
	domain: str = Field(min_length=1)
	subdomain: str = Field(min_length=1)
	confirmed_task: str = Field(min_length=1)
	action_reprs: list[str] = Field(min_length=1)


def test_all_agent_evaluation_sources_are_valid_and_unique() -> None:
	"""Validate every live evaluation YAML and reject duplicate scenarios."""
	task_paths = sorted(AGENT_TASKS_DIR.glob('*.yaml'))
	assert len(task_paths) >= 17, 'The live evaluation source set was unexpectedly reduced'

	tasks = [EvaluationTask.model_validate(yaml.safe_load(path.read_text(encoding='utf-8'))) for path in task_paths]
	names = [task.name for task in tasks]
	prompts = [task.task for task in tasks]

	assert len(names) == len(set(names)), 'Evaluation task names must be unique'
	assert len(prompts) == len(set(prompts)), 'Evaluation task prompts must be unique'
	assert all(task.keyless.expected_domains for task in tasks)
	assert all(task.keyless.fields or task.keyless.collections or task.keyless.required_text_patterns for task in tasks)
	assert sum(task.keyless.blocked_snapshot is not None for task in tasks) <= 1, (
		'Keyless evaluation must remain predominantly live; reviewed snapshots are exceptional fallbacks'
	)


def test_catalog_covers_all_behavioral_social_and_prompt_sources() -> None:
	"""Require broad catalog coverage and a task for every behavioral source."""
	catalog = load_data_source_catalog(DATA_SOURCE_CATALOG_PATH)
	assert len(catalog.sources) >= 48, 'The external data source catalog was unexpectedly reduced'

	social_sources = [source for source in catalog.sources if source.category == DataSourceCategory.SOCIAL_MEDIA]
	prompt_sources = [source for source in catalog.sources if source.category == DataSourceCategory.PROMPT_LIBRARY]
	assert len(social_sources) >= 24, 'Social-media coverage was unexpectedly reduced'
	assert len(prompt_sources) >= 20, 'Prompt-source coverage was unexpectedly reduced'

	task_paths = sorted(AGENT_TASKS_DIR.glob('*.yaml'))
	tasks = [EvaluationTask.model_validate(yaml.safe_load(path.read_text(encoding='utf-8'))) for path in task_paths]
	task_source_ids = {task.source_id for task in tasks}
	unknown_source_ids = task_source_ids - catalog.by_id.keys()
	assert not unknown_source_ids, f'Evaluation tasks reference unknown data sources: {sorted(unknown_source_ids)}'

	behavioral_source_ids = {source.id for source in catalog.sources if source.test_level == DataSourceTestLevel.BEHAVIORAL}
	assert task_source_ids == behavioral_source_ids, (
		'Every behavioral source must have a live evaluation task and availability-only sources must not be '
		'present in the live task set'
	)


def test_high_difficulty_browser_sources_define_semantic_contracts() -> None:
	"""Keep target fidelity and meaningful rendered-evidence requirements explicit."""
	catalog = load_data_source_catalog(DATA_SOURCE_CATALOG_PATH)
	for source_id in HIGH_DIFFICULTY_BROWSER_SOURCE_IDS:
		contract = catalog.by_id[source_id].browser_contract
		assert contract.allowed_final_path_prefixes, f'{source_id} must constrain its final browser path'
		assert contract.expected_title_markers, f'{source_id} must declare expected title evidence'
		assert contract.required_content_markers, f'{source_id} must declare rendered content evidence'
		assert contract.minimum_visible_text_chars >= 100
		assert contract.minimum_meaningful_elements >= 3
		assert contract.minimum_interactive_elements >= 1


def test_mind2web_dataset_is_complete_well_formed_and_unique() -> None:
	"""Validate all Mind2Web records so the bundled dataset cannot silently rot."""
	raw_records = json.loads(MIND2WEB_DATASET_PATH.read_text(encoding='utf-8'))
	records = TypeAdapter(list[Mind2WebTask]).validate_python(raw_records)

	assert len(records) >= 1_000, 'The bundled Mind2Web dataset was unexpectedly truncated'
	assert len({record.id for record in records}) == len(records), 'Mind2Web record IDs must be unique'
	assert len({(record.website, record.confirmed_task) for record in records}) == len(records), (
		'Mind2Web website/task pairs must be unique'
	)
	assert all(action.strip() for record in records for action in record.action_reprs)


def test_static_browser_html_sources_are_nonempty_documents() -> None:
	"""Ensure every checked-in browser HTML fixture remains a usable document."""
	fixture_paths = sorted(BROWSER_FIXTURES_DIR.glob('*.html'))
	assert fixture_paths, 'At least one static browser HTML fixture is required'

	for fixture_path in fixture_paths:
		content = fixture_path.read_text(encoding='utf-8').lower()
		assert '<html' in content, f'{fixture_path.name} is missing an <html> root'
		assert '<body' in content, f'{fixture_path.name} is missing a <body> element'
