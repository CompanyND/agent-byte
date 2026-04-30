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

        logger.info(f"[Programmer] {issue_key} | repo: {repo_slug} | stack: {stack}")

        # 3. Sestavení branch name z typu ticketu
        issue_type = ticket_ctx.get("issue_type", "")
        branch_name = self._make_branch_name(issue_key, issue_type)
        stack_str = self._format_stack(stack)

        # Zjisti release větev — z ní se vytvoří branch
        release_branch = await self._get_release_branch(repo_slug, issue_key)
        if not release_branch:
            await self._report_error(
                issue_key,
                "Nepodařilo se určit release větev pro repozitář `" + repo_slug + "`.",
                "Buď větev 'release' neexistuje, nebo vypršel timeout čekání na odpověď."
            )
            return ProgrammingResult(False, message="Čeká na výběr release větve")

        # Hlavní branch repozitáře — PR míří sem (master nebo main)
        main_branch = await self._get_default_branch(repo_slug) or "master"

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

        # 5. Vytvoř branch Z release větve (nebo použij existující)
        branch_ok = await self._bb.create_branch(repo_slug, branch_name, release_branch)
        if not branch_ok:
            await self._report_error(
                issue_key,
                f"Nepodařilo se vytvořit branch `{branch_name}` z `{default_branch}`.",
                f"Zkontroluj přístupy Byte k repozitáři `{repo_slug}`."
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
            await self._report_error(
                issue_key,
                "Commit selhal.",
                f"Zkontroluj přístupy Byte k repozitáři `{repo_slug}`."
            )
            return ProgrammingResult(False, message="Commit failed")

        # 8. Vytvoř PR
        reviewer_account_id = (ticket_ctx.get("previous_assignee") or {}).get("account_id", "")
        pr = await self._bb.create_pr(
            repo_slug=repo_slug,
            title=f"[BYTE] {issue_key} — {ticket_ctx.get('summary', '')[:60]}",
            source_branch=branch_name,
            destination_branch=main_branch,
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

        # 9. Přepni Jira ticket na Ready to test + přiřaď zpět na předchozího assignee
        await self._jira.transition(issue_key, "Ready to test")
        previous_account_id = (ticket_ctx.get("previous_assignee") or {}).get("account_id")
        previous_name = (ticket_ctx.get("previous_assignee") or {}).get("display_name", "předchozí assignee")
        if previous_account_id:
            await self._jira.assign(issue_key, previous_account_id)
            logger.info(f"[Programmer] {issue_key} přiřazen zpět na {previous_name}")
        else:
            logger.warning(f"[Programmer] {issue_key} — předchozí assignee nenalezen, ticket zůstává na Byte")

        # 10. Závěrečný komentář do Jiry — stručný
        reviewer_name = (ticket_ctx.get("previous_assignee") or {}).get("display_name", "reviewer")
        pr_number = pr.get("id", "")
        summary = code_result.get("summary", "")
        skipped = code_result.get("skipped", "")

        comment_lines = [
            "✅ Hotovo",
            "",
            f"[PR #{pr_number}]({pr_url}) → `{main_branch}`",
            f"Reviewer: {reviewer_name}",
        ]
        if summary:
            comment_lines += ["", summary]
        if skipped:
            comment_lines += ["", f"Vynecháno: {skipped}"]
        comment_lines += ["", "Připomínky? Napiš do PR nebo sem napiš **zapracuj komentáře**."]

        await self._jira.add_comment(issue_key, "\n".join(comment_lines))

        # 11. Samo-dokumentace
        await self._bb.append_log(
            repo_slug,
            f"**{issue_key}** — {ticket_ctx.get('summary', '')[:60]} | "
            f"PR #{pr_id} | stack: {stack_str}"
        )

        # 12. Přičti cenu za tokeny do Jira customfield_10307
        await self._update_ticket_cost(issue_key, code_result)

        logger.info(f"[Programmer] {issue_key} dokončeno — PR #{pr_id}: {pr_url}")
        return ProgrammingResult(True, branch=branch_name, pr_url=pr_url, pr_id=pr_id)

    async def _report_error(self, issue_key: str, message: str, context: str = ""):
        """Napíše chybový komentář do Jiry — aby vývojář věděl co se stalo."""
        full_message = f"❌ **Chyba při zpracování ticketu**\n\n{message}"
        if context:
            full_message += f"\n\n**Detail:** `{context}`"
        full_message += "\n\nOprav problém a přesuň ticket zpět na **In development**."
        try:
            await self._jira.add_comment(issue_key, full_message)
        except Exception as e:
            logger.error(f"[Programmer] Nepodařilo se zapsat chybu do Jiry: {e}")

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
        stack, memories = await asyncio.gather(
            self._bb.detect_stack(repo_slug),
            self._bb.read_memory(repo_slug),
        )
        global_mem = memories[0] if memories else ""
        project_mem = memories[1] if len(memories) > 1 else ""
        repo_mem = memories[2] if len(memories) > 2 else ""

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
        self._last_input_tokens = response.usage.input_tokens
        self._last_output_tokens = response.usage.output_tokens
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

    async def _update_ticket_cost(self, issue_key: str, code_result: Optional[dict]):
        """Přičte cenu za Claude volání do Jira customfield_10307."""
        try:
            model_cfg = cfg.agent("byte").model
            cost_input = getattr(model_cfg, "cost_input_per_1m", 3.0)
            cost_output = getattr(model_cfg, "cost_output_per_1m", 15.0)

            # Tokeny jsou v code_result metadata (pokud jsou)
            # Fallback na 0 pokud nejsou k dispozici
            input_tokens = getattr(self, "_last_input_tokens", 0)
            output_tokens = getattr(self, "_last_output_tokens", 0)

            if input_tokens or output_tokens:
                cost = (input_tokens * cost_input + output_tokens * cost_output) / 1_000_000
                await self._jira.update_cost(issue_key, cost)
        except Exception as e:
            logger.warning(f"[Programmer] Nepodařilo se aktualizovat cenu: {e}")

    def _make_branch_name(self, issue_key: str, issue_type: str) -> str:
        """
        Vytvoří název větve podle typu ticketu:
        - Bug / Chyba / Dílčí úkol → bugfix/{TICKET-ID}
        - vše ostatní              → feat/{TICKET-ID}
        """
        bug_types = cfg.byte.bug_issue_types if hasattr(cfg.byte, "bug_issue_types") else [
            "Bug", "Chyba", "Subtask", "Sub-task", "Dílčí úkol"
        ]
        patterns = cfg.byte.branch_pattern if hasattr(cfg.byte, "branch_pattern") else {}
        if issue_type in bug_types:
            pattern = patterns.get("bugfix", "bugfix/{ticket-id}") if isinstance(patterns, dict) else "bugfix/{ticket-id}"
        else:
            pattern = patterns.get("feat", "feat/{ticket-id}") if isinstance(patterns, dict) else "feat/{ticket-id}"
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
        """
        Zjistí release větev pro repozitář.
        - Pokud repo má 1 release větev → použije ji
        - Pokud má více → zeptá se v Jiře a čeká na odpověď
        - Pokud nemá žádnou → vrátí None (Byte eskaluje)
        """
        multi = {}
        if hasattr(cfg.byte, "multi_release_repos"):
            multi = cfg.byte.multi_release_repos or {}

        if repo_slug in multi:
            branches = multi[repo_slug]
            if len(branches) == 1:
                logger.info(f"[Programmer] {repo_slug} — release větev: {branches[0]}")
                return branches[0]
            else:
                # Více větví — zeptej se
                return await self._ask_release_branch(issue_key, repo_slug, branches)
        else:
            # Výchozí — hledej větev 'release'
            default = cfg.byte.default_release_branch if hasattr(cfg.byte, "default_release_branch") else "release"
            token = await self._bb._get_token()
            import httpx
            url = f"https://api.bitbucket.org/2.0/repositories/{self._bb._workspace}/{repo_slug}/refs/branches/{default}"
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
                if resp.is_success:
                    logger.info(f"[Programmer] {repo_slug} — release větev: {default}")
                    return default
            logger.warning(f"[Programmer] {repo_slug} — větev '{default}' nenalezena")
            return None

    async def _ask_release_branch(self, issue_key: str, repo_slug: str, branches: list) -> Optional[str]:
        """
        Zeptá se v Jiře na výběr release větve a čeká na odpověď.
        Odpověď rozpozná z dalšího komentáře (číslo 1, 2, 3 nebo název větve).
        """
        options = "\n".join([f"{i+1}) `{b}`" for i, b in enumerate(branches)])
        await self._jira.add_comment(
            issue_key,
            f"Našel jsem více release větví v `{repo_slug}`:\n\n{options}\n\n"
            f"Ze které mám vytvořit branch? Odpověz číslem nebo názvem větve."
        )

        # Počkej max 10 minut na odpověď (polling každých 30s)
        import asyncio
        byte_email = cfg.agent("byte").jira.email.lower()
        for _ in range(20):  # 20 × 30s = 10 minut
            await asyncio.sleep(30)
            ticket = await self._jira.get_ticket(issue_key)
            if not ticket:
                continue
            comments = ticket.get("fields", {}).get("comment", {}).get("comments", [])
            # Hledej poslední komentář od člověka (ne Byte)
            for c in reversed(comments):
                author_email = (c.get("author") or {}).get("emailAddress", "").lower()
                if author_email == byte_email:
                    continue
                body = self._jira._extract_text_from_adf(c.get("body", {})).strip()
                # Zkus číslo
                if body.isdigit():
                    idx = int(body) - 1
                    if 0 <= idx < len(branches):
                        logger.info(f"[Programmer] Vybrána větev: {branches[idx]}")
                        return branches[idx]
                # Zkus název větve
                for b in branches:
                    if body.lower() == b.lower():
                        logger.info(f"[Programmer] Vybrána větev: {b}")
                        return b
                break

        # Timeout — eskaluj
        await self._jira.add_comment(
            issue_key,
            "⏱️ Čekal jsem 10 minut na výběr release větve ale nedostal jsem odpověď.\n"
            "Prosím vyber větev a přesuň ticket zpět na **In development**."
        )
        return None

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

        default_branch = ticket_ctx.get("main_branch", "master")
        desc = (
            f"## [{issue_key}]({jira_url}) — {ticket_ctx.get('summary', '')}\n\n"
            f"**Stack:** {stack_str}\n"
            f"**Branch:** `{branch_name}` → `{default_branch}`\n\n"
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