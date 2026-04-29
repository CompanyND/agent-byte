"""
core/registry.py — Registr agentů.

Načte všechny zapnuté agenty z configu a drží je v paměti.
Ostatní moduly volají get_agent("byte") — neřeší jak se agent vytváří.
"""

import logging
from typing import Optional
from core.config import cfg
from core.agent import AgentRunner

logger = logging.getLogger(__name__)

_registry: dict[str, AgentRunner] = {}


def _load_all():
    """Inicializuje všechny zapnuté agenty."""
    for slug in cfg.enabled_agents():
        try:
            _registry[slug] = AgentRunner(slug)
            logger.info(f"Agent '{slug}' načten (model: {cfg.agent(slug).model.model})")
        except Exception as e:
            logger.error(f"Nepodařilo se načíst agenta '{slug}': {e}")


def get_agent(slug: str) -> AgentRunner:
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
