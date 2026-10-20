"""Aniroodh's infrastructure handoff: LLM wrapper signature and per-game event log.

See infra/README.md. Standard library only, except GeminiBackend which imports google-genai on first use.
"""

from .llm import (LLMClient, MockBackend, GeminiBackend, TransportError, LLMCallError,
                  ROLES, PURPOSES, PHASES, ATTEMPT_KINDS, CALL_STATUSES, AGENT_IDS, SUSPECT_AGENT_IDS,
                  suspect_agent_ids)
from .pricing import pricing_for, PRICES, PRICING_BASIS
from .eventlog import (EventLog, read_events, validate_events, trace_prefix_before_first_accusation,
                       to_annotation_exchanges, summarise_cost, REQUIRED_KEYS, KINDS, CONDITIONS)

__all__ = ["LLMClient", "MockBackend", "GeminiBackend", "TransportError", "LLMCallError",
           "ROLES", "PURPOSES", "PHASES", "ATTEMPT_KINDS", "CALL_STATUSES", "AGENT_IDS", "SUSPECT_AGENT_IDS",
           "suspect_agent_ids", "pricing_for", "PRICES", "PRICING_BASIS",
           "EventLog", "read_events", "validate_events", "trace_prefix_before_first_accusation",
           "to_annotation_exchanges", "summarise_cost", "REQUIRED_KEYS", "KINDS", "CONDITIONS"]
