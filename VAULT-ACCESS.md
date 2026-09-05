# Credential access for agents working in this project

**Do not ask the user to paste API keys, and do not create `.env` files.**
Every credential this project uses already lives in the NOBILITY DEPOSITORY
vault. Read it through the bridge.

## Where it is

| What | Path |
|---|---|
| Vault file (single source of truth) | `~/.config/nobility-depository/vault.json` |
| Python bridge | `/home/hunt/vault.py` |
| Live status endpoint | `GET http://127.0.0.1:8000/api/vault/status` |
| Force re-read | `POST http://127.0.0.1:8000/api/vault/reload` |
| Dated backups | `~/.config/nobility-depository/backups/` |

The vault is managed by the Electron app at
`~/Downloads/family Reunion/vault-app` (the BIG MA version — this is the
current one). A second, older copy exists at `~/NobilityDepository`; prefer
the BIG MA app.

## How to read a credential

```python
from vault import vault
token = vault.require("OPENAI_API_KEY")   # raises if absent
key   = vault.get("GEMINI_API_KEY")       # returns None if absent
env   = vault.export_env(["XAI_API_KEY"]) # dict, for subprocess env=
```

`vault.load()` re-reads automatically when the file's mtime changes, so a key
added in the Depository app is visible immediately — no server restart.

## Rules

1. **Never send credential values to the browser or any client.** The status
   endpoint is redacted by design: names, categories and verification state
   only. Keep it that way.
2. **Never write values into source, logs, commit messages, or command
   arguments.** They are readable in `ps` and shell history.
3. **Never edit `vault.json` directly.** The Electron app owns it and
   rewrites the whole file on save; a direct write can be clobbered. Add
   credentials through the app.
4. **Verify before use, and report what came back.** A 200 alone is not
   "connected" — a read-only or wrong-project key authenticates fine and then
   fails at call time. `vault.status_report()` does this per provider.

## Entry names — check both spellings

Two apps wrote this vault with different conventions, so aliases exist.
`vault.py` resolves them; if you read the file yourself, check both:

| Canonical | Alias also present |
|---|---|
| `HUGGINGFACE_TOKEN` | `HF_TOKEN` (different values — verify each) |
| `NVIDIA_NIM_API_KEY` | `NVIDIA_NIM_KEY` |
| `GOOGLE_CLOUD_API_KEY` | `GOOGLE_CLOUD_KEY` |
| `NVIDIA_ENTERPRISE_KEY` | `NVIDIA_ENTERPRISE_LICENSE` |

Single-name entries: `OPENAI_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY`,
`NGC_API_KEY`, `GITHUB_TOKEN`, `AWS_*`.

## Known state (2026-09-04)

Verifying: OpenAI, NVIDIA NIM, NGC, Gemini.
Rejected by provider: Hugging Face (401 — token needs regenerating),
Google Cloud (403 — likely the TTS API is not enabled on that project),
xAI Grok (403).

## Security note

The vault is plaintext on disk at mode `600`. Fine for local single-user
work; it must never be copied into a repo, a container image, or any
deployed environment. Two live keys are still hardcoded in
`~/NobilityDepository/src/renderer/renderer.js` and in that repo's git
history — they are exposed and should be rotated before that repo is
published anywhere.
