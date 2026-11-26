# Streamlit UI for the app task: the only exposed port (8501). No secrets, no certs; it only
# knows EXPENSE_API_URL (D45). Build for linux/amd64:
#   docker build --platform linux/amd64 --build-arg GIT_SHA=$(git rev-parse HEAD) \
#     -t 666127452756.dkr.ecr.eu-west-2.amazonaws.com/akinsds1126-expense-ui:<sha> \
#     -f docker/ui.Dockerfile .
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-default-groups --group ui --no-install-project

COPY src ./src
COPY .streamlit ./.streamlit
RUN uv sync --locked --no-dev --no-default-groups --group ui --no-editable

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA} \
    PATH="/app/.venv/bin:${PATH}" \
    HOME=/tmp \
    EXPENSE_API_URL=http://127.0.0.1:8000

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001

EXPOSE 8501
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "expense_agent.healthcheck", "http://127.0.0.1:8501/_stcore/health"]
ENTRYPOINT ["python", "-m", "streamlit", "run", "/app/src/expense_agent/ui/app.py", \
    "--server.address", "0.0.0.0", "--server.port", "8501", "--server.headless", "true"]
