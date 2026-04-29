# Railway — nasazení Byte agenta

## ENV Variables (Railway → Service → Variables)

Zkopíruj přesně tyto názvy:

```
ANTHROPIC_API_KEY              sk-ant-...
BYTE_JIRA_API_TOKEN            ATATT3x...
BYTE_BB_OAUTH_CLIENT_ID        ...
BYTE_BB_OAUTH_CLIENT_SECRET    ...
BYTE_RAILWAY_URL               https://byte-agent.up.railway.app
FORGE_SHARED_SECRET            <náhodný hex — viz níže>
```

### Generování FORGE_SHARED_SECRET
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```
Stejnou hodnotu nastav i ve Forge CLI:
```bash
forge variables set FORGE_SHARED_SECRET <hodnota>
```

## Struktura souborů v Railway service

Railway očekává v kořeni repozitáře:
- requirements.txt  ← Python závislosti
- Procfile          ← start příkaz
- railway.json      ← konfigurace

## Postup nasazení

1. Propoj byte-agent BB repozitář s Railway service
   Railway Dashboard → New Service → GitHub/Bitbucket repo

2. Nastav ENV Variables (viz výše)

3. Railway automaticky deployuje při každém push na main branch

4. Ověř nasazení:
   https://byte-agent.up.railway.app/health

   Odpověď by měla být:
   {
     "status": "ok",
     "agents": ["byte"],
     "jira": "ok",
     "bitbucket": "ok",
     "model": "claude-sonnet-4-6"
   }

## ai-personas ve stejném service

Railway potřebuje přístup k ai-personas souborům (SOUL.md, PERSONA.md, skills/).
Možnosti:
  A) Přidej ai-personas jako git submodule do byte-agent repozitáře (doporučeno)
  B) Byte načítá personas za běhu z BB API (pomalejší, ale flexibilnější)

### Varianta A — git submodule (doporučeno pro start)
```bash
cd byte-agent
git submodule add https://bitbucket.org/netdirect-custom-solution/ai-personas.git ai-personas
git commit -m "add ai-personas submodule"
```

### Varianta B — načítání z BB API
Přidej do core/agent.py načítání přes BitbucketClient.get_file()
místo čtení z lokálního filesystemu.
