"""
core/config.py — Centrální konfigurace.
Struktura z agents.config.yaml + tajné hodnoty z ENV.
"""

from __future__ import annotations
import os
import yaml
import logging
from pathlib import Path
from datetime import date, datetime
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)
CONFIG_PATH = Path("config/agents.config.yaml")


@dataclass
class ModelConfig:
    provider: str
    model: str
    max_tokens: int
    temperature: float


@dataclass
class JiraConfig:
    base_url: str
    email: str
    api_token: str          # z ENV
    token_expires: Optional[str] = None
    allowed_projects: list = field(default_factory=list)


@dataclass
class BitbucketConfig:
    workspace: str
    email: str
    auth_method: str              # "oauth2"
    oauth_client_id: str          # z ENV
    oauth_client_secret: str      # z ENV


@dataclass
class AgentConfig:
    name: str
    slug: str
    enabled: bool
    model: ModelConfig
    jira: Optional[JiraConfig] = None
    bitbucket: Optional[BitbucketConfig] = None


@dataclass
class ByteConfig:
    """Byte-specifická konfigurace z byte_config sekce."""
    jira_statuses: dict
    triggers: dict
    skip_branches: dict
    branch_pattern: str
    ignored_files: list
    limits: dict
    memory: dict
    self_documentation: dict


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"


class Config:
    """
    Singleton — načte se jednou při startu.
    Struktura z YAML, tajemství z ENV.
    """

    def __init__(self, config_path: Path = CONFIG_PATH):
        if not config_path.exists():
            raise FileNotFoundError(
                f"Config nenalezen: {config_path}\n"
                f"Zkopíruj config/agents.config.example.yaml jako config/agents.config.yaml"
            )
        with open(config_path) as f:
            raw = yaml.safe_load(f)

        self._agents: dict[str, AgentConfig] = {}
        self._parse(raw)
        self._validate_enabled_agents()
        self._warn_expiring_tokens()

    def _env(self, key: str, required: bool = False) -> str:
        value = os.environ.get(key, "")
        if required and not value:
            raise EnvironmentError(
                f"Chybí povinná ENV proměnná: {key}\n"
                f"Přidej do .env nebo Railway Environment Variables."
            )
        return value

    def _parse(self, raw: dict):
        # Anthropic
        self.anthropic_api_key: str = self._env("ANTHROPIC_API_KEY", required=True)

        # Atlassian base
        atl = raw.get("atlassian", {})
        self.jira_base_url: str = atl.get("jira_base_url", "")
        self.bitbucket_workspace: str = atl.get("bitbucket_workspace", "")

        # Forge secret pro ověření requestů
        self.forge_shared_secret: str = self._env("FORGE_SHARED_SECRET")

        # Byte Railway URL (Forge ho volá)
        self.byte_railway_url: str = self._env("BYTE_RAILWAY_URL")

        # Server
        srv = raw.get("server", {})
        self.server = ServerConfig(
            host=srv.get("host", "0.0.0.0"),
            port=srv.get("port", 8000),
            log_level=srv.get("log_level", "info"),
        )

        # Byte specifická konfigurace
        bc = raw.get("byte_config", {})
        self.byte = ByteConfig(
            jira_statuses=bc.get("jira_statuses", {}),
            triggers=bc.get("triggers", {}),
            skip_branches=bc.get("skip_branches", {}),
            branch_pattern=bc.get("branch_pattern", "byte/{ticket-id}-{slug}"),
            ignored_files=bc.get("ignored_files", []),
            limits=bc.get("limits", {}),
            memory=bc.get("memory", {}),
            self_documentation=bc.get("self_documentation", {}),
        )

        # Agenti
        for slug, agent_raw in raw.get("agents", {}).items():
            self._agents[slug] = self._parse_agent(slug, agent_raw)

    def _parse_agent(self, slug: str, raw: dict) -> AgentConfig:
        slug_upper = slug.upper()

        model_raw = raw.get("model", {})
        model = ModelConfig(
            provider=model_raw.get("provider", "anthropic"),
            model=model_raw.get("model", "claude-sonnet-4-6"),
            max_tokens=model_raw.get("max_tokens", 4096),
            temperature=model_raw.get("temperature", 0.2),
        )

        jira = None
        if raw.get("jira"):
            j = raw["jira"]
            jira = JiraConfig(
                base_url=self.jira_base_url,
                email=j.get("email", ""),
                api_token=self._env(f"{slug_upper}_JIRA_API_TOKEN"),
                token_expires=j.get("token_expires") or None,
                allowed_projects=j.get("allowed_projects") or [],
            )

        bitbucket = None
        if raw.get("bitbucket"):
            b = raw["bitbucket"]
            bitbucket = BitbucketConfig(
                workspace=self.bitbucket_workspace,
                email=b.get("email", ""),
                auth_method=b.get("auth_method", "oauth2"),
                oauth_client_id=self._env(f"{slug_upper}_BB_OAUTH_CLIENT_ID"),
                oauth_client_secret=self._env(f"{slug_upper}_BB_OAUTH_CLIENT_SECRET"),
            )

        return AgentConfig(
            name=slug.capitalize(),
            slug=slug,
            enabled=raw.get("enabled", False),
            model=model,
            jira=jira,
            bitbucket=bitbucket,
        )

    def _validate_enabled_agents(self):
        errors = []
        for slug, agent in self._agents.items():
            if not agent.enabled:
                continue
            slug_upper = slug.upper()
            if agent.jira and not agent.jira.api_token:
                errors.append(f"  {slug_upper}_JIRA_API_TOKEN")
            if agent.bitbucket:
                if not agent.bitbucket.oauth_client_id:
                    errors.append(f"  {slug_upper}_BB_OAUTH_CLIENT_ID")
                if not agent.bitbucket.oauth_client_secret:
                    errors.append(f"  {slug_upper}_BB_OAUTH_CLIENT_SECRET")
        if errors:
            raise EnvironmentError(
                "Chybí ENV proměnné pro zapnuté agenty:\n" +
                "\n".join(errors)
            )

    def _warn_expiring_tokens(self):
        today = date.today()
        for slug, agent in self._agents.items():
            if not agent.jira or not agent.jira.token_expires:
                continue
            try:
                expires = datetime.strptime(agent.jira.token_expires, "%Y-%m-%d").date()
                days = (expires - today).days
                if days < 0:
                    logger.error(f"🔴 EXPIRED: Jira token '{slug}' expiroval {agent.jira.token_expires}!")
                elif days <= 30:
                    logger.warning(f"🟡 Jira token '{slug}' expiruje za {days} dní ({agent.jira.token_expires})")
                elif days <= 90:
                    logger.info(f"ℹ️  Jira token '{slug}' expiruje za {days} dní")
            except ValueError:
                logger.warning(f"Neplatný formát token_expires pro '{slug}'")

    def agent(self, slug: str) -> AgentConfig:
        if slug not in self._agents:
            raise ValueError(f"Agent '{slug}' nenalezen. Dostupní: {list(self._agents)}")
        return self._agents[slug]

    def enabled_agents(self) -> list[str]:
        return [s for s, a in self._agents.items() if a.enabled]

    def token_expiry_report(self) -> str:
        today = date.today()
        lines = ["Token expiry report:"]
        for slug, agent in self._agents.items():
            if not agent.jira or not agent.jira.token_expires:
                lines.append(f"  {slug}: token_expires nenastaveno")
                continue
            try:
                expires = datetime.strptime(agent.jira.token_expires, "%Y-%m-%d").date()
                days = (expires - today).days
                icon = "🔴" if days < 0 else "🟡" if days <= 30 else "✅"
                lines.append(f"  {icon} {slug}: expiruje {agent.jira.token_expires} ({days} dní)")
            except ValueError:
                lines.append(f"  ❓ {slug}: neplatný formát")
        return "\n".join(lines)


# Singleton
_cfg: Optional[Config] = None

def get_config() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = Config()
    return _cfg

cfg = get_config()
