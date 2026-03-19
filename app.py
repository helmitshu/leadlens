import os
import json
import time
import secrets
import anthropic
import requests
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_cors import CORS
from dotenv import load_dotenv
from tavily import TavilyClient
import bcrypt

load_dotenv()

app = Flask(__name__)
CORS(app)
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(32))

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TAVILY_API_KEY    = os.getenv("TAVILY_API_KEY")
ADMIN_PASSWORD    = os.getenv("ADMIN_PASSWORD", "changeme123")
DATABASE_URL      = os.getenv("DATABASE_URL")   # Set by Render automatically
MODEL             = "claude-sonnet-4-5"
REPORT_LIMIT      = 10

if not ANTHROPIC_API_KEY or not TAVILY_API_KEY:
    print("WARNING: API keys missing. Check your .env file.")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
tavily = TavilyClient(api_key=TAVILY_API_KEY)

# Groq client for lightweight AI tasks (free)
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL   = "llama-3.3-70b-versatile"

def ask_groq(prompt):
    try:
        r = requests.post(GROQ_URL, headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }, json={
            "model": GROQ_MODEL,
            "messages": [{"role":"user","content":prompt}],
            "temperature": 0.4,
            "max_tokens": 300
        }, timeout=15)
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print("Groq error:", e)
        return None

# ──────────────────────────────────────────────
# DATABASE — SQLite locally, PostgreSQL on Render
# ──────────────────────────────────────────────

USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    # Render gives DATABASE_URL starting with postgres:// but psycopg2 needs postgresql://
    DB_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    print("Using PostgreSQL database")
else:
    import sqlite3
    print("Using SQLite database")

def get_db():
    if USE_POSTGRES:
        conn = psycopg2.connect(DB_URL)
        conn.autocommit = False
        return conn
    else:
        conn = sqlite3.connect("leadlens.db")
        conn.row_factory = sqlite3.Row
        return conn

def db_execute(conn, sql, params=()):
    """Execute a query — handles both SQLite and PostgreSQL placeholder differences."""
    if USE_POSTGRES:
        # PostgreSQL uses %s placeholders, SQLite uses ?
        sql = sql.replace("?", "%s")
        # PostgreSQL uses SERIAL not AUTOINCREMENT
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        cur = conn.cursor()
    cur.execute(sql, params)
    return cur

def fetchall(cur):
    rows = cur.fetchall()
    if USE_POSTGRES:
        return [dict(r) for r in rows]
    else:
        return [dict(r) for r in rows]

def fetchone(cur):
    row = cur.fetchone()
    if row is None:
        return None
    if USE_POSTGRES:
        return dict(row)
    else:
        return dict(row)

def last_insert_id(conn, cur, table="leads"):
    if USE_POSTGRES:
        cur2 = conn.cursor()
        cur2.execute(f"SELECT lastval()")
        return cur2.fetchone()[0]
    else:
        return cur.lastrowid

def init_db():
    conn = get_db()
    if USE_POSTGRES:
        cur = conn.cursor()
        serial = "SERIAL PRIMARY KEY"
        text_pk = "SERIAL PRIMARY KEY"
    else:
        cur = conn.cursor()
        serial = "INTEGER PRIMARY KEY AUTOINCREMENT"
        text_pk = "INTEGER PRIMARY KEY AUTOINCREMENT"

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS users (
            id             {serial},
            email          TEXT    NOT NULL UNIQUE,
            password_hash  TEXT    NOT NULL,
            invite_code    TEXT    NOT NULL,
            report_count   INTEGER NOT NULL DEFAULT 0,
            report_limit   INTEGER NOT NULL DEFAULT 10,
            created_at     TEXT    NOT NULL
        )
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS invitation_codes (
            id         {serial},
            code       TEXT    NOT NULL UNIQUE,
            used       INTEGER NOT NULL DEFAULT 0,
            used_by    TEXT    DEFAULT NULL,
            created_at TEXT    NOT NULL
        )
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS early_access (
            id         {serial},
            email      TEXT    NOT NULL UNIQUE,
            created_at TEXT    NOT NULL
        )
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS access_requests (
            id          {serial},
            user_id     INTEGER NOT NULL,
            email       TEXT    NOT NULL,
            status      TEXT    NOT NULL DEFAULT 'pending',
            created_at  TEXT    NOT NULL,
            resolved_at TEXT    DEFAULT NULL
        )
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS leads (
            id               {serial},
            user_id          INTEGER NOT NULL DEFAULT 0,
            company          TEXT    NOT NULL,
            user_name        TEXT,
            user_role        TEXT    DEFAULT '',
            product          TEXT,
            scores           TEXT,
            fit_check        TEXT,
            signals          TEXT,
            profile          TEXT,
            opener           TEXT,
            questions        TEXT,
            objections       TEXT,
            next_steps       TEXT,
            email            TEXT,
            talk_track       TEXT,
            linkedin         TEXT,
            competitor_battle TEXT,
            email_sequence   TEXT,
            notes            TEXT    DEFAULT '',
            created_at       TEXT
        )
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS reminders (
            id           {serial},
            user_id      INTEGER NOT NULL,
            lead_id      INTEGER DEFAULT NULL,
            company      TEXT    DEFAULT '',
            note         TEXT    NOT NULL,
            date         TEXT    NOT NULL,
            ai_suggested INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT    NOT NULL
        )
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS lead_notes (
            id         {serial},
            lead_id    INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            note       TEXT    NOT NULL,
            created_at TEXT    NOT NULL
        )
    """)

    # SQLite-only migrations
    if not USE_POSTGRES:
        try:
            cur.execute("ALTER TABLE leads ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0")
        except: pass
        try:
            cur.execute("ALTER TABLE leads ADD COLUMN user_role TEXT DEFAULT ''")
        except: pass

    conn.commit()
    conn.close()

init_db()

# ── DB WRAPPER — makes conn.execute() work the same for SQLite and PostgreSQL ──

class DBCursor:
    def __init__(self, cur, is_pg):
        self._cur = cur; self._is_pg = is_pg
    def fetchone(self):
        row = self._cur.fetchone()
        return dict(row) if row else None
    def fetchall(self):
        return [dict(r) for r in self._cur.fetchall()]
    @property
    def lastrowid(self):
        if self._is_pg:
            self._cur.execute("SELECT lastval()"); return self._cur.fetchone()[0]
        return self._cur.lastrowid

class DBConn:
    def __init__(self, conn, is_pg):
        self._conn = conn; self._is_pg = is_pg
    def execute(self, sql, params=()):
        if self._is_pg:
            sql = sql.replace("?", "%s")
            cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            cur = self._conn.cursor()
        cur.execute(sql, params)
        return DBCursor(cur, self._is_pg)
    def commit(self): self._conn.commit()
    def close(self):  self._conn.close()

def get_db():
    if USE_POSTGRES:
        conn = psycopg2.connect(DB_URL)
        return DBConn(conn, is_pg=True)
    else:
        import sqlite3 as _sq
        conn = _sq.connect("leadlens.db")
        conn.row_factory = _sq.Row
        return DBConn(conn, is_pg=False)


# ──────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect("/admin/login")
        return f(*args, **kwargs)
    return decorated

def get_current_user():
    if "user_id" not in session:
        return None
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE id = ?", (session["user_id"],)
    ).fetchone()
    conn.close()
    return user

def hash_password(password):
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def check_password(password, hashed):
    return bcrypt.checkpw(password.encode(), hashed.encode())

# ──────────────────────────────────────────────
# PUBLIC ROUTES
# ──────────────────────────────────────────────

@app.route("/")
def landing():
    return render_template("landing.html")

@app.route("/register", methods=["POST"])
def register():
    data     = request.json or {}
    email    = data.get("email", "").strip().lower()
    password = data.get("password", "")
    code     = data.get("invitation_code", "").strip().upper()

    if not email or not password or not code:
        return jsonify({"error": "All fields are required."}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400

    conn = get_db()

    # Check invite code
    invite = conn.execute(
        "SELECT * FROM invitation_codes WHERE code = ?", (code,)
    ).fetchone()
    if not invite:
        conn.close()
        return jsonify({"error": "Invalid invitation code."}), 400
    if invite["used"]:
        conn.close()
        return jsonify({"error": "This invitation code has already been used."}), 400

    # Check email not taken
    existing = conn.execute(
        "SELECT id FROM users WHERE email = ?", (email,)
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({"error": "An account with this email already exists."}), 400

    # Create user
    pw_hash    = hash_password(password)
    created_at = datetime.now().strftime("%b %d, %Y %I:%M %p")

    conn.execute("""
        INSERT INTO users (email, password_hash, invite_code, report_count, report_limit, created_at)
        VALUES (?, ?, ?, 0, ?, ?)
    """, (email, pw_hash, code, REPORT_LIMIT, created_at))

    # Mark invite as used
    conn.execute("""
        UPDATE invitation_codes SET used = 1, used_by = ? WHERE code = ?
    """, (email, code))

    conn.commit()

    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()

    session["user_id"] = user["id"]
    session["user_email"] = user["email"]
    return jsonify({"success": True}), 200

@app.route("/login", methods=["POST"])
def login():
    data     = request.json or {}
    email    = data.get("email", "").strip().lower()
    password = data.get("password", "")

    if not email or not password:
        return jsonify({"error": "Email and password are required."}), 400

    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE email = ?", (email,)
    ).fetchone()
    conn.close()

    if not user or not check_password(password, user["password_hash"]):
        return jsonify({"error": "Invalid email or password."}), 401

    session["user_id"]    = user["id"]
    session["user_email"] = user["email"]
    return jsonify({"success": True}), 200

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/early-access", methods=["POST"])
def early_access():
    email = (request.json or {}).get("email", "").strip().lower()
    if not email:
        return jsonify({"error": "Email required"}), 400
    try:
        conn = get_db()
        conn.execute(
            "INSERT OR IGNORE INTO early_access (email, created_at) VALUES (?, ?)",
            (email, datetime.now().strftime("%b %d, %Y %I:%M %p"))
        )
        conn.commit()
        conn.close()
    except:
        pass
    return jsonify({"success": True})

# ──────────────────────────────────────────────
# DASHBOARD
# ──────────────────────────────────────────────

@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect("/")
    return render_template("index.html")

# ──────────────────────────────────────────────
# APP API — LEADS
# ──────────────────────────────────────────────

@app.route("/leads", methods=["GET"])
@login_required
def get_leads():
    try:
        conn  = get_db()
        leads = conn.execute(
            "SELECT * FROM leads WHERE user_id = ? ORDER BY created_at DESC",
            (session["user_id"],)
        ).fetchall()
        conn.close()
        result = []
        for lead in leads:
            d = dict(lead)
            for field in ["scores", "fit_check", "signals"]:
                try:
                    d[field] = json.loads(d[field]) if d[field] else {}
                except:
                    d[field] = {}
            result.append(d)
        return jsonify(result)
    except Exception as e:
        print("get_leads error:", e)
        return jsonify([])

@app.route("/leads/<int:lead_id>", methods=["DELETE"])
@login_required
def delete_lead(lead_id):
    try:
        conn = get_db()
        conn.execute(
            "DELETE FROM leads WHERE id = ? AND user_id = ?",
            (lead_id, session["user_id"])
        )
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except:
        return jsonify({"success": False})

@app.route("/leads/<int:lead_id>/notes", methods=["GET"])
@login_required
def get_notes(lead_id):
    try:
        conn = get_db()
        entries = conn.execute(
            "SELECT * FROM lead_notes WHERE lead_id=? AND user_id=? ORDER BY id DESC",
            (lead_id, session["user_id"])
        ).fetchall()
        conn.close()
        return jsonify([dict(e) for e in entries])
    except Exception as e:
        print("get_notes error:", e)
        return jsonify([])

@app.route("/leads/<int:lead_id>/notes/add", methods=["POST"])
@login_required
def add_note(lead_id):
    note = (request.json or {}).get("note", "").strip()
    if not note:
        return jsonify({"error": "Note is empty"}), 400
    created_at = datetime.now().strftime("%b %d, %Y %I:%M %p")
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO lead_notes (lead_id, user_id, note, created_at) VALUES (?,?,?,?)",
            (lead_id, session["user_id"], note, created_at)
        )
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        print("add_note error:", e)
        return jsonify({"error": str(e)}), 500
    try:
        notes = request.json.get("notes", "")
        conn  = get_db()
        conn.execute(
            "UPDATE leads SET notes = ? WHERE id = ? AND user_id = ?",
            (notes, lead_id, session["user_id"])
        )
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except:
        return jsonify({"success": False})

# ──────────────────────────────────────────────
# USAGE — CHECK & REQUEST MORE
# ──────────────────────────────────────────────

@app.route("/usage", methods=["GET"])
@login_required
def get_usage():
    user = get_current_user()
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify({
        "email":        user["email"],
        "report_count": user["report_count"],
        "report_limit": user["report_limit"],
        "remaining":    user["report_limit"] - user["report_count"]
    })

@app.route("/request-access", methods=["POST"])
@login_required
def request_access():
    user = get_current_user()
    if not user:
        return jsonify({"error": "User not found"}), 404

    conn = get_db()
    # Check if already has a pending request
    existing = conn.execute(
        "SELECT id FROM access_requests WHERE user_id = ? AND status = 'pending'",
        (user["id"],)
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({"success": True, "already_requested": True})

    conn.execute("""
        INSERT INTO access_requests (user_id, email, status, created_at)
        VALUES (?, ?, 'pending', ?)
    """, (user["id"], user["email"], datetime.now().strftime("%b %d, %Y %I:%M %p")))
    conn.commit()
    conn.close()
    return jsonify({"success": True, "already_requested": False})

# ──────────────────────────────────────────────
# AI HELPERS
# ──────────────────────────────────────────────

def search_web(query):
    try:
        results = tavily.search(query=query, max_results=5)
        content = ""
        for r in results["results"]:
            content += f"Source: {r['url']}\n{r['content']}\n\n"
        return content
    except Exception as e:
        print(f"search_web error: {e}")
        return ""

def ask_ai(prompt, retries=3):
    for attempt in range(retries):
        try:
            message = claude.messages.create(
                model=MODEL,
                max_tokens=2048,
                system="You are a senior sales intelligence analyst. Never mention AI, language models, or that you are an assistant. Always respond as a human expert analyst would. Be specific, direct, and actionable.",
                messages=[{"role": "user", "content": prompt}]
            )
            return message.content[0].text
        except Exception as e:
            print(f"ask_ai exception (attempt {attempt+1}): {e}")
            if attempt < retries - 1:
                time.sleep(2)
    return ""

def ask_ai_json(prompt, retries=3):
    for attempt in range(retries):
        try:
            message = claude.messages.create(
                model=MODEL,
                max_tokens=1024,
                system="You are a senior sales intelligence analyst. Never mention AI or language models. Respond only in valid JSON format. No markdown, no backticks, no explanation. Just the raw JSON object.",
                messages=[{"role": "user", "content": prompt}]
            )
            text  = message.content[0].text.strip()
            text  = text.replace("```json", "").replace("```", "").strip()
            start = text.find("{")
            end   = text.rfind("}") + 1
            if start == -1 or end == 0:
                if attempt < retries - 1:
                    time.sleep(2)
                continue
            return json.loads(text[start:end])
        except Exception as e:
            print(f"ask_ai_json exception (attempt {attempt+1}): {e}")
            if attempt < retries - 1:
                time.sleep(2)
    return {}

def extract_section(text, header, all_headers):
    try:
        start = text.find(header)
        if start == -1:
            return ""
        start += len(header)
        end    = len(text)
        for h in all_headers:
            if h != header:
                pos = text.find(h, start)
                if pos != -1 and pos < end:
                    end = pos
        return text[start:end].strip()
    except:
        return ""

# ──────────────────────────────────────────────
# RESEARCH ROUTE
# ──────────────────────────────────────────────

@app.route("/research", methods=["POST"])
@login_required
def research():
    # ── Check report limit ──
    user = get_current_user()
    if not user:
        return jsonify({"error": True, "message": "Session expired. Please log in again."}), 401

    if user["report_count"] >= user["report_limit"]:
        return jsonify({
            "error":        True,
            "limit_reached": True,
            "message":      f"You've used all {user['report_limit']} of your demo reports.",
        }), 403

    data         = request.json
    company_name = data.get("company", "").strip()
    user_name    = data.get("name",    "").strip()
    user_role    = data.get("role",    "").strip()
    product      = data.get("product", "").strip()

    if not company_name or not user_name or not product:
        return jsonify({
            "error":   True,
            "message": "Please fill in your name, what you sell, and a company name"
        })

    print(f"\n--- Researching: {company_name} | Product: {product} | User: {user['email']} ---")

    # ── Search strategy ──
    search_strategy = ask_ai_json(f"""
You are a sales research expert.
A salesperson sells: {product}
They want to research: {company_name}

What specific information would help them sell {product} to {company_name}?

Return ONLY raw JSON:
{{
    "search_query_1": "<search to understand company size employees offices and operations>",
    "search_query_2": "<search to find signals that indicate need for {product}>",
    "search_query_3": "<search to find procurement or buying process for {product}>",
    "key_facts_needed": "<one sentence: the 3 most important things to know about this company for selling {product}>",
    "pain_points": "<one sentence: what pain points would make {company_name} buy {product} right now>"
}}
""")

    if not search_strategy:
        search_strategy = {
            "search_query_1": f"{company_name} company employees offices size",
            "search_query_2": f"{company_name} news growth 2025",
            "search_query_3": f"{company_name} procurement purchasing",
            "key_facts_needed": f"Company size, growth stage, and decision maker for {product}",
            "pain_points":      f"Need for {product} based on company operations"
        }

    q1 = search_strategy.get("search_query_1", f"{company_name} company overview")
    q2 = search_strategy.get("search_query_2", f"{company_name} news 2025")
    q3 = search_strategy.get("search_query_3", f"{company_name} procurement")

    web_data = search_web(f"{company_name} {q1}");  time.sleep(0.5)
    web_news = search_web(f"{company_name} {q2}");  time.sleep(0.5)
    web_jobs = search_web(f"{company_name} {q3}")

    if not web_data or len(web_data.strip()) < 50:
        return jsonify({
            "error":   True,
            "message": f"Could not find information about '{company_name}'. Check the spelling or try adding more detail."
        })

    # ── Fit check ──
    fit_check = ask_ai_json(f"""
You are an experienced sales director with 20 years across all industries.

A salesperson wants to sell: {product}
To this company: {company_name}
Web data about this company: {web_data[:600]}

Step 1 - Is this a UNIVERSAL product?
Universal products every business with employees needs:
office supplies, paper, pens, stationery, printer ink
snacks, food, beverages, coffee, water, catering
cleaning supplies, janitorial services
software, computers, phones, internet, utilities
HR services, payroll, accounting, legal, insurance
furniture, office equipment, security systems
uniforms, workwear, safety equipment
marketing services, advertising, printing
vehicles, cars, company fleet transportation
If {product} is universal: set is_fit=true, confidence=90, warning=""

Step 2 - If NOT universal, does this specific company need it?
Only mark is_fit=false if the product is highly specialized
and this company clearly has absolutely no use for it.

Step 3 - Is this the WRONG company?
Did the web search return a completely different business than intended?

Return ONLY raw JSON:
{{
    "is_fit": <true or false>,
    "confidence": <0-100>,
    "product_category": "<one phrase describing this product type>",
    "reasoning": "<one sentence explaining your decision>",
    "warning": "<if not fit: specific reason and better target. if fit: empty string>",
    "right_company": "<if wrong company found: suggest correct search. otherwise: empty string>",
    "decision_maker": "<exact job title at {company_name} who would buy {product}>"
}}
""")

    if not fit_check:
        fit_check = {
            "is_fit": True, "confidence": 70,
            "product_category": product, "reasoning": "Proceeding with research.",
            "warning": "", "right_company": "", "decision_maker": "Procurement Manager"
        }

    created_at = datetime.now().strftime("%b %d, %Y %I:%M %p")

    if not fit_check.get("is_fit", True):
        result = {
            "error": False, "company": company_name,
            "user_name": user_name, "user_role": user_role, "product": product,
            "scores": {"deal_readiness":0,"need_score":0,"budget_score":0,"decision_speed":0,"overall":0},
            "fit_check": fit_check, "signals": {},
            "profile":"","opener":"","questions":"","objections":"","next_steps":"",
            "email":"","talk_track":"","linkedin":"","competitor_battle":"","email_sequence":"",
            "notes":"","created_at":created_at
        }
        try:
            conn = get_db()
            conn.execute("""
                INSERT INTO leads (user_id,company,user_name,user_role,product,
                    scores,fit_check,signals,profile,opener,questions,objections,next_steps,
                    email,talk_track,linkedin,competitor_battle,email_sequence,notes,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                session["user_id"], company_name, user_name, user_role, product,
                json.dumps(result["scores"]), json.dumps(fit_check), json.dumps({}),
                "","","","","","","","","","","", created_at
            ))
            lead_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "UPDATE users SET report_count = report_count + 1 WHERE id = ?",
                (session["user_id"],)
            )
            conn.commit()
            conn.close()
            result["id"] = lead_id
        except Exception as e:
            print("DB error (fit fail):", e)
            result["id"] = None
        return jsonify(result)

    # ── Buying signals ──
    time.sleep(0.5)
    signal_funding    = search_web(f"{company_name} funding raised investment");    time.sleep(0.5)
    signal_leadership = search_web(f"{company_name} new CEO CFO VP director hired"); time.sleep(0.5)
    signal_expansion  = search_web(f"{company_name} expansion growth new office");   time.sleep(0.5)
    signal_contracts  = search_web(f"{company_name} new contract partnership deal")

    signals = ask_ai_json(f"""
You are a sales timing expert analyzing buying signals.
Company: {company_name}
Product: {product}

FUNDING DATA: {signal_funding[:400]}
LEADERSHIP CHANGES: {signal_leadership[:400]}
EXPANSION NEWS: {signal_expansion[:400]}
JOB POSTINGS: {web_jobs[:400]}
NEW CONTRACTS: {signal_contracts[:400]}

Return ONLY raw JSON:
{{
    "timing_score": <0-100>,
    "recommendation": "<one sentence: reach out now wait or avoid and why>",
    "funding":    {{"detected": <true/false>, "score": <0-10>, "detail": "<what found or No recent funding detected>"}},
    "leadership": {{"detected": <true/false>, "score": <0-10>, "detail": "<what found or No leadership changes detected>"}},
    "expansion":  {{"detected": <true/false>, "score": <0-10>, "detail": "<what found or No expansion signals detected>"}},
    "hiring":     {{"detected": <true/false>, "score": <0-10>, "detail": "<what found or No relevant hiring detected>"}},
    "contracts":  {{"detected": <true/false>, "score": <0-10>, "detail": "<what found or No new contracts detected>"}}
}}
""")

    if not signals:
        signals = {
            "timing_score": 50, "recommendation": "Research this company further before reaching out.",
            "funding":    {"detected":False,"score":0,"detail":"No recent funding detected"},
            "leadership": {"detected":False,"score":0,"detail":"No leadership changes detected"},
            "expansion":  {"detected":False,"score":0,"detail":"No expansion signals detected"},
            "hiring":     {"detected":False,"score":0,"detail":"No relevant hiring detected"},
            "contracts":  {"detected":False,"score":0,"detail":"No new contracts detected"}
        }

    # ── Scores ──
    scores = ask_ai_json(f"""
You are a sales intelligence analyst scoring a {product} opportunity.

COMPANY: {company_name}
PRODUCT: {product}
PRODUCT CATEGORY: {fit_check.get('product_category', product)}
FIT CONFIDENCE: {fit_check.get('confidence', 70)}%
KEY FACTS: {search_strategy.get('key_facts_needed', '')}
PAIN POINTS: {search_strategy.get('pain_points', '')}
WEB DATA: {web_data[:500]}
NEWS: {web_news[:300]}
BUYING SIGNALS:
  Funding: {signals.get('funding', {}).get('detail', 'none')}
  Leadership: {signals.get('leadership', {}).get('detail', 'none')}
  Expansion: {signals.get('expansion', {}).get('detail', 'none')}
  Hiring: {signals.get('hiring', {}).get('detail', 'none')}

DEAL READINESS — Start at 40. Adjust:
+ Fresh funding last 6 months → +25
+ Active relevant hiring → +15
+ New leadership → +15
+ Expansion/growth news → +15
+ Strong revenue → +10
- Layoffs/cost cutting → -25
- No signals → stay 40

NEED SCORE:
- Universal product + employees → 72-82
- Specialized + exact industry → 75-88
- Specialized + adjacent → 45-65

BUDGET SCORE:
- Series B+ or public → 75-88
- Series A or mid-size → 60-75
- Small under 50, no funding → 40-58
- Government/institution → 62-75
- No signals → 48-55
- Cost cutting/trouble → 20-38

DECISION SPEED:
- Startup <50 → 65-78
- Growing 50-200 → 50-65
- Mid-size 200-500 → 38-52
- Large 500+ → 22-42
- Government → 15-35

OVERALL = deal_readiness×0.25 + need_score×0.35 + budget_score×0.25 + decision_speed×0.15

Return ONLY raw JSON:
{{
    "deal_readiness": <0-100>,
    "need_score":     <0-100>,
    "budget_score":   <0-100>,
    "decision_speed": <0-100>,
    "overall":        <0-100>
}}
""")

    if not scores or "overall" not in scores:
        scores = {"deal_readiness":50,"need_score":55,"budget_score":50,"decision_speed":50,"overall":51}

    salesperson_intro = f"{user_name}{f', {user_role}' if user_role else ''}"

    all_headers_1 = ["COMPANY PROFILE:","OPENING LINE:","DISCOVERY QUESTIONS:","OBJECTIONS AND RESPONSES:","NEXT STEPS:"]
    all_headers_2 = ["COLD EMAIL:","TALK TRACK:","LINKEDIN MESSAGES:"]
    all_headers_3 = ["COMPETITOR BATTLE CARD:","EMAIL SEQUENCE:"]

    # ── Part 1 ──
    part1 = ask_ai(f"""
You are a senior sales intelligence analyst.
Company: {company_name}
Web data: {web_data[:600]}
News: {web_news[:300]}
Salesperson: {salesperson_intro} sells {product}
Decision maker: {fit_check.get('decision_maker', 'Procurement Manager')}
Key facts needed: {search_strategy.get('key_facts_needed', '')}
Pain points: {search_strategy.get('pain_points', '')}

Write all sections below using EXACTLY these headers on their own line.

COMPANY PROFILE:
Write 4-5 sentences focused specifically on what matters for selling {product} to {company_name}.
Include company size, office locations, growth trajectory, procurement hints, and signals that indicate need for {product}.

OPENING LINE:
Write ONE personalized opening sentence for a cold email that connects a real fact
about {company_name} directly to a pain point that {product} solves. One sentence only.

DISCOVERY QUESTIONS:
1. [Question that uncovers if they currently have a solution for {product}]
2. [Question that uncovers their pain point or budget for {product}]
3. [Question that uncovers the decision making process for buying {product}]

OBJECTIONS AND RESPONSES:
Objection: [most likely objection specific to buying {product}]
Response: [sharp confident response]
Objection: [second likely objection]
Response: [sharp confident response]
Objection: [third likely objection]
Response: [sharp confident response]

NEXT STEPS:
1. [Specific actionable next step for selling {product} to {company_name}]
2. [Specific actionable next step]
3. [Specific actionable next step]
""")

    # ── Part 2 ──
    part2 = ask_ai(f"""
You are a senior sales intelligence analyst.
Company: {company_name}
Salesperson: {salesperson_intro} sells {product}
Decision maker: {fit_check.get('decision_maker', 'Procurement Manager')}
Company context: {web_data[:400]}
Pain points: {search_strategy.get('pain_points', '')}

Write all sections below using EXACTLY these headers on their own line.

COLD EMAIL:
Subject: [specific subject line connecting {company_name} situation to {product}]

[Opening line tied to a real specific fact about {company_name}]

[2 sentences connecting their specific pain point to how {product} solves it]

[One clear call to action — 15 minute call]

{user_name}
{user_role if user_role else ''}

TALK TRACK:
[Hook — one sentence about {company_name} that grabs attention]
[Who you are — {salesperson_intro} one sentence]
[Why calling them specifically — tied to their actual business situation]
[Permission question — do they have 2 minutes?]
[Key opening question to get them talking about their need for {product}]

LINKEDIN MESSAGES:
1. CONNECTION REQUEST (under 300 characters specific to {company_name}):
[Write the connection request message]

2. FOLLOW UP MESSAGE (under 500 characters after connecting):
[Write the follow up message referencing something real about {company_name}]
""")

    # ── Part 3 ──
    part3 = ask_ai(f"""
You are a senior sales intelligence analyst.
Company: {company_name}
Salesperson: {salesperson_intro} sells {product}
Company context: {web_data[:400]}
Pain points: {search_strategy.get('pain_points', '')}

Write all sections below using EXACTLY these headers on their own line.

COMPETITOR BATTLE CARD:
Likely using: [what solution {company_name} most likely uses instead of {product}]

Weakness 1: [weakness of their current solution]
Your angle: [what {salesperson_intro} should say to position {product}]

Weakness 2: [second weakness]
Your angle: [sharp positioning response]

Weakness 3: [third weakness]
Your angle: [sharp positioning response]

EMAIL SEQUENCE:
EMAIL 1 - Day 1:
Subject: [subject]
[2-3 sentences referencing real {company_name} data and {product}]

EMAIL 2 - Day 3:
Subject: [subject]
[2-3 sentences adding value specific to their situation]

EMAIL 3 - Day 7:
Subject: [subject]
[2-3 sentences from a different angle addressing likely objection]

EMAIL 4 - Day 14:
Subject: [subject]
[2-3 sentences with social proof or case study]

EMAIL 5 - Day 21:
Subject: [subject]
[2 sentences breakup email]
""")

    profile          = extract_section(part1, "COMPANY PROFILE:",         all_headers_1)
    opener           = extract_section(part1, "OPENING LINE:",            all_headers_1)
    questions        = extract_section(part1, "DISCOVERY QUESTIONS:",     all_headers_1)
    objections       = extract_section(part1, "OBJECTIONS AND RESPONSES:",all_headers_1)
    next_steps       = extract_section(part1, "NEXT STEPS:",              all_headers_1)
    email            = extract_section(part2, "COLD EMAIL:",              all_headers_2)
    talk_track       = extract_section(part2, "TALK TRACK:",              all_headers_2)
    linkedin         = extract_section(part2, "LINKEDIN MESSAGES:",       all_headers_2)
    competitor_battle= extract_section(part3, "COMPETITOR BATTLE CARD:",  all_headers_3)
    email_sequence   = extract_section(part3, "EMAIL SEQUENCE:",          all_headers_3)

    result = {
        "company": company_name, "user_name": user_name,
        "user_role": user_role,  "product":   product,
        "scores": scores,  "fit_check": fit_check, "signals": signals,
        "profile": profile, "opener": opener, "questions": questions,
        "objections": objections, "next_steps": next_steps,
        "email": email, "talk_track": talk_track, "linkedin": linkedin,
        "competitor_battle": competitor_battle, "email_sequence": email_sequence,
        "notes": "", "created_at": created_at
    }

    try:
        conn = get_db()
        conn.execute("""
            INSERT INTO leads (
                user_id, company, user_name, user_role, product,
                scores, fit_check, signals,
                profile, opener, questions, objections, next_steps,
                email, talk_track, linkedin, competitor_battle,
                email_sequence, notes, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            session["user_id"],
            company_name, user_name, user_role, product,
            json.dumps(scores), json.dumps(fit_check), json.dumps(signals),
            profile, opener, questions, objections, next_steps,
            email, talk_track, linkedin, competitor_battle,
            email_sequence, "", created_at
        ))
        lead_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Increment report count
        conn.execute(
            "UPDATE users SET report_count = report_count + 1 WHERE id = ?",
            (session["user_id"],)
        )
        conn.commit()
        conn.close()
        result["id"] = lead_id
        print(f"Lead saved: ID {lead_id} | User: {user['email']}")
    except Exception as e:
        print("DB error:", e)
        result["id"] = None

    # Return remaining count so frontend can update UI
    updated_user = get_current_user()
    result["reports_remaining"] = updated_user["report_limit"] - updated_user["report_count"] if updated_user else 0

    return jsonify(result)

# ──────────────────────────────────────────────
# DEBRIEF & OBJECTION
# ──────────────────────────────────────────────

@app.route("/debrief", methods=["POST"])
@login_required
def debrief():
    data      = request.json
    company   = data.get("company", "")
    notes     = data.get("notes",   "")
    user_name = data.get("name",    "")
    user_role = data.get("role",    "")
    product   = data.get("product", "")
    salesperson_intro = f"{user_name}{f', {user_role}' if user_role else ''}"

    follow_up = ask_ai(f"""
{salesperson_intro} just had a sales call with {company} about {product}.
Call notes: {notes}

Write a professional follow up email based on these exact notes.
Format:
Subject: [specific subject referencing what was discussed]

[Reference something specific from the call]
[Summarize key points and next steps agreed]
[Clear single call to action]

{user_name}
{user_role if user_role else ''}

Under 150 words. Human and specific.
""")

    next_action = ask_ai(f"""
Call notes from {salesperson_intro} with {company}: {notes}
What is the single best next action to move this deal forward?
One sentence, very specific and actionable.
""")

    score_data = ask_ai_json(f"""
Call notes from a sales call with {company}: {notes}
Return ONLY raw JSON:
{{
    "score": <updated deal score 0-100>,
    "status": "<Hot|Warm|Cold>",
    "reasoning": "<one sentence why>"
}}
""")

    if not score_data:
        score_data = {"score": 50, "status": "Warm", "reasoning": "Call completed"}

    return jsonify({"follow_up": follow_up, "next_action": next_action, "score_data": score_data})

@app.route("/objection", methods=["POST"])
@login_required
def objection():
    data           = request.json
    company        = data.get("company",   "")
    product        = data.get("product",   "")
    objection_text = data.get("objection", "")
    profile        = data.get("profile",   "")

    response = ask_ai(f"""
You are helping during a live sales call.
Company: {company} | Product: {product}
Profile: {profile[:400]}
The prospect just said: "{objection_text}"
Give one sharp confident response to say RIGHT NOW.
2-3 sentences. Natural not scripted. Acknowledge first.
""")

    return jsonify({"response": response})

# ──────────────────────────────────────────────
# ADMIN PANEL
# ──────────────────────────────────────────────

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = (request.json or request.form).get("password", "")
        if password == ADMIN_PASSWORD:
            session["is_admin"] = True
            return jsonify({"success": True})
        return jsonify({"error": "Invalid password"}), 401

    # GET — serve simple login page
    return """
<!DOCTYPE html>
<html>
<head>
  <title>LeadLens Admin</title>
  <link href="https://fonts.googleapis.com/css2?family=Sora:wght@600;700&family=DM+Sans:wght@400;500&display=swap" rel="stylesheet"/>
  <style>
    *{box-sizing:border-box;margin:0;padding:0;font-family:'DM Sans',sans-serif}
    body{background:#0B1829;display:flex;align-items:center;justify-content:center;min-height:100vh}
    .card{background:#112240;border:1px solid rgba(148,163,184,0.15);border-radius:16px;padding:40px;width:360px}
    h2{font-family:'Sora',sans-serif;color:#F8FAFF;font-size:1.4rem;margin-bottom:8px}
    p{color:#94A3B8;font-size:0.85rem;margin-bottom:28px}
    input{width:100%;background:rgba(255,255,255,0.05);border:1px solid rgba(148,163,184,0.15);border-radius:8px;padding:12px 16px;color:#F8FAFF;font-size:0.9rem;outline:none;margin-bottom:16px}
    input:focus{border-color:#2563EB}
    button{width:100%;background:#2563EB;border:none;color:white;padding:13px;border-radius:8px;font-family:'Sora',sans-serif;font-size:0.95rem;font-weight:600;cursor:pointer}
    button:hover{background:#3B82F6}
    .err{color:#FCA5A5;font-size:0.82rem;margin-top:10px;display:none}
  </style>
</head>
<body>
  <div class="card">
    <h2>Admin Login</h2>
    <p>LeadLens Admin Panel</p>
    <input type="password" id="pw" placeholder="Admin password" onkeydown="if(event.key==='Enter')login()"/>
    <button onclick="login()">Sign In</button>
    <div class="err" id="err">Invalid password.</div>
  </div>
  <script>
    async function login(){
      const pw=document.getElementById('pw').value;
      const r=await fetch('/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})});
      if(r.ok){window.location.href='/admin';}
      else{document.getElementById('err').style.display='block';}
    }
  </script>
</body>
</html>
"""

@app.route("/admin")
@admin_required
def admin_panel():
    conn = get_db()
    users    = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    codes    = conn.execute("SELECT * FROM invitation_codes ORDER BY created_at DESC").fetchall()
    requests = conn.execute("SELECT * FROM access_requests ORDER BY created_at DESC").fetchall()
    waitlist = conn.execute("SELECT * FROM early_access ORDER BY created_at DESC").fetchall()
    conn.close()

    users_html    = "".join([f"""
        <tr>
          <td>{u['email']}</td>
          <td>{u['invite_code']}</td>
          <td><span class="badge {'badge-ok' if u['report_count'] < u['report_limit'] else 'badge-warn'}">{u['report_count']} / {u['report_limit']}</span></td>
          <td>{u['created_at']}</td>
          <td><button class="btn-sm" onclick="grantReports({u['id']}, '{u['email']}')">+10 Reports</button></td>
        </tr>""" for u in users])

    codes_html    = "".join([f"""
        <tr>
          <td><code>{c['code']}</code></td>
          <td><span class="badge {'badge-warn' if c['used'] else 'badge-ok'}">{' Used by ' + c['used_by'] if c['used'] else 'Available'}</span></td>
          <td>{c['created_at']}</td>
        </tr>""" for c in codes])

    requests_html = "".join([f"""
        <tr>
          <td>{r['email']}</td>
          <td><span class="badge {'badge-ok' if r['status']=='approved' else 'badge-warn'}">{r['status']}</span></td>
          <td>{r['created_at']}</td>
          <td>{'<button class="btn-sm" onclick="approveRequest('+str(r['id'])+','+str(r['user_id'])+')">Grant +10</button>' if r['status']=='pending' else '✓'}</td>
        </tr>""" for r in requests])

    waitlist_html = "".join([f"<tr><td>{w['email']}</td><td>{w['created_at']}</td></tr>" for w in waitlist])

    return f"""
<!DOCTYPE html>
<html>
<head>
  <title>LeadLens Admin</title>
  <link href="https://fonts.googleapis.com/css2?family=Sora:wght@600;700;800&family=DM+Sans:wght@400;500&display=swap" rel="stylesheet"/>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0;font-family:'DM Sans',sans-serif}}
    body{{background:#0B1829;color:#F8FAFF;min-height:100vh}}
    nav{{background:#112240;border-bottom:1px solid rgba(148,163,184,0.15);padding:16px 32px;display:flex;align-items:center;justify-content:space-between}}
    .logo{{font-family:'Sora',sans-serif;font-size:1.1rem;font-weight:700;color:#F8FAFF}}
    .logo span{{color:#3B82F6}}
    a.logout{{font-size:0.82rem;color:#94A3B8;text-decoration:none}}
    a.logout:hover{{color:#F8FAFF}}
    main{{padding:32px}}
    h2{{font-family:'Sora',sans-serif;font-size:1.1rem;font-weight:700;margin-bottom:16px;color:#F8FAFF}}
    .section{{background:#112240;border:1px solid rgba(148,163,184,0.15);border-radius:12px;padding:24px;margin-bottom:28px}}
    .section-header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px}}
    table{{width:100%;border-collapse:collapse;font-size:0.85rem}}
    th{{text-align:left;padding:8px 12px;color:#94A3B8;font-size:0.72rem;text-transform:uppercase;letter-spacing:0.08em;border-bottom:1px solid rgba(148,163,184,0.1)}}
    td{{padding:10px 12px;border-bottom:1px solid rgba(148,163,184,0.07);color:#CBD5E1}}
    tr:last-child td{{border-bottom:none}}
    code{{background:rgba(37,99,235,0.1);padding:2px 8px;border-radius:4px;color:#60A5FA;font-size:0.82rem}}
    .badge{{font-size:0.72rem;padding:3px 10px;border-radius:99px;font-weight:600}}
    .badge-ok{{background:rgba(16,185,129,0.12);color:#34D399;border:1px solid rgba(16,185,129,0.2)}}
    .badge-warn{{background:rgba(245,158,11,0.12);color:#FCD34D;border:1px solid rgba(245,158,11,0.2)}}
    .btn-sm{{background:#2563EB;border:none;color:white;padding:5px 14px;border-radius:6px;font-size:0.78rem;font-weight:600;cursor:pointer}}
    .btn-sm:hover{{background:#3B82F6}}
    .btn-gen{{background:#2563EB;border:none;color:white;padding:8px 18px;border-radius:7px;font-size:0.85rem;font-weight:600;cursor:pointer}}
    .btn-gen:hover{{background:#3B82F6}}
    .stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:28px}}
    .stat-card{{background:#112240;border:1px solid rgba(148,163,184,0.15);border-radius:10px;padding:20px}}
    .stat-num{{font-family:'Sora',sans-serif;font-size:2rem;font-weight:800;color:#3B82F6}}
    .stat-lbl{{font-size:0.78rem;color:#94A3B8;margin-top:4px}}
    .toast{{position:fixed;bottom:24px;right:24px;background:#112240;border:1px solid rgba(37,99,235,0.3);border-radius:8px;padding:12px 20px;font-size:0.85rem;color:#34D399;display:none;z-index:999}}
  </style>
</head>
<body>
<nav>
  <div class="logo">Lead<span>Lens</span> <span style="font-size:0.75rem;color:#94A3B8;font-weight:400;margin-left:8px">Admin Panel</span></div>
  <a class="logout" href="/admin/logout">Log out</a>
</nav>
<main>
  <div class="stats">
    <div class="stat-card"><div class="stat-num">{len(users)}</div><div class="stat-lbl">Registered Users</div></div>
    <div class="stat-card"><div class="stat-num">{sum(1 for c in codes if not c['used'])}</div><div class="stat-lbl">Available Codes</div></div>
    <div class="stat-card"><div class="stat-num">{sum(1 for r in requests if r['status']=='pending')}</div><div class="stat-lbl">Pending Requests</div></div>
    <div class="stat-card"><div class="stat-num">{len(waitlist)}</div><div class="stat-lbl">Waitlist Signups</div></div>
  </div>

  <div class="section">
    <div class="section-header">
      <h2>Users</h2>
    </div>
    <table><thead><tr><th>Email</th><th>Invite Code</th><th>Reports Used</th><th>Joined</th><th>Action</th></tr></thead>
    <tbody>{users_html or '<tr><td colspan=5 style="color:#94A3B8;text-align:center;padding:20px">No users yet</td></tr>'}</tbody></table>
  </div>

  <div class="section">
    <div class="section-header">
      <h2>Access Requests</h2>
    </div>
    <table><thead><tr><th>Email</th><th>Status</th><th>Requested</th><th>Action</th></tr></thead>
    <tbody>{requests_html or '<tr><td colspan=4 style="color:#94A3B8;text-align:center;padding:20px">No requests yet</td></tr>'}</tbody></table>
  </div>

  <div class="section">
    <div class="section-header">
      <h2>Invitation Codes</h2>
      <button class="btn-gen" onclick="generateCode()">+ Generate Code</button>
    </div>
    <table><thead><tr><th>Code</th><th>Status</th><th>Created</th></tr></thead>
    <tbody id="codes-tbody">{codes_html or '<tr><td colspan=3 style="color:#94A3B8;text-align:center;padding:20px">No codes yet</td></tr>'}</tbody></table>
  </div>

  <div class="section">
    <h2>Early Access Waitlist</h2>
    <table><thead><tr><th>Email</th><th>Signed Up</th></tr></thead>
    <tbody>{waitlist_html or '<tr><td colspan=2 style="color:#94A3B8;text-align:center;padding:20px">No signups yet</td></tr>'}</tbody></table>
  </div>
</main>
<div class="toast" id="toast"></div>
<script>
  function showToast(msg){{
    const t=document.getElementById('toast');
    t.textContent=msg;t.style.display='block';
    setTimeout(()=>t.style.display='none',3000);
  }}

  async function grantReports(userId, email){{
    if(!confirm(`Grant +10 reports to ${{email}}?`))return;
    const r=await fetch('/admin/grant-reports',{{
      method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{user_id:userId}})
    }});
    if(r.ok){{showToast('✓ Reports granted. Refreshing...');setTimeout(()=>location.reload(),1500);}}
    else showToast('Error granting reports.');
  }}

  async function approveRequest(requestId, userId){{
    if(!confirm('Approve this request and grant +10 reports?'))return;
    const r=await fetch('/admin/approve-request',{{
      method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{request_id:requestId,user_id:userId}})
    }});
    if(r.ok){{showToast('✓ Request approved. Refreshing...');setTimeout(()=>location.reload(),1500);}}
    else showToast('Error approving request.');
  }}

  async function generateCode(){{
    const r=await fetch('/admin/generate-code',{{method:'POST'}});
    const d=await r.json();
    if(d.code){{
      showToast('✓ Code generated: '+d.code);
      const tbody=document.getElementById('codes-tbody');
      const tr=document.createElement('tr');
      tr.innerHTML=`<td><code>${{d.code}}</code></td><td><span class="badge badge-ok">Available</span></td><td>Just now</td>`;
      tbody.prepend(tr);
    }}
  }}
</script>
</body>
</html>
"""

@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect("/admin/login")

@app.route("/admin/generate-code", methods=["POST"])
@admin_required
def generate_code():
    code = "-".join([secrets.token_hex(3).upper() for _ in range(2)])
    conn = get_db()
    conn.execute(
        "INSERT INTO invitation_codes (code, used, created_at) VALUES (?, 0, ?)",
        (code, datetime.now().strftime("%b %d, %Y %I:%M %p"))
    )
    conn.commit()
    conn.close()
    return jsonify({"code": code})

@app.route("/admin/grant-reports", methods=["POST"])
@admin_required
def grant_reports():
    user_id = request.json.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    conn = get_db()
    conn.execute(
        "UPDATE users SET report_limit = report_limit + 10 WHERE id = ?", (user_id,)
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/admin/approve-request", methods=["POST"])
@admin_required
def approve_request():
    data       = request.json or {}
    request_id = data.get("request_id")
    user_id    = data.get("user_id")
    if not request_id or not user_id:
        return jsonify({"error": "request_id and user_id required"}), 400
    resolved_at = datetime.now().strftime("%b %d, %Y %I:%M %p")
    conn = get_db()
    conn.execute(
        "UPDATE access_requests SET status = 'approved', resolved_at = ? WHERE id = ?",
        (resolved_at, request_id)
    )
    conn.execute(
        "UPDATE users SET report_limit = report_limit + 10 WHERE id = ?", (user_id,)
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True})

# ──────────────────────────────────────────────

# ──────────────────────────────────────────────
# REMINDERS / CALENDAR
# ──────────────────────────────────────────────

@app.route("/reminders", methods=["GET"])
@login_required
def get_reminders():
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM reminders WHERE user_id=? ORDER BY date ASC",
            (session["user_id"],)
        ).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        print("get_reminders error:", e)
        return jsonify([])

@app.route("/reminders", methods=["POST"])
@login_required
def add_reminder():
    data         = request.json or {}
    note         = data.get("note","").strip()
    date         = data.get("date","").strip()
    lead_id      = data.get("lead_id")
    company      = data.get("company","")
    ai_suggested = 1 if data.get("ai_suggested") else 0

    if not note or not date:
        return jsonify({"error":"Note and date required"}), 400

    created_at = datetime.now().strftime("%b %d, %Y %I:%M %p")
    try:
        conn = get_db()
        conn.execute("""
            INSERT INTO reminders (user_id,lead_id,company,note,date,ai_suggested,created_at)
            VALUES (?,?,?,?,?,?,?)
        """, (session["user_id"], lead_id, company, note, date, ai_suggested, created_at))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        print("add_reminder error:", e)
        return jsonify({"error": str(e)}), 500

@app.route("/reminders/<int:reminder_id>", methods=["DELETE"])
@login_required
def delete_reminder(reminder_id):
    try:
        conn = get_db()
        conn.execute(
            "DELETE FROM reminders WHERE id=? AND user_id=?",
            (reminder_id, session["user_id"])
        )
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False})

@app.route("/reminders/ai-suggest", methods=["POST"])
@login_required
def ai_suggest_reminder():
    data    = request.json or {}
    lead_id = data.get("lead_id")
    company = data.get("company","")
    profile = data.get("profile","")

    if not lead_id:
        return jsonify({"error": "lead_id required"}), 400

    # Fetch saved notes for this lead
    conn = get_db()
    notes = conn.execute(
        "SELECT note, created_at FROM lead_notes WHERE lead_id=? AND user_id=? ORDER BY id DESC LIMIT 5",
        (lead_id, session["user_id"])
    ).fetchall()
    conn.close()

    if not notes:
        return jsonify({"error": "No notes found for this lead yet."})

    notes_text = "\n".join([f"[{n['created_at']}] {n['note']}" for n in notes])
    today     = datetime.now().strftime("%Y-%m-%d")
    today_dow = datetime.now().strftime("%A")  # e.g. "Thursday"

    prompt = f"""You are a sales assistant. Today is {today_dow}, {today}.

A salesperson is working on a deal with: {company}

Company profile: {profile[:300]}

Their recent notes about this lead:
{notes_text}

Based on these notes, suggest the best follow-up date and a one-sentence reason why.
Be precise with dates — today is {today_dow} {today}, so "next Friday" means the coming Friday which you must calculate correctly from today's actual date.

Respond in this exact format only — no other text:
DATE: YYYY-MM-DD
REASON: one sentence explaining why this date and what to follow up on"""

    result = ask_groq(prompt)

    if not result:
        return jsonify({"error": "Could not get suggestion from Groq."})

    try:
        date_line   = [l for l in result.split('\n') if l.startswith('DATE:')]
        reason_line = [l for l in result.split('\n') if l.startswith('REASON:')]
        date        = date_line[0].replace('DATE:','').strip() if date_line else today
        suggestion  = reason_line[0].replace('REASON:','').strip() if reason_line else result
        return jsonify({"date": date, "suggestion": suggestion})
    except:
        return jsonify({"error": "Could not parse suggestion."})

# ──────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=True, port=5000)