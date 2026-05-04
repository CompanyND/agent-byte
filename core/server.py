"""
core/server.py — FastAPI server, vstupní bod.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from core.config import cfg
from core.registry import get_agent, list_agents
from integrations.jira.webhook import router as jira_router


logging.basicConfig(
    level=cfg.server.log_level.upper(),
    format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup — inicializuj agenty."""
    logger.info("=== Byte Agent Server startuje ===")
    logger.info(f"Zapnutí agenti: {cfg.enabled_agents()}")
    logger.info(cfg.token_expiry_report())
    # Inicializuj všechny zapnuté agenty přes registry
    for slug in cfg.enabled_agents():
        try:
            get_agent(slug)
        except Exception as e:
            logger.error(f"Nepodařilo se inicializovat agenta '{slug}': {e}")
    yield
    logger.info("=== Byte Agent Server se zastavuje ===")


app = FastAPI(
    title="Byte Agent",
    description="AI developer agent pro netdirect.cz",
    version="0.2.0",
    lifespan=lifespan,
)

app.include_router(jira_router)


@app.get("/health")
async def health():
    """Health check — pro Railway a monitoring."""
    byte_cfg = cfg.agent("byte")
    return {
        "status": "ok",
        "agents": cfg.enabled_agents(),
        "jira": "ok" if byte_cfg.jira and byte_cfg.jira.api_token else "missing token",
        "bitbucket": "ok" if byte_cfg.bitbucket and byte_cfg.bitbucket.oauth_client_id else "missing oauth",
        "model": byte_cfg.model.model,
    }


@app.get("/admin/tokens")
async def token_report():
    """Přehled expirací API tokenů."""
    return JSONResponse(content={"report": cfg.token_expiry_report()})


@app.post("/admin/reload-personas")
async def reload_personas():
    """
    Vynutí znovu načtení SOUL.md, PERSONA.md a skills z BB ai-personas repo.
    Volej po každé změně osobnosti — bez restartu Railway service.

    curl -X POST https://agent-byte-production.up.railway.app/admin/reload-personas
    """
    for slug in list_agents():
        try:
            get_agent(slug).reload_personas()
        except Exception as e:
            logger.warning(f"Reload personas pro '{slug}' selhal: {e}")
    return {"status": "ok", "message": "Personas budou znovu načteny při příštím volání."}


@app.post("/debug/run/{issue_key}")
async def debug_run(issue_key: str):
    """
    Ruční spuštění Byte pro daný ticket — pro testování bez Jira eventu.
    """
    from agents.byte.programmer import ByteProgrammer
    import asyncio
    programmer = ByteProgrammer()
    asyncio.create_task(programmer.run(issue_key))
    return {"status": "started", "issue": issue_key}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "core.server:app",
        host=cfg.server.host,
        port=cfg.server.port,
        reload=False,
    )
