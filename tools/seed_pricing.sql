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
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS mesh_telemetry;

CREATE TABLE IF NOT EXISTS mesh_telemetry.token_usage (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent_hash   TEXT NOT NULL,          -- hivemind rate-limit bucket (already hashed)
    provider     TEXT NOT NULL,          -- observed (detect_provider profile name)
    model        TEXT NOT NULL,          -- observed from the request body
    tokens_in    BIGINT,                 -- all optional: providers vary
    tokens_out   BIGINT,
    cache_read   BIGINT,
    cache_write  BIGINT,
    reasoning    BIGINT,
    latency_ms   DOUBLE PRECISION,
    status       INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS token_usage_ts_idx ON mesh_telemetry.token_usage (ts);

CREATE TABLE IF NOT EXISTS mesh_telemetry.model_pricing (
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    price_in          DOUBLE PRECISION,  -- USD per 1M tokens
    price_cache_read  DOUBLE PRECISION,
    price_cache_write DOUBLE PRECISION,
    price_out         DOUBLE PRECISION,
    PRIMARY KEY (provider, model)
);

CREATE OR REPLACE VIEW mesh_telemetry.usage_cost AS
SELECT u.*,
       CASE WHEN p.provider IS NULL THEN NULL
            ELSE round(CAST((
              (coalesce(u.tokens_in, 0) - coalesce(u.cache_write, 0) - coalesce(u.cache_read, 0))
                * coalesce(p.price_in, 0)
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
