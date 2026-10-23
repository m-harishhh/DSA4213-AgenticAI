from .prompts import (NEXT_STEP, PROMPT_VERSION, REPAIR_TEMPLATE, SYSTEM_PROMPT, render_public_case,
                      render_status)


def detective_decide(memory_view , context, llm, game_id, repair_errors = None, repair_of = None):
    """
    This function makes the detective to either choose to interrogate or make an accusation

    This functions calls llm.call(role = "detective", response_schema = None, ...)

    repair_errors holds the parse decision errors from the previous attempt and is used for the single repair
    repair_of is the call_id of the attempt being repaired, logged in the record's tags

    returns: {text, call_id, prompt_version} --> controller parses the text
    """
    if context.public is None:
        raise ValueError("context.public is required; build the context with DecisionContext.from_public")

    user_content = "\n\n".join([render_public_case(context.public), render_status(context),
                                memory_view["payload"], NEXT_STEP])
    if repair_errors:
        user_content += "\n\n" + REPAIR_TEMPLATE.format(errors="; ".join(repair_errors))
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_content}]

    phase = "through_first_accusation" if not context.previous_accusations else "accusation_retries"

    # LLMCallError is not caught: the controller treats it as a failed run.
    record = llm.call(role="detective", purpose="detective_decision", phase=phase, messages=messages,
                      game_id=game_id, response_schema=None,
                      max_output_tokens=context.config["max_output_tokens"],
                      tags={"repair_of": repair_of} if repair_of is not None else None)
    return {"text": record["text"], "call_id": record["call_id"], "prompt_version": PROMPT_VERSION}
