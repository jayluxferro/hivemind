# Security

## Reporting

Please report vulnerabilities by opening a private security advisory on GitHub or contacting the maintainers listed in `pyproject.toml`.

## TLS

By default the proxy verifies upstream HTTPS certificates (`http_tls_verify=true` in config). Use `--insecure` / `hm.config` `http_tls_verify: false` only for local development (for example, self-signed certificates on `127.0.0.1`).

## Secrets and CI

- Do not commit API keys, `.env` files, or PEM material; `.gitignore` lists common patterns.
- Pull requests and **PyPI publish** workflows run **Gitleaks** (see `.github/workflows/ci.yml` and `publish.yml`). Paths under `tests/` and `evaluation/` are listed in `.gitleaksignore` because fixtures can resemble production formats.
- Optional local **Ruff** checks run via **pre-commit** (see `CONTRIBUTING.md` and `.pre-commit-config.yaml`). Enable **GitHub secret scanning** (and push protection) in repository settings for hosted repos.

## Supply chain

Install from PyPI or a pinned git tag. Prefer `pip install hivemind-scheduler` with hash pinning in production lockfiles.
