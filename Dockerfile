FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CLAIMS_TRACE_DIR=/srv/traces
WORKDIR /srv

COPY --from=ghcr.io/astral-sh/uv:0.11.13 /uv /usr/local/bin/uv

# Dependencies come from pyproject.toml and uv.lock (anthropic, openai, fastapi, uvicorn, pydantic, python-dotenv).
# The optional Presidio group is left out: without it the parser and the fail-closed sweep still run.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY static ./static

# No API key is baked in. With SOP_LLM=fake the app runs without one; otherwise pass a key at run time.
EXPOSE 8000
CMD ["/srv/.venv/bin/uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8000"]
