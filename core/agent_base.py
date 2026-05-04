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

    async def _get_repo_context(
        self,
        repo_slug: str,
        ticket_summary: str = "",
        ticket_description: str = "",
        stack: dict = None,
    ) -> tuple[str, str, str]:
        """
        Sdílená metoda pro všechny skills — načte kompletní kontext repozitáře:
        1. Strom repozitáře do hloubky 7
        2. Relevantní soubory (obsah) dle ticketu
        3. Posledních 10 commitů

        Vrátí (tree_str, files_context, commits_str)
        """
        from integrations.bitbucket.client import BitbucketClient
        bb = BitbucketClient()

        CONTEXT_LIMIT = 150_000
        FILE_MAX_CHARS = 10_000

        # 1. Strom repozitáře do hloubky 7
        try:
            tree = await bb.get_repo_tree(repo_slug, max_depth=7)
            tree_str = bb.format_tree(tree)
        except Exception as e:
            logger.warning(f"[AgentBase] get_repo_tree selhal: {e}")
            tree_str = ""

        # 2. Posledních 10 commitů
        try:
            commits_str = await bb.get_recent_commits(repo_slug, limit=10)
        except Exception as e:
            logger.warning(f"[AgentBase] get_recent_commits selhal: {e}")
            commits_str = ""

        # 3. Relevantní soubory — jen pokud máme ticket kontext
        files_context = ""
        if ticket_summary and tree_str:
            try:
                prompt = (
                    f"Repozitář: {repo_slug}\n"
                    f"Stack: {self._format_stack(stack or {})}\n"
                    f"Struktura repozitáře (hloubka 7):\n{tree_str[:8000]}\n\n"
                    f"Ticket: {ticket_summary}\n"
                    f"Popis: {ticket_description[:800]}\n\n"
                    f"Vypiš VŠECHNY soubory relevantní pro implementaci tohoto ticketu.\n"
                    f"Každou cestu na nový řádek. Pouze cesty, bez komentářů.\n"
                    f"Zahrň: komponenty, service, module, controller, model, DTO, interface.\n"
                    f"Čím více kontextu vidíš, tím lepší kód napíšeš."
                )

                response = self._client.messages.create(
                    model=self._model_cfg.model,
                    max_tokens=2048,
                    messages=[{"role": "user", "content": prompt}],
                )

                raw_paths = response.content[0].text.strip().split("\n")
                paths = []
                for line in raw_paths:
                    path = line.strip().lstrip("- *0123456789. ").strip()
                    if path and "." in path and not path.startswith("#"):
                        paths.append(path)

                logger.info(f"[AgentBase] {repo_slug} — Claude označil {len(paths)} relevantních souborů")

                import asyncio
                contents = await asyncio.gather(
                    *[bb.get_file(repo_slug, p) for p in paths],
                    return_exceptions=True
                )

                result = {}
                total_chars = 0
                for path, file_content in zip(paths, contents):
                    if not isinstance(file_content, str) or not file_content:
                        continue
                    truncated = file_content[:FILE_MAX_CHARS]
                    if total_chars + len(truncated) > CONTEXT_LIMIT:
                        break
                    result[path] = truncated
                    total_chars += len(truncated)

                if result:
                    parts = [f"### {p}\n```\n{c}\n```" for p, c in result.items()]
                    files_context = "\n\n".join(parts)
                    logger.info(f"[AgentBase] {repo_slug} — načteno {len(result)} souborů ({total_chars:,} znaků)")

            except Exception as e:
                logger.warning(f"[AgentBase] _get_repo_context soubory selhaly: {e}")

        return tree_str, files_context, commits_str

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