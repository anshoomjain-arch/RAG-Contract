"""Embedding provider interface and the Bedrock Titan Text Embeddings V2 implementation.

Model ID and dimension come from the environment (EMBEDDING_MODEL_ID, EMBEDDING_DIMENSIONS, BEDROCK_REGION).
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol

from shared.llm import _require


@dataclass
class Embedding:
    vector: list[float]
    input_tokens: int


class EmbeddingProvider(Protocol):
    model_id: str
    dimensions: int

    def embed(self, text: str) -> Embedding: ...


class TitanEmbeddingProvider:
    """Titan V2 takes one text per request, so batches run as parallel requests. One boto3 client per thread
    (see BedrockConverseProvider)."""

    MAX_CHARS = 50_000   # Titan V2 input limit (8K tokens / 50K characters)

    def __init__(self, model_id: str | None = None, dimensions: int | None = None, region: str | None = None):
        self.model_id = model_id or _require("EMBEDDING_MODEL_ID")
        self.dimensions = dimensions or int(_require("EMBEDDING_DIMENSIONS"))
        self.region = region or _require("BEDROCK_REGION")
        self._local = threading.local()
        self._lock = threading.Lock()

    @property
    def client(self):
        client = getattr(self._local, "client", None)
        if client is None:
            import boto3
            from botocore.config import Config

            with self._lock:
                client = boto3.session.Session().client(
                    "bedrock-runtime", region_name=self.region,
                    config=Config(retries={"max_attempts": 8, "mode": "adaptive"}, read_timeout=60))
            self._local.client = client
        return client

    def embed(self, text: str) -> Embedding:
        body = {"inputText": text[:self.MAX_CHARS], "dimensions": self.dimensions, "normalize": True}
        resp = self.client.invoke_model(modelId=self.model_id, body=json.dumps(body),
                                        contentType="application/json", accept="application/json")
        out = json.loads(resp["body"].read())
        return Embedding(out["embedding"], out.get("inputTextTokenCount", 0))


def embed_many(provider: EmbeddingProvider, texts: list[str], workers: int = 4) -> list[Embedding]:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(provider.embed, texts))
