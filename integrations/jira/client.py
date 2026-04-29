"""
integrations/jira/client.py — Jira REST API klient pro Byte.
Byte vystupuje vždy pod vlastním účtem (BYTE_JIRA_API_TOKEN).
"""

from __future__ import annotations

import re
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
        Převede Markdown text na Atlassian Document Format (ADF).
        Podporuje: **bold**, `code`, [link](url), nadpisy ##/###, code bloky ```.
        """
        content = []
        lines = text.strip().split("\n")
        i = 0

        while i < len(lines):
            line = lines[i]

            # Code blok (```)
            if line.strip().startswith("```"):
                code_lines = []
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("```"):
                    code_lines.append(lines[i])
                    i += 1
                content.append({
                    "type": "codeBlock",
                    "attrs": {},
                    "content": [{"type": "text", "text": "\n".join(code_lines)}]
                })
                i += 1
                continue

            # Nadpis ###
            if line.startswith("### "):
                content.append({
                    "type": "heading",
                    "attrs": {"level": 3},
                    "content": [{"type": "text", "text": line[4:].strip()}]
                })
                i += 1
                continue

            # Nadpis ##
            if line.startswith("## "):
                content.append({
                    "type": "heading",
                    "attrs": {"level": 2},
                    "content": [{"type": "text", "text": line[3:].strip()}]
                })
                i += 1
                continue

            # Nadpis #
            if line.startswith("# "):
                content.append({
                    "type": "heading",
                    "attrs": {"level": 1},
                    "content": [{"type": "text", "text": line[2:].strip()}]
                })
                i += 1
                continue

            # Prázdný řádek — přeskočit
            if not line.strip():
                i += 1
                continue

            # Normální odstavec s inline formátováním
            inline = self._parse_inline(line)
            if inline:
                content.append({"type": "paragraph", "content": inline})
            i += 1

        if not content:
            content.append({
                "type": "paragraph",
                "content": [{"type": "text", "text": text}]
            })

        return {"type": "doc", "version": 1, "content": content}

    def _parse_inline(self, text: str) -> list:
        """Parsuje inline Markdown: **bold**, `code`, [text](url), plain text."""
        nodes = []
        pattern = re.compile(
            r'(\*\*(.+?)\*\*)'         # **bold**
            r'|(`(.+?)`)'               # `code`
            r'|(\[(.+?)\]\((.+?)\))'   # [text](url)
        )
        last = 0
        for m in pattern.finditer(text):
            # Text před matchem
            if m.start() > last:
                nodes.append({"type": "text", "text": text[last:m.start()]})

            if m.group(1):  # **bold**
                nodes.append({
                    "type": "text",
                    "text": m.group(2),
                    "marks": [{"type": "strong"}]
                })
            elif m.group(3):  # `code`
                nodes.append({
                    "type": "text",
                    "text": m.group(4),
                    "marks": [{"type": "code"}]
                })
            elif m.group(5):  # [text](url)
                nodes.append({
                    "type": "text",
                    "text": m.group(6),
                    "marks": [{"type": "link", "attrs": {"href": m.group(7)}}]
                })
            last = m.end()

        # Zbytek textu
        if last < len(text):
            nodes.append({"type": "text", "text": text[last:]})

        return nodes if nodes else [{"type": "text", "text": text}]

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

        previous_assignee = self._find_previous_assignee(changelog)

        comments = []
        for c in fields.get("comment", {}).get("comments", []):
            comments.append({
                "author": c.get("author", {}).get("displayName", ""),
                "body": self._extract_text_from_adf(c.get("body", {})),
                "created": c.get("created", ""),
            })

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
            "repo_slug": components[0] if components else "",
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
                    from_account = item.get("from")
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

    async def add_comment_adf(
        self,
        issue_key: str,
        pr_url: str,
        pr_number: int,
        branch_name: str,
        default_branch: str,
        reviewer_name: str,
        summary: str,
    ) -> bool:
        """
        Přidá závěrečný komentář Byte s plným ADF formátováním:
        - PR jako klikatelný odkaz
        - branch jako inline kód
        - 'zapracuj komentáře' tučně a zeleně
        """
        url = self._url(f"issue/{issue_key}/comment")

        payload = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    # ✅ Hotovo
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": "✅ Hotovo."}
                        ]
                    },
                    # PR odkaz
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": "PR: ", "marks": [{"type": "strong"}]},
                            {
                                "type": "text",
                                "text": f"PR #{pr_number}",
                                "marks": [{"type": "link", "attrs": {"href": pr_url}}]
                            }
                        ]
                    },
                    # Branch
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": "Branch: ", "marks": [{"type": "strong"}]},
                            {"type": "text", "text": branch_name, "marks": [{"type": "code"}]},
                            {"type": "text", "text": " → "},
                            {"type": "text", "text": default_branch, "marks": [{"type": "code"}]},
                        ]
                    },
                    # Reviewer
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": "Reviewer: ", "marks": [{"type": "strong"}]},
                            {"type": "text", "text": reviewer_name}
                        ]
                    },
                    # Summary
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": summary}]
                    },
                    # Instrukce — zapracuj komentáře zeleně
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": "Pokud máš připomínky, napiš je do PR komentářů a sem napiš "},
                            {
                                "type": "text",
                                "text": "zapracuj komentáře",
                                "marks": [
                                    {"type": "strong"},
                                    {"type": "textColor", "attrs": {"color": "#00875A"}}
                                ]
                            },
                            {"type": "text", "text": "."}
                        ]
                    },
                ]
            }
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url, json=payload, auth=self._auth, timeout=15
            )
            if resp.is_success:
                logger.info(f"[Jira] Závěrečný komentář přidán do {issue_key}")
                return True
            logger.error(f"[Jira] add_comment_adf {issue_key} selhalo: {resp.status_code} {resp.text[:200]}")
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
