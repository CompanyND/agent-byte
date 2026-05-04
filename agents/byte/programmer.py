"""
agents/byte/programmer.py — Byte programuje.

Když ticket přejde do In development:
1. Detekuje stack
2. Načte paměti (3 úrovně)
3. Vygeneruje kód přes Claude
4. Vytvoří branch Z release větve
5. Commitne soubory
6. Vytvoří PR DO master větve
7. Přepne Jira ticket na Ready to test + přiřadí zpět
8. Stručný komentář do Jiry
9. Zapíše repozitářovou paměť (na pozadí)
10. Zapíše billing (přes core.billing)
"""

from __future__ import annotations

import re
import json
import logging
import asyncio
import anthropic
from dataclasses import dataclass
from typing import Optional

from core.config import cfg
from core.billing import record_cost
from core.agent_base import AgentTask
from agents.byte.agent import get_byte
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

        # Sledování tokenů přes celý cyklus — kumulativní
        self._total_input_tokens = 0
        self._total_output_tokens = 0

    def _track_usage(self, response) -> None:
        """Přičte tokeny z Claude response do kumulativního počítadla."""
        self._total_input_tokens += response.usage.input_tokens
        self._total_output_tokens += response.usage.output_tokens

    # -------------------------------------------------------------------------
    # Hlavní vstupní bod
    # -------------------------------------------------------------------------

    async def run(self, issue_key: str) -> ProgrammingResult:
        """
        Kompletní programovací cyklus pro daný ticket.
        Volá se když ticket přejde do In development s Bytem jako assignee.
        """
        logger.info(f"[Programmer] Spouštím programovací cyklus pro {issue_key}")

        # Reset tokenů pro tento cyklus
        self._total_input_tokens = 0
        self._total_output_tokens = 0

        # 1. Načti kontext
        ticket_ctx = await self._jira.get_ticket_context(issue_key)
        if not ticket_ctx:
            return ProgrammingResult(False, message=f"Nepodařilo se načíst ticket {issue_key}")

        repo_slug = ticket_ctx.get("repo_slug", "")
        if not repo_slug:
            await self._report_error(
                issue_key,
                "Ticket nemá nastavenou **Komponentu** → nevím na jakém repozitáři pracovat.",
                "Nastav komponentu v ticketu. Komponenta = přesný název BB repozitáře."
            )
            return ProgrammingResult(False, message="Chybí komponenta/repo_slug")

        # 2. Paralelně: stack + paměti
        stack, memories = await asyncio.gather(
            self._bb.detect_stack(repo_slug),
            self._bb.read_memory(repo_slug),
        )
        global_mem = memories[0] if memories else ""
        project_mem = memories[1] if len(memories) > 1 else ""
        repo_mem = memories[2] if len(memories) > 2 else ""
        combined_memory = "\n\n".join(filter(None, [global_mem, project_mem, repo_mem]))

        logger.info(f"[Programmer] {issue_key} | repo: {repo_slug} | stack: {stack}")

        # 3. Branch name
        issue_type = ticket_ctx.get("issue_type", "")
        branch_name = self._make_branch_name(issue_key, issue_type)
        stack_str = self._format_stack(stack)

        # 4. Zjisti release větev (Z které se vytváří branch)
        release_branch = await self._get_release_branch(repo_slug, issue_key)
        if not release_branch:
            await self._report_error(
                issue_key,
                f"Nepodařilo se určit release větev pro repozitář `{repo_slug}`.",
                "Buď větev 'release' neexistuje, nebo vypršel timeout čekání na odpověď."
            )
            return ProgrammingResult(False, message="Chybí release větev")

        # 5. Hlavní branch (DO které míří PR)
        main_branch = await self._get_default_branch(repo_slug) or "master"

        # 6. Zkontroluj existující PR
        existing_pr = await self._find_existing_pr(repo_slug, branch_name)
        if existing_pr:
            logger.info(f"[Programmer] PR #{existing_pr['id']} existuje pro {issue_key}")

        # 7. Vytvoř branch Z release
        branch_ok = await self._bb.create_branch(repo_slug, branch_name, release_branch)
        if not branch_ok:
            await self._report_error(
                issue_key,
                f"Nepodařilo se vytvořit branch `{branch_name}` z `{release_branch}`.",
                f"Zkontroluj přístupy Byte k repozitáři `{repo_slug}`."
            )
            return ProgrammingResult(False, message="Branch creation failed")

        # 8. Vygeneruj kód
        code_result = await self._generate_code(
            ticket_ctx=ticket_ctx,
            stack=stack,
            global_memory=combined_memory,
            project_memory="",
            repo_slug=repo_slug,
            branch_name=branch_name,
        )

        if not code_result:
            await self._jira.add_comment(issue_key, "❌ Generování kódu selhalo. Eskaluji na zadavatele.")
            return ProgrammingResult(False, message="Code generation failed")

        # 9. Commitni soubory
        commit_ok = await self._bb.commit_files(
            repo_slug=repo_slug,
            branch=branch_name,
            files=code_result["files"],
            message=f"{issue_key}: {ticket_ctx.get('summary', '')[:60]}",
        )

        if not commit_ok:
            await self._report_error(
                issue_key,
                "Commit selhal.",
                f"Zkontroluj přístupy Byte k repozitáři `{repo_slug}`."
            )
            return ProgrammingResult(False, message="Commit failed")

        # 10. Vytvoř PR — DO main_branch
        reviewer_account_id = (ticket_ctx.get("previous_assignee") or {}).get("account_id", "")
        pr = await self._bb.create_pr(
            repo_slug=repo_slug,
            title=f"[BYTE] {issue_key} — {ticket_ctx.get('summary', '')[:60]}",
            source_branch=branch_name,
            destination_branch=main_branch,
            description=self._build_pr_description(ticket_ctx, stack, code_result, branch_name, main_branch),
            reviewer_account_id=reviewer_account_id,
        )

        if not pr:
            await self._jira.add_comment(
                issue_key,
                f"❌ PR se nepodařilo vytvořit. Branch je připravena: `{branch_name}` — vytvoř PR prosím ručně."
            )
            return ProgrammingResult(False, branch=branch_name, message="PR creation failed")

        pr_url = pr.get("links", {}).get("html", {}).get("href", "")
        pr_id = pr.get("id")

        # 11. Přepni Jira + přiřaď zpět
        await self._jira.transition(issue_key, "Ready to test")
        previous_account_id = (ticket_ctx.get("previous_assignee") or {}).get("account_id")
        previous_name = (ticket_ctx.get("previous_assignee") or {}).get("display_name", "reviewer")
        if previous_account_id:
            await self._jira.assign(issue_key, previous_account_id)

        # 12. Stručný komentář do Jiry
        summary = code_result.get("summary", "")
        skipped = code_result.get("skipped", "")
        comment_lines = [
            "✅ Hotovo",
            "",
            f"[PR #{pr_id}]({pr_url}) → `{main_branch}`",
            f"Reviewer: {previous_name}",
        ]
        if summary:
            comment_lines += ["", summary]
        if skipped:
            comment_lines += ["", f"Vynecháno: {skipped}"]
        comment_lines += ["", "Připomínky? Napiš do PR nebo sem napiš **zapracuj komentáře**."]
        await self._jira.add_comment(issue_key, "\n".join(comment_lines))

        # 13. Repozitářová paměť (na pozadí)
        asyncio.create_task(self._update_repo_memory(
            repo_slug=repo_slug,
            issue_key=issue_key,
            stack=stack,
            code_result=code_result,
            ticket_ctx=ticket_ctx,
        ))

        # 14. Samo-dokumentace
        await self._bb.append_log(
            repo_slug,
            f"**{issue_key}** — {ticket_ctx.get('summary', '')[:60]} | PR #{pr_id} | stack: {stack_str}"
        )

        # 15. Billing — všechny tokeny z celého cyklu
        await record_cost(issue_key, self._total_input_tokens, self._total_output_tokens, "byte")

        logger.info(f"[Programmer] {issue_key} dokončeno — PR #{pr_id}: {pr_url}")
        return ProgrammingResult(True, branch=branch_name, pr_url=pr_url, pr_id=pr_id)

    # -------------------------------------------------------------------------
    # Repozitářová paměť
    # -------------------------------------------------------------------------

    async def _update_repo_memory(
        self,
        repo_slug: str,
        issue_key: str,
        stack: dict,
        code_result: Optional[dict],
        ticket_ctx: dict,
    ):
        """
        Byte analyzuje co zjistil a zapíše poznatky do repozitářové paměti.
        Běží na pozadí — neblokuje hlavní cyklus.
        """
        if not repo_slug or not code_result:
            return

        try:
            memory_cfg = cfg.byte.memory
            memory_repo = memory_cfg.get("global_repo", "byte-memory")
            repo_path = memory_cfg.get("repo_path", "repos/{repo-slug}/pamet.md").replace(
                "{repo-slug}", repo_slug
            )
            existing_memory = await self._bb.get_file(memory_repo, repo_path) or ""

            stack_str = self._format_stack(stack)
            files_changed = list((code_result.get("files") or {}).keys())
            summary = code_result.get("summary", "")

            from datetime import datetime
            today = datetime.now().strftime("%Y-%m-%d")

            prompt = (
                f"Právě jsi dokončil práci na repozitáři `{repo_slug}`.\n\n"
                f"Ticket: {issue_key} — {ticket_ctx.get('summary', '')}\n"
                f"Stack: {stack_str or 'neznámý'}\n"
                f"Soubory: {', '.join(files_changed[:10]) or 'neznámé'}\n"
                f"Co jsi udělal: {summary}\n\n"
                f"Aktuální paměť:\n{existing_memory or '(prázdná)'}\n\n"
                f"Napiš STRUČNÉ nové poznatky o architektuře (max 5 odrážek). "
                f"Pouze info které v paměti ještě není. Pokud nemáš nic nového, odpověz prázdným řetězcem.\n\n"
                f"Formát:\n## {today} — {issue_key}\n- [poznatek]"
            )

            response = self._client.messages.create(
                model=cfg.agent("byte").model.model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            # Poznatky o paměti se nepočítají do billingu ticketu
            observations = response.content[0].text.strip()

            if not observations or len(observations) < 20:
                return

            updated = (existing_memory.rstrip() + "\n\n" + observations).strip()
            await self._bb.commit_files(
                repo_slug=memory_repo,
                branch="main",
                files={repo_path: updated},
                message=f"memory: {repo_slug} — poznatky z {issue_key}",
            )
            logger.info(f"[Programmer] Repozitářová paměť {repo_slug} aktualizována")

        except Exception as e:
            logger.warning(f"[Programmer] Paměť: {e}")

    # -------------------------------------------------------------------------
    # Opravný cyklus
    # -------------------------------------------------------------------------

    async def fix(self, issue_key: str, pr_id: int, repo_slug: str) -> bool:
        """Zapracuje PR komentáře."""
        logger.info(f"[Programmer] Opravný cyklus {issue_key} PR #{pr_id}")

        pr_comments = await self._bb.get_pr_comments(repo_slug, pr_id)
        if not pr_comments:
            await self._jira.add_comment(issue_key, "Nenašel jsem komentáře v PR.")
            return False

        byte_email = cfg.agent("byte").jira.email
        human_comments = [
            c for c in pr_comments
            if byte_email.split("@")[0] not in (c.get("author") or {}).get("nickname", "").lower()
        ]

        if not human_comments:
            await self._jira.add_comment(issue_key, "Všechny PR komentáře jsou ode mě. Žádné připomínky.")
            return False

        comments_text = "\n\n".join([
            f"**{(c.get('author') or {}).get('display_name', 'reviewer')}** "
            f"({(c.get('inline') or {}).get('path', 'obecný')}"
            f"{':' + str((c.get('inline') or {}).get('to', '')) if c.get('inline') else ''}):\n"
            f"{(c.get('content') or {}).get('raw', '')}"
            for c in human_comments
        ])

        ticket_ctx = await self._jira.get_ticket_context(issue_key)
        stack, memories = await asyncio.gather(
            self._bb.detect_stack(repo_slug),
            self._bb.read_memory(repo_slug),
        )
        global_mem = memories[0] if memories else ""
        project_mem = memories[1] if len(memories) > 1 else ""

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

        issue_type = (ticket_ctx or {}).get("issue_type", "")
        branch_name = self._make_branch_name(issue_key, issue_type)

        commit_ok = await self._bb.commit_files(
            repo_slug=repo_slug,
            branch=branch_name,
            files=fix_result["files"],
            message=f"{issue_key}: zapracování PR komentářů",
        )

        if commit_ok:
            await self._jira.add_comment(
                issue_key,
                f"✅ Komentáře zapracovány.\n\n{fix_result.get('summary', '')}\n\nPR je aktualizované."
            )
            await record_cost(issue_key, self._total_input_tokens, self._total_output_tokens, "byte")

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
        task = AgentTask(
            ticket_id=ticket_ctx.get("ticket_id", ""),
            ticket_summary=ticket_ctx.get("summary", ""),
            ticket_description=ticket_ctx.get("description", ""),
            ticket_status="In development",
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
        user_message += """

Vygeneruj implementaci. Odpověz POUZE validním JSON:
{
  "files": {
    "cesta/k/souboru.ts": "obsah souboru"
  },
  "summary": "Co jsem udělal — 2-3 věty",
  "skipped": "Co jsem vynechal a proč (nebo prázdný string)"
}

Pravidla:
- Kód odpovídající existující architektuře projektu
- Respektuj verzi stacku
- Nevynechávej imports
- Nepiš placeholder komentáře — implementuj
"""

        response = self._client.messages.create(
            model=cfg.agent("byte").model.model,
            max_tokens=cfg.agent("byte").model.max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        self._track_usage(response)
        logger.info(f"[Programmer] Kód vygenerován | tokeny: {response.usage.input_tokens}+{response.usage.output_tokens}")

        return self._parse_json_response(response.content[0].text)

    async def _generate_fix(
        self,
        ticket_ctx: dict,
        pr_comments: str,
        stack: dict,
        global_memory: str,
        project_memory: str,
    ) -> Optional[dict]:
        task = AgentTask(
            ticket_id=ticket_ctx.get("ticket_id", ""),
            ticket_summary=ticket_ctx.get("summary", ""),
            ticket_description=ticket_ctx.get("description", ""),
            ticket_status="In development",
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

Zapracuj PR komentáře. Odpověz POUZE validním JSON:
{
  "files": {"cesta/souboru.ts": "opravený obsah"},
  "summary": "Co jsem opravil",
  "skipped": "Co jsem nezapracoval a proč"
}
"""

        response = self._client.messages.create(
            model=cfg.agent("byte").model.model,
            max_tokens=cfg.agent("byte").model.max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        self._track_usage(response)
        return self._parse_json_response(response.content[0].text)

    def _parse_json_response(self, raw: str) -> Optional[dict]:
        try:
            clean = re.sub(r"^```json\s*", "", raw.strip())
            clean = re.sub(r"^```\s*", "", clean)
            clean = re.sub(r"\s*```$", "", clean)
            return json.loads(clean.strip())
        except json.JSONDecodeError as e:
            logger.error(f"[Programmer] JSON parse error: {e}\nRaw: {raw[:300]}")
            return None

    # -------------------------------------------------------------------------
    # Pomocné metody
    # -------------------------------------------------------------------------

    async def _report_error(self, issue_key: str, message: str, context: str = ""):
        full = f"❌ **Chyba při zpracování ticketu**\n\n{message}"
        if context:
            full += f"\n\n**Detail:** `{context}`"
        full += "\n\nOprav problém a přesuň ticket zpět na **In development**."
        try:
            await self._jira.add_comment(issue_key, full)
        except Exception as e:
            logger.error(f"[Programmer] Nepodařilo se zapsat chybu: {e}")

    def _make_branch_name(self, issue_key: str, issue_type: str) -> str:
        bug_types = cfg.byte.bug_issue_types
        patterns = cfg.byte.branch_pattern
        if issue_type in bug_types:
            pattern = patterns.get("bugfix", "bugfix/{ticket-id}")
        else:
            pattern = patterns.get("feat", "feat/{ticket-id}")
        return pattern.replace("{ticket-id}", issue_key.upper())

    def _format_stack(self, stack: dict) -> str:
        parts = []
        if stack.get("angular"):
            parts.append(f"Angular {stack['angular']}")
        if stack.get("dotnet"):
            parts.append(f".NET {stack['dotnet']}")
        if stack.get("php"):
            parts.append(f"PHP {stack['php']}")
        return " | ".join(parts) if parts else "neznámý"

    async def _get_release_branch(self, repo_slug: str, issue_key: str) -> Optional[str]:
        multi = cfg.byte.multi_release_repos or {}
        if repo_slug in multi:
            branches = multi[repo_slug]
            if len(branches) == 1:
                return branches[0]
            return await self._ask_release_branch(issue_key, repo_slug, branches)
        else:
            default = cfg.byte.default_release_branch
            import httpx
            token = await self._bb._get_token()
            url = f"https://api.bitbucket.org/2.0/repositories/{self._bb._workspace}/{repo_slug}/refs/branches/{default}"
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
                if resp.is_success:
                    return default
            logger.warning(f"[Programmer] {repo_slug} — větev '{default}' nenalezena")
            return None

    async def _ask_release_branch(self, issue_key: str, repo_slug: str, branches: list) -> Optional[str]:
        options = "\n".join([f"{i+1}) `{b}`" for i, b in enumerate(branches)])
        await self._jira.add_comment(
            issue_key,
            f"Našel jsem více release větví v `{repo_slug}`:\n\n{options}\n\n"
            f"Ze které mám vytvořit branch? Odpověz číslem nebo názvem větve."
        )

        byte_email = cfg.agent("byte").jira.email.lower()
        for _ in range(20):
            await asyncio.sleep(30)
            ticket = await self._jira.get_ticket(issue_key)
            if not ticket:
                continue
            comments = ticket.get("fields", {}).get("comment", {}).get("comments", [])
            for c in reversed(comments):
                if (c.get("author") or {}).get("emailAddress", "").lower() == byte_email:
                    continue
                body = self._jira._extract_text_from_adf(c.get("body", {})).strip()
                if body.isdigit():
                    idx = int(body) - 1
                    if 0 <= idx < len(branches):
                        return branches[idx]
                for b in branches:
                    if body.lower() == b.lower():
                        return b
                break

        await self._jira.add_comment(
            issue_key,
            "⏱️ Čekal jsem 10 minut na výběr release větve ale nedostal jsem odpověď.\n"
            "Prosím vyber větev a přesuň ticket zpět na **In development**."
        )
        return None

    async def _find_existing_pr(self, repo_slug: str, branch_name: str) -> Optional[dict]:
        import httpx
        token = await self._bb._get_token()
        url = f"https://api.bitbucket.org/2.0/repositories/{self._bb._workspace}/{repo_slug}/pullrequests"
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                params={"state": "OPEN", "q": f'source.branch.name="{branch_name}"'},
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            if resp.is_success:
                prs = resp.json().get("values", [])
                return prs[0] if prs else None
        return None

    async def _get_default_branch(self, repo_slug: str) -> Optional[str]:
        import httpx
        token = await self._bb._get_token()
        url = f"https://api.bitbucket.org/2.0/repositories/{self._bb._workspace}/{repo_slug}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
            if resp.is_success:
                return resp.json().get("mainbranch", {}).get("name", "master")
        return "master"

    def _build_pr_description(
        self,
        ticket_ctx: dict,
        stack: dict,
        code_result: dict,
        branch_name: str,
        main_branch: str,
    ) -> str:
        issue_key = ticket_ctx.get("ticket_id", "")
        jira_url = f"{cfg.jira_base_url}/browse/{issue_key}"
        stack_str = self._format_stack(stack)

        desc = (
            f"## [{issue_key}]({jira_url}) — {ticket_ctx.get('summary', '')}\n\n"
            f"**Stack:** {stack_str}\n"
            f"**Branch:** `{branch_name}` → `{main_branch}`\n\n"
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
