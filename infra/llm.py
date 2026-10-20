"""LLM wrapper: one signature for every role, one logged record per logical call.

Provider-neutral. Today this module fixes the call signature, the record shape,
the retry/repair policy and the vocabularies, and ships a scripted MockBackend
for offline work. The GeminiBackend is a documented stub; the working backend
with measured usage lands with the "working wrapper" item (Tue 20).

Plain dicts at module boundaries, standard library only (Josh's convention).
"""

import json
import time

ROLES = ("detective", "suspect", "interpreter", "extractor", "annotator")
PURPOSES = ("question", "accusation", "interrogation_response", "invitation", "acceptance",
            "conversation_message", "conversation_reply", "intent_interpretation",
            "ledger_extraction", "annotation")
# The contradiction report (proposal 5.3) rides on the first accusation call; it is not a purpose (decisions.md D2).
SINGLETON_AGENTS = ("detective", "interpreter", "extractor", "annotator")
SUSPECT_AGENT_IDS = tuple(f"suspect_{i}" for i in range(1, 6))
# decisions.md D3: stable agent IDs across games; suspects are numbered in the case's public suspect order.
AGENT_IDS = ("detective",) + SUSPECT_AGENT_IDS + ("interpreter", "extractor", "annotator")


def suspect_agent_ids(suspects):
    """{suspect name: "suspect_<i>"} in public order (decisions.md D3), the controller's agent_ids_for mapping."""
    names = list(suspects)
    if len(names) != len(SUSPECT_AGENT_IDS):
        raise ValueError(f"Expected {len(SUSPECT_AGENT_IDS)} suspects, got {len(names)}")
    return dict(zip(names, SUSPECT_AGENT_IDS))
# Proposal 5.2: cost through the first accusation, retry cost and post-game cost are reported separately.
PHASES = ("through_first_accusation", "accusation_retries", "post_game_analysis")
ATTEMPT_KINDS = ("initial", "transport_retry", "format_repair")
ATTEMPT_STATUSES = ("ok", "transport_error", "format_error")
CALL_STATUSES = ("ok", "transport_failure", "format_failure")
MESSAGE_ROLES = ("system", "user", "assistant")

REPAIR_INSTRUCTION = ("Your previous reply was not valid JSON for the required schema. "
                      "Return only a JSON value that matches the schema: no prose, no code fences.")

USAGE_KEYS = ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")


class TransportError(Exception):
    """Retryable provider failure: network error, timeout, rate limit, 5xx."""


class LLMCallError(Exception):
    """Bounded retries/repairs exhausted. The failed record was already logged.

    Controllers catch this, log a `failure` event and end the game as a failed run.
    Never regenerate testimony to work around it.
    """

    def __init__(self, record):
        super().__init__(f"{record['status']} for {record['role']}/{record['purpose']} ({record['call_id']})")
        self.record = record


def empty_usage():
    return {key: 0 for key in USAGE_KEYS}


def normalise_usage(usage):
    """Backend usage -> full usage dict; total is derived when the backend omits it."""
    usage = dict(usage or {})
    result = {key: int(usage.get(key, 0) or 0) for key in USAGE_KEYS}
    if not usage.get("total_tokens"):
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"] + result["reasoning_tokens"]
    return result


def add_usage(total, usage):
    return {key: total[key] + usage[key] for key in USAGE_KEYS}


def estimate_tokens(text):
    """Rough offline estimate for mocks only; never report it as measured usage."""
    return max(1, len(text) // 4) if text else 0


def cost_usd(usage, pricing):
    """USD cost from per-million-token prices; None when no pricing is configured."""
    if not pricing:
        return None
    cost = (usage["input_tokens"] * pricing["input_per_million"]
            + usage["output_tokens"] * pricing["output_per_million"]
            + usage["reasoning_tokens"] * pricing.get("reasoning_per_million", pricing["output_per_million"]))
    return cost / 1_000_000


def strip_code_fences(text):
    stripped = text.strip()
    if stripped.startswith("```"):
        first_break = stripped.find("\n")
        stripped = stripped[first_break + 1:] if first_break != -1 else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip()


_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def check_shape(value, schema, path="$"):
    """Minimal structural check: type, required, properties, additionalProperties, enum, items.

    This is deliberately not a full JSON Schema validator. It is enough for the
    finite structured outputs in this project (accusation triples, identity-only
    invitations, accept/reject decisions, ledger entry lists). Returns error strings.
    """
    errors = []
    expected = schema.get("type")
    if expected is not None:
        allowed = expected if isinstance(expected, list) else [expected]
        if not any(_TYPE_CHECKS[name](value) for name in allowed):
            errors.append(f"{path}: expected type {expected}")
            return errors
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, dict):
        for key in schema.get("required", ()):
            if key not in value:
                errors.append(f"{path}: missing required key {key!r}")
        properties = schema.get("properties", {})
        for key, sub in properties.items():
            if key in value:
                errors.extend(check_shape(value[key], sub, f"{path}.{key}"))
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}: unexpected key {key!r}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(check_shape(item, schema["items"], f"{path}[{index}]"))
    return errors


def parse_structured(text, schema):
    """(parsed, errors): strip fences, load JSON, check the shape."""
    candidate = strip_code_fences(text)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        starts = [i for i in (candidate.find("{"), candidate.find("[")) if i != -1]
        ends = [i for i in (candidate.rfind("}"), candidate.rfind("]")) if i != -1]
        if not starts or not ends:
            return None, [f"not JSON: {exc.msg}"]
        try:
            parsed = json.loads(candidate[min(starts):max(ends) + 1])
        except json.JSONDecodeError as inner:
            return None, [f"not JSON: {inner.msg}"]
    errors = check_shape(parsed, schema)
    return (parsed if not errors else None), errors


class MockBackend:
    """Scripted offline backend. No network, no cost.

    script: a list consumed in order (str -> text, dict/list -> JSON text,
    Exception instance -> raised, so TransportError tests retries), or a callable
    request -> str. With no script every call returns a fixed placeholder.
    Usage is a character-count estimate so cost code can be exercised offline.
    """

    def __init__(self, script=None, usage_estimator=estimate_tokens):
        self.script = list(script) if isinstance(script, (list, tuple)) else script
        self.estimate = usage_estimator
        self.requests = []

    def generate(self, *, model_id, messages, decoding, response_schema=None, max_output_tokens=None):
        request = {"model_id": model_id, "messages": messages, "decoding": decoding,
                   "response_schema": response_schema, "max_output_tokens": max_output_tokens}
        self.requests.append(request)
        if callable(self.script):
            item = self.script(request)
        elif self.script is None:
            item = "[mock response]"
        elif self.script:
            item = self.script.pop(0)
        else:
            raise RuntimeError("Mock script exhausted")
        if isinstance(item, BaseException):
            raise item
        text = item if isinstance(item, str) else json.dumps(item)
        prompt_text = "\n".join(message["content"] for message in messages)
        return {"text": text, "finish_reason": "stop",
                "usage": {"input_tokens": self.estimate(prompt_text),
                          "output_tokens": self.estimate(text), "reasoning_tokens": 0}}


class GeminiBackend:
    """Stub for the google-genai SDK; generate() is implemented on Tue 20.

    Planned mapping (so callers can rely on the record shape now):
      messages[system]            -> GenerateContentConfig.system_instruction
      messages[user|assistant]    -> contents with roles "user" / "model"
      decoding.temperature/top_p/seed, max_output_tokens -> GenerateContentConfig
      response_schema             -> response_mime_type="application/json" + response_schema
      usage.input_tokens          <- usage_metadata.prompt_token_count
      usage.output_tokens         <- usage_metadata.candidates_token_count
      usage.reasoning_tokens      <- usage_metadata.thoughts_token_count (billed thinking)
      finish_reason               <- candidates[0].finish_reason
      TransportError              <- APIError with 429/5xx, connection and timeout errors
    The API key is read from the environment, never from a config file in the repo.
    """

    def __init__(self, api_key_env="GEMINI_API_KEY"):
        self.api_key_env = api_key_env
        self._client = None

    def generate(self, *, model_id, messages, decoding, response_schema=None, max_output_tokens=None):
        raise NotImplementedError("GeminiBackend.generate lands with the working wrapper (Tue 20); "
                                  "use MockBackend for offline development")


class LLMClient:
    """The one entry point every role uses. Each call() writes exactly one `llm_call` event.

    backend: object with generate(model_id=, messages=, decoding=, response_schema=, max_output_tokens=)
             returning {"text", "usage": {input_tokens, output_tokens, reasoning_tokens}, "finish_reason"}
             and raising TransportError for retryable failures.
    model_id, decoding: fixed for the whole experiment and copied onto every record.
    log: an object with emit(kind, **payload), normally infra.eventlog.EventLog; None disables logging.
    pricing: {"input_per_million", "output_per_million", optional "reasoning_per_million", "basis"}.
    clock/sleep: injectable for deterministic tests and examples.

    Detective decisions pass response_schema=None (decisions.md D1): the controller parses
    them with detective.parse_decision and owns the single repair. The wrapper's own
    format repair applies to calls that do pass a schema (extraction, invitation, acceptance).
    """

    def __init__(self, backend, model_id, decoding, log=None, pricing=None,
                 max_transport_retries=3, max_format_repairs=1, backoff_s=(1, 2, 4),
                 clock=time.monotonic, sleep=time.sleep):
        if not isinstance(decoding, dict):
            raise ValueError("decoding must be a dict of fixed decoding settings")
        if pricing is not None and not {"input_per_million", "output_per_million"} <= set(pricing):
            raise ValueError("pricing needs input_per_million and output_per_million")
        self.backend = backend
        self.model_id = model_id
        self.decoding = dict(decoding)
        self.log = log
        self.pricing = dict(pricing) if pricing else None
        self.max_transport_retries = max_transport_retries
        self.max_format_repairs = max_format_repairs
        self.backoff_s = tuple(backoff_s)
        self.clock = clock
        self.sleep = sleep
        self._counter = 0

    def next_call_id(self):
        self._counter += 1
        return f"c{self._counter:06d}"

    def call(self, *, role, purpose, phase, messages, game_id=None, agent_id=None, response_schema=None,
             max_output_tokens=None, tags=None):
        """Run one logical call with bounded transport retries and one bounded format repair.

        agent_id is the stable persona across games (decisions.md D3): suspect_1..suspect_5
        in the case's public suspect order for role "suspect" (required; see
        suspect_agent_ids), and the role itself for the singleton agents (detective,
        interpreter, extractor, annotator), which is the default when omitted.
        Returns the call record (also logged). Raises LLMCallError after logging when
        the bounds are exhausted; the record then has status transport_failure or
        format_failure and the caller must treat the game as a failed run.
        """
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        if purpose not in PURPOSES:
            raise ValueError(f"purpose must be one of {PURPOSES}")
        if phase not in PHASES:
            raise ValueError(f"phase must be one of {PHASES}")
        if agent_id is None and role != "suspect":
            agent_id = role
        if role == "suspect":
            if agent_id not in SUSPECT_AGENT_IDS:
                raise ValueError("suspect calls need agent_id suspect_1..suspect_5 in public suspect order "
                                 "(decisions.md D3); use suspect_agent_ids(public['suspects'])")
        elif agent_id != role:
            raise ValueError(f"agent_id for role {role!r} must be {role!r} (decisions.md D3)")
        messages = [dict(message) for message in messages]
        for message in messages:
            if message.get("role") not in MESSAGE_ROLES or not isinstance(message.get("content"), str):
                raise ValueError("messages must be [{'role': system|user|assistant, 'content': str}]")
        if max_output_tokens is None:
            max_output_tokens = self.decoding.get("max_output_tokens")

        record = {"call_id": self.next_call_id(), "game_id": game_id, "role": role, "agent_id": agent_id,
                  "purpose": purpose,
                  "phase": phase, "model_id": self.model_id, "decoding": dict(self.decoding),
                  "response_schema": response_schema, "max_output_tokens": max_output_tokens,
                  "messages": messages, "text": "", "parsed": None, "usage": empty_usage(),
                  "cost_usd": None, "finish_reason": None, "latency_ms": 0, "attempts": [],
                  "repair_used": False, "status": None, "tags": dict(tags or {})}
        current_messages = messages
        transport_errors = 0
        repairs = 0
        kind = "initial"
        while True:
            started = self.clock()
            attempt = {"attempt_no": len(record["attempts"]) + 1, "kind": kind, "status": None,
                       "usage": empty_usage(), "latency_ms": 0, "error": None}
            try:
                output = self.backend.generate(model_id=self.model_id, messages=current_messages,
                                               decoding=self.decoding, response_schema=response_schema,
                                               max_output_tokens=max_output_tokens)
            except TransportError as exc:
                attempt.update(status="transport_error", error=str(exc),
                               latency_ms=int((self.clock() - started) * 1000))
                record["attempts"].append(attempt)
                transport_errors += 1
                if transport_errors > self.max_transport_retries:
                    record["status"] = "transport_failure"
                    break
                self.sleep(self.backoff_s[min(transport_errors - 1, len(self.backoff_s) - 1)])
                kind = "transport_retry"
                continue
            attempt["latency_ms"] = int((self.clock() - started) * 1000)
            attempt["usage"] = normalise_usage(output.get("usage"))
            text = output.get("text", "") or ""
            record["text"] = text
            record["finish_reason"] = output.get("finish_reason")
            parsed, errors = (None, [])
            if response_schema is not None:
                parsed, errors = parse_structured(text, response_schema)
            if errors:
                attempt.update(status="format_error", error="; ".join(errors))
                record["attempts"].append(attempt)
                if repairs >= self.max_format_repairs:
                    record["status"] = "format_failure"
                    break
                repairs += 1
                current_messages = current_messages + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": REPAIR_INSTRUCTION + "\nSchema: " + json.dumps(response_schema)}]
                kind = "format_repair"
                continue
            attempt["status"] = "ok"
            record["attempts"].append(attempt)
            record["parsed"] = parsed
            record["status"] = "ok"
            break

        for attempt in record["attempts"]:
            record["usage"] = add_usage(record["usage"], attempt["usage"])
            record["latency_ms"] += attempt["latency_ms"]
        record["cost_usd"] = cost_usd(record["usage"], self.pricing)
        record["repair_used"] = repairs > 0
        if self.log is not None:
            self.log.emit("llm_call", **record)
        if record["status"] != "ok":
            raise LLMCallError(record)
        return record
