"""
agents/byte/agent.py — Byte AgentRunner.

Dědí z AgentBase — sdílená logika (personas, Claude API, billing).
Byte-specifické: skill routing, user message s action instructions.
"""

from __future__ import annotations

import logging
from typing import Optional

from core.agent_base import AgentBase, AgentTask, AgentResponse

logger = logging.getLogger(__name__)

# Zpětná kompatibilita — ByteTask a ByteResponse jsou aliasy
ByteTask = AgentTask
ByteResponse = AgentResponse


class ByteAgent(AgentBase):
    """
    Byte — senior developer + QA tester + E2E tester.
    Načítá osobnost z ai-personas BB repo za běhu.
    """

    def __init__(self):
        super().__init__("byte")
        self._skill_names = ["review", "qa", "e2e"]

    def _resolve_skill(self, action: str) -> Optional[str]:
        return {
            "review": "review",
            "qa": "qa",
            "e2e_test": "e2e",
            "fix": "review",
            "program": None,
            "chat": None,
            "assigned": None,
            "memory_show": None,
            "memory_save_repo": None,
            "memory_save_project": None,
            "memory_save_global": None,
        }.get(action)

    def _build_user_message(self, task: AgentTask) -> str:
        """Sestaví user zprávu — přidá action instrukce specifické pro Byte."""
        base = super()._build_user_message(task)

        action_instructions = {
            "assigned": (
                "\n**Akce:** Byl jsem přiřazen na tento ticket. Proveď proaktivní analýzu:\n"
                "1. Přečti zadání a acceptance kritéria\n"
                "2. Podívej se na stack a paměť projektu — co víš o repozitáři?\n"
                "3. Pokud máš kontext posledních PR, projdi co se v projektu nedávno dělo\n"
                "4. Navrhni konkrétní přístup k řešení\n"
                "5. Vypiš co ti chybí nebo co je nejasné — konkrétní otázky\n"
                "\nBuď stručný. Žádné romány. Vývojář chce vědět: pochopil jsi co má být hotovo? Co potřebuješ?"
                "\nNEPROGRAMUJ — jen analyzuj a ptej se."
            ),
            "chat": (
                "\n**Akce:** Jsem v CHAT režimu (ticket není In development). "
                "Přečti zadání, zeptej se na nejasnosti, navrhni přístup. NEPROGRAMUJ."
            ),
            "program": (
                "\n**Akce:** Ticket je In development. Začni programovat. "
                "Napiš co uděláš (branch, přístup) a případné otázky než začneš."
            ),
            "review": "\n**Akce:** Proveď code review dle REVIEW skill pravidel.",
            "qa": "\n**Akce:** Proveď QA testování dle QA skill pravidel.",
            "e2e_test": "\n**Akce:** Vygeneruj Playwright E2E testy dle E2E skill pravidel.",
            "fix": (
                "\n**Akce:** Zapracuj komentáře z PR. "
                "Přečti je, shrň co opravíš a začni."
            ),
        }

        if task.action in action_instructions:
            base += action_instructions[task.action]

        return base


# Singleton
_byte: Optional[ByteAgent] = None


def get_byte() -> ByteAgent:
    global _byte
    if _byte is None:
        _byte = ByteAgent()
    return _byte
