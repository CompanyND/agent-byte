"""
core/server.py — FastAPI server, vstupní bod.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from core.config import cfg
from core.agent import get_byte
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
    # Inicializuj Byte singleton
    get_byte()
    yield
    logger.info("=== Byte Agent Server se zastavuje ===")


app = FastAPI(
    title="Byte Agent",
    description="AI developer agent pro netdirect.cz",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(jira_router)


@app.get("/health")
async def health():
    """Health check — pro Railway a monitoring."""
    return {
        "status": "ok",
        "agents": cfg.enabled_agents(),
        "jira": "ok" if cfg.agent("byte").jira.api_token else "missing token",
        "bitbucket": "ok" if cfg.agent("byte").bitbucket.oauth_client_id else "missing oauth",
        "model": cfg.agent("byte").model.model,
    }


@app.get("/admin/tokens")
async def token_report():
    """Přehled expirací API tokenů."""
    return JSONResponse(content={"report": cfg.token_expiry_report()})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "core.server:app",
        host=cfg.server.host,
        port=cfg.server.port,
        reload=False,
    )


@app.post("/debug/run/{issue_key}")
async def debug_run(issue_key: str):
    """
    Ruční spuštění Byte pro daný ticket — pro testování bez Jira eventu.
    Pouze pro vývoj, v produkci zakázat nebo chránit API klíčem.
    """
    from core.programmer import ByteProgrammer
    import asyncio
    programmer = ByteProgrammer()
    asyncio.create_task(programmer.run(issue_key))
    return {"status": "started", "issue": issue_key}


@app.post("/admin/reload-personas")
async def reload_personas():
    """
    Vynutí znovu načtení SOUL.md, PERSONA.md a skills z BB ai-personas repo.
    Volej po každé změně osobnosti Byte — bez restartu Railway service.
    """
    get_byte().reload_personas()
    return {"status": "ok", "message": "Personas budou znovu načteny při příštím volání."}
