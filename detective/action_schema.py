"""Detective action schema and validation.

Defines what the detective LLM is allowed to output and validates each response
before the controller acts on it. There are four types of detective calls:

  1. Main decision: interrogate a suspect or make an accusation (culprit, weapon, location).
  2. Invitation response: accept or reject a suspect's conversation invite (§3.4).
  3. Conversation reply: send a bounded message back to a suspect who invited the detective (§3.4).
  4. Contradiction report: submitted alongside the first accusation only (§5.3).

Design notes:
I'm using Pydantic models internally to enforce structure and validate fields,
while accepting and returning plain dicts/JSON at the module boundaries to stay
consistent with Josh's casework convention (raw input in -> cleaned dict out).

The detective does not output explicit intents (overview, ask_lead, whereabouts,
corroborate, challenge). Instead, it asks a free-form question, which Harish's
interpreter then maps to an intent (§3.3).
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Union

from pydantic import (BaseModel, ConfigDict, Field, TypeAdapter, ValidationError,
                      ValidationInfo, field_validator, model_validator)

# ---------------------------------------------------------------------------
# Validation configuration.
# Initial character caps are starting baselines to keep prompts and outputs bounded;
# we can tune them based on pilot results (§3.5).
# ---------------------------------------------------------------------------
SCHEMA_CONFIG: dict[str, Any] = {
    # Keep detective questions focused and prevent context blowout.
    "max_question_chars": 500,
    # Limit direct conversation replies to a reasonable length.
    "max_reply_chars": 500,
    # Allow a brief reasoning string for logging and analysis, but keep it capped.
    # This rationale is never fed back into future prompts to avoid hidden scratchpads (§4.2).
    "max_rationale_chars": 300,
    # No upper bound on reported contradictions for now, letting the detective report everything it finds.
    "max_contradiction_pairs": None,
    # Output token cap for detective calls; placeholder until the pilot.
    "max_output_tokens": 800,
    # Proposal §3.5; mirrors controller ACCUSATION_BUDGET (detective must not import controller, decisions.md D7).
    "max_accusations": 3,
    # Output token cap for ledger extraction calls. pilot_01 extractor outputs were median 448 / max 585 tokens
    # vs a 600 cap, with one truncation + repair; pilot_02 lost exchange e19 to a failed extraction.
    # Revisit after the pilot.
    "max_extraction_tokens": 1200,
}


# ---------------------------------------------------------------------------
# External context required to validate decisions (allowed entities, turn budget, visible history).
# Constructed by the controller/memory module before each detective call.
# ---------------------------------------------------------------------------
class DecisionContext(BaseModel):
    """Per-call validation context containing only information visible to the detective."""

    model_config = ConfigDict(frozen=True)

    suspects: list[str]
    weapons: list[str]
    locations: list[str]
    # Map of exchange_id to exchange text for dialogues currently within the memory window.
    # In the no-memory condition, this only holds the latest exchange.
    visible_exchanges: dict[str, str] = Field(default_factory=dict)
    # Past (culprit, weapon, location) triples, used to ensure retries guess a distinct combination.
    previous_accusations: list[tuple[str, str, str]] = Field(default_factory=list)
    # Remaining interrogation turns. Once depleted (0), interrogation is locked and the detective must accuse (§3.5).
    turns_left: int
    config: dict[str, Any] = Field(default_factory=lambda: dict(SCHEMA_CONFIG))
    # Public case view (public_view output) used to render the detective prompt.
    public: dict | None = None

    @property
    def is_first_accusation(self) -> bool:
        return not self.previous_accusations

    @classmethod
    def from_public(cls, public: dict, **kwargs: Any) -> "DecisionContext":
        """Construct context from public case data, making sure ground truth isn't leaked."""
        return cls(suspects=list(public["suspects"]), weapons=list(public["weapons"]),
                   locations=list(public["locations"]), public=public, **kwargs)


def _ctx(info: ValidationInfo) -> DecisionContext:
    if not info.context or "ctx" not in info.context:
        raise ValueError("Internal error: validate through the parse_* functions, which supply context")
    return info.context["ctx"]


def _warn(info: ValidationInfo, message: str) -> None:
    info.context["warnings"].append(message)


def _canonical(value: str, allowed: list[str], label: str) -> str:
    """Normalize input to the canonical spelling case-insensitively so minor casing differences don't fail validation."""
    lookup = {name.casefold(): name for name in allowed}
    key = value.strip().casefold()
    if key not in lookup:
        raise ValueError(f"{label} {value!r} is not one of {allowed}")
    return lookup[key]


def _check_length(value: str | None, limit: int | None, label: str) -> str | None:
    if value is not None and limit is not None and len(value) > limit:
        raise ValueError(f"{label} is {len(value)} characters; the limit is {limit}")
    return value


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------
class _Strict(BaseModel):
    # Forbid unexpected fields so hallucinations or extra keys are caught immediately.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _HasRationale(_Strict):
    # Optional rationale field captured purely for telemetry/debugging.
    # The memory module must not feed this back into future prompts (§4.2).
    rationale: str | None = None

    @field_validator("rationale")
    @classmethod
    def _rationale_cap(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _check_length(value, _ctx(info).config["max_rationale_chars"], "rationale")


class Citation(_Strict):
    """One half of a contradiction report: the source exchange ID and the specific quote."""

    exchange_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)


class ContradictionPair(_Strict):
    # Pairs two conflicting statements. We cite quotes instead of raw claim IDs because
    # the detective LLM doesn't see internal claim IDs; evaluators map quotes to IDs post-hoc.
    left: Citation
    right: Citation


# ---------------------------------------------------------------------------
# 1. Main decision schemas (interrogate or accuse)
# ---------------------------------------------------------------------------
class InterrogateAction(_HasRationale):
    action: Literal["interrogate"]
    suspect: str
    question: str = Field(min_length=1)
    # Optional exchange IDs supporting the question. Passing these lets Harish populate
    # cited_claim_ids when mapping to a challenge intent without having to parse quotes from text.
    cites: list[str] = Field(default_factory=list)

    @field_validator("suspect")
    @classmethod
    def _suspect(cls, value: str, info: ValidationInfo) -> str:
        return _canonical(value, _ctx(info).suspects, "suspect")

    @field_validator("question")
    @classmethod
    def _question_cap(cls, value: str, info: ValidationInfo) -> str:
        return _check_length(value, _ctx(info).config["max_question_chars"], "question")

    @field_validator("cites")
    @classmethod
    def _cites_visible(cls, value: list[str], info: ValidationInfo) -> list[str]:
        # If an exchange ID is outside the current memory window, drop it with a warning
        # rather than rejecting the entire action.
        visible = _ctx(info).visible_exchanges
        kept: list[str] = []
        for exchange_id in value:
            if exchange_id in visible:
                if exchange_id not in kept:
                    kept.append(exchange_id)
            else:
                _warn(info, f"cites: dropped {exchange_id!r} (not in the current memory window)")
        return kept

    @model_validator(mode="after")
    def _budget_left(self, info: ValidationInfo) -> "InterrogateAction":
        # Once interrogation turns run out, only accusations are allowed (§3.5).
        if _ctx(info).turns_left <= 0:
            raise ValueError("turn budget exhausted: only 'accuse' is allowed now")
        return self


class AccuseAction(_HasRationale):
    action: Literal["accuse"]
    culprit: str
    weapon: str
    location: str
    # Bundled with the first accusation to save an extra LLM call.
    # Must be provided (even if empty) on the initial accusation, and ignored on retries.
    contradiction_report: list[ContradictionPair] | None = None

    @field_validator("culprit")
    @classmethod
    def _culprit(cls, value: str, info: ValidationInfo) -> str:
        return _canonical(value, _ctx(info).suspects, "culprit")

    @field_validator("weapon")
    @classmethod
    def _weapon(cls, value: str, info: ValidationInfo) -> str:
        return _canonical(value, _ctx(info).weapons, "weapon")

    @field_validator("location")
    @classmethod
    def _location(cls, value: str, info: ValidationInfo) -> str:
        return _canonical(value, _ctx(info).locations, "location")

    @model_validator(mode="after")
    def _accusation_rules(self, info: ValidationInfo) -> "AccuseAction":
        ctx = _ctx(info)
        # Accusation retries must guess a different combination (§3.5).
        if (self.culprit, self.weapon, self.location) in ctx.previous_accusations:
            raise ValueError("this triple was already accused; a retry must be a different triple")

        if not ctx.is_first_accusation:
            # Contradictions are only evaluated on the first accusation (§5.3); ignore any passed on retries.
            if self.contradiction_report is not None:
                _warn(info, "contradiction_report: ignored (only collected on the first accusation)")
                self.contradiction_report = None
            return self

        if self.contradiction_report is None:
            raise ValueError("contradiction_report is required on the first accusation "
                             "(use an empty list if you found no contradictions)")

        limit = ctx.config["max_contradiction_pairs"]
        if limit is not None and len(self.contradiction_report) > limit:
            raise ValueError(f"contradiction_report has {len(self.contradiction_report)} pairs; limit is {limit}")

        # If a quote doesn't match or cites an expired exchange, keep it but log a warning.
        # We don't discard or fix them here because evaluation explicitly tracks unsupported
        # detections (§5.3), and auto-repairing would artificially inflate precision scores.
        for index, pair in enumerate(self.contradiction_report):
            for side_name in ("left", "right"):
                side = getattr(pair, side_name)
                text = ctx.visible_exchanges.get(side.exchange_id)
                if text is None:
                    _warn(info, f"contradiction_report[{index}].{side_name}: exchange "
                                f"{side.exchange_id!r} not in memory window (kept, unsupported)")
                elif side.quote.casefold() not in text.casefold():
                    _warn(info, f"contradiction_report[{index}].{side_name}: quote not found in "
                                f"exchange {side.exchange_id!r} (kept, unsupported)")
        return self


DetectiveDecision = Annotated[Union[InterrogateAction, AccuseAction], Field(discriminator="action")]
_DECISION_ADAPTER: TypeAdapter = TypeAdapter(DetectiveDecision)


# ---------------------------------------------------------------------------
# 2. Invitation response (§3.4: boolean accept/reject without an extra message)
# ---------------------------------------------------------------------------
class InvitationResponse(_Strict):
    accept: bool


# ---------------------------------------------------------------------------
# 3. Conversation reply (§3.4: single bounded reply to an invite)
# ---------------------------------------------------------------------------
class ConversationReply(_Strict):
    message: str = Field(min_length=1)

    @field_validator("message")
    @classmethod
    def _reply_cap(cls, value: str, info: ValidationInfo) -> str:
        return _check_length(value, _ctx(info).config["max_reply_chars"], "message")


# ---------------------------------------------------------------------------
# Public parsing functions: take raw dict/JSON string and return a normalized result dict.
# ---------------------------------------------------------------------------
def _run(validate, raw: Any, ctx: DecisionContext) -> dict:
    """Validate raw LLM output against a schema model.

    Returns a standardized dictionary:
      - ok: whether validation succeeded.
      - action: dumped valid data, or None if invalid.
      - errors: reasons for failure (allowing one retry repair before failing per §3.5).
      - warnings: non-fatal issues logged for tracking and scoring.
    """
    warnings: list[str] = []
    context = {"ctx": ctx, "warnings": warnings}
    try:
        if isinstance(raw, (str, bytes)):
            raw = json.loads(raw)
        model = validate(raw, context)
    except json.JSONDecodeError as exc:
        return {"ok": False, "action": None, "errors": [f"output is not valid JSON: {exc}"], "warnings": []}
    except ValidationError as exc:
        errors = []
        for err in exc.errors():
            where = ".".join(str(part) for part in err["loc"]) or "output"
            errors.append(f"{where}: {err['msg']}")
        return {"ok": False, "action": None, "errors": errors, "warnings": warnings}
    return {"ok": True, "action": model.model_dump(), "errors": [], "warnings": warnings}


def parse_decision(raw: Any, ctx: DecisionContext) -> dict:
    return _run(lambda data, c: _DECISION_ADAPTER.validate_python(data, context=c), raw, ctx)


def parse_invitation_response(raw: Any, ctx: DecisionContext) -> dict:
    return _run(lambda data, c: InvitationResponse.model_validate(data, context=c), raw, ctx)


def parse_conversation_reply(raw: Any, ctx: DecisionContext) -> dict:
    return _run(lambda data, c: ConversationReply.model_validate(data, context=c), raw, ctx)


def decision_json_schema() -> dict:
    """Export JSON schema for main decisions to supply to structured LLM endpoints.

    This covers structural shape (fields and types); semantic context validation
    (valid names, available turns, unique triples) is handled during parse_decision.
    """
    return _DECISION_ADAPTER.json_schema()
