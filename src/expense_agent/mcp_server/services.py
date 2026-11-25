"""Cloud clients the tools use: RDS (per role), S3, Textract, Azure OpenAI and the model cache.

No local database, model or vector store: everything is a client of a cloud service. Database
credentials are read from Secrets Manager once per role and reused; connections are opened per
call (pooling is future work).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from expense_agent.db.connection import (
    AppRole,
    DbCredentials,
    DbSettings,
    connect_kwargs,
    get_app_credentials,
    make_session,
)
from expense_agent.ml.serving import ModelCache

DEFAULT_RAG_MODEL = "text-embedding-3-large"  # Phase 3 winner (docs/RAG.md)


def rag_model() -> str:
    return os.environ.get("EXPENSE_RAG_MODEL", DEFAULT_RAG_MODEL)


@dataclass
class Services:
    profile: str | None
    settings: DbSettings = field(default_factory=DbSettings.from_env)
    _session: Any = None
    _creds: dict[str, DbCredentials] = field(default_factory=dict)
    _clients: dict[str, Any] = field(default_factory=dict)
    _embedder: Any = None
    _model_cache: ModelCache | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def session(self) -> Any:
        with self._lock:
            if self._session is None:
                self._session = make_session(self.profile, self.settings.region)
            return self._session

    def client(self, name: str) -> Any:
        session = self.session
        with self._lock:
            if name not in self._clients:
                self._clients[name] = session.client(name)
            return self._clients[name]

    def _credentials(self, role: AppRole) -> DbCredentials:
        session = self.session
        with self._lock:
            if role not in self._creds:
                self._creds[role] = get_app_credentials(session, self.settings, role)
            return self._creds[role]

    @contextmanager
    def db(self, role: AppRole) -> Iterator[Any]:
        """An autocommit connection as ``readonly`` (query tools) or ``writer`` (workflow)."""
        import psycopg

        kwargs = connect_kwargs(self._credentials(role), self.settings)
        with psycopg.connect(**kwargs, autocommit=True) as conn:
            yield conn

    def embed(self, text: str) -> list[float]:
        from expense_agent.rag.azure import Embedder, load_settings

        session = self.session
        with self._lock:
            if self._embedder is None:
                self._embedder = Embedder(load_settings(session))
            embedder = self._embedder
        return embedder.embed([text], rag_model())[0]

    def model(self) -> Any:
        s3 = self.client("s3")
        with self._lock:
            if self._model_cache is None:
                self._model_cache = ModelCache(s3)
            cache = self._model_cache
        return cache.get()
