"""
LeadLens — main.py  (Sprint 2)
================================
Changes from Sprint 1:
  - Parallel pipeline: all 7 Tavily searches fire simultaneously
  - Parallel AI: signals+scores together, then all 3 prose sections together
  - Truth Engine: anti-hallucination system prompts, "Data Not Publicly Available" enforcement
  - Sector-aware prompts: Mining / CleanTech / AgTech buying committee mapping
  - Site vs HQ validation step
  - Incumbent + displacement angle in competitor battle card
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Optional

import asyncpg
import bcrypt
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jose import JWTError, jwt
from pydantic import BaseModel
from tavily import AsyncTavilyClient

load_dotenv()

# ── Config ────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TAVILY_API_KEY    = os.getenv("TAVILY_API_KEY", "")
DATABASE_URL      = os.getenv("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)
JWT_SECRET        = os.getenv("JWT_SECRET", secrets.token_hex(32))
JWT_ALGORITHM     = "HS256"
JWT_EXPIRE_HOURS  = 24
ADMIN_TOKEN       = os.getenv("ADMIN_TOKEN", "")
PRIMARY_MODEL     = "claude-sonnet-4-6"
FAST_MODEL        = "claude-haiku-4-5-20251001"
REPORT_LIMIT      = 10

claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
tavily = AsyncTavilyClient(api_key=TAVILY_API_KEY)

_pool: asyncpg.Pool | None = None

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10, command_timeout=30)
    return _pool

@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_pool()
    print("Database pool ready")
    yield
    if _pool:
        await _pool.close()

app = FastAPI(title="LeadLens API", version="0.3.0", lifespan=lifespan, docs_url="/api/docs", redoc_url="/api/redoc")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
templates = Jinja2Templates(directory="templates")


# ============================================================
# SECTOR INTELLIGENCE
# ============================================================

SECTOR_CONTEXT = {
    "mining": {
        "champion":        "Mine Site Manager / VP Operations",
        "economic_buyer":  "CFO / VP Capital Projects",
        "gatekeeper":      "IT Director / OT Systems Engineer",
        "pain_points":     "unplanned downtime, safety compliance (MSHA/ISO), aging OT infrastructure, ESG reporting, capex justification",
        "buying_triggers": "permit approvals, CapEx announcements, new mine development, ESG deadlines, executive hires",
        "incumbents":      "Hexagon Mining, Caterpillar MineStar, Komatsu KOMTRAX, Wenco, Micromine",
        "avg_deal_size":   "$250k-$2M+",
        "sales_cycle":     "6-18 months",
    },
    "cleantech": {
        "champion":        "Director of Operations / Plant Manager",
        "economic_buyer":  "CEO / CFO / Chief Sustainability Officer",
        "gatekeeper":      "Engineering Lead / Systems Integrator",
        "pain_points":     "grid interconnection delays, ITC/PTC compliance, O&M cost reduction, carbon credit verification",
        "buying_triggers": "IRA grant awards, PPA signings, new project announcements, Series B+ funding",
        "incumbents":      "Svante, Carbon Clean, Envision Energy, AVEVA, AutoGrid",
        "avg_deal_size":   "$150k-$1M",
        "sales_cycle":     "4-12 months",
    },
    "agtech": {
        "champion":        "Farm Operations Manager / Precision Ag Director",
        "economic_buyer":  "Owner / CFO / Co-op Procurement Director",
        "gatekeeper":      "IT Manager / Agronomist",
        "pain_points":     "input cost reduction, yield variability, water efficiency, regulatory compliance, labour shortages",
        "buying_triggers": "USDA grant awards, crop season planning, co-op contract renewals, new farm acquisition",
        "incumbents":      "Trimble Ag, John Deere Operations Center, Ag Leader, Climate Corp, Granular",
        "avg_deal_size":   "$50k-$500k",
        "sales_cycle":     "3-9 months",
    },
    "": {
        "champion":        "Operations Lead / Site Manager",
        "economic_buyer":  "CFO / VP Finance",
        "gatekeeper":      "IT Director / Engineering Lead",
        "pain_points":     "operational efficiency, cost reduction, compliance, integration complexity",
        "buying_triggers": "funding rounds, executive hires, expansion announcements, contract wins",
        "incumbents":      "category leaders in their specific vertical",
        "avg_deal_size":   "$100k+",
        "sales_cycle":     "3-12 months",
    }
}

def get_sector_ctx(sector: str) -> dict:
    return SECTOR_CONTEXT.get(sector, SECTOR_CONTEXT[""])


# ============================================================
# TRUTH ENGINE SYSTEM PROMPTS
# ============================================================

TRUTH_ENGINE_SYSTEM = """You are a senior sales intelligence analyst specializing in industrial B2B sectors (Mining, CleanTech, AgTech).

ANTI-HALLUCINATION RULES — FOLLOW WITHOUT EXCEPTION:
1. If a specific data point cannot be verified from the provided source data, write exactly: "Data Not Publicly Available"
2. Never invent names, titles, phone numbers, email addresses, or financial figures.
3. Every factual claim must be grounded in the provided web data.
4. Distinguish clearly between SITE LOCATION (where operations happen) and CORPORATE HQ (where decisions are made).
5. Be specific, direct, and actionable. No filler phrases."""

TRUTH_ENGINE_JSON_SYSTEM = """You are a senior sales intelligence analyst. Output ONLY valid JSON.

ANTI-HALLUCINATION RULES:
1. Only report what is explicitly in the provided source data.
2. If a data point is not found, use "Data Not Publicly Available".
3. Never invent names, figures, or facts not present in the source data.
4. No markdown, no backticks. Raw JSON only."""


# ============================================================
# AUTH
# ============================================================

class TenantContext(BaseModel):
    user_id: int
    org_id: int
    email: str
    role: str
    org_features: dict

def create_jwt(user_id: int, org_id: int, email: str, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS)
    return jwt.encode({"sub": str(user_id), "org": org_id, "email": email, "role": role, "exp": expire}, JWT_SECRET, algorithm=JWT_ALGORITHM)

def decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

async def get_tenant(request: Request) -> TenantContext:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization header required")
    payload = decode_jwt(auth.removeprefix("Bearer ").strip())
    user_id = int(payload["sub"])
    org_id  = int(payload["org"])
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(f"SET LOCAL app.current_org_id = '{org_id}'")
        org = await conn.fetchrow("SELECT features FROM organizations WHERE id = $1", org_id)
        if not org:
            raise HTTPException(status_code=403, detail="Organization not found")
    features = org["features"] if org else {}
    if isinstance(features, str):
        features = json.loads(features)
    elif not isinstance(features, dict):
        features = {}
    return TenantContext(user_id=user_id, org_id=org_id, email=payload.get("email",""), role=payload.get("role","member"), org_features=features)

async def require_feature(feature: str, ctx: TenantContext):
    if not ctx.org_features.get(feature, False):
        raise HTTPException(status_code=403, detail=f"Feature '{feature}' not enabled for your plan.")

@asynccontextmanager
async def tenant_conn(org_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f"SET LOCAL app.current_org_id = '{org_id}'")
            yield conn

def hash_password(p: str) -> str: return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()
def check_password(p: str, h: str) -> bool: return bcrypt.checkpw(p.encode(), h.encode())


# ============================================================
# REQUEST MODELS
# ============================================================

class RegisterRequest(BaseModel):
    email: str; password: str; invitation_code: str; org_name: Optional[str] = None

class LoginRequest(BaseModel):
    email: str; password: str

class ResearchRequest(BaseModel):
    company: str; name: str; role: str = ""; product: str; sector: str = ""; force_fit: bool = False; force_fit: bool = False

class NoteRequest(BaseModel):
    note: str

class ReminderRequest(BaseModel):
    note: str; date: str; lead_id: Optional[int] = None; company: str = ""; ai_suggested: bool = False

class DebriefRequest(BaseModel):
    company: str; notes: str; name: str; role: str = ""; product: str

class ObjectionRequest(BaseModel):
    company: str; product: str; objection: str; profile: str = ""

class EarlyAccessRequest(BaseModel):
    email: str

class UpdateLeadStageRequest(BaseModel):
    pipeline_stage: str

class AdminFeatureRequest(BaseModel):
    feature: str; enabled: bool

class AdminPlanRequest(BaseModel):
    plan_tier: str

class AdminGrantRequest(BaseModel):
    amount: int = 10

class AdminCodeRequest(BaseModel):
    org_id: int


# ============================================================
# AI HELPERS
# ============================================================

async def search_web(query: str) -> str:
    try:
        results = await tavily.search(query=query, max_results=5)
        return "".join(f"[Source: {r['url']}]\n{r['content']}\n\n" for r in results.get("results", []))
    except Exception as e:
        print(f"search_web error: {e}")
        return ""

async def ask_ai(prompt: str, model: str = PRIMARY_MODEL, retries: int = 3) -> str:
    for attempt in range(retries):
        try:
            msg = await claude.messages.create(model=model, max_tokens=2048, system=TRUTH_ENGINE_SYSTEM, messages=[{"role":"user","content":prompt}])
            return msg.content[0].text
        except Exception as e:
            print(f"ask_ai error (attempt {attempt+1}): {e}")
            if attempt < retries - 1: await asyncio.sleep(2)
    return ""

async def ask_ai_json(prompt: str, model: str = PRIMARY_MODEL, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            msg = await claude.messages.create(model=model, max_tokens=1024, system=TRUTH_ENGINE_JSON_SYSTEM, messages=[{"role":"user","content":prompt}])
            text = msg.content[0].text.strip().replace("```json","").replace("```","").strip()
            start, end = text.find("{"), text.rfind("}") + 1
            if start == -1 or end == 0: continue
            return json.loads(text[start:end])
        except Exception as e:
            print(f"ask_ai_json error (attempt {attempt+1}): {e}")
            if attempt < retries - 1: await asyncio.sleep(2)
    return {}

def extract_section(text: str, header: str, all_headers: list) -> str:
    try:
        start = text.find(header)
        if start == -1: return ""
        start += len(header)
        end = len(text)
        for h in all_headers:
            if h != header:
                pos = text.find(h, start)
                if pos != -1 and pos < end: end = pos
        return text[start:end].strip()
    except: return ""


# ============================================================
# PUBLIC ROUTES
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    return templates.TemplateResponse("landing.html", {"request": request})

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/join", response_class=HTMLResponse)
async def join_page(request: Request, code: Optional[str] = None):
    if not code:
        return templates.TemplateResponse("join.html", {"request": request, "error": True, "org_name": "", "code": ""})
    pool = await get_pool()
    async with pool.acquire() as conn:
        invite = await conn.fetchrow("""
            SELECT ic.code, ic.used, o.name AS org_name
            FROM invitation_codes ic
            JOIN organizations o ON o.id = ic.organization_id
            WHERE ic.code = $1
        """, code.strip().upper())
    if not invite or invite["used"]:
        return templates.TemplateResponse("join.html", {"request": request, "error": True, "org_name": "", "code": ""})
    return templates.TemplateResponse("join.html", {
        "request": request, "error": False,
        "org_name": invite["org_name"], "code": code.strip().upper()
    })

@app.post("/early-access")
async def early_access(body: EarlyAccessRequest):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO early_access (email) VALUES ($1) ON CONFLICT (email) DO NOTHING", body.email.strip().lower())
    return {"success": True}

@app.post("/register")
async def register(body: RegisterRequest):
    email = body.email.strip().lower()
    code  = body.invitation_code.strip().upper()
    if not email or not body.password or not code: raise HTTPException(400, "All fields required.")
    if len(body.password) < 8: raise HTTPException(400, "Password must be at least 8 characters.")
    pool = await get_pool()
    async with pool.acquire() as conn:
        invite = await conn.fetchrow("SELECT * FROM invitation_codes WHERE code = $1", code)
        if not invite: raise HTTPException(400, "Invalid invitation code.")
        if invite["used"]: raise HTTPException(400, "Invitation code already used.")
        if await conn.fetchrow("SELECT id FROM users WHERE email = $1", email):
            raise HTTPException(400, "Email already registered.")
        org_id = invite["organization_id"]
        org    = await conn.fetchrow("SELECT name, report_limit_per_user FROM organizations WHERE id = $1", org_id)
        limit  = org["report_limit_per_user"] if org else REPORT_LIMIT
        count  = await conn.fetchval("SELECT COUNT(*) FROM users WHERE organization_id = $1", org_id)
        role   = "owner" if count == 0 else "member"
        async with conn.transaction():
            uid = await conn.fetchval("""
                INSERT INTO users (organization_id, email, password_hash, invite_code, role, report_limit)
                VALUES ($1,$2,$3,$4,$5,$6) RETURNING id
            """, org_id, email, hash_password(body.password), code, role, limit)
            await conn.execute("UPDATE invitation_codes SET used=TRUE, used_by=$1 WHERE code=$2", email, code)
    org_name = org["name"] if org else ""
    return {"success": True, "token": create_jwt(uid, org_id, email, role), "role": role, "org_name": org_name}

@app.post("/login")
async def login(body: LoginRequest):
    email = body.email.strip().lower()
    if not email or not body.password: raise HTTPException(400, "Email and password required.")
    pool = await get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE email = $1", email)
    if not user or not check_password(body.password, user["password_hash"]): raise HTTPException(401, "Invalid email or password.")
    if not user["is_active"]: raise HTTPException(403, "Account deactivated.")
    pool2 = await get_pool()
    async with pool2.acquire() as conn2:
        org = await conn2.fetchrow("SELECT name FROM organizations WHERE id = $1", user["organization_id"])
    org_name = org["name"] if org else ""
    return {"success": True, "token": create_jwt(user["id"], user["organization_id"], email, user["role"]), "role": user["role"], "org_name": org_name}


# ============================================================
# USAGE & ACCESS
# ============================================================

@app.get("/usage")
async def get_usage(ctx: TenantContext = Depends(get_tenant)):
    async with tenant_conn(ctx.org_id) as conn:
        user = await conn.fetchrow("SELECT email, report_count, report_limit FROM users WHERE id = $1", ctx.user_id)
    if not user: raise HTTPException(404, "User not found")
    return {"email": user["email"], "report_count": user["report_count"], "report_limit": user["report_limit"], "remaining": user["report_limit"] - user["report_count"]}

@app.post("/invite/generate")
async def invite_generate(request: Request, ctx: TenantContext = Depends(get_tenant)):
    if ctx.role not in ("owner", "admin"):
        raise HTTPException(403, "Only owners and admins can generate invite links.")
    code = "-".join([secrets.token_hex(3).upper() for _ in range(2)])
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO invitation_codes (organization_id, code) VALUES ($1,$2)",
            ctx.org_id, code
        )
        org = await conn.fetchrow("SELECT name FROM organizations WHERE id = $1", ctx.org_id)
    base_url = str(request.base_url).rstrip("/")
    invite_link = f"{base_url}/join?code={code}"
    return {"invite_link": invite_link, "code": code, "org_name": org["name"]}

@app.post("/request-access")
async def request_access(ctx: TenantContext = Depends(get_tenant)):
    async with tenant_conn(ctx.org_id) as conn:
        if await conn.fetchrow("SELECT id FROM access_requests WHERE user_id=$1 AND status='pending'", ctx.user_id):
            return {"success": True, "already_requested": True}
        await conn.execute("INSERT INTO access_requests (organization_id, user_id, email, status) VALUES ($1,$2,$3,'pending')", ctx.org_id, ctx.user_id, ctx.email)
    return {"success": True, "already_requested": False}


# ============================================================
# LEADS
# ============================================================

@app.get("/leads")
async def get_leads(ctx: TenantContext = Depends(get_tenant)):
    async with tenant_conn(ctx.org_id) as conn:
        rows = await conn.fetch("SELECT * FROM leads WHERE organization_id=$1 ORDER BY created_at DESC", ctx.org_id)
    result = []
    for row in rows:
        d = dict(row)
        for f in ["scores","fit_check","signals","sources"]:
            if isinstance(d.get(f), str):
                try: d[f] = json.loads(d[f])
                except: d[f] = {}
        for f in ["created_at","updated_at"]:
            if d.get(f): d[f] = d[f].isoformat()
        result.append(d)
    return result

@app.delete("/leads/{lead_id}")
async def delete_lead(lead_id: int, ctx: TenantContext = Depends(get_tenant)):
    async with tenant_conn(ctx.org_id) as conn:
        await conn.execute("DELETE FROM leads WHERE id=$1 AND user_id=$2", lead_id, ctx.user_id)
    return {"success": True}

@app.patch("/leads/{lead_id}/stage")
async def update_lead_stage(lead_id: int, body: UpdateLeadStageRequest, ctx: TenantContext = Depends(get_tenant)):
    valid = {"new","contacted","meeting","proposal","closed_won","closed_lost"}
    if body.pipeline_stage not in valid: raise HTTPException(400, "Invalid stage.")
    async with tenant_conn(ctx.org_id) as conn:
        await conn.execute("UPDATE leads SET pipeline_stage=$1 WHERE id=$2 AND organization_id=$3", body.pipeline_stage, lead_id, ctx.org_id)
    return {"success": True}

@app.get("/leads/{lead_id}/notes")
async def get_notes(lead_id: int, ctx: TenantContext = Depends(get_tenant)):
    async with tenant_conn(ctx.org_id) as conn:
        rows = await conn.fetch("SELECT * FROM lead_notes WHERE lead_id=$1 AND organization_id=$2 ORDER BY id DESC", lead_id, ctx.org_id)
    result = [dict(r) for r in rows]
    for d in result:
        if d.get("created_at"): d["created_at"] = d["created_at"].isoformat()
    return result

@app.post("/leads/{lead_id}/notes/add")
async def add_note(lead_id: int, body: NoteRequest, ctx: TenantContext = Depends(get_tenant)):
    if not body.note.strip(): raise HTTPException(400, "Note is empty")
    async with tenant_conn(ctx.org_id) as conn:
        await conn.execute("INSERT INTO lead_notes (organization_id, lead_id, user_id, note) VALUES ($1,$2,$3,$4)", ctx.org_id, lead_id, ctx.user_id, body.note.strip())
    return {"success": True}


# ── Reminders ─────────────────────────────────────────────────

@app.get("/reminders")
async def get_reminders(ctx: TenantContext = Depends(get_tenant)):
    async with tenant_conn(ctx.org_id) as conn:
        rows = await conn.fetch("SELECT * FROM reminders WHERE user_id=$1 AND organization_id=$2 ORDER BY due_date ASC", ctx.user_id, ctx.org_id)
    result = [dict(r) for r in rows]
    for d in result:
        for f in ["created_at","due_date"]:
            if d.get(f): d[f] = d[f].isoformat()
    return result

@app.post("/reminders")
async def add_reminder(body: ReminderRequest, ctx: TenantContext = Depends(get_tenant)):
    if not body.note or not body.date: raise HTTPException(400, "Note and date required")
    async with tenant_conn(ctx.org_id) as conn:
        await conn.execute("INSERT INTO reminders (organization_id, user_id, lead_id, company, note, due_date, ai_suggested) VALUES ($1,$2,$3,$4,$5,$6,$7)", ctx.org_id, ctx.user_id, body.lead_id, body.company, body.note, body.date, body.ai_suggested)
    return {"success": True}

@app.delete("/reminders/{reminder_id}")
async def delete_reminder(reminder_id: int, ctx: TenantContext = Depends(get_tenant)):
    async with tenant_conn(ctx.org_id) as conn:
        await conn.execute("DELETE FROM reminders WHERE id=$1 AND user_id=$2", reminder_id, ctx.user_id)
    return {"success": True}

@app.post("/reminders/ai-suggest")
async def ai_suggest_reminder(request: Request, ctx: TenantContext = Depends(get_tenant)):
    data = await request.json()
    lead_id = data.get("lead_id")
    if not lead_id: raise HTTPException(400, "lead_id required")
    async with tenant_conn(ctx.org_id) as conn:
        notes = await conn.fetch("SELECT note, created_at FROM lead_notes WHERE lead_id=$1 AND user_id=$2 ORDER BY id DESC LIMIT 5", lead_id, ctx.user_id)
    if not notes: return {"error": "No notes found for this lead yet."}
    notes_text = "\n".join([f"[{n['created_at']}] {n['note']}" for n in notes])
    today = datetime.now().strftime("%Y-%m-%d")
    today_dow = datetime.now().strftime("%A")
    result = await ask_ai(f"""Today is {today_dow}, {today}.
Salesperson working on deal with: {data.get('company','')}
Recent notes: {notes_text}
Suggest best follow-up date. Respond ONLY in this format:
DATE: YYYY-MM-DD
REASON: one sentence""")
    try:
        dl = [l for l in result.split('\n') if l.startswith('DATE:')]
        rl = [l for l in result.split('\n') if l.startswith('REASON:')]
        return {"date": dl[0].replace('DATE:','').strip() if dl else today, "suggestion": rl[0].replace('REASON:','').strip() if rl else result}
    except:
        return {"error": "Could not parse suggestion."}


# ============================================================
# RESEARCH — Sprint 2: Parallel Pipeline + Truth Engine
# ============================================================

@app.post("/research")
async def research(body: ResearchRequest, ctx: TenantContext = Depends(get_tenant)):

    async with tenant_conn(ctx.org_id) as conn:
        user = await conn.fetchrow("SELECT report_count, report_limit FROM users WHERE id=$1", ctx.user_id)
    if not user: raise HTTPException(401, "Session expired.")
    if user["report_count"] >= user["report_limit"]:
        raise HTTPException(403, json.dumps({"error": True, "limit_reached": True, "message": f"You've used all {user['report_limit']} reports."}))

    company_name = body.company.strip()
    user_name    = body.name.strip()
    user_role    = body.role.strip()
    product      = body.product.strip()
    sector       = body.sector.strip().lower()

    if not company_name or not user_name or not product:
        raise HTTPException(400, "Please fill in your name, what you sell, and a company name")

    sc = get_sector_ctx(sector)
    print(f"\n=== Research: {company_name} | {product} | sector={sector or 'generic'} ===")

    # ── Step 1: Search strategy (fast model) ──────────────────
    strategy = await ask_ai_json(f"""
Selling {product} to {company_name}. Sector: {sector or 'industrial B2B'}.
Return ONLY raw JSON:
{{
    "q_overview":    "<company size, HQ, revenue, key operations>",
    "q_signals":     "<expansion, funding, hiring signals for {product}>",
    "q_procurement": "<how {company_name} buys technology/services like {product}>",
    "q_site":        "<{company_name} operational sites, facilities, field offices>",
    "key_facts":     "<3 most important facts for selling {product} to {company_name}>",
    "pain_points":   "<specific pain points making {company_name} likely to buy {product}>"
}}
""", model=FAST_MODEL) or {
        "q_overview": f"{company_name} company overview revenue employees",
        "q_signals":  f"{company_name} expansion hiring news 2025",
        "q_procurement": f"{company_name} technology procurement vendor",
        "q_site":     f"{company_name} office locations operations",
        "key_facts":  f"Company size, growth, decision maker for {product}",
        "pain_points": f"Operational challenges relevant to {product}",
    }

    # ── Step 2: ALL 12 searches fire simultaneously ────────────
    t0 = datetime.now()
    current_year = datetime.now().year
    prev_year    = current_year - 1
    champion_role   = sc['champion'].split('/')[0].strip()
    buyer_role      = sc['economic_buyer'].split('/')[0].strip()
    (web_overview, web_news, web_procurement, web_site,
     web_funding, web_leadership, web_contracts,
     web_contacts, web_email_pattern, web_executives,
     web_linkedin_exec, web_linkedin_champion) = await asyncio.gather(
        search_web(f"{company_name} {strategy['q_overview']}"),
        search_web(f"{company_name} {strategy['q_signals']}"),
        search_web(f"{company_name} {strategy['q_procurement']}"),
        search_web(f"{company_name} {strategy['q_site']}"),
        search_web(f"{company_name} funding raised investment series {prev_year} {current_year}"),
        search_web(f"{company_name} CEO president leadership team {current_year}"),
        search_web(f"{company_name} new contract win partnership awarded {prev_year} {current_year}"),
        search_web(f"{company_name} email format site:hunter.io OR site:rocketreach.co OR site:apollo.io"),
        search_web(f"{company_name} email contact domain"),
        search_web(f"{company_name} {champion_role} OR {buyer_role} linkedin OR contact"),
        search_web(f'"{company_name}" CEO OR "{buyer_role}" site:linkedin.com/in'),
        search_web(f'"{company_name}" "{champion_role}" site:linkedin.com/in'),
    )
    print(f"  12 searches done in {(datetime.now()-t0).total_seconds():.1f}s")

    if not web_overview or len(web_overview.strip()) < 50:
        raise HTTPException(400, f"Could not find information about '{company_name}'. Check spelling.")

    # ── Step 3: Site vs HQ + Fit check — simultaneously ────────
    location_data, fit_check = await asyncio.gather(
        ask_ai_json(f"""
Company: {company_name}
Source: {web_overview[:500]} {web_site[:300]}
Return ONLY raw JSON:
{{
    "corporate_hq":   "<city, state/country of HQ, or 'Data Not Publicly Available'>",
    "site_locations": "<operational sites, mines, plants, or 'Data Not Publicly Available'>",
    "buying_note":    "<where buying decision for {product} actually happens>"
}}
""", model=FAST_MODEL),
        ask_ai_json(f"""
Selling: {product} | To: {company_name} | Sector: {sector or 'unknown'}
Web data: {web_overview[:600]}

PRODUCT DESCRIPTION: {product}
This is a B2B sales intelligence platform. It helps sales reps and business development teams research companies, find contacts, and close deals faster.

WHO IS A GOOD FIT — mark is_fit=true if ANY of these apply:
1. Company has a dedicated sales team, account executives, or business development reps
2. Company sells B2B products or services (any industry)
3. Company has enterprise or mid-market customers they need to prospect
4. Company is a technology vendor, equipment supplier, services firm, or consultant
5. Company has complex deals requiring research and relationship-building
6. Company is small but sells high-value contracts ($50k+)

WHO IS NOT A FIT — only mark is_fit=false if ALL of these apply:
1. Company is purely a consumer brand with no B2B sales motion
2. Company has zero sales team or BD function
3. Company sells only low-value transactional products
4. Company is a mine/farm/plant operator with no outbound sales team at all

KEY INSIGHT: Even small B2B tech companies (10-100 employees) with just 2-3 BD reps closing large deals are EXCELLENT fits. Do not penalize small company size.

Return ONLY raw JSON:
{{
    "is_fit":           <true or false>,
    "confidence":       <0-100>,
    "product_category": "<product type>",
    "reasoning":        "<one sentence grounded in the web data>",
    "warning":          "<if not fit: specific reason + better target suggestion. if fit: empty string>",
    "right_company":    "<if wrong company found in search: suggest correct search. otherwise: empty>",
    "decision_maker":   "<exact job title who approves buying {product} — e.g. VP Sales, Head of Business Development>",
    "champion_title":   "<frontline user title — e.g. Business Development Manager, Account Executive, Sales Rep>",
    "gatekeeper_title": "<IT or ops gatekeeper — e.g. Sales Operations Manager, CRM Administrator, IT Manager>"
}}
"""),
    )

    hq_location   = (location_data or {}).get("corporate_hq", "Data Not Publicly Available")
    site_location = (location_data or {}).get("site_locations", "Data Not Publicly Available")
    buying_note   = (location_data or {}).get("buying_note", "")

    fit_check = fit_check or {
        "is_fit": True, "confidence": 70, "product_category": product,
        "reasoning": "Proceeding with research.", "warning": "", "right_company": "",
        "decision_maker": sc["economic_buyer"], "champion_title": sc["champion"],
        "gatekeeper_title": sc["gatekeeper"],
    }

    # Force fit override — user clicked "Research anyway"
    if body.force_fit:
        fit_check["is_fit"] = True
        fit_check["warning"] = ""
        print("  ✓ Force fit override applied")

    created_at = datetime.now(timezone.utc)

    if not fit_check.get("is_fit", True):
        result = _build_empty_result(company_name, user_name, user_role, product, sector, fit_check, created_at)
        result["site_location"] = site_location
        result["hq_location"]   = hq_location
        lead_id = await _save_lead(ctx, result, fit_check, {}, {}, sector, site_location, hq_location)
        result["id"] = lead_id
        return result

    # ── Step 4: Signals + Scores + Contact Enrichment — all simultaneously ──
    signals, scores, contacts = await asyncio.gather(
        ask_ai_json(f"""
Buying signals for: {company_name} | Product: {product}
FUNDING: {web_funding[:500]}
LEADERSHIP: {web_leadership[:500]}
EXPANSION/NEWS: {web_news[:500]}
CONTRACTS: {web_contracts[:500]}
SECTOR TRIGGERS: {sc['buying_triggers']}
Only report signals explicitly found above. Return ONLY raw JSON:
{{
    "timing_score":   <0-100>,
    "recommendation": "<reach out now/wait/avoid — cite actual data>",
    "funding":    {{"detected":<true/false>,"score":<0-10>,"detail":"<finding or No recent funding detected>"}},
    "leadership": {{"detected":<true/false>,"score":<0-10>,"detail":"<finding or No leadership changes>"}},
    "expansion":  {{"detected":<true/false>,"score":<0-10>,"detail":"<finding or No expansion signals>"}},
    "hiring":     {{"detected":<true/false>,"score":<0-10>,"detail":"<finding or No relevant hiring>"}},
    "contracts":  {{"detected":<true/false>,"score":<0-10>,"detail":"<finding or No new contracts>"}}
}}
"""),
        ask_ai_json(f"""
Score opportunity: {company_name} | {product} | {sector or 'industrial'}
FIT: {fit_check.get('confidence',70)}% | FACTS: {strategy.get('key_facts','')}
PAIN: {strategy.get('pain_points','')} | DATA: {web_overview[:400]}
DEAL SIZE: {sc['avg_deal_size']} | CYCLE: {sc['sales_cycle']}
Return ONLY raw JSON:
{{"deal_readiness":<0-100>,"need_score":<0-100>,"budget_score":<0-100>,"decision_speed":<0-100>,"overall":<0-100>}}
"""),
        ask_ai_json(f"""
Extract contact intelligence for {company_name}.

EMAIL PATTERN DATA: {web_email_pattern[:600]}
EXECUTIVE DATA: {web_executives[:600]}
CONTACT DATA: {web_contacts[:400]}
COMPANY OVERVIEW: {web_overview[:400]}
LINKEDIN EXECUTIVE SEARCH: {web_linkedin_exec[:600]}
LINKEDIN CHAMPION SEARCH: {web_linkedin_champion[:600]}

ROLES TO FIND:
- Champion: {fit_check.get('champion_title', sc['champion'])}
- Economic Buyer: {fit_check.get('decision_maker', sc['economic_buyer'])}
- Gatekeeper: {fit_check.get('gatekeeper_title', sc['gatekeeper'])}

RULES:
1. Only report names explicitly found in the source data above. Never invent names.
2. For LinkedIn URLs: extract any linkedin.com/in/... URLs found in the search results above.
   Assign them to the correct role based on the person's title mentioned alongside the URL.
3. EMAIL CONSTRUCTION: If you find both a person's name AND the company email pattern, construct their actual email.
   - Pattern "firstname.lastname@domain.com" + "John Smith" = "john.smith@domain.com"
   - Pattern "f.lastname@domain.com" + "John Smith" = "j.smith@domain.com"
   - Pattern "[first_initial][last]@domain.com" + "John Smith" = "jsmith@domain.com"
   Always construct the actual email — never return the raw pattern with brackets.
4. email_pattern: return a clean readable example like "jsmith@domain.com" NOT "[first_initial][last]@domain.com"
5. If name is unknown, set email to "Data Not Publicly Available"
6. company_domain: extract just the domain (e.g. "minesense.com")

Return ONLY raw JSON:
{{
    "company_domain": "<domain.com or 'Data Not Publicly Available'>",
    "email_pattern": "<human readable example e.g. jsmith@domain.com>",
    "email_confidence": "<High|Medium|Low|Unknown>",
    "champion": {{
        "name": "<full name if found or 'Data Not Publicly Available'>",
        "title": "<exact title if found>",
        "email": "<constructed email or 'Data Not Publicly Available'>",
        "email_status": "<Verified|Inferred|Data Not Publicly Available>",
        "linkedin": "<full linkedin.com/in/... URL if found in search results or 'Data Not Publicly Available'>"
    }},
    "economic_buyer": {{
        "name": "<full name if found or 'Data Not Publicly Available'>",
        "title": "<exact title if found>",
        "email": "<constructed email or 'Data Not Publicly Available'>",
        "email_status": "<Verified|Inferred|Data Not Publicly Available>",
        "linkedin": "<full linkedin.com/in/... URL if found or 'Data Not Publicly Available'>"
    }},
    "gatekeeper": {{
        "name": "<full name if found or 'Data Not Publicly Available'>",
        "title": "<exact title if found>",
        "email": "<constructed email or 'Data Not Publicly Available'>",
        "email_status": "<Verified|Inferred|Data Not Publicly Available>",
        "linkedin": "<full linkedin.com/in/... URL if found or 'Data Not Publicly Available'>"
    }}
}}
"""),
    )

    signals = signals or {"timing_score":50,"recommendation":"Research further.","funding":{"detected":False,"score":0,"detail":"No recent funding detected"},"leadership":{"detected":False,"score":0,"detail":"No leadership changes"},"expansion":{"detected":False,"score":0,"detail":"No expansion signals"},"hiring":{"detected":False,"score":0,"detail":"No relevant hiring"},"contracts":{"detected":False,"score":0,"detail":"No new contracts"}}
    scores  = scores if scores and "overall" in scores else {"deal_readiness":50,"need_score":55,"budget_score":50,"decision_speed":50,"overall":51}

    _na = "Data Not Publicly Available"
    _blank_person = lambda title: {"name":_na,"title":title,"email":_na,"email_status":_na,"linkedin":_na}
    contacts = contacts or {
        "company_domain": _na, "email_pattern": _na, "email_confidence": "Unknown",
        "champion":       _blank_person(fit_check.get('champion_title', sc['champion'])),
        "economic_buyer": _blank_person(fit_check.get('decision_maker', sc['economic_buyer'])),
        "gatekeeper":     _blank_person(fit_check.get('gatekeeper_title', sc['gatekeeper'])),
    }

    # ── Step 5: All 3 prose sections — simultaneously ──────────
    sp        = f"{user_name}{f', {user_role}' if user_role else ''}"
    dm        = fit_check.get("decision_maker", sc["economic_buyer"])
    champion  = fit_check.get("champion_title", sc["champion"])
    gatekeeper= fit_check.get("gatekeeper_title", sc["gatekeeper"])
    incumbent = sc["incumbents"]

    H1 = ["COMPANY PROFILE:","HQ VS SITE:","BUYING COMMITTEE:","OPENING LINE:","DISCOVERY QUESTIONS:","OBJECTIONS AND RESPONSES:","NEXT STEPS:"]
    H2 = ["COLD EMAIL:","TALK TRACK:","LINKEDIN MESSAGES:"]
    H3 = ["COMPETITOR BATTLE CARD:","EMAIL SEQUENCE:"]

    part1, part2, part3 = await asyncio.gather(
        ask_ai(f"""
Selling: {product} | Target: {company_name} ({sector or 'industrial'})
Source data: {web_overview[:700]} {web_news[:300]}
Salesperson: {sp}
Champion: {champion} | Economic Buyer: {dm} | Gatekeeper: {gatekeeper}
Corporate HQ: {hq_location} | Site Locations: {site_location}
{f'Buying note: {buying_note}' if buying_note else ''}

Write using EXACTLY these headers on their own line:

COMPANY PROFILE:
4-5 sentences on what matters for selling {product} to {company_name}.
Only include facts from the source data. Use "Data Not Publicly Available" for unverified figures.

HQ VS SITE:
One paragraph: where is the HQ vs where operations happen. Which location to target for {product}.

BUYING COMMITTEE:
Champion — {champion}: [specific pain with {product}]
Economic Buyer — {dm}: [ROI/budget concern]
Technical Gatekeeper — {gatekeeper}: [integration/reliability concern]

OPENING LINE:
ONE sentence referencing a SPECIFIC verifiable fact from the source data. Connect to {product} pain point.

DISCOVERY QUESTIONS:
1. [Uncovers current solution or gap — specific to {company_name}]
2. [Uncovers budget authority and timeline]
3. [Uncovers technical requirements]

OBJECTIONS AND RESPONSES:
Objection: [Most likely from {dm}]
Response: [Sharp, specific]
Objection: [Second objection]
Response: [Sharp response]
Objection: [Third objection]
Response: [Sharp response]

NEXT STEPS:
1. [Specific action with named title at {company_name}]
2. [Specific action]
3. [Specific action]
"""),
        ask_ai(f"""
Selling: {product} | Target: {company_name} | Salesperson: {sp}
Contact: {dm} | Context: {web_overview[:400]} | Pain: {strategy.get('pain_points', sc['pain_points'])}

COLD EMAIL:
Subject: [specific subject]
[Opening tied to verifiable fact about {company_name}]
[2 sentences connecting pain to {product}]
[One CTA — 15 min call]
{user_name}
{user_role if user_role else ''}

TALK TRACK:
Hook: [one sentence about {company_name} — reference real data]
Who I am: [{sp} — one sentence]
Why you: [tied to their actual situation]
Permission: [2 minutes?]
Question: [opens their need for {product}]

LINKEDIN MESSAGES:
1. CONNECTION REQUEST (under 300 chars):
[specific to {company_name}]
2. FOLLOW UP (under 500 chars):
[references real {company_name} data]
"""),
        ask_ai(f"""
Selling: {product} | Target: {company_name} ({sector or 'industrial'})
Known incumbents: {incumbent}
Context: {web_overview[:400]} | Procurement: {web_procurement[:300]}

COMPETITOR BATTLE CARD:
Likely incumbent: [most likely current solution at {company_name} — or 'Data Not Publicly Available']
Displacement angle: [single strongest reason {product} wins at {company_name} specifically]
Weakness 1: [incumbent weakness relevant to {company_name}]
Your angle: [how {product} addresses this]
Weakness 2: [second weakness]
Your angle: [response]
Weakness 3: [third weakness]
Your angle: [response]

EMAIL SEQUENCE:
EMAIL 1 - Day 1:
Subject: [subject]
[2-3 sentences referencing real {company_name} data]

EMAIL 2 - Day 3:
Subject: [subject]
[2-3 sentences adding value]

EMAIL 3 - Day 7:
Subject: [subject]
[different angle — address likely objection]

EMAIL 4 - Day 14:
Subject: [subject]
[social proof or case study angle]

EMAIL 5 - Day 21:
Subject: [subject]
[breakup email — 2 sentences]
"""),
    )

    profile           = extract_section(part1, "COMPANY PROFILE:",         H1)
    hq_vs_site        = extract_section(part1, "HQ VS SITE:",              H1)
    buying_committee  = extract_section(part1, "BUYING COMMITTEE:",        H1)
    opener            = extract_section(part1, "OPENING LINE:",            H1)
    questions         = extract_section(part1, "DISCOVERY QUESTIONS:",     H1)
    objections        = extract_section(part1, "OBJECTIONS AND RESPONSES:",H1)
    next_steps        = extract_section(part1, "NEXT STEPS:",              H1)
    email             = extract_section(part2, "COLD EMAIL:",              H2)
    talk_track        = extract_section(part2, "TALK TRACK:",              H2)
    linkedin          = extract_section(part2, "LINKEDIN MESSAGES:",       H2)
    competitor_battle = extract_section(part3, "COMPETITOR BATTLE CARD:",  H3)
    email_sequence    = extract_section(part3, "EMAIL SEQUENCE:",          H3)

    if hq_vs_site or buying_committee:
        profile = (profile
            + (f"\n\n**HQ vs Site:** {hq_vs_site}" if hq_vs_site else "")
            + (f"\n\n**Buying Committee:**\n{buying_committee}" if buying_committee else ""))

    total = (datetime.now() - t0).total_seconds()
    print(f"  Total: {total:.1f}s")

    result = {
        "company": company_name, "user_name": user_name, "user_role": user_role,
        "product": product, "sector": sector,
        "scores": scores, "fit_check": fit_check, "signals": signals,
        "contacts": contacts,
        "profile": profile, "opener": opener, "questions": questions,
        "objections": objections, "next_steps": next_steps,
        "email": email, "talk_track": talk_track, "linkedin": linkedin,
        "competitor_battle": competitor_battle, "email_sequence": email_sequence,
        "notes": "", "created_at": created_at.isoformat(),
        "site_location": site_location, "hq_location": hq_location,
    }

    result["id"] = await _save_lead(ctx, result, fit_check, signals, scores, sector, site_location, hq_location)

    async with tenant_conn(ctx.org_id) as conn:
        updated = await conn.fetchrow("SELECT report_count, report_limit FROM users WHERE id=$1", ctx.user_id)
    result["reports_remaining"] = (updated["report_limit"] - updated["report_count"]) if updated else 0

    return result


def _build_empty_result(company, user_name, user_role, product, sector, fit_check, created_at):
    return {"error": False, "company": company, "user_name": user_name, "user_role": user_role,
            "product": product, "sector": sector,
            "scores": {"deal_readiness":0,"need_score":0,"budget_score":0,"decision_speed":0,"overall":0},
            "fit_check": fit_check, "signals": {},
            "profile":"","opener":"","questions":"","objections":"","next_steps":"",
            "email":"","talk_track":"","linkedin":"","competitor_battle":"","email_sequence":"",
            "notes":"","created_at":created_at.isoformat(),"site_location":"","hq_location":""}


async def _save_lead(ctx, result, fit_check, signals, scores, sector, site_location="", hq_location=""):
    try:
        async with tenant_conn(ctx.org_id) as conn:
            lid = await conn.fetchval("""
                INSERT INTO leads (
                    organization_id, user_id, company, user_name, user_role, product,
                    scores, fit_check, signals,
                    profile, opener, questions, objections, next_steps,
                    email, talk_track, linkedin, competitor_battle,
                    email_sequence, notes, sector, site_location, hq_location
                ) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb,$9::jsonb,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23)
                RETURNING id
            """, ctx.org_id, ctx.user_id,
                result["company"], result["user_name"], result["user_role"], result["product"],
                json.dumps(scores), json.dumps(fit_check), json.dumps(signals),
                result.get("profile",""), result.get("opener",""), result.get("questions",""),
                result.get("objections",""), result.get("next_steps",""), result.get("email",""),
                result.get("talk_track",""), result.get("linkedin",""), result.get("competitor_battle",""),
                result.get("email_sequence",""), "", sector, site_location, hq_location)
            await conn.execute("UPDATE users SET report_count = report_count + 1 WHERE id=$1", ctx.user_id)
        print(f"  Lead saved: ID {lid}")
        return lid
    except Exception as e:
        print(f"  DB error: {e}")
        return None


# ============================================================
# DEBRIEF & OBJECTION
# ============================================================

@app.post("/debrief")
async def debrief(body: DebriefRequest, ctx: TenantContext = Depends(get_tenant)):
    sp = f"{body.name}{f', {body.role}' if body.role else ''}"
    follow_up, next_action, score_data = await asyncio.gather(
        ask_ai(f"{sp} had a call with {body.company} about {body.product}.\nNotes: {body.notes}\nWrite a follow-up email under 150 words. Format: Subject: [subject]\n[body]\n{body.name}"),
        ask_ai(f"Call notes from {sp} with {body.company}: {body.notes}\nBest single next action to move deal forward? One sentence."),
        ask_ai_json(f"Call notes: {body.notes}\nCompany: {body.company}\nReturn ONLY raw JSON:\n{{\"score\":<0-100>,\"status\":\"<Hot|Warm|Cold>\",\"reasoning\":\"<one sentence>\"}}")
    )
    return {"follow_up": follow_up, "next_action": next_action, "score_data": score_data or {"score":50,"status":"Warm","reasoning":"Call completed"}}

@app.post("/objection")
async def objection(body: ObjectionRequest, ctx: TenantContext = Depends(get_tenant)):
    response = await ask_ai(f"Live call. Company: {body.company} | Product: {body.product}\nProfile: {body.profile[:400]}\nProspect said: \"{body.objection}\"\nGive one sharp response RIGHT NOW. 2-3 sentences. Acknowledge first.")
    return {"response": response}


# ============================================================
# TEAM ANALYTICS (feature-flagged)
# ============================================================

@app.get("/analytics/team")
async def team_analytics(ctx: TenantContext = Depends(get_tenant)):
    await require_feature("team_analytics", ctx)
    async with tenant_conn(ctx.org_id) as conn:
        member_rows = await conn.fetch("""
            SELECT u.id AS user_id, u.email, u.role,
                   COUNT(l.id) AS lead_count,
                   AVG((l.scores->>'overall')::numeric) AS avg_score,
                   MAX(l.created_at) AS last_active
            FROM users u LEFT JOIN leads l ON l.user_id = u.id
            WHERE u.organization_id = $1
            GROUP BY u.id, u.email, u.role
            ORDER BY lead_count DESC
        """, ctx.org_id)
        activity_rows = await conn.fetch("""
            SELECT l.id, l.company, l.sector, l.created_at,
                   (l.scores->>'overall')::numeric AS score,
                   u.email AS user_email, u.id AS user_id
            FROM leads l JOIN users u ON l.user_id = u.id
            WHERE l.organization_id = $1
            ORDER BY l.created_at DESC LIMIT 10
        """, ctx.org_id)
        org_stats = await conn.fetchrow("""
            SELECT COUNT(l.id) AS total_leads,
                   AVG((l.scores->>'overall')::numeric) AS avg_score,
                   COUNT(DISTINCT CASE WHEN l.created_at >= NOW() - INTERVAL '7 days'
                         THEN l.user_id END) AS active_members
            FROM leads l WHERE l.organization_id = $1
        """, ctx.org_id)
    members = []
    for r in member_rows:
        d = dict(r)
        if d.get("last_active"): d["last_active"] = d["last_active"].isoformat()
        d["avg_score"]  = round(float(d["avg_score"]), 1) if d.get("avg_score") else 0.0
        d["lead_count"] = int(d["lead_count"])
        members.append(d)
    top_user = members[0]["email"] if members and members[0]["lead_count"] > 0 else None
    activity = []
    for r in activity_rows:
        activity.append({
            "lead_id":    r["id"],
            "company":    r["company"],
            "sector":     r["sector"] or "",
            "score":      int(r["score"]) if r["score"] else 0,
            "user_email": r["user_email"],
            "user_id":    r["user_id"],
            "created_at": r["created_at"].isoformat(),
        })
    return {
        "members": members,
        "org": {
            "total_leads":    int(org_stats["total_leads"] or 0),
            "avg_score":      round(float(org_stats["avg_score"]), 1) if org_stats["avg_score"] else 0.0,
            "top_user":       top_user,
            "active_members": int(org_stats["active_members"] or 0),
        },
        "activity": activity,
    }


# ============================================================
# ADMIN
# ============================================================

def _check_admin(request: Request):
    if request.headers.get("X-Admin-Token","") != ADMIN_TOKEN:
        raise HTTPException(403, "Admin access required")

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page():
    return HTMLResponse("<h1>LeadLens Admin — use API endpoints with X-Admin-Token header</h1>")

@app.post("/admin/create-org")
async def admin_create_org(request: Request):
    _check_admin(request)
    data = await request.json()
    name = data.get("name","").strip()
    if not name: raise HTTPException(400, "org name required")
    slug = name.lower().replace(" ","-")
    plan = data.get("plan_tier", "solo")
    limit = int(data.get("report_limit_per_user", REPORT_LIMIT))
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            org_id = await conn.fetchval(
                "INSERT INTO organizations (name, slug, plan_tier, report_limit_per_user) VALUES ($1,$2,$3,$4) RETURNING id",
                name, slug, plan, limit
            )
            code = "-".join([secrets.token_hex(3).upper() for _ in range(2)])
            await conn.execute("INSERT INTO invitation_codes (organization_id, code) VALUES ($1,$2)", org_id, code)
    return {"org_id": org_id, "slug": slug, "first_invite_code": code}

@app.post("/admin/generate-code")
async def admin_generate_code(request: Request):
    _check_admin(request)
    data = await request.json()
    org_id = data.get("org_id")
    if not org_id: raise HTTPException(400, "org_id required")
    code = "-".join([secrets.token_hex(3).upper() for _ in range(2)])
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO invitation_codes (organization_id, code) VALUES ($1,$2)", org_id, code)
    return {"code": code}

@app.post("/admin/grant-reports")
async def admin_grant_reports(request: Request):
    _check_admin(request)
    data = await request.json()
    user_id = data.get("user_id")
    if not user_id: raise HTTPException(400, "user_id required")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET report_limit = report_limit + $1 WHERE id=$2", data.get("amount",10), user_id)
    return {"success": True}


# ── Sprint 3: Admin Panel UI routes ────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request):
    # Token checked client-side; just serve the HTML
    return templates.TemplateResponse("admin.html", {"request": request})

@app.get("/admin/api/stats")
async def admin_api_stats(request: Request):
    _check_admin(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        total_orgs   = await conn.fetchval("SELECT COUNT(*) FROM organizations")
        total_users  = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_leads  = await conn.fetchval("SELECT COUNT(*) FROM leads")
        active_today = await conn.fetchval(
            "SELECT COUNT(DISTINCT user_id) FROM leads WHERE created_at >= CURRENT_DATE"
        )
    return {"total_orgs": total_orgs, "total_users": total_users,
            "total_leads": total_leads, "active_today": active_today}

@app.get("/admin/api/orgs")
async def admin_api_orgs(request: Request):
    _check_admin(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT o.id, o.name, o.slug, o.plan_tier, o.features,
                   COUNT(DISTINCT u.id)  AS user_count,
                   COUNT(DISTINCT l.id)  AS lead_count
            FROM organizations o
            LEFT JOIN users u ON u.organization_id = o.id
            LEFT JOIN leads l ON l.organization_id = o.id
            GROUP BY o.id ORDER BY o.id
        """)
    result = []
    for r in rows:
        d = dict(r)
        f = d.get("features")
        if isinstance(f, str):
            try:    d["features"] = json.loads(f)
            except: d["features"] = {}
        elif not isinstance(f, dict):
            d["features"] = {}
        result.append(d)
    return result

@app.post("/admin/api/orgs/{org_id}/features")
async def admin_api_update_features(org_id: int, body: AdminFeatureRequest, request: Request):
    _check_admin(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE organizations SET features = features || $1::jsonb WHERE id = $2",
            json.dumps({body.feature: body.enabled}), org_id
        )
    return {"success": True}

@app.post("/admin/api/orgs/{org_id}/plan")
async def admin_api_update_plan(org_id: int, body: AdminPlanRequest, request: Request):
    _check_admin(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE organizations SET plan_tier = $1 WHERE id = $2", body.plan_tier, org_id)
    return {"success": True}

@app.get("/admin/api/users")
async def admin_api_users(request: Request):
    _check_admin(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT u.id, u.email, u.role, u.report_count, u.report_limit, u.is_active,
                   o.name AS org_name
            FROM users u
            JOIN organizations o ON o.id = u.organization_id
            ORDER BY u.id
        """)
    return [dict(r) for r in rows]

@app.post("/admin/api/users/{user_id}/grant")
async def admin_api_grant_user(user_id: int, body: AdminGrantRequest, request: Request):
    _check_admin(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET report_limit = report_limit + $1 WHERE id = $2",
            body.amount, user_id
        )
    return {"success": True}

@app.get("/admin/api/codes")
async def admin_api_codes(request: Request):
    _check_admin(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT ic.id, ic.code, ic.used, ic.used_by, ic.created_at,
                   o.name AS org_name
            FROM invitation_codes ic
            JOIN organizations o ON o.id = ic.organization_id
            ORDER BY ic.id DESC
        """)
    result = []
    for r in rows:
        d = dict(r)
        if d.get("created_at"): d["created_at"] = d["created_at"].isoformat()
        result.append(d)
    return result

@app.post("/admin/api/codes/generate")
async def admin_api_generate_code(body: AdminCodeRequest, request: Request):
    _check_admin(request)
    code = "-".join([secrets.token_hex(3).upper() for _ in range(2)])
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO invitation_codes (organization_id, code) VALUES ($1,$2)",
            body.org_id, code
        )
    return {"code": code}

@app.get("/admin/api/requests")
async def admin_api_requests(request: Request):
    _check_admin(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT ar.id, ar.email, ar.status, ar.created_at, ar.user_id,
                   o.name AS org_name
            FROM access_requests ar
            JOIN organizations o ON o.id = ar.organization_id
            ORDER BY ar.id DESC
        """)
    result = []
    for r in rows:
        d = dict(r)
        if d.get("created_at"): d["created_at"] = d["created_at"].isoformat()
        result.append(d)
    return result

@app.post("/admin/api/requests/{request_id}/approve")
async def admin_api_approve_request(request_id: int, request: Request):
    _check_admin(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM access_requests WHERE id = $1", request_id)
        if not row:
            raise HTTPException(404, "Request not found")
        async with conn.transaction():
            await conn.execute(
                "UPDATE access_requests SET status = 'approved' WHERE id = $1", request_id
            )
            await conn.execute(
                "UPDATE users SET report_limit = report_limit + 10 WHERE id = $1", row["user_id"]
            )
    return {"success": True}

@app.post("/admin/api/requests/{request_id}/deny")
async def admin_api_deny_request(request_id: int, request: Request):
    _check_admin(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE access_requests SET status = 'denied' WHERE id = $1", request_id
        )
    return {"success": True}