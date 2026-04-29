"""
core/agent.py — Byte AgentRunner.

System prompt = 4 vrstvy:
  1. SOUL.md       — kdo jsem v jádru
  2. PERSONA.md    — jak se chovám
  3. skill.md      — co dělám teď (review / qa / developer)
  4. Paměti        — globální + projektová

Pak přijde kontext ticketu + zadání jako user message.
"""

from __future__ import annotations

import asyncio
import logging
import anthropic
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from core.config import cfg

logger = logging.getLogger(__name__)

# Cesta k ai-personas repo (lokální klon nebo načtené z BB)
PERSONAS_PATH = Path("ai-personas")


@dataclass
class ByteTask:
    """Vstupní úkol pro Byte — sestavený z Jira kontextu."""
    ticket_id: str
    ticket_summary: str
    ticket_description: str
    ticket_status: str
    acceptance_criteria: str
    repo_slug: str                          # z Jira komponenty
    previous_assignee: Optional[dict]       # pro PR reviewer
    comments: list[dict]
    stack: dict                             # {"angular": "17", "dotnet": "net8.0"}
    global_memory: str
    project_memory: str
    action: str                             # "chat", "program", "review", "qa", "fix"
    extra_context: str = ""                 # např. PR komentáře při opravě


@dataclass
class ByteResponse:
    """Výstup Byte — co říká a co udělat dál."""
    content: str                            # text odpovědi / komentáře
    action: str                             # "jira_comment", "create_pr", "log_only"
    metadata: dict


class ByteAgent:
    """
    Byte — senior developer + QA tester.
    Načítá osobnost z ai-personas repo, paměť z byte-memory repo.
    """

    def __init__(self):
        self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        self._model_cfg = cfg.agent("byte").model
        self._soul = self._load_persona_file("SOUL.md")
        self._persona = self._load_persona_file("PERSONA.md")
        self._skills = {
            "review": self._load_persona_file("skills/REVIEW.md"),
            "qa": self._load_persona_file("skills/QA.md"),
        }
        logger.info(f"[Byte] Inicializován — model: {self._model_cfg.model}")

    def _load_persona_file(self, relative_path: str) -> str:
        """Načte soubor z ai-personas/byte/."""
        path = PERSONAS_PATH / "byte" / relative_path
        if path.exists():
            return path.read_text(encoding="utf-8")
        logger.warning(f"[Byte] Persona soubor nenalezen: {path}")
        return ""

    def _build_system_prompt(self, task: ByteTask) -> str:
        """
        Sestaví system prompt ze 4 vrstev.
        Pořadí: SOUL → PERSONA → skill → paměti
        """
        parts = []

        # Vrstva 1 — SOUL
        if self._soul:
            parts.append(self._soul)

        # Vrstva 2 — PERSONA
        if self._persona:
            parts.append(self._persona)

        # Vrstva 3 — aktivní skill
        skill = self._resolve_skill(task.action)
        if skill and self._skills.get(skill):
            parts.append(f"---\n## Aktivní skill: {skill}\n\n{self._skills[skill]}")

        # Vrstva 4 — paměti
        if task.global_memory:
            parts.append(f"---\n## Globální paměť\n\n{task.global_memory}")
        if task.project_memory:
            parts.append(f"---\n## Projektová paměť ({task.repo_slug})\n\n{task.project_memory}")

        return "\n\n".join(filter(None, parts))

    def _resolve_skill(self, action: str) -> Optional[str]:
        mapping = {
            "review": "review",
            "qa": "qa",
            "program": None,    # programování nepoužívá extra skill, PERSONA stačí
            "chat": None,
            "fix": "review",    # při opravě použij review skill jako kontext
        }
        return mapping.get(action)

    def _build_user_message(self, task: ByteTask) -> str:
        """Sestaví user message s celým kontextem ticketu."""
        lines = []

        # Stack kontext
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
            for c in task.comments[-10:]:  # posledních 10 komentářů
                lines.append(f"  [{c['author']}]: {c['body'][:500]}")

        if task.extra_context:
            lines.append(f"\n**Dodatečný kontext:**\n{task.extra_context}")

        # Akce
        action_instructions = {
            "chat": (
                "\n**Akce:** Jsem v CHAT režimu (ticket není In Progress). "
                "Přečti zadání, zeptej se na nejasnosti, navrhni přístup. NEPROGRAMUJ."
            ),
            "program": (
                "\n**Akce:** Ticket je In Progress. Začni programovat. "
                "Napiš co uděláš (branch, přístup) a případné otázky než začneš."
            ),
            "review": (
                "\n**Akce:** Proveď code review dle REVIEW skill pravidel."
            ),
            "qa": (
                "\n**Akce:** Proveď QA testování dle QA skill pravidel."
            ),
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
        output_action = self._resolve_output_action(task.action)

        logger.info(
            f"[Byte] {task.ticket_id} hotovo | "
            f"tokeny: in={response.usage.input_tokens} out={response.usage.output_tokens}"
        )

        return ByteResponse(
            content=content,
            action=output_action,
            metadata={
                "ticket_id": task.ticket_id,
                "task_action": task.action,
                "model": self._model_cfg.model,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "stack": task.stack,
            },
        )

    def _resolve_output_action(self, task_action: str) -> str:
        mapping = {
            "chat": "jira_comment",
            "program": "jira_comment",     # oznámení že začíná + branch
            "review": "jira_comment",
            "qa": "jira_comment",
            "fix": "jira_comment",
        }
        return mapping.get(task_action, "jira_comment")


# Singleton
_byte: Optional[ByteAgent] = None

def get_byte() -> ByteAgent:
    global _byte
    if _byte is None:
        _byte = ByteAgent()
    return _byte
