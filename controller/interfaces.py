"""
This file is a frozen cross module interface
it will import functions from other team packages 

The purpose of this file is to catch function name mismatches before being passed into a game loop

Disclaimer: This file is frozen, any changes requires a pr.

"""


# Casework module (owned by Josh)
from casework import load_case, public_view, suspect_view, initial_state, allowed_content, record_delivery, share_information
from casework import TURN_BUDGET, MEMORY_K# see decisions.md D6
from casework.disclosure import branch_prompt, communication_options
from casework.model import INTENTS

# Infrastructure (Owned by Aniroodh)
from infra import LLMClient, MockBackend, TransportError, LLMCallError, EventLog, ROLES, PURPOSES, PHASES, CONDITIONS
from infra import summarise_cost
# Detective (owned by Aravindh)
from detective import DecisionContext, parse_decision, parse_invitation_response, parse_conversation_reply
from detective.agent import detective_decide
from detective.memory import build_memory, extract_ledger


ACCUSATION_BUDGET = 3

# The below is parameters for conversation between agents in checkpoints
CHECKPOINTS_AFTER_INTERROGATION = (5, 10, 15)
INVITATION_PASSES = 2
MAX_CONVERSATIONS_PER_CHECKPOINT = 3 
MAX_DETECTIVE_CONVERSATIONS_PER_CHECKPOINT = 1 # per checkpoint
MAX_CONVERSATIONS_PER_PARTICIPANT = 1 #see decisions.md 
DETECTIVE_FORMAT_REPAIRS = 1 #see decisions.md
AGENT_IDS = ('detective', 'suspect_1', 'suspect_2', 'suspect_3', 'suspect_4', 'suspect_5', 'interpreter', 'extractor', 'annotator') #see decisions.md



def handle_accusation(game, action): 
    """
    Score one parsed accuse actions against the case solution 

    Returns {"correct": bool, "feedback": str, "game_over": bool}
    """

    raise NotImplementedError("Harish, wed 21")


def run_game(case_path, config, seed, condition, llm, log_dir = 'runs'):
    '''
    This functions runs the game
    To be used by Aniroodh for orchestration 

    returns: {game_id, log_path, outcome, first_accusations_correct}
    '''
    from controller.game import run_game as _run_game

    return _run_game(case_path, config, seed, condition, llm, log_dir, parts = None)


def run_checkpoint(game, checkpoint_index, after_interrogation, rng):
    """
    Allows for the agents to interact at checkpoints

    returns: {converstions[list]}, where conversation is a list of completed conversations
    """

    raise NotImplementedError("Harish, thur 22")
    

# Question interpreter (owned by Harish), implemented in controller/interpreter.py:
# interpret(question, suspect, cites, public, delivered, llm, game_id) -> {intent, cited_claim_ids, call_id}
#     maps one free-form question to one of Josh's 5 intents, using public info and delivered dialogue only
from controller.interpreter import interpret

# Suspect pipeline (owned by Harish), implemented in controller/suspect.py:
# suspect_respond(case, suspect, question, intent, state, history, llm, game_id, cited_claim_ids=())
#     -> {text, allowed_branches, instructions, call_id}; the suspect picks the branch, text delivered unchanged
# match_delivery(case, suspect, text, allowed_branches) -> {claim_ids, lead_ids, retraction_ids}
from controller.suspect import suspect_respond, match_delivery


def agent_ids_for(case): 
    '''
    This functions maps suspect names to a unique id 
    '''
    names = public_view(case)['suspects']
    result = {}

    for i, name in enumerate(names, start=1):
        result[name] = f"suspect_{i}"
    return result
