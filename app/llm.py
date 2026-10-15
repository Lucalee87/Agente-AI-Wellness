"""Chiamata a Claude con gli strumenti dell'agente (usata dalla chat e dal simulatore)."""

import anthropic

from .config import settings
from .voice.tools import TOOL_DEFINITIONS

TOOLS = [
    {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
    for t in TOOL_DEFINITIONS
]

_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def ask_claude(system: str, messages: list) -> anthropic.types.beta.BetaMessage:
    return client().beta.messages.create(
        model=settings.llm_model,
        max_tokens=16000,
        system=system,
        tools=TOOLS,
        messages=messages,
        # Risposte brevi e rapide, come in una conversazione
        output_config={"effort": "low"},
        # Se il modello rifiuta una richiesta, l'API riprova su un modello di riserva
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
