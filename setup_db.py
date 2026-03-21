"""
LeadLens — setup_db.py
Run this once to create all tables in your local PostgreSQL database.
Usage: python setup_db.py
"""

import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)

if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set in .env file")
    exit(1)

print(f"Connecting to database...")

try:
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()
    print("Connected OK")
except Exception as e:
    print(f"Connection failed: {e}")
    exit(1)

steps = []

# 0. Drop all existing tables
steps.append(("Drop existing tables", """
    DROP TABLE IF EXISTS lead_notes CASCADE;
    DROP TABLE IF EXISTS reminders CASCADE;
    DROP TABLE IF EXISTS leads CASCADE;
    DROP TABLE IF EXISTS access_requests CASCADE;
    DROP TABLE IF EXISTS early_access CASCADE;
    DROP TABLE IF EXISTS invitation_codes CASCADE;
    DROP TABLE IF EXISTS users CASCADE;
    DROP TABLE IF EXISTS organizations CASCADE;
"""))

# 1. Organizations
steps.append(("Create organizations", """
    CREATE TABLE organizations (
        id                    SERIAL PRIMARY KEY,
        name                  TEXT        NOT NULL,
        slug                  TEXT        NOT NULL UNIQUE,
        plan_tier             TEXT        NOT NULL DEFAULT 'solo'
                              CHECK (plan_tier IN ('solo', 'team', 'enterprise')),
        features              JSONB       NOT NULL DEFAULT '{
            "crm_sync":        false,
            "team_analytics":  false,
            "bulk_research":   false,
            "api_access":      false,
            "site_hq_mapping": false,
            "buying_committee": false
        }',
        report_limit_per_user INTEGER     NOT NULL DEFAULT 10,
        created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
"""))

# 2. Users
steps.append(("Create users", """
    CREATE TABLE users (
        id              SERIAL PRIMARY KEY,
        organization_id INTEGER     NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        email           TEXT        NOT NULL UNIQUE,
        password_hash   TEXT        NOT NULL,
        invite_code     TEXT        NOT NULL,
        role            TEXT        NOT NULL DEFAULT 'member'
                        CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
        report_count    INTEGER     NOT NULL DEFAULT 0,
        report_limit    INTEGER     NOT NULL DEFAULT 10,
        is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
"""))

steps.append(("Index: users org", """
    CREATE INDEX idx_users_org ON users(organization_id)
"""))

# 3. Invitation codes
steps.append(("Create invitation_codes", """
    CREATE TABLE invitation_codes (
        id              SERIAL PRIMARY KEY,
        organization_id INTEGER     NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        code            TEXT        NOT NULL UNIQUE,
        used            BOOLEAN     NOT NULL DEFAULT FALSE,
        used_by         TEXT,
        expires_at      TIMESTAMPTZ,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
"""))

steps.append(("Index: invite codes org", """
    CREATE INDEX idx_invite_codes_org ON invitation_codes(organization_id)
"""))

# 4. Early access
steps.append(("Create early_access", """
    CREATE TABLE early_access (
        id         SERIAL PRIMARY KEY,
        email      TEXT        NOT NULL UNIQUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
"""))

# 5. Access requests
steps.append(("Create access_requests", """
    CREATE TABLE access_requests (
        id              SERIAL PRIMARY KEY,
        organization_id INTEGER     NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        user_id         INTEGER     NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        email           TEXT        NOT NULL,
        status          TEXT        NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'approved', 'denied')),
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        resolved_at     TIMESTAMPTZ
    )
"""))

# 6. Leads
steps.append(("Create leads", """
    CREATE TABLE leads (
        id               SERIAL PRIMARY KEY,
        organization_id  INTEGER     NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        user_id          INTEGER     NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        company          TEXT        NOT NULL,
        user_name        TEXT,
        user_role        TEXT        DEFAULT '',
        product          TEXT,
        scores           JSONB       DEFAULT '{}',
        fit_check        JSONB       DEFAULT '{}',
        signals          JSONB       DEFAULT '{}',
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
        sources          JSONB       DEFAULT '[]',
        sector           TEXT        DEFAULT ''
                         CHECK (sector IN ('', 'mining', 'cleantech', 'agtech', 'other')),
        site_location    TEXT        DEFAULT '',
        hq_location      TEXT        DEFAULT '',
        notes            TEXT        DEFAULT '',
        pipeline_stage   TEXT        NOT NULL DEFAULT 'new'
                         CHECK (pipeline_stage IN ('new', 'contacted', 'meeting', 'proposal', 'closed_won', 'closed_lost')),
        created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
"""))

steps.append(("Indexes: leads", """
    CREATE INDEX idx_leads_org      ON leads(organization_id);
    CREATE INDEX idx_leads_user     ON leads(user_id);
    CREATE INDEX idx_leads_org_user ON leads(organization_id, user_id);
    CREATE INDEX idx_leads_company  ON leads(company);
    CREATE INDEX idx_leads_sector   ON leads(sector);
    CREATE INDEX idx_leads_stage    ON leads(pipeline_stage);
    CREATE INDEX idx_leads_created  ON leads(created_at DESC)
"""))

# 7. Lead notes
steps.append(("Create lead_notes", """
    CREATE TABLE lead_notes (
        id              SERIAL PRIMARY KEY,
        organization_id INTEGER     NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        lead_id         INTEGER     NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
        user_id         INTEGER     NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        note            TEXT        NOT NULL,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
"""))

steps.append(("Indexes: lead_notes", """
    CREATE INDEX idx_notes_lead ON lead_notes(lead_id);
    CREATE INDEX idx_notes_org  ON lead_notes(organization_id)
"""))

# 8. Reminders
steps.append(("Create reminders", """
    CREATE TABLE reminders (
        id              SERIAL PRIMARY KEY,
        organization_id INTEGER     NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        user_id         INTEGER     NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        lead_id         INTEGER     REFERENCES leads(id) ON DELETE SET NULL,
        company         TEXT        DEFAULT '',
        note            TEXT        NOT NULL,
        due_date        DATE        NOT NULL,
        is_complete     BOOLEAN     NOT NULL DEFAULT FALSE,
        ai_suggested    BOOLEAN     NOT NULL DEFAULT FALSE,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
"""))

steps.append(("Indexes: reminders", """
    CREATE INDEX idx_reminders_user ON reminders(user_id);
    CREATE INDEX idx_reminders_org  ON reminders(organization_id);
    CREATE INDEX idx_reminders_date ON reminders(due_date)
"""))

# 9. RLS
steps.append(("Enable RLS", """
    ALTER TABLE organizations    ENABLE ROW LEVEL SECURITY;
    ALTER TABLE users            ENABLE ROW LEVEL SECURITY;
    ALTER TABLE invitation_codes ENABLE ROW LEVEL SECURITY;
    ALTER TABLE access_requests  ENABLE ROW LEVEL SECURITY;
    ALTER TABLE leads            ENABLE ROW LEVEL SECURITY;
    ALTER TABLE lead_notes       ENABLE ROW LEVEL SECURITY;
    ALTER TABLE reminders        ENABLE ROW LEVEL SECURITY
"""))

steps.append(("Create RLS helper function", """
    CREATE OR REPLACE FUNCTION current_org_id() RETURNS INTEGER AS $$
        SELECT NULLIF(current_setting('app.current_org_id', TRUE), '')::INTEGER;
    $$ LANGUAGE sql STABLE
"""))

steps.append(("Create RLS policies", """
    CREATE POLICY org_isolation        ON organizations    USING (id = current_org_id());
    CREATE POLICY users_org_isolation  ON users            USING (organization_id = current_org_id());
    CREATE POLICY invite_org_isolation ON invitation_codes USING (organization_id = current_org_id());
    CREATE POLICY access_req_isolation ON access_requests  USING (organization_id = current_org_id());
    CREATE POLICY leads_org_isolation  ON leads            USING (organization_id = current_org_id());
    CREATE POLICY notes_org_isolation  ON lead_notes       USING (organization_id = current_org_id());
    CREATE POLICY reminders_isolation  ON reminders        USING (organization_id = current_org_id())
"""))

# 10. updated_at trigger
steps.append(("Create updated_at trigger", """
    CREATE OR REPLACE FUNCTION set_updated_at()
    RETURNS TRIGGER AS $$
    BEGIN
        NEW.updated_at = NOW();
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
"""))

steps.append(("Attach triggers", """
    CREATE TRIGGER trg_orgs_updated_at
        BEFORE UPDATE ON organizations
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    CREATE TRIGGER trg_users_updated_at
        BEFORE UPDATE ON users
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    CREATE TRIGGER trg_leads_updated_at
        BEFORE UPDATE ON leads
        FOR EACH ROW EXECUTE FUNCTION set_updated_at()
"""))

# 11. Seed data
steps.append(("Seed admin organization", """
    INSERT INTO organizations (name, slug, plan_tier, features, report_limit_per_user)
    VALUES (
        'LeadLens Platform',
        'leadlens-admin',
        'enterprise',
        '{"crm_sync": true, "team_analytics": true, "bulk_research": true, "api_access": true, "site_hq_mapping": true, "buying_committee": true}',
        9999
    )
"""))

# ── Run all steps ─────────────────────────────────────────────
print("\nRunning migration...\n")
failed = False
for label, sql in steps:
    try:
        cur.execute(sql)
        print(f"  OK  {label}")
    except Exception as e:
        print(f"  FAIL {label}: {e}")
        failed = True
        break

if not failed:
    print("\n✓ Migration complete! Verifying tables...\n")
    cur.execute("""
        SELECT tablename, rowsecurity
        FROM pg_tables
        WHERE schemaname = 'public'
        ORDER BY tablename
    """)
    rows = cur.fetchall()
    print(f"  {'Table':<25} {'RLS'}")
    print(f"  {'-'*25} {'-'*5}")
    for row in rows:
        print(f"  {row[0]:<25} {'ON' if row[1] else 'OFF'}")
    print(f"\n  Total tables: {len(rows)}")
    print("\n✓ LeadLens database is ready.\n")
else:
    print("\nMigration failed. Fix the error above and run again.\n")

cur.close()
conn.close()