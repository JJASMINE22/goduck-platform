<p align="left">
  <a href="https://github.com/JJASMINE22/goduck-platform"><img src="https://img.shields.io/badge/%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-555555?style=for-the-badge" alt="简体中文"></a>
  <img src="https://img.shields.io/badge/English-2EA44F?style=for-the-badge" alt="English">
</p>

<p align="center">
  <img src="logo.png" alt="goduck-platform" width="440">
</p>

<p align="center">
  <b>Build a Personal Agent for you / your team</b> — a full-stack open-source platform based on deepagents + LangGraph + MCP
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black" alt="React 19">
  <img src="https://img.shields.io/badge/LangGraph-agent-1C3C3C" alt="LangGraph">
  <img src="https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white" alt="PostgreSQL 16">
  <img src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white" alt="Docker Compose">
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
</p>

> This is the condensed English README. The [Chinese README](README.md) is the fully detailed reference (deployment deep-dives, GPU acceleration, FAQ, per-module docs).

## Highlights

- **Hot-reloadable agents** — create and replace agents at runtime; add/remove tools & skills anytime, auto-reloaded on restart
- **Sessions decoupled from agents** — switch agents per turn within one session; context / rollback / abort / HITL unaffected (locked during a pending interrupt); updating an agent never breaks existing sessions
- **Traceable data** — deleting an agent keeps its sessions & checkpoints; agent config history is append-only with one-click restore

## Features

| Area | Capabilities |
|---|---|
| Agent orchestration | Tree-based AgentConfig editor with live validation, Mermaid delegation topology, config history & restore, YAML export |
| Streaming sessions | SSE token streaming, HITL (approve / edit / reject / append-reply), TODO plan panel, per-turn timeline |
| Agent switching | Per-turn agent selection within a session, context & checkpoints anchored to the session thread |
| Scheduled tasks | Cron jobs bound to agents, catch-up runs, run history, failure circuit-breaker |
| Workspace | Per-user isolated file tree, preview / download / delete, file-change badges (`+3 −2`) |
| Multimodal input | Image / document uploads (MinIO-backed), vision-model image reading, document parsing |
| Knowledge base RAG | pdf/docx ingestion & vectorization (Elasticsearch), session-anchored KBs, retrieval injected via user prompt so the agent calls its own search tool |
| Voice input | Push-to-talk with live transcription (AssemblyAI Streaming STT), auto-triggers a turn on release |
| Users & auth | Built-in login: registration approval, roles (manager / user), instant token revocation, multi-user workspace isolation |
| Model management | OpenAI-compatible multi-provider registration (OpenAI / DeepSeek / Qwen / local), connectivity test, key rotation |
| MCP tools | Runtime registration of stdio / http MCP servers, namespaced `{server}__{tool}` tools, health checks |
| Skills pool | Cross-agent shared `/skills/` directories, multipart upload, ready on upload |
| JEV smart approval | Probability-gated `allow_shell` approval (shadow / enforce rollout), rubric fast-path, KB-retrieval noise reduction; verdicts served by the bundled self-hosted laya-serve (Jev wire protocol) |

## Architecture

Layered modular monolith. Request path: `API → Service → Runtime (deepagents) → Persistence → Infra`.

```text
Browser
  │  HTTP / SSE / WebSocket
  ▼
Nginx edge gateway (the only public entry :80 in container deployment; Vite proxy in local dev)
  ├── /          → Frontend (React 19 SPA served by nginx)
  └── /api/*     → Backend (FastAPI + LangGraph + deepagents)
                    ├── PostgreSQL 16      business tables + LangGraph checkpoints / store
                    ├── MinIO              attachments & KB raw-file storage
                    ├── Elasticsearch      KB vector / full-text search
                    ├── TEI (in compose)   embeddings (OpenAI-compatible /v1/embeddings)
                    ├── MCP servers        stdio / http tool ecosystem (built-in goduck service: kb_search / image_understand)
                    └── AssemblyAI (ext.)  voice streaming STT (optional)
```

Key design decisions:

- **Data-driven**: models (LLM + API key) and MCP servers live in DB tables, registered at runtime via API — never in env vars; auto-reloaded on restart
- **Capability switches**: `/health` exposes `capabilities` as the single source of truth; the frontend shows/hides entries accordingly
- **No agent versioning**: agent `id` is stable; updates overwrite in place + append a history snapshot

## Tech Stack

| Layer | Technologies |
|---|---|
| Backend | FastAPI · LangGraph · deepagents · langchain-openai · psycopg3 · MinIO · Elasticsearch · pydantic-settings |
| Frontend | React 19 · TypeScript · Vite · Ant Design · Zustand · TanStack Query · Mermaid · @xyflow/react · react-hook-form |
| MCP ecosystem | fastmcp · langchain-mcp-adapters · uv/uvx · node/npx (MCP package cache pre-warmed in image) |
| Infrastructure | PostgreSQL 16 · MinIO · Elasticsearch 8 · TEI · Nginx (docker-compose) |

## Quick Start

### Option A: All-in-one Docker deployment (recommended)

Requires Docker + Docker Compose v2.

```bash
# 1) Credentials & first admin (once)
printf 'admin:$(openssl passwd -apr1 "YOUR_STRONG_PASSWORD")\n' > nginx/.htpasswd
printf 'BOOTSTRAP_USERS=admin:manager:YOUR_STRONG_PASSWORD\n' > .env

# 2) Prepare host data dirs + pre-download the embedding model (TEI loads from a local path, zero runtime downloads)
mkdir -p volumes/backend volumes/docling volumes/tei
cp -r backend/data/skills volumes/backend/skills
sudo chown -R 1001:1001 volumes/backend volumes/docling
docker run --rm -e HF_ENDPOINT=https://hf-mirror.com -v "$PWD/volumes/tei:/out" \
  python:3.11-slim bash -c "pip -q install huggingface_hub && huggingface-cli download \
   Qwen/Qwen3-Embedding-0.6B --local-dir /out/Qwen3-Embedding-0.6B"

# 3) Build & start all services
#    (postgres / minio / elasticsearch / tei / laya / backend / frontend / nginx)
docker compose up -d --build

# 4) Watch for "goduck-platform started"
docker compose logs -f backend
```

Then open `http://<server-ip>/` (port controlled by `EDGE_PORT`, default 80; double login = nginx basic auth edge gate → platform account bootstrapped via `BOOTSTRAP_USERS`).

> Only nginx publishes a port; all debug ports of postgres / minio / es / tei are bound to `127.0.0.1` — production-safe by default. GPU acceleration (docling parsing + TEI embeddings) is built into the compose file; see the [Chinese README](README.md) for the NVIDIA setup steps.

### Option B: Local development

Requires Docker, Python ≥ 3.10, Node ≥ 20.

```bash
# 1) Infrastructure
docker compose up -d postgres minio elasticsearch tei

# 2) Backend  → http://localhost:8000
cd backend && cp .env.example .env && pip install -e . && python -m app.main

# 3) Frontend → http://localhost:5173 (/api proxied to 8000)
cd frontend && npm install && npm run dev

# 4) Built-in MCP tools (kb_search / image_understand) are mounted in-process at
#    backend /mcp and auto-registered on startup (no separate gateway needed)
```

All tables (business + LangGraph native) are created automatically on backend startup — no manual migrations.

## First-Run Wizard

```text
① Register a model ──▶ ② Create an agent ──▶ ③ (optional) MCP / skills / KB ──▶ ④ Start chatting
```

1. **Register a model** (`/settings/models`): any OpenAI-compatible endpoint — OpenAI / DeepSeek / Qwen / Ollama / vLLM
2. **Create an agent** (`/agents/new`): tree-edit the AgentConfig (main agent + sub-agent delegation), bind the registered model, live Mermaid topology preview
3. **Extend (optional)**: register MCP servers (`/settings/mcp`), upload skills (`/settings/skills`), build a knowledge base (`/settings/knowledge`)
4. **Chat** (`/sessions`): SSE streaming, TODO plan panel, attachments, KB anchoring, voice input, workspace panel, HITL approval cards

## Capability Switches

Optional capabilities are driven by backend env vars; `/health` reports them and the UI hides disabled features entirely.

| Capability | Switch | Default | Depends on |
|---|---|---|---|
| Knowledge base RAG | `KB_ENABLED` | ✅ on | TEI (bundled) + Elasticsearch |
| Voice input | `VOICE_ENABLED` | ⛔ off | AssemblyAI API key |
| JEV smart approval | `JEV_ENABLED` | ⛔ off | laya service (bundled) + API key |

## Project Layout

```text
goduck-platform/
├── backend/        # FastAPI + LangGraph backend (api / services / core / models / streaming / mcps)
├── frontend/       # React 19 frontend (api / features / pages)
├── nginx/          # edge reverse proxy (SSE no-buffering + WebSocket passthrough)
├── docker-compose.yml  # one-command full-stack orchestration
└── LICENSE         # MIT
```

Module deep-dives: [backend/README.md](backend/README.md) · [frontend/README.md](frontend/README.md)

## Quality

```bash
cd backend  && ruff check app/     # backend lint (line-length 110)
cd frontend && npm run typecheck && npm run lint
```

## License

Code is released under the [MIT License](LICENSE). Third-party components orchestrated by docker-compose (MinIO · AGPL-3.0, Elasticsearch · Elastic License v2, …) keep their respective licenses.

## Acknowledgements

- [deepagents](https://github.com/langchain-ai/deepagents) (MIT) — agent kernel
- [LangGraph](https://github.com/langchain-ai/langgraph) (MIT) — graph runtime & checkpoints
- FastAPI / React / Ant Design and other open-source projects

Contributions welcome — issues and PRs are appreciated.
