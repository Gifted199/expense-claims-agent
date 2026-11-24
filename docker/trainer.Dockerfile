# One-off anomaly-model trainer (runs as an ECS Fargate task). Build for linux/amd64:
#   docker build --platform linux/amd64 --build-arg GIT_SHA=$(git rev-parse HEAD) \
#     -t <account>.dkr.ecr.eu-west-2.amazonaws.com/akinsds1126-expense-trainer:<sha> \
#     -f docker/trainer.Dockerfile .
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 1) Dependencies only (cached until pyproject/uv.lock change). No dev tools in the image.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-default-groups --group ml --no-install-project

# 2) Project code and the public RDS CA bundle (no .env, no .git; see .dockerignore).
COPY src ./src
COPY certs ./certs
RUN uv sync --locked --no-dev --no-default-groups --group ml --no-editable

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA} \
    PATH="/app/.venv/bin:${PATH}" \
    EXPENSE_DB_CA_BUNDLE=/app/certs/rds-global-bundle.pem \
    EXPENSE_AWS_PROFILE="" \
    AWS_REGION=eu-west-2

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin trainer
USER 10001

ENTRYPOINT ["python", "-m", "expense_agent.ml.train"]
