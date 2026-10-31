"""Small Gemini REST client with bounded retries and actionable, redacted errors.

API contract: https://ai.google.dev/api/generate-content#GenerationConfig
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import time
from email.utils import parsedate_to_datetime

import requests

from config.settings import GEMINI_MODEL

MAX_ATTEMPTS = 3
MAX_RETRY_WAIT = 60
MAX_RESPONSE_BYTES = 1_048_576
MAX_ERROR_BYTES = 16_384
TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}


class GeminiError(RuntimeError):
    """A request failed without disclosing credentials or raw service responses."""


def _error_detail(response: requests.Response, api_key: str) -> str:
    """Expose bounded provider diagnostics without logging credentials or raw bodies."""
    try:
        body = bytearray()
        for chunk in response.iter_content(chunk_size=4096):
            body.extend(chunk)
            if len(body) > MAX_ERROR_BYTES:
                return ""
        payload = json.loads(body)
        error = payload.get("error") if isinstance(payload, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        if not isinstance(message, str):
            return ""
    except (requests.RequestException, ValueError, UnicodeDecodeError):
        return ""
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    message = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "[REDACTED]", message)
    message = re.sub(r"(?i)([?&](?:key|api_key)=)[^\s&\"']+", r"\1[REDACTED]", message)
    return " ".join(message.split())[:600]


def _retry_delay(response: requests.Response | None, attempt: int) -> float:
    value = response.headers.get("Retry-After", "") if response is not None else ""
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            delay = None
    if delay is not None and math.isfinite(delay):
        if delay > MAX_RETRY_WAIT:
            raise GeminiError(
                f"Gemini requested a wait of {math.ceil(delay)} seconds. "
                "Retry later; automatic retries stop when the requested wait exceeds 60 seconds."
            )
        return max(delay, 0.0)
    base = min(5.0 * 2 ** attempt, 30.0)
    return base + random.uniform(0, base * 0.2)


def _extract_json(payload: object) -> object:
    if not isinstance(payload, dict):
        raise GeminiError("Gemini returned an invalid response object")
    candidates = payload.get("candidates", [])
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        raise GeminiError("Gemini returned no candidate (the request may have been blocked)")
    candidate = candidates[0]
    if candidate.get("finishReason") != "STOP":
        raise GeminiError("Gemini did not complete its response; try a different topic or model")
    content = candidate.get("content", {})
    parts = content.get("parts", []) if isinstance(content, dict) else []
    if not isinstance(parts, list):
        raise GeminiError("Gemini returned malformed content")
    text = "".join(part["text"] for part in parts
                   if isinstance(part, dict) and isinstance(part.get("text"), str)
                   and not part.get("thought"))
    # Tolerate fences from compatible models, but never try to salvage partial JSON.
    text = re.sub(r"\A```(?:json)?\s*|\s*```\Z", "", text.strip())
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        raise GeminiError("Gemini returned invalid JSON; try again or provide a script file") from None


def generate_json(prompt: str, *, schema: dict, system_instruction: str = "") -> object:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise GeminiError("Set GEMINI_API_KEY in .env or the environment, or provide a script file")
    model = GEMINI_MODEL.removeprefix("models/")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", model):
        raise GeminiError("GEMINI_MODEL must be a model ID such as gemini-3.8-flash")
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 8192},
    }
    if model.startswith("gemini-2."):
        # Gemini 2.x accepts responseFormat but can silently ignore its schema.
        # Keep the compatible generateContent fields for these older models.
        payload["generationConfig"].update(responseMimeType="application/json", responseJsonSchema=schema)
    else:
        # TextResponseFormat.mimeType is a protobuf enum, not a MIME string.
        payload["generationConfig"]["responseFormat"] = {"text": {"mimeType": "APPLICATION_JSON", "schema": schema}}
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    print(f"  Gemini text model: {model}", flush=True)
    for attempt in range(MAX_ATTEMPTS):
        response = None
        reason = "network connection failed"
        try:
            response = requests.post(
                url, headers={"x-goog-api-key": api_key}, json=payload,
                timeout=(10, 90), allow_redirects=False, stream=True,
            )
            status = response.status_code
            if status in TRANSIENT_STATUS:
                reason = f"HTTP {status}"
                if attempt == MAX_ATTEMPTS - 1:
                    detail = _error_detail(response, api_key)
                    diagnostic = f" {detail}" if detail else ""
                    problem = "quota or rate limit reached" if status == 429 else "service unavailable"
                    raise GeminiError(
                        f"Gemini {problem} (HTTP {status}, model {model}) after {MAX_ATTEMPTS} attempts."
                        f"{diagnostic} Retry the saved run later."
                    )
                delay = _retry_delay(response, attempt)
            elif status != 200:
                advice = {
                    400: "Check the request format and generation settings",
                    401: "Check GEMINI_API_KEY",
                    403: "Check GEMINI_API_KEY and API access",
                    404: "Check GEMINI_MODEL is available to your account",
                }.get(status, "Check Gemini API status and configuration")
                detail = _error_detail(response, api_key)
                diagnostic = f" {detail}" if detail else ""
                raise GeminiError(f"Gemini request failed (HTTP {status}).{diagnostic} {advice}")
            else:
                body = bytearray()
                for chunk in response.iter_content(chunk_size=65536):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise GeminiError("Gemini response exceeded the size limit")
                try:
                    decoded = json.loads(body)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    raise GeminiError("Gemini returned an invalid API response") from None
                return _extract_json(decoded)
        except requests.RequestException:
            if attempt == MAX_ATTEMPTS - 1:
                raise GeminiError("Could not reach Gemini after 3 attempts; check your network and retry") from None
            delay = _retry_delay(None, attempt)
        finally:
            if response is not None:
                response.close()
        print(f"  Gemini ({model}): {reason}; retry {attempt + 2}/{MAX_ATTEMPTS} in {delay:.1f}s.", flush=True)
        time.sleep(delay)
    raise GeminiError("Gemini request failed")  # Defensive; loop always returns or raises.
