# Repository Guidelines

## Project Structure & Module Organization
- Entry point: `coinTrade2.py` (modes: `basic`, `volatility`, `volume`, `rsi`, `rsi_trend`).
- Helpers: `price_source.py`, `kakao_notify.py`, `fg_index.py`, `discord_notify.py`.
- Config and assets: `config/config.yml`, `config/banner.txt`, secrets in `config/.env` (not committed).
- Logs and state: created under `logs/` at runtime (e.g., `logs/state.json`).
- Scripts: `trade.sh` (Unix-like environments). README is minimal; prefer this guide.

## Setup, Run, and Development Commands
- Create env (PowerShell): `python -m venv .venv; .\\.venv\\Scripts\\Activate.ps1`.
- Install deps: `pip install pyupbit pyyaml python-dotenv requests wcwidth`.
- Run locally: `python coinTrade2.py --mode basic` (or `volatility|volume|rsi|rsi_trend`).
- Config path: uses `config/config.yml` and optional `config/.env` for keys.
- Unix helper: `./trade.sh` may wrap typical run parameters.

## Coding Style & Naming Conventions
- Python, 4-space indentation; prefer type hints and dataclasses (see `Config`).
- Modules/files: `snake_case.py`; functions/variables: `snake_case`; constants: `UPPER_SNAKE` (e.g., `BASE_DIR`).
- Keep modules cohesive; isolate network/IO; use `logging` (avoid `print`).
- Lines ≤ 100 chars; clear, descriptive names reflecting trading logic.

## Testing Guidelines
- Framework: `pytest` (suggested). Place tests in `tests/` as `test_*.py` (e.g., `tests/test_price_source.py`).
- Run: `pytest -q` (mock network/Upbit calls; avoid hitting real APIs).
- Aim for coverage on strategy decisions, config parsing, and error handling.

## Commit & Pull Request Guidelines
- Commit style observed: `[YYYY.MM.DD] - Author, summary` (Korean/English OK). Example: `[2025.10.28] - 김건우, rsi_trend 로직 수정`.
- Subject: imperative; include scope (e.g., `[rsi_trend]`, `fg_index`).
- PRs must include: concise summary, rationale, affected modes, run command(s), config keys touched, and before/after logs or screenshots.

## Security & Configuration Tips
- Do not commit secrets; store tokens/keys in `config/.env` (loaded via `python-dotenv`).
- Keep `config/config.yml` minimal; document defaults; rotate keys if leaked.
- Scrub `logs/` before sharing; avoid persisting PII or full credentials.
