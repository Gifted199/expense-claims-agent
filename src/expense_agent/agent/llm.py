"""Azure OpenAI chat model from the akinsds1126-expense-azure-openai secret (Bedrock is blocked)."""

from __future__ import annotations

from typing import Any

from expense_agent.rag.azure import AzureSettings, load_settings


def chat_model(settings: AzureSettings) -> Any:
    from langchain_openai import AzureChatOpenAI

    return AzureChatOpenAI(
        azure_endpoint=settings.endpoint,
        api_key=settings.api_key,  # type: ignore[arg-type]
        api_version=settings.api_version,
        azure_deployment=settings.chat_deployment,
        max_retries=3,
        timeout=60,
    )


def chat_model_from_session(session: Any) -> Any:
    return chat_model(load_settings(session))
