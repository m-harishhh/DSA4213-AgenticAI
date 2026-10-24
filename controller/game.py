'''
Game loop and controller state (Owned by Harish)
'''

from controller.interfaces import public_view, CONDITIONS, EventLog, agent_ids_for, initial_state
from controller.interfaces import MEMORY_K, TURN_BUDGET, ACCUSATION_BUDGET
from controller.interfaces import DecisionContext, parse_decision, record_delivery, load_case
from controller.interfaces import LLMCallError, summarise_cost
from controller.interfaces import build_memory, detective_decide, interpret, suspect_respond, match_delivery, extract_ledger
from controller.interfaces import CHECKPOINTS_AFTER_INTERROGATION, INVITATION_PASSES, MAX_CONVERSATIONS_PER_CHECKPOINT
from controller.interfaces import share_information
from controller.checkpoint import (DETECTIVE, communicated, suspect_invite, suspect_accept, suspect_message,
                                   suspect_reply)
from detective import detective_invitation_response, detective_conversation_reply
from infra.llm import strip_code_fences
import random
from pathlib import Path
import time


class FailedRun(Exception):
    """
    The game cannot continue (proposal section 3.5: persistent invalid output is a failed run)
    """

def new_game(case, condition, seed, log):
    '''
    Fresh per-game state for one run. Every suspect starts from Josh's initial state.
    case: an already-loaded case dict (load_case)
    condition: one of CONDITIONS ("none", "raw", "ledger")
    seed: recorded seed for this run; rng drives the checkpoint proposal order
    log: this game's EventLog (None only in quick checks)

    Two separate counters:
    interrogations_used counts questions only (turn budget, checkpoints)
    exchange_index counts every detective-visible exchange, including detective conversations

    returns: dict holding the case, counters, suspect states/histories, exchanges and ledger
    '''
    if condition not in CONDITIONS: 
        raise ValueError(f"Unknown memory condition {condition!r}; expected one of {CONDITIONS}")

    rng = random.Random(seed)
    public = public_view(case)
    names = public["suspects"]
    states = {name: initial_state(case, name) for name in names}

    histories = {name: [] for name in names}
    exchanges = []
    ledger = {}
    interrogations_used = 0
    exchange_index = 0
    decision_index = 0
    accusations = []

    agent_ids = agent_ids_for(case)

    return {
        'accusations': accusations,
        'agent_ids': agent_ids,
        'case': case,
        'condition': condition,
        'decision_index': decision_index,
        'exchange_index': exchange_index,
        'exchanges': exchanges,
        'histories': histories,
        'interrogations_used': interrogations_used,
        'ledger': ledger,
        'log': log,
        'public': public,
        'rng': rng,
        'seed': seed,
        'states': states,
        'game_id': log.game_id if log else None,
        # checkpoint statistics for game_end (§5.2 voluntary interaction)
        'checkpoint_stats': {'checkpoints_reached': 0,
                             'conversations': {'detective': 0, 'private': 0},
                             'invitations': {'sent': 0, 'accepted': 0, 'rejected': 0}},
    }

def detective_view(game, parts):
    '''What the detective sees for its next call: this condition's memory view plus the validation
    context built from public information only. Used by decisions and checkpoint calls alike.'''
    memory = parts["memory"](game["condition"], game["exchanges"], game['ledger'], MEMORY_K)
    ctx = DecisionContext.from_public(game['public'],
                                      visible_exchanges = memory["visible_exchanges"],
                                      previous_accusations = game['accusations'],
                                      turns_left = 0 if game.get('testimony_frozen') else TURN_BUDGET - game['interrogations_used']
                                      )
    return memory, ctx


def decide(game, parts, llm):
    '''
    One detective decision: interrogate or accuse.
    Builds the memory view for this game's condition, asks the detective, and validates
    the output with parse_decision. An invalid output gets exactly one repair (decisions.md D1).
    Logs what the detective saw as a memory_view event.

    returns: parse_decision result {ok, action, errors, warnings} plus call_id of the call used.
    ok=False after the repair means a failed run (§3.5); run_game ends the game.
    '''
    game['decision_index'] += 1

    memory, ctx = detective_view(game, parts)

    raw = parts["decide"](memory, ctx, llm, game['game_id'])

    # Formatting only: gemini-3.5-flash-lite wraps valid JSON in ```json fences (pilot_04c).
    # The logged llm_call keeps the original text.
    result = parse_decision(strip_code_fences(raw['text']), ctx)

    if not result['ok']:
        repair_errors = result['errors']
        raw = parts['decide'](memory, ctx, llm, game['game_id'],
                              repair_errors = repair_errors, repair_of = raw['call_id'])

        result = parse_decision(strip_code_fences(raw['text']), ctx)

    
    if result['ok'] and result['action']['action'] == 'accuse':
        decision_type = "accusation"
    
    elif result['ok']:
        decision_type = 'question'

    else:
        decision_type = 'decision'

    
    game['log'].memory_view(decision_index = game['decision_index'], 
                            decision_type = decision_type, 
                            window_exchange_ids= memory['window_exchange_ids'],
                            memory_payload = memory['payload'],
                            call_id = raw['call_id']
                            )

    if not result['ok']:
        game['log'].failure(stage = 'detective_decision', 
                            reason = 'format_failure', 
                            call_id = raw['call_id'], 
                            detail = result['errors'])
        raise FailedRun('Format Failure')
    
    result['call_id'] = raw['call_id']
    result['window_exchange_ids'] = memory['window_exchange_ids']
    return result


def play_interrogation(game, decision, parts, llm):
    '''
    One interrogation turn: the detective's question goes in, the suspect's answer comes out.
    decision is the ok result from decide whose action is "interrogate" (suspect, question, cites).
    The answer is delivered unchanged; state is updated only from what was actually said.

    returns: the new detective-visible exchange
    '''
    action = decision['action']
    case = game['case']
    suspect = action['suspect']
    question = action['question']

    # Interpreter sees every exchange the detective has seen, never the memory window (section 3.3)
    delivered = {exchange['exchange_id']: exchange for exchange in game['exchanges']}
    interp = parts['interpret'](question, suspect, action['cites'], game['public'],
                                delivered, llm, game['game_id'])

    answer = parts['respond'](case, suspect, question, interp['intent'], game['states'][suspect],
                              game['histories'][suspect], llm, game['game_id'],
                              interp['cited_claim_ids'])

    matched = parts['match'](case, suspect, answer['text'], answer['allowed_branches'])
    game['states'][suspect] = record_delivery(case, suspect, game['states'][suspect],
                                              actual_claim_ids = matched['claim_ids'],
                                              actual_lead_ids = matched['lead_ids'],
                                              actual_retraction_ids = matched['retraction_ids'])

    game['interrogations_used'] += 1
    game['exchange_index'] += 1

    game['histories'][suspect].append({'speaker': 'Detective', 'text': question})
    game['histories'][suspect].append({'speaker': suspect, 'text': answer['text']})

    event = game['log'].interrogation(interrogation_index = game['interrogations_used'],
                                      exchange_index = game['exchange_index'],
                                      suspect = suspect,
                                      question = question,
                                      question_call_id = decision['call_id'],
                                      intent = interp['intent'],
                                      intent_call_id = interp['call_id'],
                                      allowed_branches = answer['allowed_branches'],
                                      instructions = answer['instructions'],
                                      selected_branch_id = None,  # the suspect model chooses, not the controller
                                      response = answer['text'],
                                      response_call_id = answer['call_id'],
                                      controller_matched = matched)

    return add_exchange(game, event['exchange_id'], 'interrogation', suspect,
                        f"Detective: {question}\n{suspect}: {answer['text']}",
                        matched['claim_ids'], matched['lead_ids'], parts, llm)


def add_exchange(game, exchange_id, kind, speaker, text, claim_ids, lead_ids, parts, llm):
    '''Add one detective-visible exchange (an interrogation or a detective conversation) to the
    detective's history, and extract it in the ledger condition.
    returns: the exchange'''
    case = game['case']
    exchange = {
        'exchange_id': exchange_id,
        'kind': kind,                     # build_memory only counts interrogation / detective_conversation
        'claim_keys': {cid: case['claims'][cid]['key'] for cid in claim_ids},   # what each claim is about (interpreter)
        'lead_labels': {lid: case['leads'][lid]['label'] for lid in lead_ids},   # topics now heard (interpreter)
        'matched_claim_ids': list(claim_ids),
        'speaker': speaker,
        'text': text,
    }
    game['exchanges'].append(exchange)

    # Ledger condition only: each exchange is extracted once, on its own (§4.2)
    if game['condition'] == 'ledger':
        extraction = parts['extract'](exchange, llm, game['game_id'])
        failure_entry = extraction.get('failure_entry')
        # A failed extraction stays visible as an explicit failure entry, never a fallback to raw text (§4.2)
        game['ledger'][exchange['exchange_id']] = [failure_entry] if failure_entry else extraction['entries']
        game['log'].extraction(exchange_id = exchange['exchange_id'],
                               call_id = extraction['call_id'],
                               status = extraction['status'],
                               entries = extraction['entries'],
                               failure_entry = failure_entry)

    return exchange


def handle_accusation(game,action):
    triple = (action['culprit'], action['weapon'], action['location'])
    solution = game['case']['solution']
    answer = (solution['culprit'], solution['weapon'], solution['location'])
    game['accusations'].append(triple)
    correct = triple == answer
    
    return {
        "correct": correct,
        'feedback': 'correct' if correct else 'incorrect',
        "game_over": correct or len(game['accusations']) >= ACCUSATION_BUDGET, 
    }

def accusation_phase(game, decision, parts, llm):
    game['testimony_frozen'] = True

    game['log'].contradiction_report(call_id = decision['call_id'],
        window_exchange_ids = decision['window_exchange_ids'],
        reported_pairs_raw = decision['action']['contradiction_report'])

    while True:
        action  = decision['action']
        verdict = handle_accusation(game, action)
        game['log'].accusation(attempt = len(game['accusations']),
            triple = {'culprit': action['culprit'], 'weapon': action['weapon'], 'location': action['location']},
            valid = True,
            correct = verdict['correct'], feedback = verdict['feedback'],
            call_id = decision['call_id'], exchange_index = game['exchange_index'],
            interrogations_used = game['interrogations_used'])
        
        if verdict['game_over']: 
            break
        decision = decide(game, parts, llm)      
        
        if not decision['ok']: 
            break

    return {
        "correct": verdict['correct'],
        "attempts": len(game['accusations']), 
        "ok": decision['ok']
    }


def detective_checkpoint_call(game, parts, llm, decision_type, call):
    '''One detective checkpoint call (accept or reply) from its current memory view.
    Aravindh's functions do the one repair; ok=False afterwards is a failed run (§3.5).'''
    game['decision_index'] += 1
    memory, ctx = detective_view(game, parts)
    result = call(memory, ctx)
    game['log'].memory_view(decision_index = game['decision_index'], decision_type = decision_type,
                            window_exchange_ids = memory['window_exchange_ids'],
                            memory_payload = memory['payload'], call_id = result['call_id'])
    if not result['ok']:
        game['log'].failure(stage = f'checkpoint_{decision_type}', reason = 'format_failure',
                            call_id = result['call_id'], detail = result['errors'])
        raise FailedRun(f'checkpoint {decision_type} format failure')
    return result


def run_checkpoint(game, checkpoint_index, after_interrogation, parts, llm):
    '''
    Voluntary conversations after interrogation 5, 10 or 15 (proposal §3.4).

    Suspects are offered the chance to invite in a seeded random order, for up to INVITATION_PASSES
    passes. Each still-free suspect may invite one free participant (another suspect or the detective)
    or nobody. Invitations and decisions carry identity only. A rejection uses nobody's slot; an
    accepted conversation is one message and one reply and uses both participants' slots. At most
    MAX_CONVERSATIONS_PER_CHECKPOINT conversations, so at most one with the detective.

    Suspect-only conversations stay private: only claims actually said pass to the listener
    (share_information). A detective conversation becomes one detective-visible exchange that costs no
    interrogation turn but enters memory, so it can push the oldest exchange out of the window.
    '''
    case, log, game_id = game['case'], game['log'], game['game_id']
    suspects = list(game['public']['suspects'])
    stats = game['checkpoint_stats']
    stats['checkpoints_reached'] += 1

    order = list(suspects)
    game['rng'].shuffle(order)
    log.checkpoint_start(checkpoint_index = checkpoint_index, after_interrogation = after_interrogation,
                         proposal_order = order, seed_used = game['seed'])

    busy = set()            # participants who completed a conversation at this checkpoint
    sent = accepted = rejected = completed = 0

    for pass_no in range(1, INVITATION_PASSES + 1):
        for inviter in order:
            if completed >= MAX_CONVERSATIONS_PER_CHECKPOINT:
                break
            if inviter in busy:
                continue
            available = [s for s in suspects if s != inviter and s not in busy]
            if DETECTIVE not in busy:
                available.append(DETECTIVE)
            if not available:
                continue

            invite = parts['invite'](case, inviter, game['states'][inviter], game['histories'][inviter],
                                     available, llm, game_id)
            recipient = invite['invite']
            if recipient is None:
                continue                      # chose to stay silent this pass

            sent += 1
            stats['invitations']['sent'] += 1
            if recipient == DETECTIVE:
                decision = detective_checkpoint_call(game, parts, llm, 'acceptance',
                    lambda memory, ctx: parts['detective_accept'](inviter, memory, ctx, llm, game_id))
            else:
                decision = parts['accept'](case, recipient, inviter, game['states'][recipient],
                                           game['histories'][recipient], llm, game_id)
            log.invitation(invitation_id = f"inv{stats['invitations']['sent']}", checkpoint_index = checkpoint_index,
                           pass_no = pass_no, sender = inviter, recipient = recipient,
                           decision = 'accepted' if decision['accept'] else 'rejected',
                           invitation_call_id = invite['call_id'], decision_call_id = decision['call_id'])
            if not decision['accept']:
                rejected += 1
                stats['invitations']['rejected'] += 1
                continue

            accepted += 1
            stats['invitations']['accepted'] += 1
            busy |= {inviter, recipient}
            completed += 1
            if recipient == DETECTIVE:
                detective_conversation(game, checkpoint_index, inviter, parts, llm)
            else:
                private_conversation(game, checkpoint_index, inviter, recipient, parts, llm)

    log.checkpoint_end(checkpoint_index = checkpoint_index, invitations_sent = sent, accepted = accepted,
                       rejected = rejected, conversations_completed = completed)


def private_conversation(game, checkpoint_index, inviter, recipient, parts, llm):
    '''Suspect to suspect: one message, one reply. Never enters the detective's memory.'''
    case, states, histories, game_id = game['case'], game['states'], game['histories'], game['game_id']
    message = parts['message'](case, inviter, recipient, states[inviter], histories[inviter], llm, game_id)
    said = communicated(case, inviter, message['text'], message['options'])
    states[recipient] = share_information(case, states[recipient], said['claim_ids'])

    reply = parts['reply'](case, recipient, inviter, message['text'], states[recipient], histories[recipient],
                           llm, game_id)
    replied = communicated(case, recipient, reply['text'], reply['options'])
    states[inviter] = share_information(case, states[inviter], replied['claim_ids'])

    # Both remember the conversation; each line is attributed, since a fellow suspect is not the detective
    for name in (inviter, recipient):
        histories[name].append({'speaker': inviter, 'text': f"(private conversation) {inviter}: {message['text']}"})
        histories[name].append({'speaker': recipient, 'text': f"(private conversation) {recipient}: {reply['text']}"})

    game['checkpoint_stats']['conversations']['private'] += 1
    game['log'].conversation(conversation_id = f"p{game['checkpoint_stats']['conversations']['private']}",
                             checkpoint_index = checkpoint_index, visibility = 'private',
                             inviter = inviter, recipient = recipient,
                             message = message['text'], reply = reply['text'],
                             message_call_id = message['call_id'], reply_call_id = reply['call_id'],
                             controller_matched = {'message_claim_ids': said['claim_ids'],
                                                   'reply_claim_ids': replied['claim_ids'],
                                                   'proposal_claim_ids': said['proposal_claim_ids']})


def detective_conversation(game, checkpoint_index, inviter, parts, llm):
    '''Suspect to detective: the suspect's message, the detective's bounded reply (no answer to it).
    Becomes one detective-visible exchange: no interrogation turn, but it enters memory (§3.4).'''
    case, game_id = game['case'], game['game_id']
    message = parts['message'](case, inviter, DETECTIVE, game['states'][inviter], game['histories'][inviter],
                               llm, game_id)
    reply = detective_checkpoint_call(game, parts, llm, 'conversation_reply',
        lambda memory, ctx: parts['detective_reply'](inviter, message['text'], memory, ctx, llm, game_id))

    # What the suspect said is delivered testimony, recorded like an interrogation answer
    said = communicated(case, inviter, message['text'], message['options'])
    game['states'][inviter] = record_delivery(case, inviter, game['states'][inviter],
                                              actual_claim_ids = said['own_claim_ids'])
    game['histories'][inviter].append({'speaker': inviter, 'text': message['text']})
    game['histories'][inviter].append({'speaker': DETECTIVE, 'text': reply['message']})

    game['exchange_index'] += 1
    game['checkpoint_stats']['conversations']['detective'] += 1
    event = game['log'].conversation(conversation_id = f"d{game['checkpoint_stats']['conversations']['detective']}",
                                     checkpoint_index = checkpoint_index, visibility = 'detective',
                                     exchange_index = game['exchange_index'], inviter = inviter,
                                     recipient = DETECTIVE, message = message['text'], reply = reply['message'],
                                     message_call_id = message['call_id'], reply_call_id = reply['call_id'],
                                     controller_matched = {'message_claim_ids': said['claim_ids'],
                                                           'proposal_claim_ids': said['proposal_claim_ids']})
    add_exchange(game, event['exchange_id'], 'detective_conversation', inviter,
                 f"{inviter}: {message['text']}\nDetective: {reply['message']}",
                 said['own_claim_ids'], [], parts, llm)


def investigation_phase(game, parts, llm):

    while True:
        decision = decide(game, parts, llm)

        if decision['action']['action'] == 'accuse':
            return decision

        play_interrogation(game, decision, parts, llm)

        # Checkpoints after interrogation 5, 10 and 15, only while further questioning remains possible (§3.4)
        used = game['interrogations_used']
        if used in CHECKPOINTS_AFTER_INTERROGATION and used < TURN_BUDGET:
            run_checkpoint(game, CHECKPOINTS_AFTER_INTERROGATION.index(used) + 1, used, parts, llm)

def default_parts():
    '''The real module functions; tests pass fakes via parts= instead.'''
    return {'memory': build_memory, 'decide': detective_decide, 'interpret': interpret,
            'respond': suspect_respond, 'match': match_delivery, 'extract': extract_ledger,
            # checkpoints (§3.4): suspect side in controller/checkpoint.py, detective side is Aravindh's
            'invite': suspect_invite, 'accept': suspect_accept, 'message': suspect_message, 'reply': suspect_reply,
            'detective_accept': detective_invitation_response, 'detective_reply': detective_conversation_reply}



def prompt_versions():
    '''The prompt versions actually running, read from the code so the log cannot drift from it
    (a hand-written config value could; Aravindh's review of #21).'''
    from detective.prompts import PROMPT_VERSION, EXTRACTOR_VERSION
    from controller.interpreter import INTERPRETER_PROMPT_VERSION
    from controller.suspect import SUSPECT_PROMPT_VERSION
    from controller.checkpoint import CHECKPOINT_PROMPT_VERSION
    return {'detective': PROMPT_VERSION, 'suspect': SUSPECT_PROMPT_VERSION,
            'interpreter': INTERPRETER_PROMPT_VERSION, 'extractor': EXTRACTOR_VERSION,
            'checkpoint': CHECKPOINT_PROMPT_VERSION}


def run_game(case_path, config, seed, condition, llm, log_dir = 'runs', parts = None):
    started = time.monotonic()
    if parts is None:
        parts = default_parts()
    case = load_case(case_path)
    game_id = f"{config.get('run_id', 'local')}_{condition}_{seed}" #unique per game

    path = Path(log_dir) / f"{game_id}.jsonl"
    log = EventLog(path, game_id); llm.log = log

    # Three kinds of failure, kept apart for accounting:
    #   FailedRun      model output still invalid after the repair -> failure logged in decide, failed_run
    #   LLMCallError   an LLM call failed for good (retries used up) -> failure logged here, failed_run
    #   anything else  a software bug -> failure 'software_crash' logged, then re-raised with no game_end,
    #                  so the orchestrator records the game as crashed, not as a model failure
    # The log is closed in every case.
    try:
        log.game_start(run_id = config.get('run_id', 'local'), case_id = case['case_id'],
                   case_sha256 = config.get('case_sha256'), condition = condition, k = MEMORY_K,
                   turn_budget = TURN_BUDGET, accusation_budget = ACCUSATION_BUDGET, seed = seed,
                   model_id = llm.model_id, decoding = llm.decoding,
                   prompt_versions = prompt_versions(), pricing = llm.pricing)

        game = new_game(case, condition, seed, log)

        try:
            decision = investigation_phase(game, parts, llm)
            result = accusation_phase(game, decision, parts, llm)
            if len(game['accusations']) == 1 and result['correct']:
                outcome = 'solved_first'
            elif len(game['accusations']) > 1 and result['correct']:
                outcome = 'solved_retry'
            else:
                outcome = 'unsolved'

        except FailedRun:
            outcome = 'failed_run'

        except LLMCallError as exc:
            log.failure(stage = 'llm_call', reason = exc.record['status'], call_id = exc.record['call_id'],
                        detail = f"{exc.record['role']}/{exc.record['purpose']}")
            outcome = 'failed_run'

        first = game['accusations'][0] if game['accusations'] else None
        solution = case['solution']
        answer = (solution['culprit'], solution['weapon'], solution['location'])
        first_correct = first == answer

        component_correct = ({'culprit': first[0] == answer[0], 'weapon': first[1] == answer[1],
                          'location': first[2] == answer[2]} if first else None)

        log.game_end(outcome = outcome, attempts_used = len(game['accusations']),
                 first_accusation_correct = first_correct, component_correct = component_correct,
                 interrogations_used = game['interrogations_used'], exchanges_total = game['exchange_index'],
                 checkpoints_reached = game['checkpoint_stats']['checkpoints_reached'],
                 conversations = game['checkpoint_stats']['conversations'],
                 invitations = game['checkpoint_stats']['invitations'],
                 cost= summarise_cost(log.events), duration_s= time.monotonic() - started)

    except Exception as exc:
        log.failure(stage = 'software_crash', reason = type(exc).__name__, detail = str(exc)[:500])
        raise

    finally:
        log.close()

    return {
        'game_id': game_id, 
        "log_path": str(path),
        "outcome": outcome,
        "first_accusations_correct": first_correct
    }