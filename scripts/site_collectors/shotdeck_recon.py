"""Phase 1: agent-driven reconnaissance of an authenticated ShotDeck session.

Attaches over CDP to an already-logged-in Chrome, then maps account state, navigation
surface, search filters, and any XHR/JSON endpoints worth driving deterministically later.
Run with: .venv\\Scripts\\python.exe shotdeck_recon.py
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from browser_use import Agent, BrowserSession
from browser_use.runtime import resolve_runtime_llm

CDP_URL = os.environ.get('BROWSER_USE_CDP_URL', 'http://127.0.0.1:9222')
OUTPUT_PATH = Path(__file__).parent / 'shotdeck_recon.json'


class NavigationTarget(BaseModel):
	"""One reachable page in the authenticated application shell."""

	model_config = ConfigDict(extra='forbid')

	label: str
	url: str
	purpose: str = Field(description='what this page exposes')


class SearchFilter(BaseModel):
	"""One filter facet offered by the shot browser."""

	model_config = ConfigDict(extra='forbid')

	name: str
	control_type: str = Field(description='select, checkbox, text, range, tag, etc.')
	sample_values: list[str] = Field(default_factory=list, max_length=12)


class ReconReport(BaseModel):
	"""Structured map of the authenticated ShotDeck surface."""

	model_config = ConfigDict(extra='forbid')

	logged_in: bool
	account_label: str = Field(default='', description='visible username or email, empty if not shown')
	subscription_status: str = Field(default='', description='plan or tier text if visible')
	navigation: list[NavigationTarget] = Field(default_factory=list, max_length=30)
	search_url_pattern: str = Field(default='', description='URL shape used by the shot search, with query params')
	search_filters: list[SearchFilter] = Field(default_factory=list, max_length=40)
	shot_card_fields: list[str] = Field(default_factory=list, max_length=40)
	shot_detail_fields: list[str] = Field(default_factory=list, max_length=60)
	pagination_mechanism: str = Field(default='', description='infinite scroll, numbered pages, load-more button, etc.')
	notes: str = ''


TASK = """
You are attached to an already-logged-in ShotDeck browser session (https://shotdeck.com/).
Do NOT log out, do NOT change any account settings, do NOT delete anything.

Map the authenticated surface of the site. Work through these steps:

1. Confirm you are logged in. Find the visible account name/email and any subscription or plan
   label shown in the account/profile menu or account page.
2. List the main navigation destinations available while logged in (browse/search shots, decks,
   my account, etc.) with their URLs.
3. Open the shot browser/search page. Record the exact URL and how query parameters appear in the
   address bar when you apply a filter or a search term.
4. Enumerate the available search filter facets (e.g. genre, shot type, lighting, color, location,
   era, camera). For each, note the control type and a few sample values.
5. Inspect a single shot result card and then open one shot's detail view. List every metadata
   field name visible on the card, and every field visible on the detail view.
6. Determine how more results are loaded: numbered pagination, a load-more button, or infinite scroll.

Be concrete and use the real field names as the site spells them. Return the structured report.
"""


async def main() -> None:
	"""Run the reconnaissance agent against the attached CDP session."""
	browser_session = BrowserSession(cdp_url=CDP_URL, is_local=False, use_cloud=False)
	agent = Agent(
		task=TASK,
		llm=resolve_runtime_llm(),
		browser_session=browser_session,
		output_model_schema=ReconReport,
		use_vision=False,
		max_actions_per_step=3,
		max_steps=60,
	)
	history = await agent.run(max_steps=60)

	result = history.final_result()
	if result is None:
		print('agent produced no final result')
		return
	payload = json.loads(result) if isinstance(result, str) else result
	OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
	print(f'wrote {OUTPUT_PATH}')
	print(json.dumps(payload, ensure_ascii=False, indent=2)[:4000])


if __name__ == '__main__':
	asyncio.run(main())
