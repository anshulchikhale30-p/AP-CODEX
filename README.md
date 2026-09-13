<div align="center">

# AP-CODEX

### Hands-On AI — Where answers end, AP-CODEX begins.

> The action engine that goes beyond answers to execute tasks, automate workflows,
> and extend your human reach.

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688?logo=fastapi&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-24c5aa)
![Status](https://img.shields.io/badge/build-passing-24c5aa)

</div>

---

## What is AP-CODEX?

AP-CODEX is an **AI agent that does things** — not just answers. Type something like
**"Email my manager that I'm running late"** or **"Create a Google Meet for the client call"**
and the agent plans, calls the right tool, executes the action, and reports back.

It ships as:

- A polished **landing page + chat console** (vanilla HTML/CSS/JS, no build step)
- A **FastAPI orchestration backend** with a streaming agent loop
- A **connector pipeline** that turns REST APIs and MCP servers into LLM tools
- **Auth**: local accounts + Google Sign-In

---

## Features

- **Autonomous agent loop** — LLM plans → calls tools → feeds results back → replies,
  with live streaming events (`status` / `delta` / `tool` / `done` / `error`) over SSE.
- **Provider-agnostic LLM layer** — OpenAI-compatible `/chat/completions` (OpenAI, Ollama,
  LM Studio, vLLM and more) with streaming, tool-calling, and usage tracking.
- **Deterministic scenario engine** — scripted plans for compound real-world tasks
  ("buy a shirt under ₹1,000", "book an Uber for 8 PM"), then falls through to the LLM agent.
- **Connector registry** — declarative config (JSON) registers REST API and MCP connectors;
  tools surface to the model as one flat, collision-free list.
- **Built-in lifestyle connectors** — shopping, rides, email (simulated outbox), Gmail,
  Google Calendar, Google Meet, and Twitter.
- **Custom connectors at runtime** — `POST /connectors/register` with a JSON config.
- **Full auth** — email/password sign-up & sign-in plus "Continue with Google", with
  bearer-token sessions and a local user store.
- **Simulated outbox** — no real emails are sent during demos; sent mail is persisted
  to `ap-codex-outbox.json`.
- **Middleware hardening** — every layer returns JSON dicts, never raises into the loop;
  secrets stay in env vars and never leak into errors.

---

## Architecture

```
┌─────────────────────────┐      ┌──────────────────────────────────────────────┐
│   Frontend (vanilla)    │      │   FastAPI backend (src/api)                  │
│  ┌───────────────────┐  │ HTTP │  ┌────────────────────┐                       │
│  │ Landing page      │──┼──────┼─▶│ /health /chat      │                       │
│  │  index.html +     │  │      │  │ /chat/stream (SSE) │                       │
│  │  script.js        │  │      │  │ /connectors/* /auth│                       │
│  ├───────────────────┤  │      │  └─────────┬──────────┘                       │
│  │ Chat console      │  │      │            │                                  │
│  │  chat.js          │  │      │            ▼                                  │
│  ├───────────────────┤  │      │  ┌────────────────────┐                       │
│  │ Auth modal        │  │      │  │ AgentLoop (core)   │──▶ ScenarioRunner     │
│  │  auth.js          │  │      │  └─────────┬──────────┘   (scripted plans)    │
│  └───────────────────┘  │      │            │                                  │
└─────────────────────────┘      │            ▼                                  │
                                 │  ┌────────────────────┐   LLM (OpenAI-        │
                                 │  │ ConnectorManager   │   compatible)         │
                                 │  └───┬──────┬──────┬──┘                       │
                                 │      │      │      │                          │
                                 │   APIClient  MCPClient  built-in connectors   │
                                 └──────────────────────────────────────────────┘
```

### The agent loop

`chat → AgentLoop → LLM (tools = connector schemas) → execute_connector_action
→ tool results fed back → final reply`

Everything the loop emits is a JSON-serializable dict event:

| Event | Meaning |
|-------|---------|
| `status` | phase change (e.g. `thinking`) |
| `delta` | streaming text token |
| `tool`  | tool call lifecycle: `started` → `ok` / `error` |
| `done`  | final reply + summary + usage |
| `error` | non-fatal failure surfaced to the client |

---

## Tech stack

| Layer | Tech |
|-------|------|
| Frontend | Vanilla HTML, CSS, JS (no framework, no build step) |
| Backend | Python 3.10+, FastAPI, Uvicorn |
| HTTP | `httpx` (async, blocking + streaming) |
| Connectors | REST via declarative config, plus MCP (stdio) servers |
| Auth | Local email/password + Google OAuth2 (`google-auth` flow, no SDK required) |
| Tests | `pytest` + `pytest-asyncio` |

---

## Getting started

### Prerequisites

- Python **3.10+**
- An OpenAI-compatible LLM endpoint (OpenAI, or local via Ollama / LM Studio / vLLM)
- Node.js (`npx`) — only if you use the bundled MCP example

### 1. Install

```bash
git clone https://github.com/anshulchikhale30-p/AP-CODEX.git
cd AP-CODEX
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure environment

```bash
# LLM provider (OpenAI-compatible)
export AP_CODEX_API_KEY="sk-..."          # or OPENAI_API_KEY
export AP_CODEX_MODEL="gpt-4o-mini"        # optional
export AP_CODEX_API_BASE="https://api.openai.com/v1"   # optional, Ollama example below

# Optional
# export AP_CODEX_CONNECTORS="path/to/connectors.json"
# export AP_CODEX_CORS_ORIGINS="http://localhost:8000"
```

> Using Ollama locally? Point the base at it and pick any local model:
> `export AP_CODEX_API_BASE="http://localhost:11434/v1"` `export AP_CODEX_MODEL="llama3.1"`

### 3. Run

```bash
uvicorn src.api.app:app --reload --port 8000
```

Open **http://localhost:8000** — the landing page is served automatically, and
interactive API docs live at **http://localhost:8000/docs**.

### Deploy to Vercel

The repo is pre-configured for Vercel — the entrypoint is auto-detected at
`src/app.py` (no `pyproject.toml` needed) and `vercel.json` lifts the function
timeout to 60s. Import the repo in Vercel and set these environment variables
(Settings → Environment Variables):

```bash
AP_CODEX_API_KEY=sk-...       # or OPENAI_API_KEY
AP_CODEX_MODEL=gpt-4o-mini
AP_CODEX_API_BASE=https://api.openai.com/v1
```

### 4. Run tests

```bash
pip install pytest pytest-asyncio
pytest -q
```

---

## API reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET`  | `/health` | Service health, connector/tool counts, active model |
| `GET`  | `/tools` | All discovered LLM tool schemas |
| `GET`  | `/connectors` | Registered connectors + kind + tool counts |
| `POST` | `/connectors/register` | Register a connector from JSON config |
| `POST` | `/chat` | One-shot agent completion (JSON) |
| `POST` | `/chat/stream` | Streaming agent run (`text/event-stream`) |
| `POST` | `/auth/signup` | Create a local account |
| `POST` | `/auth/login` | Sign in (returns bearer token) |
| `POST` | `/auth/logout` | Revoke the session |
| `GET`  | `/auth/me` | Current user from bearer token |
| `GET`  | `/auth/google/authorize` | Start Google Sign-In |
| `GET`  | `/auth/google/callback` | Google OAuth callback |
| `GET`  | `/connectors/gmail/authorize` · `/callbacks/*` | Gmail / Calendar / Meet OAuth |
| `GET`  | `/connectors/twitter/status` · `/connectors/meet/status` | Connector status |

#### Example: streaming chat

```bash
curl -N http://localhost:8000/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Add a team standup to my calendar tomorrow at 9 am"}]}'
```

---

## Connectors

Connectors are the agent's hands. Two types are supported, both declared in JSON:

1. **API connectors** — declarative REST endpoints with auth. `connectors.json` example:

```json
{
  "connectors": [
    {
      "id": "github",
      "name": "GitHub REST API",
      "type": "api",
      "base_url": "https://api.github.com",
      "auth": { "type": "bearer", "token_env": "GITHUB_TOKEN" },
      "endpoints": {
        "get_user": { "method": "GET", "path": "/users/{username}", "params": ["username"] }
      }
    }
  ]
}
```

2. **MCP connectors** — stdio Model Context Protocol servers:

```json
{
  "connectors": [
    {
      "id": "filesystem",
      "type": "mcp",
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]
    }
  ]
}
```

### Built-in connectors

| Connector | What it can do |
|-----------|----------------|
| `shopping` | Order from catalog (amazon/flipkart/myntra/ajio) with budget parsing |
| `rides` | Book an Uber/cab with time normalization (`8 PM`, `20:00`) |
| `email` | Simulated outbox — sends are persisted to `ap-codex-outbox.json` |
| `gmail` | Real Gmail via OAuth2 |
| `calendar` | Google Calendar events via OAuth2 |
| `meet` | Google Meet creation via OAuth2 (developer preview API) |
| `twitter` | Tweet/status via the Twitter API |

Google connectors need a `gmail_credentials.json` with your OAuth client id/secret —
created files (tokens, users, outbox) are git-ignored.

---

## Environment variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `AP_CODEX_API_KEY` / `OPENAI_API_KEY` | LLM API key | – |
| `AP_CODEX_API_KEY_ENV` | Name of the env var holding the key | `OPENAI_API_KEY` |
| `AP_CODEX_API_BASE` | OpenAI-compatible base URL | `https://api.openai.com/v1` |
| `AP_CODEX_MODEL` | Model name | `gpt-4o-mini` |
| `AP_CODEX_TEMPERATURE` | Sampling temperature | `0.7` |
| `AP_CODEX_MAX_TOKENS` | Max completion tokens | `4096` |
| `AP_CODEX_TIMEOUT` | LLM request timeout (s) | `120` |
| `AP_CODEX_CONNECTORS` | Path to connectors JSON | `./connectors.json` |
| `AP_CODEX_CORS_ORIGINS` | Comma-separated CORS origins | `*` |
| `AP_CODEX_AUTH_REDIRECT_URI` | Google Sign-In callback | derived from host |
| `AP_CODEX_GMAIL_REDIRECT_URI` | Gmail OAuth callback | derived from host |
| `GITHUB_TOKEN` | Auth for the GitHub API connector | – |

---

## Project structure

```
AP-CODEX/
├── index.html            # Landing page
├── styles.css            # All styling (aurora, cards, chat, auth…)
├── script.js             # Landing interactions (typing, tilt, counters…)
├── chat.js               # Chat console UI + SSE client
├── auth.js               # Auth modal + Google Sign-In flow
├── connectors.json       # Declarative connector config (GitHub API + MCP fs)
├── requirements.txt      # Python dependencies
├── src/
│   ├── api/              # FastAPI app, routes, auth, schemas, user store
│   ├── core/             # AgentLoop, LLM adapter, scenario engine, types
│   └── connectors/       # Manager, APIClient, MCPClient, OAuth, built-ins
└── tests/                # pytest suite (unit + integration)
```

---

## Roadmap

- [x] Landing page + chat console
- [x] Streaming agent loop with tool calling
- [x] Scenario engine for scripted workflows
- [x] REST API + MCP connector registry
- [x] Local auth + Google Sign-In
- [ ] Persistent conversation history
- [ ] Real email/calendar/twitter flows out of the box
- [ ] Docker + one-command deploy