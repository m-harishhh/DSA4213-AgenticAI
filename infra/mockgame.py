"""Seeded mock game with the exact signature and return shape of controller.interfaces.run_game.

It plays a plausible game against Josh's case through the real EventLog and LLMClient,
so the orchestrator, the replay tool and the metrics can be built and tested before
Harish's controller lands. The "model" is a scripted MockBackend that says whatever the
mock controller decides; nothing here is evidence about real detective behaviour, and
the claim bookkeeping copies the chosen branch, which a live controller must never do.
"""

import json
import random
from pathlib import Path

from casework import load_case, public_view, initial_state, allowed_content, record_delivery, share_information
from casework.disclosure import branch_prompt, communication_options

from .eventlog import EventLog, summarise_cost
from .llm import MockBackend, TransportError, LLMCallError, suspect_agent_ids

CHECKPOINTS_AFTER_INTERROGATION = (5, 10, 15)
# Mock-only odds: varied, valid traces are the point, not realism.
P_FIRST_CORRECT = {"none": 0.25, "raw": 0.45, "ledger": 0.6}
P_INVITE, P_ACCEPT, P_FAILED_RUN = 0.5, 0.7, 0.05
LEDGER_SCHEMA = {"type": "object", "required": ["entries"], "properties": {"entries": {"type": "array"}}}
ACCEPT_SCHEMA = {"type": "object", "required": ["accept"], "properties": {"accept": {"type": "boolean"}}}


def _question_for(intent, suspect):
    kind = intent["kind"]
    if kind == "overview":
        return f"{suspect}, what can you tell me about last night?"
    if kind == "whereabouts":
        return f"Where were you at {intent['time']}?"
    if kind == "corroborate":
        return f"Did you see where {intent['subject']} was at {intent['time']}?"
    return f"Tell me about {intent['topic'].replace('_', ' ')}."


def _wrong_triple(rng, public, solution, used):
    while True:
        triple = {"culprit": rng.choice(public["suspects"]), "weapon": rng.choice(public["weapons"]),
                  "location": rng.choice(public["locations"])}
        if triple != solution and triple not in used:
            return triple


def run_game(case_path, config, seed, condition, llm, log_dir="runs"):
    """Harish's signature. Returns {game_id, log_path, outcome, first_accusations_correct}."""
    case = load_case(case_path)
    public = public_view(case)
    rng = random.Random(seed)
    k = int(config.get("k") or 5)
    turn_budget = int(config.get("turn_budget") or 20)
    game_id = f"mock_{condition}_s{seed}"
    log_path = Path(log_dir) / f"{game_id}.jsonl"
    if log_path.exists():
        log_path.unlink()
    backend = MockBackend([])
    script = backend.script             # the mock controller feeds the mock model its lines
    llm.backend = backend
    llm.sleep = lambda seconds: None    # never wait on mock backoff
    agent_ids = suspect_agent_ids(public["suspects"])
    solution = case["solution"]
    suspects = list(public["suspects"])

    with EventLog(log_path, game_id) as log:
        llm.log = log
        log.game_start(run_id=str(config.get("run_id", "mock")), case_id=case["case_id"],
                       case_sha256=str(config.get("case_sha256", "")), condition=condition, k=k,
                       turn_budget=turn_budget, accusation_budget=3, seed=seed,
                       model_id=llm.model_id, decoding=llm.decoding,
                       prompt_versions=config.get("prompt_versions") or {"mock": "v0"},
                       pricing=llm.pricing, manifest_sha256=None, code_version="mockgame")
        states = {name: initial_state(case, name) for name in suspects}
        window, visible_text, ledger = [], {}, {}
        decisions = 0
        phase = "through_first_accusation"
        counters = {"interrogations": 0, "checkpoints": 0, "detective_conv": 0, "private_conv": 0,
                    "invited": 0, "accepted": 0, "rejected": 0}

        def memory():
            ids = window[-1:] if condition == "none" else window[-k:]
            payload = [ledger.get(eid, []) for eid in ids] if condition == "ledger" else [visible_text[eid] for eid in ids]
            return ids, payload

        def detective(purpose, decision_type, reply, user_text, phase_now=None):
            nonlocal decisions
            decisions += 1
            ids, payload = memory()
            script.append(reply)
            record = llm.call(role="detective", purpose=purpose, phase=phase_now or phase, game_id=game_id,
                              messages=[{"role": "system", "content": "You are the detective. " + public["description"]},
                                        {"role": "user", "content": f"Memory: {json.dumps(payload)}\n{user_text}"}])
            log.memory_view(decision_index=decisions, decision_type=decision_type, window_exchange_ids=ids,
                            memory_payload=payload, call_id=record["call_id"])
            return record

        def suspect(name, purpose, reply, user_text, system_text=None, schema=None):
            script.append(reply)
            messages = ([{"role": "system", "content": system_text}] if system_text else []) + \
                       [{"role": "user", "content": user_text}]
            return llm.call(role="suspect", agent_id=agent_ids[name], purpose=purpose, phase=phase,
                            game_id=game_id, messages=messages, response_schema=schema)

        def extract(exchange_id, speaker, text):
            entries = [{"entry_id": f"{exchange_id}_1", "speaker": speaker, "kind": "claim", "text": text,
                        "source_exchange_id": exchange_id,
                        "source_span": {"start": 0, "end": len(text), "text": text}}]
            script.append({"entries": entries})
            record = llm.call(role="extractor", purpose="ledger_extraction", phase=phase, game_id=game_id,
                              messages=[{"role": "user", "content": f"{speaker}: {text}"}],
                              response_schema=LEDGER_SCHEMA)
            ledger[exchange_id] = record["parsed"]["entries"]
            log.extraction(exchange_id=exchange_id, call_id=record["call_id"], status="ok", entries=entries)

        def end(outcome, first_correct, component_correct, attempts_used):
            log.game_end(outcome=outcome, attempts_used=attempts_used, first_accusation_correct=first_correct,
                         component_correct=component_correct, interrogations_used=counters["interrogations"],
                         exchanges_total=len(window), checkpoints_reached=counters["checkpoints"],
                         conversations={"detective": counters["detective_conv"], "private": counters["private_conv"]},
                         invitations={"sent": counters["invited"], "accepted": counters["accepted"],
                                      "rejected": counters["rejected"]},
                         cost=summarise_cost(log.events), duration_s=0.0)
            return {"game_id": game_id, "log_path": str(log_path), "outcome": outcome,
                    "first_accusations_correct": first_correct}

        def checkpoint(index, after):
            counters["checkpoints"] += 1
            order = list(suspects)
            rng.shuffle(order)
            log.checkpoint_start(checkpoint_index=index, after_interrogation=after, proposal_order=order, seed_used=seed)
            busy, done = set(), 0
            detective_used = False
            sent = accepted = rejected = 0
            for pass_no in (1, 2):
                for inviter in order:
                    if inviter in busy or done >= 3 or rng.random() > P_INVITE:
                        continue
                    options = [s for s in suspects if s != inviter and s not in busy]
                    if not detective_used:
                        options.append("Detective")
                    if not options:
                        continue
                    recipient = rng.choice(options)
                    counters["invited"] += 1
                    sent += 1
                    invitation_id = f"inv{counters['invited']}"
                    invite = suspect(inviter, "invitation", {"invite": recipient}, "Invite one participant or null.")
                    if recipient == "Detective":
                        decision = detective("acceptance", "acceptance", {"accept": rng.random() < P_ACCEPT},
                                             f"{inviter} invites you. Accept?")
                        accept = json.loads(decision["text"])["accept"]
                    else:
                        decision = suspect(recipient, "acceptance", {"accept": rng.random() < P_ACCEPT},
                                           f"{inviter} invites you. Accept?", schema=ACCEPT_SCHEMA)
                        accept = decision["parsed"]["accept"]
                    log.invitation(invitation_id=invitation_id, checkpoint_index=index, pass_no=pass_no, sender=inviter,
                                   recipient=recipient, decision="accepted" if accept else "rejected",
                                   invitation_call_id=invite["call_id"], decision_call_id=decision["call_id"])
                    if not accept:
                        counters["rejected"] += 1
                        rejected += 1
                        continue
                    counters["accepted"] += 1
                    accepted += 1
                    busy.add(inviter)
                    done += 1
                    option = rng.choice(communication_options(case, inviter, states[inviter]))
                    message = suspect(inviter, "conversation_message", option["text"], f"Send one bounded message to {recipient}.")
                    if recipient == "Detective":
                        detective_used = True
                        reply = detective("conversation_reply", "conversation_reply", "Noted. Thank you.",
                                          f"{inviter} says: {option['text']}\nReply briefly.")
                        counters["detective_conv"] += 1
                        exchange_index = len(window) + 1
                        event = log.conversation(conversation_id=f"conv{counters['detective_conv']}", checkpoint_index=index,
                                                 visibility="detective", exchange_index=exchange_index, inviter=inviter,
                                                 recipient="Detective", message=message["text"], reply=reply["text"],
                                                 message_call_id=message["call_id"], reply_call_id=reply["call_id"],
                                                 controller_matched={"message_claim_ids": option["claim_ids"],
                                                                     "proposal_claim_ids": option["proposal_claim_ids"]})
                        window.append(event["exchange_id"])
                        visible_text[event["exchange_id"]] = f"{inviter}: {message['text']}\nDetective: {reply['text']}"
                        if condition == "ledger":
                            extract(event["exchange_id"], inviter, message["text"])
                    else:
                        busy.add(recipient)
                        states[recipient] = share_information(case, states[recipient], option["claim_ids"])
                        answer = rng.choice(communication_options(case, recipient, states[recipient]))
                        reply = suspect(recipient, "conversation_reply", answer["text"],
                                        f"{inviter} says: {option['text']}\nReply briefly.")
                        states[inviter] = share_information(case, states[inviter], answer["claim_ids"])
                        counters["private_conv"] += 1
                        log.conversation(conversation_id=f"p{counters['private_conv']}", checkpoint_index=index,
                                         visibility="private", inviter=inviter, recipient=recipient,
                                         message=message["text"], reply=reply["text"],
                                         message_call_id=message["call_id"], reply_call_id=reply["call_id"],
                                         controller_matched={"message_claim_ids": option["claim_ids"],
                                                             "reply_claim_ids": answer["claim_ids"],
                                                             "proposal_claim_ids": option["proposal_claim_ids"]})
            log.checkpoint_end(checkpoint_index=index, invitations_sent=sent, accepted=accepted, rejected=rejected,
                               conversations_completed=done)

        n_interrogations = rng.randint(4, min(turn_budget, 12))
        failing_turn = rng.randint(1, n_interrogations) if rng.random() < P_FAILED_RUN else None
        asked_overview = set()
        try:
            for turn in range(1, n_interrogations + 1):
                name = rng.choice(suspects)
                spec = case["suspects"][name]
                choices = [{"kind": "whereabouts", "time": rng.choice(public["times"])},
                           {"kind": "corroborate", "subject": rng.choice([s for s in suspects if s != name]),
                            "time": rng.choice(public["times"])}]
                if name not in asked_overview:
                    choices.append({"kind": "overview"})
                if spec["lead_ids"]:
                    choices.append({"kind": "ask_lead", "topic": rng.choice(spec["lead_ids"])})
                intent = rng.choice(choices)
                if intent["kind"] == "overview":
                    asked_overview.add(name)
                question_text = _question_for(intent, name)
                question = detective("question", "question",
                                     {"action": "interrogate", "suspect": name, "question": question_text},
                                     "Interrogate one suspect or accuse.")
                branches = allowed_content(case, name, intent, states[name])
                branch = rng.choice(branches)
                instructions = branch_prompt(case, branch)
                reply_text = " ".join(instructions["required_claims"] + instructions["required_leads"]) \
                    or "I would rather not go into that."
                if turn == failing_turn:
                    for _ in range(llm.max_transport_retries + 1):
                        script.append(TransportError("503 mock outage"))
                    try:
                        suspect(name, "interrogation_response", "unused", question_text, json.dumps(instructions))
                    except LLMCallError as exc:
                        log.failure(stage="suspect_response", reason=exc.record["status"], call_id=exc.record["call_id"],
                                    detail="Persistent mock transport failure")
                        return end("failed_run", None, None, 0)
                answer = suspect(name, "interrogation_response", reply_text, question_text, json.dumps(instructions))
                states[name] = record_delivery(case, name, states[name], branch["claim_ids"], branch["lead_ids"],
                                               branch["retract_claim_ids"])
                counters["interrogations"] += 1
                event = log.interrogation(interrogation_index=turn, exchange_index=len(window) + 1, suspect=name,
                                          question=question_text, question_call_id=question["call_id"], intent=intent,
                                          allowed_branches=branches, instructions=instructions,
                                          selected_branch_id=branch["branch_id"], response=answer["text"],
                                          response_call_id=answer["call_id"],
                                          controller_matched={"claim_ids": branch["claim_ids"], "lead_ids": branch["lead_ids"],
                                                              "retraction_ids": branch["retract_claim_ids"]})
                window.append(event["exchange_id"])
                visible_text[event["exchange_id"]] = f"Detective: {question_text}\n{name}: {answer['text']}"
                if condition == "ledger":
                    extract(event["exchange_id"], name, answer["text"])
                if turn in CHECKPOINTS_AFTER_INTERROGATION and turn < n_interrogations:
                    checkpoint(CHECKPOINTS_AFTER_INTERROGATION.index(turn) + 1, turn)
        except LLMCallError as exc:  # any other exhausted call: operational failure, kept in accounting
            log.failure(stage="unexpected", reason=exc.record["status"], call_id=exc.record["call_id"], detail=None)
            return end("failed_run", None, None, 0)

        used, first_correct, component = [], None, None
        outcome = "unsolved"
        for attempt in (1, 2, 3):
            phase_now = "through_first_accusation" if attempt == 1 else "accusation_retries"
            correct = rng.random() < (P_FIRST_CORRECT[condition] if attempt == 1 else 0.5)
            triple = dict(solution) if correct else _wrong_triple(rng, public, solution, used)
            used.append(triple)
            decision = {"action": "accuse", **triple}
            if attempt == 1:
                pairs = []
                if len(window) >= 2 and rng.random() < 0.6:
                    left, right = rng.sample(window, 2)
                    pairs.append({"left": {"exchange_id": left, "quote": visible_text[left].split("\n")[-1][:60]},
                                  "right": {"exchange_id": right, "quote": visible_text[right].split("\n")[-1][:60]},
                                  "note": "mock citation"})
                decision["contradiction_report"] = pairs
            action = detective("accusation", "accusation", decision, "Make your accusation.", phase_now)
            if attempt == 1:
                log.contradiction_report(call_id=action["call_id"], window_exchange_ids=memory()[0],
                                         reported_pairs_raw=decision["contradiction_report"])
                first_correct = correct
                component = {key: triple[key] == solution[key] for key in triple}
            log.accusation(attempt=attempt, triple=triple, valid=True, correct=correct,
                           feedback="Correct." if correct else f"Incorrect. {3 - attempt} attempt(s) remain.",
                           call_id=action["call_id"], exchange_index=len(window),
                           interrogations_used=counters["interrogations"])
            if correct:
                outcome = "solved_first" if attempt == 1 else "solved_retry"
                return end(outcome, first_correct, component, attempt)
        return end(outcome, first_correct, component, 3)
