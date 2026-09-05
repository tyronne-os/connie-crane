"""
NOBILITY DEPOSITORY -> CONNIE CRANE credential bridge.

Reads the Nobility Depository vault directly from disk. Credential VALUES stay
in this process and are never serialized to the browser -- the UI only ever
receives names, categories and verification status via status_report().

The Depository (Electron) remains the single source of truth. Nothing here
writes to the vault, so there is no clear/overwrite path to guard and no
export/import backup to maintain on this side.
"""

import json
import os
import stat
import urllib.request
import urllib.error
import urllib.parse

DEFAULT_VAULT_PATH = os.path.expanduser(
    "~/.config/nobility-depository/vault.json"
)
MANIFEST_FILE = os.path.join(os.path.dirname(__file__), "crane_manifest.json")

# What CONNIE actually needs, and what breaks without it.
ACCESS_MANIFEST = [
    {
        "name": "HF_TOKEN",
        "aliases": ["HUGGINGFACE_TOKEN"],
        "label": "Hugging Face",
        "category": "huggingface",
        "scope": "read (inference)",
        "used_for": "TTS / STT inference endpoints for the voice loop",
        "breaks": "CONNIE cannot synthesize or transcribe speech via HF",
        "required": True,
    },
    {
        "name": "NVIDIA_NIM_API_KEY",
        "aliases": ["NVIDIA_NIM_KEY"],
        "label": "NVIDIA NIM",
        "category": "nvidia",
        "scope": "nim inference",
        "used_for": "Low-latency Riva speech synthesis and recognition",
        "breaks": "No low-latency voice path; falls back to HF",
        "required": True,
    },
    {
        "name": "NGC_API_KEY",
        "label": "NVIDIA NGC",
        "category": "nvidia",
        "scope": "ngc catalog",
        "used_for": "Pulling Riva model containers",
        "breaks": "Cannot fetch Riva models",
        "required": False,
    },
    {
        "name": "NVIDIA_ENTERPRISE_KEY",
        "aliases": ["NVIDIA_ENTERPRISE_LICENSE"],
        "label": "NVIDIA Enterprise",
        "category": "other",
        "scope": "enterprise license",
        "used_for": "NVIDIA AI Enterprise entitlement for Riva/NIM at scale",
        "breaks": "Enterprise Riva features unavailable",
        "required": False,
    },
    {
        "name": "GOOGLE_CLOUD_API_KEY",
        "aliases": ["GOOGLE_CLOUD_KEY"],
        "label": "Google Cloud Speech",
        "category": "gcp",
        "scope": "speech-to-text, text-to-speech",
        "used_for": "GCP STT/TTS as the third voice provider",
        "breaks": "No GCP voice path",
        "required": True,
    },
    {
        "name": "GEMINI_API_KEY",
        "label": "Google Gemini",
        "category": "gemini",
        "scope": "generativelanguage",
        "used_for": "Gemini reasoning and multimodal voice responses",
        "breaks": "No Gemini path for CONNIE's responses",
        "required": False,
    },
    {
        "name": "XAI_API_KEY",
        "label": "xAI Grok",
        "category": "grok",
        "scope": "api",
        "used_for": "Grok reasoning backend for CONNIE",
        "breaks": "No Grok path for CONNIE's responses",
        "required": False,
    },
    {
        "name": "OPENAI_API_KEY",
        "label": "OpenAI",
        "category": "openai",
        "scope": "api",
        "used_for": "Realtime / Whisper voice and reasoning calls",
        "breaks": "No OpenAI voice or reasoning path",
        "required": True,
    },
]


class VaultError(RuntimeError):
    pass


class Vault:
    def __init__(self, path=None):
        self.path = path or os.environ.get(
            "NOBILITY_VAULT_PATH", DEFAULT_VAULT_PATH
        )
        self._creds = {}
        self._mtime = None
        self._warnings = []

    # -- loading ---------------------------------------------------------

    def _stale(self):
        try:
            return os.path.getmtime(self.path) != self._mtime
        except OSError:
            return True

    def load(self, force=False):
        """Read the vault, re-reading automatically if it changed on disk.

        Editing a credential in the Depository app therefore takes effect in
        CONNIE without restarting the server.
        """
        if not force and self._mtime is not None and not self._stale():
            return self._creds

        if not os.path.exists(self.path):
            raise VaultError(
                f"Nobility Depository vault not found at {self.path}. "
                "Open the Depository app, or set NOBILITY_VAULT_PATH."
            )

        st = os.stat(self.path)
        self._warnings = []
        if stat.S_IMODE(st.st_mode) & 0o077:
            self._warnings.append(
                f"vault.json is group/world readable "
                f"({oct(stat.S_IMODE(st.st_mode))}); expected 0600"
            )

        try:
            with open(self.path, "r") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise VaultError(f"Could not parse vault: {e}") from e

        creds = {}
        for entry in data.get("entries", []):
            name = entry.get("name")
            if name:
                creds[name] = entry.get("value", "")

        self._creds = creds
        self._mtime = st.st_mtime
        self.saved_at = data.get("savedAt")
        return self._creds

    # -- access ----------------------------------------------------------

    def get(self, name, default=None):
        return self.load().get(name, default)

    def require(self, name):
        val = self.get(name)
        if not val:
            raise VaultError(
                f"{name} is not in the Nobility Depository vault. "
                "Add it in the Depository app, then reload."
            )
        return val

    def names(self):
        return sorted(self.load().keys())

    def export_env(self, names):
        """Return a dict suitable for passing as env to a subprocess."""
        creds = self.load()
        return {n: creds[n] for n in names if creds.get(n)}


# -- verification --------------------------------------------------------
# Never report "connected" on a 200 alone -- report the identity/scope that
# actually came back, so a read-only or wrong-project key is visible now
# rather than at first voice call.


BLOCK_HINTS = (
    "credits", "spending limit", "quota", "billing",
    "has not been used in project", "is disabled",
    "API_KEY_SERVICE_BLOCKED", "blocked", "SERVICE_DISABLED",
)


def _classify_http(code, body):
    """Distinguish a bad credential from a good one that was refused.

    401 means the provider rejected the credential itself.
    403 usually means it authenticated fine and then hit billing, quota, a
    disabled API, or a key restriction -- the key is good, the account or
    project needs attention. Reporting those as "invalid" sends you off
    rotating a working key.
    """
    if code == 401:
        return "invalid", "rejected: credential not accepted"
    if code == 403:
        low = (body or "").lower()
        if any(h.lower() in low for h in BLOCK_HINTS):
            snippet = " ".join((body or "").split())[:160]
            return "blocked", "key OK, call refused: " + snippet
        return "blocked", "key OK, access refused (403)"
    return "invalid", f"provider returned HTTP {code}"


def _http_err(e):
    try:
        body = e.read().decode("utf-8", "replace")
    except Exception:
        body = ""
    return _classify_http(e.code, body)


def _get(url, headers, timeout=6):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8") or "{}")


def _verify_huggingface(token):
    try:
        _s, body = _get(
            "https://huggingface.co/api/whoami-v2",
            {"Authorization": f"Bearer {token}"},
        )
    except urllib.error.HTTPError as e:
        st, msg = _http_err(e)
        return st, f"Hugging Face — {msg}", {}
    except Exception as e:
        return None, f"could not reach Hugging Face: {e}", {}

    auth = body.get("auth", {}) or {}
    access = auth.get("accessToken", {}) or {}
    scopes = access.get("role") or access.get("scopes") or "unknown"
    detail = {
        "identity": body.get("name"),
        "token_name": access.get("displayName"),
        "scopes": scopes,
    }
    if scopes in ("read", "fineGrained", "unknown"):
        return True, f"valid, scope={scopes}", detail
    return True, f"valid, scope={scopes}", detail


def _verify_nvidia(key):
    try:
        _s, body = _get(
            "https://integrate.api.nvidia.com/v1/models",
            {"Authorization": f"Bearer {key}"},
        )
    except urllib.error.HTTPError as e:
        st, msg = _http_err(e)
        return st, f"NVIDIA — {msg}", {}
    except Exception as e:
        return None, f"could not reach NVIDIA: {e}", {}
    models = body.get("data", []) or []
    return True, f"valid, {len(models)} models visible", {
        "models": len(models)
    }


def _verify_openai(key):
    try:
        _s, body = _get(
            "https://api.openai.com/v1/models",
            {"Authorization": f"Bearer {key}"},
        )
    except urllib.error.HTTPError as e:
        st, msg = _http_err(e)
        return st, f"OpenAI — {msg}", {}
    except Exception as e:
        return None, f"could not reach OpenAI: {e}", {}
    models = body.get("data", []) or []
    return True, f"valid, {len(models)} models visible", {
        "models": len(models)
    }


def _verify_gemini(key):
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models?key="
        + urllib.parse.quote(key)
    )
    try:
        _s, body = _get(url, {})
    except urllib.error.HTTPError as e:
        st, msg = _http_err(e)
        return st, f"Google AI — {msg}", {}
    except Exception as e:
        return None, f"could not reach Google AI: {e}", {}
    models = body.get("models", []) or []
    return True, f"valid, {len(models)} models visible", {"models": len(models)}


def _verify_grok(key):
    try:
        _s, body = _get(
            "https://api.x.ai/v1/models",
            {"Authorization": f"Bearer {key}"},
        )
    except urllib.error.HTTPError as e:
        st, msg = _http_err(e)
        return st, f"xAI — {msg}", {}
    except Exception as e:
        return None, f"could not reach xAI: {e}", {}
    models = body.get("data", []) or []
    return True, f"valid, {len(models)} models visible", {"models": len(models)}


def _verify_gcp(key):
    url = (
        "https://texttospeech.googleapis.com/v1/voices?key="
        + urllib.parse.quote(key)
    )
    try:
        _s, body = _get(url, {})
    except urllib.error.HTTPError as e:
        st, msg = _http_err(e)
        return st, f"Google — {msg}", {}
    except Exception as e:
        return None, f"could not reach Google: {e}", {}
    voices = body.get("voices", []) or []
    return True, f"valid, {len(voices)} voices available", {
        "voices": len(voices)
    }

VERIFIERS = {
    "huggingface": _verify_huggingface,
    "nvidia": _verify_nvidia,
    "gcp": _verify_gcp,
    "openai": _verify_openai,
    "gemini": _verify_gemini,
    "grok": _verify_grok,
}


def status_report(vault, verify=True):
    """Redacted status for the UI. Contains no credential values."""
    try:
        creds = vault.load()
        err = None
    except VaultError as e:
        creds = {}
        err = str(e)

    items = []
    for spec in ACCESS_MANIFEST:
        resolved = spec["name"]
        value = creds.get(spec["name"])
        if not value:
            for alt in spec.get("aliases", []):
                if creds.get(alt):
                    value, resolved = creds[alt], alt
                    break
        item = {
            "name": resolved,
            "label": spec["label"],
            "category": spec["category"],
            "scope": spec["scope"],
            "used_for": spec["used_for"],
            "breaks": spec["breaks"],
            "required": spec["required"],
            "present": bool(value),
            "state": "missing",
            "detail": "not in vault -- add it in Nobility Depository",
            "meta": {},
        }
        if value:
            item["state"] = "present"
            item["detail"] = "in vault, not verified"
            verifier = VERIFIERS.get(spec["category"])
            if verify and verifier:
                ok, msg, meta = verifier(value)
                if isinstance(ok, str):
                    item["state"] = ok          # "invalid" or "blocked"
                else:
                    item["state"] = {
                        True: "valid",
                        False: "invalid",
                        None: "unreachable",
                    }[ok]
                item["detail"] = msg
                item["meta"] = meta
        items.append(item)

    return {
        "vault_path": vault.path,
        "saved_at": getattr(vault, "saved_at", None),
        "error": err,
        "warnings": vault._warnings,
        "total_in_vault": len(creds),
        "other_credentials": sorted(
            n for n in creds
            if n not in {s["name"] for s in ACCESS_MANIFEST}
        ),
        "ready": all(
            i["state"] in ("valid", "blocked")
            for i in items if i["required"]
        ),
        "credentials": items,
    }


vault = Vault()
