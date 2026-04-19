# Contributing

## Setup

```bash
git clone https://github.com/jayluxferro/hivemind.git
cd hivemind
pip install -e ".[dev]"
```

## Pre-commit (Ruff)

Optional but recommended so `ruff check` matches CI before you push:

```bash
pip install pre-commit
pre-commit install
pre-commit run --all-files   # once, to warm caches
```

Hooks are defined in `.pre-commit-config.yaml` (currently **Ruff** only; `evaluation/` and `paper/` are excluded so they stay aligned with `ruff check src/ tests/` in CI).

## Secret scanning (Gitleaks)

CI runs **[Gitleaks](https://github.com/gitleaks/gitleaks)** on every push and pull request (see `.github/workflows/ci.yml`), and the **publish** workflow runs it before building for PyPI. Paths under `tests/` and `evaluation/` are ignored via `.gitleaksignore` because fixtures can resemble production secrets.

To scan locally before committing (optional):

- Install a [Gitleaks release](https://github.com/gitleaks/gitleaks/releases) for your OS, then run `gitleaks protect --staged --verbose` in the repo root, or  
- Rely on CI.

The official Gitleaks **pre-commit** hook builds with Go; it is not enabled here so `pre-commit install` stays lightweight.

## Tests and lint

```bash
python -m pytest tests/ -v
ruff check src/ tests/
```

## Maintainer checklist (GitHub settings)

These cannot be enforced from git alone; configure them in the repository or organization settings:

1. **Branch protection** on `main`: require pull request before merge, dismiss stale reviews, and require status checks (**CI / test**, **Lint**, and optionally **Secret scan (Gitleaks)**) to pass before merge.
2. **Required reviewers** for sensitive paths (optional, via CODEOWNERS).
3. **Secret scanning** and **push protection** for registered patterns (GitHub Advanced Security or equivalent for private repos).

See also [SECURITY.md](SECURITY.md).

## CLI layout

- Shared **`hivemind proxy`** / **`hivemind-proxy`** flags live in **`src/hivemind/cli_args.py`** (`register_proxy_cli_arguments`, `hivemind_config_from_proxy_cli_args`).
- **`hivemind serve`** overlapping flags use **`register_serve_cli_arguments`** and **`apply_serve_cli_args_to_config`** in the same module.
- **`hivemind.proxy.cli`** re-exports the proxy helpers for backward compatibility.
