# AI Agents — Firemní ekosystém

Monorepo pro všechny AI agenty. Každý agent má vlastní složku s osobností, konfigurací a testy.

## Agenti

| Agent | Role | Stav |
|-------|------|------|
| **Byte** | Senior developer + QA tester | 🟡 Ve vývoji |
| **Atlas** | Analytik | 📋 Plánováno |
| **Lucy** | Zákaznická podpora | 📋 Plánováno |

## Struktura repozitáře

```
agents/
  byte/           ← Byte: senior dev + QA
  atlas/          ← Atlas: analytik (budoucnost)
  lucy/           ← Lucy: podpora (budoucnost)

core/             ← Sdílená logika (API klient, paměť, util)
integrations/
  jira/           ← Jira webhook + REST API
  bitbucket/      ← Bitbucket API (PR, komentáře)
  teams/          ← MS Teams notifikace (budoucnost)

skills/           ← gstack SKILL.md role definice
config/           ← Konfigurace prostředí
docs/             ← Architektura, rozhodnutí, workflow
scripts/          ← Deployment, setup skripty
tests/            ← Integrační testy
```

## Rychlý start

```bash
cp config/.env.example config/.env
# Vyplň ANTHROPIC_API_KEY, JIRA_TOKEN, BITBUCKET_TOKEN
pip install -r requirements.txt
python -m core.server
```

## Přidání nového agenta

1. Vytvoř složku `agents/<jmeno>/`
2. Zkopíruj `agents/byte/` jako šablonu
3. Uprav `persona.md` a `config.yaml`
4. Zaregistruj agenta v `core/registry.py`
