"""
Shared LLM infrastructure for vault scripts.

Provides model configuration, API callers, and utilities used by
weekly_review_llm.py and llm_panel.py.
"""

import os
import sys
import time
import json

try:
    import requests
except ImportError:
    print("Error: 'requests' package required. Install with: pip install requests")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Model configuration
#
# Keys MUST match the H1 headings in AI notes exactly.
# Values: (provider, api_model_id)
# ---------------------------------------------------------------------------

MODELS = {
    "Gpt-5.4": ("openai", "gpt-5.4"),
    "gpt-5.4-pro": ("openai_responses", "gpt-5.4-pro"),
    "Claude-opus-4.6": ("anthropic", "claude-opus-4-6"),
    "Claude-sonnet-4.5": ("anthropic", "claude-sonnet-4-5-20250929"),
    "gemini-3.1-pro-preview": ("google", "gemini-3.1-pro-preview"),
}

# Panel defaults: all models except the expensive/slow pro model
PANEL_MODELS = {k: v for k, v in MODELS.items() if k != "gpt-5.4-pro"}

API_KEY_VARS = {
    "openai": "OPENAI_API_KEY",
    "openai_responses": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
}

DEFAULT_TIMEOUT = 300       # 5 minutes
PRO_MODEL_TIMEOUT = 900     # 15 minutes


# ---------------------------------------------------------------------------
# API callers
# ---------------------------------------------------------------------------

def call_openai(model, system, user, api_key):
    """Standard OpenAI Chat Completions API."""
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_completion_tokens": 8192,
        },
        timeout=DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def call_openai_responses(model, system, user, api_key):
    """OpenAI Responses API (required for pro models)."""
    resp = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "instructions": system,
            "input": user,
        },
        timeout=PRO_MODEL_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    for item in data.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return content.get("text", "")

    return f"*Could not parse response. Raw output:*\n\n```json\n{json.dumps(data, indent=2)}\n```"


def call_anthropic(model, system, user, api_key):
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 8192,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"]


def call_google(model, system, user, api_key):
    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"Content-Type": "application/json"},
        params={"key": api_key},
        json={
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"maxOutputTokens": 8192},
        },
        timeout=DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"]


CALLERS = {
    "openai": call_openai,
    "openai_responses": call_openai_responses,
    "anthropic": call_anthropic,
    "google": call_google,
}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def call_model(heading, provider, model_id, system, user):
    """Call a single model. Returns (heading, response_text)."""
    api_key_var = API_KEY_VARS[provider]
    api_key = os.environ.get(api_key_var)
    if not api_key:
        return heading, f"*Skipped: {api_key_var} not set*"

    try:
        print(f"  {heading}: sending...", flush=True)
        start = time.time()
        response = CALLERS[provider](model_id, system, user, api_key)
        elapsed = time.time() - start
        print(f"  {heading}: done ({len(response):,} chars, {elapsed:.0f}s)", flush=True)
        return heading, response
    except requests.exceptions.HTTPError as e:
        body = ""
        if e.response is not None:
            try:
                body = e.response.json()
            except Exception:
                body = e.response.text[:500]
        return heading, f"*Error ({e.response.status_code}): {body}*"
    except Exception as e:
        return heading, f"*Error: {e}*"


def check_api_keys(models):
    """Print API key availability for the given models dict."""
    seen = set()
    for heading, (provider, _) in models.items():
        key_var = API_KEY_VARS[provider]
        if key_var not in seen:
            status = "set" if os.environ.get(key_var) else "MISSING"
            print(f"  {key_var}: {status}")
            seen.add(key_var)


def read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()
