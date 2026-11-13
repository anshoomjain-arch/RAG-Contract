"""LLM provider interface and the Bedrock (Converse API) implementation.

Model ID and region always come from the environment (LLM_MODEL_ID, BEDROCK_REGION), never from code.
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from typing import Iterator, Protocol

try:  # local development convenience; harmless in Lambda
    from dotenv import find_dotenv, load_dotenv
    load_dotenv(find_dotenv(".env.local", usecwd=True))
except ImportError:
    pass

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


@dataclass
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int


class LLMProvider(Protocol):
    model_id: str

    def complete(self, system: str, user: str, max_tokens: int = 512, temperature: float = 0.0) -> LLMResult: ...

    def stream(self, system: str, user: str, max_tokens: int = 1024, temperature: float = 0.0,
               usage: "Usage | None" = None) -> Iterator[str]: ...


class BedrockConverseProvider:
    """One boto3 client per thread: boto3 sessions are not thread-safe, and on Windows (Python 3.13,
    OpenSSL 3.0) concurrent TLS handshakes through one client's shared SSL context crash the process."""

    def __init__(self, model_id: str | None = None, region: str | None = None):
        self.model_id = model_id or _require("LLM_MODEL_ID")
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
                    config=Config(retries={"max_attempts": 8, "mode": "adaptive"}, read_timeout=120))
            self._local.client = client
        return client

    def complete(self, system: str, user: str, max_tokens: int = 512, temperature: float = 0.0) -> LLMResult:
        resp = self.client.converse(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
        )
        text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
        usage = resp.get("usage", {})
        return LLMResult(strip_thinking(text), usage.get("inputTokens", 0), usage.get("outputTokens", 0))

    def stream(self, system: str, user: str, max_tokens: int = 1024, temperature: float = 0.0,
               usage: "Usage | None" = None) -> Iterator[str]:
        """Yield answer text as it arrives (ConverseStream), with any <think> block removed."""
        resp = self.client.converse_stream(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
        )
        think = ThinkFilter()
        for event in resp["stream"]:
            if "contentBlockDelta" in event:
                text = think.feed(event["contentBlockDelta"]["delta"].get("text", ""))
                if text:
                    yield text
            elif "metadata" in event and usage:
                u = event["metadata"].get("usage", {})
                usage.add(LLMResult("", u.get("inputTokens", 0), u.get("outputTokens", 0)))
        rest = think.flush()
        if rest:
            yield rest


class ThinkFilter:
    """Streaming version of strip_thinking: drops a leading <think>...</think> block (Qwen3 may send an empty one
    even with /no_think) without holding back the rest of the answer."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.state, self.buf = "start", ""

    def feed(self, text: str) -> str:
        if self.state == "pass":
            return text
        if self.state == "trim":                           # whitespace after </think>, possibly in later chunks
            text = text.lstrip()
            if text:
                self.state = "pass"
            return text
        self.buf += text
        if self.state == "start":
            head = self.buf.lstrip()
            if len(head) < len(self.OPEN) and self.OPEN.startswith(head):
                return ""                                  # could still become <think>
            if not head.startswith(self.OPEN):
                self.state, out, self.buf = "pass", self.buf, ""
                return out
            self.state, self.buf = "think", head[len(self.OPEN):]
        if self.CLOSE not in self.buf:
            self.buf = self.buf[-len(self.CLOSE):]         # keep only what could be the start of </think>
            return ""
        out = self.buf.split(self.CLOSE, 1)[1].lstrip()
        self.state, self.buf = ("pass" if out else "trim"), ""
        return out

    def flush(self) -> str:
        out = self.buf if self.state == "start" else ""
        self.buf = ""
        return out


class Usage:
    """Thread-safe token and call counter for a run of LLM calls."""

    def __init__(self):
        self.calls = self.input_tokens = self.output_tokens = 0
        self._lock = threading.Lock()

    def add(self, r: LLMResult) -> None:
        with self._lock:
            self.calls += 1
            self.input_tokens += r.input_tokens
            self.output_tokens += r.output_tokens

    def cost(self, price_in_per_1k: float, price_out_per_1k: float) -> float:
        return self.input_tokens / 1000 * price_in_per_1k + self.output_tokens / 1000 * price_out_per_1k


def complete_json(provider: LLMProvider, system: str, user: str, validate, usage: Usage | None = None,
                  max_tokens: int = 1024, retries: int = 2, no_think: bool = True):
    """Ask for one JSON object, validate it, and retry with the validation error fed back (SPEC section 2).

    validate(data) returns (result, error). An error with a result is soft: the call is retried, but if no later
    reply is clean the last usable result is returned with its error. Returns (result, error); result is None only
    when no reply was usable. Exceptions from the provider (network, throttling after boto retries) propagate.
    """
    suffix = "\n/no_think" if no_think else ""
    prompt, err, fallback = user, None, None
    for _ in range(1 + retries):
        r = provider.complete(system, prompt + suffix, max_tokens=max_tokens)
        if usage:
            usage.add(r)
        m = re.search(r"\{.*\}", r.text, re.DOTALL)
        if not m:
            err = "No JSON object found."
        else:
            try:
                result, err = validate(json.loads(m.group(0)))
            except json.JSONDecodeError as e:
                err = f"Invalid JSON: {e}"
            else:
                if err is None:
                    return result, None
                if result is not None:
                    fallback = result
        prompt = f"{user}\n\nYour previous reply was invalid: {err} Reply again with valid JSON only."
    return fallback, err


def strip_thinking(text: str) -> str:
    """Qwen3 may emit <think>...</think>; it must never be parsed or shown."""
    text = THINK_RE.sub("", text)
    return text.split("</think>")[-1].strip()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set (see .env.example)")
    return value
