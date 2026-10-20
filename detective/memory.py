def build_memory(condition, exchanges, ledger, k):
    """
    condition is one of none/raw/ledger
    none: latest exchange only
    raw and ledger: latest K exchanges

    returns: {window_exchange_ids, payload, visible_exchanges}
    where visible_exchange = {exchange_id: text} --> feeds DecisionContext 
    """

    raise NotImplementedError("Aravindh")


def extract_ledger(exchange, llm, game_id): 
    '''
    This function extracts entries for one delivered exchange 
    Each entry keeps speaker, text and source_span 
    returns: {entries, status, call_id}
    '''

    raise NotImplementedError("Aravindh")