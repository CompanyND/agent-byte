"""
core/registry.py — Registr agentů.

Načte všechny zapnuté agenty z configu a drží je v paměti.
Ostatní moduly volají get_agent("byte") — neřeší jak se agent vytváří.

Přidání nového agenta:
1. Vytvoř agents/{slug}/agent.py s třídou dědící AgentBase
2. Zaregistruj ho v _create_agent() níže
3. Přidej do agents.config.yaml
"""

import logging
from typing import Optional
from core.config import cfg
from core.agent_base import AgentBase

logger = logging.getLogger(__name__)

_registry: dict[str, AgentBase] = {}


def _create_agent(slug: str) -> AgentBase:
    """
    Factory — vytvoří správnou instanci agenta podle slugu.
    Přidej sem každého nového agenta.
    """
    if slug == "byte":
        from agents.byte.agent import ByteAgent
        return ByteAgent()

    # Budoucí agenti:
    # if slug == "atlas":
    #     from agents.atlas.agent import AtlasAgent
    #     return AtlasAgent()
    #
    # if slug == "lucy":
    #     from agents.lucy.agent import LucyAgent
    #     return LucyAgent()

    raise ValueError(f"Agent '{slug}' není implementován. Přidej ho do core/registry.py")


def _load_all():
    """Inicializuje všechny zapnuté agenty."""
    for slug in cfg.enabled_agents():
        try:
            _registry[slug] = _create_agent(slug)
            logger.info(f"Agent '{slug}' načten (model: {cfg.agent(slug).model.model})")
        except Exception as e:
            logger.error(f"Nepodařilo se načíst agenta '{slug}': {e}")


def get_agent(slug: str) -> AgentBase:
    """Vrátí instanci agenta. Lazy-load při prvním volání."""
    if not _registry:
        _load_all()
    if slug not in _registry:
        raise ValueError(f"Agent '{slug}' není zapnutý nebo neexistuje.")
    return _registry[slug]


def list_agents() -> list[str]:
    """Vrátí seznam načtených agentů."""
    if not _registry:
        _load_all()
    return list(_registry.keys())
