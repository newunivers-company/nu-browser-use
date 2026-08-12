"""Tests for code-level live browser evidence completion gates."""

from browser_use import Agent
from browser_use.agent.views import ActionResult, AgentHistory, AgentHistoryList
from browser_use.browser.views import BrowserStateHistory
from browser_use.llm.subscription_cli import ChatSubscriptionCLI, SubscriptionCLIProvider


def make_completed_history(*, state_message: str, final_output: str) -> AgentHistoryList:
	"""Build one browser completion history with the pre-action live state retained."""
	wrapped_state_message = (
		'<user_request>Search prompts.chat for Linux Terminal.</user_request>\n'
		'<agent_history>prior model memory</agent_history>\n'
		f'<browser_state>{state_message}</browser_state>'
	)
	return AgentHistoryList(
		history=[
			AgentHistory(
				model_output=None,
				result=[ActionResult(is_done=True, success=True, extracted_content=final_output)],
				state=BrowserStateHistory(
					url='https://prompts.chat/prompts?q=Linux%20Terminal',
					title='Linux Terminal prompt',
					tabs=[],
					interacted_element=[],
				),
				state_message=wrapped_state_message,
			)
		]
	)


def test_live_evidence_gate_accepts_terms_observed_in_current_page_state() -> None:
	"""A successful answer is grounded when its novel values occur in pre-action browser state."""
	history = make_completed_history(
		state_message='Linux Terminal — I want you to act as a linux terminal and return commands.',
		final_output='Title: Linux Terminal. First sentence: I want you to act as a linux terminal.',
	)

	gate = history.live_evidence('Search prompts.chat for Linux Terminal and return the title and first sentence.')

	assert gate.passed is True
	assert {'want', 'act'}.issubset(gate.matched_output_terms)
	assert gate.records[0].content_sha256
	assert not hasattr(gate.records[0], 'content')


def test_live_evidence_gate_rejects_unobserved_model_memory() -> None:
	"""A plausible answer from model memory cannot pass when the live page never exposed its values."""
	history = make_completed_history(
		state_message='Search results are still loading. No prompt body is available.',
		final_output='Title: Linux Terminal. First sentence: I want you to act as a linux terminal.',
	)

	gate = history.live_evidence('Search prompts.chat for Linux Terminal and return the title and first sentence.')

	assert gate.passed is False
	assert 'required output terms' in gate.reason


def test_live_evidence_gate_excludes_agent_memory_from_page_proof() -> None:
	"""Terms copied into agent history cannot masquerade as visible DOM evidence."""
	history = make_completed_history(
		state_message='Search results are still loading.',
		final_output='Title: Linux Terminal. First sentence: I want you to act as a linux terminal.',
	)
	history.history[0].state_message = (
		'<agent_history>I want you to act as a linux terminal.</agent_history>'
		'<browser_state>Search results are still loading.</browser_state>'
	)

	gate = history.live_evidence('Search prompts.chat for Linux Terminal and return the title and first sentence.')

	assert gate.passed is False


def test_agent_code_gate_downgrades_unsupported_success() -> None:
	"""The Agent changes its own success result before an external judge or callback sees it."""
	agent = Agent(
		task='Search prompts.chat for Linux Terminal and return the title and first sentence.',
		llm=ChatSubscriptionCLI(provider_name=SubscriptionCLIProvider.CODEX, executable='/bin/true'),
		require_live_evidence=True,
		directly_open_url=False,
	)
	agent.history = make_completed_history(
		state_message='Search results are still loading. No prompt body is available.',
		final_output='Title: Linux Terminal. First sentence: I want you to act as a linux terminal.',
	)

	agent._apply_live_evidence_gate()

	assert agent.history.is_successful() is False
	metadata = agent.history.action_results()[-1].metadata or {}
	assert metadata['live_evidence_gate']['passed'] is False
