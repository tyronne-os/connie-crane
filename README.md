# CONNIE CRANE

Terminal-style IDE and voice foundry for CONNIE, a sovereign voice agent.
FastAPI backend serving a single-page UI, wired directly to the **NOBILITY
DEPOSITORY** credential vault.

## The vault bridge

CONNIE holds no credentials of its own. `vault.py` reads the Nobility
Depository vault from disk at request time, so a key added in the Depository
app is live immediately — no restart, no `.env`, no second copy of a secret.

**Credential values never reach the browser.** `/api/vault/status` returns
names, categories and verification state only; the keys stay in the Python
process. The sidebar panel renders that redacted status.

Verification reports what the provider actually returned, and distinguishes a
rejected credential (401) from a working one that was refused for billing,
quota, a disabled API, or a key restriction (403). A 200 alone is never
reported as "connected" — a read-only token authenticates fine and then fails
at call time.

## Run

```bash
pip install -r requirements.txt
python app.py            # http://127.0.0.1:8000
```

The vault is read from `~/.config/nobility-depository/vault.json`, or from
`NOBILITY_VAULT_PATH` if set.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/vault/status` | Redacted credential status + access manifest |
| `POST` | `/api/vault/reload` | Force a re-read of the vault |
| `GET` | `/api/state` | Cast roster, harvested voices, lexicon |
| `POST` | `/api/agents/new` | Forge a new agent |
| `POST` | `/api/lexicon/add` | Add a vernacular pronunciation |
| `GET` | `/api/harvest/librivox` | Search LibriVox for voice sources |

## Working in this repo

Read [VAULT-ACCESS.md](VAULT-ACCESS.md) before touching credentials. Short
version: never ask for API keys, never create `.env` files, never write
`vault.json` directly — the Electron app owns that file and rewrites it whole
on save.

## Security

The vault is plaintext on disk at mode `600`. That is appropriate for local
single-user work and nothing else: it must never be copied into a repo, a
container image, or a deployed environment.
