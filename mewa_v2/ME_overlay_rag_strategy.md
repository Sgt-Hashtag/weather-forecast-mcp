# RAG Architecture — GENIE-AI Overlay Stack

> Based on code analysis of the live docker-compose stack. All other documentation files are outdated — this reflects what the code actually does.

---

## Overview

The system is **not vanilla OPEA**. It uses OPEA's microservice patterns as a base, but the `genie-ai-overlay/` layer wraps every component with custom logic: semantic labeling, ArangoDB graph storage, label-based filtering, and graph traversal. The result is a seven-service pipeline.

```
USER QUERY
    ↓
[ChatQnA Orchestrator]  port 8888   ← mega-service, assembles the full pipeline
    ↓
    ├── [TEI Embedding Wrapper]  port 6000  → TEI Service  port 7000
    ├── [ArangoDB Retriever]     port 7025  ← custom GENIE component
    ├── [TEI Reranker Wrapper]   port 6100  → TEI Reranker port 7100
    └── [vLLM]                   port 8000  (+ Translation vLLM port 9031)
```

**Database**: ArangoDB 3.12.4 (`genie-ai` database)
**Embedding model**: `BAAI/bge-base-en-v1.5` — 768-dimensional dense vectors
**LLM**: `ibm-granite/granite-3.3-2b-instruct` served via vLLM

---

## Part 1 — Ingestion Pipeline

**Entry point**: `POST /v1/dataprep/ingest_file` (port 5000)
**Code**: `genie-ai-overlay/dataprep/genieai_dataprep_arangodb.py`

### Stage 1: Extraction & Chunking

Docling (AI-powered layout-aware extraction) is the default (`CONTENT_EXTRACTION_METHOD=docling`). OPEA standard extraction is the fallback.

Chunking strategy depends on format:

| Format | Splitter | Chunk size |
|--------|----------|------------|
| PDF / HTML | HTMLHeaderTextSplitter (respects headings) | 500 tokens |
| DOCX | RecursiveCharacterTextSplitter | 1 000 tokens |
| XLSX | RecursiveCharacterTextSplitter | 1 500 tokens |
| PPTX / TXT / MD | RecursiveCharacterTextSplitter | 500 tokens |

Overlap: 50 tokens (default). Empty/whitespace chunks are filtered out immediately.

### Stage 2: Safety Guardrail (optional)

If `GUARDRAIL_ENABLED=true`, each chunk is sent to `http://guardrail:9090/v1/guardrails` before further processing. Chunks that fail the check are dropped.

### Stage 3: Semantic Labeling

This is the most important ingestion step. Every chunk is automatically assigned labels from the organization's knowledge taxonomy (fetched live from the backend: `GET /api/service-categories/categories`). Labels are stored on the chunk and are the primary basis for retrieval filtering.

Three strategies (selected by `LABELING_STRATEGY`):

**A. LLM-based** (default, most accurate)

Each chunk is sent to vLLM with a system prompt instructing the model to assign 1–4 labels from the full taxonomy. Runs with concurrency control (`MAX_CONCURRENT_BATCHES=5`, 3 retries per chunk). Synonym/fuzzy matching handles minor label name variations.

**B. Embedding-based** (fast, no LLM calls)

Cosine similarity between chunk embedding and each label embedding. Label is assigned if similarity ≥ `EMBEDDING_LABEL_THRESHOLD` (default 0.75).

**C. BM25-based** (keyword / TF-IDF)

BM25 score of chunk against tokenized label names. Threshold: `BM25_LABEL_THRESHOLD=2.00`. Best for terminology-heavy documents.

### Stage 4: Graph Storage in ArangoDB

Each labeled chunk is converted to a graph document. LangChain's `LLMGraphTransformer` extracts named entities and relationships from chunk text, then everything is inserted into ArangoDB in parallel batches of 10 (`MAX_CONCURRENT_BATCHES=5`).

**ArangoDB graph structure** (graph name `GRAPH_TEST` — configurable):

```
GRAPH_TEST/
├── SOURCE       (vertex)  — original text chunks
│                            fields: text, embedding[768], file_id, chunk_labels, metadata
├── ENTITY       (vertex)  — extracted named entities
│                            fields: description, embedding[768], properties
├── HAS_SOURCE   (edge)    — connects ENTITY → SOURCE
│                            fields: embedding[768], relationship_description
└── LINKS_TO     (edge)    — connects ENTITY → ENTITY (relationships)
                             fields: text, embedding[768], file_id, relationship_type
```

All four node/edge types carry 768-dim BAAI/bge-base-en-v1.5 embeddings. `embed_source=True`, `embed_nodes=True`, `embed_relationships=True` — every object in the graph is searchable by semantic similarity.

### Stage 5: Status Tracking

Ingestion progress is reported back to the Document Repository:

```
Ingesting → Ingested        (success)
Ingesting → Ingestion Error (failure)
Ingesting → Killed          (admin cancellation)
Ingesting → Retracted       (rollback)
```

Per-stage log entries are written via `POST /api/files/{file_id}/ingestion-log`.

---

## Part 2 — Query & Retrieval Pipeline

**Entry point**: `POST /v1/retrieval` (port 7025)
**Code**: `genie-ai-overlay/retriever/genieai_retriever_arangodb.py`

### Step 1: Query Embedding

```
User query text
    → [OPEA Embedding Wrapper] port 6000
    → [TEI Service] port 7000  (BAAI/bge-base-en-v1.5)
    → 768-dimensional dense vector
```

If the caller already provides a precomputed embedding it is used directly, skipping this step.

### Step 2: Vector Similarity Search

Three modes (selected per request):

**A. Similarity with score threshold** (default)

```aql
FOR doc IN SOURCE
  FILTER ["Service A", "Service B"] ANY IN doc.chunk_labels
  LET score = COSINE_SIMILARITY(doc.embedding, @query_embedding)
  FILTER score >= 0.5
  SORT score DESC
  LIMIT 5
  RETURN doc
```

`k=5` (default, `RETRIEVER_ARANGO_K`), `score_threshold=0.5` (`RETRIEVER_ARANGO_SCORE_THRESHOLD`).

**B. Maximum Marginal Relevance (MMR)**

Retrieves `fetch_k=15` candidates first, then selects `k=5` that maximise both relevance and diversity via `lambda_mult=0.5`. Prevents near-duplicate results when the index has many very similar chunks.

**C. Standard similarity search**

Plain cosine top-k with no threshold filtering.

### Step 3: Label-Based Metadata Filtering

The retrieval request carries a `retrieval_context` with category and service labels. These are translated into AQL filter clauses before the vector search runs:

- `ARANGO_FILTER_STRATEGY=AND` → `ALL IN doc.chunk_labels` (chunk must have **all** requested labels)
- `ARANGO_FILTER_STRATEGY=OR` → `ANY IN doc.chunk_labels` (chunk must have **at least one**)

This narrows the vector search to the relevant service domain before cosine similarity is even computed.

### Step 4: Graph Traversal (optional)

If `RETRIEVER_ARANGO_TRAVERSAL_ENABLED=true`, after the initial vector search the retriever walks the knowledge graph to enrich each result with semantically related context.

Default mode (`search_start=chunk`):

```aql
FOR node IN 1..1 INBOUND @chunk_id GRAPH_TEST_HAS_SOURCE
  FOR node2, edge IN 1..3 ANY node GRAPH_TEST_LINKS_TO
    LET score = COSINE_SIMILARITY(edge.embedding, @query_embedding)
    FILTER score >= 0.5
    SORT score DESC
    LIMIT 3
    RETURN edge.text
```

Depth: `RETRIEVER_ARANGO_TRAVERSAL_MAX_DEPTH=3`, max returned per seed: `RETRIEVER_ARANGO_TRAVERSAL_MAX_RETURNED=3`.

The enriched context appended to each chunk looks like:

```
<original chunk text>
------
RELATED INFORMATION:
------
<graph-traversal text 1>
<graph-traversal text 2>
```

Two other traversal modes exist: `edge` (start from relationship edges) and `node` (start from entity nodes).

### Step 5: Optional Summarization

If `SUMMARIZER_ENABLED=true`, each retrieved chunk is individually summarized by vLLM using a query-focused summarization prompt before being passed to the reranker.

---

## Part 3 — Reranking

**Entry point**: `/v1/reranking` (port 6100)
**Code**: `genie-ai-overlay/reranker/genieai_tei_reranker.py`
**Backend**: TEI reranker service port 7100 (`sentence-transformers/bge-reranker-base`, cross-encoder)

The cross-encoder scores each `(query, document)` pair jointly — unlike the bi-encoder used for retrieval, it sees both texts at once and produces a single relevance score per document.

Three reranking strategies (`RERANKING_STRATEGY`):

| Strategy | Behaviour |
|----------|-----------|
| `threshold` | Keep docs with score ≥ `RERANKING_THRESHOLD` (default 0.9) |
| `slice` | Keep top-`RERANKER_TOP_N` docs (default 2) |
| `knee_threshold` | Automatic threshold detection based on score distribution |

---

## Part 4 — LLM Generation

**Entry point**: `/v1/chat/completions` via ChatQnA orchestrator (port 8888)
**Code**: `genie-ai-overlay/chatqna/genieai_chatqna.py`
**LLM**: vLLM OpenAI-compatible API at `http://vllm:8000`

### Prompt assembly

```
[System instructions]
  "You are a friendly information assistant.
   Answer ONLY using provided knowledge base content."

[User context]
  Sanitized user details (age, name, preferences — PII stripped)

[Chat history]
  Previous turns, translated to English if needed

[Retrieved knowledge base context]
  Top-N reranked chunks (with optional graph traversal enrichment)
```

Token budget management: the tokenizer (`AutoTokenizer.from_pretrained(LLM_MODEL)`) counts the assembled prompt tokens. If `prompt_tokens + max_answer_tokens > max_model_tokens - 200`, the chat history is truncated to fit. Retrieved context is never truncated — it has already been filtered by the reranker.

### Generation parameters

```python
model       = "ibm-granite/granite-3.3-2b-instruct"
max_tokens  = 1024
temperature = 0.01   # near-deterministic for factual answers
```

### Multilingual support

A second vLLM instance (port 9031, `google/gemma-3-4b-it` 4B) handles translation. User queries in non-English languages are detected and translated to English before retrieval runs. The final response is translated back before delivery.

---

## Full Data Flow

```
INGESTION
─────────
Document file
  → Docling extraction (layout-aware)
  → Chunking (format-aware sizes, 50-token overlap)
  → Guardrail check (optional)
  → LLM semantic labeling (vLLM, live taxonomy from backend)
  → LLMGraphTransformer entity/relationship extraction
  → ArangoDB batch insert
      SOURCE vertices  (chunk text + 768-dim embedding + labels)
      ENTITY vertices  (named entities + 768-dim embeddings)
      LINKS_TO edges   (relationships + 768-dim embeddings)
      HAS_SOURCE edges (entity → source pointers)
  → Document Repository status update + audit log


QUERY
─────
User question  (auto-detected language)
  → [Optional translation] to English via Gemma 3-4B
  → [TEI] query embedding → 768-dim BAAI/bge-base-en-v1.5 vector
  → [ArangoDB Retriever]
      label filter (AND/OR from retrieval_context)
      cosine similarity search (top-5, threshold 0.5)
      optional graph traversal (depth 3, top-3 per seed)
      optional per-chunk summarization
  → [TEI Reranker] cross-encoder rescoring (top-2 by default)
  → [ChatQnA] prompt assembly
      system prompt + user context + history + reranked chunks
  → [vLLM Granite 3.3-2B] generation (temp 0.01, max 1024 tokens)
  → [Optional translation] response back to user's language
  → Response with source citations
```

---

## Model & Component Reference

| Component | Model | Port | Output |
|-----------|-------|------|--------|
| Embedding (query + chunks) | `BAAI/bge-base-en-v1.5` | TEI: 7000 / Wrapper: 6000 | 768-dim vector |
| Reranker | `sentence-transformers/bge-reranker-base` | TEI: 7100 / Wrapper: 6100 | relevance score (cross-encoder) |
| LLM generation | `ibm-granite/granite-3.3-2b-instruct` | 8000 | text |
| Translation | `google/gemma-3-4b-it` | 9031 | text |
| Vector + graph store | ArangoDB 3.12.4 | 8529 | — |
| Orchestrator | ChatQnA mega-service | 8888 | — |

---

## Key Environment Variables

```env
# Embedding
EMBEDDING_MODEL_ID=BAAI/bge-base-en-v1.5
TEI_EMBEDDING_ENDPOINT=http://tei:80

# Retrieval
RETRIEVER_ARANGO_K=5
RETRIEVER_ARANGO_SCORE_THRESHOLD=0.5
RETRIEVER_ARANGO_TRAVERSAL_ENABLED=false
RETRIEVER_ARANGO_TRAVERSAL_MAX_DEPTH=3
ARANGO_FILTER_STRATEGY=OR

# Ingestion labeling
LABELING_STRATEGY=llm
EMBEDDING_LABEL_THRESHOLD=0.75
BM25_LABEL_THRESHOLD=2.00
MAX_CONCURRENT_BATCHES=5

# Generation
VLLM_LLM_MODEL_ID=ibm-granite/granite-3.3-2b-instruct
VLLM_ENDPOINT=http://vllm:8000
```

---

## What GENIE-AI Adds Over Bare OPEA

OPEA provides the microservice shell (embedding wrapper, reranker wrapper, ChatQnA skeleton). The `genie-ai-overlay/` layer adds:

1. **Semantic labeling pipeline** — LLM/embedding/BM25 strategies to tag every chunk with domain taxonomy labels at ingest time
2. **ArangoDB knowledge graph** — entities, relationships, and cross-chunk edges stored as first-class objects with their own embeddings
3. **Graph traversal retrieval** — enriches top-k chunks with semantically related neighbours from the graph
4. **Label-based pre-filtering** — narrows vector search to the relevant service domain before cosine similarity runs
5. **Backend taxonomy integration** — labels fetched live from the backend service, not hardcoded
6. **Document lifecycle management** — per-file status tracking and ingestion audit logs in the Document Repository
7. **JWT auth caching** — token refresh logic for secure inter-service calls
8. **Multilingual support** — separate translation vLLM instance, automatic query/response translation layer
