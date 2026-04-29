"""
core/agent.py — Byte AgentRunner.

Personas se načítají za běhu z BB API (ai-personas repo v Bitbucket).
Žádná závislost na lokálním filesystému — Railway deployuje jen kód.

System prompt = 4 vrstvy:
  1. SOUL.md       — kdo jsem v jádru
  2. PERSONA.md    — jak se chovám
  3. skill.md      — co dělám teď (review / qa / developer)
  4. Paměti        — globální + projektová (z byte-memory repo)
"""

from __future__ import annotations

import asyncio
import logging
import anthropic
from dataclasses import dataclass
from typing import Optional

from core.config import cfg

logger = logging.getLogger(__name__)

# BB repozitář kde žijí osobnosti všech agentů
PERSONAS_REPO = "ai-personas"


@dataclass
class ByteTask:
    """Vstupní úkol pro Byte — sestavený z Jira kontextu."""
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
    action: str                   # "chat", "program", "review", "qa", "fix"
    extra_context: str = ""


@dataclass
class ByteResponse:
    """Výstup Byte."""
    content: str
    action: str
    metadata: dict


class ByteAgent:
    """
    Byte — senior developer + QA tester.
    Načítá osobnost z ai-personas BB repo za běhu.
    """

    def __init__(self):
        self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        self._model_cfg = cfg.agent("byte").model

        # Cache pro personas — načtou se při prvním použití
        self._soul: Optional[str] = None
        self._persona: Optional[str] = None
        self._skills: dict[str, Optional[str]] = {
            "review": None,
            "qa": None,
        }
        self._personas_loaded = False

        logger.info(f"[Byte] Inicializován — model: {self._model_cfg.model}")

    async def _load_personas(self):
        """
        Načte personas z BB ai-personas repo.
        Volá se lazy — při prvním process() volání.
        Cachuje výsledky — BB API se volá jen jednou za restart.
        """
        if self._personas_loaded:
            return

        # Import zde aby nedošlo k circular importu
        from integrations.bitbucket.client import BitbucketClient
        bb = BitbucketClient()

        logger.info("[Byte] Načítám personas z BB ai-personas repo...")

        # Paralelní načítání všech souborů
        results = await asyncio.gather(
            bb.get_file(PERSONAS_REPO, "byte/SOUL.md"),
            bb.get_file(PERSONAS_REPO, "byte/PERSONA.md"),
            bb.get_file(PERSONAS_REPO, "byte/skills/REVIEW.md"),
            bb.get_file(PERSONAS_REPO, "byte/skills/QA.md"),
            return_exceptions=True,
        )

        soul, persona, review_skill, qa_skill = results

        self._soul = soul if isinstance(soul, str) else ""
        self._persona = persona if isinstance(persona, str) else ""
        self._skills["review"] = review_skill if isinstance(review_skill, str) else ""
        self._skills["qa"] = qa_skill if isinstance(qa_skill, str) else ""
        self._personas_loaded = True

        loaded = [
            f"SOUL({'ok' if self._soul else 'CHYBÍ'})",
            f"PERSONA({'ok' if self._persona else 'CHYBÍ'})",
            f"REVIEW({'ok' if self._skills['review'] else 'CHYBÍ'})",
            f"QA({'ok' if self._skills['qa'] else 'CHYBÍ'})",
        ]
        logger.info(f"[Byte] Personas načteny: {', '.join(loaded)}")

        if not self._soul or not self._persona:
            logger.warning(
                "[Byte] SOUL.md nebo PERSONA.md chybí v ai-personas repo! "
                "Nahraj soubory do: bitbucket.org/netdirect-custom-solution/ai-personas"
            )

    def _build_system_prompt(self, task: ByteTask) -> str:
        """Sestaví system prompt ze 4 vrstev."""
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
            parts.append(f"---\n## Globální paměť\n\n{task.global_memory}")

        if task.project_memory:
            parts.append(
                f"---\n## Projektová paměť ({task.repo_slug})\n\n{task.project_memory}"
            )

        return "\n\n".join(filter(None, parts))

    def _resolve_skill(self, action: str) -> Optional[str]:
        return {
            "review": "review",
            "qa": "qa",
            "fix": "review",
            "program": None,
            "chat": None,
        }.get(action)

    def _build_user_message(self, task: ByteTask) -> str:
        lines = []

        # Stack
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

        action_instructions = {
            "chat": (
                "\n**Akce:** Jsem v CHAT režimu (ticket není In Progress). "
                "Přečti zadání, zeptej se na nejasnosti, navrhni přístup. NEPROGRAMUJ."
            ),
            "program": (
                "\n**Akce:** Ticket je In Progress. Začni programovat. "
                "Napiš co uděláš (branch, přístup) a případné otázky než začneš."
            ),
            "review": "\n**Akce:** Proveď code review dle REVIEW skill pravidel.",
            "qa": "\n**Akce:** Proveď QA testování dle QA skill pravidel.",
            "fix": (
                "\n**Akce:** Zapracuj komentáře z PR. "
                "Přečti je, shrň co opravíš a začni."
            ),
        }
        if task.action in action_instructions:
            lines.append(action_instructions[task.action])

        return "\n".join(lines)

    async def process(self, task: ByteTask) -> ByteResponse:
        """Hlavní vstupní bod — zpracuje úkol a vrátí odpověď."""

        # Lazy load personas z BB
        await self._load_personas()

        system_prompt = self._build_system_prompt(task)
        user_message = self._build_user_message(task)

        logger.info(
            f"[Byte] Zpracovávám {task.ticket_id} | akce: {task.action} | "
            f"stack: {task.stack} | model: {self._model_cfg.model}"
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
            f"[Byte] {task.ticket_id} hotovo | "
            f"tokeny: in={input_tokens} out={output_tokens}"
        )

        return ByteResponse(
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
        """Vynutí znovu načtení personas z BB — volej po změně SOUL.md nebo PERSONA.md."""
        self._personas_loaded = False
        logger.info("[Byte] Personas cache invalidována — znovu načtou se při příštím volání.")


# Singleton
_byte: Optional[ByteAgent] = None


def get_byte() -> ByteAgent:
    global _byte
    if _byte is None:
        _byte = ByteAgent()
    return _byte
