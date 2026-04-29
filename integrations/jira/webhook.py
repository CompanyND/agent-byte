"""
integrations/jira/webhook.py — Přijímá eventy z Jira Forge.

Forge funkce (v Atlassian cloudu) zavolá tento endpoint při každém
relevantním Jira eventu. Zde parsujeme, rozhodujeme a spouštíme Byte.
"""

from __future__ import annotations

import re
import hmac
import hashlib
import logging
import asyncio
from fastapi import APIRouter, Request, HTTPException

from core.config import cfg
from core.agent import get_byte, ByteTask
from integrations.jira.client import JiraClient
from integrations.bitbucket.client import BitbucketClient

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhook"])

JIRA_ID_PATTERN = re.compile(r"([A-Z]{2,10}-\d+)", re.IGNORECASE)


def _verify_forge_secret(body: bytes, signature: str) -> bool:
    """Ověří HMAC podpis z Forge shared secret."""
    secret = cfg.forge_shared_secret
    if not secret:
        return True  # dev mode — bez ověření
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature or "")


def _classify_event(payload: dict) -> tuple[str, dict]:
    """
    Klasifikuje Jira event na typ + relevantní data.
    Vrátí (event_type, data) nebo ("ignore", {}).
    """
    event = payload.get("eventType", payload.get("webhookEvent", ""))
    issue = payload.get("issue", {})
    fields = issue.get("fields", {})

    # Přiřazení ticketu na Byte
    if "updated" in event:
        changelog = payload.get("changelog", {})
        for item in changelog.get("items", []):
            if item.get("field") == "assignee":
                to_str = item.get("toString", "")
                if cfg.agent("byte").jira.email.split("@")[0].lower() in to_str.lower():
                    return "assigned_to_byte", {
                        "issue_key": issue.get("key", ""),
                        "new_status": fields.get("status", {}).get("name", ""),
                    }

            # Přechod do In Progress (ticket je na Byte)
            if item.get("field") == "status":
                new_status = item.get("toString", "")
                assignee_email = (fields.get("assignee") or {}).get("emailAddress", "")
                if (new_status in cfg.byte.jira_statuses.get("programming_mode", []) and
                        cfg.agent("byte").jira.email.lower() == assignee_email.lower()):
                    return "in_progress", {
                        "issue_key": issue.get("key", ""),
                        "new_status": new_status,
                    }

    # Komentář na ticketu přiřazeném Byte
    if "commented" in event:
        assignee_email = (fields.get("assignee") or {}).get("emailAddress", "")
        if cfg.agent("byte").jira.email.lower() == assignee_email.lower():
            comment = payload.get("comment", {})
            body_text = _extract_comment_text(comment.get("body", {}))
            author_email = (comment.get("author") or {}).get("emailAddress", "")

            # Ignoruj vlastní komentáře Byte
            if author_email.lower() == cfg.agent("byte").jira.email.lower():
                return "ignore", {}

            return "comment_on_byte_ticket", {
                "issue_key": issue.get("key", ""),
                "comment_text": body_text,
                "author": (comment.get("author") or {}).get("displayName", ""),
            }

    return "ignore", {}


def _extract_comment_text(body) -> str:
    """Extrahuje text z ADF nebo string."""
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        texts = []
        for node in body.get("content", []):
            texts.append(_extract_node_text(node))
        return " ".join(filter(None, texts))
    return ""


def _extract_node_text(node: dict) -> str:
    if not node:
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    return " ".join(_extract_node_text(c) for c in node.get("content", []))


def _resolve_action(event_type: str, comment_text: str = "") -> str:
    """Rozhodne jakou akci Byte provede."""
    if event_type == "in_progress":
        return "program"

    if event_type in ("assigned_to_byte", "comment_on_byte_ticket"):
        # Zkontroluj klíčová slova v komentáři
        comment_lower = comment_text.lower()
        triggers = cfg.byte.triggers.get("on_comment_keywords", {})

        for action, keywords in triggers.items():
            if any(kw.lower() in comment_lower for kw in keywords):
                return action

        # Výchozí — chat
        return "chat"

    return "chat"


async def _process_event(event_type: str, event_data: dict):
    """
    Zpracuje event asynchronně — sestaví kontext a spustí Byte.
    Běží na pozadí (background task).
    """
    issue_key = event_data.get("issue_key", "")
    if not issue_key:
        return

    jira = JiraClient()
    bb = BitbucketClient()
    byte = get_byte()

    # Načti kontext ticketu
    ticket_ctx = await jira.get_ticket_context(issue_key)
    if not ticket_ctx:
        logger.error(f"[Webhook] Nepodařilo se načíst ticket {issue_key}")
        return

    repo_slug = ticket_ctx.get("repo_slug", "")
    comment_text = event_data.get("comment_text", "")
    action = _resolve_action(event_type, comment_text)

    logger.info(f"[Webhook] {issue_key} | event: {event_type} | akce: {action} | repo: {repo_slug}")

    # Paralelní načítání: stack + paměti
    stack = {}
    global_memory, project_memory = "", ""

    if repo_slug:
        stack, (global_memory, project_memory) = await asyncio.gather(
            bb.detect_stack(repo_slug),
            bb.read_memory(repo_slug),
        )
    else:
        logger.warning(f"[Webhook] {issue_key} nemá nastavenou komponentu → repo_slug prázdný")

    # Extra kontext pro fix akci — načti PR komentáře
    extra_context = ""
    if action == "fix" and repo_slug:
        # TODO: načíst PR komentáře z BB — implementace v dalším kroku
        extra_context = comment_text

    # Sestav úkol pro Byte
    task = ByteTask(
        ticket_id=issue_key,
        ticket_summary=ticket_ctx.get("summary", ""),
        ticket_description=ticket_ctx.get("description", ""),
        ticket_status=ticket_ctx.get("status", ""),
        acceptance_criteria=ticket_ctx.get("acceptance_criteria", ""),
        repo_slug=repo_slug,
        previous_assignee=ticket_ctx.get("previous_assignee"),
        comments=ticket_ctx.get("comments", []),
        stack=stack,
        global_memory=global_memory,
        project_memory=project_memory,
        action=action,
        extra_context=extra_context,
    )

    # Spusť Byte
    response = await byte.process(task)

    # Odešli odpověď do Jiry
    if response.action == "jira_comment":
        await jira.add_comment(issue_key, response.content)

    # Přechod stavu pokud programuje
    if action == "program":
        # Byte začíná — oznámí branch a začíná pracovat
        # Skutečný commit/PR přijde v dalším kroku (core/programmer.py)
        pass

    # Samo-dokumentace
    if repo_slug:
        log_entry = (
            f"**{issue_key}** | akce: {action} | "
            f"stack: {stack} | "
            f"tokeny: {response.metadata.get('input_tokens', 0) + response.metadata.get('output_tokens', 0)}"
        )
        await bb.append_log(repo_slug, log_entry)

    logger.info(f"[Webhook] {issue_key} zpracováno | tokeny: {response.metadata}")


@router.post("/jira")
async def handle_jira_event(request: Request):
    """
    Hlavní endpoint pro Jira Forge eventy.
    Forge manifest.yml → trigger → tento endpoint.
    """
    raw_body = await request.body()

    # Ověř Forge podpis
    signature = request.headers.get("x-forge-signature", "")
    if not _verify_forge_secret(raw_body, signature):
        raise HTTPException(status_code=401, detail="Invalid Forge signature")

    payload = await request.json()
    event_type, event_data = _classify_event(payload)

    if event_type == "ignore":
        return {"status": "ignored"}

    # Zpracuj na pozadí — Forge má timeout 25s, odpovídáme hned
    import asyncio
    asyncio.create_task(_process_event(event_type, event_data))

    return {"status": "accepted", "event": event_type, "issue": event_data.get("issue_key")}


# ---------------------------------------------------------------------------
# Programmer integration — přidáme na konec souboru
# ---------------------------------------------------------------------------

async def _run_programmer(issue_key: str):
    """Spustí programovací cyklus na pozadí."""
    from core.programmer import ByteProgrammer
    programmer = ByteProgrammer()
    result = await programmer.run(issue_key)
    if not result.success:
        logger.error(f"[Webhook] Programovací cyklus selhal pro {issue_key}: {result.message}")
