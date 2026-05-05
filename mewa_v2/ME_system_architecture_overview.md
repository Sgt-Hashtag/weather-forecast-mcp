# MEWA System Architecture Overview

**MEWA** (Meteorological Early Warning for Agriculture) is a RAG-based chatbot for
Bangladesh farmers. It answers two categories of questions:

- **Agricultural knowledge queries** — retrieved from ingested documents via vector search + LLM
- **Live weather forecast queries** — served from an ArangoDB forecast cache, refreshed hourly

This document describes every Docker service, how they connect, what models they run,
and the key configuration knobs. It is the entry point for anyone new to the stack.

---

## Table of Contents

1. [Full Architecture Diagram](#diagram)
2. [Service Groups at a Glance](#groups)
3. [Access Layer](#access-layer) — nginx, Kong, frontend
4. [Application Layer](#application-layer) — backend, document-repository, weather-mcp-standalone, http-service
5. [OPEA RAG Pipeline](#opea-rag) — chatqna-xeon-backend-server, dataprep, retriever
6. [AI Inference Layer](#ai-inference) — vllm, vllm-translation-guardrail, TEI, embedding, reranker
7. [Infrastructure](#infrastructure) — ArangoDB, Redis, ClamAV, Kong DB
8. [Data Flows](#data-flows) — RAG query end-to-end, weather query end-to-end, document ingestion
9. [Key Configuration File: .env](#env)
10. [Port Map](#port-map)
11. [Common Operations](#operations)

---

## 1. Full Architecture Diagram <a name="diagram"></a>

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  User Browser  (HTTPS only)                                                 │
└───────────────────────────────┬─────────────────────────────────────────────┘
                                │ :443
                                ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  nginx  (SSL termination + reverse proxy)                                 │
│  /        → frontend:8090  (Vue.js SPA)                                   │
│  /api/    → kong:8000      (API gateway)                                  │
└──────────────────┬────────────────────────────────────────────────────────┘
                   │ /api/
                   ▼
┌──────────────────────────────┐
│  Kong  (API Gateway)         │
│  · Auth (JWT verify)         │
│  · Rate limiting             │
│  · Routes → backend:3000     │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  backend  (Node.js / Express, port 3000)                                     │
│  · User auth + sessions                                                      │
│  · Chat history (ArangoDB)                                                   │
│  · Document management gateway                                               │
│  · Translation (CPU NLLB or GPU Gemma via Redis cache)                       │
│  · FIVE-TIER WEATHER ROUTER (query-service.js)                               │
│      Tier 0 doc signals   → OPEA ChatQnA                                     │
│      Tier 1 hard weather  → weather-mcp-standalone:8100                      │
│      Tier 2 agro terms    → OPEA ChatQnA                                     │
│      Tier 3 ambiguous     → Granite classifier → OPEA or weather             │
│      Tier 4 default       → OPEA ChatQnA                                     │
└────────────┬──────────────────────────────────────┬───────────────────────── ┘
             │  RAG path                             │  Weather path
             ▼                                       ▼
┌────────────────────────────┐      ┌────────────────────────────────────────┐
│ chatqna-xeon-backend-server│      │ weather-mcp-standalone  (Python/FastAPI│
│ (OPEA ChatQnA megaservice) │      │  port 8100)                            │
│ port 8888                  │      │  1. Intent → Gemma-3-4b (port 9031)    │
│ Orchestrates:              │      │  2. District lookup (local)            │
│  embedding → retriever     │      │  3. Forecast cache (ArangoDB)          │
│  → reranker → vllm         │      │  4. RiskEngine (rule-based)            │
└──────┬─────────────────────┘      │  5. Explanation → Gemma-3-4b          │
       │                            └────────────────────────────────────────┘
  ┌────┼──────────────────────────────────────┐
  │    │  RAG sub-services                    │
  ▼    ▼                                      │
┌──────────────┐  ┌──────────────┐  ┌──────────────────────────────────────┐
│  embedding   │  │  retriever   │  │  reranker                            │
│  (port 6000) │  │  (port 7025) │  │  (port 6100)                         │
│  wraps TEI   │  │  queries     │  │  wraps tei_reranker                  │
└──────┬───────┘  │  ArangoDB    │  └──────┬───────────────────────────────┘
       │          └──────┬───────┘         │
       ▼                 │                 ▼
┌──────────────┐         │        ┌──────────────────┐
│  tei         │         │        │  tei_reranker    │
│  (port 7000) │         │        │  (port 7100)     │
│  BGE-base    │         │        │  ms-marco-MiniLM │
└──────────────┘         │        └──────────────────┘
                         ▼
              ┌────────────────────┐
              │  arango-vector-db  │
              │  (port 8529)       │
              │  · chunks (vector) │
              │  · chat history    │
              │  · forecast cache  │
              └────────────────────┘

GPU Inference (shared across multiple services):
┌─────────────────────────────────────────────────────────────┐
│  vllm  (port 8000)  ibm-granite/granite-3.3-2b-instruct     │
│  Used by: chatqna-xeon-backend-server (RAG answers)         │
│           query-service.js (Tier 3 classifier, YES/NO only) │
└─────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────┐
│  vllm-translation-guardrail  (port 9031)                    │
│  google/gemma-3-4b-it                                       │
│  Used by: weather-mcp-standalone (intent + explanation)     │
│           guardrail (content safety)                        │
│           translation microservice (GPU translation path)   │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. Service Groups at a Glance <a name="groups"></a>

| Group | Services | What it does |
|---|---|---|
| Access | `nginx`, `kong`, `kong-database`, `frontend` | TLS, routing, auth gateway, Vue SPA |
| Application | `backend`, `document-repository`, `weather-mcp-standalone`, `http-service` | Business logic, chat, docs, weather |
| OPEA RAG | `chatqna-xeon-backend-server`, `dataprep-arango-service`, `retriever-arango-service` | Ingest, embed, retrieve, rerank, generate |
| AI Inference | `vllm`, `vllm-translation-guardrail`, `tei`, `tei_reranker`, `embedding`, `reranker`, `textgen`, `translation`, `guardrail` | All GPU/CPU model serving |
| Infrastructure | `arango-vector-db`, `redis-cache`, `clamav` | Storage, cache, antivirus |
| Legacy OPEA UI | `chatqna-xeon-ui-server`, `chatqna-xeon-nginx-server` | OPEA's built-in UI (unused by MEWA frontend) |

---

## 3. Access Layer <a name="access-layer"></a>

### `nginx`

```yaml
container: nginx
image:     nginx:latest
port:      443 (HTTPS only)
config:    api-gateway-solution/nginx/conf/default.conf
certs:     api-gateway-solution/nginx/certs/server.crt + server.key
```

Single entry point for all browser traffic. Terminates TLS and splits routes:

```nginx
# / → Vue.js SPA
set $frontend_upstream http://frontend:8090;
proxy_pass $frontend_upstream;

# /api/ → Kong
set $kong_upstream http://kong:8000;
proxy_pass $kong_upstream;
```

> **Important:** The upstream is stored in a variable (`$frontend_upstream`, `$kong_upstream`),
> not passed directly to `proxy_pass`. This forces Nginx to re-resolve the Docker hostname on
> each request (requires `resolver 127.0.0.11 valid=10s;`). Without this, if a container is
> rebuilt and gets a new IP, Nginx will 502 until it is reloaded.
>
> After any container rebuild: `docker exec nginx nginx -s reload`

---

### `kong` + `kong-database`

```yaml
containers: kong, kong-database
images:     kong:latest, postgres:13
ports:      8010 (proxy), 8443 (proxy SSL), 8001 (admin API)
config:     api-gateway-solution/new-config/kong_config.json
```

Kong is the API gateway. All `/api/` requests from Nginx hit Kong first. Kong:
- Verifies JWT tokens issued by `backend`
- Routes to the correct upstream service based on path prefix
- Provides rate limiting and logging

Kong config is applied once at setup using a management script:
```bash
cd api-gateway-solution/new-config/
./manage-kong-config.sh -a      # apply full config
./manage-kong-config.sh -l      # list current config
```

Kong needs its Postgres schema bootstrapped exactly once:
```bash
docker compose run --rm kong kong migrations bootstrap
```

---

### `frontend`

```yaml
container:  frontend  (named genieai_mvp-frontend-1 at runtime)
build:      components/gov-chat-frontend/Dockerfile-single-node
port:       8090
framework:  Vue 3 + Vite
```

The MEWA Vue SPA. Built at `docker compose build` time — environment variables are baked in
at build time via `VUE_APP_API_URL`, `VUE_PROXY_HOST`, and `VUE_APP_CSP_CONNECT_SRC`.

**Runtime config (not baked, loaded at page load):**
```
components/gov-chat-frontend/public/config/genie-ai-config.json
```
This file is served as a static asset. It controls features like quick-help buttons, chat
settings, and UI feature flags. Changes take effect without a rebuild — you can hot-patch
the running container:
```bash
docker cp public/config/genie-ai-config.json genieai_mvp-frontend-1:/app/dist/config/genie-ai-config.json
```

Key chatbot component: `components/gov-chat-frontend/src/components/ChatBotComponent.vue`

---

## 4. Application Layer <a name="application-layer"></a>

### `backend`

```yaml
container:  genieai_mvp-backend-1
build:      components/gov-chat-backend/Dockerfile-single-node
port:       3000
runtime:    Node.js / Express
```

The main custom application server. This is the brain of the system — everything the user
does flows through here. Key responsibilities:

| Service file | Responsibility |
|---|---|
| `api.js` | Express router — mounts all sub-routes |
| `auth-service.js` | Login, logout, JWT issuance |
| `query-service.js` | **Core**: weather router + OPEA call + weather-mcp call |
| `chat-history-service.js` | Stores/retrieves chat sessions in ArangoDB |
| `translation-service.js` | Translates messages (CPU NLLB or GPU Gemma via cache) |
| `user-profile-service.js` | User preferences, profile |
| `session-service.js` | Session management |
| `analytics-service.js` | Usage metrics |
| `database-operations-service.js` | ArangoDB CRUD helpers |
| `security-scan-service.js` | ClamAV integration for uploads |
| `tool-registry.js` | MCP tool definitions available to the LLM |
| `tool-orchestrator.js` | Executes tool calls returned by the LLM |
| `weather-service.js` | Thin wrapper around weather-mcp-standalone |
| `opea-worker.js` | Worker thread: sends requests to OPEA without blocking Node's event loop |

#### How the RAG call works (query-service.js)

```javascript
// After the weather router decides this is a RAG query:
const opeaHost = process.env.OPEA_HOST || 'chatqna-xeon-backend-server';
const opeaPort = process.env.OPEA_PORT || '8888';
const opeaUrl  = `http://${opeaHost}:${opeaPort}/v1/chatqna`;

// Request is dispatched to a worker thread so Node's event loop is not blocked
const workerResult = await this.runOPEAWorker(opeaUrl, opeaPayload);
```

The payload to OPEA includes the message history, user profile context, and system prompt
(passed as `CHATQNA_SYSTEM_PROMPT` env var which the OPEA server receives at startup).

#### Translation backend selection

```
TRANSLATION_BACKEND=cpu    → Xenova/nllb-200-distilled-600M  (no GPU, runs in Node process)
TRANSLATION_BACKEND=gpu    → google/gemma-3-4b-it via vllm-translation-guardrail:9031
TRANSLATION_BACKEND=auto   → try GPU, fallback to CPU
```

Translated responses are cached in Redis (`redis-cache:6379`) to avoid re-translating
identical strings. Cache TTL is managed by the translation service.

---

### `document-repository`

```yaml
container:  doc-repo-dev
build:      components/document-repository/Dockerfile
port:       3001
```

Handles document uploads from the frontend. Responsibilities:
- Accepts file uploads (PDF, DOCX, XLSX, MD, HTML, etc.)
- Runs ClamAV virus scan before accepting any file
- Stores files on a named Docker volume (`doc_repo_uploads`)
- Notifies `dataprep-arango-service` to ingest the new document
- Manages document metadata in ArangoDB

Virus scan is handled by connecting to `clamav:3310`. Any file that fails the scan is
rejected immediately.

---

### `weather-mcp-standalone`

```yaml
container:  weather-mcp-standalone
build:      components/weather-mcp-service/Dockerfile
port:        8100 (internal: 8000)  ← note: WEATHER_MCP_URL uses port 8000 internally
runtime:    Python / FastAPI
```

Serves live weather answers for Bangladesh. All forecast data is pre-cached in ArangoDB
hourly by an APScheduler job. No forecast API is called at query time.

**Endpoints:**
```
GET  /health                  — liveness
POST /query                   — natural-language weather query (called by backend)
GET  /risk/latest?location=   — latest stored risk tier for a district
POST /internal/run-pipeline   — manually trigger hourly data refresh
POST /mcp/tools/list          — MCP tool registry
POST /mcp/tools/call          — execute retrieve_weather_forecast tool
```

**The full pipeline per query:**
```
POST /query { "query": "What is the weather in Dhaka tomorrow?" }
    │
    ├─ 1. _extract_intent()        Gemma-3-4b (port 9031, max_tokens=80, temp=0)
    │       → { location: "Dhaka", user_context: "FARMER", forecast_days: 3 }
    │
    ├─ 2. _find_district()         Local lookup (no LLM, no API)
    │       → "Dhaka"
    │
    ├─ 3. ArangoDB cache           Pre-filled hourly by open-meteo scheduler
    │       → { forecast: [ {date, temperature, precipitation, humidity}, ... ] }
    │
    ├─ 4. RiskEngine.classify()    Rule-based, no LLM
    │       → Tier 0–4 (Normal / Advisory / Watch / Warning / Emergency)
    │
    └─ 5. _generate_explanation()  Gemma-3-4b (port 9031, max_tokens=300, temp=0.2)
            → "Temperatures will range from 28°C to 35°C..."
```

**Data source for forecasts:** open-meteo (free, no API key). The scheduler fetches all
64 Bangladesh districts hourly and stores structured `UnifiedForecast` objects in ArangoDB.

**Risk tiers:**
```
Tier 0 — Normal      (baseline, no alerts)
Tier 1 — Advisory    (e.g. rain > 20mm/day)
Tier 2 — Watch       (e.g. rain > 50mm/day)
Tier 3 — Warning     (storm-level conditions)
Tier 4 — Emergency   (cyclone / flood thresholds)
```

---

### `http-service`

```yaml
container:  http-service
build:      genie-ai-overlay/http-service/Dockerfile-http-service_genie-ai
port:       6666
```

A small Express microservice used only by the OPEA components (`dataprep`, `chatqna-server`)
to obtain a JWT auth token for calling the `backend`. It acts as a token vending machine:

```
GET_AUTH_TOKEN_URL=http://http-service:6666/get-token
```

This token is then attached to calls that `dataprep` makes back to `backend` during ingestion
(e.g. to fetch document metadata, notify of completion).

---

## 5. OPEA RAG Pipeline <a name="opea-rag"></a>

OPEA (Open Platform for Enterprise AI) is Intel's open-source RAG framework. MEWA uses
OPEA v1.3 components as the backbone of its retrieval pipeline, wrapped in custom Dockerfiles
under `genie-ai-overlay/`.

```
genie-ai-overlay/
  chatqna/      Dockerfile-chatqna_genie-ai    ← orchestrator
  dataprep/     Dockerfile-dataprep_genie-ai   ← ingestion
  retriever/    Dockerfile-retriever_genie-ai  ← vector search
  reranker/     Dockerfile-reranker_genie-ai   ← reranking
  http-service/ Dockerfile-http-service_genie-ai
  core/         shared patches
```

Each Dockerfile clones the OPEA repo at build time and applies patches:
```dockerfile
ARG OPEA_VERSION=v1.3
ARG OPEA_REPO_URL=https://github.com/opea-project/GenAIComps.git
RUN git clone --depth 1 --branch ${OPEA_VERSION} ${OPEA_REPO_URL} /tmp/opea
# ... then copies custom integrations on top
```

---

### `chatqna-xeon-backend-server`

```yaml
container:  genie-ai-chatqna-server
build:      genie-ai-overlay/chatqna/Dockerfile-chatqna_genie-ai
port:       8888
endpoint:   POST /v1/chatqna
```

The OPEA ChatQnA megaservice. It is the orchestrator of the full RAG pipeline. When called,
it:

1. Calls `embedding:6000` → embeds the query using `BAAI/bge-base-en-v1.5`
2. Calls `retriever-arango-service:7025` → vector search in ArangoDB, returns top-15 candidates
3. Calls `reranker:6100` → reranks to top-5 using `ms-marco-MiniLM-L-6-v2`
4. Calls `vllm:8000` → generates response with `ibm-granite/granite-3.3-2b-instruct`

Key env vars:
```
CHATQNA_TYPE=GENIEAI
CHATQNA_SYSTEM_PROMPT=<the system prompt — see ME_chatbot_prompt_and_router_configuration.md>
CHATQNA_ENFORCE_ABSTENTION=true    ← returns "I don't know" when context is insufficient
RERANKER_TOP_N=5                   ← top-5 chunks sent to the LLM
```

> **Container recreate required for prompt changes.** This container reads `.env` only at
> startup. `docker restart` does NOT re-read `.env`. Use:
> ```bash
> docker compose up -d chatqna-xeon-backend-server
> ```

---

### `dataprep-arango-service`

```yaml
container:  genie-ai-dataprep-arango
build:      genie-ai-overlay/dataprep/Dockerfile-dataprep_genie-ai
port:       5000  (mapped as DATAPREP_PORT=6007 externally, but container listens on 5000)
key file:   genie-ai-overlay/dataprep/genieai_dataprep_arangodb.py
```

Processes documents into searchable chunks stored in ArangoDB. The full ingestion pipeline:

```
File upload received
    │
    ├─ Content extraction:
    │    CONTENT_EXTRACTION_METHOD=docling
    │    Docling handles PDF, DOCX, PPTX (OCR-capable)
    │    LangChain HTMLHeaderTextSplitter for .html
    │    Plain text read for .md, .txt
    │
    ├─ Chunking (per-type sizes, all character-based):
    │    PDF:   DATAPREP_CHUNK_SIZE_PDF=500   overlap=50
    │    DOCX:  DATAPREP_CHUNK_SIZE_DOCX=1000
    │    XLSX:  DATAPREP_CHUNK_SIZE_XLSX=1500
    │    MD:    DATAPREP_CHUNK_SIZE_MD=500
    │    HTML:  DATAPREP_CHUNK_SIZE_HTML=500
    │    TXT:   DATAPREP_CHUNK_SIZE_TXT=500
    │    Uses RecursiveCharacterTextSplitter (separators: \n\n → \n → space → .)
    │
    ├─ Embedding:
    │    Each chunk → TEI embedding server (BAAI/bge-base-en-v1.5) → 768-dim vector
    │
    ├─ Labeling (LABELING_STRATEGY=llm):
    │    Granite LLM assigns 1–3 semantic labels per chunk from a predefined taxonomy
    │    (MAX_CONCURRENT_BATCHES=5 batches processed in parallel)
    │
    └─ Storage in ArangoDB:
         Graph: GRAPH_TEST
         Collections: chunk (vertices), document (vertices), belongs_to (edges)
         Each chunk stored with: text, vector, labels, file_id, source_path
```

**Important note on HTML ingestion:**
`HTMLHeaderTextSplitter.split_text()` returns `List[Document]` — use `.split_text()`, NOT
`.create_documents()`. The latter does not exist on that class and raises `AttributeError`.

```python
# Correct usage in genieai_dataprep_arangodb.py
if path.endswith(".html"):
    docs = text_splitter.split_text(content)   # returns List[Document]
else:
    docs = text_splitter.create_documents([content])   # standard
plain_chunks = [d.page_content for d in docs]
```

---

### `retriever-arango-service`

```yaml
container:  genie-ai-retriever-arango
build:      genie-ai-overlay/retriever/Dockerfile-retriever_genie-ai
port:       7025
```

Receives an embedded query vector from `chatqna-xeon-backend-server` and searches ArangoDB.

Key config:
```
RETRIEVER_ARANGO_K=5            ← final top-K returned
RETRIEVER_ARANGO_FETCH_K=15     ← candidates fetched before reranking
RETRIEVER_ARANGO_SCORE_THRESHOLD=0.5
RETRIEVER_ARANGO_SEARCH_MODE=vector   ← pure vector search (not hybrid)
RETRIEVER_ARANGO_TRAVERSAL_ENABLED=false  ← graph traversal off by default
ARANGO_FILTER_STRATEGY=OR       ← label filter logic
```

Graph traversal (`RETRIEVER_ARANGO_TRAVERSAL_ENABLED=true`) is available but disabled. When
enabled, after finding the top chunk it walks the ArangoDB graph edges to include neighboring
chunks from the same document — useful for finding context that is nearby but not the
highest-scoring hit.

---

## 6. AI Inference Layer <a name="ai-inference"></a>

### `vllm` — Main LLM

```yaml
container:  vllm-vllm-2
image:      vllm/vllm-openai:latest
port:       8000
model:      ibm-granite/granite-3.3-2b-instruct
GPU:        1x NVIDIA (VLLM_GPU_UTIL=0.35)
```

Serves the OpenAI-compatible API used by both the OPEA pipeline and the Tier 3 weather
router classifier. Exposes `/v1/chat/completions` and `/health`.

```bash
# vllm startup command (from docker-compose.yaml)
--model ibm-granite/granite-3.3-2b-instruct
--gpu_memory_utilization 0.35
--served-model-name ibm-granite/granite-3.3-2b-instruct
--max_model_len 16384
--max_num_seqs 1024
--dtype half
```

Models are cached at `/root/.cache/huggingface` (mounted as a volume — not re-downloaded
on rebuild).

---

### `vllm-translation-guardrail` — Translation/Safety LLM

```yaml
container:  vllm-vllm-translation-guardrail
image:      vllm/vllm-openai:latest
port:       9031
model:      google/gemma-3-4b-it
GPU:        1x NVIDIA (VLLM_TRANSLATION_GPU_UTIL=0.35)
```

A second vLLM instance for tasks that must not compete with the main RAG model:
- **Weather intent extraction** — `weather-mcp-standalone` calls this for `_extract_intent()`
- **Weather explanation generation** — `weather-mcp-standalone` calls this for `_generate_explanation()`
- **Content guardrails** — `guardrail` container validates inputs/outputs
- **GPU translation** — when `TRANSLATION_BACKEND=gpu`, the backend translates via this endpoint

> `start_period: 300s` in the healthcheck — Gemma-3-4b takes up to 5 minutes to load on
> GPU. Several downstream services (`tei`, `tei_reranker`) depend on this being healthy
> before they start.

---

### `tei` — Embedding Inference Server

```yaml
container:  tei-embedding-serving
image:      ghcr.io/huggingface/text-embeddings-inference:latest
port:       7000 → 80 (internal)
model:      BAAI/bge-base-en-v1.5  (768-dim dense embeddings)
```

HuggingFace TEI server for fast GPU embedding inference. Used by both `dataprep` (at
ingestion time) and `embedding`/`retriever` (at query time). TEI handles batching and
quantization internally.

---

### `embedding` — OPEA Embedding Wrapper

```yaml
container:  embedding
image:      opea/embedding:latest
port:       6000
```

Thin OPEA wrapper around `tei`. Accepts the OPEA request format and forwards to
`tei:7000`. Called by `chatqna-xeon-backend-server` during query processing.

---

### `tei_reranker` — Reranker Inference Server

```yaml
container:  tei-reranker-serving
image:      ghcr.io/huggingface/text-embeddings-inference:latest
port:       7100 → 80 (internal)
model:      cross-encoder/ms-marco-MiniLM-L-6-v2
```

Cross-encoder reranker. Given a query and a list of candidate chunks, it scores each
(query, chunk) pair and returns a relevance score. More accurate than embedding similarity
alone — used to pick the best top-5 from the initial top-15 retrieved candidates.

---

### `reranker` — OPEA Reranker Wrapper

```yaml
container:  genie-ai-reranker
build:      genie-ai-overlay/reranker/Dockerfile-reranker_genie-ai
port:       6100 → 8000 (internal)
```

OPEA wrapper around `tei_reranker`. Applies the `RERANKING_STRATEGY`:

```
RERANKING_STRATEGY=slice        ← return top-N regardless of score
RERANKER_TOP_N=5                ← N=5
# alternatives:
# RERANKING_STRATEGY=threshold  ← discard below RERANKING_THRESHOLD=0.01
# RERANKING_STRATEGY=knee_threshold  ← automatic elbow detection
```

---

### `guardrail`

```yaml
container:  llm-guardrail
image:      opea/guardrails:latest
port:       9090
model:      google/gemma-3-4b-it  (via vllm-translation-guardrail:9031)
```

OPEA guardrail service. Wraps Gemma-3-4b for content safety checks. Called by
`dataprep-arango-service` before ingesting content. Uses `OPEA_LLM_GUARD` component.

---

### `textgen`

```yaml
container:  llm-textgen
image:      opea/llm-textgen:latest
port:       9000
```

OPEA text generation wrapper around `vllm:8000`. Used by `backend` for direct completions
(non-RAG path). Exposes `/v1/chat/completions` in OPEA format.

---

### `translation`

```yaml
container:  translation-microservice
image:      opea/translation:latest
port:       9030
model:      google/gemma-3-4b-it  (via vllm-translation-guardrail:9031)
```

OPEA translation microservice. Separate from the backend's built-in translation — this is
the OPEA-standard translation endpoint used when OPEA components need to translate content.
The backend's own translation uses a different code path (CPU NLLB or GPU Gemma directly).

---

## 7. Infrastructure <a name="infrastructure"></a>

### `arango-vector-db`

```yaml
container:  arango-vector-db
image:      arangodb/arangodb:3.12.4
port:       8529
data:       /root/arango_data  (host path mount — NOT a named volume)
auth:       root / ${ARANGO_PASSWORD}
```

ArangoDB is used for **three different purposes** in this system:

| Purpose | Collection/Graph | Who uses it |
|---|---|---|
| RAG chunk storage | Graph: `GRAPH_TEST`, collections: `chunk`, `document`, `belongs_to` | dataprep (write), retriever (read) |
| Chat history | Collections: `chat_sessions`, `messages` | backend (read/write) |
| Weather forecast cache | Collections: `forecasts`, `risk_assessments` | weather-mcp-standalone (read/write) |

> **Data survives container rebuilds** because the data directory is mounted from the host
> (`/root/arango_data`), not from a Docker volume. Back up this directory before any
> ArangoDB version upgrades.

ArangoDB 3.12 is required for `--experimental-vector-index=true`, which enables the
HNSW-based approximate nearest-neighbor search used by the retriever.

**Web UI:** `http://localhost:8529` (not exposed via nginx — direct access only)

---

### `redis-cache`

```yaml
container:  redis-cache
image:      redis:7-alpine
port:       6379  (mapped to TRANSLATION_CACHE_PORT)
auth:       TRANSLATION_CACHE_PASSWORD
persistence: appendonly yes
```

Used exclusively as a translation cache by `backend`. When a string has already been
translated (e.g. Bengali → English), the result is stored in Redis so the next identical
request skips the LLM/NLLB call entirely.

---

### `clamav`

```yaml
container:  clamav
image:      clamav/clamav
port:       3310
```

Antivirus scanner. `document-repository` sends every uploaded file to ClamAV via the
clamd protocol on port 3310 before accepting it. Files that fail the scan are rejected
and never stored or ingested.

---

## 8. Data Flows <a name="data-flows"></a>

### RAG Query (end-to-end)

```
1. Browser → HTTPS POST /api/query
2. nginx → Kong → backend:3000  (auth verified)
3. query-service.js:
     a. Weather router evaluates tiers 0–4 → result: RAG
     b. runOPEAWorker() → POST chatqna-xeon-backend-server:8888/v1/chatqna
4. chatqna-xeon-backend-server:
     a. POST embedding:6000   → BAAI/bge-base-en-v1.5 → 768-dim query vector
     b. POST retriever:7025   → ArangoDB HNSW search, FETCH_K=15 candidates
     c. POST reranker:6100    → ms-marco-MiniLM-L-6-v2 → top 5 ranked
     d. POST vllm:8000        → ibm-granite/granite-3.3-2b-instruct
                                 system_prompt + user_profile + history + chunks
5. Response propagates back → backend → browser
```

### Weather Query (end-to-end)

```
1. Browser → HTTPS POST /api/query
2. nginx → Kong → backend:3000  (auth verified)
3. query-service.js:
     a. Weather router → Tier 1 (hard event) OR Tier 3 (ambiguous → Granite says YES)
     b. POST weather-mcp-standalone:8100/query { "query": "..." }
4. weather-mcp-standalone (agent.py):
     a. POST vllm-translation-guardrail:9031  → Gemma-3-4b-it → JSON intent
     b. _find_district() → local district lookup (no LLM)
     c. ArangoDB.get_latest_forecast(district, max_age=6h) → cached data
     d. RiskEngine.classify(forecast) → tier 0–4 (no LLM)
     e. POST vllm-translation-guardrail:9031  → Gemma-3-4b-it → explanation text
5. Response propagates back → backend → browser
```

### Document Ingestion (end-to-end)

```
1. Browser → HTTPS POST /api/upload
2. nginx → Kong → document-repository:3001
3. document-repository:
     a. ClamAV scan (clamav:3310) → reject if infected
     b. Store file on doc_repo_uploads volume
     c. Notify backend (via http-service JWT token)
     d. Trigger dataprep-arango-service:5000/v1/dataprep/ingest
4. dataprep-arango-service (genieai_dataprep_arangodb.py):
     a. Docling content extraction (or HTMLHeaderTextSplitter for HTML)
     b. Chunk by type (MD=500 chars, DOCX=1000, XLSX=1500, overlap=50)
     c. POST tei:7000 for each chunk → 768-dim embedding vectors
     d. POST vllm:8000 for each batch → Granite assigns 1–3 semantic labels
     e. Store (chunk text + vector + labels) in ArangoDB GRAPH_TEST
5. Document becomes queryable immediately
```

---

## 9. Key Configuration File: .env <a name="env"></a>

All services read from `/root/mewa_v2/.env`. Key sections:

```bash
# ── Ports ──────────────────────────────────────────────
FRONTEND_PORT=8090
BACKEND_PORT=3000
DOC_REPO_PORT=3001
OPEA_PORT=8888
DATAPREP_PORT=6007         # maps to container port 5000
RETRIEVER_SERVICE_PORT=7025
GUARDRAIL_SERVICE_PORT=9090
WEATHER_MCP_PORT=8100      # note: internal URL uses 8000

# ── ArangoDB ───────────────────────────────────────────
ARANGO_URL=http://arango-vector-db:8529
ARANGO_DB_NAME=genie-ai
ARANGO_GRAPH_NAME=GRAPH_TEST

# ── Main LLM (vllm) ────────────────────────────────────
VLLM_ENDPOINT=http://vllm:8000
VLLM_LLM_MODEL_ID=ibm-granite/granite-3.3-2b-instruct
VLLM_GPU_UTIL=0.35
VLLM_MAX_MODEL_LEN=16384
VLLM_DTYPE=half

# ── Translation/Guardrail LLM ──────────────────────────
VLLM_TRANSLATION_ENDPOINT=http://vllm-translation-guardrail:9031
VLLM_TRANSLATION_MODEL_ID=google/gemma-3-4b-it
VLLM_TRANSLATION_GPU_UTIL=0.35

# ── Embedding + Reranking ──────────────────────────────
EMBEDDING_MODEL_ID=BAAI/bge-base-en-v1.5
RERANKER_MODEL_ID=cross-encoder/ms-marco-MiniLM-L-6-v2
RETRIEVER_ARANGO_K=5
RETRIEVER_ARANGO_FETCH_K=15
RERANKER_TOP_N=5

# ── Chunking sizes ─────────────────────────────────────
DATAPREP_CHUNK_SIZE_MD=500
DATAPREP_CHUNK_SIZE_PDF=500
DATAPREP_CHUNK_SIZE_DOCX=1000
DATAPREP_CHUNK_SIZE_XLSX=1500
DATAPREP_CHUNK_OVERLAP=50

# ── Weather routing ────────────────────────────────────
WEATHER_ENABLED=true
WEATHER_MCP_URL=http://weather-mcp-standalone:8000

# ── System prompt (see ME_chatbot_prompt_and_router_configuration.md) ──
CHATQNA_SYSTEM_PROMPT="<INSTRUCTIONS>\n..."   # single line, \n for newlines
CHATQNA_ENFORCE_ABSTENTION=true

# ── Auth ───────────────────────────────────────────────
JWT_SECRET=...
JWT_EXPIRES_IN=24h
```

**Rules for editing `.env`:**
- Only one definition of each variable — the parser uses the first occurrence
- No backslash line continuation — collapses to `\` silently
- After any change: `docker compose up -d <service>` (not `docker restart`)

---

## 10. Port Map <a name="port-map"></a>

| Port | Container | Service |
|---|---|---|
| **443** | nginx | HTTPS entry point (user-facing) |
| 3000 | backend | Node.js API server |
| 3001 | doc-repo-dev | Document repository |
| 3310 | clamav | ClamAV antivirus daemon |
| 5432 | kong-database | PostgreSQL (Kong config store) |
| 6000 | embedding | OPEA embedding wrapper |
| 6100 | genie-ai-reranker | OPEA reranker wrapper (maps to 8000 inside) |
| 6379 | redis-cache | Redis translation cache |
| 6666 | http-service | JWT token vending service |
| 6007 | genie-ai-dataprep-arango | Dataprep ingest API (maps to 5000 inside) |
| 7000 | tei-embedding-serving | HF TEI embedding inference (maps to 80) |
| 7025 | genie-ai-retriever-arango | OPEA retriever |
| 7100 | tei-reranker-serving | HF TEI reranker inference (maps to 80) |
| 8000 | vllm-vllm-2 | vLLM main (Granite 3.3-2b) |
| 8010 | kong | Kong proxy |
| 8090 | frontend | Vue.js SPA |
| 8100 | weather-mcp-standalone | Weather agent FastAPI |
| 8443 | kong | Kong proxy (SSL) |
| 8529 | arango-vector-db | ArangoDB web UI + API |
| 8888 | genie-ai-chatqna-server | OPEA ChatQnA megaservice |
| 9000 | llm-textgen | OPEA textgen wrapper |
| 9030 | translation-microservice | OPEA translation wrapper |
| 9031 | vllm-vllm-translation-guardrail | vLLM translation (Gemma-3-4b) |
| 9090 | llm-guardrail | OPEA guardrail |
| 5173 | chatqna-xeon-ui-server | OPEA default UI (legacy, unused) |
| 80 | chatqna-xeon-nginx-server | OPEA default nginx (legacy, unused) |

---

## 11. Common Operations <a name="operations"></a>

### Starting the full stack

```bash
cd /root/mewa_v2
docker compose up -d
```

**GPU services start slowly.** Dependency order matters:
- `vllm` must be healthy before `tei`, `tei_reranker`, `dataprep`, `chatqna-server`
- `vllm-translation-guardrail` must be healthy before `tei`, `tei_reranker` (start_period=300s)
- `arango-vector-db` must be healthy before `backend`, `dataprep`, `retriever`
- `redis-cache` must be healthy before `backend`

### Updating the system prompt

```bash
# 1. Edit .env — ensure ONLY ONE active CHATQNA_SYSTEM_PROMPT line
nano /root/mewa_v2/.env

# 2. Recreate the container (not restart — restart reuses old env)
docker compose up -d chatqna-xeon-backend-server

# 3. Verify
docker exec genie-ai-chatqna-server env | grep CHATQNA_SYSTEM_PROMPT
```

### Rebuilding the frontend

```bash
docker compose build frontend
docker compose up -d frontend
# Nginx auto-reconnects via DNS re-resolution — no nginx reload needed
```

### Hot-patching a file without rebuild

```bash
# Backend service (e.g. router logic change)
docker cp components/gov-chat-backend/services/query-service.js genieai_mvp-backend-1:/app/services/query-service.js
docker restart genieai_mvp-backend-1   # OK for backend — does not need .env changes

# Frontend runtime config (no rebuild needed)
docker cp components/gov-chat-frontend/public/config/genie-ai-config.json \
    genieai_mvp-frontend-1:/app/dist/config/genie-ai-config.json

# Nginx config reload (no restart needed)
docker cp api-gateway-solution/nginx/conf/default.conf nginx:/etc/nginx/conf.d/default.conf
docker exec nginx nginx -s reload
```

### Re-ingesting a document

```bash
# First, retract the old version via the UI or API, then re-upload
# Or directly trigger dataprep:
curl -X POST http://localhost:6007/v1/dataprep/ingest \
  -H "Content-Type: multipart/form-data" \
  -F "files=@/path/to/document.md"
```

### Checking service health

```bash
# All containers
docker compose ps

# Specific health checks
curl http://localhost:8000/health       # vllm main
curl http://localhost:9031/health       # vllm translation
curl http://localhost:8100/health       # weather agent
curl http://localhost:8529/_api/version # ArangoDB

# Check what system prompt a running container sees
docker exec genie-ai-chatqna-server env | grep CHATQNA_SYSTEM_PROMPT
```

### ArangoDB web console

Access directly (not via nginx):
```
http://<server-ip>:8529
user: root
password: ${ARANGO_PASSWORD}  (from .env)
database: genie-ai
graph: GRAPH_TEST
```

### Trigger weather pipeline manually

```bash
curl -X POST http://localhost:8100/internal/run-pipeline
# Returns immediately; pipeline runs in background
# Fetches open-meteo data for all 64 Bangladesh districts
```

---

## Related Documents

| Document | Topic |
|---|---|
| `ME_chatbot_prompt_and_router_configuration.md` | Deep dive: system prompt, weather router, all LLM models |
| `ME_everything_about_RAG.md` | Chunking strategy, document ingestion details, ArangoDB schema |
| `ME_overlay_rag_strategy.md` | Labeling strategy, retrieval tuning |
| `GENIE.AI-Installation-Configuration-Guide.md` | First-time server setup |
| `MEWA-ARCHITECTURE.md` | Earlier architecture notes (may be superseded by this document) |
| `router_logic.md` | Weather router design rationale |
