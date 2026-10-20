def detective_decide(memory_view , context, llm, game_id, repair_errors = None):
    """
    This function makes the detective to either choose to interrogate or make an accusation 
    
    This functions calls llm.call(role = "detective", response_schema = None, ...)

    repair_errors holds the parse decision errors from the previous attempt and is used for the single repair 

    returns: {text, call_id} --> controller parses the text 
    """
    raise NotImplementedError("Aravindh, Tue 20")