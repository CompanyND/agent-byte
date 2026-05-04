"""
core/billing.py — Sdílené počítání ceny za Claude tokeny.

Používají všichni agenti (Byte, Atlas, Lucy).
Ceny se berou z agents.config.yaml → models sekce každého agenta.

Použití:
    from core.billing import record_cost
    await record_cost(issue_key, input_tokens, output_tokens, agent_slug="byte")
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def record_cost(
    issue_key: str,
    input_tokens: int,
    output_tokens: int,
    agent_slug: str = "byte",
) -> float:
    """
    Vypočítá cenu za Claude volání a přičte ji do Jira customfield_10307.

    Cena = (input_tokens * cost_input_per_1m + output_tokens * cost_output_per_1m) / 1_000_000

    Vrátí vypočítanou cenu v USD. Při chybě loguje warning a vrátí 0.0.
    """
    if not issue_key:
        return 0.0

    try:
        from core.config import cfg
        from integrations.jira.client import JiraClient

        model_cfg = cfg.agent(agent_slug).model
        cost_input = getattr(model_cfg, "cost_input_per_1m", 3.00)
        cost_output = getattr(model_cfg, "cost_output_per_1m", 15.00)
        cost = (input_tokens * cost_input + output_tokens * cost_output) / 1_000_000

        jira = JiraClient(agent_slug)
        await jira.update_cost(issue_key, cost)

        logger.info(
            f"[Billing] {issue_key} | agent: {agent_slug} | "
            f"tokeny: {input_tokens}+{output_tokens} | cena: ${cost:.4f}"
        )
        return cost

    except Exception as e:
        logger.warning(f"[Billing] Nepodařilo se zapsat cenu pro {issue_key}: {e}")
        return 0.0
