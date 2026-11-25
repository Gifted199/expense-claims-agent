"""Azure OpenAI access for embeddings (decision D29).

Credentials come from the Secrets Manager secret ``akinsds1126-expense-azure-openai`` and are
never logged. The endpoint is normalised to the host root, in case a full "Target URI"
(``.../openai/deployments/<name>/embeddings?api-version=...``) was stored by mistake.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

DEFAULT_AZURE_SECRET_ID = "akinsds1126-expense-azure-openai"  # noqa: S105 - a *name*
EMBEDDING_DIMENSIONS = 1536  # fits vector(1536) and the HNSW index for both -small and -large
BATCH_SIZE = 64


def normalise_endpoint(url: str) -> str:
    """Keep only ``https://<host>/``. Rejects anything that is not an https URL with a host."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Azure OpenAI endpoint must be an https URL with a host name")
    host = parsed.hostname
    port = f":{parsed.port}" if parsed.port else ""
    return f"https://{host}{port}/"


@dataclass(frozen=True)
class AzureSettings:
    endpoint: str
    api_version: str
    chat_deployment: str
    embed_deployment: str
    api_key: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            f"AzureSettings(endpoint={self.endpoint!r}, api_version={self.api_version!r}, "
            f"chat_deployment={self.chat_deployment!r}, "
            f"embed_deployment={self.embed_deployment!r}, api_key='***')"
        )

    @classmethod
    def from_secret(cls, secret: dict[str, Any]) -> AzureSettings:
        return cls(
            endpoint=normalise_endpoint(str(secret["AZURE_OPENAI_ENDPOINT"])),
            api_version=str(secret["AZURE_OPENAI_API_VERSION"]),
            chat_deployment=str(secret["AZURE_OPENAI_CHAT_DEPLOYMENT"]),
            embed_deployment=str(secret["AZURE_OPENAI_EMBED_DEPLOYMENT"]),
            api_key=str(secret["AZURE_OPENAI_API_KEY"]),
        )


def load_settings(session: Any, secret_id: str | None = None) -> AzureSettings:
    from expense_agent.db.connection import get_secret_json

    sid = secret_id or os.environ.get("EXPENSE_AZURE_SECRET_ID", DEFAULT_AZURE_SECRET_ID)
    return AzureSettings.from_secret(get_secret_json(session, sid))


class Embedder:
    """Embeds texts with a named deployment. Always requests 1536 dimensions (D27)."""

    def __init__(self, settings: AzureSettings, client: Any | None = None) -> None:
        self.settings = settings
        if client is None:
            from openai import AzureOpenAI  # lazy: keeps tests and other images free of it

            client = AzureOpenAI(
                azure_endpoint=settings.endpoint,
                api_key=settings.api_key,
                api_version=settings.api_version,
                max_retries=5,  # the SDK backs off on 429 / 5xx
                timeout=60,
            )
        self.client = client

    def embed(self, texts: Sequence[str], deployment: str) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            batch = list(texts[start : start + BATCH_SIZE])
            response = self.client.embeddings.create(
                model=deployment, input=batch, dimensions=EMBEDDING_DIMENSIONS
            )
            data = sorted(response.data, key=lambda d: d.index)
            if len(data) != len(batch):
                raise RuntimeError(f"expected {len(batch)} embeddings, got {len(data)}")
            for item in data:
                if len(item.embedding) != EMBEDDING_DIMENSIONS:
                    raise RuntimeError(f"unexpected embedding size {len(item.embedding)}")
                vectors.append(list(item.embedding))
        return vectors
