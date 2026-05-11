"""
AltoLex — api_v2.py
FastAPI server with JWT authentication.
Every endpoint extracts firm_id and attorney_id from the Supabase JWT —
no tenant context is accepted from the client body.

Run: uvicorn api_v2:app --reload --port 8000
"""

import os, tempfile
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

import jwt as pyjwt   # pip install PyJWT
from supabase import create_client

from rag_v2 import ask
from ingest_v2 import ingest_file

SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_ANON   = os.environ["SUPABASE_ANON_KEY"]
SUPABASE_SVC    = os.environ["SUPABASE_SERVICE_KEY"]
JWT_SECRET      = os.environ["SUPABASE_JWT_SECRET"]   # from Supabase project settings

app = FastAPI(title="AltoLex API v2")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.environ.get("FRONTEND_URL", "*")],
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer()


# ── Auth dependency ───────────────────────────────────────────────────────────

class AttorneyContext:
    def __init__(self, attorney_id: str, firm_id: str, token: str, ip: str):
        self.attorney_id = attorney_id
        self.firm_id     = firm_id
        self.token       = token
        self.ip          = ip


def get_attorney(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> AttorneyContext:
    """
    Validates the Supabase JWT and resolves the attorney's firm_id.
    firm_id is NEVER taken from the request body — always from the verified token.
    """
    token = credentials.credentials
    try:
        payload = pyjwt.decode(
            token,
            JWT_SECRET,
            algorithms=["HS256"],
            options={"verify_aud": False}
        )
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except pyjwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    attorney_id = payload.get("sub")
    if not attorney_id:
        raise HTTPException(status_code=401, detail="No subject in token")

    # Resolve firm_id from DB (not from token claims — they could be stale)
    svc = create_client(SUPABASE_URL, SUPABASE_SVC)
    row = svc.table("attorneys").select("firm_id").eq("id", attorney_id).single().execute()
    if not row.data:
        raise HTTPException(status_code=403, detail="Attorney not found")

    firm_id = row.data["firm_id"]
    ip = request.client.host if request.client else None

    return AttorneyContext(attorney_id=attorney_id, firm_id=firm_id, token=token, ip=ip)


# ── Request/response models ───────────────────────────────────────────────────

class QueryRequest(BaseModel):
    question: str
    history:  list[dict] = []
    case_id:  str        = None
    doc_type: str        = None

class QueryResponse(BaseModel):
    answer:      str
    chunks_used: int


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/ask", response_model=QueryResponse)
async def query(req: QueryRequest, atty: AttorneyContext = Depends(get_attorney)):
    try:
        from rag_v2 import retrieve
        chunks = retrieve(req.question, atty.firm_id, atty.token,
                          req.case_id, req.doc_type)
        answer = ask(
            question             = req.question,
            firm_id              = atty.firm_id,
            attorney_id          = atty.attorney_id,
            attorney_token       = atty.token,
            conversation_history = req.history,
            case_id              = req.case_id,
            doc_type             = req.doc_type,
            ip_address           = atty.ip,
        )
        return QueryResponse(answer=answer, chunks_used=len(chunks))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ingest")
async def ingest(
    file:    UploadFile = File(...),
    case_id: str        = None,
    atty:    AttorneyContext = Depends(get_attorney)
):
    """Upload a document to the firm's knowledge base. Optionally scoped to a case."""
    import os as _os
    suffix = _os.path.splitext(file.filename)[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    try:
        ingest_file(tmp_path, firm_id=atty.firm_id, case_id=case_id)

        # Audit log the ingest
        svc = create_client(SUPABASE_URL, SUPABASE_SVC)
        svc.table("audit_log").insert({
            "firm_id":       atty.firm_id,
            "attorney_id":   atty.attorney_id,
            "action":        "ingest",
            "resource_type": "document",
            "resource_id":   file.filename,
            "ip_address":    atty.ip,
            "metadata":      {"case_id": case_id},
        }).execute()

        return {"status": "ok", "filename": file.filename,
                "firm_id": atty.firm_id, "case_id": case_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _os.unlink(tmp_path)


@app.get("/health")
def health():
    return {"status": "ok", "version": "2.0"}
