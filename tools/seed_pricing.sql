-- ============================================================================
-- Token ledger: pricing seed + schema (SPEC-token-ledger.md §3)
--
--  ⚠  PLACEHOLDER PRICES — VERIFY BEFORE TRUSTING ANY COST.  The numbers
--     below are plausible snapshots for planning only.  Provider pricing
--     pages change; re-check every row against the provider's published
--     prices before using the dashboard for real decisions, and keep this
--     file updated by hand (deliberate: no auto-fetch, no drift surprise).
--
--  ⚠  The `provider` column must match what HIVEMIND RECORDS, not the
--     brand you bought from.  Hivemind stores the DETECTED PROFILE NAME of
--     the upstream it forwards to (SPEC D2): api.deepseek.com / api.kimi.com
--     / api.z.ai / api.anthropic.com ALL detect as the "Anthropic" profile,
--     so rows recorded for those hosts carry provider='Anthropic'.  The
--     model name is what disambiguates which real service the row came from
--     (deepseek-chat only rides DeepSeek, claude-sonnet-4-* only Anthropic,
--     kimi-* only Moonshot).  Models are NEVER priced by guesswork — a
--     (provider, model) pair with no row here costs NULL, which the
--     dashboard shows as uncosted rather than inventing a number.
--
--     Reconcile against reality before seeding:
--         SELECT DISTINCT provider, model FROM mesh_telemetry.token_usage;
--
-- Idempotent: safe to run repeatedly (IF NOT EXISTS / ON CONFLICT DO NOTHING).
-- Run with:  psql "$MESH_TELEMETRY_DSN" -f tools/seed_pricing.sql
--
-- ============================================================================
-- SCHEMA SECTION — MIRROR ONLY.  SOURCE OF TRUTH:
--   src/hivemind/telemetry/ledger.py  (_SCHEMA_DDL)
-- Hivemind applies that DDL itself on every connect, so running the schema
-- below by hand is never required; it lives here for operators reading or
-- pricing the ledger from psql.  If this block and _SCHEMA_DDL ever
-- disagree, _SCHEMA_DDL wins — fix this file, don't fork the DDL.
--
-- tokens_in semantics (billing invariant): FRESH (uncached) input tokens
-- for EVERY provider.  Rows are normalized AT INGEST — OpenAI-shape
-- providers report prompt_tokens INCLUDING cached_tokens, and hivemind
-- subtracts cache_read before the row is written — so the view's fresh
-- term is simply tokens_in.  Do NOT subtract cache terms here: with
-- fresh-only rows that double-subtracts and goes NEGATIVE on all-cached
-- traffic.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS mesh_telemetry;

CREATE TABLE IF NOT EXISTS mesh_telemetry.token_usage (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent_hash   TEXT NOT NULL,          -- hivemind rate-limit bucket (already hashed)
    provider     TEXT NOT NULL,          -- observed (detect_provider profile name)
    model        TEXT NOT NULL,          -- observed from the request body
    tokens_in    BIGINT,                 -- FRESH input tokens for every provider (ingest
                                         -- invariant: total-shape providers are normalized
                                         -- at record time; see ledger module docstring)
    tokens_out   BIGINT,
    cache_read   BIGINT,
    cache_write  BIGINT,
    reasoning    BIGINT,
    latency_ms   DOUBLE PRECISION,
    status       INTEGER NOT NULL,
    conversation_hash TEXT               -- sha256[:16] of the client session header
                                         -- (x-claude-code-session-id et al); NULL = none sent
);

CREATE INDEX IF NOT EXISTS token_usage_ts_idx ON mesh_telemetry.token_usage (ts);

-- Existing databases predate the column; CREATE TABLE IF NOT EXISTS does
-- not touch them, so this ALTER is the migration.
ALTER TABLE mesh_telemetry.token_usage ADD COLUMN IF NOT EXISTS conversation_hash TEXT;

CREATE TABLE IF NOT EXISTS mesh_telemetry.model_pricing (
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    price_in          DOUBLE PRECISION,  -- USD per 1M tokens
    price_cache_read  DOUBLE PRECISION,
    price_cache_write DOUBLE PRECISION,
    price_out         DOUBLE PRECISION,
    PRIMARY KEY (provider, model)
);

-- Migration guard: the view is SELECT u.*, so it freezes the table's
-- column list at creation time.  When the ALTER above adds a column,
-- CREATE OR REPLACE cannot reconcile the old shape — PG refuses to
-- rename a view column ("cannot change name of view column
-- cost_usd to conversation_hash").  Drop the view exactly when the
-- shapes mismatch; the REPLACE below then recreates it.
DO $$ BEGIN
  IF (SELECT count(*) FROM information_schema.columns
       WHERE table_schema = 'mesh_telemetry' AND table_name = 'usage_cost')
     <> (SELECT count(*) FROM information_schema.columns
          WHERE table_schema = 'mesh_telemetry' AND table_name = 'token_usage') + 1
  THEN
    EXECUTE 'DROP VIEW IF EXISTS mesh_telemetry.usage_cost';
  END IF;
END $$;

CREATE OR REPLACE VIEW mesh_telemetry.usage_cost AS
SELECT u.*,
       CASE WHEN p.provider IS NULL THEN NULL
            ELSE round(CAST((
                    coalesce(u.tokens_in, 0)    * coalesce(p.price_in, 0)
                  + coalesce(u.cache_read, 0)  * coalesce(p.price_cache_read, 0)
                  + coalesce(u.cache_write, 0) * coalesce(p.price_cache_write, 0)
                  + coalesce(u.tokens_out, 0)  * coalesce(p.price_out, 0)
                ) / 1e6 AS numeric), 6)
           END AS cost_usd
FROM mesh_telemetry.token_usage u
LEFT JOIN mesh_telemetry.model_pricing p
  ON p.provider = u.provider AND p.model = u.model;

-- ---------------------------------------------------------------------------
-- Pricing rows (USD per 1M tokens).  Verify against provider pages; cache
-- prices are 0 where the provider does not publish a distinct one.
-- ---------------------------------------------------------------------------

-- Anthropic — Claude Sonnet 4 (provider string is the detected profile name;
-- verify: SELECT DISTINCT provider FROM mesh_telemetry.token_usage)
INSERT INTO mesh_telemetry.model_pricing
    (provider, model, price_in, price_cache_read, price_cache_write, price_out)
VALUES
    ('Anthropic', 'claude-sonnet-4-20250514', 3.00, 0.30, 3.75, 15.00)
ON CONFLICT (provider, model) DO NOTHING;

-- DeepSeek chat/reasoner ride the "Anthropic" profile too (api.deepseek.com
-- detects as ANTHROPIC — see note at the top of this file)
INSERT INTO mesh_telemetry.model_pricing
    (provider, model, price_in, price_cache_read, price_cache_write, price_out)
VALUES
    ('Anthropic', 'deepseek-chat',      0.27, 0.07, 0.27, 1.10),
    ('Anthropic', 'deepseek-reasoner',  0.55, 0.14, 0.55, 2.19)
ON CONFLICT (provider, model) DO NOTHING;

-- Moonshot Kimi coding plan (api.kimi.com detects as ANTHROPIC)
INSERT INTO mesh_telemetry.model_pricing
    (provider, model, price_in, price_cache_read, price_cache_write, price_out)
VALUES
    ('Anthropic', 'kimi-for-coding', 0.60, 0.15, 0.60, 2.50)
ON CONFLICT (provider, model) DO NOTHING;

-- Local Ollama models (only recorded when hivemind itself forwards to a
-- local Ollama upstream, provider 'Ollama (local)') — priced at zero: the
-- electricity is the cost, and "free" must be an explicit 0, not a guess.
INSERT INTO mesh_telemetry.model_pricing
    (provider, model, price_in, price_cache_read, price_cache_write, price_out)
VALUES
    ('Ollama (local)', 'llama3.2:1b',     0, 0, 0, 0),
    ('Ollama (local)', 'llama3.2:3b',     0, 0, 0, 0),
    ('Ollama (local)', 'qwen3.5:4b',      0, 0, 0, 0),
    ('Ollama (local)', 'gemma4:e4b',      0, 0, 0, 0),
    ('Ollama (local)', 'qwen3-embedding', 0, 0, 0, 0)
ON CONFLICT (provider, model) DO NOTHING;
