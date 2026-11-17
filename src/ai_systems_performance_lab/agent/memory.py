"""Optional case-scoped Amazon Bedrock AgentCore Memory adapter."""

from __future__ import annotations

import asyncio
from typing import Any


class AgentCoreCaseMemory:
    """Supplemental recall scoped to one synthetic case, never a fact store.

    The AgentCore Memory resource and its strategy/namespace configuration must
    exist before this adapter is enabled. Retrieval failures are intentionally
    surfaced to the caller so the request layer can apply its fail-open policy.
    """

    def __init__(
        self, *, memory_id: str, region_name: str, namespace_template: str
    ) -> None:
        from bedrock_agentcore.memory import MemoryClient

        self.client = MemoryClient(region_name=region_name)
        self.memory_id = memory_id
        self.region_name = region_name
        self.namespace_template = namespace_template

    def _namespace(self, case_id: str) -> str:
        return self.namespace_template.format(actorId=case_id, case_id=case_id)

    async def recall(self, *, case_id: str, query: str) -> list[str]:
        records = await asyncio.to_thread(
            self.client.retrieve_memories,
            self.memory_id,
            namespace=self._namespace(case_id),
            query=query,
            actor_id=case_id,
            top_k=3,
        )
        return [text for item in records if (text := _record_text(item))]

    async def remember(
        self, *, case_id: str, session_id: str, user_text: str, assistant_text: str
    ) -> None:
        await asyncio.to_thread(
            self.client.create_event,
            self.memory_id,
            actor_id=case_id,
            session_id=session_id,
            messages=[(user_text, "USER"), (assistant_text, "ASSISTANT")],
        )


def _record_text(record: Any) -> str | None:
    """Extract only text from the SDK's memory-record envelope."""
    content = record.get("content") if isinstance(record, dict) else None
    if isinstance(content, dict):
        value = content.get("text")
        return value if isinstance(value, str) else None
    if isinstance(content, list):
        values = [part.get("text") for part in content if isinstance(part, dict)]
        return "\n".join(value for value in values if isinstance(value, str)) or None
    return None
