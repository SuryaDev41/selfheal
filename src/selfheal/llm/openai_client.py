"""OpenAI client used only when semantic and cached locators cannot resolve a control."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from openai import OpenAI

from selfheal.telemetry.langsmith import LangSmithTracker

from .openai_pricing import estimate_cost_usd

LOGGER = logging.getLogger(__name__)


class LocatorResponseError(ValueError):
    """The model response could not be interpreted as a locator suggestion."""


class OpenAILLMClient:
    """Request structured locator suggestions from OpenAI."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-3.5-turbo",
        *,
        langsmith: LangSmithTracker | None = None,
        max_retries: int = 3,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAI API key is required")
        self.langsmith = langsmith if langsmith is not None else LangSmithTracker()
        self.client = self.langsmith.wrap_openai(OpenAI(api_key=api_key))
        self.model = model
        self.max_retries = max_retries

    def identify_element(
        self,
        page_tree: str,
        target: str,
        confidence_threshold: float = 0.85,
    ) -> dict[str, Any]:
        """Return a structured candidate locator for the requested page control."""
        prompt = self._build_prompt(page_tree, target, confidence_threshold)
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            try:
                started_at = time.monotonic()
                response = self.client.chat.completions.create(
                    model=self.model,
                    max_tokens=500,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                )
                result = self._parse_response(response.choices[0].message.content or "{}")
                usage = response.usage
                input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
                result.update(
                    {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "total_tokens": input_tokens + output_tokens,
                        "model": self.model,
                        "latency_ms": int((time.monotonic() - started_at) * 1000),
                        "cost_usd": estimate_cost_usd(self.model, input_tokens, output_tokens),
                    }
                )
                return result
            except LocatorResponseError as error:
                last_error = error
            except Exception as error:
                last_error = error
                LOGGER.warning("OpenAI locator request failed on attempt %s: %s", attempt + 1, error)

            if attempt < self.max_retries - 1:
                time.sleep(2**attempt)

        raise RuntimeError(
            f"Could not identify {target!r} after {self.max_retries} attempts"
        ) from last_error

    @staticmethod
    def _build_prompt(page_tree: str, target: str, confidence_threshold: float) -> str:
        return f"""You identify controls for browser automation.

TARGET: {target}
VISIBLE CONTROLS:
{page_tree}

Return JSON only with this exact shape:
{{"found": true, "locator": "CSS or XPath selector", "confidence": 0.0}}

Rules:
- Select only a control present in VISIBLE CONTROLS.
- Return a selector that works directly with Playwright page.locator().
- Prefer stable standard CSS based on id, className, name, href, or type.
- Do not use jQuery syntax such as [text='...'], CSS text selectors, or prose.
- Use XPath only when standard CSS cannot identify exactly one visible control.
- Use found=false and locator=null when uncertain.
- Report confidence from 0 to 1. A usable answer should be at least {confidence_threshold:.2f}.
"""

    @staticmethod
    def _parse_response(response_text: str) -> dict[str, Any]:
        """Parse and normalize the model JSON response."""
        try:
            payload = json.loads(response_text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", response_text, re.DOTALL)
            if match is None:
                raise LocatorResponseError("No JSON object found in the model response") from None
            try:
                payload = json.loads(match.group())
            except json.JSONDecodeError as error:
                raise LocatorResponseError("Model response contains invalid JSON") from error

        if not isinstance(payload, dict):
            raise LocatorResponseError("Model response must be a JSON object")

        locator = payload.get("locator")
        if not isinstance(locator, str) or not locator.strip():
            locator = None
        try:
            confidence = float(payload.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = min(1.0, max(0.0, confidence))

        return {
            "found": bool(payload.get("found")) and locator is not None,
            "locator": locator,
            "confidence": confidence,
        }
