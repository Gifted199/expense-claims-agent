# Agent API (FastAPI + LangChain agent) for the app task. Binds 127.0.0.1:8000: only the UI
# container in the same task can reach it (D48). Demo auth comes from EXPENSE_DEMO_AUTH.
#   docker build --platform linux/amd64 --build-arg GIT_SHA=$(git rev-parse HEAD) \
#     -t 666127452756.dkr.ecr.eu-west-2.amazonaws.com/akinsds1126-expense-agent:<sha> \
#     -f docker/agent.Dockerfile .
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-default-groups --group agent --no-install-project

COPY src ./src
COPY certs ./certs
RUN uv sync --locked --no-dev --no-default-groups --group agent --no-editable

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA} \
    PATH="/app/.venv/bin:${PATH}" \
    EXPENSE_DB_CA_BUNDLE=/app/certs/rds-global-bundle.pem \
    EXPENSE_AWS_PROFILE="" \
    AWS_REGION=eu-west-2

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001

HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-m", "expense_agent.healthcheck", "http://127.0.0.1:8000/health"]
ENTRYPOINT ["python", "-m", "expense_agent.api", "--host", "127.0.0.1", "--port", "8000"]
