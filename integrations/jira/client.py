"""
integrations/jira/client.py — Jira REST API klient pro Byte.
Byte vystupuje vždy pod vlastním účtem (BYTE_JIRA_API_TOKEN).
"""

from __future__ import annotations

import httpx
import logging
from typing import Optional

from core.config import cfg

logger = logging.getLogger(__name__)


class JiraClient:
    """
    Jira REST API v3.
    Všechny akce se provádí jako Byte (vlastní API token).
    """

    def __init__(self, agent_slug: str = "byte"):
        agent_cfg = cfg.agent(agent_slug).jira
        self._base = agent_cfg.base_url.rstrip("/")
        self._auth = (agent_cfg.email, agent_cfg.api_token)

    def _url(self, path: str) -> str:
        return f"{self._base}/rest/api/3/{path.lstrip('/')}"

    def _text_to_adf(self, text: str) -> dict:
        """
        Převede plain text na Atlassian Document Format (ADF).
        Jira API v3 vyžaduje ADF pro body komentářů.
        Zachová základní markdown (code bloky).
        """
        # Rozdělíme text na odstavce
        paragraphs = text.strip().split("\n\n")
        content = []
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            # Code block detekce
            if para.startswith("```") and para.endswith("```"):
                code = para.strip("`").strip()
                content.append({
                    "type": "codeBlock",
                    "content": [{"type": "text", "text": code}]
                })
            else:
                # Normální odstavec — zachováme newlines jako hard breaks
                lines = para.split("\n")
                inline = []
                for i, line in enumerate(lines):
                    inline.append({"type": "text", "text": line})
                    if i < len(lines) - 1:
                        inline.append({"type": "hardBreak"})
                content.append({"type": "paragraph", "content": inline})

        return {"type": "doc", "version": 1, "content": content}

    # -------------------------------------------------------------------------
    # Čtení ticketů
    # -------------------------------------------------------------------------

    async def get_ticket(self, issue_key: str) -> Optional[dict]:
        """Načte ticket včetně polí, komentářů a changelogu."""
        url = self._url(f"issue/{issue_key}")
        params = {
            "expand": "changelog,renderedFields",
            "fields": "summary,description,status,assignee,reporter,comment,"
                     "customfield_10014,priority,labels,components,attachment,"
                     "customfield_10016,customfield_10000"
                     # 10014 = Epic Link, 10016 = Story Points, 10000 = AK (typicky)
        }
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, auth=self._auth, timeout=15)
            if resp.is_success:
                return resp.json()
            logger.error(f"[Jira] get_ticket {issue_key} selhalo: {resp.status_code}")
            return None

    async def get_ticket_context(self, issue_key: str) -> dict:
        """
        Sestaví kompletní kontext ticketu pro Byte.
        Vrátí strukturovaný dict s vším co Byte potřebuje.
        """
        ticket = await self.get_ticket(issue_key)
        if not ticket:
            return {}

        fields = ticket.get("fields", {})
        changelog = ticket.get("changelog", {}).get("histories", [])

        # Najdi předchozího assignee z changelogu
        previous_assignee = self._find_previous_assignee(changelog)

        # Komentáře
        comments = []
        for c in fields.get("comment", {}).get("comments", []):
            comments.append({
                "author": c.get("author", {}).get("displayName", ""),
                "body": self._extract_text_from_adf(c.get("body", {})),
                "created": c.get("created", ""),
            })

        # Komponenta → BB repo slug
        components = [comp.get("name", "") for comp in fields.get("components", [])]

        return {
            "ticket_id": issue_key,
            "summary": fields.get("summary", ""),
            "description": self._extract_text_from_adf(fields.get("description") or {}),
            "status": fields.get("status", {}).get("name", ""),
            "assignee": (fields.get("assignee") or {}).get("displayName", ""),
            "assignee_account_id": (fields.get("assignee") or {}).get("accountId", ""),
            "reporter": (fields.get("reporter") or {}).get("displayName", ""),
            "reporter_account_id": (fields.get("reporter") or {}).get("accountId", ""),
            "previous_assignee": previous_assignee,
            "components": components,
            "repo_slug": components[0] if components else "",   # první komponenta = repo
            "comments": comments,
            "labels": fields.get("labels", []),
            "priority": (fields.get("priority") or {}).get("name", ""),
        }

    def _find_previous_assignee(self, changelog: list) -> Optional[dict]:
        """
        Najde posledního člověka (ne Byte) který byl assignee před Bytem.
        Prochází changelog od nejnovějšího.
        """
        byte_email = cfg.agent("byte").jira.email
        for history in reversed(changelog):
            for item in history.get("items", []):
                if item.get("field") == "assignee":
                    from_account = item.get("from")  # account_id předchozího
                    from_string = item.get("fromString", "")
                    if from_account and byte_email.lower() not in from_string.lower():
                        return {
                            "account_id": from_account,
                            "display_name": from_string,
                        }
        return None

    def _extract_text_from_adf(self, adf: dict) -> str:
        """Extrahuje plain text z ADF dokumentu."""
        if not adf or not isinstance(adf, dict):
            return ""
        texts = []
        for node in adf.get("content", []):
            texts.append(self._extract_node_text(node))
        return "\n".join(filter(None, texts))

    def _extract_node_text(self, node: dict) -> str:
        if not node:
            return ""
        if node.get("type") == "text":
            return node.get("text", "")
        texts = []
        for child in node.get("content", []):
            texts.append(self._extract_node_text(child))
        return " ".join(filter(None, texts))

    # -------------------------------------------------------------------------
    # Komentáře
    # -------------------------------------------------------------------------

    async def add_comment(self, issue_key: str, body: str) -> bool:
        """Přidá komentář k ticketu jako Byte."""
        url = self._url(f"issue/{issue_key}/comment")
        payload = {"body": self._text_to_adf(body)}
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url, json=payload, auth=self._auth, timeout=15
            )
            if resp.is_success:
                logger.info(f"[Jira] Komentář přidán do {issue_key}")
                return True
            logger.error(f"[Jira] add_comment {issue_key} selhalo: {resp.status_code} {resp.text[:200]}")
            return False

    # -------------------------------------------------------------------------
    # Přechody stavů
    # -------------------------------------------------------------------------

    async def get_transitions(self, issue_key: str) -> list[dict]:
        """Vrátí dostupné přechody stavů pro ticket."""
        url = self._url(f"issue/{issue_key}/transitions")
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, auth=self._auth, timeout=10)
            if resp.is_success:
                return resp.json().get("transitions", [])
            return []

    async def transition(self, issue_key: str, target_status: str) -> bool:
        """
        Přepne ticket do cílového stavu.
        target_status = název stavu (např. "Ready to test", "In Progress")
        """
        transitions = await self.get_transitions(issue_key)
        transition_id = None
        for t in transitions:
            if t.get("to", {}).get("name", "").lower() == target_status.lower():
                transition_id = t["id"]
                break

        if not transition_id:
            available = [t.get("to", {}).get("name") for t in transitions]
            logger.warning(
                f"[Jira] Stav '{target_status}' nenalezen pro {issue_key}. "
                f"Dostupné: {available}"
            )
            return False

        url = self._url(f"issue/{issue_key}/transitions")
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                json={"transition": {"id": transition_id}},
                auth=self._auth,
                timeout=10,
            )
            if resp.is_success:
                logger.info(f"[Jira] {issue_key} → {target_status}")
                return True
            logger.error(f"[Jira] transition selhalo: {resp.status_code}")
            return False

    async def assign(self, issue_key: str, account_id: Optional[str]) -> bool:
        """Přiřadí ticket danému uživateli (None = unassign)."""
        url = self._url(f"issue/{issue_key}/assignee")
        async with httpx.AsyncClient() as client:
            resp = await client.put(
                url,
                json={"accountId": account_id},
                auth=self._auth,
                timeout=10,
            )
            return resp.is_success
