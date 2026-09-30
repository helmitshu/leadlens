# LeadLens — AI Sales Intelligence Agent

A full-stack AI web app that researches any company worldwide and generates a complete sales brief in under 90 seconds.

## What it does

- Researches any company using live web data
- Detects buying signals: funding, leadership changes, expansion, hiring
- Scores deal readiness, product need, budget, and decision speed
- Generates cold email, talk track, LinkedIn messages, and battle cards
- Handles objections live during calls
- Exports the full report as PDF

## Tech stack

- Python + Flask (backend)
- Anthropic Claude (reasoning)
- Tavily (real-time web search)
- Groq (lightweight AI tasks)
- SQLite (lead database)
- Chart.js (data visualization)
- jsPDF (PDF export)

## Setup

1. Clone the repo
2. Create a virtual environment: `python -m venv venv`
3. Activate it: `venv\Scripts\activate` (Windows) or `source venv/bin/activate` (Mac/Linux)
4. Install packages: `pip install -r requirements.txt`
5. Create a `.env` file with your own API keys:
   ```
   ANTHROPIC_API_KEY=your-key
   TAVILY_API_KEY=your-key
   GROQ_API_KEY=your-key
   SECRET_KEY=your-secret
   ADMIN_PASSWORD=your-password
   ```
6. Run: `python app.py`
7. Open: http://localhost:5000

No keys are committed to this repo. Everything secret lives in `.env`, which is gitignored.
