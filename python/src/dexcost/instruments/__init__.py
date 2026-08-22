"""Auto-instrumentation for LLM provider SDKs and MCP tool calls.

This package provides monkey-patching based instrumentation that
automatically captures LLM calls and MCP tool invocations within tracked tasks.
"""

from dexcost.instruments.anthropic import instrument_anthropic, uninstrument_anthropic
from dexcost.instruments.bedrock import instrument_bedrock, uninstrument_bedrock
from dexcost.instruments.cohere import instrument_cohere, uninstrument_cohere
from dexcost.instruments.fal import instrument_fal, uninstrument_fal
from dexcost.instruments.gemini import instrument_gemini, uninstrument_gemini
from dexcost.instruments.litellm import instrument_litellm, uninstrument_litellm
from dexcost.instruments.mcp import instrument_mcp, uninstrument_mcp
from dexcost.instruments.ollama import instrument_ollama, uninstrument_ollama
from dexcost.instruments.openai import instrument_openai, uninstrument_openai
from dexcost.instruments.openrouter import instrument_openrouter, uninstrument_openrouter
from dexcost.instruments.perplexity import (
    instrument_perplexity,
    uninstrument_perplexity,
)

__all__ = [
    "instrument_anthropic",
    "instrument_bedrock",
    "instrument_cohere",
    "instrument_fal",
    "instrument_gemini",
    "instrument_litellm",
    "instrument_mcp",
    "instrument_ollama",
    "instrument_openai",
    "instrument_openrouter",
    "instrument_perplexity",
    "uninstrument_anthropic",
    "uninstrument_bedrock",
    "uninstrument_cohere",
    "uninstrument_fal",
    "uninstrument_gemini",
    "uninstrument_litellm",
    "uninstrument_mcp",
    "uninstrument_ollama",
    "uninstrument_openai",
    "uninstrument_openrouter",
    "uninstrument_perplexity",
]
