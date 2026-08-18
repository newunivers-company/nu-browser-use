"""Title normalization and similarity, shared by the collectors that match names.

Two callers need the same primitives for different reasons: the newtoki watch
compares our titles against a pirate index, and the cross-source join compares
the same work as five platforms spell it. Both hit the identical traps — curly
apostrophes, inconsistent spacing, CJK text where whitespace tokenization is
meaningless — so the logic lives here once rather than drifting in two places.

Bigram Jaccard is used rather than token overlap because it degrades sensibly
across scripts: a Korean title is often a single whitespace-free run, and a
word-based metric scores that as one token against one token.
"""

from __future__ import annotations

import re
import unicodedata

# Decorations platforms bolt onto a title that are not part of the work's name.
DECORATION_RE = re.compile(r'\s*(?:\[[^\]]*\]|\([^)]*\)|\bS\d+\b|(?:season|시즌)\s*\d+|\bpart\s*\d+\b|\bdubbed\b|\braw\b|\bfull\b|\bofficial\b)\s*', re.I)
_PUNCT_RE = re.compile(r'[^0-9a-z가-힣]+')
_QUOTES = (('’', "'"), ('‘', "'"), ('“', '"'), ('”', '"'), ('–', '-'), ('—', '-'))


def normalize(title: str) -> str:
	"""Fold a title to a comparison key.

	Curly apostrophes are the practical trap: one catalog writes "Dragon's"
	with U+2019 while another types an ASCII quote, and a raw comparison then
	silently misses a real match.
	"""
	folded = unicodedata.normalize('NFKC', title or '').casefold()
	for fancy, plain in _QUOTES:
		folded = folded.replace(fancy, plain)
	return _PUNCT_RE.sub('', folded)


def strip_decorations(title: str) -> str:
	"""Drop season/dub/part markers so the underlying work can be compared."""
	return re.sub(r'\s+', ' ', DECORATION_RE.sub(' ', title or '')).strip()


def bigrams(value: str) -> set[str]:
	return {value[i : i + 2] for i in range(len(value) - 1)} or ({value} if value else set())


def similarity(left: str, right: str) -> float:
	"""Character-bigram Jaccard over two already-normalized strings."""
	if not left or not right:
		return 0.0
	a, b = bigrams(left), bigrams(right)
	if not a or not b:
		return 0.0
	return len(a & b) / len(a | b)


def classify(reference_normalized: str, candidate: str, near_threshold: float) -> tuple[str, float]:
	"""exact | near | none for a candidate title against a normalized reference."""
	other = normalize(candidate)
	if not reference_normalized or not other:
		return 'none', 0.0
	if reference_normalized in other or other in reference_normalized:
		return 'exact', 1.0
	score = similarity(reference_normalized, other)
	return ('near', score) if score >= near_threshold else ('none', score)
