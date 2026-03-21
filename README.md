# LeadLens — Verified Sales Intelligence Platform

AI-powered B2B sales intelligence for industrial sectors (Mining, CleanTech, AgTech).

Transforms a company name into a verified sales brief in under 30 seconds.

## Features

- **Truth Engine** — anti-hallucination AI with "Data Not Publicly Available" enforcement
- **Multi-tenant architecture** with PostgreSQL Row-Level Security
- **12 parallel async web searches** per report
- **Sector-aware intelligence** — Mining, CleanTech, AgTech buying committee mapping
- **Site vs HQ validation** — distinguishes corporate HQ from operational site
- **Contact enrichment** — email pattern inference + LinkedIn URL
- **Admin panel** — manage orgs, users, feature flags, invite codes
- **Team invite flow** — shareable join links with org auto-assignment
- **Team analytics dashboard** — leaderboard, activity feed, org-wide stats (owner/admin only)
- **PDF report download** — mobile-friendly via jsPDF

## Tech Stack

FastAPI · PostgreSQL · asyncpg · Claude Sonnet 4.6 · Tavily · JWT · Jinja2 · Vanilla JS

## Getting Started

### Prerequisites

- Python 3.11+
- PostgreSQL 14+

### Installation

```bash
git clone https://github.com/helmitshu/leadlens-platform.git
cd leadlens-platform
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Environment Variables

Create a `.env` file:

```env
ANTHROPIC_API_KEY=sk-ant-...
TAVILY_API_KEY=tvly-...
DATABASE_URL=postgresql://user:password@localhost:5432/leadlens
JWT_SECRET=your-random-secret-at-least-32-chars
ADMIN_TOKEN=your-admin-token
```

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Claude API key (claude-sonnet-4-6) |
| `TAVILY_API_KEY` | Yes | Tavily search API key |
| `DATABASE_URL` | Yes | PostgreSQL connection string |
| `JWT_SECRET` | Yes | Secret for signing JWT tokens (min 32 chars) |
| `ADMIN_TOKEN` | Yes | Token for `/admin/*` endpoints |

### Database Setup

```bash
python setup_db.py
```

Creates all tables, indexes, and the default schema. Re-running drops and recreates all tables.

### Run

```bash
uvicorn main:app --reload
```

App available at `http://localhost:8000`. API docs at `http://localhost:8000/api/docs`.

## Deployment (Render)

`render.yaml` is included for one-click Render deployment. Set the five environment variables in the Render dashboard. `DATABASE_URL` is auto-injected from the linked PostgreSQL instance.

## API Overview

| Method | Path | Description |
|---|---|---|
| `POST` | `/auth/register` | Register org + owner account |
| `POST` | `/auth/login` | Login, returns JWT |
| `GET` | `/usage` | Remaining report quota |
| `POST` | `/run` | Generate a lead report |
| `GET` | `/leads` | List all leads for current user |
| `DELETE` | `/leads/{id}` | Delete a lead |
| `PUT` | `/leads/{id}/notes` | Save notes on a lead |
| `GET` | `/analytics/team` | Team analytics (owner/admin, Team plan+) |
| `POST` | `/invite/generate` | Generate team invite link |
| `GET` | `/admin/stats` | Admin: platform-wide stats |
| `POST` | `/admin/create-org` | Admin: create new org |

## Architecture

```
Browser (Vanilla JS)
    │
    ▼
FastAPI (main.py)
    ├── JWT auth middleware
    ├── Tenant isolation (org_id scoped queries)
    ├── /run endpoint
    │     ├── 12 parallel Tavily searches
    │     └── 3-phase Claude pipeline
    │           ├── Phase 1: signals + scores (parallel)
    │           └── Phase 2: profile, outreach, objections (parallel)
    └── PostgreSQL via asyncpg connection pool
```

### Report Pipeline

1. **Site/HQ validation** — deduplicate corporate HQ vs operational site
2. **12 parallel searches** — news, funding, hiring, leadership, contracts, ESG, tech stack, site, products, awards, incumbents, financials
3. **Phase 1 AI** (parallel) — buying signals + 5-dimensional scores
4. **Phase 2 AI** (parallel) — company profile, personalised opener, discovery questions, objection handling, next steps, email, LinkedIn, competitor battle card, email sequence

## Multi-Tenancy

Every org gets its own isolated data scope. All queries are filtered by `organization_id`. Feature flags (team analytics, bulk research, API access, etc.) are stored as JSONB on the `organizations` table and enforced server-side.

## License

MIT
