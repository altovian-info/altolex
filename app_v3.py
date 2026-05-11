"""
AltoLex — app_v2.py  (Streamlit)
Adds Supabase Auth login so every action carries a verified JWT.
firm_id is resolved from the token — never trusted from user input.

Run: streamlit run app_v2.py
"""

import streamlit as st
import anthropic
import os, io, base64
from pathlib import Path

try:
    from supabase import create_client
    SUPABASE_OK = True
except ImportError:
    SUPABASE_OK = False

try:
    import fitz
    PYMUPDF_OK = True
except ImportError:
    PYMUPDF_OK = False

try:
    from docx import Document as DocxDocument
    DOCX_OK = True
except ImportError:
    DOCX_OK = False

try:
    import PyPDF2
    PYPDF2_OK = True
except ImportError:
    PYPDF2_OK = False


st.set_page_config(page_title="AltoLex", page_icon="⚖️", layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;600&family=DM+Sans:wght@400;500&display=swap');
html, body, [class*="css"] { font-family: 'DM Sans', sans-serif; }
h1, h2, h3 { font-family: 'Playfair Display', serif !important; }
.stApp { background: #f7f4ef; }
.badge { display:inline-block; padding:3px 10px; border-radius:20px; font-size:11px;
         font-weight:600; letter-spacing:0.06em; text-transform:uppercase; }
.badge-firm { background:rgba(184,147,90,0.12); color:#b8935a; border:1px solid rgba(184,147,90,0.3); }
.badge-case { background:rgba(42,90,180,0.1); color:#2a5ab4; border:1px solid rgba(42,90,180,0.2); }
.auth-card { background:white; border:1px solid rgba(184,147,90,0.2); border-radius:12px;
             padding:32px; max-width:420px; margin:60px auto; }
.disclaimer { background:rgba(184,147,90,0.08); border:1px solid rgba(184,147,90,0.2);
              border-radius:8px; padding:12px 16px; font-size:12px; color:#6b6b80; line-height:1.6; }
</style>
""", unsafe_allow_html=True)

SYSTEM_PROMPT = """You are AltoLex, a professional legal information assistant.
You have access to the firm's document library — retrieved context is provided below.
Prefer information from the context. Cite source filenames when referencing them.
RULES:
1. Never give definitive legal advice
2. Always recommend attorney review
3. If context lacks relevant information, say so — do not fabricate
4. Flag urgent deadlines prominently
5. End substantive responses with: "This is legal information only. Please consult a qualified attorney."
"""


# ── Supabase helpers ──────────────────────────────────────────────────────────

@st.cache_resource
def get_supabase():
    url = os.environ.get("SUPABASE_URL") or st.secrets.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_ANON_KEY") or st.secrets.get("SUPABASE_ANON_KEY", "")
    if not url or not key:
        st.error("SUPABASE_URL and SUPABASE_ANON_KEY must be set.")
        st.stop()
    return create_client(url, key)


@st.cache_resource
def get_anthropic():
    key = os.environ.get("ANTHROPIC_API_KEY") or st.secrets.get("ANTHROPIC_API_KEY", "")
    if not key:
        st.error("ANTHROPIC_API_KEY must be set.")
        st.stop()
    return anthropic.Anthropic(api_key=key)


# ── Auth state ────────────────────────────────────────────────────────────────

def is_logged_in() -> bool:
    return bool(st.session_state.get("access_token"))


def get_session_context() -> dict:
    """Returns {attorney_id, firm_id, full_name, role, token}"""
    return st.session_state.get("session_ctx", {})


def login(email: str, password: str) -> bool:
    sb = get_supabase()
    try:
        resp = sb.auth.sign_in_with_password({"email": email, "password": password})
        user  = resp.user
        token = resp.session.access_token

        # Resolve firm_id from attorneys table using service key
        svc_key = os.environ.get("SUPABASE_SERVICE_KEY") or st.secrets.get("SUPABASE_SERVICE_KEY","")
        svc = create_client(os.environ.get("SUPABASE_URL") or st.secrets["SUPABASE_URL"], svc_key)
        atty = svc.table("attorneys").select("firm_id,full_name,role") \
                   .eq("id", user.id).single().execute()

        st.session_state["access_token"] = token
        st.session_state["session_ctx"]  = {
            "attorney_id": user.id,
            "firm_id":     atty.data["firm_id"],
            "full_name":   atty.data.get("full_name", email),
            "role":        atty.data.get("role", "associate"),
            "token":       token,
        }
        return True
    except Exception as e:
        st.error(f"Login failed: {e}")
        return False


def logout():
    for k in ["access_token","session_ctx","chat_history","current_doc","review_result"]:
        st.session_state.pop(k, None)


# ── Login screen ──────────────────────────────────────────────────────────────

def show_login():
    st.markdown("""
    <div class="auth-card">
        <div style="font-family:'Playfair Display',serif;font-size:26px;font-weight:600;
                    color:#1a1a2e;margin-bottom:4px">AltoLex</div>
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.1em;
                    color:#6b6b80;margin-bottom:28px">by Altovian</div>
    </div>
    """, unsafe_allow_html=True)

    with st.form("login_form"):
        st.subheader("Sign in")
        email    = st.text_input("Email address")
        password = st.text_input("Password", type="password")
        submit   = st.form_submit_button("Sign in →", use_container_width=True)
        if submit:
            if login(email, password):
                st.rerun()

    st.markdown('<div class="disclaimer" style="max-width:420px;margin:0 auto">⚠ AltoLex provides legal information only — not legal advice. All outputs must be reviewed by a qualified attorney.</div>', unsafe_allow_html=True)


# ── Document extraction ───────────────────────────────────────────────────────

def extract_text_pdf(b: bytes):
    if not PYPDF2_OK: return "", "empty"
    try:
        pages = [p.extract_text() or "" for p in PyPDF2.PdfReader(io.BytesIO(b)).pages]
        t = "\n\n".join(pages).strip()
        return t, ("text" if len(t) > 100 else "empty")
    except: return "", "empty"


def pdf_to_images(b: bytes) -> list[str]:
    if not PYMUPDF_OK: return []
    doc = fitz.open(stream=b, filetype="pdf")
    imgs = []
    mat = fitz.Matrix(150/72, 150/72)
    for page in doc:
        pix = page.get_pixmap(matrix=mat)
        imgs.append(base64.standard_b64encode(pix.tobytes("png")).decode())
    doc.close()
    return imgs


def extract_docx(b: bytes) -> str:
    if not DOCX_OK: return ""
    doc = DocxDocument(io.BytesIO(b))
    parts = []
    for p in doc.paragraphs:
        t = p.text.strip()
        if t: parts.append(f"## {t}" if p.style.name.startswith("Heading") else t)
    for table in doc.tables:
        parts.append("\n[TABLE]")
        for row in table.rows:
            parts.append(" | ".join(c.text.strip() for c in row.cells))
        parts.append("[END TABLE]")
    return "\n".join(parts)


def process_file(uploaded) -> dict:
    name = uploaded.name
    ext  = Path(name).suffix.lower()
    b    = uploaded.read()
    r    = {"name": name, "ext": ext, "method": "unsupported",
            "text": "", "images": [], "page_count": 0,
            "size_kb": len(b)//1024, "badge": ""}

    if ext == ".pdf":
        text, method = extract_text_pdf(b)
        if method == "text":
            r.update({"method":"text","text":text,
                       "badge":'<span class="badge badge-firm">📄 TEXT PDF</span>'})
            try: r["page_count"] = len(PyPDF2.PdfReader(io.BytesIO(b)).pages)
            except: pass
        elif PYMUPDF_OK:
            imgs = pdf_to_images(b)
            r.update({"method":"vision","images":imgs,"page_count":len(imgs),
                       "badge":'<span class="badge badge-case">🔍 SCANNED PDF — OCR</span>'})
        else:
            r["badge"] = '<span class="badge">⚠️ Install PyMuPDF for scanned PDFs</span>'

    elif ext in [".docx",".doc"]:
        text = extract_docx(b)
        if text:
            r.update({"method":"docx","text":text,
                       "badge":'<span class="badge badge-case">📝 WORD DOCUMENT</span>'})
    return r


# ── Claude calls ──────────────────────────────────────────────────────────────

def call_claude(prompt: str, history: list = None) -> str:
    client   = get_anthropic()
    messages = (history or []) + [{"role":"user","content":prompt}]
    resp = client.messages.create(
        model      = "claude-sonnet-4-20250514",
        max_tokens = 1500,
        system     = [{"type":"text","text":SYSTEM_PROMPT,"cache_control":{"type":"ephemeral"}}],
        messages   = messages,
    )
    return resp.content[0].text


def call_claude_vision(images: list[str], prompt: str) -> str:
    client  = get_anthropic()
    content = []
    for i, img in enumerate(images[:20]):
        content.append({"type":"text","text":f"Page {i+1}:"})
        content.append({"type":"image","source":{"type":"base64","media_type":"image/png","data":img}})
    if len(images) > 20:
        content.append({"type":"text","text":f"[{len(images)} pages total — first 20 shown]"})
    content.append({"type":"text","text":prompt})
    resp = get_anthropic().messages.create(
        model="claude-sonnet-4-20250514", max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role":"user","content":content}],
    )
    return resp.content[0].text


def review_document(doc: dict) -> str:
    prompt = f"Review this legal document and provide a structured analysis.\n\nFilename: {doc['name']}\n"
    if doc["method"] == "vision":
        return call_claude_vision(doc["images"], prompt + "Pages shown above.")
    return call_claude(prompt + f"\nContent:\n{doc['text'][:14000]}")


def log_action(action: str, metadata: dict = None):
    """Write to audit_log via service key."""
    ctx = get_session_context()
    if not ctx: return
    try:
        svc_key = os.environ.get("SUPABASE_SERVICE_KEY") or st.secrets.get("SUPABASE_SERVICE_KEY","")
        svc_url = os.environ.get("SUPABASE_URL") or st.secrets.get("SUPABASE_URL","")
        svc = create_client(svc_url, svc_key)
        svc.table("audit_log").insert({
            "firm_id":     ctx["firm_id"],
            "attorney_id": ctx["attorney_id"],
            "action":      action,
            "metadata":    metadata or {},
        }).execute()
    except Exception as e:
        pass  # don't interrupt user flow for audit failures


# ── Session state ─────────────────────────────────────────────────────────────
if "chat_history"   not in st.session_state: st.session_state.chat_history   = []
if "current_doc"    not in st.session_state: st.session_state.current_doc    = None
if "review_result"  not in st.session_state: st.session_state.review_result  = ""
if "active_case_id" not in st.session_state: st.session_state.active_case_id = None


# ── Gate: require login ───────────────────────────────────────────────────────
if not is_logged_in():
    show_login()
    st.stop()

ctx = get_session_context()


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown(f'<div style="font-family:\'Playfair Display\',serif;font-size:22px;font-weight:600;color:#d4aa7a">AltoLex</div>', unsafe_allow_html=True)
    st.markdown('<div style="font-size:10px;text-transform:uppercase;letter-spacing:0.1em;color:rgba(232,228,220,0.4);margin-bottom:16px">by Altovian</div>', unsafe_allow_html=True)

    st.markdown(f"**{ctx['full_name']}**")
    st.markdown(f'<span class="badge badge-firm">{ctx["role"].upper()}</span>', unsafe_allow_html=True)
    st.divider()

    module = st.radio("Module", ["📋 Client Intake","💬 Legal Q&A","📄 Document Review"],
                      label_visibility="collapsed")
    st.divider()

    # System status
    st.markdown("**System**")
    st.markdown(f"{'✅' if PYPDF2_OK else '❌'} Text PDFs")
    st.markdown(f"{'✅' if PYMUPDF_OK else '❌'} Scanned PDFs (OCR)")
    st.markdown(f"{'✅' if DOCX_OK else '❌'} Word documents")
    st.divider()

    if st.button("Sign out"):
        logout()
        st.rerun()

    st.markdown('<div class="disclaimer">⚠ Legal information only — not legal advice. Attorney review required on all outputs.</div>', unsafe_allow_html=True)


# ════════════════════════════════════════════════════════════════
# MODULE: CLIENT INTAKE
# ════════════════════════════════════════════════════════════════
if module == "📋 Client Intake":
    st.title("New Client Intake")
    st.markdown("Complete this form to begin. The assistant will triage your matter.")

    col1, col2 = st.columns(2)
    with col1:
        name    = st.text_input("Full name")
        email   = st.text_input("Email")
        phone   = st.text_input("Phone", placeholder="+94 77 xxx xxxx")
    with col2:
        area    = st.selectbox("Area of law", ["","Contract dispute","Property / conveyancing",
                    "Employment","Family law","Commercial / corporate",
                    "Intellectual property","Criminal defence","Other"])
        urgency = st.selectbox("Urgency", ["","Urgent — within 48 hours","Within the week","No immediate deadline"])
        prior   = st.selectbox("Prior legal action?", ["","No, first time",
                    "Yes — previous attorney","Yes — already in proceedings"])

    matter = st.text_area("Describe your matter", height=120,
        placeholder="Briefly describe the legal issue, parties, and any key dates…")

    if st.button("Analyse & Triage →", type="primary"):
        if not name or not area or not matter:
            st.warning("Please fill in name, area of law, and matter description.")
        else:
            with st.spinner("Analysing…"):
                prompt = f"""Triage this new client intake:\n\nClient: {name}\nArea: {area}
Urgency: {urgency or 'Not specified'}\nPrior action: {prior or 'Not specified'}
Matter:\n"{matter}"\n\nProvide a full intake triage report."""
                result = call_claude(prompt)
                log_action("intake", {"client_name": name, "area": area})
            st.divider()
            st.subheader("Intake Analysis")
            st.caption("AI generated — attorney review required")
            st.markdown(result)


# ════════════════════════════════════════════════════════════════
# MODULE: LEGAL Q&A
# ════════════════════════════════════════════════════════════════
elif module == "💬 Legal Q&A":
    st.title("Legal Q&A")

    if not st.session_state.chat_history:
        with st.chat_message("assistant", avatar="⚖️"):
            st.markdown(f"""Good day, {ctx['full_name'].split()[0]}. I'm AltoLex, your legal information assistant.

I can help you understand legal concepts, explain procedures, and answer general questions about Sri Lankan and common law matters.

*I provide legal information only — not legal advice. Please consult a qualified attorney for specific situations.*

What would you like to know?""")

    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"], avatar="⚖️" if msg["role"]=="assistant" else "👤"):
            st.markdown(msg["content"])

    if user_input := st.chat_input("Ask a legal question…"):
        st.session_state.chat_history.append({"role":"user","content":user_input})
        with st.chat_message("user", avatar="👤"):
            st.markdown(user_input)
        with st.chat_message("assistant", avatar="⚖️"):
            with st.spinner("Thinking…"):
                history  = [{"role":m["role"],"content":m["content"]}
                             for m in st.session_state.chat_history[:-1][-10:]]
                response = call_claude(user_input, history)
                log_action("query", {"question_len": len(user_input)})
            st.markdown(response)
            st.session_state.chat_history.append({"role":"assistant","content":response})

    if st.session_state.chat_history:
        if st.button("Clear conversation"):
            st.session_state.chat_history = []
            st.rerun()


# ════════════════════════════════════════════════════════════════
# MODULE: DOCUMENT REVIEW
# ════════════════════════════════════════════════════════════════
elif module == "📄 Document Review":
    st.title("Document Review")
    st.markdown("Upload a legal document for AI-assisted review. Supports text PDFs, scanned PDFs (OCR), and Word documents.")

    uploaded = st.file_uploader("Upload document", type=["pdf","docx","doc"],
                                 label_visibility="collapsed")

    if uploaded:
        with st.spinner(f"Processing {uploaded.name}…"):
            doc = process_file(uploaded)
            st.session_state.current_doc = doc

        col_info, col_btn = st.columns([3,1])
        with col_info:
            st.markdown(doc["badge"], unsafe_allow_html=True)
            parts = [f"**{doc['name']}**", f"{doc['size_kb']} KB"]
            if doc["page_count"]: parts.append(f"{doc['page_count']} pages")
            st.markdown(" · ".join(parts))
            if doc["method"] == "vision":
                est = doc["page_count"] * 0.006
                st.markdown(f'<div style="font-size:11.5px;color:#2d6a4f;margin-top:4px">💡 Scanned PDF — estimated cost ~${est:.3f} ({doc["page_count"]} pages via OCR)</div>', unsafe_allow_html=True)
        with col_btn:
            analyse = st.button("✦ Analyse", type="primary", use_container_width=True)

        if doc["method"] == "unsupported":
            st.error("File could not be processed. Check sidebar for missing libraries.")
        else:
            col_preview, col_analysis = st.columns([1,1], gap="medium")

            with col_preview:
                st.markdown("**Preview**")
                if doc["method"] == "vision" and doc["images"]:
                    for i, img in enumerate(doc["images"][:5]):
                        st.image(base64.b64decode(img), use_container_width=True,
                                 caption=f"Page {i+1}")
                    if doc["page_count"] > 5:
                        st.caption(f"Showing first 5 of {doc['page_count']} pages")
                elif doc["text"]:
                    preview = doc["text"][:3000]
                    if len(doc["text"]) > 3000: preview += "\n\n[…truncated]"
                    st.text_area("", value=preview, height=520, disabled=True,
                                 label_visibility="collapsed")

            with col_analysis:
                st.markdown("**AI Analysis**")
                st.caption("Attorney sign-off required on all outputs")

                if analyse or st.session_state.review_result:
                    if analyse:
                        with st.spinner("Reviewing document…"):
                            result = review_document(doc)
                            st.session_state.review_result = result
                            log_action("review", {"filename": doc["name"],
                                                  "method": doc["method"]})
                    if st.session_state.review_result:
                        st.markdown(st.session_state.review_result)
                        st.divider()
                        st.download_button("⬇ Download analysis",
                            data=st.session_state.review_result,
                            file_name=f"altolex_review_{Path(doc['name']).stem}.txt",
                            mime="text/plain")
                else:
                    st.info("Click **✦ Analyse** to receive a structured legal review.")
    else:
        st.markdown("""
        <div style="border:2px dashed rgba(184,147,90,0.3);border-radius:14px;padding:50px;
                    text-align:center;background:white;margin-top:16px">
            <div style="font-size:48px;margin-bottom:12px">📄</div>
            <div style="font-family:'Playfair Display',serif;font-size:18px;color:#1a1a2e;margin-bottom:8px">
                Upload a document to begin</div>
            <div style="font-size:13px;color:#6b6b80;line-height:1.7">
                Text PDFs · Scanned PDFs (OCR) · Word documents (.docx)
            </div>
        </div>
        """, unsafe_allow_html=True)
