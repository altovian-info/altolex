"""
AltoLex — app_v4.py
Full implementation:
  - Client + case management (saved to DB)
  - Document upload embedded via voyage-law-2, stored in documents table
  - Documents tied to a specific case OR firm-wide (ordinances)
  - AI Search with RAG over stored documents
  - Admin panel for user management
"""

import streamlit as st
import anthropic
import os, io, base64, hashlib
from pathlib import Path
from auth import (login, logout, validate_session,
                  create_user, update_user, deactivate_user,
                  reactivate_user, list_users, log_action)
from db import ScopedDB, raw_client

try:
    import fitz;        PYMUPDF_OK = True
except ImportError:     PYMUPDF_OK = False
try:
    from docx import Document as DocxDocument; DOCX_OK = True
except ImportError:     DOCX_OK = False
try:
    import PyPDF2;      PYPDF2_OK = True
except ImportError:     PYPDF2_OK = False
try:
    import voyageai;    VOYAGE_OK = True
except ImportError:     VOYAGE_OK = False


st.set_page_config(page_title="AltoLex", page_icon="=", layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;600&family=DM+Sans:wght@300;400;500&display=swap');
html, body, [class*="css"] { font-family: 'DM Sans', sans-serif; }
h1, h2, h3 { font-family: 'Playfair Display', serif !important; }
.stApp { background: #f7f4ef; }
.badge { display:inline-block; padding:3px 10px; border-radius:20px; font-size:11px;
         font-weight:600; letter-spacing:0.06em; text-transform:uppercase; margin-right:4px; }
.badge-firm   { background:rgba(184,147,90,0.12); color:#b8935a; border:1px solid rgba(184,147,90,0.3); }
.badge-case   { background:rgba(42,90,180,0.1);   color:#2a5ab4; border:1px solid rgba(42,90,180,0.2); }
.badge-common { background:rgba(45,106,79,0.1);   color:#2d6a4f; border:1px solid rgba(45,106,79,0.2); }
.badge-admin  { background:rgba(139,38,53,0.08);  color:#8b2635; border:1px solid rgba(139,38,53,0.2); }
.disclaimer   { background:rgba(184,147,90,0.08); border:1px solid rgba(184,147,90,0.2);
                border-radius:8px; padding:12px 16px; font-size:12px; color:#6b6b80; line-height:1.6; }
</style>
""", unsafe_allow_html=True)

SYSTEM_PROMPT = """You are AltoLex, a professional legal information assistant for a law firm in Sri Lanka.
Retrieved context from the firm document library is below - prefer this over general knowledge.
Cite source document names when referencing retrieved content.
RULES:
1. Never give definitive legal advice - provide legal information only
2. Always recommend attorney review for specific situations
3. If context lacks relevant information, say so clearly - never fabricate
4. Flag urgent deadlines or limitation periods prominently
5. End substantive responses with: "This is legal information only. Please consult a qualified attorney."
"""

ROLES        = ["admin", "partner", "associate", "paralegal", "readonly"]
AREAS_OF_LAW = ["Contract dispute", "Property / conveyancing", "Employment", "Family law",
                "Commercial / corporate", "Intellectual property", "Criminal defence", "Other"]
CHUNK_SIZE   = 500
CHUNK_OVERLAP = 80


# ---- API clients -------------------------------------------------------
@st.cache_resource
def get_anthropic():
    key = os.environ.get("ANTHROPIC_API_KEY") or st.secrets.get("ANTHROPIC_API_KEY", "")
    return anthropic.Anthropic(api_key=key)

@st.cache_resource
def get_voyage():
    key = os.environ.get("VOYAGE_API_KEY") or st.secrets.get("VOYAGE_API_KEY", "")
    return voyageai.Client(api_key=key)


# ---- Session helpers ---------------------------------------------------
def ctx() -> dict:
    return st.session_state.get("ctx", {})

def sdb() -> ScopedDB:
    return ScopedDB(ctx()["firm_id"])

def is_admin()    -> bool: return ctx().get("role") == "admin"
def is_readonly() -> bool: return ctx().get("role") == "readonly"


# ---- Client / case DB helpers -----------------------------------------
def get_clients() -> list:
    r = sdb().table("clients").select("id,full_name,email,phone,created_at").order("full_name").execute()
    return r.data or []

def get_cases(client_id: str) -> list:
    r = sdb().table("cases").select("id,title,area_of_law,status,created_at") \
             .eq("client_id", client_id).order("created_at", desc=True).execute()
    return r.data or []

def create_client_record(full_name: str, email: str, phone: str) -> dict:
    r = sdb().table("clients").insert({"full_name": full_name, "email": email or "", "phone": phone or ""}).execute()
    return r.data[0]

def create_case_record(client_id: str, title: str, area: str) -> dict:
    r = sdb().table("cases").insert({"client_id": client_id, "title": title, "area_of_law": area, "status": "open"}).execute()
    return r.data[0]

def get_stored_docs(case_id: str = None, firm_wide: bool = False) -> list:
    if firm_wide:
        rc = raw_client()
        rows = rc.table("documents").select("metadata,file_hash,created_at") \
                 .eq("firm_id", ctx()["firm_id"]).is_("case_id", "null").execute().data or []
    else:
        rows = sdb().table("documents").select("metadata,file_hash,created_at") \
                    .eq("case_id", case_id).execute().data or []
    seen = set(); result = []
    for row in rows:
        h = row.get("file_hash", "")
        if h not in seen:
            seen.add(h)
            result.append({"name": row["metadata"].get("source", "unknown"),
                           "file_hash": h, "created_at": row.get("created_at", "")})
    return result


# ---- Document extraction ----------------------------------------------
def extract_text(file_bytes: bytes, filename: str) -> tuple:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf" and PYPDF2_OK:
        try:
            pages = [p.extract_text() or "" for p in PyPDF2.PdfReader(io.BytesIO(file_bytes)).pages]
            t = "\n\n".join(pages).strip()
            if len(t) > 100:
                return t, "text"
        except Exception:
            pass
        return "", "vision_needed"
    elif ext in [".docx", ".doc"] and DOCX_OK:
        try:
            doc = DocxDocument(io.BytesIO(file_bytes))
            parts = []
            for p in doc.paragraphs:
                t = p.text.strip()
                if t:
                    parts.append("## " + t if p.style.name.startswith("Heading") else t)
            for table in doc.tables:
                parts.append("[TABLE]")
                for row in table.rows:
                    parts.append(" | ".join(c.text.strip() for c in row.cells))
                parts.append("[END TABLE]")
            return "\n".join(parts), "docx"
        except Exception:
            pass
    return "", "empty"

def chunk_text(text: str) -> list:
    words = text.split()
    step  = CHUNK_SIZE - CHUNK_OVERLAP
    return [" ".join(words[i:i+CHUNK_SIZE]).strip()
            for i in range(0, len(words), step) if words[i:i+CHUNK_SIZE]]

def doc_type_tag(name: str) -> str:
    n = name.lower()
    if any(k in n for k in ["nda","non-disclosure"]):          return "nda"
    if any(k in n for k in ["employment","service"]):          return "employment"
    if any(k in n for k in ["lease","tenancy","rental"]):      return "property"
    if any(k in n for k in ["judgment","ruling","case"]):      return "case_law"
    if any(k in n for k in ["ordinance","act","regulation",
                             "statute","gazette"]):             return "ordinance"
    return "general"

def embed_and_store(file_bytes: bytes, filename: str, firm_id: str, case_id: str = None) -> dict:
    """Extract -> chunk -> embed -> store. case_id=None = firm-wide."""
    fhash = hashlib.md5(file_bytes).hexdigest()
    existing = raw_client().table("documents").select("id") \
                .eq("firm_id", firm_id).eq("file_hash", fhash).limit(1).execute()
    if existing.data:
        return {"skipped": True, "chunks": 0, "method": ""}

    text, method = extract_text(file_bytes, filename)

    # OCR for scanned PDFs via Claude vision
    if method == "vision_needed" and PYMUPDF_OK:
        try:
            pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
            mat = fitz.Matrix(150/72, 150/72)
            page_texts = []
            for i, page in enumerate(pdf_doc):
                if i >= 15: break
                pix = page.get_pixmap(matrix=mat)
                img_b64 = base64.standard_b64encode(pix.tobytes("png")).decode()
                resp = get_anthropic().messages.create(
                    model="claude-sonnet-4-20250514", max_tokens=2000,
                    messages=[{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                        {"type": "text", "text": "Extract all text from this document page. Return only the text."}
                    ]}])
                page_texts.append(resp.content[0].text)
            text = "\n\n".join(page_texts)
            method = "vision_ocr"
        except Exception as e:
            return {"skipped": False, "chunks": 0, "method": "error", "error": str(e)}

    if not text.strip():
        return {"skipped": False, "chunks": 0, "method": "empty"}

    chunks  = chunk_text(text)
    vo      = get_voyage()
    vectors = []
    for i in range(0, len(chunks), 64):
        r = vo.embed(chunks[i:i+64], model="voyage-law-2", input_type="document")
        vectors.extend(r.embeddings)

    rows = [{"firm_id": firm_id, "case_id": case_id, "content": chunk, "embedding": vector,
             "metadata": {"source": filename, "doc_type": doc_type_tag(filename), "chunk_idx": idx},
             "file_hash": fhash}
            for idx, (chunk, vector) in enumerate(zip(chunks, vectors))]

    rc = raw_client()
    for i in range(0, len(rows), 50):
        rc.table("documents").insert(rows[i:i+50]).execute()

    return {"skipped": False, "chunks": len(chunks), "method": method}


# ---- RAG Q&A -----------------------------------------------------------
def rag_ask(question: str, case_id: str = None, history: list = None) -> tuple:
    c = ctx()
    vector = get_voyage().embed([question], model="voyage-law-2", input_type="query").embeddings[0]
    params = {"query_embedding": vector, "match_count": 5}
    if case_id:
        params["p_case_id"] = case_id

    chunks = ScopedDB(c["firm_id"]).rpc("match_documents", params).execute().data or []

    if chunks:
        lines = ["--- RETRIEVED CONTEXT ---\n"]
        for i, ch in enumerate(chunks, 1):
            src = ch.get("metadata", {}).get("source", "unknown")
            lines.append(f"[{i}] {src}\n{ch.get('content','')}\n")
        lines.append("--- END CONTEXT ---")
        context = "\n".join(lines)
    else:
        context = "No relevant documents found in the knowledge base for this query."

    msgs = (history or []) + [{"role": "user", "content": question}]
    resp = get_anthropic().messages.create(
        model="claude-sonnet-4-20250514", max_tokens=1500,
        system=[{"type": "text", "text": SYSTEM_PROMPT + "\n\n" + context,
                 "cache_control": {"type": "ephemeral"}}],
        messages=msgs)
    answer = resp.content[0].text

    try:
        sdb().table("conversations").insert({
            "user_id": c["user_id"], "case_id": case_id,
            "question": question, "answer": answer,
            "doc_sources": [ch.get("metadata", {}).get("source") for ch in chunks],
        }).execute()
    except Exception:
        pass

    return answer, len(chunks)

def call_claude(prompt: str, history: list = None) -> str:
    msgs = (history or []) + [{"role": "user", "content": prompt}]
    resp = get_anthropic().messages.create(
        model="claude-sonnet-4-20250514", max_tokens=1500,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=msgs)
    return resp.content[0].text

def pdf_page_images(file_bytes: bytes, max_pages: int = 3) -> list:
    if not PYMUPDF_OK: return []
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    mat = fitz.Matrix(120/72, 120/72)
    imgs = []
    for i, page in enumerate(doc):
        if i >= max_pages: break
        pix = page.get_pixmap(matrix=mat)
        imgs.append(base64.standard_b64encode(pix.tobytes("png")).decode())
    doc.close()
    return imgs


# ---- Login ------------------------------------------------------------
def show_login():
    st.markdown("""<div style="max-width:420px;margin:60px auto 24px;background:white;
        border:1px solid rgba(184,147,90,0.2);border-radius:12px;padding:32px">
        <div style="font-family:'Playfair Display',serif;font-size:28px;font-weight:600;color:#1a1a2e;margin-bottom:3px">AltoLex</div>
        <div style="font-size:10px;text-transform:uppercase;letter-spacing:0.1em;color:#6b6b80;margin-bottom:28px">by Altovian</div>
        </div>""", unsafe_allow_html=True)
    with st.form("login"):
        st.subheader("Sign in")
        email    = st.text_input("Email address")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in ->", use_container_width=True):
            result = login(email, password)
            if result:
                st.session_state["ctx"] = result; st.rerun()
            else:
                st.error("Invalid email or password.")
    st.markdown('<div class="disclaimer" style="max-width:420px;margin:0 auto">AltoLex provides legal information only - not legal advice.</div>', unsafe_allow_html=True)


# ---- Sidebar ----------------------------------------------------------
def show_sidebar():
    c = ctx()
    with st.sidebar:
        st.markdown('<div style="font-family:\'Playfair Display\',serif;font-size:22px;font-weight:600;color:#d4aa7a">AltoLex</div>', unsafe_allow_html=True)
        st.markdown('<div style="font-size:10px;text-transform:uppercase;letter-spacing:0.1em;color:rgba(232,228,220,0.4);margin-bottom:16px">by Altovian</div>', unsafe_allow_html=True)
        st.markdown(f"**{c.get('full_name','')}**")
        st.markdown(f"<small style='color:#6b6b80'>{c.get('email','')}</small>", unsafe_allow_html=True)
        bc = "badge-admin" if c.get("role") == "admin" else "badge-firm"
        st.markdown(f'<span class="badge {bc}">{c.get("role","").upper()}</span>', unsafe_allow_html=True)
        st.divider()

        modules = ["📋 Client Intake", "👥 Clients & Cases",
                   "📁 Document Library", "💬 AI Search", "⚖️ Common Knowledge"]
        if is_admin():
            modules.append("⚙ Admin")

        module = st.radio("Module", modules, label_visibility="collapsed")
        st.divider()
        st.markdown("**System**")
        st.markdown(f"{'OK' if PYPDF2_OK else 'X'} Text PDFs")
        st.markdown(f"{'OK' if PYMUPDF_OK else 'X'} Scanned PDFs (OCR)")
        st.markdown(f"{'OK' if DOCX_OK else 'X'} Word docs")
        st.markdown(f"{'OK' if VOYAGE_OK else 'X'} voyage-law-2")
        st.divider()
        if st.button("Sign out"):
            logout(c.get("token")); st.session_state.clear(); st.rerun()
        st.markdown('<div class="disclaimer">Legal information only. Attorney review required on all outputs.</div>', unsafe_allow_html=True)
    return module


# ---- Admin panel -------------------------------------------------------
def show_admin():
    c = ctx()
    st.title("Admin - User Management")
    tab_users, tab_add = st.tabs(["Users", "Add User"])

    with tab_users:
        users = list_users(c["firm_id"])
        st.markdown(f"**{len(users)} users**")
        st.divider()
        for u in users:
            with st.expander(f"{'Active' if u['is_active'] else 'Inactive'} | {u['full_name']} - {u['email']} ({u['role']})"):
                col1, col2, col3 = st.columns([2, 2, 1])
                with col1:
                    new_name = st.text_input("Full name", value=u["full_name"], key=f"n_{u['id']}")
                    new_role = st.selectbox("Role", ROLES, index=ROLES.index(u["role"]), key=f"r_{u['id']}")
                with col2:
                    new_pw = st.text_input("New password (blank = no change)", type="password", key=f"p_{u['id']}")
                    st.caption(f"Last login: {u.get('last_login', 'Never')}")
                with col3:
                    st.markdown("&nbsp;", unsafe_allow_html=True)
                    if st.button("Save", key=f"s_{u['id']}"):
                        upd = {"full_name": new_name, "role": new_role}
                        if new_pw.strip(): upd["password"] = new_pw.strip()
                        update_user(c["firm_id"], u["id"], upd)
                        st.success("Saved."); st.rerun()
                    if u["id"] != c["user_id"]:
                        if u["is_active"]:
                            if st.button("Deactivate", key=f"d_{u['id']}"):
                                deactivate_user(c["firm_id"], u["id"]); st.rerun()
                        else:
                            if st.button("Reactivate", key=f"d_{u['id']}"):
                                reactivate_user(c["firm_id"], u["id"]); st.rerun()

    with tab_add:
        st.subheader("Add a new user")
        with st.form("add_user"):
            col1, col2 = st.columns(2)
            with col1:
                nfn = st.text_input("Full name *")
                nem = st.text_input("Email *")
            with col2:
                nrl = st.selectbox("Role *", ROLES, index=2)
                npw = st.text_input("Temporary password *", type="password")
            cpw = st.text_input("Confirm password *", type="password")
            if st.form_submit_button("Create user ->", use_container_width=True):
                errs = []
                if not nfn.strip(): errs.append("Full name required.")
                if not nem.strip(): errs.append("Email required.")
                if not npw.strip(): errs.append("Password required.")
                if npw != cpw:      errs.append("Passwords do not match.")
                if len(npw) < 8:    errs.append("Password must be at least 8 characters.")
                if errs:
                    for e in errs: st.error(e)
                else:
                    try:
                        create_user(c["firm_id"], nem.strip(), npw, nfn.strip(), nrl, c["user_id"])
                        log_action(c["firm_id"], c["user_id"], "user_create", {"email": nem})
                        st.success(f"User {nfn} created as {nrl}.")
                    except ValueError as e:
                        st.error(str(e))


# ======================================================================
# SESSION GATE
# ======================================================================
for k, v in [("ctx", {}), ("chat_history", []), ("_last_scope", "")]:
    if k not in st.session_state:
        st.session_state[k] = v

if st.session_state["ctx"]:
    fresh = validate_session(st.session_state["ctx"].get("token", ""))
    if fresh:
        st.session_state["ctx"] = fresh
    else:
        st.session_state.clear(); st.rerun()

if not st.session_state["ctx"]:
    show_login(); st.stop()

c      = ctx()
module = show_sidebar()


# ======================================================================
# MODULE: CLIENT INTAKE
# ======================================================================
if module == "📋 Client Intake":
    if is_readonly():
        st.warning("Readonly role cannot submit intake forms."); st.stop()

    st.title("New Client Intake")
    col1, col2 = st.columns(2)
    with col1:
        name    = st.text_input("Full name *")
        email_i = st.text_input("Email")
        phone_i = st.text_input("Phone", placeholder="+94 77 xxx xxxx")
    with col2:
        area    = st.selectbox("Area of law", [""] + AREAS_OF_LAW)
        urgency = st.selectbox("Urgency", ["", "Urgent - within 48 hours", "Within the week", "No immediate deadline"])
        prior   = st.selectbox("Prior legal action?", ["", "No, first time", "Yes - previous attorney", "Yes - in proceedings"])

    matter = st.text_area("Describe the matter *", height=120)
    save_client = st.checkbox("Save as new client record in the system", value=True)

    if st.button("Analyse & Triage ->", type="primary"):
        if not name or not area or not matter:
            st.warning("Please fill in name, area of law, and matter description.")
        else:
            with st.spinner("Analysing..."):
                prompt = (f"Triage this new client intake:\n\nClient: {name}\nArea: {area}\n"
                          f"Urgency: {urgency or 'Not specified'}\nPrior: {prior or 'Not specified'}\n"
                          f"Matter:\n\"{matter}\"\n\nProvide a full intake triage report.")
                result = call_claude(prompt)
                if save_client:
                    cl = create_client_record(name, email_i, phone_i)
                    create_case_record(cl["id"], f"{area} - {name}", area)
                    log_action(c["firm_id"], c["user_id"], "intake", {"client_id": cl["id"]})
                    st.success(f"Client **{name}** and case saved. Go to Document Library to upload files.")
            st.divider()
            st.subheader("Intake Analysis")
            st.caption("AI generated - attorney review required")
            st.markdown(result)


# ======================================================================
# MODULE: CLIENTS & CASES
# ======================================================================
elif module == "👥 Clients & Cases":
    st.title("Clients & Cases")
    tab_list, tab_new = st.tabs(["All Clients", "New Client"])

    with tab_list:
        clients = get_clients()
        if not clients:
            st.info("No clients yet. Use the New Client tab or create one during intake.")
        else:
            for cl in clients:
                with st.expander(f"{cl['full_name']}  {'| ' + cl['email'] if cl.get('email') else ''}"):
                    st.markdown(f"**Email:** {cl.get('email') or '-'}  |  **Phone:** {cl.get('phone') or '-'}")
                    cases = get_cases(cl["id"])
                    if cases:
                        st.markdown(f"**{len(cases)} case(s):**")
                        for ca in cases:
                            icon = "Open" if ca["status"] == "open" else "Closed"
                            st.markdown(f"- [{icon}] **{ca['title']}** · {ca['area_of_law']}")
                    else:
                        st.markdown("*No cases yet.*")

                    with st.form(f"addcase_{cl['id']}"):
                        st.markdown("**Add a case:**")
                        ct = st.text_input("Case title", key=f"ct_{cl['id']}")
                        ca_area = st.selectbox("Area of law", AREAS_OF_LAW, key=f"caa_{cl['id']}")
                        if st.form_submit_button("Add case"):
                            if ct.strip():
                                create_case_record(cl["id"], ct.strip(), ca_area)
                                st.success("Case added."); st.rerun()

    with tab_new:
        st.subheader("New client")
        with st.form("new_client"):
            col1, col2 = st.columns(2)
            with col1:
                nc_name  = st.text_input("Full name *")
                nc_email = st.text_input("Email")
            with col2:
                nc_phone = st.text_input("Phone")
                nc_area  = st.selectbox("Initial area of law", [""] + AREAS_OF_LAW)
            nc_case = st.text_input("Initial case title (optional)")
            if st.form_submit_button("Create client ->", use_container_width=True):
                if not nc_name.strip():
                    st.error("Full name is required.")
                else:
                    cl = create_client_record(nc_name.strip(), nc_email, nc_phone)
                    if nc_case.strip() and nc_area:
                        create_case_record(cl["id"], nc_case.strip(), nc_area)
                    log_action(c["firm_id"], c["user_id"], "client_create", {"name": nc_name})
                    st.success(f"Client **{nc_name}** created."); st.rerun()


# ======================================================================
# MODULE: DOCUMENT LIBRARY
# ======================================================================
elif module == "📁 Document Library":
    st.title("Document Library")
    st.markdown("Upload documents for a specific client case. Files are embedded and stored for AI search.")

    clients = get_clients()
    if not clients:
        st.warning("No clients yet. Create a client first in **Clients & Cases**.")
        st.stop()

    col1, col2 = st.columns(2)
    with col1:
        client_map = {cl["full_name"]: cl for cl in clients}
        sel_cl = client_map[st.selectbox("Client", list(client_map.keys()))]
    with col2:
        cases = get_cases(sel_cl["id"])
        if not cases:
            st.warning("This client has no cases. Add one in Clients & Cases first.")
            st.stop()
        case_map = {ca["title"]: ca for ca in cases}
        sel_ca = case_map[st.selectbox("Case", list(case_map.keys()))]

    st.divider()

    # Show existing documents
    existing = get_stored_docs(case_id=sel_ca["id"])
    if existing:
        st.markdown(f"**{len(existing)} document(s) stored for this case:**")
        for d in existing:
            st.markdown(f"- {d['name']}  _(uploaded {d['created_at'][:10]})_")
        st.divider()

    # Upload
    st.subheader("Upload new documents")
    uploaded_files = st.file_uploader(
        "Choose files", type=["pdf", "docx", "doc"],
        accept_multiple_files=True, label_visibility="collapsed"
    )

    if uploaded_files:
        label = f"Embed & Store {len(uploaded_files)} file(s) for '{sel_ca['title']}'"
        if st.button(label, type="primary", disabled=is_readonly()):
            for uf in uploaded_files:
                with st.spinner(f"Processing {uf.name}..."):
                    file_bytes = uf.read()
                    if uf.name.lower().endswith(".pdf") and PYMUPDF_OK:
                        imgs = pdf_page_images(file_bytes, max_pages=1)
                        if imgs:
                            st.image(base64.b64decode(imgs[0]), width=260, caption=uf.name)
                    result = embed_and_store(file_bytes, uf.name, c["firm_id"], sel_ca["id"])

                if result.get("skipped"):
                    st.warning(f"Skipped {uf.name} - already stored.")
                elif result.get("chunks", 0) > 0:
                    st.success(f"Stored {uf.name} - {result['chunks']} chunks ({result['method']})")
                    log_action(c["firm_id"], c["user_id"], "ingest",
                               {"filename": uf.name, "case_id": sel_ca["id"], "chunks": result["chunks"]})
                else:
                    err = result.get("error", "")
                    st.error(f"Failed {uf.name} - could not extract text. {err}")
            st.rerun()


# ======================================================================
# MODULE: AI SEARCH (RAG)
# ======================================================================
elif module == "💬 AI Search":
    st.title("AI Search")
    st.markdown("Ask questions grounded in stored documents. Searches both case documents and the common knowledge base.")

    scope = st.radio("Search scope", ["Specific case", "All firm documents"], horizontal=True)

    sel_case_id = None
    sel_ca_title = "all documents"
    if scope == "Specific case":
        clients = get_clients()
        if not clients:
            st.info("No clients yet."); st.stop()
        col1, col2 = st.columns(2)
        with col1:
            client_map = {cl["full_name"]: cl for cl in clients}
            sel_cl = client_map[st.selectbox("Client", list(client_map.keys()))]
        with col2:
            cases = get_cases(sel_cl["id"])
            if not cases:
                st.warning("No cases for this client."); st.stop()
            case_map = {ca["title"]: ca for ca in cases}
            sel_ca = case_map[st.selectbox("Case", list(case_map.keys()))]
            sel_case_id  = sel_ca["id"]
            sel_ca_title = sel_ca["title"]

    scope_key = f"{scope}_{sel_case_id}"
    if st.session_state.get("_last_scope") != scope_key:
        st.session_state.chat_history = []
        st.session_state["_last_scope"] = scope_key

    st.divider()

    if not st.session_state.chat_history:
        with st.chat_message("assistant", avatar="="):
            st.markdown(f"Ready to search **{sel_ca_title}**. What would you like to know?")

    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"], avatar="=" if msg["role"] == "assistant" else "U"):
            st.markdown(msg["content"])
            if msg.get("chunks_used") is not None:
                st.caption(f"{msg['chunks_used']} document chunks retrieved")

    if user_input := st.chat_input("Ask about these documents..."):
        st.session_state.chat_history.append({"role": "user", "content": user_input})
        with st.chat_message("user", avatar="U"):
            st.markdown(user_input)
        with st.chat_message("assistant", avatar="="):
            with st.spinner("Searching documents..."):
                history = [{"role": m["role"], "content": m["content"]}
                           for m in st.session_state.chat_history[:-1][-8:]]
                answer, n = rag_ask(user_input, sel_case_id, history)
            st.markdown(answer)
            st.caption(f"{n} document chunks retrieved")
            st.session_state.chat_history.append({"role": "assistant", "content": answer, "chunks_used": n})

    if st.session_state.chat_history:
        if st.button("Clear conversation"):
            st.session_state.chat_history = []; st.rerun()


# ======================================================================
# MODULE: COMMON KNOWLEDGE (Ordinances / Firm-wide)
# ======================================================================
elif module == "⚖️ Common Knowledge":
    st.title("Common Knowledge Base")
    st.markdown("Firm-wide documents — **ordinances, acts, regulations, standard templates** — available for any client search. Not tied to any specific case.")

    tab_up, tab_browse = st.tabs(["Upload", "Browse"])

    with tab_up:
        if is_readonly():
            st.warning("Readonly role cannot upload documents.")
        else:
            common_files = st.file_uploader(
                "Upload ordinances, acts, or firm templates",
                type=["pdf", "docx", "doc"],
                accept_multiple_files=True,
                label_visibility="collapsed"
            )
            if common_files:
                if st.button(f"Store {len(common_files)} file(s) as Common Knowledge", type="primary"):
                    for uf in common_files:
                        with st.spinner(f"Processing {uf.name}..."):
                            file_bytes = uf.read()
                            if uf.name.lower().endswith(".pdf") and PYMUPDF_OK:
                                imgs = pdf_page_images(file_bytes, max_pages=1)
                                if imgs:
                                    st.image(base64.b64decode(imgs[0]), width=260, caption=uf.name)
                            result = embed_and_store(file_bytes, uf.name, c["firm_id"], case_id=None)
                        if result.get("skipped"):
                            st.warning(f"Skipped {uf.name} - already stored.")
                        elif result.get("chunks", 0) > 0:
                            st.success(f"Stored {uf.name} - {result['chunks']} chunks")
                            log_action(c["firm_id"], c["user_id"], "ingest_common", {"filename": uf.name})
                        else:
                            st.error(f"Failed {uf.name} - {result.get('error','could not extract text')}")
                    st.rerun()

    with tab_browse:
        common_docs = get_stored_docs(firm_wide=True)
        if not common_docs:
            st.info("No common documents yet. Upload ordinances and templates in the Upload tab.")
        else:
            st.markdown(f"**{len(common_docs)} common document(s):**")
            for d in common_docs:
                dtype = doc_type_tag(d["name"])
                badge = "badge-common" if dtype in ("ordinance", "sop") else "badge-case"
                st.markdown(
                    f'<span class="badge {badge}">{dtype}</span> {d["name"]} '
                    f'<small style="color:#6b6b80">uploaded {d["created_at"][:10]}</small>',
                    unsafe_allow_html=True
                )


# ======================================================================
# MODULE: ADMIN
# ======================================================================
elif module == "⚙ Admin":
    if not is_admin():
        st.error("Access denied."); st.stop()
    show_admin()
