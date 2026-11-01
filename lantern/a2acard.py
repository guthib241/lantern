"""Export an installed agent pack as an A2A Agent Card (`agent-card.json`).

Field names follow the A2A spec (https://a2a-protocol.org/latest/specification/
sections 4.4.1 AgentCard and 4.4.5 AgentSkill). Lantern doesn't serve agents
over the network, so no endpoint URL is published: `supportedInterfaces` is
left out (it has no honest value here), and so are the documentation, security
and signature fields, which have nothing to carry. See docs/DECISIONS.md.
"""
from __future__ import annotations

from .agentpacks import Pack


def card_for_pack(pack: Pack) -> dict:
    """One pack as an Agent Card: its name, description, version, the author
    as provider, and one `skills` entry (id, name, description, tags from its
    tool names, examples from its inputs' examples). Only pack metadata: no
    secrets, no local paths, no endpoint URL."""
    examples = [i.example for i in pack.inputs if i.example]
    skill: dict = {"id": pack.name, "name": pack.title,
                   "description": pack.description, "tags": list(pack.tools)}
    if examples:
        skill["examples"] = examples
    return {"name": pack.name, "description": pack.description,
            "version": pack.version, "provider": {"organization": pack.author},
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["text"], "defaultOutputModes": ["text"],
            "skills": [skill]}
