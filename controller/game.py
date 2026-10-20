'''
Game loop and controller state (Owned by Harish)
'''

from controller.interfaces import public_view, CONDITIONS, EventLog, agent_ids_for, initial_state
from controller.interfaces import MEMORY_K, TURN_BUDGET
from controller.interfaces import DecisionContext, parse_decision, record_delivery
import random


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
                                      turns_left = TURN_BUDGET - game['interrogations_used']
                                      )

    raw = parts["decide"](memory, ctx, llm, game['game_id'])

    result = parse_decision(raw['text'], ctx)

    if not result['ok']:
        repair_errors = result['errors']
        raw = parts['decide'](memory, ctx, llm, game['game_id'], repair_errors)

        result = parse_decision(raw['text'], ctx)

    
    game['log'].memory_view(decision_index = game['decision_index'], 
                            decision_type = 'decision', 
                            window_exchange_ids= memory['window_exchange_ids'],
                            memory_payload = memory['payload'],
                            call_id = raw['call_id']
                            )
    result['call_id'] = raw['call_id']
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
        'matched_claim_ids': matched['claim_ids'],
        'speaker': suspect,
        'text': f"Detective: {question}\n{suspect}: {answer['text']}",
    }
    game['exchanges'].append(exchange)

    # Ledger condition only: each exchange is extracted once, on its own (§4.2)
    if game['condition'] == 'ledger':
        extraction = parts['extract'](exchange, llm, game['game_id'])
        game['ledger'][exchange['exchange_id']] = extraction['entries']
        game['log'].extraction(exchange_id = exchange['exchange_id'],
                               call_id = extraction['call_id'],
                               status = extraction['status'],
                               entries = extraction['entries'])

    return exchange