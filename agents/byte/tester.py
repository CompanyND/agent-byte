"""
core/tester.py — Byte E2E Tester.

Spouští se když ticket přejde do stavu IN TESTING a je přiřazen na Byte.

Flow:
1. Zkontroluje jestli E2ETests/ existuje v repozitáři
2. Pokud ne → zeptá se na URL a čeká na potvrzení (@Byte vytvoř E2ETests)
3. Pokud ano → načte AK + PR diff z mergnutého PR
4. Vygeneruje Playwright testy pomocí Claude
5. Commitne {TICKET-ID}.spec.ts do E2ETests/
6. Napíše do Jiry instrukci ke spuštění pipeline
7. Přepne na Ready to test + přiřadí zpět na testera
"""

from __future__ import annotations

import json
import logging
import asyncio
import anthropic
from typing import Optional
from dataclasses import dataclass

from core.config import cfg
from core.billing import record_cost
from integrations.jira.client import JiraClient
from integrations.bitbucket.client import BitbucketClient

logger = logging.getLogger(__name__)

E2E_FOLDER = "E2ETests"

# Playwright system prompt — přizpůsobený pro Byte
PLAYWRIGHT_SYSTEM_PROMPT = """Jsi expert na Playwright TypeScript testy.
Generuješ E2E testy pro webové aplikace (Angular, PHP).

Pravidla:
- Piš TypeScript s @playwright/test importy
- Používej process.env.BASE_URL jako base URL
- Každý test case pojmenuj: {TicketID}_{co_testuji}_{očekávaný_výsledek}
- Pokrývej: happy path, edge cases, error states
- Používej data-testid atributy pokud existují v source kódu, jinak role/text selektory
- Přidej waitForLoadState('networkidle') po navigaci
- Skupiny testů zabal do describe s názvem ticketu
- Nepoužívej page.waitForTimeout() — používej explicit waits
- Odpovídej POUZE kódem, bez vysvětlení, bez markdown backticks
"""


@dataclass
class TesterResult:
    success: bool
    message: str = ""
    waiting_for_setup: bool = False  # True = čeká na odpověď o URL


class ByteTester:
    """
    Byte jako E2E tester — generuje Playwright testy z AK a PR diffu.
    """

    def __init__(self):
        self._jira = JiraClient()
        self._bb = BitbucketClient()
        self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

    # -------------------------------------------------------------------------
    # Hlavní vstupní bod
    # -------------------------------------------------------------------------

    async def run(self, issue_key: str) -> TesterResult:
        """
        Kompletní E2E testovací cyklus pro daný ticket.
        Volá se když ticket přejde do IN TESTING s Bytem jako assignee.
        """
        logger.info(f"[Tester] Spouštím E2E testovací cyklus pro {issue_key}")

        ticket_ctx = await self._jira.get_ticket_context(issue_key)
        if not ticket_ctx:
            return TesterResult(False, message=f"Nepodařilo se načíst ticket {issue_key}")

        repo_slug = ticket_ctx.get("repo_slug", "")
        if not repo_slug:
            await self._jira.add_comment(
                issue_key,
                "❌ Ticket nemá nastavenou **Komponentu** → nevím na jakém repozitáři pracovat.\n"
                "Nastav komponentu = přesný název BB repozitáře."
            )
            return TesterResult(False, message="Chybí repo_slug")

        # 1. Zkontroluj jestli E2ETests/ existuje
        e2e_exists = await self._check_e2e_folder(repo_slug)

        if not e2e_exists:
            # Zeptej se na URL a čekej na potvrzení
            await self._ask_for_e2e_setup(issue_key, repo_slug, ticket_ctx)
            return TesterResult(True, waiting_for_setup=True,
                               message="Čeká na potvrzení vytvoření E2ETests složky")

        # 2. E2ETests existuje → načti konfiguraci
        e2e_config = await self._load_e2e_config(repo_slug)
        if not e2e_config:
            await self._jira.add_comment(
                issue_key,
                "❌ Složka `E2ETests/` existuje ale `e2e.config.json` chybí nebo je nevalidní.\n"
                "Zkontroluj soubor `E2ETests/e2e.config.json` v repozitáři."
            )
            return TesterResult(False, message="Chybí nebo nevalidní e2e.config.json")

        # 3. Načti AK z ticketu
        ac_text = ticket_ctx.get("description", "")
        if not ac_text:
            await self._jira.add_comment(
                issue_key,
                "❌ Ticket nemá popis nebo acceptance kritéria. Doplň je a přesuň ticket zpět na IN TESTING."
            )
            return TesterResult(False, message="Chybí AK/popis")

        # 4. Načti PR diff z mergnutého PR
        pr_diff, source_files = await self._get_merged_pr_diff(issue_key, repo_slug)
        if not pr_diff:
            logger.warning(f"[Tester] {issue_key} — PR diff nenačten, generuji jen z AK")

        # 5. Zjisti URL prostředí
        component_name = (ticket_ctx.get("components") or [None])[0]
        dev_url = self._resolve_url(e2e_config, component_name)

        logger.info(f"[Tester] {issue_key} | repo: {repo_slug} | url: {dev_url} | source files: {len(source_files)}")

        # 6. Vygeneruj Playwright testy
        test_code = await self._generate_tests(
            issue_key=issue_key,
            summary=ticket_ctx.get("summary", ""),
            ac_text=ac_text,
            dev_url=dev_url,
            source_files=source_files,
        )

        if not test_code:
            await self._jira.add_comment(issue_key, "❌ Generování Playwright testů selhalo.")
            return TesterResult(False, message="Generování testů selhalo")

        # 7. Commitni test soubor — nová branch e2e/{TICKET-ID}
        branch_name = f"e2e/{issue_key.upper()}"
        test_file_path = f"{E2E_FOLDER}/{issue_key}.spec.ts"

        # Vytvoř branch z masteru
        main_branch = await self._get_main_branch(repo_slug)
        branch_ok = await self._bb.create_branch(repo_slug, branch_name, main_branch)
        if not branch_ok:
            # Branch možná existuje — pokračuj
            logger.warning(f"[Tester] Branch {branch_name} se nepodařilo vytvořit, zkusím commitnout na existující")

        commit_ok = await self._bb.commit_files(
            repo_slug=repo_slug,
            branch=branch_name,
            files={test_file_path: test_code},
            message=f"{issue_key}: Playwright E2E testy",
        )

        if not commit_ok:
            await self._jira.add_comment(
                issue_key,
                f"❌ Nepodařilo se commitnout Playwright testy do `{test_file_path}`.\n"
                f"Zkontroluj přístupy Byte k repozitáři `{repo_slug}`."
            )
            return TesterResult(False, message="Commit testů selhal")

        # 8. Napíše do Jiry instrukci
        previous_assignee = ticket_ctx.get("previous_assignee") or {}
        tester_name = previous_assignee.get("display_name", "tester")
        tester_account_id = previous_assignee.get("account_id")

        await self._jira.add_comment(
            issue_key,
            f"✅ Playwright testy vygenerovány.\n\n"
            f"**Soubor:** `{test_file_path}`\n"
            f"**Branch:** `{branch_name}`\n\n"
            f"Spusť BB pipeline na branchi `{branch_name}` — testy proběhnou proti `{dev_url}`.\n\n"
            f"Pokud selžou kvůli selektorům, uprav je ručně a pushni na stejnou branch.\n\n"
            f"**Reviewer:** {tester_name}"
        )

        # 9. Přepni stav a přiřaď zpět na testera
        await self._jira.transition(issue_key, "Ready to test")
        if tester_account_id:
            await self._jira.assign(issue_key, tester_account_id)
            logger.info(f"[Tester] {issue_key} přiřazen zpět na {tester_name}")

        # Billing
        await record_cost(issue_key, response.usage.input_tokens, response.usage.output_tokens, "byte")

        logger.info(f"[Tester] {issue_key} dokončeno — testy v {test_file_path}")
        return TesterResult(True, message=f"Testy commitnuty do {test_file_path}")

    # -------------------------------------------------------------------------
    # Setup E2ETests složky
    # -------------------------------------------------------------------------

    async def setup_e2e_folder(self, issue_key: str, comment_text: str) -> TesterResult:
        """
        Vytvoří E2ETests/ složku po potvrzení od uživatele.
        Volá se když komentář obsahuje '@Byte vytvoř E2ETests'.
        Pokud komentář obsahuje URL, použije je. Jinak se zeptá.
        """
        ticket_ctx = await self._jira.get_ticket_context(issue_key)
        if not ticket_ctx:
            return TesterResult(False, message="Nepodařilo se načíst ticket")

        repo_slug = ticket_ctx.get("repo_slug", "")
        component_name = (ticket_ctx.get("components") or [None])[0] or repo_slug

        # Parsuj URL z komentáře
        urls = self._parse_urls_from_comment(comment_text)

        if not urls.get("dev"):
            # Nemáme URL — znovu se zeptej
            await self._jira.add_comment(
                issue_key,
                "Nevidím URL v tvé odpovědi. Odpověz ve formátu:\n\n"
                "```\n"
                "dev: https://dev.projekt.cz\n"
                "test: https://test.projekt.cz\n"
                "prod: https://projekt.cz\n"
                "@Byte vytvoř E2ETests\n"
                "```"
            )
            return TesterResult(True, waiting_for_setup=True, message="Čeká na URL")

        # Vytvoř E2ETests složku se všemi soubory
        files = self._generate_e2e_scaffold(repo_slug, component_name, urls)

        main_branch = await self._get_main_branch(repo_slug)
        branch_name = f"e2e/setup-{repo_slug}"
        await self._bb.create_branch(repo_slug, branch_name, main_branch)

        commit_ok = await self._bb.commit_files(
            repo_slug=repo_slug,
            branch=branch_name,
            files=files,
            message=f"setup: E2ETests složka pro {repo_slug}",
        )

        if commit_ok:
            await self._jira.add_comment(
                issue_key,
                f"✅ Složka `E2ETests/` vytvořena na branchi `{branch_name}`.\n\n"
                f"Obsahuje: `e2e.config.json`, `playwright.config.js`, `package.json`, "
                f"`global-setup.js`, `bitbucket-pipelines.yml`.\n\n"
                f"Mergni branch do masteru a pak přesuň ticket zpět na **IN TESTING** — "
                f"vygeneruji Playwright testy."
            )
            return TesterResult(True, message="E2ETests složka vytvořena")
        else:
            await self._jira.add_comment(issue_key, "❌ Nepodařilo se vytvořit E2ETests složku.")
            return TesterResult(False, message="Commit selhal")

    def _generate_e2e_scaffold(self, repo_slug: str, component_name: str, urls: dict) -> dict[str, str]:
        """Generuje obsah všech souborů E2ETests složky."""

        e2e_config = {
            "project": repo_slug,
            "default_component": component_name,
            "auth_url": "/pa",
            "components": {
                component_name: {
                    "folder": component_name.lower().replace(" ", "-"),
                    "urls": {k: v for k, v in urls.items() if v}
                }
            }
        }

        playwright_config = """const { defineConfig, devices } = require('@playwright/test');

module.exports = defineConfig({
  testDir: '.',
  testMatch: '**/*.spec.ts',
  globalSetup: './global-setup.js',
  timeout: 30000,
  retries: 1,
  reporter: [['html', { open: 'never' }], ['list']],
  use: {
    baseURL: process.env.BASE_URL || 'http://localhost:4200',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    storageState: 'auth.json',
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile', use: { ...devices['Pixel 5'] } },
  ],
});
"""

        global_setup = """const { chromium } = require('@playwright/test');
const fs = require('fs');

async function globalSetup() {
  const baseURL = process.env.BASE_URL || 'http://localhost:4200';
  const username = process.env.DEV_USER || '';
  const password = process.env.DEV_PASSWORD || '';

  let authPath = '/pa';
  try {
    const config = require('./e2e.config.json');
    if (config.auth_url !== undefined) authPath = config.auth_url;
  } catch (e) {}

  if (!authPath) {
    console.log('[Setup] auth_url is null/false, skipping auth');
    fs.writeFileSync('auth.json', JSON.stringify({ cookies: [], origins: [] }));
    return;
  }

  if (!username || !password) {
    console.log('[Setup] DEV_USER/DEV_PASSWORD not set, skipping auth');
    fs.writeFileSync('auth.json', JSON.stringify({ cookies: [], origins: [] }));
    return;
  }

  const loginURL = baseURL.replace(/\\/$/, '') + authPath;
  const browser = await chromium.launch();
  const page = await browser.newPage();

  console.log(`[Setup] Navigating to login: ${loginURL}`);
  await page.goto(loginURL);
  await page.waitForLoadState('networkidle');

  const hasLogin = await page.locator('#UserName').count() > 0;
  if (!hasLogin) {
    console.log('[Setup] No login form found, skipping auth');
    await page.context().storageState({ path: 'auth.json' });
    await browser.close();
    return;
  }

  await page.locator('#UserName').fill(username);
  await page.locator('#Password').fill(password);
  await page.locator('input[type="submit"]').click();
  await page.waitForLoadState('networkidle');

  await page.context().storageState({ path: 'auth.json' });
  console.log('[Setup] Auth saved to auth.json');
  await browser.close();
}

module.exports = globalSetup;
"""

        package_json = json.dumps({
            "name": "e2e-tests",
            "version": "1.0.0",
            "description": f"Playwright E2E testy pro {repo_slug}",
            "scripts": {
                "test": "playwright test",
                "test:report": "playwright test --reporter=html"
            },
            "devDependencies": {
                "@playwright/test": "^1.44.0"
            }
        }, indent=2, ensure_ascii=False)

        pipelines = """image: mcr.microsoft.com/playwright:v1.59.1-noble

pipelines:
  branches:
    main:
      - step:
          name: Playwright E2E Tests
          caches:
            - node
          script:
            - npm install
            - export BASE_URL=$(node -e "const c=require('./e2e.config.json'); const comp=c.components?Object.values(c.components)[0]:null; const urls=comp?.urls||c.urls||{}; console.log(urls.dev||urls.test||urls.prod||'http://localhost:4200')")
            - echo "Testing against $BASE_URL"
            - BASE_URL=$BASE_URL DEV_USER=$DEV_USER DEV_PASSWORD=$DEV_PASSWORD npx playwright test --reporter=html
          artifacts:
            - playwright-report/**
    master:
      - step:
          name: Playwright E2E Tests
          caches:
            - node
          script:
            - npm install
            - export BASE_URL=$(node -e "const c=require('./e2e.config.json'); const comp=c.components?Object.values(c.components)[0]:null; const urls=comp?.urls||c.urls||{}; console.log(urls.dev||urls.test||urls.prod||'http://localhost:4200')")
            - echo "Testing against $BASE_URL"
            - BASE_URL=$BASE_URL DEV_USER=$DEV_USER DEV_PASSWORD=$DEV_PASSWORD npx playwright test --reporter=html
          artifacts:
            - playwright-report/**
"""

        return {
            f"{E2E_FOLDER}/e2e.config.json": json.dumps(e2e_config, indent=2, ensure_ascii=False),
            f"{E2E_FOLDER}/playwright.config.js": playwright_config,
            f"{E2E_FOLDER}/global-setup.js": global_setup,
            f"{E2E_FOLDER}/package.json": package_json,
            f"{E2E_FOLDER}/bitbucket-pipelines.yml": pipelines,
        }

    # -------------------------------------------------------------------------
    # Generování testů
    # -------------------------------------------------------------------------

    async def _generate_tests(
        self,
        issue_key: str,
        summary: str,
        ac_text: str,
        dev_url: str,
        source_files: list[tuple[str, str]],
    ) -> Optional[str]:
        """Generuje Playwright TypeScript testy pomocí Claude."""

        source_context = ""
        if source_files:
            parts = []
            for filepath, file_content in source_files:
                parts.append(f"=== {filepath} ===\n{file_content[:3000]}")
            source_context = (
                "\n\nZměněné zdrojové soubory z PR (hledej v nich reálné selektory — "
                "data-testid atributy, HTML IDs, Angular komponenty, router linky):\n\n"
                + "\n\n".join(parts)
            )

        user_prompt = (
            f"Vygeneruj Playwright TypeScript testy pro Jira ticket {issue_key}: {summary}\n\n"
            f"Base URL: použij process.env.BASE_URL || '{dev_url}'\n\n"
            f"Acceptance kritéria / popis:\n{ac_text}"
            f"{source_context}"
        )

        model_cfg = cfg.agent("byte").model
        response = self._client.messages.create(
            model=model_cfg.model,
            max_tokens=4096,
            system=PLAYWRIGHT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw = response.content[0].text.strip()

        # Odstraň markdown backticks pokud jsou
        if raw.startswith("```"):
            lines = raw.split("\n")[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            raw = "\n".join(lines)

        # Najdi začátek importů
        if "import" in raw and not raw.startswith("import"):
            raw = raw[raw.index("import"):]

        logger.info(
            f"[Tester] Testy vygenerovány | "
            f"tokeny: {response.usage.input_tokens}+{response.usage.output_tokens} | "
            f"délka: {len(raw)} znaků"
        )
        return raw

    # -------------------------------------------------------------------------
    # BB helpers
    # -------------------------------------------------------------------------

    async def _check_e2e_folder(self, repo_slug: str) -> bool:
        """Zkontroluje jestli E2ETests/ složka existuje v repozitáři."""
        config = await self._bb.get_file(repo_slug, f"{E2E_FOLDER}/e2e.config.json")
        return config is not None

    async def _load_e2e_config(self, repo_slug: str) -> Optional[dict]:
        """Načte a parsuje e2e.config.json z repozitáře."""
        content = await self._bb.get_file(repo_slug, f"{E2E_FOLDER}/e2e.config.json")
        if not content:
            return None
        try:
            return json.loads(content)
        except Exception:
            return None

    async def _get_merged_pr_diff(
        self, issue_key: str, repo_slug: str
    ) -> tuple[str, list[tuple[str, str]]]:
        """
        Načte diff a zdrojové soubory z mergnutého PR pro daný ticket.
        Používá Jira dev-status API stejně jako původní e2e agent.
        """
        import httpx

        jira_cfg = cfg.agent("byte").jira
        auth = (jira_cfg.email, jira_cfg.api_token)

        # Nejdřív získej Jira issue ID
        ticket = await self._jira.get_ticket(issue_key)
        if not ticket:
            return "", []
        issue_id = ticket.get("id", "")

        # Jira dev-status API — najdi linked PR
        jira_base = jira_cfg.base_url.rstrip("/")
        url = f"{jira_base}/rest/dev-status/1.0/issue/detail"
        params = {
            "issueId": issue_id,
            "applicationType": "bitbucket",
            "dataType": "pullrequest"
        }

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, params=params, auth=auth, timeout=15)
                if not resp.is_success:
                    logger.warning(f"[Tester] dev-status API chyba: {resp.status_code}")
                    return "", []

                data = resp.json()
                prs = data.get("detail", [{}])[0].get("pullRequests", [])
                if not prs:
                    logger.info(f"[Tester] {issue_key} — žádné linked PR")
                    return "", []

                # Preferuj mergnuté PR
                merged = [pr for pr in prs if pr.get("status") == "MERGED"]
                pr = max(merged or prs, key=lambda p: p.get("lastUpdate", ""))
                pr_repo = pr.get("repositoryName", "").split("/")[-1]
                pr_id = int(pr.get("id", 0))

                if not pr_id:
                    return "", []

                logger.info(f"[Tester] Linked PR #{pr_id} v repo: {pr_repo}")

        except Exception as e:
            logger.warning(f"[Tester] dev-status chyba: {e}")
            return "", []

        # Načti zdrojové soubory z PR diffstat
        try:
            token = await self._bb._get_token()
            bb_api = "https://api.bitbucket.org/2.0"
            workspace = self._bb._workspace

            async with httpx.AsyncClient() as client:
                diff_resp = await client.get(
                    f"{bb_api}/repositories/{workspace}/{pr_repo}/pullrequests/{pr_id}/diffstat",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=15,
                    follow_redirects=True,
                )
                if not diff_resp.is_success:
                    return "", []

                files = diff_resp.json().get("values", [])
                logger.info(f"[Tester] PR #{pr_id} změnil {len(files)} souborů")

                source_files = []
                for f in files:
                    filepath = (f.get("new") or f.get("old") or {}).get("path", "")
                    if not filepath:
                        continue
                    ext = filepath.split(".")[-1].lower()
                    # Načti jen relevantní soubory (Angular: .ts, .html; PHP: .php; .NET: .cs)
                    if ext not in ("ts", "html", "php", "cs") or filepath.endswith(".spec.ts"):
                        continue
                    src_resp = await client.get(
                        f"{bb_api}/repositories/{workspace}/{pr_repo}/src/HEAD/{filepath}",
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=15,
                        follow_redirects=True,
                    )
                    if src_resp.is_success:
                        source_files.append((filepath, src_resp.text[:4000]))

                # Vrať také diff string (pro kontext)
                raw_diff = await self._bb.get_byte_pr_diff(pr_repo, issue_key)
                return raw_diff, source_files

        except Exception as e:
            logger.warning(f"[Tester] Chyba při načítání PR diff: {e}")
            return "", []

    async def _get_main_branch(self, repo_slug: str) -> str:
        """Zjistí hlavní branch repozitáře."""
        import httpx
        token = await self._bb._get_token()
        url = f"https://api.bitbucket.org/2.0/repositories/{self._bb._workspace}/{repo_slug}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url, headers={"Authorization": f"Bearer {token}"}, timeout=10
            )
            if resp.is_success:
                return resp.json().get("mainbranch", {}).get("name", "master")
        return "master"

    # -------------------------------------------------------------------------
    # Parsování a URL helpers
    # -------------------------------------------------------------------------

    def _resolve_url(self, e2e_config: dict, component_name: Optional[str]) -> str:
        """Zjistí DEV URL z e2e.config.json pro danou komponentu."""
        components = e2e_config.get("components", {})

        # Zkus najít komponentu podle názvu
        comp_config = None
        if component_name and component_name in components:
            comp_config = components[component_name]
        elif components:
            # Fallback — použij default_component nebo první
            default = e2e_config.get("default_component", "")
            comp_config = components.get(default) or list(components.values())[0]

        if comp_config:
            urls = comp_config.get("urls", {})
        else:
            urls = e2e_config.get("urls", {})

        return (urls.get("dev") or urls.get("test") or urls.get("prod") or "http://localhost:4200")

    def _parse_urls_from_comment(self, comment_text: str) -> dict:
        """Parsuje URL z komentáře ve formátu 'dev: https://...'"""
        import re
        urls = {}
        for env in ("dev", "test", "prod"):
            match = re.search(rf"{env}:\s*(https?://\S+)", comment_text, re.IGNORECASE)
            if match:
                urls[env] = match.group(1).rstrip(".,;)")
        return urls

    async def _ask_for_e2e_setup(
        self, issue_key: str, repo_slug: str, ticket_ctx: dict
    ):
        """Zeptá se na URL prostředí a instrukce pro vytvoření E2ETests složky."""
        component = (ticket_ctx.get("components") or [repo_slug])[0]
        await self._jira.add_comment(
            issue_key,
            f"Složka `E2ETests/` v repozitáři `{repo_slug}` neexistuje.\n\n"
            f"Mám ji vytvořit? Potřebuji znát URL prostředí pro komponentu `{component}`.\n\n"
            f"Odpověz ve formátu a přidej potvrzení:\n\n"
            f"```\n"
            f"dev: https://dev.{repo_slug}.cz\n"
            f"test: https://test.{repo_slug}.cz\n"
            f"prod: https://{repo_slug}.cz\n"
            f"@Byte vytvoř E2ETests\n"
            f"```\n\n"
            f"Pokud projekt nepotřebuje přihlášení, přidej také: `auth_url: null`"
        )
