'''
Question interpreter (owned by Harish): one free-form detective question -> one of Josh's 5 intents.

Proposal Section 3.3: a fixed component that uses the question and permitted dialogue context only,
never the hidden case or the detective's memory condition. It sees the public case and every
exchange the detective has been delivered (not the memory window), so it behaves identically
across conditions. Topics come only from leads already mentioned to the detective.

Imports come from casework/infra directly, never controller.interfaces (circular import).
'''
from casework.model import INTENTS

INTERPRETER_PROMPT_VERSION = 'interpreter-v1'
INTERPRETER_MAX_OUTPUT_TOKENS = 200

INTERPRETER_SYSTEM = '''You classify one question that a detective asks a suspect into exactly one intent.

Intents:
- "overview": a broad question about the suspect's evening, what they saw or what they know.
- "ask_lead": a follow-up on a topic someone mentioned they have information about. Set "topic" to one of the listed topic keys.
- "whereabouts": where the suspect themselves was at a time. Set "time".
- "corroborate": whether the suspect saw or knows where ANOTHER person was at a time. Set "subject" and "time".
- "challenge": confronting the suspect with evidence that an earlier statement of theirs is false. Set "challenged" to the exchange ID of their earlier statement, and "evidence" to the exchange IDs of the contradicting statements.

If a question covers several targets, choose the main one. Use null for fields that do not apply. Use only the times, names, topic keys and exchange IDs listed below; never invent one.
Reply with exactly one JSON object and nothing else.'''

INTERPRETER_SCHEMA = {
    'type': 'object',
    'required': ['kind', 'time', 'subject', 'topic', 'challenged', 'evidence'],
    'additionalProperties': False,
    'properties': {
        'kind': {'type': 'string', 'enum': list(INTENTS)},
        'time': {'type': ['string', 'null']},
        'subject': {'type': ['string', 'null']},
        'topic': {'type': ['string', 'null']},
        'challenged': {'type': ['string', 'null']},
        'evidence': {'type': 'array', 'items': {'type': 'string'}},
    },
}


def heard_topics(delivered):
    '''{lead_id: label} for every lead the detective has had mentioned to it so far.'''
    topics = {}
    for exchange in delivered.values():
        topics.update(exchange.get('lead_labels', {}))
    return topics


def interpreter_user_prompt(question, suspect, cites, public, delivered):
    topics = heard_topics(delivered)
    own = [e for e in delivered.values() if e.get('speaker') == suspect]
    lines = [f'Suspect being asked: {suspect}',
             'Suspects: ' + ', '.join(public['suspects']),
             'Times: ' + ', '.join(public['times']),
             'Topic keys mentioned so far: ' + (', '.join(f'{lid} ({label})' for lid, label in topics.items()) or 'none')]
    lines.append(f'Earlier exchanges with {suspect}:')
    lines += [f"[{e['exchange_id']}] {e['text']}" for e in own] or ['(none)']
    others = [e for e in delivered.values() if e.get('speaker') != suspect]
    lines.append('Earlier exchanges with others:')
    lines += [f"[{e['exchange_id']}] {e['text']}" for e in others] or ['(none)']
    if cites:
        lines.append('The detective cites: ' + ', '.join(cites))
    lines.append(f'Question: {question}')
    return '\n'.join(lines)


def build_intent(parsed, suspect, cites, public, delivered):
    '''Turn the model's answer into Josh's intent dict plus the cited claim IDs.
    Invalid fields are dropped (set to None), so allowed_content falls back to a decline
    instead of crashing; they stay visible in the log for the interpretation audit.'''
    kind = parsed['kind']
    time = parsed['time'] if parsed['time'] in public['times'] else None

    if kind == 'whereabouts':
        intent = {'kind': kind, 'time': time}
    elif kind == 'corroborate':
        subject = parsed['subject'] if parsed['subject'] in public['suspects'] else None
        intent = {'kind': kind, 'subject': subject, 'time': time}
    elif kind == 'ask_lead':
        topic = parsed['topic'] if parsed['topic'] in heard_topics(delivered) else None
        intent = {'kind': kind, 'topic': topic}
    elif kind == 'challenge':
        intent = {'kind': kind,
                  'claim_id': challenged_claim(parsed['challenged'], suspect, cites, parsed['evidence'], delivered)}
    else:
        intent = {'kind': 'overview'}

    # Counter-evidence: claims from the cited and referenced exchanges, never the challenged claim itself
    cited_claim_ids = [cid for cid in evidence_claims(cites, parsed['evidence'], delivered)
                       if cid != intent.get('claim_id')]
    return intent, cited_claim_ids


def evidence_claims(cites, evidence, delivered):
    '''{claim_id: key} from the exchanges the detective cited plus those the question refers to,
    only ones actually delivered to the detective.'''
    claims = {}
    for eid in list(cites) + list(evidence):
        if eid in delivered:
            claims.update(delivered[eid].get('claim_keys', {}))
    return claims


def challenged_claim(challenged_id, suspect, cites, evidence, delivered):
    '''The suspect's own earlier claim the challenge is about.

    Prefer a claim on the same topic as the evidence (same key, e.g. position:Alex:21:00), so
    "Drew saw you in the Library at 21:00" targets Alex's 21:00 account, not their 20:30 one.
    Search the challenged exchange first, then the suspect's other answers (newest first), because
    the detective often points at a later answer that repeated the statement without matching it.
    Falls back to the challenged exchange's last claim; None if the suspect made no matched claim.'''
    own = [e for e in delivered.values() if e.get('speaker') == suspect and e.get('claim_keys')]
    first = [e for e in own if e['exchange_id'] == challenged_id]
    candidates = first + [e for e in reversed(own) if e['exchange_id'] != challenged_id]
    evidence_keys = set(evidence_claims(cites, evidence, delivered).values())

    for exchange in candidates:
        for cid, key in exchange['claim_keys'].items():
            if key in evidence_keys:
                return cid
    if first:
        return list(first[0]['claim_keys'])[-1]
    return None


def interpret(question, suspect, cites, public, delivered, llm, game_id):
    '''
    Maps one free-form detective question to one of Josh's 5 intents.
    cites: exchange IDs from the detective's interrogate action
    delivered: {exchange_id: exchange} for every exchange the detective has been delivered,
               each with speaker, text, matched_claim_ids and lead_labels
    returns: {intent, cited_claim_ids, call_id}
    '''
    record = llm.call(role = 'interpreter',
                      purpose = 'intent_interpretation',
                      phase = 'through_first_accusation',   # questions only happen before the first accusation
                      messages = [{'role': 'system', 'content': INTERPRETER_SYSTEM},
                                  {'role': 'user', 'content': interpreter_user_prompt(question, suspect, cites,
                                                                                     public, delivered)}],
                      game_id = game_id,
                      response_schema = INTERPRETER_SCHEMA,   # the wrapper does the one format repair
                      max_output_tokens = INTERPRETER_MAX_OUTPUT_TOKENS,
                      tags = {'prompt_version': INTERPRETER_PROMPT_VERSION})
    intent, cited_claim_ids = build_intent(record['parsed'], suspect, cites, public, delivered)
    return {'intent': intent, 'cited_claim_ids': cited_claim_ids, 'call_id': record['call_id']}
