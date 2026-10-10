"""Trace OpenAI fallback calls to LangSmith without storing page content."""

import os
from collections.abc import Mapping
from contextlib import nullcontext
from typing import Any


def _usage_only(outputs: Any) -> dict:
    if not isinstance(outputs, Mapping):
        return {}
    usage = outputs.get("usage_metadata") or outputs.get("usage")
    return {"usage_metadata": usage} if usage else {}


class LangSmithTracker:
    def __init__(self) -> None:
        self.enabled = os.getenv("LANGSMITH_TRACING", "").strip().casefold() == "true"
        self.project = os.getenv("LANGSMITH_PROJECT", "").strip() or "selfheal"
        self._client = None
        self._errors: list[Exception] = []
        if not self.enabled:
            return
        api_key = os.getenv("LANGSMITH_API_KEY", "").strip()
        if not api_key:
            raise ValueError("LANGSMITH_TRACING=true requires LANGSMITH_API_KEY")
        from langsmith import Client

        self._client = Client(
            api_key=api_key,
            hide_inputs=True,
            hide_outputs=_usage_only,
            tracing_error_callback=self._errors.append,
        )

    def wrap_openai(self, client):
        if not self.enabled:
            return client
        from langsmith.wrappers import wrap_openai

        return wrap_openai(client, tracing_extra={"client": self._client})

    def case_context(self, run_id: str, test_case_id: str, environment: str):
        if not self.enabled:
            return nullcontext()
        from langsmith import tracing_context

        return tracing_context(
            client=self._client,
            project_name=self.project,
            metadata={
                "suite_run_id": run_id,
                "test_case_id": test_case_id,
                "environment": environment,
            },
        )

    def flush(self) -> None:
        if not self.enabled:
            return
        self._client.flush(timeout=30)
        if self._errors:
            raise RuntimeError("LangSmith could not upload all traces; check the API key and endpoint")
