import json
from pathlib import Path

from loguru import logger

from agent.llm_client import LLMClient
from agent.profile import read_profile
from agent.tools import TOOL_DEFINITIONS, execute_tool

SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "system.md"


def _render_system_prompt() -> str:
    template = SYSTEM_PROMPT_PATH.read_text()
    profile  = read_profile()
    return template.replace("{profile}", json.dumps(profile, indent=2))


async def run(
    messages: list,
    db,
    llm: LLMClient | None = None,
) -> str:
    if llm is None:
        llm = LLMClient()

    system_prompt = _render_system_prompt()
    history = [{"role": "system", "content": system_prompt}] + messages

    for iteration in range(1, 11):
        logger.debug(f"Agent loop iteration {iteration}")

        response = llm.chat(history, TOOL_DEFINITIONS)
        message  = response.choices[0].message

        if not message.tool_calls:
            logger.debug("Agent loop complete — no tool calls in response")
            return message.content

        logger.debug(f"Tool calls requested: {[c.function.name for c in message.tool_calls]}")

        history.append(message)

        for call in message.tool_calls:
            result = await execute_tool(
                tool_name=call.function.name,
                arguments=json.loads(call.function.arguments),
                db=db,
            )
            logger.debug(f"Tool '{call.function.name}' result: {result}")
            history.append({
                "role":         "tool",
                "tool_call_id": call.id,
                "content":      result,
            })

    logger.warning("Agent loop hit max iterations — returning fallback")
    return "I ran into an issue completing that request. Please try again."
