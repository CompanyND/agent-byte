"""
integrations/bitbucket/client.py

OAuth2 client — přesně jako v main.py, jen čte credentials z cfg.
Token se cachuje a automaticky obnovuje před expirací.
"""

from __future__ import annotations

import re
import time
import fnmatch
import asyncio
import httpx
import logging
from typing import Optional

from core.config import cfg

logger = logging.getLogger(__name__)

BB_API = "https://api.bitbucket.org/2.0"
BB_OAUTH = "https://bitbucket.org/site/oauth2/access_token"


class BitbucketClient:
    """
    Bitbucket API klient pro Byte.
    Jeden instance per agent — drží OAuth token cache.
    """

    def __init__(self, agent_slug: str = "byte"):
        self._agent_cfg = cfg.agent(agent_slug).bitbucket
        self._workspace = self._agent_cfg.workspace
        self._token_cache: dict = {"token": None, "expires_at": 0.0}

    # -------------------------------------------------------------------------
    # OAuth2
    # -------------------------------------------------------------------------

    async def _get_token(self) -> str:
        """Vrátí platný OAuth2 access token, obnoví pokud expiruje."""
        if self._token_cache["token"] and time.time() < self._token_cache["expires_at"] - 60:
            return self._token_cache["token"]

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                BB_OAUTH,
                data={"grant_type": "client_credentials"},
                auth=(
                    self._agent_cfg.oauth_client_id,
                    self._agent_cfg.oauth_client_secret,
                ),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            self._token_cache["token"] = data["access_token"]
            self._token_cache["expires_at"] = time.time() + data.get("expires_in", 7200)
            logger.info(f"[BB OAuth] Nový token získán, platný {data.get('expires_in', 7200)}s")
            return self._token_cache["token"]

    def _headers(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    # -------------------------------------------------------------------------
    # Čtení repozitáře
    # -------------------------------------------------------------------------

    async def get_file(self, repo_slug: str, path: str) -> Optional[str]:
        """Stáhne obsah souboru z HEAD branché."""
        token = await self._get_token()
        url = f"{BB_API}/repositories/{self._workspace}/{repo_slug}/src/HEAD/{path}"
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url, headers=self._headers(token), timeout=10)
            if resp.is_success:
                return resp.text
            return None

    async def get_json_file(self, repo_slug: str, path: str) -> Optional[dict]:
        """Stáhne a parsuje JSON soubor z repozitáře."""
        content = await self.get_file(repo_slug, path)
        if not content:
            return None
        try:
            import json
            return json.loads(content)
        except Exception:
            return None

    async def list_dir(self, repo_slug: str, path: str = "") -> list[dict]:
        """Vrátí všechny soubory v dané cestě — se stránkováním."""
        token = await self._get_token()
        all_values = []
        url = f"{BB_API}/repositories/{self._workspace}/{repo_slug}/src/HEAD/{path}?pagelen=100"
        async with httpx.AsyncClient(follow_redirects=True) as client:
            while url:
                resp = await client.get(url, headers=self._headers(token), timeout=10)
                if not resp.is_success:
                    break
                data = resp.json()
                all_values.extend(data.get("values", []))
                url = data.get("next")
        return all_values

    async def get_diff(self, diff_url: str) -> str:
        """Stáhne diff PR."""
        token = await self._get_token()
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(diff_url, headers=self._headers(token), timeout=30)
            resp.raise_for_status()
            return resp.text

    # -------------------------------------------------------------------------
    # Stack detekce — přesně z main.py
    # -------------------------------------------------------------------------

    async def detect_angular_version(self, repo_slug: str) -> Optional[str]:
        """
        Detekuje verzi Angularu z package.json.
        Prohledá root i podsložky. Fallback na "6" pokud @angular/core nenajde.
        """
        token = await self._get_token()
        async with httpx.AsyncClient(follow_redirects=True) as client:
            try:
                candidates = ["package.json"]
                root_files = await self.list_dir(repo_slug)
                for f in root_files:
                    if f.get("type") == "commit_directory":
                        candidates.append(f"{f['path']}/package.json")

                seen_versions = set()
                for path in candidates:
                    url = f"{BB_API}/repositories/{self._workspace}/{repo_slug}/src/HEAD/{path}"
                    resp = await client.get(url, headers=self._headers(token), timeout=10)
                    if not resp.is_success:
                        continue
                    try:
                        pkg = resp.json()
                    except Exception:
                        continue
                    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                    version = deps.get("@angular/core", "")
                    if not version:
                        continue
                    match = re.search(r"(\d+)", version)
                    if match:
                        seen_versions.add(match.group(1))

                if not seen_versions:
                    return None  # není Angular projekt
                return str(max(int(v) for v in seen_versions))
            except Exception:
                return None

    async def detect_dotnet_version(self, repo_slug: str) -> Optional[str]:
        """Detekuje verzi .NET z .csproj souborů. Majority vote."""
        token = await self._get_token()
        async with httpx.AsyncClient(follow_redirects=True) as client:
            try:
                candidates: list[str] = []
                root_files = await self.list_dir(repo_slug)
                for f in root_files:
                    if f["path"].endswith(".csproj"):
                        candidates.append(f["path"])
                    elif f.get("type") == "commit_directory":
                        sub_files = await self.list_dir(repo_slug, f["path"])
                        for sf in sub_files:
                            if sf["path"].endswith(".csproj"):
                                candidates.append(sf["path"])

                versions = []
                for csproj_path in candidates:
                    url = f"{BB_API}/repositories/{self._workspace}/{repo_slug}/src/HEAD/{csproj_path}"
                    resp = await client.get(url, headers=self._headers(token), timeout=10)
                    if not resp.is_success:
                        continue
                    match = re.search(
                        r"<TargetFramework(?:Version)?>(.*?)</TargetFramework(?:Version)?>",
                        resp.text,
                    )
                    if match:
                        versions.append(match.group(1).strip())

                if not versions:
                    return None
                from collections import Counter
                return Counter(versions).most_common(1)[0][0]
            except Exception:
                return None

    async def detect_php_version(self, repo_slug: str) -> Optional[str]:
        """Detekuje PHP verzi a framework z composer.json."""
        pkg = await self.get_json_file(repo_slug, "composer.json")
        if not pkg:
            return None
        require = pkg.get("require", {})
        php_version = require.get("php", "")
        # Detekuj framework
        frameworks = []
        if "laravel/framework" in require:
            frameworks.append(f"Laravel {require['laravel/framework']}")
        if "symfony/symfony" in require or "symfony/framework-bundle" in require:
            frameworks.append("Symfony")
        if "nette/nette" in require or "nette/application" in require:
            frameworks.append("Nette")
        result = php_version
        if frameworks:
            result += f" ({', '.join(frameworks)})"
        return result or None

    async def detect_stack(self, repo_slug: str) -> dict:
        """
        Detekuje celý stack repozitáře paralelně.
        Vrátí: {"angular": "17", "dotnet": "net8.0", "php": "^8.1 (Laravel ^10.0)"}
        """
        angular, dotnet, php = await asyncio.gather(
            self.detect_angular_version(repo_slug),
            self.detect_dotnet_version(repo_slug),
            self.detect_php_version(repo_slug),
        )
        stack = {}
        if angular:
            stack["angular"] = angular
        if dotnet:
            stack["dotnet"] = dotnet
        if php:
            stack["php"] = php
        return stack

    # -------------------------------------------------------------------------
    # Branch a commity
    # -------------------------------------------------------------------------

    async def create_branch(self, repo_slug: str, branch_name: str, from_branch: str = "main") -> bool:
        """Vytvoří novou branch. Pokud už existuje, použije ji."""
        token = await self._get_token()
        async with httpx.AsyncClient() as client:
            # Zkontroluj jestli branch už existuje
            check_url = f"{BB_API}/repositories/{self._workspace}/{repo_slug}/refs/branches/{branch_name}"
            check_resp = await client.get(check_url, headers=self._headers(token), timeout=10)
            if check_resp.is_success:
                logger.info(f"[BB] Branch '{branch_name}' už existuje v {repo_slug} — použiji ji")
                return True

            # Branch neexistuje — zjisti hash HEAD commitu na zdrojové branchi
            url = f"{BB_API}/repositories/{self._workspace}/{repo_slug}/refs/branches/{from_branch}"
            resp = await client.get(url, headers=self._headers(token), timeout=10)
            if not resp.is_success:
                logger.error(f"[BB] Branch '{from_branch}' nenalezena v {repo_slug}")
                return False
            target_hash = resp.json()["target"]["hash"]

            create_url = f"{BB_API}/repositories/{self._workspace}/{repo_slug}/refs/branches"
            resp2 = await client.post(
                create_url,
                json={"name": branch_name, "target": {"hash": target_hash}},
                headers=self._headers(token),
                timeout=10,
            )
            if resp2.is_success:
                logger.info(f"[BB] Branch '{branch_name}' vytvořena v {repo_slug}")
                return True
            logger.error(f"[BB] Nepodařilo se vytvořit branch: {resp2.text}")
            return False

    async def commit_files(
        self,
        repo_slug: str,
        branch: str,
        files: dict[str, str],  # {path: content}
        message: str,
    ) -> bool:
        """Commitne soubory na branch přes BB API (multipart form)."""
        token = await self._get_token()
        url = f"{BB_API}/repositories/{self._workspace}/{repo_slug}/src"

        form_data = {"message": message, "branch": branch}
        for path, content in files.items():
            form_data[path] = content

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                data=form_data,
                headers=self._headers(token),
                timeout=30,
            )
            if resp.is_success:
                logger.info(f"[BB] Commit na {branch}: {message[:50]}")
                return True
            logger.error(f"[BB] Commit selhal: {resp.status_code} {resp.text[:200]}")
            return False

    # -------------------------------------------------------------------------
    # Pull Requesty
    # -------------------------------------------------------------------------

    async def create_pr(
        self,
        repo_slug: str,
        title: str,
        source_branch: str,
        destination_branch: str,
        description: str,
        reviewer_account_id: str,
    ) -> Optional[dict]:
        """Vytvoří PR s daným reviewerem."""
        token = await self._get_token()
        url = f"{BB_API}/repositories/{self._workspace}/{repo_slug}/pullrequests"

        payload = {
            "title": title,
            "description": description,
            "source": {"branch": {"name": source_branch}},
            "destination": {"branch": {"name": destination_branch}},
            "reviewers": [{"account_id": reviewer_account_id}] if reviewer_account_id else [],
            "close_source_branch": False,
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                json=payload,
                headers=self._headers(token),
                timeout=15,
            )
            if resp.is_success:
                pr = resp.json()
                logger.info(f"[BB] PR #{pr['id']} vytvořen: {title}")
                return pr
            logger.error(f"[BB] PR vytvoření selhalo: {resp.status_code} {resp.text[:200]}")
            return None

    async def add_pr_comment(
        self,
        repo_slug: str,
        pr_id: int,
        content: str,
        file_path: Optional[str] = None,
        line: Optional[int] = None,
    ) -> bool:
        """Přidá komentář k PR — globální nebo inline (file:line)."""
        token = await self._get_token()
        url = f"{BB_API}/repositories/{self._workspace}/{repo_slug}/pullrequests/{pr_id}/comments"

        payload: dict = {"content": {"raw": content}}
        if file_path and line:
            payload["inline"] = {"path": file_path, "to": line}

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                json=payload,
                headers=self._headers(token),
                timeout=15,
            )
            return resp.is_success

    async def get_pr_comments(self, repo_slug: str, pr_id: int) -> list[dict]:
        """Načte všechny komentáře PR."""
        token = await self._get_token()
        url = f"{BB_API}/repositories/{self._workspace}/{repo_slug}/pullrequests/{pr_id}/comments?pagelen=100"
        comments = []
        async with httpx.AsyncClient() as client:
            while url:
                resp = await client.get(url, headers=self._headers(token), timeout=10)
                if not resp.is_success:
                    break
                data = resp.json()
                comments.extend(data.get("values", []))
                url = data.get("next")
        return comments

    # -------------------------------------------------------------------------
    # Uživatelé
    # -------------------------------------------------------------------------

    async def get_user_account_id(self, email: str) -> Optional[str]:
        """Najde Bitbucket account_id podle emailu (potřeba pro reviewery v PR)."""
        token = await self._get_token()
        url = f"{BB_API}/workspaces/{self._workspace}/members"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=self._headers(token), timeout=10)
            if not resp.is_success:
                return None
            for member in resp.json().get("values", []):
                user = member.get("user", {})
                # BB API nevrací emaily přímo — matchujeme přes nickname nebo display name
                if user.get("nickname", "").lower() in email.lower():
                    return user.get("account_id")
        return None

    # -------------------------------------------------------------------------
    # Diff filtrování — přesně z main.py
    # -------------------------------------------------------------------------

    def filter_diff(self, raw_diff: str) -> tuple[str, list[str]]:
        """Odfiltruje ignorované soubory z diffu."""
        ignored_patterns = cfg.byte.ignored_files
        ignored = []
        filtered_blocks = []
        current_block = []
        current_file = None
        skip_current = False

        for line in raw_diff.splitlines(keepends=True):
            if line.startswith("diff --git "):
                if current_block and not skip_current:
                    filtered_blocks.extend(current_block)
                parts = line.strip().split(" ")
                current_file = parts[-1].lstrip("b/") if len(parts) >= 4 else ""
                skip_current = self._should_ignore(current_file, ignored_patterns)
                if skip_current and current_file:
                    ignored.append(current_file)
                current_block = [line]
            else:
                current_block.append(line)

        if current_block and not skip_current:
            filtered_blocks.extend(current_block)

        return "".join(filtered_blocks), ignored

    def _should_ignore(self, filename: str, patterns: list[str]) -> bool:
        for pattern in patterns:
            if filename == pattern:
                return True
            if fnmatch.fnmatch(filename, pattern):
                return True
            basename = filename.split("/")[-1]
            if basename == pattern or fnmatch.fnmatch(basename, pattern):
                return True
        return False

    def count_changed_lines(self, diff: str) -> int:
        count = 0
        for line in diff.splitlines():
            if (line.startswith("+") and not line.startswith("+++")) or \
               (line.startswith("-") and not line.startswith("---")):
                count += 1
        return count

    # -------------------------------------------------------------------------
    # byte-memory repo
    # -------------------------------------------------------------------------

    async def read_memory(self, repo_slug: str, bb_project: str = "") -> tuple[str, str, str]:
        """
        Načte 3 úrovně paměti z byte-memory repo.
        Vrátí (global_memory, project_memory, repo_memory).
        
        repo_slug   = název BB repozitáře (např. "web-frontend")
        bb_project  = název BB projektu (např. "iftech") — prefix repozitáře
        """
        memory_cfg = cfg.byte.memory
        memory_repo = memory_cfg.get("global_repo", "byte-memory")

        # Globální paměť
        global_path = memory_cfg.get("global_path", "global/pamet.md")

        # Projektová paměť — pokud bb_project není zadán, odvoď ho z repo_slug
        if not bb_project and repo_slug:
            bb_project = repo_slug.split("_")[0] if "_" in repo_slug else repo_slug.split("-")[0]

        project_path = memory_cfg.get("project_path", "projects/{bb-project}/pamet.md").replace(
            "{bb-project}", bb_project
        ).replace("{slug}", bb_project)

        # Repozitářová paměť
        repo_path = memory_cfg.get("repo_path", "repos/{repo-slug}/pamet.md").replace(
            "{repo-slug}", repo_slug
        ).replace("{slug}", repo_slug)

        global_mem, project_mem, repo_mem = await asyncio.gather(
            self.get_file(memory_repo, global_path),
            self.get_file(memory_repo, project_path),
            self.get_file(memory_repo, repo_path),
        )

        return (global_mem or ""), (project_mem or ""), (repo_mem or "")

    async def write_memory(self, project_slug: str, path_key: str, content: str, message: str) -> bool:
        """Zapíše do byte-memory repo."""
        memory_cfg = cfg.byte.memory
        memory_repo = memory_cfg.get("global_repo", "byte-memory")
        path = memory_cfg.get(path_key, "").replace("{slug}", project_slug)
        if not path:
            return False
        return await self.commit_files(memory_repo, "main", {path: content}, message)

    async def find_pr_for_ticket(self, repo_slug: str, issue_key: str) -> Optional[dict]:
        """Najde otevřený nebo mergnutý PR pro daný ticket (podle branch name)."""
        token = await self._get_token()
        # Hledáme branch která začíná feat/ nebo bugfix/ a obsahuje issue key
        issue_lower = issue_key.lower()
        for state in ["OPEN", "MERGED"]:
            url = f"{BB_API}/repositories/{self._workspace}/{repo_slug}/pullrequests"
            params = {"state": state, "pagelen": 50}
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, headers=self._headers(token), params=params, timeout=10)
                if not resp.is_success:
                    continue
                for pr in resp.json().get("values", []):
                    branch = pr.get("source", {}).get("branch", {}).get("name", "").lower()
                    if issue_lower in branch:
                        return pr
        return None

    async def get_byte_pr_diff(self, repo_slug: str, issue_key: str) -> str:
        """
        Načte diff PR který Byte vytvořil pro daný ticket.
        Filtruje ignorované soubory a vrátí jen relevantní změny.
        """
        pr = await self.find_pr_for_ticket(repo_slug, issue_key)
        if not pr:
            logger.info(f"[BB] PR pro {issue_key} nenalezen v {repo_slug}")
            return ""

        pr_id = pr.get("id")
        pr_url = pr.get("links", {}).get("html", {}).get("href", "")
        source_branch = pr.get("source", {}).get("branch", {}).get("name", "")
        dest_branch = pr.get("destination", {}).get("branch", {}).get("name", "")

        logger.info(f"[BB] Načítám diff PR #{pr_id} ({source_branch} → {dest_branch})")

        # Načti diff přes BB API
        token = await self._get_token()
        diff_url = f"{BB_API}/repositories/{self._workspace}/{repo_slug}/diff/{source_branch}..{dest_branch}"
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(diff_url, headers=self._headers(token), timeout=30)
            if not resp.is_success:
                logger.warning(f"[BB] Diff pro PR #{pr_id} nenačten: {resp.status_code}")
                return ""

        raw_diff = resp.text
        filtered_diff, ignored = self.filter_diff(raw_diff)
        changed_lines = self.count_changed_lines(filtered_diff)

        if ignored:
            logger.info(f"[BB] Ignorované soubory v diff: {ignored}")

        # Limit — pokud je diff příliš velký, zkrátíme
        max_chars = 80_000
        if len(filtered_diff) > max_chars:
            filtered_diff = filtered_diff[:max_chars] + f"\n\n... (diff zkrácen, celkem {changed_lines} změněných řádků)"

        return f"## PR #{pr_id}: {source_branch} → {dest_branch}\n\n```diff\n{filtered_diff}\n```"

    async def append_log(self, project_slug: str, entry: str) -> bool:
        """Přidá záznam do samo-dokumentačního logu projektu."""
        if not cfg.byte.self_documentation.get("enabled"):
            return True
        memory_repo = cfg.byte.memory.get("global_repo", "byte-memory")
        log_path = cfg.byte.self_documentation.get("log_path", "repos/{repo-slug}/log.md").replace(
            "{repo-slug}", project_slug
        ).replace("{slug}", project_slug)
        existing = await self.get_file(memory_repo, log_path) or ""
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        new_entry = f"\n## {timestamp}\n{entry}\n"
        updated = existing + new_entry
        return await self.commit_files(
            memory_repo, "main",
            {log_path: updated},
            f"log: {entry[:60]}"
        )