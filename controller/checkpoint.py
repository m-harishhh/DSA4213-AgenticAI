'''
Suspect side of the checkpoint conversations (proposal §3.4), owned by Harish.

Four suspect calls: invite someone (or nobody), accept or reject an invitation, write the one
message, write the one reply. Invitations and decisions are identity only (no content). Messages
use Josh's communication_options (share a claim, relay attributed testimony, propose a cover, decline);
the suspect chooses and phrases it. Unlike interrogations, the prompt keeps the full motive, whose
"You may approach the detective..." sentence is exactly checkpoint guidance.

The scheduler (order, passes, slots, logging, detective memory) is run_checkpoint in controller/game.py.
Imports come from casework/infra/controller.suspect directly, never controller.interfaces (circular import).
'''
import re

from casework import suspect_view
from casework.disclosure import communication_options
from infra import suspect_agent_ids
from controller.suspect import match_delivery

CHECKPOINT_PROMPT_VERSION = 'checkpoint-v2'   # v2: no cover proposals to the detective (pilot_07)
CHECKPOINT_MAX_OUTPUT_TOKENS = 200
DETECTIVE = 'Detective'

INVITE_SCHEMA = {'type': 'object', 'required': ['invite'], 'additionalProperties': False,
                 'properties': {'invite': {'type': ['string', 'null']}}}
ACCEPT_SCHEMA = {'type': 'object', 'required': ['accept'], 'additionalProperties': False,
                 'properties': {'accept': {'type': 'boolean'}}}

ROLE_TEXT = {
    'culprit': 'You committed the murder. You want to avoid being identified.',
    'self_protective': 'You are innocent of the murder, but you have a private secret you want to protect.',
    'truthful': 'You are innocent of the murder and you answer truthfully.',
}

# A message asking the listener to back an account up counts as a cover proposal (§5.2 coordination)
PROPOSAL_CUES = re.compile(r'\b(help|back me|back up|support|confirm|say (that|i|we)|tell (them|the detective)|cover|go along)\b')


def checkpoint_system_prompt(view, task):
    '''Role, private knowledge and rules for one suspect at a checkpoint, plus this call's task.'''
    sections = [
        f"You are {view['name']}, a staff member at the museum where a murder happened. "
        "A detective is investigating. The investigation has paused for a moment.",
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
    rules = view['instructions'] + ['Do not invent facts beyond what you know or were told.']
    sections.append('Rules:\n' + '\n'.join(f'- {rule}' for rule in rules))
    sections.append(task)
    return '\n\n'.join(sections)


def history_messages(suspect, history):
    return [{'role': 'assistant' if turn['speaker'] == suspect else 'user', 'content': turn['text']}
            for turn in history]


def suspect_call(case, suspect, state, history, task, purpose, llm, game_id, schema=None):
    view = suspect_view(case, suspect, state)
    messages = ([{'role': 'system', 'content': checkpoint_system_prompt(view, task)}]
                + history_messages(suspect, history)
                + [{'role': 'user', 'content': task.split('\n')[0]}])
    return llm.call(role = 'suspect',
                    agent_id = suspect_agent_ids(case['public']['suspects'])[suspect],
                    purpose = purpose,
                    phase = 'through_first_accusation',   # checkpoints only happen before the first accusation
                    messages = messages,
                    game_id = game_id,
                    response_schema = schema,              # the wrapper does the one format repair
                    max_output_tokens = CHECKPOINT_MAX_OUTPUT_TOKENS,
                    tags = {'prompt_version': CHECKPOINT_PROMPT_VERSION})


def render_communication_options(options):
    return '\n'.join(f'- {option["text"]}' for option in options)


def suspect_invite(case, suspect, state, history, available, llm, game_id):
    '''Invite one available participant, or nobody. Identity only.
    returns: {invite: name, "Detective" or None, call_id}; an unavailable name counts as None'''
    task = ('You may now invite ONE person for a short private conversation, or invite nobody.\n'
            'Available: ' + ', '.join(available) + '.\n'
            'An invitation contains only your name; you will say what you want only if they accept. '
            'Choose according to your motive. Reply with exactly {"invite": "<name>"} or {"invite": null}.')
    record = suspect_call(case, suspect, state, history, task, 'invitation', llm, game_id, INVITE_SCHEMA)
    choice = record['parsed']['invite']
    return {'invite': choice if choice in available else None, 'call_id': record['call_id']}


def suspect_accept(case, suspect, inviter, state, history, llm, game_id):
    '''Accept or reject an identity-only invitation.
    returns: {accept, call_id}'''
    task = (f'{inviter} invites you to a short private conversation. You do not know what it is about.\n'
            'It would be one message from them and one reply from you. '
            'Reply with exactly {"accept": true} or {"accept": false}.')
    record = suspect_call(case, suspect, state, history, task, 'acceptance', llm, game_id, ACCEPT_SCHEMA)
    return {'accept': record['parsed']['accept'], 'call_id': record['call_id']}


def suspect_message(case, suspect, recipient, state, history, llm, game_id):
    '''The inviter's one message.
    returns: {text, options, call_id}'''
    options = communication_options(case, suspect, state)
    note = ''
    if recipient == DETECTIVE:
        # pilot_07: offered the cover-proposal option, suspects asked the detective to help maintain their lie
        options = [option for option in options if option['kind'] != 'propose_cover']
        note = 'The detective is investigating the murder; never ask the detective to cover for you.\n'
    task = (f'{recipient} accepted your invitation. Write your one message to {recipient}.\n'
            + note
            + 'You may say things like these (rephrase freely; only share what you know or were told, '
            'and attribute what others told you):\n'
            + render_communication_options(options)
            + '\nReply in character, in first person, in at most three sentences.')
    record = suspect_call(case, suspect, state, history, task, 'conversation_message', llm, game_id)
    return {'text': record['text'], 'options': options, 'call_id': record['call_id']}


def suspect_reply(case, suspect, inviter, message, state, history, llm, game_id):
    '''The recipient's one reply to a suspect's message (the conversation ends after it).
    returns: {text, options, call_id}'''
    options = communication_options(case, suspect, state)
    task = (f'{inviter} says to you: "{message}"\nWrite your one reply to {inviter}; '
            'the conversation ends after it.\n'
            'You may say things like these (rephrase freely; only share what you know or were told, '
            'and attribute what others told you; you may agree or refuse to back an account):\n'
            + render_communication_options(options)
            + '\nReply in character, in first person, in at most three sentences.')
    record = suspect_call(case, suspect, state, history, task, 'conversation_reply', llm, game_id)
    return {'text': record['text'], 'options': options, 'call_id': record['call_id']}


def communicated(case, speaker, text, options):
    '''Which catalogued claims a checkpoint message actually contained, and which were proposed as a
    cover to back up. Heuristic bookkeeping like match_delivery; the post-game annotation is the
    real measurement of coordination (§5.2).
    returns: {claim_ids, own_claim_ids, proposal_claim_ids}'''
    branches = [{'claim_ids': option['claim_ids'] + option['proposal_claim_ids'],
                 'lead_ids': [], 'retract_claim_ids': []} for option in options]
    claim_ids = match_delivery(case, speaker, text, branches)['claim_ids']
    offered_covers = {cid for option in options for cid in option['proposal_claim_ids']}
    proposals = ([cid for cid in claim_ids if cid in offered_covers]
                 if PROPOSAL_CUES.search(text.lower()) else [])
    own = [cid for cid in claim_ids if case['claims'][cid]['speaker'] == speaker]
    return {'claim_ids': claim_ids, 'own_claim_ids': own, 'proposal_claim_ids': proposals}
