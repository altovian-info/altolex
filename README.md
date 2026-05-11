# AltoLex v3 — voyage-law-2 embeddings

## What changed from v2

| | v2 | v3 |
|---|---|---|
| Embedding model | all-MiniLM-L6-v2 (local, free) | voyage-law-2 (API, legal-specialised) |
| Vector dimensions | 384 | 1024 |
| Embedding library | sentence-transformers | voyageai |
| New env variable | — | VOYAGE_API_KEY |
| input_type | not used | "document" at ingest, "query" at retrieval |
| Legal retrieval accuracy | baseline | +6% over OpenAI on 8 legal datasets |

Files changed: ingest_v3.py, rag_v3.py, requirements.txt, supabase_setup_v3.sql
Files unchanged: api_v3.py, app_v3.py

## Setup

### 1. Get a Voyage API key
Sign up at https://dash.voyageai.com → API Keys → Create key.
Add to .env: VOYAGE_API_KEY=pa-...

### 2. Database
Fresh install: run supabase_setup_v3.sql (uses VECTOR(1024))
Upgrading from v2: run the MIGRATION section at the bottom of the SQL file

### 3. Install dependencies
pip install -r requirements.txt

### 4. Re-ingest all documents (required when upgrading from v2)
Old 384-dim vectors are incompatible with 1024-dim. Must re-ingest everything.
python ingest_v3.py --dir ./docs --firm-id <uuid>

### 5. Run
streamlit run app_v3.py

## Voyage API cost
voyage-law-2 is billed per token:
- ~$0.00012 per 1000 tokens at ingest
- ~$0.00012 per 1000 tokens per query embedding
- A 10-page contract (~3000 tokens) costs ~$0.00036 to ingest
- Each user query costs ~$0.000012 (100 tokens) to embed
- Effectively negligible vs Claude API costs
