"""Pinned metadata for Browser Use's default Chromium extensions."""

import re

from pydantic import BaseModel, ConfigDict, field_validator


class DefaultExtensionSpec(BaseModel):
	"""Immutable identity and integrity metadata for a default extension artifact."""

	model_config = ConfigDict(frozen=True)

	name: str
	extension_id: str
	version: str
	sha256: str
	url: str
	allowed_download_hosts: frozenset[str] = frozenset({'clients2.google.com', 'clients2.googleusercontent.com'})

	@field_validator('extension_id')
	@classmethod
	def validate_extension_id(cls, extension_id: str) -> str:
		"""Require a canonical Chrome Web Store extension identifier."""
		if not re.fullmatch(r'[a-p]{32}', extension_id):
			raise ValueError('extension_id must be a 32-character Chrome extension ID')
		return extension_id

	@field_validator('sha256')
	@classmethod
	def validate_sha256(cls, digest: str) -> str:
		"""Require a lowercase SHA-256 digest."""
		if not re.fullmatch(r'[0-9a-f]{64}', digest):
			raise ValueError('sha256 must be a 64-character lowercase digest')
		return digest


class ExtensionArtifactLock(BaseModel):
	"""Persisted record of the extension artifact verified in the local cache."""

	extension_id: str
	version: str
	sha256: str
	source_url: str


DEFAULT_EXTENSION_SPECS = (
	DefaultExtensionSpec(
		name='uBlock Origin Lite',
		extension_id='ddkjiahejlhfcafbddmgiahcphecmpfh',
		version='2026.804.1652',
		sha256='e8179d6d6b70165b375127a50337768a4f37d4f8ac9156f878cd05a0f9ad10b2',
		url='https://clients2.google.com/service/update2/crx?response=redirect&prodversion=133&acceptformat=crx3&x=id%3Dddkjiahejlhfcafbddmgiahcphecmpfh%26uc',
	),
	DefaultExtensionSpec(
		name="I still don't care about cookies",
		extension_id='edibdbjcniadpccecjdfdjjppcpchdlm',
		version='1.1.9',
		sha256='46b6cdc30343dd297c6d34d9179c5e1cc1ee404c8e63b154def158e13010c37d',
		url='https://clients2.google.com/service/update2/crx?response=redirect&prodversion=133&acceptformat=crx3&x=id%3Dedibdbjcniadpccecjdfdjjppcpchdlm%26uc',
	),
	DefaultExtensionSpec(
		name='Force Background Tab',
		extension_id='gidlfommnbibbmegmgajdbikelkdcmcl',
		version='2.2.2',
		sha256='c59681a461944aa84731cb530b32fc03f74f418382b11f5696a0fc7ab71fe00f',
		url='https://clients2.google.com/service/update2/crx?response=redirect&prodversion=133&acceptformat=crx3&x=id%3Dgidlfommnbibbmegmgajdbikelkdcmcl%26uc',
	),
)
