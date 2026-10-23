'''
Game loop and controller state (Owned by Harish)
'''

from controller.interfaces import public_view, CONDITIONS, EventLog, agent_ids_for, initial_state
from controller.interfaces import MEMORY_K, TURN_BUDGET, ACCUSATION_BUDGET
from controller.interfaces import DecisionContext, parse_decision, record_delivery, load_case
from controller.interfaces import LLMCallError, summarise_cost
from controller.interfaces import build_memory, detective_decide, interpret, suspect_respond, match_delivery, extract_ledger
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
    }

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
    
    memory = parts["memory"](game["condition"], game["exchanges"], game['ledger'], MEMORY_K)

    ctx = DecisionContext.from_public(game['public'],
                                      visible_exchanges = memory["visible_exchanges"],
                                      previous_accusations = game['accusations'],
                                      turns_left = 0 if game.get('testimony_frozen') else TURN_BUDGET - game['interrogations_used']
                                      )

    raw = parts["decide"](memory, ctx, llm, game['game_id'])

    result = parse_decision(raw['text'], ctx)

    if not result['ok']:
        repair_errors = result['errors']
        raw = parts['decide'](memory, ctx, llm, game['game_id'], 
                              repair_errors = repair_errors, repair_of = raw['call_id'])

        result = parse_decision(raw['text'], ctx)

    
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

    exchange = {
        'exchange_id': event['exchange_id'],
        'kind': 'interrogation',
        'claim_keys': {cid: case['claims'][cid]['key'] for cid in matched['claim_ids']},   # what each claim is about (interpreter)
        'lead_labels': {lid: case['leads'][lid]['label'] for lid in matched['lead_ids']},   # topics now heard (interpreter)
        'matched_claim_ids': matched['claim_ids'],
        'speaker': suspect,
        'text': f"Detective: {question}\n{suspect}: {answer['text']}",
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


def investigation_phase(game, parts, llm):

    while True:
        decision = decide(game, parts, llm)

        if decision['action']['action'] == 'accuse':
            return decision

        play_interrogation(game, decision, parts, llm)

def default_parts():
    '''The real module functions; tests pass fakes via parts= instead.'''
    return {'memory': build_memory, 'decide': detective_decide, 'interpret': interpret,
            'respond': suspect_respond, 'match': match_delivery, 'extract': extract_ledger}



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
                   prompt_versions = config.get('prompt_versions'), pricing = llm.pricing)

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
                 checkpoints_reached= 0, conversations= {}, invitations = {},     # filled in by checkpoints (Thu)
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