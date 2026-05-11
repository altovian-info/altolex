"""
AltoLex — rag_v2.py
Tenant-scoped RAG query engine.
All retrievals are hard-scoped to firm_id + optional case_id.
Every query is written to the audit log.
"""

import os
from dotenv import load_dotenv
load_dotenv()

import anthropic
import voyageai
from supabase import create_client

SUPABASE_URL  = os.environ["SUPABASE_URL"]
VOYAGE_MODEL  = "voyage-law-2"
TOP_K         = 5

vo     = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SYSTEM_PROMPT = """You are AltoLex, a professional legal information assistant for a law firm.

You have access to the firm's document library — retrieved context is provided below.
Prefer information from the context over general knowledge. Cite the source filename when referencing it.

RULES:
1. Never give definitive legal advice — provide legal information only
2. Always recommend attorney review for specific situations  
3. If context does not contain relevant information, say so clearly — do not fabricate
4. Flag urgent deadlines or limitation periods prominently
5. End substantive responses with: "This is legal information only. Please consult a qualified attorney."
"""


def get_client_for_token(token: str):
    """Return a Supabase client authenticated as the calling attorney (JWT).
    RLS policies will automatically scope all queries to their firm."""
    return create_client(
        SUPABASE_URL,
        os.environ["SUPABASE_ANON_KEY"],
        options={"headers": {"Authorization": f"Bearer {token}"}}
    )


def retrieve(query: str, firm_id: str, attorney_token: str,
             case_id: str = None, doc_type: str = None) -> list[dict]:
    """
    Embed query and find top-K matching chunks.
    Scoped to firm_id + optional case_id via the match_documents function.
    Uses the attorney's JWT so RLS is enforced as a second layer.
    """
    client = get_client_for_token(attorney_token)

    # input_type="query" pairs correctly with input_type="document" used at ingest time.
    # This asymmetric prompting is what gives voyage-law-2 its retrieval accuracy advantage.
    vector = vo.embed([query], model=VOYAGE_MODEL, input_type="query").embeddings[0]

    params = {
        "query_embedding": vector,
        "p_firm_id":       firm_id,
        "match_count":     TOP_K,
    }
    if case_id:
        params["p_case_id"] = case_id
    if doc_type:
        params["filter_doc_type"] = doc_type

    result = client.rpc("match_documents", params).execute()
    return result.data or []


def build_context(chunks: list[dict]) -> str:
    if not chunks:
        return "No relevant documents found in the knowledge base for this case."
    lines = ["--- RETRIEVED CONTEXT FROM FIRM DOCUMENT LIBRARY ---\n"]
    for i, chunk in enumerate(chunks, 1):
        src = chunk.get("metadata", {}).get("source", "unknown")
        dtype = chunk.get("metadata", {}).get("doc_type", "")
        lines.append(f"[{i}] {src} ({dtype})\n{chunk.get('content','')}\n")
    lines.append("--- END CONTEXT ---")
    return "\n".join(lines)


def log_query(service_client, firm_id: str, attorney_id: str,
              question: str, answer: str, sources: list,
              case_id: str = None, ip: str = None):
    """Write to audit_log and conversations tables using service role key."""
    try:
        service_client.table("conversations").insert({
            "firm_id":     firm_id,
            "case_id":     case_id,
            "attorney_id": attorney_id,
            "question":    question,
            "answer":      answer,
            "doc_sources": [c.get("metadata", {}).get("source") for c in sources],
        }).execute()

        service_client.table("audit_log").insert({
            "firm_id":       firm_id,
            "attorney_id":   attorney_id,
            "action":        "query",
            "resource_type": "document",
            "ip_address":    ip,
            "metadata":      {"case_id": case_id, "chunks_retrieved": len(sources)},
        }).execute()
    except Exception as e:
        print(f"[warn] Audit log write failed: {e}")


def ask(
    question: str,
    firm_id: str,
    attorney_id: str,
    attorney_token: str,
    conversation_history: list = None,
    case_id: str = None,
    doc_type: str = None,
    ip_address: str = None,
) -> str:
    # 1. Retrieve scoped chunks
    chunks  = retrieve(question, firm_id, attorney_token, case_id, doc_type)
    context = build_context(chunks)

    # 2. Build augmented system prompt
    system = f"{SYSTEM_PROMPT}\n\n{context}"

    # 3. Call Claude with prompt caching on system + context
    messages = (conversation_history or []) + [{"role": "user", "content": question}]
    response = claude.messages.create(
        model      = "claude-sonnet-4-20250514",
        max_tokens = 1024,
        system     = [{"type": "text", "text": system,
                       "cache_control": {"type": "ephemeral"}}],
        messages   = messages,
    )
    answer = response.content[0].text

    # 4. Audit log (uses service role key — bypasses RLS for write)
    service_client = create_client(SUPABASE_URL, os.environ["SUPABASE_SERVICE_KEY"])
    log_query(service_client, firm_id, attorney_id, question, answer,
              chunks, case_id, ip_address)

    return answer
