"""
AltoLex — rag_v4.py
Tenant-scoped RAG. Uses ScopedDB — firm_id enforced structurally.
Removed: SUPABASE_ANON_KEY (dead since v4 custom auth).
"""

import os
from dotenv import load_dotenv
load_dotenv()

import anthropic
import voyageai
from db import ScopedDB

VOYAGE_MODEL = "voyage-law-2"
TOP_K        = 5

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


def retrieve(query: str, firm_id: str,
             case_id: str = None, doc_type: str = None) -> list[dict]:
    """
    Embed query and find top-K chunks.
    All retrieval is scoped to firm_id via ScopedDB.rpc() —
    p_firm_id cannot be overridden by the caller.
    """
    vector = vo.embed([query], model=VOYAGE_MODEL, input_type="query").embeddings[0]

    params = {"query_embedding": vector, "match_count": TOP_K}
    if case_id:   params["p_case_id"]        = case_id
    if doc_type:  params["filter_doc_type"]  = doc_type

    # ScopedDB.rpc() always injects and verifies p_firm_id
    result = ScopedDB(firm_id).rpc("match_documents", params).execute()
    return result.data or []


def build_context(chunks: list[dict]) -> str:
    if not chunks:
        return "No relevant documents found in the knowledge base for this case."
    lines = ["--- RETRIEVED CONTEXT FROM FIRM DOCUMENT LIBRARY ---\n"]
    for i, chunk in enumerate(chunks, 1):
        src   = chunk.get("metadata", {}).get("source", "unknown")
        dtype = chunk.get("metadata", {}).get("doc_type", "")
        lines.append(f"[{i}] {src} ({dtype})\n{chunk.get('content','')}\n")
    lines.append("--- END CONTEXT ---")
    return "\n".join(lines)


def ask(
    question: str,
    firm_id: str,
    user_id: str,
    conversation_history: list = None,
    case_id: str = None,
    doc_type: str = None,
    ip_address: str = None,
) -> str:
    # 1. Retrieve — scoped to firm via ScopedDB
    chunks  = retrieve(question, firm_id, case_id, doc_type)
    context = build_context(chunks)

    # 2. Build augmented prompt with prompt caching
    system   = f"{SYSTEM_PROMPT}\n\n{context}"
    messages = (conversation_history or []) + [{"role": "user", "content": question}]

    response = claude.messages.create(
        model      = "claude-sonnet-4-20250514",
        max_tokens = 1024,
        system     = [{"type": "text", "text": system,
                       "cache_control": {"type": "ephemeral"}}],
        messages   = messages,
    )
    answer = response.content[0].text

    # 3. Audit — scoped to firm
    db = ScopedDB(firm_id)
    try:
        db.table("conversations").insert({
            "user_id":    user_id,
            "case_id":    case_id,
            "question":   question,
            "answer":     answer,
            "doc_sources": [c.get("metadata", {}).get("source") for c in chunks],
        }).execute()

        db.table("audit_log").insert({
            "user_id":       user_id,
            "action":        "query",
            "resource_type": "document",
            "ip_address":    ip_address,
            "metadata":      {"case_id": case_id, "chunks_retrieved": len(chunks)},
        }).execute()
    except Exception as e:
        print(f"[warn] Audit log write failed: {e}")

    return answer
