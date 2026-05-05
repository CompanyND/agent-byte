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
        Wrapper kolem _run_inner — garantuje, že se zaúčtují tokeny i při
        neúspěchu nebo výjimce (tool calling loop může spotřebovat dost
        tokenů i ve scénáři, kdy nedojdeme k commitu).
        """
        # Reset counterů pro tento běh
        self._last_input_tokens = 0
        self._last_output_tokens = 0

        try:
            return await self._run_inner(issue_key)
        finally:
            # Vždy se pokus zaúčtovat — i při neúspěchu se tokeny spotřebovaly
            if (
                getattr(self, "_last_input_tokens", 0)
                or getattr(self, "_last_output_tokens", 0)
            ):
                try:
                    await self._update_ticket_cost(issue_key, None)
                except Exception as e:
                    logger.warning(
                        f"[Programmer] {issue_key} — billing v finally selhal: {e}"
                    )

    async def _run_inner(self, issue_key: str) -> ProgrammingResult:
        """Vlastní cyklus programátora — viz run()."""
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
        # Zjisti hlavní branch repozitáře (master/main nebo specifická větev)
        main_branch = await self._get_main_branch(repo_slug, issue_key)

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

        # 6. Načti kompletní kontext repozitáře (strom 7 úrovní + soubory + commity)
        logger.info(f"[Programmer] {issue_key} — načítám kontext repozitáře {repo_slug}...")
        try:
            tree_str, files_context, commits_str = await asyncio.wait_for(
                self._byte._get_repo_context(
                    repo_slug=repo_slug,
                    ticket_summary=ticket_ctx.get("summary", ""),
                    ticket_description=ticket_ctx.get("description", ""),
                    stack=stack,
                ),
                timeout=300,  # 5 minut — velké projekty potřebují čas
            )
            logger.info(f"[Programmer] {issue_key} — kontext repozitáře načten ✅")
        except asyncio.TimeoutError:
            logger.warning(
                f"[Programmer] {issue_key} — kontext trval >5 minut, "
                f"pokračuji bez stromu souborů"
            )
            tree_str, files_context, commits_str = "", "", ""
        except Exception as e:
            logger.warning(f"[Programmer] {issue_key} — chyba kontextu: {e}")
            tree_str, files_context, commits_str = "", "", ""

        # 7. Vygeneruj kód
        code_result = await self._generate_code(
            ticket_ctx=ticket_ctx,
            stack=stack,
            global_memory=global_mem,
            project_memory=project_mem,
            repo_slug=repo_slug,
            branch_name=branch_name,
            tree_str=tree_str,
            files_context=files_context,
            commits_str=commits_str,
        )

        if not code_result:
            logger.warning(
                f"[Programmer] {issue_key} — code_result je None "
                f"(JSON parse error nebo prázdná odpověď z Claude)"
            )
            await self._jira.add_comment(
                issue_key,
                "❌ Kód se mi nepodařilo vygenerovat — odpověď z modelu nebyla "
                "ve správném formátu (JSON parse error). Zkus to prosím znovu, "
                "případně zkontroluj logy Byte agenta."
            )
            return ProgrammingResult(False, message="Code generation failed (parse error)")

        # Claude vrátil dict, ale prázdné files → poslal `skipped` s vysvětlením
        if not code_result.get("files"):
            skipped = code_result.get("skipped") or "Bez dalších detailů."
            logger.warning(
                f"[Programmer] {issue_key} — Claude vrátil prázdné files. "
                f"Skipped: {skipped[:200]}"
            )
            await self._jira.add_comment(
                issue_key,
                f"❌ Nemám dost informací na implementaci.\n\n"
                f"**Co mi chybí:**\n{skipped}\n\n"
                f"Doplň prosím detaily do popisu ticketu a přesuň zpět na "
                f"**In development**."
            )
            return ProgrammingResult(False, message="Code generation skipped (insufficient info)")

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

        # 11. Aktualizuj repozitářovou paměť — na pozadí
        asyncio.create_task(self._update_repo_memory(
            repo_slug=repo_slug,
            issue_key=issue_key,
            stack=stack,
            code_result=code_result,
            ticket_ctx=ticket_ctx,
        ))

        # 12. Samo-dokumentace
        code_summary = code_result.get("summary", "") if code_result else ""
        code_skipped = code_result.get("skipped", "") if code_result else ""
        log_lines = [
            f"**[{issue_key}]** — {ticket_ctx.get('summary', '')[:80]}",
            f"akce: program | PR #{pr_id} → `{main_branch}` | stack: {stack_str}",
        ]
        if code_summary:
            log_lines.append(f"co: {code_summary[:200]}")
        if code_skipped:
            log_lines.append(f"vynecháno: {code_skipped[:100]}")
        await self._bb.append_log(repo_slug, " | ".join(log_lines[:2]) + (f"\n  {log_lines[2]}" if len(log_lines) > 2 else ""))

        # Pozn.: účtování tokenů řeší finally v run() — garantované i při fail cestě.

        logger.info(f"[Programmer] {issue_key} dokončeno — PR #{pr_id}: {pr_url}")
        return ProgrammingResult(True, branch=branch_name, pr_url=pr_url, pr_id=pr_id)

    async def _update_repo_memory(
        self,
        repo_slug: str,
        issue_key: str,
        stack: dict,
        code_result: Optional[dict],
        ticket_ctx: dict,
    ):
        """
        Po každém úspěšném PR Byte zapíše strukturované poznatky o projektu
        do byte-memory/repos/{repo-slug}/pamet.md.

        Cíl: aby příště Byte nemusel zjišťovat stejné věci znovu.
        """
        if not repo_slug or not code_result:
            return

        try:
            from datetime import datetime
            memory_cfg = cfg.byte.memory
            memory_repo = memory_cfg.get("global_repo", "byte-memory")
            repo_path = memory_cfg.get("repo_path", "repos/{repo-slug}/pamet.md").replace(
                "{repo-slug}", repo_slug
            )
            existing_memory = await self._bb.get_file(memory_repo, repo_path) or ""

            stack_str = self._format_stack(stack)
            files_changed = list((code_result.get("files") or {}).keys())
            summary = code_result.get("summary", "")
            today = datetime.now().strftime("%Y-%m-%d")

            prompt = (
                f"Právě jsi dokončil práci na repozitáři `{repo_slug}`.\n\n"
                f"Ticket: {issue_key} — {ticket_ctx.get('summary', '')}\n"
                f"Stack: {stack_str or 'neznámý'}\n"
                f"Soubory které jsi upravoval: {', '.join(files_changed[:15]) or 'neznámé'}\n"
                f"Co jsi implementoval: {summary}\n\n"
                f"Aktuální paměť:\n{existing_memory or '(prázdná)'}\n\n"
                f"Zapiš POUZE nové poznatky které v paměti ještě nejsou. "
                f"Pokud nemáš nic nového, odpověz prázdným řetězcem.\n\n"
                f"Formát — každá sekce jen pokud máš nové info:\n\n"
                f"## {today} — {issue_key}\n\n"
                f"**Architektura:**\n"
                f"- [kde leží controllery, services, moduly — konkrétní cesty]\n"
                f"- [jak je projekt strukturován — co je kde]\n\n"
                f"**Stack a konvence:**\n"
                f"- [verze frameworků, ORM, autentizace]\n"
                f"- [coding konvence které jsi viděl v kódu]\n\n"
                f"**Specifika projektu:**\n"
                f"- [kde jsou texty/překlady, jak funguje konfigurace]\n"
                f"- [co je read-only, co se generuje automaticky]\n"
                f"- [důležité třídy/services/helpers které se opakují]\n\n"
                f"**Zjištěno při práci:**\n"
                f"- [konkrétní poznatky z tohoto ticketu které budou užitečné příště]\n\n"
                f"Buď konkrétní — piš cesty k souborům, názvy tříd, vzory. "
                f"Ne obecnosti jako 'projekt používá Angular' nebo 'kód je dobře strukturovaný'."
            )

            response = self._client.messages.create(
                model=cfg.agent("byte").model.model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )

            observations = response.content[0].text.strip()

            if not observations or len(observations) < 30:
                logger.info(f"[Programmer] {repo_slug} — žádné nové poznatky")
                return

            updated = (existing_memory.rstrip() + "\n\n" + observations).strip()
            await self._bb.commit_files(
                repo_slug=memory_repo,
                branch="main",
                files={repo_path: updated},
                message=f"memory: {repo_slug} — poznatky z {issue_key}",
            )
            logger.info(f"[Programmer] Repozitářová paměť {repo_slug} aktualizována ({len(observations)} znaků)")

        except Exception as e:
            logger.warning(f"[Programmer] Repozitářová paměť selhala: {e}")

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

    async def _fetch_relevant_files(
        self,
        repo_slug: str,
        ticket_summary: str,
        ticket_description: str,
        stack: dict,
    ) -> dict[str, str]:
        """
        Zeptá se Claudu které soubory jsou relevantní pro daný ticket,
        pak načte jejich obsah z BB. Vrátí {path: content}.
        Hlídá celkový limit znaků aby nepřeteklo kontextové okno.
        """
        CONTEXT_LIMIT = 150_000
        FILE_MAX_CHARS = 10_000

        try:
            root_files = await self._bb.list_dir(repo_slug)
            structure_lines = []
            for f in root_files:
                structure_lines.append(f["path"] + ("/" if f.get("type") == "commit_directory" else ""))
                if f.get("type") == "commit_directory":
                    sub = await self._bb.list_dir(repo_slug, f["path"])
                    for sf in sub:
                        structure_lines.append(f"  {sf['path']}")

            structure = "\n".join(structure_lines)

            prompt = (
                f"Repozitář: {repo_slug}\n"
                f"Stack: {self._format_stack(stack)}\n"
                f"Struktura repozitáře:\n{structure}\n\n"
                f"Ticket: {ticket_summary}\n"
                f"Popis: {ticket_description[:800]}\n\n"
                f"Vypiš VŠECHNY soubory které jsou relevantní pro implementaci tohoto ticketu.\n"
                f"Každou cestu na nový řádek. Pouze cesty, bez komentářů, bez číslování.\n"
                f"Zahrň: komponenty, service, module, controller, model, DTO, interface soubory\n"
                f"které přímo nebo nepřímo souvisí s tématem ticketu.\n"
                f"Čím více kontextu Byte uvidí, tím lepší kód napíše."
            )

            response = self._client.messages.create(
                model=cfg.agent("byte").model.model,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            self._track_usage(response)

            raw_paths = response.content[0].text.strip().split("\n")
            paths = []
            for line in raw_paths:
                path = line.strip().lstrip("- ").lstrip("* ").lstrip("0123456789. ").strip()
                if path and not path.startswith("#") and "." in path:
                    paths.append(path)

            logger.info(f"[Programmer] Claude označil {len(paths)} relevantních souborů")

            contents = await asyncio.gather(
                *[self._bb.get_file(repo_slug, p) for p in paths],
                return_exceptions=True
            )

            result = {}
            total_chars = 0
            skipped = []

            for path, file_content in zip(paths, contents):
                if not isinstance(file_content, str) or not file_content:
                    continue
                truncated = file_content[:FILE_MAX_CHARS]
                if total_chars + len(truncated) > CONTEXT_LIMIT:
                    skipped.append(path)
                    continue
                result[path] = truncated
                total_chars += len(truncated)

            if skipped:
                logger.info(f"[Programmer] Vynecháno {len(skipped)} souborů (limit): {', '.join(skipped[:5])}")

            logger.info(f"[Programmer] Načteno {len(result)} souborů ({total_chars:,} / {CONTEXT_LIMIT:,} znaků)")
            return result

        except Exception as e:
            logger.warning(f"[Programmer] _fetch_relevant_files selhal: {e}")
            return {}

    # -------------------------------------------------------------------------
    # Tool definice pro Claude (search_code, get_file, list_dir)
    # -------------------------------------------------------------------------

    # Limit na celý loop — bezpečnostní strop. 15 turnů by mělo bohatě stačit
    # i pro komplexní bugfix s několika hledáními a 2–3 načtenými soubory.
    MAX_AGENT_TURNS = 10
    # Limit na velikost obsahu jednoho souboru, který se vrací modelu
    TOOL_FILE_MAX_CHARS = 8000
    # Stagnation score thresholdy
    STAGNATION_NUDGE = 4    # jemné pošťouchnutí
    STAGNATION_FORCE = 6    # tvrdý požadavek na JSON

    @staticmethod
    def _agent_tools() -> list[dict]:
        """Tool definice pro Claude tool use API."""
        return [
            {
                "name": "search_code",
                "description": (
                    "Hledá v kódu repozitáře přes Bitbucket workspace search. "
                    "Použij když potřebuješ najít, kde je definovaná určitá "
                    "vlastnost, funkce, komponenta nebo kde se něco používá. "
                    "Vrátí seznam souborů s úryvky kódu kolem shod. "
                    "Hledání je code-aware — definice se řadí výš než použití. "
                    "Tipy: pro frázi obal do uvozovek; používej konkrétní "
                    "identifikátory (ne anglická slova jako 'error')."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Hledaný text. Příklady: 'Rating', "
                                "'\"this.rating\"', 'getRatingValue'. "
                                "Pro frázi víc slov použij \"\"."
                            ),
                        },
                        "ext": {
                            "type": "string",
                            "description": (
                                "Volitelná přípona souboru bez tečky "
                                "(ts, cs, html, scss). Filtruje výsledky."
                            ),
                        },
                        "path": {
                            "type": "string",
                            "description": (
                                "Volitelný filtr cesty (např. "
                                "'src/app/components')."
                            ),
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "get_file",
                "description": (
                    "Načte celý obsah souboru z repozitáře (HEAD branche). "
                    "Použij po search_code, když chceš vidět víc kontextu "
                    "kolem nálezu — celý komponent, službu, model apod. "
                    "Velké soubory budou zkrácené."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Cesta k souboru relativně k rootu repa. "
                                "Příklad: 'src/app/components/rating/"
                                "rating.component.ts'."
                            ),
                        },
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "list_dir",
                "description": (
                    "Vypíše obsah adresáře v repozitáři. Použij když "
                    "chceš prozkoumat strukturu konkrétní části projektu."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Cesta k adresáři relativně k rootu repa. "
                                "Pro root nech prázdné."
                            ),
                        },
                    },
                },
            },
        ]

    async def _execute_tool(self, name: str, tool_input: dict, repo_slug: str) -> tuple[str, str]:
        """Spustí jeden tool call a vrátí (text result, fingerprint).

        Fingerprint slouží pro detekci zacyklení — je to deterministický
        string reprezentující obsah výsledku (setříděné cesty souborů nebo
        cesta k souboru). Pokud se fingerprint opakuje, Claude nic nového
        nezjistil.
        """
        try:
            if name == "search_code":
                query = tool_input.get("query", "").strip()
                if not query:
                    return "Chyba: prázdný query."
                results = await self._bb.search_code(
                    query=query,
                    repo_slug=repo_slug,
                    ext=tool_input.get("ext"),
                    path=tool_input.get("path"),
                    max_results=15,
                )
                if not results:
                    return f"Žádné výsledky pro '{query}'.", f"empty:{query}"
                fp = "|".join(sorted(r["path"] for r in results))
                return self._bb.format_search_results(results), f"search:{fp}"

            if name == "get_file":
                path = tool_input.get("path", "").strip()
                if not path:
                    return "Chyba: prázdná cesta."
                content = await self._bb.get_file(repo_slug, path)
                if content is None:
                    return f"Soubor '{path}' neexistuje nebo není dostupný.", f"missing:{path}"
                if len(content) > self.TOOL_FILE_MAX_CHARS:
                    truncated = content[: self.TOOL_FILE_MAX_CHARS]
                    text = (
                        f"=== {path} (zkráceno: {len(content)} znaků > "
                        f"limit {self.TOOL_FILE_MAX_CHARS}) ===\n{truncated}\n"
                        f"=== ... pokračování vynecháno ==="
                    )
                    return text, f"file:{path}"
                return f"=== {path} ===\n{content}", f"file:{path}"

            if name == "list_dir":
                path = tool_input.get("path", "") or ""
                items = await self._bb.list_dir(repo_slug, path)
                if not items:
                    return f"Adresář '{path or '/'}' je prázdný nebo neexistuje.", f"empty_dir:{path}"
                lines = [f"Obsah {path or '/'}:"]
                for item in items[:80]:
                    item_path = item.get("path", "")
                    name_only = item_path.split("/")[-1]
                    kind = "📁" if item.get("type") == "commit_directory" else "📄"
                    lines.append(f"  {kind} {name_only}")
                if len(items) > 80:
                    lines.append(f"  ... a dalších {len(items) - 80}")
                return "\n".join(lines), f"dir:{path}"

            return f"Neznámý tool: {name}", f"unknown:{name}"
        except Exception as e:
            logger.warning(f"[Programmer] Tool '{name}' selhal: {e}")
            return f"Chyba při volání toolu '{name}': {e}", f"error:{name}"

    # -------------------------------------------------------------------------
    # Pre-search heuristika
    # -------------------------------------------------------------------------

    @staticmethod
    def _extract_search_candidates(text: str) -> list[str]:
        """Vytáhne z textu identifikátory vhodné pro pre-search.

        Hledá: CamelCase / camelCase identifikátory (3+ znaky), text
        v uvozovkách/zpětných uvozovkách, konkrétní názvy souborů.
        Filtruje běžná anglická slova a Jira/HTTP/AC keywords.
        """
        if not text:
            return []

        # Stopwords — buď příliš obecné, nebo specifické pro AC/Jira
        STOPWORDS = {
            "TypeError", "Error", "Exception", "Cannot", "Property", "True",
            "False", "None", "Null", "Object", "Array", "String", "Number",
            "Boolean", "Function", "Promise", "Observable", "Subject",
            "Component", "Service", "Module", "Provider", "Injectable",
            "Given", "When", "Then", "And", "Scenario", "Feature",
            "Sentry", "Jira", "JIRA", "Bitbucket", "Angular", "React", "Vue",
            "GIVEN", "WHEN", "THEN", "AND", "SCENARIO",
            # České AC slova — typická v acceptance criteria
            "Aplikace", "Uživatel", "Metoda", "Subscriber", "Hodnota",
            "Objekt", "Vlastnost", "Stream", "Pipeline",
            # Sentry/Jira obecná slova
            "Issue", "Ticket", "Bug", "Task", "Story", "Sprint",
        }

        # Regex pro minified JS bundle chunky (hash 8+ hex znaků)
        # Příklady: 23.7b287c150bbf691de56c.js, main.eff166e8985ffabbc879.js
        _MINIFIED_RE = re.compile(r"[a-f0-9]{8,}")

        candidates: list[str] = []
        seen: set[str] = set()

        # 1. Text v uvozovkách (single, double, backticks) — typicky property names
        for match in re.findall(r"['\"`]([A-Za-z_][A-Za-z0-9_]{1,30})['\"`]", text):
            if match not in seen and match not in STOPWORDS and len(match) >= 3:
                seen.add(match)
                candidates.append(match)

        # 2. CamelCase / PascalCase identifikátory (3+ znaků, max 30)
        for match in re.findall(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+){0,3}|[a-z]+(?:[A-Z][a-z]+){1,3})\b", text):
            if (
                match not in seen
                and match not in STOPWORDS
                and 3 <= len(match) <= 30
            ):
                seen.add(match)
                candidates.append(match)

        # 3. Názvy souborů — *.ts, *.cs, *.html, *.scss (ne minified bundle chunky)
        for match in re.findall(r"\b([\w.-]+\.(?:ts|tsx|cs|html|scss|js|vue))\b", text):
            if match not in seen and not _MINIFIED_RE.search(match):
                seen.add(match)
                candidates.append(match)

        return candidates[:8]  # Max 8 kandidátů — zabraňuje rate limitu

    @staticmethod
    def _tokenize_query(query: str) -> frozenset[str]:
        """Rozdělí search query na tokeny pro porovnání podobnosti.

        Odstraní BB search modifiery (repo:, ext:, path:, lang:),
        uvozovky a rozdělí na slova >=3 znaky.
        """
        # Odstraň BB modifiery
        q = re.sub(r"\b(?:repo|ext|path|lang):\S+", "", query)
        # Odstraň uvozovky a speciální znaky
        q = re.sub(r"[\x27\"` ()\[\]{}]", " ", q)
        return frozenset(w.lower() for w in q.split() if len(w) >= 3)

    def _compute_stagnation_score(
        self,
        turn_fps: list[str],
        seen_fps: set[str],
        unique_paths: set[str],
        prev_unique_count: int,
        turn_queries: list[str],
        recent_queries: list[frozenset[str]],
    ) -> int:
        """Spočítá stagnation score pro aktuální turn (0–3).

        +1  Všechny fingerprinty turnu jsou už viděné (duplicitní výsledky)
        +1  Počet unikátních cest se od minulého turnu nezvýšil (žádné nové info)
        +1  Alespoň jeden search query sdílí token s některým z posledních 2 turnů
        """
        score = 0

        # Signál 1: duplicitní fingerprinty
        if turn_fps and all(fp in seen_fps for fp in turn_fps):
            score += 1

        # Signál 2: žádné nové soubory
        if len(unique_paths) == prev_unique_count and unique_paths:
            score += 1

        # Signál 3: query variuje stejné téma
        if turn_queries and recent_queries:
            current_tokens = frozenset().union(*[
                self._tokenize_query(q) for q in turn_queries
            ])
            for prev_tokens in recent_queries[-2:]:
                if len(current_tokens & prev_tokens) >= 1:
                    score += 1
                    break

        return score

    async def _pre_search_context(
        self, repo_slug: str, ticket_ctx: dict
    ) -> str:
        """Pre-search — z ticketu vytáhne kandidáty a paralelně prohledá repo.

        Výstup je hotový string pro vložení do user message. Když nic nenajde,
        vrátí prázdný řetězec.
        """
        # Kombinuj summary + description + AC; Sentry stack trace je v description
        haystack = " ".join([
            ticket_ctx.get("summary", "") or "",
            ticket_ctx.get("description", "") or "",
            ticket_ctx.get("acceptance_criteria", "") or "",
        ])

        candidates = self._extract_search_candidates(haystack)
        if not candidates:
            return ""

        # Top 4 — víc nemá smysl, každý je 1 API call
        top = candidates[:4]
        logger.info(f"[Programmer] Pre-search kandidáti: {top}")

        results = await asyncio.gather(
            *[self._bb.search_code(q, repo_slug=repo_slug, max_results=10) for q in top],
            return_exceptions=True,
        )

        sections = []
        for q, r in zip(top, results):
            if isinstance(r, Exception):
                logger.warning(f"[Programmer] Pre-search '{q}' selhal: {r}")
                continue
            if not r:
                continue
            sections.append(
                f"### Hledání '{q}' v {repo_slug}\n\n"
                + self._bb.format_search_results(r[:5])
            )

        if not sections:
            return ""

        return (
            "\n\n## Pre-search v repozitáři\n\n"
            "Předem jsem prohledal repo na pravděpodobné identifikátory "
            "z ticketu. Použij `search_code` pro další hledání.\n\n"
            + "\n".join(sections)
        )

    # -------------------------------------------------------------------------
    # Generování kódu — agentic loop s tool use
    # -------------------------------------------------------------------------

    async def _generate_code(
        self,
        ticket_ctx: dict,
        stack: dict,
        global_memory: str,
        project_memory: str,
        repo_slug: str,
        branch_name: str,
        tree_str: str = "",
        files_context: str = "",
        commits_str: str = "",
    ) -> Optional[dict]:
        """
        Vygeneruje kód pro ticket pomocí agentic loop.
        Claude má tooly search_code, get_file, list_dir a iterativně si
        načítá kontext, dokud nemá dost informací pro implementaci.
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

        # Statický kontext (strom, commity, předem označené soubory)
        if tree_str:
            user_message += f"\n\n## Struktura repozitáře\n\n```\n{tree_str[:6000]}\n```"
        if commits_str:
            user_message += f"\n\n## Nedávná aktivita v repozitáři\n\n{commits_str}"
        if files_context:
            user_message += f"\n\n## Existující kód (relevantní soubory)\n\n{files_context}"

        # Pre-search — vytáhne identifikátory a předhodí výsledky modelu
        try:
            pre_search = await self._pre_search_context(repo_slug, ticket_ctx)
            if pre_search:
                user_message += pre_search
        except Exception as e:
            logger.warning(f"[Programmer] Pre-search selhal (pokračuju bez něj): {e}")

        # Instrukce pro tool use loop a finální JSON
        user_message += f"""

## Postup

Máš k dispozici tyto nástroje:
- `search_code` — hledání v kódu repozitáře `{repo_slug}`
- `get_file` — načtení celého obsahu souboru
- `list_dir` — výpis adresáře

**Pracuj iterativně:**
1. Pokud nemáš dost kontextu, použij tooly pro průzkum kódu.
2. Zaměř se na soubory, kterých se chyba týká — najdi je, načti, pochop.
3. Když máš dost kontextu, vrať finální odpověď jako JSON (viz níže).

**Finální odpověď** — POUZE validní JSON, nic okolo:

{{
  "files": {{
    "cesta/k/souboru.ts": "celý nový obsah souboru",
    "cesta/k/testu.spec.ts": "celý nový obsah testu"
  }},
  "summary": "Co jsem udělal — 2–3 věty pro PR popis",
  "skipped": "Co jsem záměrně vynechal a proč (nebo prázdný string)"
}}

Pokud opravdu nemáš dost informací ani po průzkumu, vrať JSON s prázdným
`files` a v `skipped` napiš konkrétně co chybí a kde jsi hledal.

**Pravidla:**
- Piš kód odpovídající existující architektuře projektu (najdi si vzory)
- Respektuj verzi stacku (Angular {stack.get('angular') or '?'}, .NET {stack.get('dotnet') or '?'})
- Nevynechávej imports
- Nepiš placeholdery jako "// TODO implement" — implementuj
- V `files` posílej CELÝ obsah souboru (commit mechanismus potřebuje celý soubor) — ale měň jen minimum řádků nutných pro fix. Zbytek zkopíruj přesně jak je. Reviewer musí v PR diff na první pohled vidět co ses dotkl.
- NIKDY nezakomentovávej starý kód (žádné `// old:`, `// bylo:`, `/* původní kód */` apod.) — buď ho oprav, nebo úplně smaž. Komentáře pro vysvětlení či pochopení kontextu jsou v pořádku, nenechávej ale mrtvý kód.
"""

        # Agentic loop
        messages = [{"role": "user", "content": user_message}]
        tools = self._agent_tools()
        total_input_tokens = 0
        total_output_tokens = 0
        final_text: Optional[str] = None

        # Stagnation tracking
        seen_fps: set[str] = set()          # viděné fingerprinty
        unique_paths: set[str] = set()      # unikátní cesty souborů celkem
        recent_query_tokens: list[frozenset[str]] = []  # tokenizované queries posledních turnů
        stagnation_score = 0                # kumulativní score

        for turn in range(self.MAX_AGENT_TURNS):
            response = self._client.messages.create(
                model=model_cfg.model,
                max_tokens=model_cfg.max_tokens,
                system=system_prompt,
                tools=tools,
                messages=messages,
            )

            total_input_tokens += response.usage.input_tokens
            total_output_tokens += response.usage.output_tokens

            # Posbírej tool_use bloky a text bloky
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            text_blocks = [b for b in response.content if b.type == "text"]

            logger.info(
                f"[Programmer] Turn {turn + 1}/{self.MAX_AGENT_TURNS} | "
                f"stop: {response.stop_reason} | "
                f"tools: {len(tool_uses)} | "
                f"stagnation: {stagnation_score} | "
                f"text: {sum(len(b.text) for b in text_blocks)} chars | "
                f"tokeny: {response.usage.input_tokens}+"
                f"{response.usage.output_tokens}"
            )

            if response.stop_reason == "tool_use" and tool_uses:
                # Přidej assistant zprávu (celý content) a vykonej tooly paralelně
                messages.append({"role": "assistant", "content": response.content})

                raw_results = await asyncio.gather(*[
                    self._execute_tool(tu.name, tu.input, repo_slug)
                    for tu in tool_uses
                ])

                # Rozbal (text, fingerprint) tuples
                tool_results = [r[0] for r in raw_results]
                turn_fps = [r[1] for r in raw_results]

                # Loguj pro přehled
                for tu, result in zip(tool_uses, tool_results):
                    preview = " ".join(str(tu.input).split())[:100]
                    logger.info(
                        f"[Programmer]   → {tu.name}({preview}) "
                        f"→ {len(result)} chars"
                    )

                # --- Stagnation score ---
                prev_unique_count = len(unique_paths)

                # Extrahuj cesty ze search výsledků a get_file volání
                turn_queries: list[str] = []
                for tu in tool_uses:
                    if tu.name == "search_code":
                        q = tu.input.get("query", "")
                        if q:
                            turn_queries.append(q)
                    elif tu.name == "get_file":
                        p = tu.input.get("path", "")
                        if p:
                            unique_paths.add(p)

                # Extrahuj cesty z fingerprints search výsledků
                for fp in turn_fps:
                    if fp.startswith("search:"):
                        for p in fp[7:].split("|"):
                            if p:
                                unique_paths.add(p)

                turn_score = self._compute_stagnation_score(
                    turn_fps=turn_fps,
                    seen_fps=seen_fps,
                    unique_paths=unique_paths,
                    prev_unique_count=prev_unique_count,
                    turn_queries=turn_queries,
                    recent_queries=recent_query_tokens,
                )

                # Pokud přibyly nové cesty, resetuj score — Claude postupuje vpřed
                if len(unique_paths) > prev_unique_count:
                    stagnation_score = 0
                else:
                    stagnation_score += turn_score

                # Aktualizuj tracking
                seen_fps.update(turn_fps)
                if turn_queries:
                    recent_query_tokens.append(frozenset().union(*[
                        self._tokenize_query(q) for q in turn_queries
                    ]))
                    recent_query_tokens = recent_query_tokens[-3:]  # max 3

                logger.debug(
                    f"[Programmer]   Stagnation turn_score={turn_score} "
                    f"cumulative={stagnation_score} "
                    f"unique_paths={len(unique_paths)}"
                )

                # Nudge / force na základě kumulativního score
                nudge_msg: Optional[str] = None
                if stagnation_score >= self.STAGNATION_FORCE:
                    nudge_msg = (
                        "[STOP HLEDÁNÍ — POSLEDNÍ VAROVÁNÍ] "
                        "Opakuješ stejné dotazy a nenacházíš nic nového. "
                        "Máš dost kontextu. NYNÍ musíš vrátit finální JSON "
                        "s {\"files\": ..., \"summary\": ..., \"skipped\": ...}. "
                        "Žádný další tool_use. Pouze JSON."
                    )
                    logger.warning(
                        f"[Programmer] Stagnation FORCE (score={stagnation_score}) "
                        f"— posílám tvrdé upozornění"
                    )
                elif stagnation_score >= self.STAGNATION_NUDGE:
                    nudge_msg = (
                        "[Pozn. pro Byte] Opakuješ výsledky, které už znáš. "
                        "Vyčerpal jsi dostupný kontext z tohoto repozitáře. "
                        "Pokud máš dost informací → vrať finální JSON. "
                        "Pokud ne → zkus zcela odlišný typ dotazu nebo vrať "
                        "JSON s prázdným files a v skipped napiš co ti chybí."
                    )
                    logger.info(
                        f"[Programmer] Stagnation NUDGE (score={stagnation_score}) "
                        f"— pošťuchuji k finální odpovědi"
                    )

                # Sestav tool_result content (s případným nudge)
                tool_result_content = []
                for i, (tu, result) in enumerate(zip(tool_uses, tool_results)):
                    # Nudge přidáme k prvnímu tool_result aby ho Claude neprehlédl
                    result_text = result
                    if nudge_msg and i == 0:
                        result_text = result + f"\n\n{nudge_msg}"
                    tool_result_content.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": result_text,
                    })

                messages.append({
                    "role": "user",
                    "content": tool_result_content,
                })
                continue

            # end_turn / max_tokens / stop_sequence — máme finální odpověď
            if text_blocks:
                final_text = "\n".join(b.text for b in text_blocks)
            break
        else:
            # Vyčerpali jsme turn limit — zkus vytáhnout poslední text
            logger.warning(
                f"[Programmer] Vyčerpán limit {self.MAX_AGENT_TURNS} turnů "
                f"bez finální odpovědi (stagnation_score={stagnation_score})"
            )
            text_blocks = [b for b in response.content if b.type == "text"]
            if text_blocks:
                final_text = "\n".join(b.text for b in text_blocks)

        self._last_input_tokens = total_input_tokens
        self._last_output_tokens = total_output_tokens
        logger.info(
            f"[Programmer] Loop hotov | "
            f"celkem tokeny: {total_input_tokens}+{total_output_tokens}"
        )

        if not final_text:
            logger.error("[Programmer] Žádný finální text z modelu")
            return None

        return self._parse_code_response(final_text)


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
        """Parsuje JSON odpověď od Claudea.

        Tolerantní vůči obalujícímu textu — Claude občas v agentic loopu
        před nebo za JSON přidá komentář. Hledáme největší validní JSON
        objekt obsahující "files".
        """
        if not raw:
            return None
        clean = raw.strip()
        # Odstraň markdown code bloky pokud jsou
        clean = re.sub(r"^```(?:json)?\s*", "", clean)
        clean = re.sub(r"\s*```$", "", clean)
        clean = clean.strip()

        # Fast path — celá zpráva je JSON
        try:
            return json.loads(clean)
        except json.JSONDecodeError:
            pass

        # Fallback — najdi první { a poslední } a zkus dekódovat
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end > start:
            candidate = clean[start : end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError as e:
                head = raw[:500].replace("\n", " ")
                tail = raw[-500:].replace("\n", " ") if len(raw) > 500 else ""
                logger.error(
                    f"[Programmer] JSON parse error: {e}\n"
                    f"Raw length: {len(raw)} chars\n"
                    f"Head: {head}\n"
                    f"Tail: {tail}"
                )
                return None

        logger.error(
            f"[Programmer] V odpovědi nebyl nalezen JSON objekt. "
            f"Raw length: {len(raw)} | Head: {raw[:300]}"
        )
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

            # Tokeny se sčítají v _generate_code přes všechny turny tool loopu
            input_tokens = getattr(self, "_last_input_tokens", 0)
            output_tokens = getattr(self, "_last_output_tokens", 0)

            if not (input_tokens or output_tokens):
                return  # Nic se nespotřebovalo (např. fail před LLM voláním)

            cost = (input_tokens * cost_input + output_tokens * cost_output) / 1_000_000
            await self._jira.update_cost(issue_key, cost)
            logger.info(
                f"[Billing] {issue_key} | tokeny: {input_tokens}+{output_tokens} | "
                f"cena: ${cost:.4f}"
            )
        except Exception as e:
            logger.warning(f"[Programmer] Nepodařilo se aktualizovat cenu: {e}")

    def _make_branch_name(self, issue_key: str, issue_type: str) -> str:
        """
        Vytvoří název větve podle typu ticketu:
        - Bug / Chyba / Dílčí úkol → bugfix/{TICKET-ID}
        - vše ostatní              → feature/{TICKET-ID}
        """
        bug_types = cfg.byte.bug_issue_types if hasattr(cfg.byte, "bug_issue_types") else [
            "Bug", "Chyba", "Subtask", "Sub-task", "Dílčí úkol"
        ]
        patterns = cfg.byte.branch_pattern if hasattr(cfg.byte, "branch_pattern") else {}
        if issue_type in bug_types:
            pattern = patterns.get("bugfix", "bugfix/{ticket-id}") if isinstance(patterns, dict) else "bugfix/{ticket-id}"
        else:
            pattern = patterns.get("feat", "feature/{ticket-id}") if isinstance(patterns, dict) else "feature/{ticket-id}"
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
            f"Ze které mám vytvořit branch? Odpověz číslem nebo názvem větve.\n\n"
            f"⏱️ Na odpověď čekám **10 minut**. Pokud nestihneš, přesuň ticket zpět na **In development**."
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

    async def _get_main_branch(self, repo_slug: str, issue_key: str) -> str:
        """
        Zjistí hlavní branch repozitáře kam míří PR.
        Pokud existuje více kandidátů (master, jakub/master atd.),
        zeptá se v Jiře. Výsledek uloží do repozitářové paměti.
        """
        import httpx

        # 1. Zkontroluj paměť
        memory_cfg = cfg.byte.memory
        memory_repo = memory_cfg.get("global_repo", "byte-memory")
        repo_path = memory_cfg.get("repo_path", "repos/{repo-slug}/pamet.md").replace("{repo-slug}", repo_slug)
        existing_memory = await self._bb.get_file(memory_repo, repo_path) or ""

        for line in existing_memory.split("\n"):
            if line.startswith("main_branch:"):
                branch = line.replace("main_branch:", "").strip()
                logger.info(f"[Programmer] {repo_slug} — main branch z paměti: {branch}")
                return branch

        # 2. Načti všechny větve
        try:
            token = await self._bb._get_token()
            url = f"https://api.bitbucket.org/2.0/repositories/{self._bb._workspace}/{repo_slug}/refs/branches"
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    url, params={"pagelen": 50},
                    headers={"Authorization": f"Bearer {token}"}, timeout=10,
                )
                branches = [b["name"] for b in resp.json().get("values", [])] if resp.is_success else []
        except Exception:
            return "master"

        # 3. Najdi kandidáty
        MAIN_PATTERNS = {"master", "main", "develop", "trunk"}
        candidates = [
            b for b in branches
            if b.lower() in MAIN_PATTERNS
            or b.lower().endswith("/master")
            or b.lower().endswith("/main")
        ]

        if not candidates:
            return "master"

        if len(candidates) == 1:
            await self._save_main_branch(repo_slug, candidates[0], memory_repo, repo_path, existing_memory)
            return candidates[0]

        # 4. Více kandidátů — zeptej se
        selected = await self._ask_main_branch(issue_key, repo_slug, candidates)
        if selected:
            await self._save_main_branch(repo_slug, selected, memory_repo, repo_path, existing_memory)
            return selected

        return candidates[0]

    async def _ask_main_branch(self, issue_key: str, repo_slug: str, branches: list) -> Optional[str]:
        """Zeptá se v Jiře na výběr cílové větve pro PR."""
        options = "\n".join([f"{i+1}) `{b}`" for i, b in enumerate(branches)])
        await self._jira.add_comment(
            issue_key,
            f"Našel jsem více možných cílových větví pro PR v `{repo_slug}`:\n\n{options}\n\n"
            f"Do které větve má mírit PR? Odpověz číslem nebo názvem.\n\n"
            f"⏱️ Na odpověď čekám **10 minut**. Pokud nestihneš, přesuň ticket zpět na **In development**."
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
        return None

    async def _save_main_branch(
        self, repo_slug: str, branch: str,
        memory_repo: str, repo_path: str, existing_memory: str
    ):
        """Uloží main branch do repozitářové paměti."""
        if "main_branch:" in existing_memory:
            return
        updated = (existing_memory.rstrip() + f"\nmain_branch: {branch}").strip()
        await self._bb.commit_files(
            repo_slug=memory_repo, branch="main",
            files={repo_path: updated},
            message=f"memory: {repo_slug} — main branch je {branch}",
        )
        logger.info(f"[Programmer] {repo_slug} — main branch {branch} uložena do paměti")

    async def _get_default_branch(self, repo_slug: str) -> Optional[str]:
        """Záložní metoda pro zpětnou kompatibilitu."""
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