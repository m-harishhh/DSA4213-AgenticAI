"""
This file is a frozen cross module interface
it will import functions from other team packages 

The purpose of this file is to catch function name mismatches before being passed into a game loop

Disclaimer: This file is frozen, any changes requires a pr.

"""


# Casework module (owned by Josh)
from casework import load_case, public_view, suspect_view, initial_state, allowed_content, record_delivery, share_information
from casework import TURN_BUDGET, MEMORY_K # see decisions.md D6
from casework.disclosure import branch_prompt, communication_options
from casework.model import INTENTS

# Infrastructure (Owned by Aniroodh)
from infra import LLMClient, MockBackend, TransportError, LLMCallError, EventLog, ROLES, PURPOSES, PHASES, CONDITIONS

# Detective (owned by Aravindh)
from detective import DecisionContext, parse_decision, parse_invitation_response, parse_conversation_reply
from detective.agent import detective_decide
from detective.memory import build_memory, extract_ledger



# Agreed Rules 
TURN_BUDGET = 20 # fixed budget, see decisions.md D6
ACCUSATION_BUDGET = 3 

# The below is parameters for conversation between agents in checkpoints
CHECKPOINTS_AFTER_INTERROGATION = (5, 10, 15)
INVITATION_PASSES = 2
MAX_CONVERSATIONS_PER_CHECKPOINT = 3 
MAX_DETECTIVE_CONVERSATIONS_PER_CHECKPOINT = 1 # per checkpoint
MAX_CONVERSATIONS_PER_PARTICIPANT = 1 #see decisions.md 
DETECTIVE_FORMAT_REPAIRS = 1 #see decisions.md
AGENT_IDS = ('detective', 'suspect_1', 'suspect_2', 'suspect_3', 'suspect_4', 'suspect_5', 'interpreter', 'extractor', 'annotator') #see decisions.md
MEMORY_K = 8 # fixed budget, see decisions.md D6


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
    raise NotImplementedError("Harish, Tue 20")


def run_checkpoint(game, checkpoint_index, after_interrogation, rng):
    """
    Allows for the agents to interact at checkpoints

    returns: {converstions[list]}, where conversation is a list of completed conversations
    """

    raise NotImplementedError("Harish, thur 22")
    

def interpret(question, suspect, cites, public, delivered, llm, game_id):
    '''
    Maps one free-form detective question to one of Josh's 5 intents. Cites are exchange IDs from the detective
    delivered: {exchange_id: {suspect, text, matched_claim_ids}} for every exchange the detective has seen
    
    returns: {intent, cited_claim_ids, call_id}
    '''
    raise NotImplementedError("Harish, tue 20")

def suspect_respond(case, suspect, question, intent, state, history, llm, game_id, cited_claim_ids=()): 
    """
    This function aids the suspect repsonse generation
    Rules: 
    1) The suspect picks among the allowed branches (the controller never forces the false one)
    2) Text is delivered unchanged 

    returns {text, allowed_branches, call_id} 
    """
    raise NotImplementedError("Harish, wed 21")

def match_delivery(case, suspect, text, allowed_branches):
    """
    This functions feeds Josh's record_delivery which known claims, 
    leads and retractions the delivered text actually contain

    returns; {claim_ids, lead_ids, retraction_ids}
    """
    
    raise NotImplementedError("Harish, wed 21")


def agent_ids_for(case): 
    '''
    This functions maps suspect names to a unique id 
    '''
    names = public_view(case)['suspects']
    result = {}

    for i, name in enumerate(names, start=1):
        result[name] = f"suspect_{i}"
    return result
