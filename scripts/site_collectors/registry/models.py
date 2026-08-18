"""Validated models and loader for the promotion-channel registry.

Follows the same contract style as `scripts/data_source_catalog.py`: the YAML is
data, this module is the schema, and every collector loads through here rather
than calling `yaml.safe_load` on its own.

The point is not tidiness. The registry encodes a collection *policy*, and until
now that policy was prose in a comment — a typo in `access_tier` or a forgotten
`collect: false` would silently authorize fetching an authwalled channel. The
model validators below turn each policy sentence into an invariant that fails
loudly at load time:

  * T2 (authwall / robots-prohibited) implies collect is false
  * decision_pending implies collect is false
  * official_status VERIFIED requires named evidence
  * every brand/company reference resolves; every URL appears once

`prohibited_domains()` exports the T2 host set for BrowserProfile, so the same
registry that documents the policy also enforces it inside the browser via the
SecurityWatchdog — a blocked channel becomes unreachable rather than merely
un-requested.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parent / 'promotion_channels.yaml'


class AccessTier(StrEnum):
	"""How reachable a channel is, which decides whether we fetch it at all."""

	KEYLESS_HTTP = 'T0'  # plain HTTP, robots-clean, structured data
	BROWSER_REQUIRED = 'T1'  # reachable but JS-rendered; needs CDP
	BLOCKED = 'T2'  # authwall or robots-prohibited; registry-only, never fetched


class RobotsVerdict(StrEnum):
	"""Observed robots.txt stance for the channel's own path."""

	ALLOW = 'allow'
	DISALLOW = 'disallow'  # `User-agent: *` is denied this path
	NAMED_AI_BLOCK = 'named_ai_block'  # `*` allowed, but AI crawlers named on a deny group
	AUTHWALL = 'authwall'  # robots is moot; content sits behind a login
	UNKNOWN = 'unknown'  # no robots.txt, or the host served a page instead


class ChannelType(StrEnum):
	WEBSITE = 'website'
	CORPORATE = 'corporate'
	BLOG = 'blog'
	FEED = 'feed'
	AFFILIATE = 'affiliate'
	LINKHUB = 'linkhub'
	TELEGRAM = 'telegram'
	YOUTUBE = 'youtube'
	TIKTOK = 'tiktok'
	INSTAGRAM = 'instagram'
	FACEBOOK = 'facebook'
	LINKEDIN = 'linkedin'


class OfficialStatus(StrEnum):
	"""Confidence that a channel really belongs to the brand it is filed under."""

	VERIFIED = 'verified'  # a first-party source states this URL
	CLAIMED = 'claimed'  # asserted somewhere, not first-party confirmed
	UNVERIFIED = 'unverified'


class OfficialEvidence(StrEnum):
	"""Where a VERIFIED status was established. Named so the check is reproducible."""

	APPSTORE_DESCRIPTION = 'appstore_description'
	APPSTORE_SELLER_URL = 'appstore_seller_url'
	SITE_FOOTER = 'site_footer'
	REDIRECT_TARGET = 'redirect_target'
	PRESS_RELEASE = 'press_release'


class Audience(StrEnum):
	CONSUMER = 'consumer'
	B2B = 'b2b'


class Company(BaseModel):
	"""A legal entity. `legal_entity` comes from a first-party attestation."""

	model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

	id: str = Field(pattern=r'^[a-z][a-z0-9_]*$')
	name: str = Field(min_length=1)
	legal_entity: str | None = None
	legal_entity_evidence: str | None = None
	parent: str | None = None
	country: str | None = Field(default=None, pattern=r'^[A-Z]{2}$')

	@model_validator(mode='after')
	def validate_entity_evidence(self) -> Self:
		"""A legal-entity claim without a source is an assertion, not a record."""
		if self.legal_entity and not self.legal_entity_evidence:
			raise ValueError(f'company {self.id}: legal_entity requires legal_entity_evidence')
		return self


class Brand(BaseModel):
	"""A consumer-facing brand, optionally with an iOS app driving appstore_watch."""

	model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

	id: str = Field(pattern=r'^[a-z][a-z0-9_]*$')
	company: str = Field(pattern=r'^[a-z][a-z0-9_]*$')
	name: str = Field(min_length=1)
	app_ios: int | None = Field(default=None, gt=0)
	app_bundle: str | None = None


class Channel(BaseModel):
	"""One promotion channel and the policy decision attached to it."""

	model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

	url: HttpUrl
	brand: str | None = Field(default=None, pattern=r'^[a-z][a-z0-9_]*$')
	company: str | None = Field(default=None, pattern=r'^[a-z][a-z0-9_]*$')
	channel_type: ChannelType
	access_tier: AccessTier
	robots_verdict: RobotsVerdict
	official_status: OfficialStatus
	official_evidence: OfficialEvidence | None = None
	consumer_or_b2b: Audience
	collect: bool
	decision_pending: bool = False
	notes: str | None = None

	@model_validator(mode='after')
	def validate_policy_invariants(self) -> Self:
		"""Make the collection policy unrepresentable to violate."""
		if self.access_tier is AccessTier.BLOCKED and self.collect:
			raise ValueError(f'{self.url}: T2 channels must have collect: false')
		if self.decision_pending and self.collect:
			raise ValueError(f'{self.url}: decision_pending channels must have collect: false until ruled on')
		if self.official_status is OfficialStatus.VERIFIED and self.official_evidence is None:
			raise ValueError(f'{self.url}: official_status verified requires official_evidence')
		if self.robots_verdict is RobotsVerdict.DISALLOW and self.collect:
			raise ValueError(f'{self.url}: robots disallows this path; collect must be false')
		if self.brand is None and self.company is None:
			raise ValueError(f'{self.url}: channel must reference a brand or a company')
		return self

	@property
	def host(self) -> str:
		return urlsplit(str(self.url)).netloc


class PromotionRegistry(BaseModel):
	"""The whole registry: entities, brands, channels, and their policy state."""

	model_config = ConfigDict(extra='forbid')

	schema_version: int = Field(ge=1)
	verified_at: date
	companies: list[Company] = Field(min_length=1)
	brands: list[Brand] = Field(min_length=1)
	channels: list[Channel] = Field(min_length=1)

	@model_validator(mode='after')
	def validate_references_and_uniqueness(self) -> Self:
		"""Dangling references and duplicate URLs both corrupt the joins downstream."""
		company_ids = {company.id for company in self.companies}
		if len(company_ids) != len(self.companies):
			raise ValueError('company ids must be unique')

		brand_ids = {brand.id for brand in self.brands}
		if len(brand_ids) != len(self.brands):
			raise ValueError('brand ids must be unique')

		urls = [str(channel.url) for channel in self.channels]
		if len(urls) != len(set(urls)):
			duplicates = sorted({url for url in urls if urls.count(url) > 1})
			raise ValueError(f'channel urls must be unique: {duplicates}')

		for company in self.companies:
			if company.parent is not None and company.parent not in company_ids:
				raise ValueError(f'company {company.id}: unknown parent {company.parent}')
		for brand in self.brands:
			if brand.company not in company_ids:
				raise ValueError(f'brand {brand.id}: unknown company {brand.company}')
		for channel in self.channels:
			if channel.brand is not None and channel.brand not in brand_ids:
				raise ValueError(f'{channel.url}: unknown brand {channel.brand}')
			if channel.company is not None and channel.company not in company_ids:
				raise ValueError(f'{channel.url}: unknown company {channel.company}')

		app_ids = [brand.app_ios for brand in self.brands if brand.app_ios]
		if len(app_ids) != len(set(app_ids)):
			raise ValueError('app_ios ids must be unique across brands')
		return self

	@property
	def by_brand(self) -> dict[str, Brand]:
		return {brand.id: brand for brand in self.brands}

	@property
	def by_company(self) -> dict[str, Company]:
		return {company.id: company for company in self.companies}

	def collectible(self, tier: AccessTier | None = None) -> list[Channel]:
		"""Channels we are permitted to fetch, optionally narrowed to one tier."""
		channels = [channel for channel in self.channels if channel.collect]
		return [channel for channel in channels if tier is None or channel.access_tier is tier]

	def prohibited_domains(self) -> list[str]:
		"""T2 hosts, as BrowserProfile patterns.

		Handed to BrowserProfile so the SecurityWatchdog refuses navigation to a
		blocked channel. The policy stops being a promise the script keeps and
		becomes a property of the browser it drives.
		"""
		hosts = {channel.host for channel in self.channels if channel.access_tier is AccessTier.BLOCKED}
		patterns: set[str] = set()
		for host in hosts:
			bare = host.removeprefix('www.')
			patterns |= {bare, f'*.{bare}'}
		return sorted(patterns)

	def allowed_domains(self, channels: list[Channel]) -> list[str]:
		"""Navigation allowlist for a browser run over exactly `channels`."""
		patterns: set[str] = set()
		for channel in channels:
			bare = channel.host.removeprefix('www.')
			patterns |= {bare, f'*.{bare}'}
		return sorted(patterns)


def load_registry(path: Path = DEFAULT_REGISTRY_PATH) -> PromotionRegistry:
	"""Load and validate the promotion-channel registry at ``path``."""
	return PromotionRegistry.model_validate(yaml.safe_load(path.read_text(encoding='utf-8')))
