# Everything About the GENIE-AI RAG Pipeline

> A complete technical reference for the MEWA RAG stack.  
> Covers: architecture, ingestion, retrieval, reranking, generation, the ArangoDB knowledge graph, the knowledge taxonomy, GPU setup, all tuning knobs, and every bug fix applied so far.

---

## Table of Contents

1. [What This Is](#1-what-this-is)
2. [Architecture Overview](#2-architecture-overview)
3. [The Seven Services](#3-the-seven-services)
4. [Part 1 — Ingestion Pipeline](#4-part-1--ingestion-pipeline)
   - [Stage 1: Extraction & Chunking](#stage-1-extraction--chunking)
   - [Stage 2: Safety Guardrail](#stage-2-safety-guardrail-optional)
   - [Stage 3: Semantic Labeling](#stage-3-semantic-labeling)
   - [Stage 4: Graph Storage in ArangoDB](#stage-4-graph-storage-in-arangodb)
   - [Stage 5: Status Tracking](#stage-5-status-tracking)
5. [Part 2 — Query & Retrieval Pipeline](#5-part-2--query--retrieval-pipeline)
   - [Step 1: Query Embedding](#step-1-query-embedding)
   - [Step 2: Label-Based Pre-Filtering](#step-2-label-based-pre-filtering)
   - [Step 3: Vector Similarity Search](#step-3-vector-similarity-search)
   - [Step 4: Graph Traversal (optional)](#step-4-graph-traversal-optional)
   - [Step 5: Optional Summarization](#step-5-optional-summarization)
6. [Part 3 — Reranking](#6-part-3--reranking)
   - [The Model](#the-model)
   - [The Three Strategies](#the-three-strategies)
   - [Critical Bug Fix: Why the Reranker Was Eliminating All Documents](#critical-bug-fix-why-the-reranker-was-eliminating-all-documents)
7. [Part 4 — LLM Generation](#7-part-4--llm-generation)
8. [The ArangoDB Knowledge Graph](#8-the-arangodb-knowledge-graph)
9. [The Knowledge Taxonomy Hierarchy](#9-the-knowledge-taxonomy-hierarchy)
10. [GPU Setup & Docker Image Fixes](#10-gpu-setup--docker-image-fixes)
11. [Environment Variable Reference](#11-environment-variable-reference)
12. [Complete Data Flow Diagram](#12-complete-data-flow-diagram)
13. [Bugs Fixed & Changes Made](#13-bugs-fixed--changes-made)
14. [Tuning Guide](#14-tuning-guide)

---

## 1. What This Is

This is **not vanilla OPEA**. The system uses Intel's [OPEA (Open Platform for Enterprise AI)](https://github.com/opea-project/GenAIComps) framework as a microservice skeleton, then wraps every component with a custom `genie-ai-overlay/` layer that adds:

- Semantic labeling of every chunk at ingest time using a live knowledge taxonomy
- ArangoDB knowledge graph (entities, relationships, cross-chunk edges with their own embeddings)
- Graph traversal retrieval that enriches top-k chunks with semantically related graph neighbours
- Label-based pre-filtering that narrows vector search to the relevant service domain
- Document lifecycle management (status tracking + audit logs per file)
- JWT auth caching for secure inter-service calls
- Multilingual query/response translation

The deployment is entirely **Docker Compose** from `/root/mewa_v2/`.

---

## 2. Architecture Overview

```
USER QUERY (via Frontend)
        │
        ▼
┌───────────────────────────────────────────────────┐
│  ChatQnA Orchestrator  (port 8888)                 │
│  genieai_chatqna.py                                │
│  Mega-service: assembles and chains all below      │
└────────────────────┬──────────────────────────────┘
                     │
        ┌────────────┼────────────────────────────┐
        ▼            ▼                            ▼
 [Translation]  [TEI Embedding]           [TEI Reranker]
  port 9031      Wrapper: 6000              Wrapper: 6100
  Gemma 3-4B     TEI Service: 7000          TEI Service: 7100
                 BAAI/bge-base-en-v1.5      ms-marco-MiniLM-L-6-v2
                        │
                        ▼
              [ArangoDB Retriever]
               port 7025
               genieai_retriever_arangodb.py
                        │
                        ▼
              [ArangoDB 3.12.4]
               port 8529
               genie-ai database
               GRAPH_TEST graph

INGESTION (separate path)
User uploads file → Document Repository → Dataprep (port 5000)
genieai_dataprep_arangodb.py → ArangoDB
```

---

## 3. The Seven Services

| Service | Container name | Port | Purpose |
|---------|---------------|------|---------|
| ChatQnA Orchestrator | `genie-ai-chatqna` | 8888 | Mega-service, coordinates the full pipeline |
| TEI Embedding Wrapper | `genie-ai-embedding` | 6000 | OPEA wrapper that calls TEI |
| TEI Service (embedder) | `tei` | 7000 | Actual embedding inference: `BAAI/bge-base-en-v1.5` |
| ArangoDB Retriever | `genie-ai-retriever` | 7025 | Custom GENIE retriever with label filtering + graph traversal |
| TEI Reranker Wrapper | `genie-ai-reranker` | 6100 | OPEA wrapper + custom multi-strategy reranking |
| TEI Service (reranker) | `tei_reranker` | 7100 | Cross-encoder inference: `cross-encoder/ms-marco-MiniLM-L-6-v2` |
| vLLM (main LLM) | `vllm` | 8000 | Text generation: `ibm-granite/granite-3.3-2b-instruct` |
| vLLM (translation) | `vllm-translation` | 9031 | Translation: `google/gemma-3-4b-it` |
| ArangoDB | `arangodb` | 8529 | Graph + vector database |
| Dataprep | `genie-ai-dataprep` | 5000 | Ingestion pipeline |

---

## 4. Part 1 — Ingestion Pipeline

**Entry point**: `POST /v1/dataprep/ingest_file` on port 5000  
**Code**: `genie-ai-overlay/dataprep/genieai_dataprep_arangodb.py`  
**Class**: `GenieArangoDataprep` extends `OpeaArangoDataprep`

### Stage 1: Extraction & Chunking

Two extraction modes are available, selected by `CONTENT_EXTRACTION_METHOD`:

**Docling** (default, `CONTENT_EXTRACTION_METHOD=docling`)  
AI-powered layout-aware extraction. Understands tables, headings, figures, multi-column PDFs. Much higher quality than raw text extraction for structured documents.

**OPEA Standard** (fallback)  
Direct text extraction via LangChain document loaders.

After extraction, chunking is format-aware:

```python
# genieai_dataprep_arangodb.py:_load_and_chunk()

if path.endswith(".html"):
    text_splitter = HTMLHeaderTextSplitter(
        headers_to_split_on=[("h1", "H1"), ("h2", "H2")]
    )
else:
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=doc_path.chunk_size,      # from request, default 500
        chunk_overlap=doc_path.chunk_overlap, # from request, default 50
        add_start_index=True,
        separators=get_separators(),
    )
```

Typical chunk sizes passed from the frontend:

| Format | Chunk size | Overlap |
|--------|------------|---------|
| PDF / HTML | 500 tokens | 50 |
| DOCX | 1000 tokens | 50 |
| XLSX | 1500 tokens | 50 |
| PPTX / TXT / MD | 500 tokens | 50 |

After splitting, empty/whitespace-only chunks are immediately filtered out:

```python
valid_chunks = [c for c in plain_chunks if is_valid_content(c)]
```

### Stage 2: Safety Guardrail (optional)

Controlled by `GUARDRAIL_ENABLED=true/false` (default: `false`).

When enabled, each chunk is sent to `http://guardrail:9090/v1/guardrails`. If any chunk fails, the entire ingestion is aborted.

```python
async def _run_guardrail(self, plain_chunks: List[str]) -> Dict[str, Any]:
    if not GUARDRAIL_ENABLED:
        return {"success": True}
    async with aiohttp.ClientSession() as session:
        for i, text in enumerate(plain_chunks):
            async with session.post(GUARDRAIL_URL, json={"text": text}) as resp:
                result = await resp.json()
                if result.get("text") != text:
                    return {"success": False, "message": f"Chunk {i}: Blocked."}
    return {"success": True}
```

### Stage 3: Semantic Labeling

**This is the most critical ingestion step.** Every chunk is automatically assigned labels from the organization's knowledge taxonomy. These labels are stored on the chunk and are the primary basis for retrieval filtering at query time.

#### Where do labels come from?

Labels are fetched **live** from the backend service at the start of every ingestion:

```python
url = f"{BACKEND_SERVICE_URL}/api/service-categories/categories"
# Returns the taxonomy tree: Category → [Service, Service, ...]
# Example: "Agricultural Guidance" → ["Crop Management", "Pest & Disease Management", ...]
```

The full label list for MEWA is:

```
Weather Forecasts
  Short-term Forecast (1-7 days)
  Extended Forecast (8-15 days)
  Seasonal Outlook (1-6 months)
Extreme Weather Alerts
  Drought Alerts
  Flood & Waterlogging Alerts
  Heat Wave Alerts
  Cold Spell & Frost Alerts
  Cyclone & Storm Surge Alerts
  Heavy Rainfall Alerts
Crop Alert Thresholds
  Rice Threshold Profiles
  Other Crop Threshold Profiles
Agricultural Guidance
  Crop Management
  Irrigation & Water Management
  Pest & Disease Management
Geospatial Risk Profiles
  Geospatial Model Interpretation
General Reference
  Government Information & Schemes
```

#### Three Labeling Strategies

Selected by `LABELING_STRATEGY` env var:

**A. LLM-based** (`LABELING_STRATEGY=llm`) — default, most accurate

The LLM (vLLM) receives a system prompt instructing it to assign 1–4 labels from the full taxonomy. Runs with concurrency control (`MAX_CONCURRENT_BATCHES=5`, 3 retries per chunk). Synonym/fuzzy matching handles minor label name variations.

```python
LABEL_SELECTOR_SYSTEM_PROMPT = """
<SYSTEM INSTRUCTIONS>
You are a precise semantic labeler for a RAG knowledge graph.
Goal: Assign 1–4 MOST RELEVANT labels from the list below that best match the chunk content.
Rules:
- Return ONLY labels that are strongly relevant.
- Most chunks get 1–3 labels. Never exceed 5.
- Do NOT "maximize" coverage.
- Do NOT suggest new labels.
- If nothing fits well → return empty list.
- Use ONLY exact strings from the list.

Labels:
{labels_list}

Output strict JSON only:
{"labels": ["Label1", "Label2"]}
</SYSTEM INSTRUCTIONS>
"""
```

After the LLM responds, the code performs fuzzy matching to handle pluralization and case differences:

```python
# Exact match first
if label in all_labels:
    final_labels.add(label)

# Case-insensitive and plural/singular match
match = next((x for x in all_labels if x.lower() == label.lower()), None)
if not match and label.endswith('s'):
    match = next((x for x in all_labels if x.lower() == label[:-1].lower()), None)
```

**B. Embedding-based** (`LABELING_STRATEGY=embedding`) — fast, no LLM calls

Cosine similarity between chunk embedding and each label embedding. Label is assigned if:

```python
sim = dot(label_vec, chunk_vec) / (norm(label_vec) * norm(chunk_vec))
if sim >= EMBEDDING_LABEL_THRESHOLD:  # default 0.75
    selected.append(label)
```

**C. BM25-based** (`LABELING_STRATEGY=bm25`) — keyword / TF-IDF

BM25 score of chunk text against tokenized label names:

```python
tokenized_labels = [re.findall(r"\b\w+\b", l.lower()) for l in all_labels]
bm25 = BM25Okapi(tokenized_labels)
tokens = re.findall(r"\b\w+\b", text.lower())
scores = bm25.get_scores(tokens)
selected = [all_labels[i] for i, s in enumerate(scores) if s >= BM25_LABEL_THRESHOLD]
# BM25_LABEL_THRESHOLD default: 2.00
```

### Stage 4: Graph Storage in ArangoDB

Each labeled chunk is converted to a LangChain `Document` with metadata:

```python
Document(
    page_content=doc["text"],
    metadata={
        "file_id": input.file_id,
        "file_path": input.storage_path,
        "chunk_index": i,
        "chunk_labels": doc["labels"]   # ← e.g. ["Pest & Disease Management", "Rice Threshold Profiles"]
    }
)
```

Then **LangChain's `LLMGraphTransformer`** extracts named entities and relationships from each chunk's text. Everything is inserted in parallel batches of 10 (`BATCH_SIZE=10`, up to `MAX_CONCURRENT_BATCHES=5` concurrent batches):

```python
graph_docs = await asyncio.to_thread(
    self.llm_transformer.convert_to_graph_documents, batch_docs
)
await asyncio.to_thread(
    self.graph.add_graph_documents,
    graph_documents=graph_docs,
    include_source=True,
    graph_name=graph_name,          # "GRAPH_TEST"
    use_one_entity_collection=True,
    embeddings=self.embeddings,
    embed_source=True,              # embed chunks
    embed_nodes=True,               # embed entities
    embed_relationships=True,       # embed relationship edges
    capitalization_strategy="upper"
)
```

**ArangoDB graph structure** (graph name `GRAPH_TEST`):

```
GRAPH_TEST/
├── GRAPH_TEST_SOURCE       (vertex collection)
│   Stores original text chunks
│   Fields: text, embedding[768], file_id, chunk_labels, metadata
│
├── GRAPH_TEST_ENTITY       (vertex collection)
│   Stores extracted named entities (e.g. "BROWN PLANT HOPPER", "MALATHION")
│   Fields: description, embedding[768], properties
│
├── GRAPH_TEST_HAS_SOURCE   (edge collection)
│   Connects: ENTITY → SOURCE
│   Fields: embedding[768], relationship_description
│
└── GRAPH_TEST_LINKS_TO     (edge collection)
    Connects: ENTITY → ENTITY (relationships)
    Fields: text, embedding[768], file_id, relationship_type
    Example: "CHLORPYRIFOS BRANDED_AS DURSBAN 20 EC"
```

All four node/edge types carry 768-dimensional `BAAI/bge-base-en-v1.5` embeddings. Every object in the graph is searchable by semantic similarity.

### Stage 5: Status Tracking

The ingestion state machine:

```
Pending → Ingesting → Ingested        (success)
                    → Ingestion Error (failure, auto-retraction runs)
                    → Killed          (admin cancellation via kill switch)
                    → Retracted       (rollback complete)
```

Per-stage log entries are written via `POST /api/files/{file_id}/ingestion-log`.  
Status updates go to `PATCH /api/files/{file_id}/status`.

If ingestion fails, **auto-retraction** deletes everything already inserted:

```
Chunks (SOURCE vertices)
→ HAS_SOURCE edges connecting those chunks
→ LINKS_TO edges associated with this file's content
→ Orphaned entities (entities with no remaining incoming edges)
→ LINKS_TO edges connected to those orphans
```

---

## 5. Part 2 — Query & Retrieval Pipeline

**Entry point**: `POST /v1/retrieval` on port 7025  
**Code**: `genie-ai-overlay/retriever/genieai_retriever_arangodb.py`  
**Class**: `GenieaiArangoRetriever`

### Step 1: Query Embedding

```
User query text
  → [OPEA Embedding Wrapper] port 6000
  → [TEI Service] port 7000
  → BAAI/bge-base-en-v1.5
  → 768-dimensional dense vector
```

If the caller already provides a precomputed embedding it is used directly.

### Step 2: Label-Based Pre-Filtering

The retrieval request carries a `context` object with `categoryLabel` and `serviceLabels`. These are translated into an AQL `FILTER` clause **before** vector search runs:

```python
# From the frontend request body:
# "context": {"categoryLabel": "Weather Forecasts", "serviceLabels": ["Pest & Disease Management"]}

labels_to_filter = ["Weather Forecasts", "Pest & Disease Management"]

# OR strategy (ARANGO_FILTER_STRATEGY=OR) — default
aql_filter_clause = 'FILTER (doc.chunk_labels != null) AND (["Weather Forecasts", "Pest & Disease Management"] ANY IN doc.chunk_labels)'

# AND strategy (ARANGO_FILTER_STRATEGY=AND) — stricter
aql_filter_clause = 'FILTER (doc.chunk_labels != null) AND (["Weather Forecasts", "Pest & Disease Management"] ALL IN doc.chunk_labels)'
```

`OR` means the chunk must have **at least one** of the requested labels.  
`AND` means the chunk must have **all** requested labels.

### Step 3: Vector Similarity Search

Three search modes (`RETRIEVER_ARANGO_SEARCH_MODE`):

**A. Similarity with score threshold** (`search_type=similarity_score_threshold`) — default

```aql
FOR doc IN GRAPH_TEST_SOURCE
  FILTER (doc.chunk_labels != null) AND (["Pest & Disease Management"] ANY IN doc.chunk_labels)
  LET score = COSINE_SIMILARITY(doc.embedding, @query_embedding)
  FILTER score >= 0.5
  SORT score DESC
  LIMIT 5
  RETURN doc
```

`k=5` (`RETRIEVER_ARANGO_K`), `score_threshold=0.5` (`RETRIEVER_ARANGO_SCORE_THRESHOLD`).

**B. Maximum Marginal Relevance (MMR)** (`search_type=mmr`)

Retrieves `fetch_k=15` candidates first, then selects `k=5` that maximise both relevance and diversity via `lambda_mult=0.5`. Prevents near-duplicate results.

**C. Standard similarity** (`search_type=similarity`)

Plain cosine top-k with no threshold.

### Step 4: Graph Traversal (optional)

Controlled by `RETRIEVER_ARANGO_TRAVERSAL_ENABLED=true/false` (default: `false`).

When enabled, after vector search the retriever walks the knowledge graph to enrich each result. Three traversal start modes:

**chunk** (default, `RETRIEVER_ARANGO_SEARCH_START=chunk`):

```aql
-- Starting from the matched chunk, find connected entities, then walk their relationships
LET raw = (
  FOR node IN 1..1 INBOUND doc GRAPH_TEST_HAS_SOURCE
    FOR node2, edge IN 1..3 ANY node GRAPH_TEST_LINKS_TO
      LET score = COSINE_SIMILARITY(edge.embedding, @query_embedding)
      FILTER score >= 0.5
      RETURN { score: score, text: edge.text }
)
FOR item IN raw
  SORT item.score DESC
  LIMIT 3
  RETURN item.text
```

The enriched output appended to each chunk:

```
<original chunk text>
------
RELATED INFORMATION:
------
['CHLORPYRIFOS BRANDED_AS DURSBAN 20 EC', 'INFESTATION STAGE BEGINS_WITH EARLY TILLERING TO MATURITY STAGE', ...]
```

**Important note**: the RELATED INFORMATION section helps the LLM but **hurts reranker scores** because the cross-encoder sees all-caps relationship strings that don't read naturally. This is why the reranking strategy is `slice` (rank-based) rather than `threshold` (score-based). See [Section 6](#6-part-3--reranking).

**node** (`search_start=node`): starts from entity vertices and walks outward.

**edge** (`search_start=edge`): starts from relationship edges and retrieves the source chunk.

Traversal depth: `RETRIEVER_ARANGO_TRAVERSAL_MAX_DEPTH=3`  
Max returned per seed: `RETRIEVER_ARANGO_TRAVERSAL_MAX_RETURNED=3`  
Score threshold for traversal edges: `RETRIEVER_ARANGO_TRAVERSAL_SCORE_THRESHOLD=0.5`

### Step 5: Optional Summarization

If `RETRIEVER_SUMMARIZER_ENABLED=true`, each retrieved chunk is individually summarized by vLLM using a query-focused prompt before being passed to the reranker:

```python
def generate_summarization_prompt(self, query: str, text: str) -> str:
    return f"""
    Summarize the Document below using the query as the foundation:
    Query: '{query}'
    
    If the Document has a 'RELATED INFORMATION' section, use it to help summarize.
    
    Document:
    ------
    {text}
    ------
    Your summary:
    """
```

---

## 6. Part 3 — Reranking

**Entry point**: `/v1/reranking` on port 6100  
**Code**: `genie-ai-overlay/reranker/genieai_tei_reranker.py`  
**Class**: `GenieTEIReranking` extends `OpeaTEIReranking`

### The Model

`cross-encoder/ms-marco-MiniLM-L-6-v2` served via TEI (port 7100).

This is a **cross-encoder**, which is fundamentally different from the bi-encoder used for retrieval:

| | Bi-encoder (retrieval) | Cross-encoder (reranking) |
|-|------------------------|--------------------------|
| Input | query OR document separately | query AND document together |
| Process | embed each independently → cosine similarity | sees both texts at once in one transformer pass |
| Speed | fast (precompute doc embeddings) | slow (must run for each query-doc pair) |
| Accuracy | good | better — sees full interaction between query and doc |
| Output | cosine similarity score (0–1) | raw logit score (unbounded) |

### The Three Strategies

```python
# genieai_tei_reranker.py

if reranking_strategy == "slice":
    # Return top-N documents by rank, regardless of absolute score
    # Safe to use even when scores are very low
    top_n = reranker_top_n if reranker_top_n else 1
    for best_response in decoded_response[:top_n]:
        reranking_results.append({
            "text": input.retrieved_docs[best_response["index"]].text,
            "score": best_response["score"]
        })

elif reranking_strategy == "threshold":
    # Only keep documents whose score meets the threshold
    # DANGEROUS: if threshold is too high, all docs are eliminated
    for best_response in decoded_response:
        if best_response["score"] >= reranking_threshold:
            reranking_results.append(...)

elif reranking_strategy == "knee_threshold":
    # Automatic threshold detection using the "knee" of the score distribution
    # Uses KneeLocator (from `kneed` library)
    kneedle = KneeLocator(indices, document_scores, curve="convex", direction="decreasing")
    cutoff = kneedle.knee + 1 if kneedle.knee is not None else len(document_scores)
    for i in range(cutoff):
        reranking_results.append(decoded_response[i])
```

### Critical Bug Fix: Why the Reranker Was Eliminating All Documents

**The problem** (discovered 2026-04-21):

Every query returned `source_documents: []` even though the retriever was finding 5 relevant chunks with cosine similarity scores of 0.73, 0.63, 0.56, 0.54, 0.54.

**Root cause 1**: Original `.env` had `RERANKING_THRESHOLD=0.90`. The `cross-encoder/ms-marco-MiniLM-L-6-v2` model outputs **raw logit scores**, not probabilities. On typical agricultural document queries, the best achievable score was ~0.27 — well below 0.90.

**Root cause 2 (deeper)**: After lowering the threshold to `0.01`, scores were still `[0.0032, 0.0013, 0.0007, 0.0005, 0.0001]` — all still below 0.01. The cause is the graph traversal RELATED INFORMATION section appended to each chunk. All-caps entity relationship strings like:

```
['BROWN PLANT HOPPER CAUSES_DAMAGE VIGNA ANGULARIS (COWPEA)', 
 'INFESTATION STAGE BEGINS_WITH EARLY TILLERING TO MATURITY STAGE']
```

...are not natural language. The cross-encoder, which was trained on natural language pairs (MS MARCO), gives very low relevance scores to any text with this pattern — even when the underlying chunk content is directly relevant.

**The fix**: Switch from `threshold` to `slice` strategy. This bypasses the absolute score entirely and always returns the top-N ranked documents:

```bash
# .env change
RERANKING_STRATEGY=slice   # was: threshold
RERANKER_TOP_N=5
RERANKING_THRESHOLD=0.01   # kept as backup
```

Then restart:
```bash
docker compose up -d reranker
```

**Why `slice` is correct for this architecture**: The reranker's job is to *reorder* the documents retrieved by the vector search (which already applied semantic similarity + label filtering). If the vector search returned 5 chunks about rice pests, the reranker should order them by relevance — not eliminate all of them. `slice` preserves this ordering function without the threshold gate.

---

## 7. Part 4 — LLM Generation

**Entry point**: `POST /v1/chat/completions` via ChatQnA orchestrator (port 8888)  
**Code**: `genie-ai-overlay/chatqna/genieai_chatqna.py`  
**LLM**: `ibm-granite/granite-3.3-2b-instruct` via vLLM (port 8000)

### Prompt Assembly

```
[System prompt]        ← CHATQNA_SYSTEM_PROMPT env var
                         "You are a helpful assistant..."

[User context]         ← Fetched from backend: GET /api/users/{userId}/context
                         Age, name, preferences — PII fields stripped
                         (password, salt, email, phoneNumber, etc.)

[Chat history]         ← Previous turns, translated to English if needed
                         Truncated to fit token budget

[Retrieved chunks]     ← Top-N reranked documents
                         Never truncated — already filtered by reranker
```

### Token Budget Management

```python
tokenizer = AutoTokenizer.from_pretrained(LLM_MODEL)
prompt_tokens = len(tokenizer.encode(assembled_prompt))

if prompt_tokens + max_answer_tokens > MAX_MODEL_LEN_TEXTGEN - 200:
    # Truncate CHAT HISTORY to fit
    # Retrieved context is NEVER truncated
    ...
```

`MAX_MODEL_LEN_TEXTGEN=4096` by default.

### Generation Parameters

```python
model       = "ibm-granite/granite-3.3-2b-instruct"
max_tokens  = 1024
temperature = 0.01   # near-deterministic for factual answers
```

### Multilingual Support

Language detection runs on every incoming query:

```python
from langdetect import detect
language = detect(query_text)  # e.g. "bn" for Bengali, "en" for English
```

If the query is not in English:
1. Query is translated to English by `google/gemma-3-4b-it` (port 9031)
2. Retrieval runs in English
3. Response is generated in English
4. Response is translated back to the user's language

Translation prompt template:
```python
prompt = f"Translate the following text to {target_language}. Only provide the translation, with no additional commentary or explanations. Text: \"{original_text}\""
```

### Abstention Handling

When `CHATQNA_ENFORCE_ABSTENTION=true` (default), the system instructs the LLM to abstain rather than hallucinate if the retrieved context does not contain enough information:

```
CHATQNA_ABSTENTION_INSTRUCTIONS=
"If the provided documents do not contain enough information to answer the question,
 say so clearly. Do not make up information."
```

---

## 8. The ArangoDB Knowledge Graph

Database: `genie-ai`  
Graph: `GRAPH_TEST`

### Collection Structure

```
GRAPH_TEST_SOURCE (vertex)
├── _key: auto-generated
├── text: "Brown plant hopper attacks rice at early tillering stage..."
├── embedding: [0.023, -0.145, 0.891, ...] (768 floats)
├── file_id: "document-uuid-here"
├── chunk_labels: ["Pest & Disease Management", "Rice Threshold Profiles"]
└── metadata: { file_path: "...", chunk_index: 42 }

GRAPH_TEST_ENTITY (vertex)
├── _key: auto-generated
├── description: "A sucking insect pest that causes hopper burn"
├── embedding: [0.031, -0.122, ...] (768 floats)
└── properties: { type: "PEST" }

GRAPH_TEST_HAS_SOURCE (edge)  ENTITY → SOURCE
├── _from: "GRAPH_TEST_ENTITY/12345"
├── _to:   "GRAPH_TEST_SOURCE/67890"
├── embedding: [0.015, ...] (768 floats)
└── relationship_description: "Brown plant hopper mentioned in this chunk"

GRAPH_TEST_LINKS_TO (edge)   ENTITY → ENTITY
├── _from: "GRAPH_TEST_ENTITY/111"
├── _to:   "GRAPH_TEST_ENTITY/222"
├── text: "CHLORPYRIFOS BRANDED_AS DURSBAN 20 EC"
├── embedding: [0.044, ...] (768 floats)
├── file_id: "document-uuid-here"
└── relationship_type: "BRANDED_AS"
```

### Useful AQL Queries

**Count everything in the graph:**
```aql
RETURN {
  sources: LENGTH(GRAPH_TEST_SOURCE),
  entities: LENGTH(GRAPH_TEST_ENTITY),
  has_source_edges: LENGTH(GRAPH_TEST_HAS_SOURCE),
  links_to_edges: LENGTH(GRAPH_TEST_LINKS_TO)
}
```

**Find all chunks labeled for a specific service:**
```aql
FOR doc IN GRAPH_TEST_SOURCE
  FILTER "Pest & Disease Management" IN doc.chunk_labels
  RETURN { id: doc._id, labels: doc.chunk_labels, preview: LEFT(doc.text, 100) }
```

**Find entities connected to a chunk:**
```aql
FOR edge IN GRAPH_TEST_HAS_SOURCE
  FILTER edge._to == "GRAPH_TEST_SOURCE/your_chunk_key"
  FOR entity IN GRAPH_TEST_ENTITY
    FILTER entity._id == edge._from
    RETURN entity.description
```

**Manual vector search (useful for debugging):**
```aql
-- Run after computing a query embedding externally
FOR doc IN GRAPH_TEST_SOURCE
  LET score = COSINE_SIMILARITY(doc.embedding, @your_query_embedding)
  FILTER score >= 0.4
  SORT score DESC
  LIMIT 10
  RETURN { score, labels: doc.chunk_labels, preview: LEFT(doc.text, 200) }
```

---

## 9. The Knowledge Taxonomy Hierarchy

### Storage in ArangoDB

The taxonomy is stored across five collections in ArangoDB:

| Collection | Contents |
|------------|----------|
| `serviceCategories` | Category documents (`_key`, `nameEN`, `catCode`) |
| `services` | Service documents (`_key`, `nameEN`) |
| `categoryServices` | Edge collection linking categories to services |
| `serviceCategoryTranslations` | Translated category names per language code |
| `serviceTranslations` | Translated service names per language code |

### The JSON Import File

Location: `components/gov-chat-backend/scripts/new-schema-scripts/mewa-hierarchy.json`

```json
[
  {
    "category": "Weather Forecasts",
    "services": [
      "Short-term Forecast (1-7 days)",
      "Extended Forecast (8-15 days)",
      "Seasonal Outlook (1-6 months)"
    ]
  },
  {
    "category": "Extreme Weather Alerts",
    "services": [
      "Drought Alerts",
      "Flood & Waterlogging Alerts",
      "Heat Wave Alerts",
      "Cold Spell & Frost Alerts",
      "Cyclone & Storm Surge Alerts",
      "Heavy Rainfall Alerts"
    ]
  },
  {
    "category": "Crop Alert Thresholds",
    "services": [
      "Rice Threshold Profiles",
      "Other Crop Threshold Profiles"
    ]
  },
  {
    "category": "Agricultural Guidance",
    "services": [
      "Crop Management",
      "Irrigation & Water Management",
      "Pest & Disease Management"
    ]
  },
  {
    "category": "Geospatial Risk Profiles",
    "services": ["Geospatial Model Interpretation"]
  },
  {
    "category": "General Reference",
    "services": ["Government Information & Schemes"]
  }
]
```

### How to Import

```bash
cd components/gov-chat-backend
node scripts/new-schema-scripts/create-knowledge-hierarchy.js \
  --file scripts/new-schema-scripts/mewa-hierarchy.json
```

> Must be run from `components/gov-chat-backend/`, not from the `new-schema-scripts/` subdirectory.  
> Node 14.6+ required (script uses private class method syntax `#loadInquirer`).

### Bug Fix: Frontend Showed Blank Labels + Selected All When Clicking One

**Root cause 1 (blank names)**: The import script only writes `nameEN` on the raw `serviceCategories` / `services` documents. The frontend's `getAllCategoriesWithServices()` query reads **exclusively** from the translations collections (`serviceCategoryTranslations`, `serviceTranslations`), which were empty after import.

**Root cause 2 (select-all)**: All 6 categories had `catCode: null`. The frontend uses `catCode` as the unique selector identifier for filtering.

**Fix** — three AQL queries run directly in ArangoDB:

```aql
-- Fix 1: Set catCode = _key for all categories
FOR doc IN serviceCategories
  UPDATE doc WITH { catCode: doc._key } IN serviceCategories

-- Fix 2: Populate EN category translations
FOR doc IN serviceCategories
  INSERT {
    serviceCategoryId: doc._key,
    languageCode: 'EN',
    translation: doc.nameEN,
    isActive: true,
    createdAt: '2026-04-21T00:00:00.000Z',
    updatedAt: '2026-04-21T00:00:00.000Z'
  } INTO serviceCategoryTranslations

-- Fix 3: Populate EN service translations
FOR doc IN services
  INSERT {
    serviceId: doc._key,
    languageCode: 'EN',
    translation: doc.nameEN,
    isActive: true,
    createdAt: '2026-04-21T00:00:00.000Z',
    updatedAt: '2026-04-21T00:00:00.000Z'
  } INTO serviceTranslations
```

---

## 10. GPU Setup & Docker Image Fixes

### The Dockerfile

Location: `genie-ai-overlay/dataprep/Dockerfile-dataprep_genie-ai`  
Base image: `nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04`

### Bug Fix 1: PyTorch Using CPU Instead of GPU

**Symptom**: Container logs showed `CUDA is not available. Using CPU.`

**Root cause**: Step F installs `docling` which pulls `torch==2.11.0`. That version is compiled for **CUDA 13.0**. The host driver version `535.288.01` only supports up to **CUDA 12.2**. PyTorch silently fell back to CPU.

**Fix**: Added Step F2 to the Dockerfile immediately after Step F to force-reinstall the correct PyTorch build:

```dockerfile
# Step F2: Pin PyTorch to a cu121 build compatible with the host NVIDIA driver (max CUDA 12.2).
# docling pulls torch 2.11+ (CUDA 13.0) which cannot initialise on this host — force-reinstall
# the latest torch available on the cu121 index so Docling and EasyOCR still work on GPU.
RUN pip install --no-cache-dir --force-reinstall \
    "torch==2.5.1+cu121" \
    "torchvision==0.20.1+cu121" \
    "torchaudio==2.5.1+cu121" \
    --index-url https://download.pytorch.org/whl/cu121 && \
    pip install --no-cache-dir "numpy<2" "scipy"
```

### Bug Fix 2: NumPy Incompatibility After PyTorch Downgrade

**Symptom**: Container failed to start with:
```
ImportError: _ARRAY_API not found in numpy.__array_api
```

**Root cause**: PyTorch 2.5.1 pulled in **NumPy 2.x** as a dependency. `easyocr` → `scipy.ndimage` → `_nd_image` was compiled against **NumPy 1.x** and is incompatible with NumPy 2.x.

**Fix**: The `pip install "numpy<2" "scipy"` at the end of Step F2 pins NumPy to the 1.x series and reinstalls scipy against it.

**To verify GPU is working** after rebuild:
```bash
docker exec genie-ai-dataprep python -c "import torch; print('CUDA:', torch.cuda.is_available())"
# Expected: CUDA: True
```

### Driver / CUDA Compatibility Reference

| NVIDIA Driver Version | Max CUDA Support |
|-----------------------|-----------------|
| 535.x                 | CUDA 12.2        |
| 545.x                 | CUDA 12.3        |
| 550.x                 | CUDA 12.4        |
| 560.x+                | CUDA 12.6+       |

Host driver is `535.288.01` → max CUDA 12.2 → must use `cu121` PyTorch builds.

---

## 11. Environment Variable Reference

### Ingestion (Dataprep)

```env
CONTENT_EXTRACTION_METHOD=docling    # docling | opea
LABELING_STRATEGY=llm                # llm | embedding | bm25
EMBEDDING_LABEL_THRESHOLD=0.75       # cosine sim threshold for embedding labeling
BM25_LABEL_THRESHOLD=2.00            # BM25 score threshold for bm25 labeling
DATAPREP_MAX_CONCURRENT_BATCHES=5    # parallel batches for graph insertion
GUARDRAIL_ENABLED=false              # true to enable per-chunk safety check
ARANGO_GRAPH_NAME=GRAPH_TEST         # ArangoDB graph name
BACKEND_SERVICE_URL=http://backend:3000
DOCUMENT_REPOSITORY_URL=http://document-repository:3001
```

### Retrieval

```env
RETRIEVER_ARANGO_K=5                    # top-k documents to retrieve
RETRIEVER_ARANGO_SCORE_THRESHOLD=0.5    # cosine similarity minimum
RETRIEVER_ARANGO_SEARCH_START=chunk     # chunk | node | edge
RETRIEVER_ARANGO_SEARCH_MODE=vector     # vector | hybrid
RETRIEVER_ARANGO_TRAVERSAL_ENABLED=false
RETRIEVER_ARANGO_TRAVERSAL_MAX_DEPTH=3
RETRIEVER_ARANGO_TRAVERSAL_MAX_RETURNED=3
RETRIEVER_ARANGO_TRAVERSAL_SCORE_THRESHOLD=0.5
RETRIEVER_ARANGO_TRAVERSAL_CONCURRENT_BATCHES=1
RETRIEVER_SUMMARIZER_ENABLED=false
ARANGO_FILTER_STRATEGY=OR              # OR | AND
RETRIEVER_ARANGO_DISTANCE_STRATEGY=COSINE
```

### Reranking

```env
RERANKING_STRATEGY=slice           # slice | threshold | knee_threshold
RERANKER_TOP_N=5                   # used by slice strategy
RERANKING_THRESHOLD=0.01           # used by threshold strategy (CURRENTLY INACTIVE — strategy is slice)
RERANKER_MODEL_ID=cross-encoder/ms-marco-MiniLM-L-6-v2
TEI_RERANKING_ENDPOINT=http://tei_reranker:80
```

### LLM Generation

```env
VLLM_LLM_MODEL_ID=ibm-granite/granite-3.3-2b-instruct
VLLM_ENDPOINT=http://vllm:8000
MAX_MODEL_LEN_TEXTGEN=4096
CHATQNA_SYSTEM_PROMPT=             # override system prompt
CHATQNA_ENFORCE_ABSTENTION=true
```

### Embedding

```env
EMBEDDING_MODEL_ID=BAAI/bge-base-en-v1.5
TEI_EMBEDDING_ENDPOINT=http://tei:80
```

---

## 12. Complete Data Flow Diagram

```
══════════════════════════════════════════════════════
INGESTION FLOW
══════════════════════════════════════════════════════

  User uploads file via Frontend
          │
          ▼
  Document Repository (port 3001)
  Stores file, generates file_id
  Status: Pending → Ingesting
          │
          ▼
  Dataprep Service (port 5000)
  GenieArangoDataprep.ingest_file_with_guardrail()
          │
          ├─[1]─ FETCH TAXONOMY
          │       GET /api/service-categories/categories
          │       Returns: 22 labels (6 categories + 16 services)
          │
          ├─[2]─ EXTRACT & CHUNK
          │       Docling (AI layout-aware) or OPEA fallback
          │       Format-aware chunk size (500–1500 tokens, 50 overlap)
          │       Filter empty chunks
          │
          ├─[3]─ GUARDRAIL (if GUARDRAIL_ENABLED=true)
          │       POST /guardrail:9090/v1/guardrails per chunk
          │       Any failure → abort
          │
          ├─[4]─ SEMANTIC LABELING
          │       Strategy: LLM / Embedding / BM25
          │       Each chunk → ["Pest & Disease Management", ...]
          │
          ├─[5]─ GRAPH EXTRACTION & INSERTION
          │       LLMGraphTransformer extracts entities & relationships
          │       Batch size 10, up to 5 concurrent batches
          │       Insert into ArangoDB:
          │         SOURCE vertices (chunks + embeddings + labels)
          │         ENTITY vertices (named entities + embeddings)
          │         HAS_SOURCE edges (entity → chunk)
          │         LINKS_TO edges (entity → entity + relationship text)
          │
          └─[6]─ STATUS UPDATE
                  Status: Ingested (success) or Ingestion Error (failure)
                  PATCH /api/files/{file_id}/status


══════════════════════════════════════════════════════
QUERY FLOW
══════════════════════════════════════════════════════

  User sends chat message
          │
          ▼
  Backend (port 3000)
  Validates JWT, adds user context, forwards to ChatQnA
          │
          ▼
  ChatQnA Orchestrator (port 8888)
          │
          ├─[1]─ LANGUAGE DETECTION
          │       langdetect.detect(query)
          │       If not English → translate via Gemma 3-4B (port 9031)
          │
          ├─[2]─ EMBED QUERY
          │       POST /v1/embeddings (Embedding Wrapper port 6000)
          │       → TEI port 7000 (BAAI/bge-base-en-v1.5)
          │       → 768-dimensional vector
          │
          ├─[3]─ RETRIEVE
          │       POST /v1/retrieval (Retriever port 7025)
          │       a. Build AQL filter from context labels (OR/AND)
          │       b. Cosine similarity search (top-5, threshold 0.5)
          │       c. [Optional] Graph traversal (depth 3, top-3/seed)
          │       d. [Optional] Per-chunk summarization
          │       Returns: 5 documents with text + metadata
          │
          ├─[4]─ RERANK
          │       POST /v1/reranking (Reranker Wrapper port 6100)
          │       Cross-encoder scores each (query, doc) pair jointly
          │       Strategy: slice → return top-5 by rank
          │       Returns: top-N reranked documents
          │
          ├─[5]─ ASSEMBLE PROMPT
          │       System prompt
          │       + User context (age, name, etc. — PII stripped)
          │       + Chat history (truncated to fit token budget)
          │       + Reranked chunks
          │       Token budget: max 4096 − 1024 answer tokens − 200 margin
          │
          ├─[6]─ GENERATE
          │       POST /v1/chat/completions (vLLM port 8000)
          │       ibm-granite/granite-3.3-2b-instruct
          │       temperature=0.01, max_tokens=1024
          │
          └─[7]─ TRANSLATE RESPONSE (if needed)
                  Translate answer back to user's original language
                  Returns response + source_documents metadata
```

---

## 13. Bugs Fixed & Changes Made

### Summary Table

| Date | Component | Problem | Fix | File Changed |
|------|-----------|---------|-----|--------------|
| 2026-04-21 | Dataprep Dockerfile | docling installs torch 2.11 (CUDA 13.0), host only supports CUDA 12.2 → CPU only | Added Step F2: force-reinstall torch 2.5.1+cu121 | `Dockerfile-dataprep_genie-ai` |
| 2026-04-21 | Dataprep Dockerfile | torch 2.5.1 pulls numpy 2.x → easyocr/scipy incompatibility → container crash | Added `numpy<2 scipy` reinstall at end of Step F2 | `Dockerfile-dataprep_genie-ai` |
| 2026-04-21 | Taxonomy / Frontend | After hierarchy import: category names blank, selecting one label selects all | AQL: set `catCode=_key`, insert EN records into `serviceCategoryTranslations` and `serviceTranslations` | ArangoDB direct |
| 2026-04-24 | Taxonomy / Frontend | Same blank label bug reappeared after truncating taxonomy collections to replace schema | Reapplied same three AQL fixes via `truncate-taxonomy.py` + manual re-run | ArangoDB direct |
| 2026-04-21 | Reranker | `RERANKING_THRESHOLD=0.90` eliminated all docs (cross-encoder scores ~0.27) | Changed `RERANKING_THRESHOLD=0.01` | `.env` |
| 2026-04-21 | Reranker | Threshold 0.01 still eliminated all docs (scores ~0.003 due to graph traversal text corrupting reranker input) | Changed `RERANKING_STRATEGY=slice` | `.env` |

### Detailed Fix: Dockerfile Step F2

```dockerfile
# ADDED AFTER Step F (which installs docling)

# Step F2: Pin PyTorch to a cu121 build compatible with the host NVIDIA driver (max CUDA 12.2).
# docling pulls torch 2.11+ (CUDA 13.0) which cannot initialise on this host — force-reinstall
# the latest torch available on the cu121 index so Docling and EasyOCR still work on GPU.
RUN pip install --no-cache-dir --force-reinstall \
    "torch==2.5.1+cu121" \
    "torchvision==0.20.1+cu121" \
    "torchaudio==2.5.1+cu121" \
    --index-url https://download.pytorch.org/whl/cu121 && \
    pip install --no-cache-dir "numpy<2" "scipy"
```

### Detailed Fix: Reranking Strategy

```bash
# Before:
RERANKING_STRATEGY=threshold
RERANKING_THRESHOLD=0.90

# After fix 1 (still broken — cross-encoder logit scores are not probabilities):
RERANKING_STRATEGY=threshold
RERANKING_THRESHOLD=0.01

# After fix 2 (working):
RERANKING_STRATEGY=slice    ← always returns top-N by rank, no score gate
RERANKER_TOP_N=5
```

---

## 14. Tuning Guide

### If the chatbot says "I don't have information about X" but the document IS ingested:

1. **Check retrieval** — are chunks being found?
   ```bash
   docker logs genie-ai-retriever --tail=50
   # Look for: "Found N documents"
   ```

2. **Check labels** — were the right labels assigned?
   ```aql
   -- In ArangoDB web UI
   FOR doc IN GRAPH_TEST_SOURCE
     FILTER "Pest & Disease Management" IN doc.chunk_labels
     RETURN COUNT(doc)
   ```
   If 0: the labeling failed. Check dataprep logs during ingestion.

3. **Check reranker output**
   ```bash
   docker logs genie-ai-reranker --tail=30
   # Look for: "Total number of documents in reranker output: 0"
   ```
   If 0 with `slice` strategy: shouldn't happen unless retrieval returned 0.

4. **Check the label filter strategy** — is `ARANGO_FILTER_STRATEGY=OR` or `AND`?
   `AND` is very strict. If the user selects "Weather Forecasts" + "Pest & Disease Management", only chunks with BOTH labels pass. Most chunks only get 1–3 labels.

### Improving label quality:

- Use `LABELING_STRATEGY=llm` (default) for best accuracy
- Add the exact label name as a keyword in your document heading structure
- Review ingestion logs for `"LLM suggested NEW label"` warnings — these indicate gaps in the taxonomy

### Improving retrieval recall:

- Lower `RETRIEVER_ARANGO_SCORE_THRESHOLD` (currently 0.5) — try 0.3 for shorter/technical documents
- Increase `RETRIEVER_ARANGO_K` from 5 to 10
- Enable traversal: `RETRIEVER_ARANGO_TRAVERSAL_ENABLED=true` — enriches each result with related graph context

### Enabling graph traversal (advanced):

```env
RETRIEVER_ARANGO_TRAVERSAL_ENABLED=true
RETRIEVER_ARANGO_TRAVERSAL_MAX_DEPTH=2    # keep low (2–3) for performance
RETRIEVER_ARANGO_TRAVERSAL_MAX_RETURNED=3
RETRIEVER_ARANGO_TRAVERSAL_SCORE_THRESHOLD=0.5
```

Note: When traversal is enabled, the RELATED INFORMATION appended to chunks will lower cross-encoder scores. Keep `RERANKING_STRATEGY=slice` (not `threshold`) in this case.

### Reranking strategies — when to use which:

| Strategy | Use when |
|----------|---------|
| `slice` | Default, especially if graph traversal is on. Always returns top-N, never empty. |
| `threshold` | Only if traversal is OFF and chunks are clean natural language. Set threshold to 0.0–0.1 range (cross-encoder scores are raw logits, not probabilities). |
| `knee_threshold` | When score distribution has a clear "elbow" — e.g., 2 highly relevant docs and 3 irrelevant ones with a big gap in between. |

### Rebuilding the dataprep image after Dockerfile changes:

```bash
cd /root/mewa_v2
docker compose build dataprep
docker compose up -d dataprep
```

### Restarting individual services after .env changes:

```bash
docker compose up -d reranker     # after RERANKING_* changes
docker compose up -d retriever    # after RETRIEVER_ARANGO_* changes
docker compose up -d chatqna      # after CHATQNA_* or LLM changes
docker compose up -d dataprep     # after LABELING_* or CONTENT_EXTRACTION_* changes
```





````markdown
# How Chunking Works MORE EXPLANATION

Yes, it's LangChain's `RecursiveCharacterTextSplitter` — but with some important nuances.

## The Splitter

```python
RecursiveCharacterTextSplitter(
    chunk_size=doc_path.chunk_size,      # in characters, not words
    chunk_overlap=doc_path.chunk_overlap,
    add_start_index=True,
    separators=get_separators(),
)
````

Character-based, not word-based. `chunk_size=500` means **500 characters** (roughly 80–120 words depending on content).

## Separator Hierarchy

The **"Recursive"** part is key — it tries each separator in order, splitting only when the chunk would exceed the limit:

1. `\n\n` — paragraph break (preferred, keeps paragraphs whole)
2. `\n` — line break
3. ` ` — space (word boundary)
4. `.` — sentence end
5. `,` — clause boundary
6. Zero-width/fullwidth Unicode chars (CJK support)
7. `""` — last resort: split mid-character

So it tries to split at natural boundaries (**paragraphs → sentences → words**), not blindly at a fixed position.

## Your Configured Sizes

| File type | Chunk size (chars)                    | Overlap (chars) |
| --------- | ------------------------------------- | --------------- |
| `.md`     | 500                                   | 50              |
| `.txt`    | 500                                   | 50              |
| `.pdf`    | 500                                   | 50              |
| `.docx`   | 1000                                  | 50              |
| `.xlsx`   | 1500                                  | 50              |
| `.html`   | uses `HTMLHeaderTextSplitter` instead | —               |

The **50-character overlap** means the end of one chunk is repeated at the start of the next, so a sentence cut at a boundary doesn't lose context entirely.

## HTML is Different

HTML doesn't use `RecursiveCharacterTextSplitter` at all — it uses `HTMLHeaderTextSplitter`, which splits on `<h1>` and `<h2>` tags.

Each section under a heading becomes its own chunk, regardless of length. This is more semantically meaningful for structured HTML docs.

## The Filter Step

After splitting, every chunk passes through `is_valid_content()`:

* Must be **>70% alphanumeric characters**
* Must have **<50% problematic lines** (very short, repetitive, etc.)

Chunks that fail are silently dropped — this is why JSON fails (too many `{`, `"`, `:` characters pulling the alphanumeric ratio below 70%).

## The `potato_calendar_dhaka.md` Example

That file produced **21 chunks** from a **500-char / 50-char-overlap** split on a Markdown file.

Chunk 1 had `"105 days"` in it — that's content that lived within 500 characters of each other in the original document, kept together because the splitter found a `\n\n` boundary nearby.

```
```
