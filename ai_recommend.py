"""
ai_recommend.py - asks Ollama what it thinks should happen to an
agent-flagged file: archive, compress, or ignore.

IMPORTANT: this is advisory only. Whatever the model says gets
passed to policy_validator.validate_action() same as any other
proposed action - the model's recommendation never grants permission
by itself. If Ollama is unreachable, falls back to a simple
score-based rule so the pipeline still works with zero AI running.
"""

import requests

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "qwen2.5:3b"

VALID_RECOMMENDATIONS = {"archive", "compress", "ignore"}


def _fallback_recommendation(file: dict) -> str:
    """Used when Ollama isn't reachable - keeps the pipeline usable
    without AI. Same rule of thumb the scoring module already uses."""
    if file.get("type") == "log":
        return "compress"
    if file.get("risk") == "high":
        return "archive"
    return "ignore"


def ask_recommendation(file: dict) -> str:
    prompt = f"""You are a Linux storage optimization assistant.

Analyze this file:

Path: {file.get('path')}
Size: {file.get('size_mb')} MB
Age: {file.get('age_days')} days
Type: {file.get('type')}
Owner: {file.get('owner', 'unknown')}
Risk: {file.get('risk', 'unknown')}

Choose exactly one word: archive, compress, or ignore.
Reply with only that one word, nothing else."""

    try:
        response = requests.post(
            OLLAMA_URL,
            json={"model": MODEL_NAME, "prompt": prompt, "stream": False},
            timeout=20,
        )
        response.raise_for_status()
        answer = response.json().get("response", "").strip().lower()

        for word in VALID_RECOMMENDATIONS:
            if word in answer:
                return word

        return _fallback_recommendation(file)

    except requests.exceptions.RequestException:
        return _fallback_recommendation(file)
