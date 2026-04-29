"""
core/programmer.py — Byte programuje.

Když ticket přejde do In Progress:
1. Detekuje stack
2. Načte paměti
3. Vygeneruje kód přes Claude
4. Vytvoří branch + commitne
5. Vytvoří PR na předchozího assignee
6. Přepne Jira ticket na Ready to test
7. Komentář do Jiry s PR linkem
"""

from __future__ import annotations

import re
import json
import logging
import asyncio
import anthropic
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from core.config import cfg
from core.agent import get_byte, ByteTask
from integrations.jira.client import JiraClient
from integrations.bitbucket.client import BitbucketClient

logger = logging.getLogger(__name__)


@dataclass
class ProgrammingResult:
    success: bool
    branch: Optional[str] = None
    pr_url: Optional[str] = None
    pr_id: Optional[int] = None
    message: str = ""


class ByteProgrammer:
    """
    Řídí celý programovací cyklus Byte.
    Odděleno od AgentRunner — agent rozhoduje CO, programmer řeší JAK.
    """

    def __init__(self):
        self._jira = JiraClient()
        self._bb = BitbucketClient()
        self._byte = get_byte()
        self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

    # -------------------------------------------------------------------------
    # Hlavní vstupní bod
    # -------------------------------------------------------------------------

    async def run(self, issue_key: str) -> ProgrammingResult:
        """
        Kompletní programovací cyklus pro daný ticket.
        Volá se když ticket přejde do In Progress s Bytem jako assignee.
        """
        logger.info(f"[Programmer] Spouštím programovací cyklus pro {issue_key}")

        # 1. Načti kontext
        ticket_ctx = await self._jira.get_ticket_context(issue_key)
        if not ticket_ctx:
            return ProgrammingResult(False, message=f"Nepodařilo se načíst ticket {issue_key}")

        repo_slug = ticket_ctx.get("repo_slug", "")
        if not repo_slug:
            msg = (
                f"Ticket {issue_key} nemá nastavenou komponentu → nevím na jakém repozitáři pracovat.\n"
                f"Nastav komponentu v ticketu (komponenta = název BB repozitáře)."
            )
            await self._jira.add_comment(issue_key, msg)
            return ProgrammingResult(False, message="Chybí komponenta/repo_slug")

        # 2. Paralelně: stack + paměti
        stack, (global_mem, project_mem) = await asyncio.gather(
            self._bb.detect_stack(repo_slug),
            self._bb.read_memory(repo_slug),
        )

        logger.info(f"[Programmer] {issue_key} | repo: {repo_slug} | stack: {stack}")

        # 3. Sestavení branch name a zjištění výchozí branch
        branch_name = self._make_branch_name(issue_key, ticket_ctx.get("summary", ""))
        stack_str = self._format_stack(stack)
        default_branch = await self._get_default_branch(repo_slug) or "main"

        # 4. Zkontroluj jestli PR už existuje pro tento ticket
        existing_pr = await self._find_existing_pr(repo_slug, branch_name)
        if existing_pr:
            logger.info(f"[Programmer] PR #{existing_pr['id']} už existuje pro {issue_key} — pokračuji na existující branch")
            # Nepřidáváme "Začínám" komentář, jen pracujeme dál na existující branch
        else:
            # Oznámení do Jiry — Byte začíná
            await self._jira.add_comment(
                issue_key,
                f"Začínám.\n\nStack: {stack_str}\nBranch: `{branch_name}`\n\nVrátím se s PR."
            )

        # 5. Vytvoř branch (nebo použij existující)
        branch_ok = await self._bb.create_branch(repo_slug, branch_name, default_branch)
        if not branch_ok:
            await self._jira.add_comment(
                issue_key,
                f"❌ Nepodařilo se vytvořit branch `{branch_name}` z `{default_branch}`.\n"
                f"Zkontroluj prosím přístupy Byte k repozitáři `{repo_slug}`."
            )
            return ProgrammingResult(False, message="Branch creation failed")

        # 6. Vygeneruj kód
        code_result = await self._generate_code(
            ticket_ctx=ticket_ctx,
            stack=stack,
            global_memory=global_mem,
            project_memory=project_mem,
            repo_slug=repo_slug,
            branch_name=branch_name,
        )

        if not code_result:
            await self._jira.add_comment(
                issue_key,
                "❌ Generování kódu selhalo. Eskaluji na zadavatele."
            )
            return ProgrammingResult(False, message="Code generation failed")

        # 7. Commitni soubory
        commit_ok = await self._bb.commit_files(
            repo_slug=repo_slug,
            branch=branch_name,
            files=code_result["files"],
            message=f"{issue_key}: {ticket_ctx.get('summary', '')[:60]}",
        )

        if not commit_ok:
            await self._jira.add_comment(
                issue_key,
                "❌ Commit selhal. Zkontroluj prosím přístupy Byte k repozitáři."
            )
            return ProgrammingResult(False, message="Commit failed")

        # 8. Vytvoř PR
        reviewer_account_id = (ticket_ctx.get("previous_assignee") or {}).get("account_id", "")
        pr = await self._bb.create_pr(
            repo_slug=repo_slug,
            title=f"[BYTE] {issue_key} — {ticket_ctx.get('summary', '')[:60]}",
            source_branch=branch_name,
            destination_branch=default_branch,
            description=self._build_pr_description(
                ticket_ctx, stack, code_result, branch_name
            ),
            reviewer_account_id=reviewer_account_id,
        )

        if not pr:
            await self._jira.add_comment(
                issue_key,
                "❌ PR se nepodařilo vytvořit. Branch je připravena: "
                f"`{branch_name}` — vytvoř PR prosím ručně."
            )
            return ProgrammingResult(False, branch=branch_name, message="PR creation failed")

        pr_url = pr.get("links", {}).get("html", {}).get("href", "")
        pr_id = pr.get("id")

        # 9. Přepni Jira ticket na Ready to test
        await self._jira.transition(issue_key, "Ready to test")

        # 10. Závěrečný komentář do Jiry — s formátováním
        reviewer_name = (ticket_ctx.get("previous_assignee") or {}).get("display_name", "reviewer")
        pr_number = pr.get("id", "")
        await self._jira.add_comment_adf(
            issue_key,
            pr_url=pr_url,
            pr_number=pr_number,
            branch_name=branch_name,
            default_branch=default_branch,
            reviewer_name=reviewer_name,
            summary=code_result.get("summary", ""),
        )

        # 11. Samo-dokumentace
        await self._bb.append_log(
            repo_slug,
            f"**{issue_key}** — {ticket_ctx.get('summary', '')[:60]} | "
            f"PR #{pr_id} | stack: {stack_str}"
        )

        logger.info(f"[Programmer] {issue_key} dokončeno — PR #{pr_id}: {pr_url}")
        return ProgrammingResult(True, branch=branch_name, pr_url=pr_url, pr_id=pr_id)

    # -------------------------------------------------------------------------
    # Opravný cyklus
    # -------------------------------------------------------------------------

    async def fix(self, issue_key: str, pr_id: int, repo_slug: str) -> bool:
        """
        Zapracuje PR komentáře.
        Volá se když zadavatel napíše "zapracuj komentáře" do Jiry.
        """
        logger.info(f"[Programmer] Opravný cyklus {issue_key} PR #{pr_id}")

        # Načti PR komentáře z BB
        pr_comments = await self._bb.get_pr_comments(repo_slug, pr_id)
        if not pr_comments:
            await self._jira.add_comment(
                issue_key,
                "Nenašel jsem žádné komentáře v PR. Jsou komentáře přidány přímo v PR?"
            )
            return False

        # Filtruj jen komentáře od lidí (ne od Byte)
        byte_email = cfg.agent("byte").jira.email
        human_comments = [
            c for c in pr_comments
            if (c.get("author") or {}).get("type") != "bot"
            and byte_email.split("@")[0] not in
               (c.get("author") or {}).get("nickname", "").lower()
        ]

        if not human_comments:
            await self._jira.add_comment(
                issue_key,
                "Všechny komentáře v PR jsou ode mě. Nenašel jsem připomínky k zapracování."
            )
            return False

        # Sestav kontext PR komentářů
        comments_text = "\n\n".join([
            f"**{(c.get('author') or {}).get('display_name', 'reviewer')}** "
            f"({(c.get('inline') or {}).get('path', 'obecný komentář')}"
            f"{':' + str((c.get('inline') or {}).get('to', '')) if c.get('inline') else ''}):\n"
            f"{(c.get('content') or {}).get('raw', '')}"
            for c in human_comments
        ])

        # Načti aktuální kontext
        ticket_ctx = await self._jira.get_ticket_context(issue_key)
        stack, (global_mem, project_mem) = await asyncio.gather(
            self._bb.detect_stack(repo_slug),
            self._bb.read_memory(repo_slug),
        )

        # Vygeneruj opravenou verzi
        fix_result = await self._generate_fix(
            ticket_ctx=ticket_ctx or {},
            pr_comments=comments_text,
            stack=stack,
            global_memory=global_mem,
            project_memory=project_mem,
        )

        if not fix_result:
            await self._jira.add_comment(issue_key, "❌ Generování oprav selhalo.")
            return False

        # Zjisti branch z PR
        branch_name = f"byte/{issue_key.lower()}"  # fallback — ideálně načíst z PR

        # Commitni opravy
        commit_ok = await self._bb.commit_files(
            repo_slug=repo_slug,
            branch=branch_name,
            files=fix_result["files"],
            message=f"{issue_key}: zapracování PR komentářů",
        )

        if commit_ok:
            await self._jira.add_comment(
                issue_key,
                f"✅ Komentáře zapracovány.\n\n"
                f"{fix_result.get('summary', '')}\n\n"
                f"PR je aktualizované, zkontroluj prosím."
            )
        return commit_ok

    # -------------------------------------------------------------------------
    # Generování kódu přes Claude
    # -------------------------------------------------------------------------

    async def _generate_code(
        self,
        ticket_ctx: dict,
        stack: dict,
        global_memory: str,
        project_memory: str,
        repo_slug: str,
        branch_name: str,
    ) -> Optional[dict]:
        """
        Vygeneruje kód pro ticket.
        Vrátí {"files": {path: content}, "summary": str} nebo None.
        """
        model_cfg = cfg.agent("byte").model

        # Sestavíme ByteTask a použijeme agent pro system prompt
        task = ByteTask(
            ticket_id=ticket_ctx.get("ticket_id", ""),
            ticket_summary=ticket_ctx.get("summary", ""),
            ticket_description=ticket_ctx.get("description", ""),
            ticket_status="In Progress",
            acceptance_criteria=ticket_ctx.get("acceptance_criteria", ""),
            repo_slug=repo_slug,
            previous_assignee=ticket_ctx.get("previous_assignee"),
            comments=ticket_ctx.get("comments", []),
            stack=stack,
            global_memory=global_memory,
            project_memory=project_memory,
            action="program",
        )

        system_prompt = self._byte._build_system_prompt(task)
        user_message = self._byte._build_user_message(task)

        # Přidáme instrukci pro strukturovaný výstup
        user_message += """

Vygeneruj implementaci. Odpověz POUZE validním JSON v tomto formátu:
{
  "files": {
    "cesta/k/souboru.ts": "obsah souboru",
    "cesta/k/souboru.spec.ts": "obsah testu"
  },
  "summary": "Co jsem udělal — 2-3 věty pro PR popis",
  "skipped": "Co jsem záměrně vynechal a proč (nebo prázdný string)"
}

Pravidla:
- Piš kód odpovídající existující architektuře projektu
- Respektuj verzi stacku (Angular verzi, .NET verzi atd.)
- Nevynechávej imports
- Nepiš placeholder komentáře jako "// TODO implement" — implementuj
- Pokud něco opravdu nevíš, zahrň otázku do "skipped"
"""

        response = self._client.messages.create(
            model=model_cfg.model,
            max_tokens=model_cfg.max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )

        raw = response.content[0].text
        logger.info(
            f"[Programmer] Kód vygenerován | "
            f"tokeny: {response.usage.input_tokens}+{response.usage.output_tokens}"
        )

        return self._parse_code_response(raw)

    async def _generate_fix(
        self,
        ticket_ctx: dict,
        pr_comments: str,
        stack: dict,
        global_memory: str,
        project_memory: str,
    ) -> Optional[dict]:
        """Vygeneruje opravenou verzi kódu na základě PR komentářů."""
        model_cfg = cfg.agent("byte").model

        task = ByteTask(
            ticket_id=ticket_ctx.get("ticket_id", ""),
            ticket_summary=ticket_ctx.get("summary", ""),
            ticket_description=ticket_ctx.get("description", ""),
            ticket_status="In Progress",
            acceptance_criteria=ticket_ctx.get("acceptance_criteria", ""),
            repo_slug=ticket_ctx.get("repo_slug", ""),
            previous_assignee=ticket_ctx.get("previous_assignee"),
            comments=ticket_ctx.get("comments", []),
            stack=stack,
            global_memory=global_memory,
            project_memory=project_memory,
            action="fix",
            extra_context=f"PR komentáře k zapracování:\n\n{pr_comments}",
        )

        system_prompt = self._byte._build_system_prompt(task)
        user_message = self._byte._build_user_message(task)
        user_message += """

Zapracuj výše uvedené PR komentáře. Odpověz POUZE validním JSON:
{
  "files": {
    "cesta/k/souboru.ts": "opravený obsah souboru"
  },
  "summary": "Co jsem opravil — 2-3 věty",
  "skipped": "Co jsem nezapracoval a proč"
}
"""

        response = self._client.messages.create(
            model=model_cfg.model,
            max_tokens=model_cfg.max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )

        return self._parse_code_response(response.content[0].text)

    def _parse_code_response(self, raw: str) -> Optional[dict]:
        """Parsuje JSON odpověď od Claudea."""
        try:
            clean = raw.strip()
            # Odstraň markdown code bloky pokud jsou
            clean = re.sub(r"^```json\s*", "", clean)
            clean = re.sub(r"^```\s*", "", clean)
            clean = re.sub(r"\s*```$", "", clean)
            return json.loads(clean.strip())
        except json.JSONDecodeError as e:
            logger.error(f"[Programmer] JSON parse error: {e}\nRaw: {raw[:300]}")
            return None

    # -------------------------------------------------------------------------
    # Pomocné metody
    # -------------------------------------------------------------------------

    def _make_branch_name(self, issue_key: str, summary: str) -> str:
        """Vytvoří název branché: byte/ND-423-pridej-readme-md (bez diakritiky)"""
        import unicodedata
        # Odstraň diakritiku: č→c, ř→r, š→s, ž→z atd.
        normalized = unicodedata.normalize("NFKD", summary)
        ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
        pattern = cfg.byte.branch_pattern
        slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower())[:40].strip("-")
        return pattern.replace("{ticket-id}", issue_key.upper()).replace("{slug}", slug)

    def _format_stack(self, stack: dict) -> str:
        parts = []
        if stack.get("angular"):
            parts.append(f"Angular {stack['angular']}")
        if stack.get("dotnet"):
            parts.append(f".NET {stack['dotnet']}")
        if stack.get("php"):
            parts.append(f"PHP {stack['php']}")
        return " | ".join(parts) if parts else "neznámý"

    async def _find_existing_pr(self, repo_slug: str, branch_name: str) -> Optional[dict]:
        """Najde existující PR pro danou branch."""
        token = await self._bb._get_token()
        url = f"https://api.bitbucket.org/2.0/repositories/{self._bb._workspace}/{repo_slug}/pullrequests"
        params = {"state": "OPEN", "q": f'source.branch.name="{branch_name}"'}
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            if resp.is_success:
                prs = resp.json().get("values", [])
                return prs[0] if prs else None
        return None

    async def _get_default_branch(self, repo_slug: str) -> Optional[str]:
        """Zjistí výchozí branch repozitáře."""
        token = await self._bb._get_token()
        url = f"https://api.bitbucket.org/2.0/repositories/{self._bb._workspace}/{repo_slug}"
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            if resp.is_success:
                return resp.json().get("mainbranch", {}).get("name", "main")
        return "main"

    def _build_pr_description(
        self,
        ticket_ctx: dict,
        stack: dict,
        code_result: dict,
        branch_name: str,
    ) -> str:
        issue_key = ticket_ctx.get("ticket_id", "")
        jira_url = f"{cfg.jira_base_url}/browse/{issue_key}"
        stack_str = self._format_stack(stack)

        desc = (
            f"## [{issue_key}]({jira_url}) — {ticket_ctx.get('summary', '')}\n\n"
            f"**Stack:** {stack_str}\n"
            f"**Branch:** `{branch_name}`\n\n"
            f"---\n\n"
            f"### Co jsem udělal\n{code_result.get('summary', '')}\n\n"
        )

        if code_result.get("skipped"):
            desc += f"### Co jsem vynechal\n{code_result['skipped']}\n\n"

        desc += (
            f"---\n"
            f"*Vygenerováno Byte AI agentem. "
            f"Připomínky pište do komentářů PR nebo do Jira ticketu.*"
        )
        return desc
