from .action_schema import parse_conversation_reply, parse_invitation_response
from .prompts import (ACCEPT_TEMPLATE, CHECKPOINT_PROTOCOL, CHECKPOINT_SYSTEM_PROMPT, NEXT_STEP, PROMPT_VERSION,
                      REPAIR_TEMPLATE, REPLY_TEMPLATE, SYSTEM_PROMPT, render_checkpoint_status,
                      render_public_case, render_status)


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


def _checkpoint_call(block, purpose, max_tokens_key, parse, memory_view, context, llm, game_id):
    """One checkpoint call (§3.4) with exactly one repair (§3.5). Nothing is carried between calls.

    returns: (parse result, call_id of the last call)
    """
    if context.public is None:
        raise ValueError("context.public is required; build the context with DecisionContext.from_public")

    user_content = "\n\n".join([render_public_case(context.public), render_checkpoint_status(context),
                                memory_view["payload"], CHECKPOINT_PROTOCOL, block])

    def call(content, tags=None):
        # LLMCallError is not caught: the controller treats it as a failed run.
        return llm.call(role="detective", purpose=purpose, phase="through_first_accusation",
                        messages=[{"role": "system", "content": CHECKPOINT_SYSTEM_PROMPT},
                                  {"role": "user", "content": content}],
                        game_id=game_id, response_schema=None,
                        max_output_tokens=context.config[max_tokens_key], tags=tags)

    record = call(user_content)
    result = parse(record["text"], context)
    if not result["ok"]:
        repair = REPAIR_TEMPLATE.format(errors="; ".join(result["errors"]))
        record = call(user_content + "\n\n" + repair, tags={"repair_of": record["call_id"]})
        result = parse(record["text"], context)
    return result, record["call_id"]


def detective_invitation_response(inviter, memory_view, context, llm, game_id):
    """
    Accept or reject one suspect's invitation, from the detective's memory and the inviter's identity only

    returns: {accept, ok, errors, call_id, prompt_version}; ok=False after the repair is a failed run
    """
    result, call_id = _checkpoint_call(ACCEPT_TEMPLATE.format(inviter=inviter), "acceptance",
                                       "max_acceptance_tokens", parse_invitation_response,
                                       memory_view, context, llm, game_id)
    return {"accept": result["action"]["accept"] if result["ok"] else None, "ok": result["ok"],
            "errors": result["errors"], "call_id": call_id, "prompt_version": PROMPT_VERSION}


def detective_conversation_reply(inviter, message, memory_view, context, llm, game_id):
    """
    One bounded reply to the inviter's message; the suspect does not answer it

    returns: {message, ok, errors, call_id, prompt_version}; ok=False after the repair is a failed run
    """
    block = REPLY_TEMPLATE.format(inviter=inviter, message=message, max_chars=context.config["max_reply_chars"])
    result, call_id = _checkpoint_call(block, "conversation_reply", "max_reply_tokens", parse_conversation_reply,
                                       memory_view, context, llm, game_id)
    return {"message": result["action"]["message"] if result["ok"] else None, "ok": result["ok"],
            "errors": result["errors"], "call_id": call_id, "prompt_version": PROMPT_VERSION}
