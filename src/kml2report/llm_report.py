"""LLM-powered formal report generator.

Takes a field-notes Markdown summary and sends it to an LLM (OpenAI,
Anthropic, Ollama, Hugging Face Inference API, or Cloudflare Workers AI)
with a system prompt tuned for producing a polished, professional CRO/HLD
field report.

Provider is selected via the ``provider`` argument:
  - ``openai``      — requires ``OPENAI_API_KEY`` env var
  - ``anthropic``   — requires ``ANTHROPIC_API_KEY`` env var
  - ``ollama``      — requires a running Ollama instance (default model: llama3)
  - ``huggingface`` — requires ``HF_TOKEN`` env var (included with HF Pro)
  - ``cloudflare``  — requires ``CF_API_TOKEN`` and ``CF_ACCOUNT_ID`` env vars
"""

from __future__ import annotations

import os
from typing import Any

SYSTEM_PROMPT = """\
You are a senior telecommunications outside-plant (OSP) engineer writing a
formal field assessment report for a fiber-optic HLD/CRO project.

You will receive raw field notes that were collected during a windshield
survey / pole audit.  Your job is to transform them into a polished,
professional report suitable for delivery to a client, project manager,
or permitting authority.

Follow these rules:
1. Use clear, professional language — no slang, no first-person.
2. Organize the report with numbered sections:
   - Executive Summary
   - Project Area Description (use the coordinates and road names provided)
   - Access Assessment (ROW vs private property, easement requirements)
   - Pole & Infrastructure Assessment (condition, existing attachers, make-ready)
   - Construction Method Recommendations (aerial strand, plow, bore — by corridor)
   - Obstacles & Hazards
   - Permitting Requirements
   - Recommendations & Next Steps
3. Reference specific coordinates, pole IDs, road names, and distances
   from the source data so the reader can cross-reference the KML map.
4. Where the field notes mention approximate distances inside fence lines
   (e.g. "4-6 feet inside fence"), include those measurements.
5. Summarize patterns — don't just repeat every note verbatim.  Group
   similar observations and call out the most critical items.
6. Flag any items that require immediate action (permits, easements,
   safety concerns) in a clearly marked section.
7. Output the report in clean Markdown format.
"""


def generate_formal_report(
    summary_md: str,
    provider: str = "openai",
    model: str | None = None,
    temperature: float = 0.3,
    base_url: str | None = None,
) -> str:
    """Send the field-notes summary to an LLM and return the formal report.

    Parameters
    ----------
    summary_md : str
        The Markdown field-notes summary produced by the summarizer.
    provider : str
        One of ``"openai"``, ``"anthropic"``, ``"ollama"``, ``"huggingface"``,
        or ``"cloudflare"``.
    model : str | None
        Model name override.  Defaults per provider:
        openai → gpt-4o, anthropic → claude-sonnet-4-20250514,
        ollama → llama3, cloudflare → @cf/meta/llama-3.1-8b-instruct.
    temperature : float
        Sampling temperature (lower = more deterministic).
    base_url : str | None
        Custom API base URL (useful for OpenAI-compatible proxies or
        non-default Ollama hosts).

    Returns
    -------
    str
        The formal report in Markdown.
    """
    provider = provider.lower().strip()

    if provider == "openai":
        return _call_openai(summary_md, model or "gpt-4o", temperature, base_url)
    elif provider == "anthropic":
        return _call_anthropic(summary_md, model or "claude-sonnet-4-20250514", temperature, base_url)
    elif provider == "ollama":
        return _call_ollama(summary_md, model or "llama3", temperature, base_url)
    elif provider == "huggingface":
        return _call_huggingface(
            summary_md,
            model or "meta-llama/Llama-3.3-70B-Instruct",
            temperature,
            base_url,
        )
    elif provider == "cloudflare":
        return _call_cloudflare(
            summary_md,
            model or "@cf/meta/llama-3.1-8b-instruct",
            temperature,
            base_url,
        )
    else:
        raise ValueError(
            f"Unknown LLM provider '{provider}'. "
            f"Choose from: openai, anthropic, ollama, huggingface, cloudflare"
        )


# ------------------------------------------------------------------
# OpenAI
# ------------------------------------------------------------------

def _call_openai(
    summary_md: str,
    model: str,
    temperature: float,
    base_url: str | None,
) -> str:
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError(
            "openai package not installed. Run:\n"
            "  pip install openai"
        )

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "OPENAI_API_KEY environment variable is not set.\n"
            "Set it with:  set OPENAI_API_KEY=sk-..."
        )

    client = OpenAI(api_key=api_key, base_url=base_url)
    response = client.chat.completions.create(
        model=model,
        temperature=temperature,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": (
                "Below are the raw field notes summary for this project. "
                "Please produce the formal report.\n\n"
                + summary_md
            )},
        ],
    )
    return response.choices[0].message.content or ""


# ------------------------------------------------------------------
# Anthropic
# ------------------------------------------------------------------

def _call_anthropic(
    summary_md: str,
    model: str,
    temperature: float,
    base_url: str | None,
) -> str:
    try:
        from anthropic import Anthropic
    except ImportError:
        raise ImportError(
            "anthropic package not installed. Run:\n"
            "  pip install anthropic"
        )

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY environment variable is not set.\n"
            "Set it with:  set ANTHROPIC_API_KEY=sk-ant-..."
        )

    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url

    client = Anthropic(**kwargs)
    response = client.messages.create(
        model=model,
        max_tokens=8192,
        temperature=temperature,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": (
                "Below are the raw field notes summary for this project. "
                "Please produce the formal report.\n\n"
                + summary_md
            )},
        ],
    )
    return response.content[0].text if response.content else ""


# ------------------------------------------------------------------
# Ollama (local)
# ------------------------------------------------------------------

def _call_ollama(
    summary_md: str,
    model: str,
    temperature: float,
    base_url: str | None,
) -> str:
    try:
        from ollama import Client
    except ImportError:
        raise ImportError(
            "ollama package not installed. Run:\n"
            "  pip install ollama\n"
            "Also ensure Ollama is running locally with your chosen model."
        )

    host = base_url or "http://localhost:11434"
    client = Client(host=host)
    response = client.chat(
        model=model,
        options={"temperature": temperature},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": (
                "Below are the raw field notes summary for this project. "
                "Please produce the formal report.\n\n"
                + summary_md
            )},
        ],
    )
    return response["message"]["content"] or ""


# ------------------------------------------------------------------
# Hugging Face Inference API
# ------------------------------------------------------------------

def _call_huggingface(
    summary_md: str,
    model: str,
    temperature: float,
    base_url: str | None,
) -> str:
    try:
        from huggingface_hub import InferenceClient
    except ImportError:
        raise ImportError(
            "huggingface_hub package not installed. Run:\n"
            "  pip install huggingface_hub"
        )

    token = os.environ.get("HF_TOKEN")
    if not token:
        raise EnvironmentError(
            "HF_TOKEN environment variable is not set.\n"
            "Set it with:  set HF_TOKEN=hf_..."
        )

    client = InferenceClient(model=model, token=token, base_url=base_url)
    response = client.chat_completion(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": (
                "Below are the raw field notes summary for this project. "
                "Please produce the formal report.\n\n"
                + summary_md
            )},
        ],
        temperature=temperature,
        max_tokens=8192,
    )
    return response.choices[0].message.content or ""


# ------------------------------------------------------------------
# Cloudflare Workers AI
# ------------------------------------------------------------------

def _call_cloudflare(
    summary_md: str,
    model: str,
    temperature: float,
    base_url: str | None,
) -> str:
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError(
            "openai package not installed. Run:\n"
            "  pip install openai"
        )

    api_token = os.environ.get("CF_API_TOKEN")
    if not api_token:
        raise EnvironmentError(
            "CF_API_TOKEN environment variable is not set.\n"
            "Set it with:  set CF_API_TOKEN=<your-cloudflare-api-token>"
        )

    account_id = os.environ.get("CF_ACCOUNT_ID")
    if not account_id:
        raise EnvironmentError(
            "CF_ACCOUNT_ID environment variable is not set.\n"
            "Set it with:  set CF_ACCOUNT_ID=<your-cloudflare-account-id>"
        )

    cf_base_url = base_url or (
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"
    )

    client = OpenAI(api_key=api_token, base_url=cf_base_url)
    response = client.chat.completions.create(
        model=model,
        temperature=temperature,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": (
                "Below are the raw field notes summary for this project. "
                "Please produce the formal report.\n\n"
                + summary_md
            )},
        ],
    )
    return response.choices[0].message.content or ""
