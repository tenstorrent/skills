"""API keys for OpenAI-compatible endpoints (`ttp secret endpoint <alias> --url <base url>`).

Each key is saved with its base URL as `endpoint/<alias>` in the per-user secrets store
(project.secrets_path(), mode 0600), next to the jev and slack slots. Only `credentials()` hands the
key back, to code that calls the endpoint; nothing here prints, logs or records it.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple

from . import redact
from .project import load_secrets, save_secret

PREFIX = "endpoint/"
ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
VERIFY_TIMEOUT_S = 10


def slot(alias: str) -> str:
    """The store key for an alias; ValueError for anything but letters, digits, '-' and '_'."""
    if not ALIAS_RE.match(alias or ""):
        raise ValueError(f"bad endpoint alias {alias!r}: use letters, digits, '-' and '_' only")
    return PREFIX + alias


def base_url(url: str) -> str:
    """The base URL without a trailing slash; ValueError unless it is http(s)://host..."""
    url = (url or "").strip().rstrip("/")
    if not re.match(r"^https?://[^/\s]+", url) or any(c.isspace() for c in url):
        raise ValueError(f"bad endpoint URL {url!r}: give the base URL, e.g. https://api.example.com/v1")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the default handler would resend the Authorization header to the new target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.URLError(f"HTTP {code} redirect; key not sent")   # no target: it names a host


def verify(url: str, key: str, timeout: float = VERIFY_TIMEOUT_S) -> tuple[bool, str]:
    """GET <url>/models with the key as a bearer token. Returns (ok, message); the message never holds the key."""
    ok, msg, _ = list_models(url, key, timeout)
    return ok, msg


def model_ids(body) -> list[str]:
    """The model ids an OpenAI-compatible /models answer lists ({"data": [{"id": ...}]}, or a bare list)."""
    rows = body.get("data") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        return []
    ids = (r.get("id") if isinstance(r, dict) else r for r in rows)
    return sorted({i for i in ids if isinstance(i, str) and i and len(i) <= 200})


def list_models(url: str, key: str, timeout: float = VERIFY_TIMEOUT_S) -> tuple[bool, str, list[str] | None]:
    """GET <url>/models with the key: (ok, message, the served model ids, or None when the answer lists
    none it can read). The message never holds the key or the URL."""
    req = urllib.request.Request(f"{url}/models", headers={"Authorization": f"Bearer {key}",
                                                           "Accept": "application/json"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as r:
            try:
                ids = model_ids(json.loads(r.read(4_000_000) or b"null"))
            except Exception:   # not JSON, or no body: reachable all the same
                ids = []
            return (True, f"HTTP {r.status}", ids or None)
    except urllib.error.HTTPError as e:
        msg = f"HTTP {e.code}" + (f" {e.reason}" if e.reason else "")
    except urllib.error.URLError as e:
        msg = str(e.reason) if "redirect; key not sent" in str(e.reason) else f"cannot reach the endpoint: {e.reason}"
    except Exception as e:   # timeouts, bad responses
        msg = f"{type(e).__name__}: {e}"
    msg = msg.replace(key, "<key>") if key else msg
    parts = urllib.parse.urlsplit(url or "")
    for part in (url, parts.netloc, parts.hostname):   # TLS and socket reasons name the host alone
        msg = msg.replace(part, "<url>" if part == url else "<host>") if part else msg
    return (False, redact.scrub(msg), None)


def save(alias: str, url: str, key: str, verified: bool, check: str) -> None:
    save_secret(slot(alias), {"url": url, "key": key, "verified": verified, "check": check})


def saved() -> dict[str, dict]:
    """Every saved endpoint by alias (key included: callers must not print it)."""
    return {k[len(PREFIX):]: v for k, v in load_secrets().items()
            if k.startswith(PREFIX) and isinstance(v, dict)}


def show_lines() -> list[str]:
    """One line per saved endpoint for `ttp secret show`: alias, base URL and check state, never the key."""
    out = []
    for alias, v in sorted(saved().items()):
        state = "verified" if v.get("verified") else f"not verified ({v.get('check') or 'no check'})"
        out.append(f"{PREFIX}{alias} {v.get('url', '?')} {state}")
    return out


class Credentials(NamedTuple):
    """(key, base URL) saved for an alias. Printing it (or a traceback showing it) never shows the key."""
    key: str
    url: str

    def __repr__(self) -> str:
        return f"Credentials(key=<hidden>, url={self.url!r})"

    __str__ = __repr__


def credentials(alias: str) -> Credentials | None:
    """(key, base URL) saved for an alias, or None when none is saved. For code that calls the
    endpoint only: to test one from a shell, `ttp secret check <alias>`."""
    v = load_secrets().get(slot(alias))
    if not isinstance(v, dict) or not v.get("key") or not v.get("url"):
        return None
    return Credentials(v["key"], v["url"])


def tier_models(cfg: dict | None, alias: str) -> list[tuple[str, str]]:
    """(config path, model) of every providers.<p>.tiers.<tier>.model whose provider uses `alias`."""
    out = []
    for prov, entry in sorted(((cfg or {}).get("providers") or {}).items()):
        if not isinstance(entry, dict) or entry.get("endpoint") != alias:
            continue
        for tier, row in sorted((entry.get("tiers") or {}).items()):
            if isinstance(row, dict) and str(row.get("model") or ""):
                out.append((f"providers.{prov}.tiers.{tier}.model", str(row["model"])))
    return out


def check(alias: str, cfg: dict | None = None) -> tuple[bool, str]:
    """`ttp secret check <alias>`: whether the saved key still works, as lines that never hold the key,
    the base URL or its host (reachable, auth ok, auth failed), then the model ids GET /models lists and
    each configured tier model (tier_models, from `cfg`) the list leaves out."""
    got = credentials(alias)
    if got is None:
        return False, f"no key saved for endpoint {alias!r}: `ttp secret endpoint {alias} --url <base url>`"
    ok, msg, ids = list_models(got.url, got.key)
    if not ok:
        state = "auth failed" if re.match(r"HTTP (401|403)\b", msg) else "not ok"
        return False, f"{PREFIX}{alias}: {state} ({msg})"
    lines = [f"{PREFIX}{alias}: reachable, auth ok ({msg})"]
    if ids is None:
        return True, "\n".join(lines + ["  models: its /models answer lists none"])
    lines.append(f"  serves {len(ids)} model{'s' * (len(ids) != 1)}: {', '.join(ids)}")
    lines += [f"  {path} {model}: NOT SERVED by this endpoint"
              for path, model in tier_models(cfg, alias) if model not in ids]
    return True, "\n".join(lines)


def env_for(alias: str) -> dict[str, str]:
    """The environment `ttp secret env <alias> -- <command>` gives the command (OpenAI-compatible names):
    the key reaches it there, never on a command line or in tool output."""
    got = credentials(alias)
    if got is None:
        raise ValueError(f"no key saved for endpoint {alias!r}")
    return {"OPENAI_API_KEY": got.key, "OPENAI_BASE_URL": got.url, "TTP_ENDPOINT_URL": got.url}
