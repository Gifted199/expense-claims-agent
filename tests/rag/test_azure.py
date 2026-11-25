"""Azure settings and embedding wrapper (no network)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from expense_agent.rag import azure as A

FAKE = "not-a-real-key"  # test value only


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://x.openai.azure.com/", "https://x.openai.azure.com/"),
        ("https://x.openai.azure.com", "https://x.openai.azure.com/"),
        (
            "https://x.openai.azure.com/openai/deployments/emb/embeddings?api-version=2025-04-01",
            "https://x.openai.azure.com/",
        ),
        (
            "  https://y.cognitiveservices.azure.com/openai  ",
            "https://y.cognitiveservices.azure.com/",
        ),
    ],
)
def test_normalise_endpoint(raw: str, expected: str) -> None:
    assert A.normalise_endpoint(raw) == expected


@pytest.mark.parametrize(
    "bad", ["http://x.openai.azure.com/", "x.openai.azure.com", "", "https://"]
)
def test_normalise_endpoint_rejects_non_https(bad: str) -> None:
    with pytest.raises(ValueError):
        A.normalise_endpoint(bad)


def settings() -> A.AzureSettings:
    return A.AzureSettings.from_secret(
        {
            "AZURE_OPENAI_ENDPOINT": "https://x.openai.azure.com/openai/deployments/a/chat",
            "AZURE_OPENAI_API_KEY": FAKE,
            "AZURE_OPENAI_API_VERSION": "2025-04-01-preview",
            "AZURE_OPENAI_CHAT_DEPLOYMENT": "gpt-5.4-mini",
            "AZURE_OPENAI_EMBED_DEPLOYMENT": "text-embedding-3-small",
        }
    )


def test_settings_from_secret_normalise_and_mask() -> None:
    s = settings()
    assert s.endpoint == "https://x.openai.azure.com/"
    assert FAKE not in repr(s) and FAKE not in str(s)


def fake_client(dim: int = 1536) -> MagicMock:
    client = MagicMock()

    def create(model: str, input: list[str], dimensions: int):  # noqa: A002 - SDK name
        data = [SimpleNamespace(index=i, embedding=[float(i)] * dim) for i in range(len(input))]
        return SimpleNamespace(data=list(reversed(data)))  # out of order on purpose

    client.embeddings.create.side_effect = create
    return client


def test_embed_batches_orders_and_requests_1536_dims() -> None:
    client = fake_client()
    out = A.Embedder(settings(), client=client).embed([f"t{i}" for i in range(130)], "dep")
    assert len(out) == 130
    assert client.embeddings.create.call_count == 3  # 64 + 64 + 2
    assert all(c.kwargs["dimensions"] == 1536 for c in client.embeddings.create.call_args_list)
    assert all(c.kwargs["model"] == "dep" for c in client.embeddings.create.call_args_list)
    assert out[0][0] == 0.0 and out[1][0] == 1.0  # re-ordered by index within the batch


def test_embed_rejects_wrong_dimension() -> None:
    with pytest.raises(RuntimeError, match="unexpected embedding size"):
        A.Embedder(settings(), client=fake_client(dim=3072)).embed(["t"], "dep")
