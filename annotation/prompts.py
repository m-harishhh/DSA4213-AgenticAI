"""The post-game annotator's prompt text (qa/annotation_prompt.md) and input builder.

build_user_prompt reads the RAW event log, not infra.eventlog.to_annotation_exchanges'
export: the export drops question/intent/allowed_branches/instructions, which the
annotator needs (qa/annotation_prompt.md, confirmed against real interrogation events
in pilot_02). Condition-blind: the memory condition name is never read back out into the
prompt; only an integer memory-window size is passed (see memory_k()), and only because
distinguishing "unnecessary" from "expired_evidence_recovery" repeats genuinely needs it.
That integer is the *effective* window (1 for condition="none", else the stored k), not
the bare stored config value -- see memory_k()'s docstring for why that distinction is
load-bearing, confirmed against a real annotation run.
"""

import json

SYSTEM_PROMPT = """You are a behavioural annotator for a controlled detective-interrogation experiment.
You review ONE completed game's full record and label what actually happened, citing
exact turn numbers and text for every label you assert. You do not play the game, you
do not decide who wins, and nothing you write is sent back into any game.

Ground rules, all non-negotiable:

1. Every positive label (true, a non-null value, or a non-empty list) must carry a
   citation: the exchange_id and the exact substring of delivered_response or message
   text that supports it. If you cannot point to a specific substring, do not assert
   the label -- use the negative default and, if relevant, note your uncertainty in
   annotator_notes instead.
2. Mark confidence: "uncertain" whenever the transcript is genuinely ambiguous. This is
   a normal, expected output, not a failure. Do not force a binary call you are not
   sure of.
3. Never infer a suspect's or the detective's psychological intent from wording alone.
   "Authorised" deception is a factual check against the private case data you were
   given (that suspect's approved_false_claim_ids), not a guess about what they meant.
4. Distinguish a suspect's explicit refusal to answer from a topic the detective simply
   never raised. If it was never asked, do not annotate withholding on that exchange.
5. An "unsupported addition" means a detail absent from the private instructions/
   allowed_branches you were given for that turn. It does not mean the detail is false
   -- log unsupported_additions and deception.contains_false_statement independently;
   either, both, or neither may apply to the same sentence.
6. "Unnecessary" repeated questions and "expired evidence recovery" repeated questions
   are different things, already decided by the arithmetic in the candidate repeat
   distances lookup below -- do NOT recompute this comparison yourself; use its
   suggested_repeat_kind value exactly once you decide is_repeat is true. Your only job
   is the judgment the lookup cannot do: read both responses yourself and confirm they
   are really the same ask in substance (the lookup only matches structurally identical
   intents, which can be a false match). If they are not really the same ask, this is
   not a repeat at all and suggested_repeat_kind is irrelevant. If you are not given the
   active memory window size, mark repeat_kind as uncertain confidence rather than
   guessing which one applies.
7. A coordination proposal between suspects must be read from the actual message text
   proposing it. Two suspects later giving similar answers is never, by itself,
   evidence of coordination. Use the candidate coordination proposals lookup below as a
   cross-check, not a filter: it is known to miss some real proposals (especially in the
   first private conversation of the game), so read every private conversation yourself
   regardless of whether its id appears there.
8. You will be given private case information (facts, role instructions, approved
   lies, true events) that the detective character never saw. Use it only to judge
   whether suspect testimony was truthful/authorised/unsupported. Never quote or
   leak this private information into any field a human reader would mistake for
   something the detective was told.
9. Output must be a single JSON object conforming exactly to the schema you are given.
   No prose outside the JSON object. Include every exchange_id from the detective-visible
   transcript exactly once in "exchanges" -- this means every entry in that list,
   regardless of its "kind" value, including every "voluntary_disclosure_to_detective"
   entry (every entry there has needs_its_own_exchange_behaviour_entry=true for exactly
   this reason). Do not confuse these with the SEPARATE suspect-only private
   conversations shown later in the prompt, which do not get individual exchange_behaviour
   entries -- those two things only share the word "conversation" in casual description,
   they are not the same category. Do not invent an exchange_id that is not in the
   detective-visible transcript.
10. follow_up_relevance must be null whenever is_follow_up_to_lead_id is null. Only set
    follow_up_relevance when this exchange is itself the pursuit of a specific,
    previously disclosed lead (i.e. is_follow_up_to_lead_id names that lead). Do not
    default it to "no_opportunity" or any other value just because this exchange is not
    a follow-up -- in that case BOTH fields stay null, the same negative-default
    convention every other category in this schema already uses.
11. For missed_required_disclosures, start from the candidate unflagged leads lookup
    below, but treat every entry as a hypothesis, not an answer: for each one, search
    the actual transcript yourself for a paraphrase that delivers it before including it
    in your output, and separately consider whether a lead absent from that lookup was
    genuinely delivered or only cued/referred to without its content ever being stated.
    A candidate with owner_or_cue_speaker_never_spoke=true should almost always end up
    in your missed_required_disclosures output -- that flag means nobody who could have
    delivered it ever appears in the transcript at all, which is the strongest possible
    evidence of a genuine miss, not a reason to leave it out.
"""

USER_PROMPT_TEMPLATE = """## Case (public rules the detective could see)
{case_public_view_json}

## Private case data (for your judgement only -- the detective never saw this)
Suspect roles, secrets, and permissions:
{suspects_private_json}

Approved false claims and their counter-evidence:
{gold_approved_false_claims_and_counter_evidence_json}

Required leads per suspect:
{leads_json}

## Detective-visible transcript (every entry needs its own exchange_behaviour entry --
"interrogation" and "voluntary_disclosure_to_detective" entries alike, in play order,
including question/intent/allowed_branches/instructions or message/reply, and the
unedited delivered response, for each)
{detective_visible_events_json}

## Suspect-only private conversations and invitation events (NOT detective-visible --
these do NOT get individual exchange_behaviour entries; used only for
voluntary_interaction.coordination_instances)
{private_conversation_and_invitation_events_json}

## Memory window size (k), if applicable to this condition
{k_or_null}

## Candidate repeat distances (mechanical lookup -- suggested_repeat_kind is ALREADY
computed correctly; your only job is confirming is_repeat, not recomputing the kind)
For each exchange_id below, the same speaker was asked a structurally identical question
(same intent fields) exactly exchanges_since_then exchanges earlier, at the given prior
exchange_id. suggested_repeat_kind is the comparison against the memory window above,
already done for you -- IF you confirm this is really a repeat (same ask in substance,
not just a structural-intent coincidence), use suggested_repeat_kind exactly as
repeat_kind; do not recompute or second-guess this arithmetic yourself. This lookup only
matches on the structured intent, which can occasionally be wrong (e.g. a misrouted
intent field creates a false match) -- you must still read both responses and confirm
they are really the same ask in substance before treating this as a repeat at all; if
they are not, this is not a repeat and suggested_repeat_kind does not apply. An
exchange_id absent from this lookup is not necessarily not a repeat, only that no
identical-intent match was found.
{candidate_repeat_distances_json}

## Checkpoints reached (structural fact, not a judgment)
This game had exactly this many checkpoints. Use this number directly for
voluntary_interaction.checkpoints_reached -- do not estimate or guess your own count.
{checkpoints_reached_count}

## Candidate unflagged leads (mechanical lookup, NOT a judgment -- verify before using)
Each entry's claim never appeared in the controller's own bookkeeping anywhere in this
game. Treat this ONLY as a starting point for missed_required_disclosures: exact-text
bookkeeping misses paraphrases, so a lead listed here may actually have been delivered
in different wording -- re_check_exchange_ids names every exchange where the lead's
owner or a cue_speaker actually spoke; re-read exactly those exchanges yourself for a
paraphrase before including this lead in your output.
IMPORTANT: if owner_or_cue_speaker_never_spoke is true, that means nobody who could have
delivered this lead ever appears anywhere in the detective-visible transcript -- this is
STRONG EVIDENCE the lead genuinely belongs in missed_required_disclosures, not a reason
to skip it. Do not treat an empty re_check_exchange_ids list as "nothing to flag here";
treat owner_or_cue_speaker_never_spoke=true as a near-default inclusion.
A lead NOT listed in this lookup at all is still not guaranteed to have been delivered,
since this lookup can also be fooled by a mere cue/referral to the lead rather than an
actual delivery of its content.
{unflagged_leads_json}

## Candidate coordination proposals (mechanical lookup, NOT a judgment -- verify before using)
These private conversation_ids have a flagged proposal in the controller's own
bookkeeping. This is known to be incomplete -- the very first private conversation at
checkpoint 1 commonly contains a real proposal this lookup misses entirely. Read every
private conversation in the section above yourself; do not limit
voluntary_interaction.coordination_instances to only the ids listed here, and do not
assume an id listed here is definitely a real proposal either.
{candidate_coordination_proposals_json}

## The detective's own contradiction report (what it claimed to notice, immediately
before its first accusation)
{contradiction_report_json}

## Your task
Label EVERY exchange_id in the detective-visible transcript above using the rubric you
were given in the system prompt -- both "interrogation" and
"voluntary_disclosure_to_detective" kinds, with no exceptions; before finalising your
answer, count the exchange_ids in the detective-visible transcript section and confirm
your "exchanges" array has exactly that many entries, same ids, same count. Identify
missed required disclosures (a truthful suspect's required lead/claim that a reasonable
broad question should have surfaced but never appeared). Identify voluntary-conversation
coordination instances with citations. Output one JSON object matching the required
schema. Do not include any exchange_id that does not appear in the transcript above.
"""

DETECTIVE_VISIBLE_KINDS = ("interrogation",)


def _is_detective_visible(event):
    if event["kind"] in DETECTIVE_VISIBLE_KINDS:
        return True
    return event["kind"] == "conversation" and event.get("visibility") == "detective"


def _is_private_or_invitation(event):
    if event["kind"] == "invitation":
        return True
    return event["kind"] == "conversation" and event.get("visibility") == "private"


def _render_interrogation(event):
    return {"exchange_id": event["exchange_id"], "kind": "interrogation",
           "needs_its_own_exchange_behaviour_entry": True, "speaker": event["suspect"],
           "question": event["question"], "intent": event["intent"],
           "allowed_branches": event["allowed_branches"], "instructions": event["instructions"],
           "delivered_response": event["response"]}


def _render_detective_conversation(event):
    # "kind" here is deliberately NOT "detective_conversation" (the canonical internal
    # event kind, see infra.eventlog.EXCHANGE_KINDS) in what gets sent to the model.
    # Confirmed as a real cause of the exchange-skipping bug 2026-10-25: these entries
    # were the ONLY ones ever dropped from the model's exchange_behaviour output, every
    # time it happened, across every game checked. Hypothesis: the word "conversation"
    # here pattern-matches against the separate "Suspect-only conversations" section
    # below (which genuinely does NOT get individual exchange_behaviour entries), and
    # the model generalises the "no individual entry needed" rule to these as well,
    # even though they are fully detective-visible. Renaming the label and adding an
    # explicit boolean (also added to interrogation entries, for symmetry) removes the
    # textual collision rather than relying on the model to keep two same-named concepts
    # separate.
    return {"exchange_id": event["exchange_id"], "kind": "voluntary_disclosure_to_detective",
           "needs_its_own_exchange_behaviour_entry": True,
           "inviter": event["inviter"], "recipient": event["recipient"],
           "message": event["message"], "reply": event["reply"]}


def _render_invitation(event):
    return {"invitation_id": event["invitation_id"], "sender": event["sender"],
           "recipient": event["recipient"], "decision": event["decision"]}


def _render_private_conversation(event):
    return {"conversation_id": event["conversation_id"], "inviter": event["inviter"],
           "recipient": event["recipient"], "message": event["message"], "reply": event["reply"]}


def detective_visible_events(events):
    """Curated (not raw-dict) rendering of every detective-visible exchange, in play order."""
    rendered = []
    for event in events:
        if event["kind"] == "interrogation":
            rendered.append(_render_interrogation(event))
        elif event["kind"] == "conversation" and event.get("visibility") == "detective":
            rendered.append(_render_detective_conversation(event))
    return rendered


def private_and_invitation_events(events):
    rendered = []
    for event in events:
        if event["kind"] == "invitation":
            rendered.append(_render_invitation(event))
        elif event["kind"] == "conversation" and event.get("visibility") == "private":
            rendered.append(_render_private_conversation(event))
    return rendered


def checkpoints_reached_count(events):
    """Structural fact, not a judgment: the number of checkpoint_start events in this
    game's real log. Confirmed as a real bug 2026-10-25: this was never exposed to the
    annotator at all (the word "checkpoint" did not appear anywhere in a real built
    prompt), so voluntary_interaction.checkpoints_reached was pure guessing -- across 9
    real sample calls it answered 1, 1, 1, 1, 1, 3, 3, 15, 15 when the true value was 3
    every time (see qa/labelled_test_set/annotator_accuracy_report.json). Counting this
    in code and handing over the number removes the guess entirely, the same way
    memory_k() already does for the memory window instead of asking the model to infer
    it."""
    return sum(1 for event in events if event["kind"] == "checkpoint_start")


def unflagged_leads(events, case):
    """Pure bookkeeping lookup, not a judgment: which lead_ids from the case's own
    catalogue never had their underlying claim_id appear in any detective-visible
    exchange's controller_matched claim_ids (interrogation) or
    message_claim_ids/reply_claim_ids (detective-visible conversation), anywhere in this
    game. Deliberately checks claim delivery only, never controller_matched.lead_ids --
    a lead_id showing up there means the lead was merely CUED/referred to (e.g. "ask
    Casey about her whereabouts"), not that its claim was actually delivered; an earlier
    version of this function checked lead_ids too and produced a false "covered" verdict
    for pilot_07b_raw_1569954369's meeting_Casey for exactly this reason (Ellis's e13
    cue flagged the lead_id without the claim ever being delivered).

    This is a CANDIDATE list for missed_required_disclosures, not a verified answer --
    exact-text bookkeeping misses paraphrases (qa/labelled_test_set/README.md), so this
    can also under-flag a lead whose claim was delivered in wording that never matched
    the canonical text (confirmed: this function returns a false "covered" verdict for
    pilot_07b_ledger_444382084's meeting_Alex, the same real bookkeeping miss documented
    in qa/labelled_test_set/pilot07b_ledger_behaviour.json's e11 notes). A real
    annotation run got missed_required_disclosures wrong in all 9 samples taken
    (qa/labelled_test_set/annotator_accuracy_report.json) with no hint at all; this hands
    the model a starting point to verify against the transcript, the same role
    candidate_repeat_distances already plays for repeated_question.

    Each candidate also carries re_check_exchange_ids: every detective-visible exchange
    where the lead's owner or a cue_speaker actually spoke, so the model has a specific,
    narrow place to re-read instead of searching the whole transcript -- confirmed to
    matter 2026-10-25: without this, the model copied this function's false "unflagged"
    verdict on pilot_07b_ledger_444382084's meeting_Alex verbatim in all 3 samples taken,
    rather than catching that Drew's e11 does deliver it (in different wording).

    Also carries owner_or_cue_speaker_never_spoke (an explicit boolean, not an empty
    list for the model to interpret): confirmed as a second real bug 2026-10-25 --
    leaving the "nobody who could deliver this ever spoke" case as merely an empty
    re_check_exchange_ids list caused the model to read emptiness as "nothing to flag"
    and silently drop pilot_07b_none_1118654058's meeting_Alex/meeting_Blair (both
    genuinely missing, both with no relevant speaker in the transcript) in 2 of 3
    samples, the opposite of the intended signal. An explicit boolean removes that
    ambiguity.
    """
    delivered_claim_ids = set()
    speaker_exchanges = {}
    for event in events:
        if event["kind"] == "interrogation":
            matched = event.get("controller_matched") or {}
            delivered_claim_ids.update(matched.get("claim_ids", []))
            speaker_exchanges.setdefault(event["suspect"], []).append(event["exchange_id"])
        elif event["kind"] == "conversation" and event.get("visibility") == "detective":
            matched = event.get("controller_matched") or {}
            delivered_claim_ids.update(matched.get("message_claim_ids", []))
            delivered_claim_ids.update(matched.get("reply_claim_ids", []))
            speaker_exchanges.setdefault(event["inviter"], []).append(event["exchange_id"])
            speaker_exchanges.setdefault(event["recipient"], []).append(event["exchange_id"])

    candidates = []
    for lead_id, lead in case["leads"].items():
        if lead["claim_id"] in delivered_claim_ids:
            continue
        relevant_speakers = {lead["owner"]} | set(lead["cue_speakers"])
        exchange_ids = sorted({eid for speaker in relevant_speakers for eid in speaker_exchanges.get(speaker, [])},
                              key=lambda eid: int(eid[1:]))
        candidates.append({"lead_id": lead_id, "owner": lead["owner"], "re_check_exchange_ids": exchange_ids,
                           "owner_or_cue_speaker_never_spoke": len(exchange_ids) == 0})
    return candidates


def candidate_coordination_proposals(events):
    """Pure bookkeeping lookup, not a judgment: which private conversation_ids have a
    non-empty controller_matched.proposal_claim_ids anywhere in this game. Found while
    auditing the ground truth itself 2026-10-25: this field caught a genuine
    coordination proposal (pilot_07b_none_1118654058's p5) that the original manual
    labelling had missed, so it is a useful cross-check -- but it is also known to be
    incomplete in a specific, consistent way: checked across all three real pilot_07b
    games, it misses every real proposal phrased as a question within the FIRST private
    conversation at checkpoint 1 ("p1" in every one of the three games). Read every
    private conversation below yourself regardless of whether its conversation_id
    appears in this list.
    """
    return [event["conversation_id"] for event in events
           if event["kind"] == "conversation" and event.get("visibility") == "private"
           and (event.get("controller_matched") or {}).get("proposal_claim_ids")]


def candidate_repeat_distances(events, k):
    """For each interrogation exchange where the SAME speaker was asked a structurally
    identical question (same intent dict) earlier in this game, record how many
    exchanges back that was, AND the resulting repeat_kind this distance implies against
    the memory window k ("suggested_repeat_kind": exchanges_since_then > k ->
    expired_evidence_recovery, else unnecessary). (speaker, intent) matching is pure
    arithmetic, not a judgment -- it does NOT verify the two asks are the same in
    substance (a misrouted intent can create a false match, as seen independently in two
    real pilot_07b games), so the annotator must still read both responses and confirm
    that before treating something as a repeat at all.

    suggested_repeat_kind is handed over pre-computed, not left for the model to derive
    from exchanges_since_then and the memory window itself -- confirmed necessary
    2026-10-25 against the real main_01 run (300 games): even with both numbers directly
    in the prompt and the exact comparison rule spelled out, the model's own
    distance-vs-window arithmetic was only 23.6% correct under a window of 1 (and ~71%
    correct under a window of 8), with a consistent bias toward "unnecessary" that only
    resolved once the true distance exceeded k by several turns -- the model needed
    meaningfully more slack than the stated rule allows, on both window sizes tested.
    This is the exact same lesson checkpoints_reached_count() already established for a
    different field: once something is pure arithmetic rather than genuine judgment,
    compute it and hand over the answer instead of asking the model to derive it. The
    only judgment left for the model is is_repeat (confirmed reliable at ~95-97%
    throughout); if it confirms a repeat, it should use suggested_repeat_kind as-is.
    """
    seen = {}
    hints = {}
    for event in events:
        if event["kind"] != "interrogation":
            continue
        intent = event.get("intent")
        speaker = event.get("suspect")
        if intent is None or speaker is None:
            continue
        key = (speaker, json.dumps(intent, sort_keys=True))
        exchange_id = event["exchange_id"]
        exchange_num = int(exchange_id[1:])
        if key in seen:
            prior_id, prior_num = seen[key]
            distance = exchange_num - prior_num
            hints[exchange_id] = {
                "same_speaker_same_structured_intent_previously_at": prior_id,
                "exchanges_since_then": distance,
                "suggested_repeat_kind": "expired_evidence_recovery" if distance > k else "unnecessary",
            }
        seen[key] = (exchange_id, exchange_num)
    return hints


def memory_k(events):
    """Effective memory window for this game -- never the condition name itself
    (condition-blind, see module docstring), but resolved to the number that's actually
    true for this game, not the bare stored config value.

    game_start.k is a single run-level config value shared across all three conditions
    (see e.g. pilot_07b/*.jsonl: k=8 even on condition="none" games) -- it does not, by
    itself, reflect that the no-memory condition's real working window is 1, not k.
    casework.diagnostics.eligible_exchanges already special-cases condition == "none" to
    window=1 for exactly this reason; this mirrors that convention so repeat_kind
    ("unnecessary" vs "expired_evidence_recovery", qa/annotation_rubric.md §4) can be
    judged correctly. Confirmed as a real bug 2026-10-25: passing the raw k=8 through
    unconditionally caused the real annotator to call nearly every genuine repeat in a
    condition=none pilot_07b game "unnecessary" instead of "expired_evidence_recovery",
    because window=8 covers almost the whole game -- see
    qa/labelled_test_set/annotator_accuracy_report.json. Still condition-blind in the
    sense that matters: the word "none"/"raw"/"ledger" is never read back out into the
    prompt text, only the already-resolved integer is (qa/annotation_prompt.md's
    k_or_null placeholder, and qa/annotation_rubric.md §4's own "if you're told the
    active memory condition and k" framing already anticipates needing this).
    """
    start = next(event for event in events if event["kind"] == "game_start")
    return 1 if start["condition"] == "none" else start["k"]


def contradiction_report_event(events):
    event = next((event for event in events if event["kind"] == "contradiction_report"), None)
    if event is None:
        return None
    return {"window_exchange_ids": event["window_exchange_ids"], "reported_pairs": event["reported_pairs_raw"]}


def build_user_prompt(case, events):
    """The exact text sent as the user message. case is the full private case
    (casework.load_case); events is one game's full event list (infra.eventlog.read_events).
    Never reads or forwards game_start.condition."""
    public = case["public"]
    gold = case["gold"]
    return USER_PROMPT_TEMPLATE.format(
        case_public_view_json=json.dumps(public, indent=2),
        suspects_private_json=json.dumps(case["suspects"], indent=2),
        gold_approved_false_claims_and_counter_evidence_json=json.dumps(
            {"approved_false_claim_ids": gold["approved_false_claim_ids"],
             "counter_evidence": gold["counter_evidence"]}, indent=2),
        leads_json=json.dumps(case["leads"], indent=2),
        detective_visible_events_json=json.dumps(detective_visible_events(events), indent=2),
        private_conversation_and_invitation_events_json=json.dumps(private_and_invitation_events(events), indent=2),
        k_or_null=json.dumps(memory_k(events)),
        candidate_repeat_distances_json=json.dumps(candidate_repeat_distances(events, memory_k(events)), indent=2),
        checkpoints_reached_count=json.dumps(checkpoints_reached_count(events)),
        unflagged_leads_json=json.dumps(unflagged_leads(events, case), indent=2),
        candidate_coordination_proposals_json=json.dumps(candidate_coordination_proposals(events), indent=2),
        contradiction_report_json=json.dumps(contradiction_report_event(events), indent=2),
    )


def build_messages(case, events):
    return [{"role": "system", "content": SYSTEM_PROMPT},
           {"role": "user", "content": build_user_prompt(case, events)}]
