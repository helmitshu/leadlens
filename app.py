import os
import json
import time
import sqlite3
import anthropic
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from tavily import TavilyClient

load_dotenv()

app = Flask(__name__)
CORS(app)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
MODEL = "claude-sonnet-4-5"

if not ANTHROPIC_API_KEY or not TAVILY_API_KEY:
    print("WARNING: API keys missing. Check your .env file.")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
tavily = TavilyClient(api_key=TAVILY_API_KEY)

def init_db():
    conn = sqlite3.connect("leadlens.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT NOT NULL,
            user_name TEXT,
            user_role TEXT DEFAULT '',
            product TEXT,
            scores TEXT,
            fit_check TEXT,
            signals TEXT,
            profile TEXT,
            opener TEXT,
            questions TEXT,
            objections TEXT,
            next_steps TEXT,
            email TEXT,
            talk_track TEXT,
            linkedin TEXT,
            competitor_battle TEXT,
            email_sequence TEXT,
            notes TEXT DEFAULT '',
            created_at TEXT
        )
    """)
    try:
        c.execute("ALTER TABLE leads ADD COLUMN user_role TEXT DEFAULT ''")
    except:
        pass
    conn.commit()
    conn.close()

init_db()

def get_db():
    conn = sqlite3.connect("leadlens.db")
    conn.row_factory = sqlite3.Row
    return conn

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
            text = message.content[0].text.strip()
            text = text.replace("```json", "").replace("```", "").strip()
            start = text.find("{")
            end = text.rfind("}") + 1
            if start == -1 or end == 0:
                print(f"No JSON found (attempt {attempt+1}):", text[:200])
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
        end = len(text)
        for h in all_headers:
            if h != header:
                pos = text.find(h, start)
                if pos != -1 and pos < end:
                    end = pos
        return text[start:end].strip()
    except:
        return ""

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/leads", methods=["GET"])
def get_leads():
    try:
        conn = get_db()
        leads = conn.execute(
            "SELECT * FROM leads ORDER BY created_at DESC"
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
def delete_lead(lead_id):
    try:
        conn = get_db()
        conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except:
        return jsonify({"success": False})

@app.route("/leads/<int:lead_id>/notes", methods=["PUT"])
def update_notes(lead_id):
    try:
        notes = request.json.get("notes", "")
        conn = get_db()
        conn.execute(
            "UPDATE leads SET notes = ? WHERE id = ?",
            (notes, lead_id)
        )
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except:
        return jsonify({"success": False})

@app.route("/research", methods=["POST"])
def research():
    data = request.json
    company_name = data.get("company", "").strip()
    user_name = data.get("name", "").strip()
    user_role = data.get("role", "").strip()
    product = data.get("product", "").strip()

    if not company_name or not user_name or not product:
        return jsonify({
            "error": True,
            "message": "Please fill in your name, what you sell, and a company name"
        })

    print(f"\n--- Researching: {company_name} | Product: {product} ---")

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

    print("SEARCH STRATEGY:", search_strategy)

    if not search_strategy:
        search_strategy = {
            "search_query_1": f"{company_name} company employees offices size",
            "search_query_2": f"{company_name} news growth 2025",
            "search_query_3": f"{company_name} procurement purchasing",
            "key_facts_needed": f"Company size, growth stage, and decision maker for {product}",
            "pain_points": f"Need for {product} based on company operations"
        }

    q1 = search_strategy.get("search_query_1", f"{company_name} company overview")
    q2 = search_strategy.get("search_query_2", f"{company_name} news 2025")
    q3 = search_strategy.get("search_query_3", f"{company_name} procurement")

    web_data = search_web(f"{company_name} {q1}")
    time.sleep(0.5)
    web_news = search_web(f"{company_name} {q2}")
    time.sleep(0.5)
    web_jobs = search_web(f"{company_name} {q3}")

    if not web_data or len(web_data.strip()) < 50:
        return jsonify({
            "error": True,
            "message": f"Could not find information about '{company_name}'. Check the spelling or try adding more detail — for example: the company full name, city, or industry."
        })

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

    print("FIT CHECK:", fit_check)

    if not fit_check:
        fit_check = {
            "is_fit": True,
            "confidence": 70,
            "product_category": product,
            "reasoning": "Proceeding with research.",
            "warning": "",
            "right_company": "",
            "decision_maker": "Procurement Manager"
        }

    if not fit_check.get("is_fit", True):
        created_at = datetime.now().strftime("%b %d, %Y %I:%M %p")
        result = {
            "error": False,
            "company": company_name,
            "user_name": user_name,
            "user_role": user_role,
            "product": product,
            "scores": {
                "deal_readiness": 0, "need_score": 0,
                "budget_score": 0, "decision_speed": 0, "overall": 0
            },
            "fit_check": fit_check,
            "signals": {},
            "profile": "", "opener": "", "questions": "",
            "objections": "", "next_steps": "", "email": "",
            "talk_track": "", "linkedin": "",
            "competitor_battle": "", "email_sequence": "",
            "notes": "", "created_at": created_at
        }
        try:
            conn = get_db()
            conn.execute("""
                INSERT INTO leads (
                    company, user_name, user_role, product,
                    scores, fit_check, signals,
                    profile, opener, questions, objections, next_steps,
                    email, talk_track, linkedin, competitor_battle,
                    email_sequence, notes, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                company_name, user_name, user_role, product,
                json.dumps(result["scores"]),
                json.dumps(fit_check),
                json.dumps({}),
                "", "", "", "", "", "", "", "", "", "", "",
                created_at
            ))
            lead_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
            conn.close()
            result["id"] = lead_id
        except Exception as e:
            print("DB error (fit fail):", e)
            result["id"] = None
        return jsonify(result)

    time.sleep(0.5)
    signal_funding = search_web(f"{company_name} funding raised investment")
    time.sleep(0.5)
    signal_leadership = search_web(f"{company_name} new CEO CFO VP director hired")
    time.sleep(0.5)
    signal_expansion = search_web(f"{company_name} expansion growth new office")
    time.sleep(0.5)
    signal_contracts = search_web(f"{company_name} new contract partnership deal")

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
    "funding": {{"detected": <true/false>, "score": <0-10>, "detail": "<what found or No recent funding detected>"}},
    "leadership": {{"detected": <true/false>, "score": <0-10>, "detail": "<what found or No leadership changes detected>"}},
    "expansion": {{"detected": <true/false>, "score": <0-10>, "detail": "<what found or No expansion signals detected>"}},
    "hiring": {{"detected": <true/false>, "score": <0-10>, "detail": "<what found or No relevant hiring detected>"}},
    "contracts": {{"detected": <true/false>, "score": <0-10>, "detail": "<what found or No new contracts detected>"}}
}}
""")

    print("SIGNALS:", signals)

    if not signals:
        signals = {
            "timing_score": 50,
            "recommendation": "Research this company further before reaching out.",
            "funding": {"detected": False, "score": 0, "detail": "No recent funding detected"},
            "leadership": {"detected": False, "score": 0, "detail": "No leadership changes detected"},
            "expansion": {"detected": False, "score": 0, "detail": "No expansion signals detected"},
            "hiring": {"detected": False, "score": 0, "detail": "No relevant hiring detected"},
            "contracts": {"detected": False, "score": 0, "detail": "No new contracts detected"}
        }

    scores = ask_ai_json(f"""
You are a sales intelligence analyst scoring a {product} opportunity.

COMPANY: {company_name}
PRODUCT: {product}
PRODUCT CATEGORY: {fit_check.get('product_category', product)}
FIT CONFIDENCE: {fit_check.get('confidence', 70)}%

KEY FACTS FOR THIS SALE: {search_strategy.get('key_facts_needed', '')}
PAIN POINTS: {search_strategy.get('pain_points', '')}

WEB DATA: {web_data[:500]}
NEWS: {web_news[:300]}

BUYING SIGNALS FOUND:
Funding: {signals.get('funding', {}).get('detail', 'none')}
Leadership changes: {signals.get('leadership', {}).get('detail', 'none')}
Expansion: {signals.get('expansion', {}).get('detail', 'none')}
Hiring: {signals.get('hiring', {}).get('detail', 'none')}

Use this exact scoring logic based on real evidence only:

DEAL READINESS — how ready to buy RIGHT NOW based on signals:
Start at 40. Then adjust based on what you actually found:
+ Fresh funding in last 6 months → add 25
+ Active hiring for relevant roles → add 15
+ New leadership (CEO/VP/Director) → add 15
+ Expansion or growth news → add 15
+ Strong revenue or profit news → add 10
- Layoffs or cost cutting news → subtract 25
- No signals found at all → stay at 40

NEED SCORE — how much do they need this specific product:
- Universal product (office supplies, food, software, cleaning, HR, utilities, vehicles, marketing) AND company has employees → score 72-82
- Specialized product AND company is in exact matching industry → score 75-88
- Specialized product AND company adjacent industry → score 45-65

BUDGET SCORE — can they actually pay based on real signals:
- Raised Series B or later OR public company → score 75-88
- Raised Series A OR mid-size established company → score 60-75
- Small company under 50 people, no funding found → score 40-58
- Government, university, or institution → score 62-75
- No financial signals found → score 48-55
- Explicit cost cutting or financial trouble → score 20-38

DECISION SPEED — how fast will they decide based on company type:
- Startup under 50 employees → score 65-78
- Growing company 50-200 employees → score 50-65
- Mid-size 200-500 employees → score 38-52
- Large enterprise 500+ employees → score 22-42
- Government or institution → score 15-35

OVERALL: Calculate as weighted average:
deal_readiness x 0.25 + need_score x 0.35 + budget_score x 0.25 + decision_speed x 0.15

Return ONLY raw JSON:
{{
    "deal_readiness": <integer 0-100>,
    "need_score": <integer 0-100>,
    "budget_score": <integer 0-100>,
    "decision_speed": <integer 0-100>,
    "overall": <integer 0-100>
}}
""")

    print("SCORES:", scores)

    if not scores or "overall" not in scores:
        scores = {
            "deal_readiness": 50,
            "need_score": 55,
            "budget_score": 50,
            "decision_speed": 50,
            "overall": 51
        }

    salesperson_intro = f"{user_name}{f', {user_role}' if user_role else ''}"

    all_headers_1 = [
        "COMPANY PROFILE:", "OPENING LINE:", "DISCOVERY QUESTIONS:",
        "OBJECTIONS AND RESPONSES:", "NEXT STEPS:"
    ]
    all_headers_2 = [
        "COLD EMAIL:", "TALK TRACK:", "LINKEDIN MESSAGES:"
    ]
    all_headers_3 = [
        "COMPETITOR BATTLE CARD:", "EMAIL SEQUENCE:"
    ]

    part1 = ask_ai(f"""
You are a senior sales intelligence analyst.
Company: {company_name}
Web data: {web_data[:600]}
News: {web_news[:300]}
Salesperson: {salesperson_intro} sells {product}
Decision maker: {fit_check.get('decision_maker', 'Procurement Manager')}
Key facts needed for this sale: {search_strategy.get('key_facts_needed', '')}
Pain points that drive purchase: {search_strategy.get('pain_points', '')}

Write all sections below using EXACTLY these headers on their own line.

COMPANY PROFILE:
Write 4-5 sentences focused specifically on what matters for selling {product} to {company_name}.
Include company size and employee count, office locations or operations, growth trajectory,
procurement hints, and specific signals that indicate need for {product}.
Every sentence must be relevant to selling {product}. No generic company history.

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

    print("PART1 length:", len(part1))

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

    print("PART2 length:", len(part2))

    part3 = ask_ai(f"""
You are a senior sales intelligence analyst.
Company: {company_name}
Salesperson: {salesperson_intro} sells {product}
Company context: {web_data[:400]}
Pain points: {search_strategy.get('pain_points', '')}

Write all sections below using EXACTLY these headers on their own line.

COMPETITOR BATTLE CARD:
Likely using: [what solution {company_name} most likely uses instead of {product}]

Weakness 1: [weakness of their current solution relevant to {company_name}]
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

    print("PART3 length:", len(part3))

    profile = extract_section(part1, "COMPANY PROFILE:", all_headers_1)
    opener = extract_section(part1, "OPENING LINE:", all_headers_1)
    questions = extract_section(part1, "DISCOVERY QUESTIONS:", all_headers_1)
    objections = extract_section(part1, "OBJECTIONS AND RESPONSES:", all_headers_1)
    next_steps = extract_section(part1, "NEXT STEPS:", all_headers_1)
    email = extract_section(part2, "COLD EMAIL:", all_headers_2)
    talk_track = extract_section(part2, "TALK TRACK:", all_headers_2)
    linkedin = extract_section(part2, "LINKEDIN MESSAGES:", all_headers_2)
    competitor_battle = extract_section(part3, "COMPETITOR BATTLE CARD:", all_headers_3)
    email_sequence = extract_section(part3, "EMAIL SEQUENCE:", all_headers_3)

    print("PROFILE:", profile[:100] if profile else "EMPTY")
    print("EMAIL:", email[:100] if email else "EMPTY")

    created_at = datetime.now().strftime("%b %d, %Y %I:%M %p")

    result = {
        "company": company_name,
        "user_name": user_name,
        "user_role": user_role,
        "product": product,
        "scores": scores,
        "fit_check": fit_check,
        "signals": signals,
        "profile": profile,
        "opener": opener,
        "questions": questions,
        "objections": objections,
        "next_steps": next_steps,
        "email": email,
        "talk_track": talk_track,
        "linkedin": linkedin,
        "competitor_battle": competitor_battle,
        "email_sequence": email_sequence,
        "notes": "",
        "created_at": created_at
    }

    try:
        conn = get_db()
        conn.execute("""
            INSERT INTO leads (
                company, user_name, user_role, product,
                scores, fit_check, signals,
                profile, opener, questions, objections, next_steps,
                email, talk_track, linkedin, competitor_battle,
                email_sequence, notes, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            company_name, user_name, user_role, product,
            json.dumps(scores),
            json.dumps(fit_check),
            json.dumps(signals),
            profile, opener, questions, objections, next_steps,
            email, talk_track, linkedin, competitor_battle,
            email_sequence, "", created_at
        ))
        lead_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        conn.close()
        result["id"] = lead_id
        print(f"Lead saved: ID {lead_id}")
    except Exception as e:
        print("DB error:", e)
        result["id"] = None

    return jsonify(result)

@app.route("/debrief", methods=["POST"])
def debrief():
    data = request.json
    company = data.get("company", "")
    notes = data.get("notes", "")
    user_name = data.get("name", "")
    user_role = data.get("role", "")
    product = data.get("product", "")

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

    return jsonify({
        "follow_up": follow_up,
        "next_action": next_action,
        "score_data": score_data
    })

@app.route("/objection", methods=["POST"])
def objection():
    data = request.json
    company = data.get("company", "")
    product = data.get("product", "")
    objection_text = data.get("objection", "")
    profile = data.get("profile", "")

    response = ask_ai(f"""
You are helping during a live sales call.
Company: {company} | Product: {product}
Profile: {profile[:400]}
The prospect just said: "{objection_text}"
Give one sharp confident response to say RIGHT NOW.
2-3 sentences. Natural not scripted. Acknowledge first.
""")

    return jsonify({"response": response})

if __name__ == "__main__":
    app.run(debug=True, port=5000)
