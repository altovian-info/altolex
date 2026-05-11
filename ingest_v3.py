"""
AltoLex — ingest_v2.py
Tenant-aware document ingestion.
Every chunk is tagged with firm_id and optionally case_id.

Usage:
    python ingest_v2.py --dir ./docs --firm-id <uuid>
    python ingest_v2.py --file nda.pdf --firm-id <uuid> --case-id <uuid>
"""

import os, sys, argparse, hashlib
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

from supabase import create_client
import voyageai
import PyPDF2

SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_KEY   = os.environ["SUPABASE_SERVICE_KEY"]   # ← service key for ingest (bypasses RLS for batch writes)
VOYAGE_MODEL   = "voyage-law-2"
CHUNK_SIZE     = 500   # tokens (approx chars / 4)
CHUNK_OVERLAP  = 80
EMBED_BATCH    = 64    # voyage-law-2 max batch is 1000; 64 is safe and efficient

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
vo       = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])


def extract_text(path: str) -> str:
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        text = []
        with open(path, "rb") as f:
            for page in PyPDF2.PdfReader(f).pages:
                text.append(page.extract_text() or "")
        return "\n".join(text)
    elif p.suffix.lower() in [".txt", ".md"]:
        return p.read_text(encoding="utf-8")
    return ""


def chunk_text(text: str) -> list[str]:
    words = text.split()
    step  = CHUNK_SIZE - CHUNK_OVERLAP
    return [
        " ".join(words[i: i + CHUNK_SIZE]).strip()
        for i in range(0, len(words), step)
        if words[i: i + CHUNK_SIZE]
    ]


def doc_type_from_filename(name: str) -> str:
    n = name.lower()
    if any(k in n for k in ["nda","non-disclosure"]):  return "nda"
    if any(k in n for k in ["employment","contract_of_service"]): return "employment"
    if any(k in n for k in ["lease","tenancy","rental"]): return "property"
    if any(k in n for k in ["case","judgment","ruling"]): return "case_law"
    if any(k in n for k in ["sop","procedure","policy"]): return "sop"
    return "general"


def ingest_file(path: str, firm_id: str, case_id: str = None):
    p = Path(path)
    print(f"\n→ {p.name}  firm={firm_id[:8]}…  case={case_id[:8]+'…' if case_id else 'firm-wide'}")

    text = extract_text(str(p))
    if not text.strip():
        print("  No text extracted — skipping.")
        return

    fhash = hashlib.md5(p.read_bytes()).hexdigest()

    # Skip if already ingested for this firm+case
    q = supabase.table("documents").select("id") \
        .eq("file_hash", fhash).eq("firm_id", firm_id)
    if case_id:
        q = q.eq("case_id", case_id)
    if q.execute().data:
        print("  Already ingested — skipping.")
        return

    chunks  = chunk_text(text)
    print(f"  {len(text)} chars → {len(chunks)} chunks")
    # Embed using voyage-law-2 — input_type="document" is critical for retrieval accuracy.
    # Voyage prepends a document-retrieval prompt which significantly improves recall
    # when matched against queries embedded with input_type="query".
    all_vectors = []
    for i in range(0, len(chunks), EMBED_BATCH):
        batch  = chunks[i: i + EMBED_BATCH]
        result = vo.embed(batch, model=VOYAGE_MODEL, input_type="document")
        all_vectors.extend(result.embeddings)
    print(f"  Embedded {len(all_vectors)} chunks via {VOYAGE_MODEL}")
    vectors = all_vectors

    rows = [
        {
            "firm_id":   firm_id,
            "case_id":   case_id,          # None = firm-wide knowledge base
            "content":   chunk,
            "embedding": vector,
            "metadata": {
                "source":    p.name,
                "doc_type":  doc_type_from_filename(p.name),
                "chunk_idx": idx,
            },
            "file_hash": fhash,
        }
        for idx, (chunk, vector) in enumerate(zip(chunks, vectors))
    ]

    batch_size = 50
    for i in range(0, len(rows), batch_size):
        supabase.table("documents").insert(rows[i: i + batch_size]).execute()
        print(f"  Batch {i // batch_size + 1}/{-(-len(rows) // batch_size)} stored")

    print(f"  ✓ {len(chunks)} chunks ingested for {p.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir",     help="Directory of documents")
    parser.add_argument("--file",    help="Single file")
    parser.add_argument("--firm-id", required=True, help="Firm UUID")
    parser.add_argument("--case-id", default=None,  help="Case UUID (optional — omit for firm-wide)")
    args = parser.parse_args()

    if args.dir:
        for ext in ["*.pdf","*.txt","*.md"]:
            for f in Path(args.dir).glob(ext):
                ingest_file(str(f), args.firm_id, args.case_id)
    elif args.file:
        ingest_file(args.file, args.firm_id, args.case_id)
    else:
        print("Usage: python ingest_v2.py --dir ./docs --firm-id <uuid>")
        sys.exit(1)

    print("\n✓ Ingestion complete.")
