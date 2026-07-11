"""OpenAI-compatible chat client with Databricks-native defaults.

One thin client covers every provider that speaks the OpenAI chat-completions
protocol (Databricks Foundation Model serving, Anthropic/OpenAI gateways,
OpenRouter, ...). Configuration resolves in order:

1. Explicit constructor arguments.
2. ``TABLESPEC_LLM_BASE_URL`` / ``TABLESPEC_LLM_API_KEY`` / ``TABLESPEC_LLM_MODEL``.
3. On a Databricks runtime: the workspace's own serving endpoint
   (``https://<workspace>/serving-endpoints``) with ambient notebook auth and
   the :data:`DEFAULT_DATABRICKS_MODEL` pay-per-token endpoint -- zero config
   in a notebook.

The ``openai`` package is the only dependency and is imported lazily, so the
base install stays LLM-free (``pip install 'tablespec[llm]'`` adds it).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

_ENV_BASE_URL = "TABLESPEC_LLM_BASE_URL"
_ENV_API_KEY = "TABLESPEC_LLM_API_KEY"
_ENV_MODEL = "TABLESPEC_LLM_MODEL"

#: Databricks pay-per-token Foundation Model endpoint used when nothing else
#: is configured on a Databricks runtime.
DEFAULT_DATABRICKS_MODEL = "databricks-claude-sonnet-4"


class LlmConfigError(RuntimeError):
    """Raised when no usable LLM endpoint configuration can be resolved."""


def _databricks_defaults() -> tuple[str, str] | None:
    """(base_url, api_key) from the ambient Databricks context, or None."""
    if "DATABRICKS_RUNTIME_VERSION" not in os.environ:
        return None
    try:  # notebook/repl context (present on interactive clusters)
        from dbruntime.databricks_repl_context import get_context  # pyright: ignore[reportMissingImports]

        ctx = get_context()
        if ctx is not None and ctx.apiUrl and ctx.apiToken:
            return f"{ctx.apiUrl}/serving-endpoints", ctx.apiToken
    except Exception:  # noqa: BLE001 - any failure just falls through
        pass
    try:  # jobs / SDK-configured contexts
        from databricks.sdk import WorkspaceClient  # pyright: ignore[reportMissingImports]

        cfg = WorkspaceClient().config
        host = (cfg.host or "").rstrip("/")
        if host and cfg.token:
            return f"{host}/serving-endpoints", cfg.token
    except Exception:  # noqa: BLE001
        pass
    return None


def _strip_fences(text: str) -> str:
    """Drop a surrounding ```json ... ``` fence if the model added one."""
    stripped = text.strip()
    match = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", stripped, flags=re.DOTALL)
    return match.group(1) if match else stripped


def extract_json(text: str) -> Any:
    """Parse the JSON object/array in an LLM response (fences tolerated)."""
    cleaned = _strip_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Fall back to the outermost {...} or [...] span.
        for open_ch, close_ch in ("{}", "[]"):
            start, end = cleaned.find(open_ch), cleaned.rfind(close_ch)
            if start != -1 and end > start:
                try:
                    return json.loads(cleaned[start : end + 1])
                except json.JSONDecodeError:
                    continue
    msg = f"LLM response is not parseable JSON:\n{text[:2000]}"
    raise ValueError(msg)


class LlmClient:
    """Chat-completions client for spec enrichment."""

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        temperature: float = 0.1,
    ) -> None:
        base_url = base_url or os.environ.get(_ENV_BASE_URL)
        api_key = api_key or os.environ.get(_ENV_API_KEY)
        model = model or os.environ.get(_ENV_MODEL)

        if not (base_url and api_key):
            ambient = _databricks_defaults()
            if ambient is not None:
                base_url, api_key = base_url or ambient[0], api_key or ambient[1]
                model = model or DEFAULT_DATABRICKS_MODEL

        if not (base_url and api_key and model):
            msg = (
                "No LLM endpoint configured. Set "
                f"{_ENV_BASE_URL} / {_ENV_API_KEY} / {_ENV_MODEL} (any "
                "OpenAI-compatible endpoint), pass model=/base_url=/api_key=, "
                "or run on a Databricks runtime (workspace serving endpoint + "
                "notebook auth are used automatically)."
            )
            raise LlmConfigError(msg)

        try:
            from openai import OpenAI  # pyright: ignore[reportMissingImports]
        except ImportError as exc:  # pragma: no cover - import guard
            msg = (
                "The 'openai' package is required for LLM enrichment: "
                "pip install 'tablespec[llm]'"
            )
            raise LlmConfigError(msg) from exc

        self.model = model
        self.temperature = temperature
        self._client = OpenAI(base_url=base_url, api_key=api_key)

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        """One chat completion; returns the assistant text."""
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        response = self._client.chat.completions.create(
            model=self.model,
            messages=messages,  # pyright: ignore[reportArgumentType]
            temperature=self.temperature,
        )
        content = response.choices[0].message.content
        return content or ""

    def complete_json(self, prompt: str, *, system: str | None = None) -> Any:
        """One chat completion parsed as JSON (markdown fences tolerated)."""
        return extract_json(self.complete(prompt, system=system))


__all__ = [
    "DEFAULT_DATABRICKS_MODEL",
    "LlmClient",
    "LlmConfigError",
    "extract_json",
]
