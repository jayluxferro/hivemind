"""Token ledger & cost dashboard (SPEC-token-ledger.md).

Hivemind is the only mesh layer that sees every cloud-bound request with
usage already parsed, so it is the sole ledger writer (SPEC D1): one row per
request recorded after the response completes, fail-open by design.
"""
