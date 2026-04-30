"""
integrations/jira/webhook.py — Přijímá eventy z Jira Automation.
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
    secret = cfg.forge_shared_secret
    if not secret:
        return True
    if not signature:
        return False
    if hmac.compare_digest(secret, signature):
        return True
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


def _classify_event(payload: dict) -> tuple[str, dict]:
    """
    Klasifikuje Jira event.
    Podporuje formát Jira Automation (formát Jira) i klasický webhook formát.
    """
    # DEBUG — loguj celý payload pro diagnostiku
    logger.info(f"[Webhook] Payload keys: {list(payload.keys())}")
    logger.info(f"[Webhook] Payload preview: {str(payload)[:800]}")

    # Jira Automation "formát Jira" posílá data přímo v rootu
    # Zkus různé struktury kde může být issue key
    issue_key = ""
    new_status = ""
    assignee_email = ""

    # Varianta A — klasický webhook: payload.issue.key
    issue = payload.get("issue", {})
    if issue:
        issue_key = issue.get("key", "")
        fields = issue.get("fields", {})
        new_status = fields.get("status", {}).get("name", "")
        assignee_email = (fields.get("assignee") or {}).get("emailAddress", "")

    # Varianta B — Jira Automation formát: issueKey přímo v rootu
    if not issue_key:
        issue_key = payload.get("issueKey", payload.get("issue_key", ""))

    # Varianta C — přes transition objekt
    transition = payload.get("transition", {})
    if transition and not new_status:
        new_status = transition.get("to", {}).get("name", "")

    # Varianta D — changelog v rootu
    changelog = payload.get("changelog", payload.get("log", {}))
    if isinstance(changelog, dict):
        items = changelog.get("items", [])
    else:
        items = []

    # Varianta E — Jira Automation může poslat stav přímo
    if not new_status:
        new_status = (
            payload.get("status", {}).get("name", "") or
            payload.get("toStatus", "") or
            payload.get("transition_to", "")
        )

    # Varianta F — assignee přímo v rootu
    if not assignee_email:
        assignee = payload.get("assignee", {})
        if isinstance(assignee, dict):
            assignee_email = assignee.get("emailAddress", assignee.get("email", ""))

    logger.info(f"[Webhook] Parsed — issue: {issue_key} | status: {new_status} | assignee: {assignee_email}")

    byte_email = cfg.agent("byte").jira.email.lower()
    programming_statuses = cfg.byte.jira_statuses.get("programming_mode", ["In Progress", "Rozpracováno"])

    # Zkontroluj přechod do In Progress
    if issue_key and new_status in programming_statuses:
        if not assignee_email or assignee_email.lower() == byte_email:
            return "in_progress", {"issue_key": issue_key, "new_status": new_status}

    # Zkontroluj changelog items
    for item in items:
        if item.get("field") == "status":
            item_status = item.get("toString", "")
            if item_status in programming_statuses:
                if not assignee_email or assignee_email.lower() == byte_email:
                    return "in_progress", {"issue_key": issue_key, "new_status": item_status}

    # Komentář
    event = payload.get("eventType", payload.get("webhookEvent", ""))
    if "commented" in event and issue_key:
        if not assignee_email or assignee_email.lower() == byte_email:
            comment = payload.get("comment", {})
            body_text = _extract_comment_text(comment.get("body", {}))
            author_email = (comment.get("author") or {}).get("emailAddress", "")
            if author_email.lower() != byte_email:
                return "comment_on_byte_ticket", {
                    "issue_key": issue_key,
                    "comment_text": body_text,
                    "author": (comment.get("author") or {}).get("displayName", ""),
                }

    logger.info(f"[Webhook] Ignoruji — issue: {issue_key} | status: '{new_status}' | assignee: '{assignee_email}'")
    return "ignore", {}


def _extract_comment_text(body) -> str:
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
    if event_type == "in_progress":
        return "program"

    if event_type in ("assigned_to_byte", "comment_on_byte_ticket"):
        comment_lower = comment_text.lower()
        triggers = cfg.byte.triggers.get("on_comment_keywords", {})
        for action, keywords in triggers.items():
            if any(kw.lower() in comment_lower for kw in keywords):
                return action
        return "chat"

    return "chat"


async def _process_event(event_type: str, event_data: dict):
    issue_key = event_data.get("issue_key", "")
    if not issue_key:
        return

    jira = JiraClient()
    bb = BitbucketClient()

    ticket_ctx = await jira.get_ticket_context(issue_key)
    if not ticket_ctx:
        logger.error(f"[Webhook] Nepodařilo se načíst ticket {issue_key}")
        return

    repo_slug = ticket_ctx.get("repo_slug", "")
    comment_text = event_data.get("comment_text", "")
    action = _resolve_action(event_type, comment_text)

    logger.info(f"[Webhook] {issue_key} | event: {event_type} | akce: {action} | repo: {repo_slug}")

    # Pokud je akce "program" → spusť programmer
    if action == "program":
        from core.programmer import ByteProgrammer
        programmer = ByteProgrammer()
        asyncio.create_task(programmer.run(issue_key))
        return

    # Jinak — chat / review / qa přes agent
    stack = {}
    global_memory, project_memory = "", ""

    if repo_slug:
        stack, (global_memory, project_memory) = await asyncio.gather(
            bb.detect_stack(repo_slug),
            bb.read_memory(repo_slug),
        )

    byte = get_byte()
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
        extra_context=comment_text if action == "fix" else "",
    )

    response = await byte.process(task)

    if response.action == "jira_comment":
        await jira.add_comment(issue_key, response.content)

    if repo_slug:
        log_entry = (
            f"**{issue_key}** | akce: {action} | stack: {stack} | "
            f"tokeny: {response.metadata.get('input_tokens', 0) + response.metadata.get('output_tokens', 0)}"
        )
        await bb.append_log(repo_slug, log_entry)

    logger.info(f"[Webhook] {issue_key} zpracováno")


@router.post("/jira")
async def handle_jira_event(request: Request):
    raw_body = await request.body()

    signature = request.headers.get("x-forge-signature", "")
    if not _verify_forge_secret(raw_body, signature):
        raise HTTPException(status_code=401, detail="Invalid Forge signature")

    # Ochrana proti prázdnému tělu (Jira Automation komentář trigger)
    if not raw_body or not raw_body.strip():
        logger.warning("[Webhook] Prázdné tělo requestu — ignoruji")
        return {"status": "ignored", "reason": "empty body"}

    try:
        payload = await request.json()
    except Exception as e:
        logger.error(f"[Webhook] JSON parse error: {e} | body: {raw_body[:200]}")
        return {"status": "ignored", "reason": "invalid json"}

    event_type, event_data = _classify_event(payload)

    if event_type == "ignore":
        return {"status": "ignored"}

    asyncio.create_task(_process_event(event_type, event_data))

    return {"status": "accepted", "event": event_type, "issue": event_data.get("issue_key")}


async def _run_programmer(issue_key: str):
    from core.programmer import ByteProgrammer
    programmer = ByteProgrammer()
    result = await programmer.run(issue_key)
    if not result.success:
        logger.error(f"[Webhook] Programovací cyklus selhal pro {issue_key}: {result.message}")