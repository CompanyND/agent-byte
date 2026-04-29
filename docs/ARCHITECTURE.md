# Architektura AI agentů

## Přehled

Monorepo s více AI agenty. Každý agent má vlastní identitu, ale sdílejí
společné jádro (orchestrátor, config, API klienti).

## Kde jsou jaké hodnoty

```
V gitu ✅                          Není v gitu ❌
─────────────────────────────      ────────────────────────
config/agents.config.yaml          .env
  - modely (claude-sonnet-4-6)       - ANTHROPIC_API_KEY
  - emaily agentů                    - BYTE_JIRA_API_TOKEN
  - Jira base_url                    - BYTE_BITBUCKET_APP_PASSWORD
  - token_expires (datum)            - ATLAS_JIRA_API_TOKEN
  - allowed_projects                 - JIRA_WEBHOOK_SECRET
  - server nastavení                 - BITBUCKET_WEBHOOK_SECRET
  - enabled: true/false              - LUCY_TEAMS_CLIENT_SECRET
```

**Pravidlo:** Pokud hodnota začíná na `sk-`, `ATB-`, `ATATT`, nebo je to heslo/secret → `.env`.

## Konvence ENV proměnných

```
ANTHROPIC_API_KEY              # jeden klíč pro všechny agenty
{AGENT}_JIRA_API_TOKEN         # např. BYTE_JIRA_API_TOKEN
{AGENT}_BITBUCKET_APP_PASSWORD # např. BYTE_BITBUCKET_APP_PASSWORD
{AGENT}_TEAMS_CLIENT_ID
{AGENT}_TEAMS_CLIENT_SECRET
JIRA_WEBHOOK_SECRET
BITBUCKET_WEBHOOK_SECRET
```

## Struktura repozitáře

```
config/
  agents.config.yaml          ← struktura a ne-tajné hodnoty (v gitu ✅)
  agents.config.example.yaml  ← (v gitu ✅, šablona pro nové agenty)

.env.example                  ← šablona ENV proměnných (v gitu ✅)
.env                          ← reálné tajné hodnoty (není v gitu ❌)

core/
  config.py     ← načte YAML + ENV, poskytne typovaný přístup
  agent.py      ← AgentRunner — persona + skill → Claude API
  registry.py   ← drží instance agentů v paměti
  server.py     ← FastAPI, webhook endpointy

agents/
  byte/
    persona.md    ← identita, tón, pravidla chování
    config.yaml   ← trigger definice (Jira stavy, klíčová slova)
  atlas/          ← budoucnost
  lucy/           ← budoucnost

integrations/
  jira/
    webhook.py    ← přijme Jira POST → AgentEvent
    client.py     ← zapíše komentář Bytovým účtem
  bitbucket/      ← budoucnost
  teams/          ← budoucnost

skills/
  review/SKILL.md   ← gstack: senior developer role
  qa/SKILL.md       ← gstack: QA tester role
```

## Tok dat

```
Jira webhook POST
      ↓
integrations/jira/webhook.py
  - ověří HMAC podpis (JIRA_WEBHOOK_SECRET z ENV)
  - parsuje payload → AgentEvent
      ↓
core/registry.py → get_agent("byte")
      ↓
core/agent.py → AgentRunner.process(event)
  - _should_act()           ← trigger pravidla z agents/byte/config.yaml
  - _build_system_prompt()  ← persona.md + skill SKILL.md
  - Claude API call         ← model z agents.config.yaml, klíč z ENV
      ↓
AgentResponse
      ↓
integrations/jira/client.py → add_comment()
  - Bytův Jira token z ENV (BYTE_JIRA_API_TOKEN)
```

## Přidání nového agenta (Atlas, Lucy, ...)

1. Vytvoř `agents/<slug>/persona.md` a `agents/<slug>/config.yaml`
2. V `config/agents.config.yaml` přidej sekci pod `agents:` — bez tokenů
3. Do `.env` přidej `{SLUG}_JIRA_API_TOKEN` atd.
4. Na hostingu přidej ENV proměnné
5. V `agents.config.yaml` nastav `enabled: true`
6. Restart serveru

## Obnovení Jira tokenu (každý rok)

1. Přihlas se jako agent (byte@firma.cz)
2. Jira → Profile picture → Manage account → Security → API tokens → Create
3. Zkopíruj nový token do `.env` (BYTE_JIRA_API_TOKEN) nebo ENV na hostingu
4. Aktualizuj `token_expires` v `agents.config.yaml`
5. Restart serveru — varování zmizí

## Bezpečnostní pravidla agentů

- Agent **nikdy** nevykonává příkazy z obsahu ticketů (ochrana proti prompt injection)
- Agent **nikdy** nepřistupuje k produkční DB ani nemerge PR
- `allowed_projects` v configu omezuje dosah agenta na konkrétní projekty
- Rate limiting: max 30 akcí/hodinu per agent
