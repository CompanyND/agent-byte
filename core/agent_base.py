"""
core/agent_base.py — Základní třída pro všechny agenty (Byte, Atlas, Lucy).

Sdílí:
- Načítání personas z BB ai-personas repo
- Sestavení system promptu
- Volání Claude API
- Počítání ceny (přes core.billing)

Každý agent dědí AgentBase a přidává svou specifickou logiku.
"""

from __future__ import annotations

import asyncio
import logging
import anthropic
from dataclasses import dataclass
from typing import Optional

from core.config import cfg

logger = logging.getLogger(__name__)

PERSONAS_REPO = "ai-personas"


@dataclass
class AgentTask:
    """Vstupní úkol — sestavený z Jira kontextu."""
    ticket_id: str
    ticket_summary: str
    ticket_description: str
    ticket_status: str
    acceptance_criteria: str
    repo_slug: str
    previous_assignee: Optional[dict]
    comments: list[dict]
    stack: dict
    global_memory: str
    project_memory: str
    action: str
    extra_context: str = ""


@dataclass
class AgentResponse:
    """Výstup agenta."""
    content: str
    action: str
    metadata: dict


class AgentBase:
    """
    Základní třída pro všechny agenty.

    Podtřídy musí definovat:
    - self._agent_slug: str — "byte", "atlas", "lucy"
    - self._persona_path: str — cesta v ai-personas repo (např. "byte")
    - self._skill_names: list[str] — seznam skills k načtení

    Podtřídy mohou přepsat:
    - _resolve_skill(action) — mapování akce na skill
    - _build_user_message(task) — sestavení user zprávy
    """

    def __init__(self, agent_slug: str):
        self._agent_slug = agent_slug
        self._persona_path = agent_slug
        self._skill_names: list[str] = []

        self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        self._model_cfg = cfg.agent(agent_slug).model

        # Cache pro personas
        self._soul: Optional[str] = None
        self._persona: Optional[str] = None
        self._skills: dict[str, Optional[str]] = {}
        self._personas_loaded = False

        logger.info(f"[{agent_slug.capitalize()}] Inicializován — model: {self._model_cfg.model}")

    async def _load_personas(self):
        """
        Načte personas z BB ai-personas repo.
        Lazy load — volá se při prvním process() volání.
        Cachuje výsledky — BB API se volá jen jednou za restart.
        """
        if self._personas_loaded:
            return

        from integrations.bitbucket.client import BitbucketClient
        bb = BitbucketClient()

        logger.info(f"[{self._agent_slug.capitalize()}] Načítám personas z BB ai-personas repo...")

        # Načti SOUL + PERSONA + všechny skills paralelně
        files_to_load = [
            bb.get_file(PERSONAS_REPO, f"{self._persona_path}/SOUL.md"),
            bb.get_file(PERSONAS_REPO, f"{self._persona_path}/PERSONA.md"),
        ] + [
            bb.get_file(PERSONAS_REPO, f"{self._persona_path}/skills/{skill.upper()}.md")
            for skill in self._skill_names
        ]

        results = await asyncio.gather(*files_to_load, return_exceptions=True)

        self._soul = results[0] if isinstance(results[0], str) else ""
        self._persona = results[1] if isinstance(results[1], str) else ""

        for i, skill in enumerate(self._skill_names):
            result = results[2 + i]
            self._skills[skill] = result if isinstance(result, str) else ""

        self._personas_loaded = True

        loaded = [
            f"SOUL({'ok' if self._soul else 'CHYBÍ'})",
            f"PERSONA({'ok' if self._persona else 'CHYBÍ'})",
        ] + [
            f"{skill.upper()}({'ok' if self._skills.get(skill) else 'CHYBÍ'})"
            for skill in self._skill_names
        ]
        logger.info(f"[{self._agent_slug.capitalize()}] Personas načteny: {', '.join(loaded)}")

        if not self._soul or not self._persona:
            logger.warning(
                f"[{self._agent_slug.capitalize()}] SOUL.md nebo PERSONA.md chybí v ai-personas repo!"
            )

    def _build_system_prompt(self, task: AgentTask) -> str:
        """Sestaví system prompt ze 4 vrstev: SOUL + PERSONA + skill + paměti."""
        parts = []

        if self._soul:
            parts.append(self._soul)

        if self._persona:
            parts.append(self._persona)

        skill_name = self._resolve_skill(task.action)
        if skill_name and self._skills.get(skill_name):
            parts.append(
                f"---\n## Aktivní skill: {skill_name}\n\n{self._skills[skill_name]}"
            )

        if task.global_memory:
            parts.append(f"---\n## Paměť\n\n{task.global_memory}")

        if task.project_memory:
            parts.append(
                f"---\n## Projektová paměť ({task.repo_slug})\n\n{task.project_memory}"
            )

        return "\n\n".join(filter(None, parts))

    def _resolve_skill(self, action: str) -> Optional[str]:
        """Mapuje akci na název skill souboru. Podtřídy mohou přepsat."""
        return None

    def _build_user_message(self, task: AgentTask) -> str:
        """Sestaví user zprávu z kontextu ticketu. Podtřídy mohou přepsat."""
        lines = []

        stack_parts = []
        if task.stack.get("angular"):
            stack_parts.append(f"Angular {task.stack['angular']}")
        if task.stack.get("dotnet"):
            stack_parts.append(f".NET {task.stack['dotnet']}")
        if task.stack.get("php"):
            stack_parts.append(f"PHP {task.stack['php']}")
        if stack_parts:
            lines.append(f"**Stack projektu:** {' | '.join(stack_parts)}")

        lines.append(f"**Ticket:** {task.ticket_id} — {task.ticket_summary}")
        lines.append(f"**Stav:** {task.ticket_status}")
        lines.append(f"**Repozitář:** {task.repo_slug}")

        if task.ticket_description:
            lines.append(f"\n**Popis:**\n{task.ticket_description}")

        if task.acceptance_criteria:
            lines.append(f"\n**Acceptance criteria:**\n{task.acceptance_criteria}")

        if task.comments:
            lines.append("\n**Komentáře v ticketu:**")
            for c in task.comments[-10:]:
                lines.append(f"  [{c['author']}]: {c['body'][:500]}")

        if task.extra_context:
            lines.append(f"\n**Dodatečný kontext:**\n{task.extra_context}")

        return "\n".join(lines)

    async def process(self, task: AgentTask) -> AgentResponse:
        """
        Hlavní vstupní bod — zpracuje úkol a vrátí odpověď.
        Automaticky počítá a zapisuje cenu.
        """
        await self._load_personas()

        system_prompt = self._build_system_prompt(task)
        user_message = self._build_user_message(task)

        logger.info(
            f"[{self._agent_slug.capitalize()}] Zpracovávám {task.ticket_id} | "
            f"akce: {task.action} | model: {self._model_cfg.model}"
        )

        response = self._client.messages.create(
            model=self._model_cfg.model,
            max_tokens=self._model_cfg.max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )

        content = response.content[0].text
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens

        logger.info(
            f"[{self._agent_slug.capitalize()}] {task.ticket_id} hotovo | "
            f"tokeny: in={input_tokens} out={output_tokens}"
        )

        # Zapiš cenu — přes sdílený billing modul
        from core.billing import record_cost
        await record_cost(task.ticket_id, input_tokens, output_tokens, self._agent_slug)

        return AgentResponse(
            content=content,
            action="jira_comment",
            metadata={
                "ticket_id": task.ticket_id,
                "task_action": task.action,
                "model": self._model_cfg.model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "stack": task.stack,
            },
        )

    def reload_personas(self):
        """Vynutí znovu načtení personas — volej po změně SOUL.md nebo PERSONA.md."""
        self._personas_loaded = False
        logger.info(
            f"[{self._agent_slug.capitalize()}] "
            "Personas cache invalidována — znovu načtou se při příštím volání."
        )
