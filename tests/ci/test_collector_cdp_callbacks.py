"""CDP event handlers in scripts/site_collectors take the signature cdp-use calls.

cdp-use invokes a registered handler as `callback(event_params, session_id)` —
the event's params dict, already unwrapped, plus the session it arrived on. Four
collectors were written against a different shape, `callback(client, message)`,
and read `message['params']['request']`. That does not raise at registration; it
raises once per event, inside cdp-use's dispatcher, which prints

    Error in event handler for Network.requestWillBeSent:
    'str' object has no attribute 'get'

and moves on. The script then completes, reports success, and has captured
nothing — the same failure mode as a collector that reports `ok` while its
snapshots are empty arrays, which is what docs/collection-policy.md means by
reading silence as safety.

Nothing catches it: these four are recon tools, so no cadence runs them, and a
handler is a closure that no unit test constructs. The signature is the only
part that can be checked without a browser, so it is what this checks.

The parse is deliberately syntactic — ast, not import — because importing a
collector runs its module body, and several open network sessions there.
"""

import ast
import re
from pathlib import Path

import pytest

COLLECTORS_DIR = Path(__file__).resolve().parents[2] / 'scripts' / 'site_collectors'

# `client.register.<Domain>.<event>(handler)` / `cdp_session.cdp_client.register...`
REGISTER_RE = re.compile(r'\.register\.[A-Za-z]+\.[A-Za-z]+\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)')

# The second parameter is the session id. Handlers that ignore it may name it
# anything, so only the count and the first parameter's meaning are enforced.
WRONG_FIRST_PARAM = {'client', '_client', 'cdp_client'}


def collector_sources() -> list[tuple[Path, str]]:
	found = []
	for path in sorted(COLLECTORS_DIR.glob('*.py')):
		text = path.read_text(encoding='utf-8')
		if '.register.' in text:
			found.append((path, text))
	return found


def registered_handler_names(text: str) -> set[str]:
	return set(REGISTER_RE.findall(text))


def function_defs(text: str) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
	tree = ast.parse(text)
	defs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
	for node in ast.walk(tree):
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
			defs.setdefault(node.name, node)
	return defs


def test_some_collector_registers_a_cdp_handler():
	"""If this ever finds nothing, the regex broke — not the collectors."""
	assert collector_sources(), 'no collector registers a CDP event handler; the detector is broken'


@pytest.mark.parametrize('path', [p for p, _ in collector_sources()], ids=lambda p: p.name)
def test_registered_handlers_take_event_and_session_id(path: Path):
	text = path.read_text(encoding='utf-8')
	defs = function_defs(text)
	problems = []
	for name in sorted(registered_handler_names(text)):
		node = defs.get(name)
		if node is None:
			continue  # registered from an import or an attribute; not ours to judge
		params = [a.arg for a in node.args.args]
		if len(params) != 2:
			problems.append(f'{name}({", ".join(params)}) takes {len(params)} params, cdp-use passes 2')
		elif params[0] in WRONG_FIRST_PARAM:
			problems.append(f'{name}({", ".join(params)}) reads the first arg as a client; cdp-use passes the event params')
	assert problems == [], f'{path.name}: ' + '; '.join(problems)


@pytest.mark.parametrize('path', [p for p, _ in collector_sources()], ids=lambda p: p.name)
def test_handlers_do_not_unwrap_a_params_envelope(path: Path):
	"""`event['params']` is the tell: the envelope is already gone by then.

	A handler that survives the signature check can still be written against the
	raw protocol message. Reading `params` off the event yields None, the
	subsequent `.get` raises inside the dispatcher, and the capture is silently
	empty — the exact shape the four recon tools shipped with.
	"""
	text = path.read_text(encoding='utf-8')
	defs = function_defs(text)
	offenders = []
	for name in sorted(registered_handler_names(text)):
		node = defs.get(name)
		if node is None:
			continue
		body = ast.get_source_segment(text, node) or ''
		if re.search(r"\.get\(\s*'params'|\[\s*'params'\s*\]", body):
			offenders.append(name)
	assert offenders == [], f'{path.name}: {offenders} unwrap a "params" envelope cdp-use already removed'
