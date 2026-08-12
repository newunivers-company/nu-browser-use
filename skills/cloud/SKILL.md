---
name: cloud
description: >
  Documentation reference for using Browser Use Cloud — the hosted API
  and SDK for browser automation. Use this skill whenever the user needs
  help with the Cloud REST API (v2 or v3), browser-use-sdk (Python or
  TypeScript), X-Browser-Use-API-Key authentication, cloud sessions,
  browser profiles, profile sync, CDP WebSocket connections, stealth
  browsers, residential proxies, CAPTCHA handling, webhooks, workspaces,
  skills marketplace, liveUrl streaming, pricing, or integration patterns
  (chat UI, subagent, adding browser tools to existing agents). Also
  trigger for questions about n8n/Make/Zapier integration, Playwright/
  Puppeteer/Selenium on cloud infrastructure, or 1Password vault
  integration. Do NOT use this for the open-source Python library
  (Agent, Browser, Tools config) — use the open-source skill instead.
allowed-tools: Read
---

# Browser Use Cloud Reference

Reference docs for the Cloud REST API, SDKs, and integration patterns.
Read the relevant file based on what the user needs.

## API & Platform

| Topic | Read |
|-------|------|
| Setup, first task, pricing, FAQ | `references/quickstart.md` |
| v2 REST API: all 30 endpoints, cURL examples, schemas | `references/api-v2.md` |
| v3 BU Agent API: sessions, messages, files, workspaces | `references/api-v3.md` |
| Sessions, profiles, auth strategies, 1Password | `references/sessions.md` |
| CDP direct access, Playwright/Puppeteer/Selenium | `references/browser-api.md` |
| Proxies, webhooks, workspaces, skills, MCP, live view | `references/features.md` |
| Parallel, streaming, geo-scraping, tutorials | `references/patterns.md` |

## Integration Guides

| Topic | Read |
|-------|------|
| Building a chat interface with live browser view | `references/guides/chat-ui.md` |
| Using browser-use as a subagent (task in → result out) | `references/guides/subagent.md` |
| Adding browser-use tools to an existing agent | `references/guides/tools-integration.md` |

## Critical Notes

- Cloud API base URL: `https://api.browser-use.com/api/v2/` (v2) or `https://api.browser-use.com/api/v3` (v3)
- Auth header: `X-Browser-Use-API-Key: <key>`
- Get API key: https://cloud.browser-use.com/new-api-key
- Set env var: `BROWSER_USE_API_KEY=<key>`
- Cloud SDK: `uv pip install browser-use-sdk` (Python) or `npm install browser-use-sdk` (TypeScript)
- Python v2: `from browser_use_sdk import AsyncBrowserUse`
- Python v3: `from browser_use_sdk.v3 import AsyncBrowserUse`
- TypeScript v2: `import { BrowserUse } from "browser-use-sdk"`
- TypeScript v3: `import { BrowserUse } from "browser-use-sdk/v3"`
- CDP WebSocket: `wss://connect.browser-use.com?apiKey=KEY&proxyCountryCode=us`

<!-- nu-skill-execution-contract:v1 -->

## Workflow

1. Validate required inputs, referenced files, rights, and permissions.
2. Follow the skill procedure and load only the references needed for the request.
3. Produce the declared artifacts and preserve source or execution provenance.
4. Run the verification checks, then report the result, blockers, and recovery path.

## Output contract

Return these named deliverables: `dry_run_plan`, `command_log`, `verification_report`, `rollback_or_recovery`.
Include assumptions, evidence or provenance, completion status, and known limitations.
When a deliverable is a file, report its path and verify that it is non-empty.

## Verification

- Confirm every required deliverable is present, non-empty, and matches its declared format or schema.
- Check referenced paths, commands, and claims against the captured evidence.
- Distinguish dry-run, mock, sandbox, and live evidence; record commands and exit status when execution occurs.
- Do not mark the task complete while required evidence or review remains missing.

## Guardrails

Treat these conditions as hard failures: `undeclared_state_change`, `missing_exit_status`, `no_recovery_path`.
Stop and report a blocker when credentials, rights, consent, cost approval, or destructive-action approval is missing.
Never expose secrets or report a mock, dry-run, or unverified output as a live success.
