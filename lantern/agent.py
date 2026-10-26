"""Lantern's brain: a plan -> act -> verify loop on Muse Spark (Meta Model API).

How one task runs:
  1. The goal is saved to memory as a new task.
  2. Muse Spark gets: its personality, the memory briefing (facts, open and
     recent tasks), the goal, and the tool list.
  3. It calls tools. We run them locally, log every call and result, and send
     results back with previous_response_id so its reasoning carries over.
  4. It must end by calling `finish` with done/failed and a one-line summary.
     That summary is what Lantern "remembers it accomplished".
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .memory import Memory
from .tools import TOOL_SPECS, Toolbox, ToolError, _fn

PERSONAS = {
    "helpful": "You are warm, upbeat and encouraging. Short sentences.",
    "sarcastic": "You are dry and sarcastic, but you always get the job done properly. Tease lightly, never insult.",
    "smug": "You are a little arrogant and very confident, like a genius who is quietly delighted to be right. Still polite, never mean.",
}

BASE_RULES = """You are Lantern, an AI companion that lives on the user's computer and gets real work done with tools.

How you work:
- Understand the goal first. If it needs files, search before guessing paths.
- Act with tools. Never claim you did something a tool did not confirm.
- After any change, check the tool's "verified" field. If it is false, say so and try to fix it.
- If the user declines an approval, do not try a workaround; finish and explain.
- Save lasting, useful facts with `remember` (names, emails, folder habits). Never save passwords or keys.
- When the task is complete or impossible, call `finish` exactly once with status and a one-line summary of what you actually did.
- Keep replies short. Stay in character, but clarity beats jokes.
"""

FINISH_SPEC = _fn(
    "finish",
    "End the task. status='done' if the goal was achieved and verified, 'failed' if not. summary: one line of what you actually did.",
    {"status": {"type": "string", "enum": ["done", "failed"]}, "summary": {"type": "string"}},
    ["status", "summary"],
)


@dataclass
class Result:
    task_id: int
    status: str
    summary: str
    reply: str
    steps: int
    tool_calls: list = field(default_factory=list)


class Agent:
    def __init__(self, client, memory: Memory, toolbox: Toolbox,
                 model: str = "muse-spark-1.3", persona: str = "smug",
                 max_steps: int = 20, on_event=None):
        self.client = client
        self.memory = memory
        self.toolbox = toolbox
        self.model = model
        self.persona = persona if persona in PERSONAS else "smug"
        self.max_steps = max_steps
        self.on_event = on_event or (lambda kind, data: None)
        self.tools = TOOL_SPECS + [FINISH_SPEC]

    def instructions(self) -> str:
        return (
            BASE_RULES
            + f"\nPersonality: {PERSONAS[self.persona]}\n\n"
            + "# Your memory\n" + self.memory.context_block()
        )

    def run(self, goal: str) -> Result:
        task = self.memory.start_task(goal)
        self.memory.log_step(task.id, "plan", {"goal": goal})
        self.on_event("task", {"id": task.id, "goal": goal})

        calls_made: list = []
        response = self.client.responses.create(
            model=self.model,
            instructions=self.instructions(),
            input=goal,
            tools=self.tools,
        )

        for step in range(1, self.max_steps + 1):
            fcalls = [o for o in response.output if getattr(o, "type", None) == "function_call"]
            text = getattr(response, "output_text", "") or ""

            if not fcalls:
                # Plain answer with no finish call: a chat reply or a quick question.
                summary = text.strip().splitlines()[0][:200] if text.strip() else "(no reply)"
                self.memory.log_step(task.id, "reply", text)
                self.memory.finish_task(task.id, "done", summary)
                return Result(task.id, "done", summary, text, step, calls_made)

            outputs = []
            finished = None
            for fc in fcalls:
                try:
                    args = json.loads(fc.arguments or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("arguments must be an object")
                except (json.JSONDecodeError, ValueError) as e:
                    args, result = {}, {"error": f"could not parse arguments: {e}"}
                else:
                    if fc.name == "finish":
                        status = args.get("status")
                        if status not in ("done", "failed"):
                            status = "failed"
                        finished = (status, str(args.get("summary", "")).strip() or "(no summary)")
                        result = {"ok": True}
                    else:
                        try:
                            result = self.toolbox.call(fc.name, args)
                        except ToolError as e:
                            result = {"error": str(e)}

                calls_made.append({"name": fc.name, "args": args, "result": result})
                self.memory.log_step(task.id, "tool_call", {"name": fc.name, "args": args})
                self.memory.log_step(task.id, "tool_result", {"name": fc.name, "result": result})
                self.on_event("tool", {"name": fc.name, "args": args, "result": result})
                outputs.append({
                    "type": "function_call_output",
                    "call_id": fc.call_id,
                    "output": json.dumps(result, ensure_ascii=False, default=str),
                })

            if finished:
                status, summary = finished
                # Let the model say its in-character closing line.
                response = self.client.responses.create(
                    model=self.model, previous_response_id=response.id,
                    input=outputs, tools=self.tools,
                )
                reply = getattr(response, "output_text", "") or summary
                self.memory.log_step(task.id, "reply", reply)
                self.memory.finish_task(task.id, status, summary)
                return Result(task.id, status, summary, reply, step, calls_made)

            response = self.client.responses.create(
                model=self.model, previous_response_id=response.id,
                input=outputs, tools=self.tools,
            )

        summary = f"stopped after {self.max_steps} steps without finishing"
        self.memory.log_step(task.id, "note", summary)
        self.memory.finish_task(task.id, "failed", summary)
        return Result(task.id, "failed", summary, summary, self.max_steps, calls_made)
