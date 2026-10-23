'''
Suspect response pipeline (owned by Harish).
Imports come from casework/infra directly, never controller.interfaces (circular import).
'''
import re

from casework import suspect_view
from casework.disclosure import allowed_content, branch_prompt
from infra import suspect_agent_ids

SUSPECT_PROMPT_VERSION = 'suspect-v2'   # v2: no checkpoint rule in interrogations; no-invention rule (pilot_02)
SUSPECT_MAX_OUTPUT_TOKENS = 200   # bounded answers (proposal §3.5); revisit after the pilot

ROLE_TEXT = {
    'culprit': 'You committed the murder. You want to avoid being identified.',
    'self_protective': 'You are innocent of the murder, but you have a private secret you want to protect.',
    'truthful': 'You are innocent of the murder and you answer truthfully.',
}


def render_options(options):
    '''The allowed answers as numbered plain-text options. No internal IDs are shown.'''
    lines = []
    for number, option in enumerate(options, start=1):
        parts = []
        if option['required_claims']:
            parts.append('state ' + ', '.join(f'"{text}"' for text in option['required_claims']))
        if option['required_leads']:
            parts.append('mention ' + ', '.join(f'"{text}"' for text in option['required_leads']))
        if option['retraction_required']:
            parts.append('correct your earlier account (this is not a confession)')
        if not parts:
            parts.append('decline to answer this question')
        elif option['may_withhold_other_details']:
            parts.append('you may keep other details to yourself')
        lines.append(f'Option {number}: ' + '; '.join(parts) + '.')
    return '\n'.join(lines)


def suspect_system_prompt(view, options):
    '''Role, private knowledge, rules and this turn's allowed answers for one suspect.'''
    sections = [
        f"You are {view['name']}, a staff member being questioned by a detective about a murder at the museum.",
        ROLE_TEXT[view['role']],
        f"Your motive: {view['motive']}",
        f"Your secret: {view['secret']}",
        'What you know:\n' + '\n'.join(f'- {fact}' for fact in view['known_facts']),
    ]
    if view['received_testimony']:
        sections.append('What others told you (their claims, not verified facts):\n'
                        + '\n'.join(f'- {claim}' for claim in view['received_testimony']))
    if view['approved_false_accounts']:
        sections.append('The only false account you may give:\n'
                        + '\n'.join(f'- {claim}' for claim in view['approved_false_accounts']))
    # The checkpoint rule only applies at checkpoints; shown here, suspects offered invitations mid-interrogation (pilot_02)
    rules = [rule for rule in view['instructions'] if not rule.startswith('At checkpoints')]
    # Josh's branch instruction ("Do not invent additional facts...") is the same for every option; state it once
    rules += sorted({option['instruction'] for option in options})
    rules.append('If asked about something not in what you know, say you do not know rather than describing it.')
    sections.append('Rules:\n' + '\n'.join(f'- {rule}' for rule in rules))
    sections.append('For this question, choose exactly ONE of these options yourself, based on your role and motive:\n'
                    + render_options(options)
                    + '\nReply in character, in first person, in at most three sentences. '
                      'Do not mention options, rules or instructions.')
    return '\n\n'.join(sections)

NEGATIONS = ('not', 'never', 'no', 'nothing', 'nowhere')
NEGATION_WINDOW = 6   # words before a match that are checked for a negation


def mentions(text, phrase):
    '''True if phrase occurs in text at least once without a negation just before it.
    "I was NOT in the Library" does not mention "library"; "I was in the Library" does.'''
    start = text.find(phrase)
    while start != -1:
        clause = re.split(r'[,.;:!?]', text[:start])[-1]    # "No, I was in the..." -> " i was in the "
        before = clause.replace("n't", ' not').split()[-NEGATION_WINDOW:]
        if not any(word.strip('"\'') in NEGATIONS for word in before):
            return True
        start = text.find(phrase, start + 1)
    return False


def match_delivery(case, suspect, text, allowed_branches):
    '''
    Which allowed claims, leads and retractions the suspect's answer actually contained.
    Heuristic for online bookkeeping only: testimony is never changed, and the real
    measurement is the post-game annotation.

    returns: {claim_ids, lead_ids, retraction_ids}
    '''
    text = text.lower()

    # 1. Candidates: every claim_id and lead_id in any allowed branch (no duplicates)
    candidate_claims = []
    candidate_leads = []
    for branch in allowed_branches:
        for cid in branch['claim_ids']:
            if cid not in candidate_claims:
                candidate_claims.append(cid)
        for lid in branch['lead_ids']:
            if lid not in candidate_leads:
                candidate_leads.append(lid)

    # 2. Claims: delivered if the claim's value is in the text.
    # Position claims also need their time, since a room alone could refer to another time.
    claim_ids = []
    for cid in candidate_claims:
        claim = case['claims'][cid]
        if not mentions(text, str(claim['value']).lower()):
            continue
        if claim['key'].startswith('position:'):
            time = claim['key'].split(':', 2)[2]    # 'position:Alex:21:00' -> '21:00'
            if time not in text:
                continue
        claim_ids.append(cid)

    # 3. Leads: delivered if the lead's keywords are in the text
    #    "the attacker's badge"        -> 'badge'
    #    "Alex's whereabouts at 21:00" -> 'alex' and '21:00' (the time alone is too common)
    lead_ids = []
    for lid in candidate_leads:
        words = case['leads'][lid]['label'].lower().split()
        if 'whereabouts' in words:
            keywords = [words[0].removesuffix("'s"), words[-1]]
        else:
            keywords = [words[-1]]
        if mentions(text, keywords[0]) and all(keyword in text for keyword in keywords[1:]):
            lead_ids.append(lid)

    # 4. Retractions: a retraction branch counts only if its true account was actually given
    retraction_ids = []
    for branch in allowed_branches:
        if branch['retract_claim_ids'] and any(cid in claim_ids for cid in branch['claim_ids']):
            for cid in branch['retract_claim_ids']:
                if cid not in retraction_ids:
                    retraction_ids.append(cid)

    return {'claim_ids': claim_ids, 'lead_ids': lead_ids, 'retraction_ids': retraction_ids}


def suspect_respond(case, suspect, question, intent, state, history, llm, game_id, cited_claim_ids=()):
    '''
    One suspect answer to one detective question.
    The suspect model chooses among all allowed branches itself (proposal §3.3);
    the controller never forces the false one. The text is delivered unchanged.

    history: this suspect's own dialogue so far, [{speaker, text}] (private to the suspect)
    returns: {text, allowed_branches, instructions, call_id}
    '''
    branches = allowed_content(case, suspect, intent, state, cited_claim_ids)
    options = [branch_prompt(case, branch) for branch in branches]

    messages = [{'role': 'system', 'content': suspect_system_prompt(suspect_view(case, suspect, state), options)}]
    for turn in history:
        role = 'assistant' if turn['speaker'] == suspect else 'user'
        messages.append({'role': role, 'content': turn['text']})
    messages.append({'role': 'user', 'content': question})

    record = llm.call(role = 'suspect',
                      agent_id = suspect_agent_ids(case['public']['suspects'])[suspect],
                      purpose = 'interrogation_response',
                      phase = 'through_first_accusation',   # suspects only answer before the first accusation
                      messages = messages,
                      game_id = game_id,
                      max_output_tokens = SUSPECT_MAX_OUTPUT_TOKENS,
                      tags = {'prompt_version': SUSPECT_PROMPT_VERSION})

    return {'text': record['text'], 'allowed_branches': branches,
            'instructions': options, 'call_id': record['call_id']}
