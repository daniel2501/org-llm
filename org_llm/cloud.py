# [[file:../../../org/20260425230731-org_llm.org::*cloud.py][cloud.py:1]]
"""Cloud GPU provider registry — automatic compute expansion beyond local Ollama."""
from __future__ import annotations

import json
import os
import ssl
import urllib.request
from pathlib import Path
from typing import NamedTuple


# ── SSL CA bundle resolution ──────────────────────────────────────────────────
# On Guix and minimal containers Python's compiled-in openssl defaults often
# point at a /gnu/store path that doesn't contain certs, breaking HTTPS to
# every cloud provider. Probe the common system locations and build a context
# that works in any environment.

def _ssl_context() -> ssl.SSLContext | None:
    """Return an SSL context with a working CA bundle, or None to use the default."""
    env_file = os.environ.get("SSL_CERT_FILE")
    env_dir  = os.environ.get("SSL_CERT_DIR")
    if env_file or env_dir:
        return ssl.create_default_context(cafile=env_file, capath=env_dir)
    # Common bundle locations across distros
    for cafile in (
        "/etc/ssl/certs/ca-certificates.crt",   # Debian/Ubuntu/Arch/Guix System
        "/etc/pki/tls/certs/ca-bundle.crt",     # Fedora/RHEL
        "/etc/ssl/cert.pem",                    # BSD/macOS
        str(Path.home() / ".guix-profile/etc/ssl/certs/ca-certificates.crt"),
        str(Path.home() / ".guix-home/profile/etc/ssl/certs/ca-certificates.crt"),
    ):
        if Path(cafile).exists():
            return ssl.create_default_context(cafile=cafile)
    # Last resort — try certifi if it's importable
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


_SSL_CONTEXT = _ssl_context()


def _urlopen(req, timeout: float = 30):
    """urlopen wrapper that injects our resolved SSL context."""
    if _SSL_CONTEXT is not None and req.full_url.startswith("https://"):
        return urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT)
    return urllib.request.urlopen(req, timeout=timeout)


# ── Provider registry ──────────────────────────────────────────────────────────

class ProviderInfo(NamedTuple):
    slug:           str          # config key prefix / ID
    name:           str          # display name
    signup_url:     str          # referral/signup link
    console_url:    str          # manage instances
    docs_url:       str          # Ollama / API setup docs
    api_compat:     str          # "ollama" | "openai" | "both"
    endpoint_hint:  str          # template for endpoint URL
    gpu_costs:      dict         # GPU name → $/hr approximate spot
    description:    str          # one-line summary
    pricing_url:    str = ""     # billing / paid-tier upgrade page
    paid_examples:  tuple = ()   # representative paid models for upgrade pitch


PROVIDERS: list[ProviderInfo] = [
    ProviderInfo(
        slug        = "runpod",
        name        = "RunPod",
        signup_url  = "https://www.runpod.io/?ref=org-llm-cli",
        console_url = "https://www.runpod.io/console/pods",
        docs_url    = "https://www.runpod.io/console/explore",
        api_compat  = "both",
        endpoint_hint = "https://{pod_id}-11434.proxy.runpod.net",
        gpu_costs   = {
            "RTX 3090":      0.37,
            "RTX 4090":      0.74,
            "RTX A6000":     0.79,
            "A100 40GB":     1.64,
            "A100 80GB":     1.89,
            "H100 PCIe":     2.49,
            "H100 80GB SXM": 2.79,
        },
        description = "Popular GPU cloud; Ollama templates; pay-per-second billing",
    ),
    ProviderInfo(
        slug        = "vast",
        name        = "Vast.ai",
        signup_url  = "https://cloud.vast.ai/",
        console_url = "https://cloud.vast.ai/",
        docs_url    = "https://vast.ai/docs/",
        api_compat  = "ollama",
        endpoint_hint = "http://{host}:{port}",
        gpu_costs   = {
            "RTX 3080":      0.14,
            "RTX 3090":      0.18,
            "RTX 4090":      0.35,
            "A100 80GB":     1.20,
            "H100 80GB SXM": 2.10,
        },
        description = "GPU marketplace — bid on spot; often 40–60% cheaper than RunPod",
    ),
    ProviderInfo(
        slug        = "lambda",
        name        = "Lambda Labs",
        signup_url  = "https://lambdalabs.com/service/gpu-cloud",
        console_url = "https://cloud.lambdalabs.com/instances",
        docs_url    = "https://docs.lambdalabs.com/on-demand-cloud/",
        api_compat  = "openai",
        endpoint_hint = "https://api.lambdalabs.com/v1",
        gpu_costs   = {
            "A10":           0.75,
            "A100 40GB SXM": 1.29,
            "A100 80GB SXM": 1.99,
            "H100 80GB SXM": 2.49,
        },
        description = "Reliable on-demand GPU cloud; strong SLA; OpenAI-compatible inference API",
    ),
    ProviderInfo(
        slug        = "tensordock",
        name        = "TensorDock",
        signup_url  = "https://tensordock.com/",
        console_url = "https://marketplace.tensordock.com/",
        docs_url    = "https://tensordock.com/docs/",
        api_compat  = "ollama",
        endpoint_hint = "http://{host}:{port}",
        gpu_costs   = {
            "RTX 3090":      0.22,
            "RTX 4090":      0.40,
            "A100 80GB":     1.35,
            "H100 80GB SXM": 2.20,
        },
        description = "Low-cost GPU marketplace; deploy Ollama containers; spot instances",
    ),
    ProviderInfo(
        slug        = "salad",
        name        = "Salad Cloud",
        signup_url  = "https://salad.com/",
        console_url = "https://portal.salad.com/",
        docs_url    = "https://docs.salad.com/",
        api_compat  = "openai",
        endpoint_hint = "https://{container_id}.salad.cloud",
        gpu_costs   = {
            "RTX 3080":      0.08,
            "RTX 3090":      0.12,
            "RTX 4090":      0.28,
            "A100 80GB":     0.80,
        },
        description = "Distributed consumer GPU network — lowest rates; best for batch inference",
    ),
    ProviderInfo(
        slug        = "paperspace",
        name        = "Paperspace (DigitalOcean GPU)",
        signup_url  = "https://www.paperspace.com/gpu-cloud",
        console_url = "https://console.paperspace.com/",
        docs_url    = "https://docs.paperspace.com/",
        api_compat  = "both",
        endpoint_hint = "https://{deployment_url}",
        gpu_costs   = {
            "A100 80GB":     3.09,
            "H100 80GB":     4.50,
        },
        description = "Managed GPU cloud; Gradient notebooks + deployments; DigitalOcean-backed",
    ),
    ProviderInfo(
        slug        = "coreweave",
        name        = "CoreWeave",
        signup_url  = "https://www.coreweave.com/",
        console_url = "https://cloud.coreweave.com/",
        docs_url    = "https://docs.coreweave.com/",
        api_compat  = "openai",
        endpoint_hint = "https://{service}.coreweave.cloud",
        gpu_costs   = {
            "RTX A6000":     0.80,
            "A100 80GB SXM": 2.06,
            "H100 80GB SXM": 2.99,
        },
        description = "Enterprise-grade GPU cloud; Kubernetes-native; highest uptime SLA",
    ),
    # ── Free-tier hosted inference (no GPU rental, just a metered API) ───────
    ProviderInfo(
        slug        = "openrouter",
        name        = "OpenRouter",
        signup_url  = "https://openrouter.ai/",
        console_url = "https://openrouter.ai/keys",
        docs_url    = "https://openrouter.ai/docs",
        api_compat  = "openai",
        endpoint_hint = "https://openrouter.ai/api/v1",
        gpu_costs   = {
            "free tier (Llama 3.1 8B)":    0.00,
            "Llama 3.3 70B":               0.40,
            "DeepSeek R1":                 0.55,
            "Claude Sonnet 4.6":           3.00,
        },
        description = "Hosted multi-model gateway; FREE tier (Llama 3.1 8B); per-token billing",
        pricing_url = "https://openrouter.ai/credits",
        # FOSS / open-weights first; closed APIs last. All are fully self-hostable
        # except the trailing two.
        paid_examples = ("deepseek/deepseek-r1",                   # MIT, open weights
                          "meta-llama/llama-3.3-70b-instruct",       # Meta Llama community, open weights
                          "qwen/qwen-2.5-72b-instruct",              # Apache 2.0, open weights
                          "openai/gpt-oss-120b",                     # Apache 2.0 (OpenAI's open release)
                          "anthropic/claude-sonnet-4.6",             # closed API; long-context tool use
                          "openai/gpt-5.5"),                         # closed API; structured-JSON specialist
    ),
    ProviderInfo(
        slug        = "huggingface",
        name        = "Hugging Face Inference",
        signup_url  = "https://huggingface.co/join",
        console_url = "https://huggingface.co/settings/tokens",
        docs_url    = "https://huggingface.co/docs/api-inference",
        api_compat  = "openai",
        endpoint_hint = "https://router.huggingface.co/v1",
        gpu_costs   = {
            "free tier (rate-limited)":  0.00,
            "Llama 3.3 70B":             0.50,
        },
        description = "Hosted inference for any HF model; FREE tier with rate limits",
        pricing_url = "https://huggingface.co/pricing",
        paid_examples = ("meta-llama/Llama-3.3-70B-Instruct", "deepseek-ai/DeepSeek-R1"),
    ),
]

PROVIDER_MAP: dict[str, ProviderInfo] = {p.slug: p for p in PROVIDERS}


def get_provider(slug: str) -> ProviderInfo | None:
    return PROVIDER_MAP.get(slug)


# ── Curated cloud-model catalog ──────────────────────────────────────────────
# Mirrors org_llm/models.py's CATALOG for local Ollama, but for hosted
# inference providers. Used by `org-llm cloud --tune` to per-role
# recommend a better cloud_model based on cost + quality.
#
# `roles` mirrors local CATALOG — role tags map to org-llm's role
# config keys (chat / fast / code / reason / instruct / text).
# `cost_in` / `cost_out` are USD per million input / output tokens
# (0.0 = free tier, may have rate limits).
# `quality` mirrors models._QUALITY scale: 50=tiny, 100=3B-class,
# 150=14B-class, 200=70B-class.

class CloudModelInfo(NamedTuple):
    provider:  str        # provider slug from PROVIDER_MAP
    slug:      str        # the model identifier the API call uses
    roles:     tuple      # (chat, fast, code, reason, instruct, text)
    quality:   int        # quality rank, comparable with local _QUALITY
    cost_in:   float      # USD per 1M input tokens (0.0 = free tier)
    cost_out:  float      # USD per 1M output tokens
    license:   str        # license / openness shorthand
    note:      str        # one-line description


# ── Catalog loader: bundled JSON + user-cache override ───────────────────────
# CLOUD_MODELS used to live here as a hardcoded list. It now loads from
# a JSON file shipped with the package (data/cloud_catalog.json) so:
#  • Pricing edits don't require source changes (community PRs are JSON
#    diffs, far less risky than Python edits).
#  • A user cache at ~/.local/share/org-llm/cloud_catalog.json can
#    override the bundled file with fresher data — written by
#    `org-llm cloud --refresh-catalog`, which polls live provider APIs.
#  • Every entry carries an `updated_at` stamp so stale recommendations
#    can be flagged in the UI.

import json as _json
from pathlib import Path as _Path

# Module-state — populated by _load_catalog() below.
CLOUD_MODELS:             list[CloudModelInfo] = []
CLOUD_MODELS_BY_PROVIDER: dict[str, list[CloudModelInfo]] = {}
CATALOG_META: dict = {
    "version":          1,
    "updated_at":       "",         # ISO-date the catalog was last refreshed
    "stale_after_days": 90,
    "source":           "bundled",   # "bundled" | "user_cache" | "merged"
}


def _bundled_catalog_path() -> _Path:
    """Path to the JSON shipped with the package."""
    return _Path(__file__).resolve().parent / "data" / "cloud_catalog.json"


def _user_catalog_path() -> _Path:
    """User-writable override at the standard org-llm data dir."""
    import os as _os
    base = _Path(_os.environ.get("XDG_DATA_HOME")
                  or _os.path.expanduser("~/.local/share"))
    return base / "org-llm" / "cloud_catalog.json"


def _read_json_safe(p: _Path) -> dict | None:
    try:
        if p.exists():
            return _json.loads(p.read_text())
    except Exception:
        return None
    return None


def _build_models(rows: list[dict]) -> list[CloudModelInfo]:
    """Coerce JSON rows → CloudModelInfo namedtuples. Rows missing
    required keys are skipped silently (we never want a typo in the
    catalog to blow up a chat call)."""
    out: list[CloudModelInfo] = []
    for r in rows or []:
        try:
            out.append(CloudModelInfo(
                provider = r["provider"],
                slug     = r["slug"],
                roles    = tuple(r.get("roles", ("chat",))),
                quality  = int(r.get("quality", 100)),
                cost_in  = float(r.get("cost_in", 0.0)),
                cost_out = float(r.get("cost_out", 0.0)),
                license  = r.get("license", ""),
                note     = r.get("note", ""),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _load_catalog() -> None:
    """(Re)populate the module-state CLOUD_MODELS / CATALOG_META.

    User cache wins when present + valid; bundled JSON is the
    authoritative fallback. Called at import time and again on
    explicit refresh.
    """
    global CLOUD_MODELS, CLOUD_MODELS_BY_PROVIDER, CATALOG_META

    bundled = _read_json_safe(_bundled_catalog_path()) or {}
    user    = _read_json_safe(_user_catalog_path()) or {}
    if user and isinstance(user.get("cloud_models"), list):
        # User cache present + has the right shape — use it as source.
        CLOUD_MODELS = _build_models(user.get("cloud_models", []))
        CATALOG_META = {
            "version":          user.get("version", 1),
            "updated_at":       user.get("updated_at", ""),
            "stale_after_days": user.get("stale_after_days", 90),
            "source":           "user_cache",
        }
    else:
        CLOUD_MODELS = _build_models(bundled.get("cloud_models", []))
        CATALOG_META = {
            "version":          bundled.get("version", 1),
            "updated_at":       bundled.get("updated_at", ""),
            "stale_after_days": bundled.get("stale_after_days", 90),
            "source":           "bundled",
        }

    CLOUD_MODELS_BY_PROVIDER = {}
    for _m in CLOUD_MODELS:
        CLOUD_MODELS_BY_PROVIDER.setdefault(_m.provider, []).append(_m)


# Populate on import.
_load_catalog()


def reload_catalog() -> None:
    """Public hook so the refresh-catalog verb can re-import after
    writing the user cache without restarting the process."""
    _load_catalog()


def catalog_age_days() -> int | None:
    """Days since the active catalog was last updated; None when the
    `updated_at` field is missing or unparseable."""
    raw = (CATALOG_META.get("updated_at") or "").strip()
    if not raw:
        return None
    from datetime import date, datetime
    try:
        d = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        try:
            d = datetime.fromisoformat(raw).date()
        except Exception:
            return None
    return (date.today() - d).days


def catalog_is_stale() -> bool:
    """True when the catalog is older than `stale_after_days`."""
    age = catalog_age_days()
    if age is None:
        return False
    return age > int(CATALOG_META.get("stale_after_days", 90))


# ── Live refresh from provider APIs ──────────────────────────────────────────

def refresh_from_openrouter(*, timeout: float = 15.0
                              ) -> tuple[list[dict], str]:
    """Fetch the live OpenRouter model list + pricing.

    Returns ``(rows, message)``: ``rows`` is the list-of-dicts shaped
    like our JSON catalog (provider/slug/roles/quality/cost_in/cost_out
    /license/note). ``message`` is a human-readable summary of what was
    fetched (counts, source).

    OpenRouter is the highest-volume churn source — they add models
    weekly. The endpoint is open (no API key needed for the model
    list). Other providers don't expose comparable JSON; those rows
    must come from manual PRs against the bundled catalog.
    """
    import urllib.request as _ur
    # Use the project's _urlopen wrapper, NOT bare urllib. _urlopen
    # injects the resolved SSL context (Guix users have their CA
    # bundle under ~/.guix-home/profile/etc/ssl/certs, not the
    # locations Python's stdlib auto-discovers). Earlier this used
    # bare urlopen and 500'd with CERTIFICATE_VERIFY_FAILED on Guix.
    req = _ur.Request("https://openrouter.ai/api/v1/models",
                       headers={"User-Agent": "org-llm/refresh-catalog"})
    with _urlopen(req, timeout=timeout) as resp:
        payload = _json.loads(resp.read().decode("utf-8"))
    raw_models = payload.get("data") or []
    rows: list[dict] = []
    for m in raw_models:
        slug = m.get("id") or m.get("slug")
        if not slug:
            continue
        # OpenRouter pricing is per-token strings ("0.0000003" = $0.30/Mtok).
        pricing = m.get("pricing") or {}
        try:
            cost_in  = float(pricing.get("prompt", 0)) * 1_000_000
            cost_out = float(pricing.get("completion", 0)) * 1_000_000
        except (TypeError, ValueError):
            cost_in = cost_out = 0.0
        # Heuristic role mapping. Names with "coder"/"code" → code role;
        # "r1"/"reasoning" → reason; default → chat+instruct.
        s = slug.lower()
        roles = ["chat", "instruct"]
        if "coder" in s or "code" in s:
            roles = ["code"]
        elif "r1" in s or "reasoning" in s:
            roles = ["reason"]
            if "distill" in s:
                roles.append("chat")
        elif "flash" in s or "8b" in s or "instant" in s or "mini" in s:
            roles = ["chat", "instruct", "fast"]
        # Heuristic quality tier from parameter count or model line. We
        # only set quality when we don't already have a curated score
        # — the merge step preserves curated quality.
        if "70b" in s or "72b" in s or "120b" in s:
            quality = 190
        elif "32b" in s or "30b" in s:
            quality = 175
        elif "13b" in s or "14b" in s:
            quality = 150
        elif "8b" in s or "9b" in s or "12b" in s:
            quality = 125
        else:
            quality = 110
        # Premium closed APIs go higher. Still heuristic.
        if "claude" in s or "gpt-5" in s or "gpt-4.5" in s:
            quality = max(quality, 240)
        # OpenRouter's /api/v1/models doesn't return a canonical
        # license string. Leave this empty so `merge_refresh` keeps the
        # curated license when one exists ("Apache 2.0", "MIT",
        # "Closed API", etc.) — overwriting curated values with a weak
        # placeholder like "see openrouter" was a regression that
        # showed up in `cloud --propose-update` as 4 spurious license
        # changes per refresh.
        license_ = ""
        top = (m.get("top_provider") or {}).get("name") or ""
        note = m.get("description") or top or ""
        if len(note) > 80:
            note = note[:77] + "…"
        rows.append({
            "provider": "openrouter",
            "slug":     slug,
            "roles":    roles,
            "quality":  quality,
            "cost_in":  round(cost_in, 4),
            "cost_out": round(cost_out, 4),
            "license":  license_,
            "note":     note,
        })
    msg = (f"Fetched {len(rows)} model(s) from OpenRouter "
            f"(/api/v1/models).")
    return rows, msg


def merge_refresh(new_rows: list[dict], *,
                    preserve_curated_quality: bool = True
                   ) -> tuple[list[dict], dict]:
    """Merge live-refreshed rows into the active catalog.

    Strategy:
      • For matching slugs: update cost_in / cost_out / license / note
        from the refresh; KEEP curated quality + roles (they're our
        editorial calls, the live API can't reproduce them).
      • For new slugs not in the local catalog: add them with the
        refresh's heuristic quality.
      • For local rows the refresh didn't return: keep them (paid
        models occasionally drop off the API but still work).

    Returns (merged_rows, diff_summary). The diff summary has counts
    of added / changed / unchanged so the caller can tell the user.
    """
    by_slug = {m.slug: m for m in CLOUD_MODELS}
    merged: list[dict] = []
    added:    list[str] = []
    changed:  list[str] = []
    price_changes: list[dict] = []
    for r in new_rows:
        slug = r["slug"]
        if slug in by_slug:
            old = by_slug[slug]
            new_row = {
                "provider": old.provider,
                "slug":     slug,
                "roles":    list(old.roles),
                "quality":  old.quality if preserve_curated_quality
                              else r.get("quality", old.quality),
                "cost_in":  r.get("cost_in",  old.cost_in),
                "cost_out": r.get("cost_out", old.cost_out),
                "license":  r.get("license") or old.license,
                "note":     r.get("note")    or old.note,
            }
            merged.append(new_row)
            if (abs(new_row["cost_out"] - old.cost_out) > 0.001
                    or abs(new_row["cost_in"] - old.cost_in) > 0.001):
                changed.append(slug)
                price_changes.append({
                    "slug": slug,
                    "old_cost_out": old.cost_out,
                    "new_cost_out": new_row["cost_out"],
                    "old_cost_in":  old.cost_in,
                    "new_cost_in":  new_row["cost_in"],
                })
        else:
            merged.append(r)
            added.append(slug)
    # Carry forward local-only rows (paid Anthropic/OpenAI models,
    # HuggingFace — refresh only covers OpenRouter today).
    seen = {m["slug"] for m in merged}
    carried_over = 0
    for m in CLOUD_MODELS:
        if m.slug not in seen:
            merged.append({
                "provider": m.provider,
                "slug":     m.slug,
                "roles":    list(m.roles),
                "quality":  m.quality,
                "cost_in":  m.cost_in,
                "cost_out": m.cost_out,
                "license":  m.license,
                "note":     m.note,
            })
            carried_over += 1
    summary = {
        "added":           added,
        "changed":         changed,
        "carried_over":    carried_over,
        "total":           len(merged),
        "price_changes":   price_changes,
    }
    return merged, summary


def write_user_catalog(rows: list[dict], *,
                          providers_meta: list[dict] | None = None,
                          stale_after_days: int | None = None) -> _Path:
    """Persist refreshed rows to the user cache. Stamps `updated_at`
    with today's date."""
    import datetime as _dt
    p = _user_catalog_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": _dt.date.today().isoformat(),
        "stale_after_days": (stale_after_days
                              if stale_after_days is not None
                              else CATALOG_META.get("stale_after_days", 90)),
        "doc": ("Auto-generated by `org-llm cloud --refresh-catalog`. "
                 "Edit by hand at your own risk; rerun the refresh to "
                 "restore from live provider APIs."),
        "cloud_models": rows,
    }
    if providers_meta is not None:
        payload["providers"] = providers_meta
    p.write_text(_json.dumps(payload, indent=2))
    return p


# ── Provider lifecycle: liveness probes + remote canonical roster ────────────

def _bundled_provider_meta() -> list[dict]:
    """Return the providers list as recorded in the active catalog
    JSON (not the PROVIDERS NamedTuple list, which is the in-memory
    source of truth for runtime data)."""
    user    = _read_json_safe(_user_catalog_path()) or {}
    bundled = _read_json_safe(_bundled_catalog_path()) or {}
    if isinstance(user.get("providers"), list):
        return user["providers"]
    return list(bundled.get("providers", []))


def probe_provider_liveness(*, timeout: float = 5.0) -> list[dict]:
    """HEAD-probe every PROVIDER's signup_url. Returns one dict per
    provider with keys: slug, name, signup_url, status_code, alive,
    error.

    Cheap (parallelisable) but conservative — many providers serve a
    redirect chain or block HEAD. We treat any 2xx OR 3xx as alive,
    and a connection error as dead.
    """
    import urllib.request as _ur
    import urllib.error  as _ue
    import socket as _sock
    out: list[dict] = []
    for p in PROVIDERS:
        url = p.signup_url or p.docs_url or p.console_url
        record = {
            "slug": p.slug, "name": p.name,
            "signup_url": url,
            "status_code": None, "alive": False, "error": "",
        }
        try:
            req = _ur.Request(url, method="HEAD",
                                headers={"User-Agent":
                                          "org-llm/refresh-catalog"})
            with _urlopen(req, timeout=timeout) as resp:
                code = resp.getcode()
                record["status_code"] = code
                record["alive"] = (200 <= code < 400)
        except _ue.HTTPError as e:
            # Some providers reject HEAD with 4xx/5xx but the site is
            # very much alive. 405 (method not allowed) and 403 (anti-
            # bot) count as alive; 404 on the signup page is suspicious.
            record["status_code"] = e.code
            record["alive"] = e.code in (403, 405)
            if not record["alive"]:
                record["error"] = f"HTTP {e.code}"
        except (_ue.URLError, _sock.timeout, OSError) as e:
            record["error"] = str(e)[:80]
            record["alive"] = False
        out.append(record)
    return out


def fetch_remote_catalog(remote_url: str = "",
                            *, timeout: float = 10.0
                           ) -> dict | None:
    """Pull the canonical catalog from this repo's main branch.

    Used by `--refresh-catalog` to discover newly-added providers
    without requiring a release of org-llm. Returns the parsed JSON
    or None on failure (network down, unreachable, malformed)."""
    import urllib.request as _ur
    if not remote_url:
        bundled = _read_json_safe(_bundled_catalog_path()) or {}
        remote_url = bundled.get("remote_url", "")
    if not remote_url:
        return None
    try:
        req = _ur.Request(remote_url,
                           headers={"User-Agent":
                                      "org-llm/refresh-catalog"})
        with _urlopen(req, timeout=timeout) as resp:
            return _json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def diff_provider_rosters(local: list[dict],
                             remote: list[dict]) -> dict:
    """Compare two providers lists. Returns added/removed/changed."""
    by_local  = {p.get("slug"): p for p in local  if p.get("slug")}
    by_remote = {p.get("slug"): p for p in remote if p.get("slug")}
    added   = sorted(set(by_remote) - set(by_local))
    removed = sorted(set(by_local)  - set(by_remote))
    changed = []
    for slug, lr in by_local.items():
        rr = by_remote.get(slug)
        if not rr:
            continue
        if lr.get("status") != rr.get("status"):
            changed.append({"slug": slug,
                              "field": "status",
                              "from": lr.get("status"),
                              "to":   rr.get("status")})
    return {"added": added, "removed": removed, "changed": changed}


def cloud_models_for_provider(provider_slug: str) -> list[CloudModelInfo]:
    """All curated cloud models hosted by this provider."""
    return CLOUD_MODELS_BY_PROVIDER.get(provider_slug, [])


def recommend_cloud_models(
    *, provider_slug: str, current_model: str = "",
    budget_per_mtok_out: float | None = None,
    role_filter: str = "",
) -> list[dict]:
    """Per-role recommend the highest-quality cloud model that fits
    the user's budget. Mirrors models.recommendations() for local.

    Returns one dict per role with keys: role, current, suggested,
    quality, cost_in, cost_out, license, note, upgrade (bool).
    """
    pool = cloud_models_for_provider(provider_slug)
    if not pool:
        return []
    if budget_per_mtok_out is not None:
        pool = [m for m in pool if m.cost_out <= budget_per_mtok_out]
    if not pool:
        return []

    # Keys we recommend on, mirroring the local roles
    # (skip "embed" — cloud embedding is a niche separately handled via
    # cloud_embed / different config).
    roles = ("chat", "fast", "code", "reason", "instruct", "text")
    recs: list[dict] = []
    for role in roles:
        if role_filter and role != role_filter:
            continue
        candidates = [m for m in pool if role in m.roles]
        if not candidates:
            continue
        candidates.sort(key=lambda m: (m.quality, -m.cost_out), reverse=True)
        winner = candidates[0]
        is_upgrade = current_model.lower() != winner.slug.lower()
        recs.append({
            "role":      role,
            "current":   current_model,
            "suggested": winner.slug,
            "quality":   winner.quality,
            "cost_in":   winner.cost_in,
            "cost_out":  winner.cost_out,
            "license":   winner.license,
            "note":      winner.note,
            "upgrade":   is_upgrade,
        })
    return recs


# ── Approximate model VRAM requirements (GB) ──────────────────────────────────
MODEL_VRAM: list[tuple[str, float]] = [
    ("1b",              1.5),
    ("3b",              3.0),
    ("7b",              5.5),
    ("8b",              6.0),
    ("phi4",            9.0),
    ("13b",            10.0),
    ("mistral",         5.5),
    ("gemma3",          6.0),
    ("llama3.2",        3.5),
    ("llama3.3",       48.0),
    ("llama3.1",        5.5),
    ("qwen2.5-coder",   5.5),
    ("deepseek-r1",    48.0),
    ("nomic-embed",     0.5),
    ("phi3",            4.0),
    ("mistral-nemo",    8.5),
    ("qwq",            22.0),
]


class CloudStatus(NamedTuple):
    provider:   str
    endpoint:   str
    model:      str
    reachable:  bool
    auth_ok:    bool
    latency_ms: float | None


# ── Hardware assessment ────────────────────────────────────────────────────────

def local_vram_gb() -> float | None:
    """Return available GPU VRAM in GB, or None if no GPU / nvidia-smi unavailable."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, timeout=5, text=True,
        )
        return sum(float(x.strip()) for x in out.strip().splitlines() if x.strip()) / 1024
    except Exception:
        return None


def local_ram_gb() -> float:
    """Return total system RAM in GB."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1_048_576
    except Exception:
        pass
    import shutil
    return shutil.disk_usage("/").total / 1e9


def model_needs_vram(model_name: str) -> float:
    name = model_name.lower()
    for fragment, vram in MODEL_VRAM:
        if fragment in name:
            return vram
    return 5.5  # assume 7B-class


def assess_local_capability(models: list[str]) -> list[dict]:
    vram = local_vram_gb()
    ram  = local_ram_gb()
    results = []
    for model in models:
        needed = model_needs_vram(model)
        if vram is not None:
            can_local = vram >= needed
            resource  = f"{vram:.0f} GB VRAM available"
        else:
            can_local = (ram * 0.6) >= needed
            resource  = f"{ram:.0f} GB RAM (CPU-only)"
        results.append({
            "model":       model,
            "vram_needed": needed,
            "can_local":   can_local,
            "resource":    resource,
            "reason":      "✓ fits locally" if can_local else f"needs {needed:.0f}GB, recommend cloud",
        })
    return results


# ── Connection check ───────────────────────────────────────────────────────────

def _candidate_paths(endpoint_url: str, kind: str) -> list[str]:
    """Return the list of probe paths to try for a given URL.

    OpenAI-compatible base URLs frequently already include `/v1` (OpenRouter,
    Lambda, …). Naively appending `/v1/...` would yield `/v1/v1/...`
    which 404s. We probe both `/v1/<thing>` and `/<thing>` so a single helper
    works for plain Ollama, hosted OpenAI gateways, and bare OpenAI APIs.
    """
    url = endpoint_url.rstrip("/")
    has_v1 = url.endswith("/v1")
    if kind == "tags":
        # Listing models — Ollama uses /api/tags, OpenAI uses /models or /v1/models
        return ["/models", "/api/tags"] if has_v1 else ["/api/tags", "/v1/models"]
    if kind == "chat":
        return ["/chat/completions", "/api/chat"] if has_v1 else ["/api/chat", "/v1/chat/completions"]
    if kind == "embed":
        return ["/embeddings", "/api/embed"] if has_v1 else ["/api/embed", "/v1/embeddings"]
    return []


def check_connection(endpoint_url: str, api_key: str = "", model: str = ""
                       ) -> CloudStatus:
    """Ping a cloud Ollama/OpenAI-compatible endpoint and return status.

    Records the last failure reason on the returned CloudStatus when
    every probe path fails — earlier this function just said "not
    reachable" with no breadcrumb, which left "Check your network"
    as the only error a user could see even when the actual cause was
    a 400 from a malformed Bearer header. Now the caller can inspect
    `status.error` and surface the real reason.
    """
    import time
    url = endpoint_url.rstrip("/")
    headers: dict[str, str] = {"Content-Type": "application/json",
                                "User-Agent":   "org-llm/check-connection"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    last_status: CloudStatus | None = None
    last_err:    str = ""
    for path in _candidate_paths(endpoint_url, "tags"):
        try:
            req = urllib.request.Request(f"{url}{path}", headers=headers, method="GET")
            t0 = time.monotonic()
            with _urlopen(req, timeout=8) as resp:
                latency = (time.monotonic() - t0) * 1000
                json.loads(resp.read())
                return CloudStatus("configured", endpoint_url, model, True, True, latency)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                last_status = CloudStatus("configured", endpoint_url, model, True, False, None)
                last_err = f"HTTP 401 unauthorised at {path}"
                continue
            # Non-401 HTTP error (e.g. 404 because we picked the wrong path,
            # or 400 from a malformed Bearer header). Record + try next.
            last_err = f"HTTP {e.code} {e.reason} at {path}"
            continue
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:120]}"
            continue

    if last_status is None:
        last_status = CloudStatus("configured", endpoint_url, model,
                                    False, False, None)
    # Attach the last error reason via the dynamic `_error` attribute.
    # CloudStatus is a NamedTuple so we can't add it as a field without
    # bumping the version; stash it on the underlying class so callers
    # that want detail can read it without breaking older callers.
    try:
        object.__setattr__(last_status, "_error", last_err)
    except Exception:
        pass
    return last_status


def last_check_error(status: CloudStatus) -> str:
    """Return the failure reason recorded by `check_connection`, or ""
    when the call succeeded. Safe to call on older CloudStatus values
    that pre-date the diagnostic field."""
    return getattr(status, "_error", "") or ""


# ── Chat / embed via cloud ─────────────────────────────────────────────────────

def cloud_chat(
    prompt: str, model: str, endpoint_url: str,
    api_key: str = "", system: str = "",
) -> str:
    """Cloud chat round-trip.

    Logs every successful call to the logbook (kind=llm,
    command=cloud-chat) so dbt + Captain's Log see it the same way as
    local Ollama calls. Failures are also logged with outcome=error.
    Conversation history is preserved in BOTH the History.response
    column AND the org file.
    """
    from .logbook import track_event as _track
    with _track("llm", "cloud-chat", model=model,
                  args=f"prompt_chars={len(prompt)} "
                       f"system_chars={len(system or '')} "
                       f"endpoint={endpoint_url}") as ev:
        out = _cloud_chat_core(prompt, model, endpoint_url, api_key, system)
        ev["response"] = out or ""
        return out


def _cloud_chat_core(
    prompt: str, model: str, endpoint_url: str,
    api_key: str = "", system: str = "",
) -> str:
    url = endpoint_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = json.dumps({"model": model, "messages": messages, "stream": False}).encode()

    import time as _time
    last_err: Exception | None = None
    # Detect provider slug from endpoint for telemetry
    provider_slug = ""
    for p in PROVIDERS:
        if p.endpoint_hint == endpoint_url or endpoint_url.startswith(
                p.endpoint_hint.split("{")[0] if "{" in p.endpoint_hint else p.endpoint_hint):
            provider_slug = p.slug
            break

    for path in _candidate_paths(endpoint_url, "chat"):
        t0 = _time.monotonic()
        try:
            req = urllib.request.Request(f"{url}{path}", data=payload, headers=headers, method="POST")
            with _urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
                latency_ms = (_time.monotonic() - t0) * 1000
                if "message" in data:
                    record_event(provider_slug, model, "ok", latency_ms=latency_ms)
                    return data["message"]["content"]
                if "choices" in data:
                    record_event(provider_slug, model, "ok", latency_ms=latency_ms)
                    return data["choices"][0]["message"]["content"]
        except Exception as e:
            last_err = e
            outcome, code = _classify_error(e)
            record_event(provider_slug, model, outcome, status_code=code,
                          detail=str(e)[:160])
            continue
    raise RuntimeError(f"Cloud chat failed — endpoint {endpoint_url} unreachable ({last_err!r})")


def cloud_embed(text: str, model: str, endpoint_url: str, api_key: str = "") -> list[float]:
    url = endpoint_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    last_err: Exception | None = None
    for path in _candidate_paths(endpoint_url, "embed"):
        try:
            payload = json.dumps({"model": model, "input": text}).encode()
            req = urllib.request.Request(f"{url}{path}", data=payload, headers=headers, method="POST")
            with _urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                if "embeddings" in data:
                    return data["embeddings"][0]
                if "data" in data:
                    return data["data"][0]["embedding"]
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"Cloud embed failed — endpoint {endpoint_url} ({last_err!r})")


def cost_per_1k_tokens(
    gpu_name: str,
    tokens_per_sec: float = 30.0,
    provider_slug: str = "runpod",
) -> float:
    """Estimate cost per 1000 tokens given GPU type, throughput, and provider."""
    provider = PROVIDER_MAP.get(provider_slug)
    costs = provider.gpu_costs if provider else PROVIDERS[0].gpu_costs
    hourly = costs.get(gpu_name, list(costs.values())[1] if len(costs) > 1 else 0.74)
    tokens_per_hour = tokens_per_sec * 3600
    return (hourly / tokens_per_hour) * 1000


def open_url(url: str) -> None:
    """Open `url` in the user's default browser without leaking the
    browser's stdout/stderr into our TTY.

    `webbrowser.open()` spawns a child process (xdg-open, qutebrowser,
    etc.) that inherits stdout/stderr from us. Some browsers print
    diagnostic lines like "INFO: Opening in existing instance" that
    interleave with whatever prompt or progress region we render
    immediately after. The classic case: a hidden-input password
    prompt for an API key that gets visually clobbered by the
    browser's log line, so the user can't tell when it's ready for
    paste. Fix: redirect child stdout/stderr to /dev/null and start
    a new session so the browser is fully detached from our TTY.

    Falls back to plain webbrowser.open() if direct subprocess
    detection fails — better an interleaved prompt than no browser.
    """
    import webbrowser, subprocess, shutil
    # webbrowser.get() returns a Browser object whose `.name` is the
    # underlying command (xdg-open, qutebrowser, firefox, etc.).
    try:
        browser = webbrowser.get()
        cmd = getattr(browser, "name", "") or ""
        bin_path = shutil.which(cmd) if cmd else ""
        if bin_path:
            subprocess.Popen(
                [bin_path, url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return
    except Exception:
        pass
    # Fallback — leakier but always works
    webbrowser.open(url)


# ── Usage telemetry: detect when a paid upgrade is justified ──────────────────
#
# We log per-call outcomes (success / rate_limit / error / latency_ms) into a
# `cloud_usage` config row as a JSON list, capped at 200 entries. The
# `recommend_upgrade()` helper reasons over that history to decide whether
# the user would benefit from a paid tier — and which model to upgrade to.

_USAGE_KEY = "cloud_usage"
_USAGE_CAP = 200


def _read_usage() -> list[dict]:
    try:
        from .db import DB_PATH, Config, make_engine
        from sqlalchemy.orm import Session
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if not path.exists():
            return []
        engine = make_engine(path)
        with Session(engine) as s:
            row = s.get(Config, _USAGE_KEY)
            if not row or not row.value:
                return []
            data = json.loads(row.value)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_usage(events: list[dict]) -> None:
    try:
        from .db import DB_PATH, Config, make_engine
        from sqlalchemy.orm import Session
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        engine = make_engine(path)
        with Session(engine) as s:
            row = s.get(Config, _USAGE_KEY)
            payload = json.dumps(events[-_USAGE_CAP:])
            if row:
                row.value = payload
            else:
                s.add(Config(key=_USAGE_KEY, value=payload))
            s.commit()
    except Exception:
        pass


def record_event(provider: str, model: str, outcome: str,
                  latency_ms: float | None = None,
                  status_code: int | None = None,
                  detail: str = "") -> None:
    """Append a usage event. outcome ∈ {ok, rate_limit, auth_error, server_error,
    timeout, network, other}. Bounded; oldest events drop off."""
    import time
    events = _read_usage()
    events.append({
        "ts":       time.time(),
        "provider": provider, "model": model, "outcome": outcome,
        "latency_ms": float(latency_ms) if latency_ms is not None else None,
        "status_code": status_code,
        "detail":   detail[:160],
    })
    _write_usage(events)


def _classify_error(exc: Exception) -> tuple[str, int | None]:
    """Map a cloud-call exception to (outcome, status_code)."""
    import urllib.error
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        if code == 401 or code == 403:
            return ("auth_error", code)
        if code == 429:
            return ("rate_limit", code)
        if 500 <= code < 600:
            return ("server_error", code)
        return ("other", code)
    msg = str(exc).lower()
    if "timeout" in msg or "timed out" in msg:
        return ("timeout", None)
    if "connect" in msg or "refused" in msg or "name or service not known" in msg:
        return ("network", None)
    return ("other", None)


# ── Recommendation engine ────────────────────────────────────────────────────

class UpgradeRecommendation(NamedTuple):
    should_upgrade: bool
    severity:       str          # "ok" | "consider" | "recommend" | "strongly"
    reasons:        list[str]
    suggested_provider: str       # slug of provider whose paid tier to use
    suggested_models:   list[str] # paid_examples from that provider
    metrics:        dict         # raw stats for the report


def recommend_upgrade(
    fixer_top_accuracy: float | None = None,
    window_seconds: float = 7 * 24 * 3600,
) -> UpgradeRecommendation:
    """Decide if the user should upgrade to a paid cloud tier.

    Considers:
      • Recent rate-limit / server-error frequency in cloud_usage events
        (within window_seconds; default 7 days)
      • Median cloud latency (slow tiers might justify a faster paid plan)
      • Best fixer benchmark accuracy across free models (if known)

    Returns an UpgradeRecommendation with reasons + a suggested provider
    (defaults to whichever the user already configured) + that provider's
    paid_examples for use in `cloud --upgrade`.
    """
    import time
    now = time.time()
    events = [e for e in _read_usage()
              if now - float(e.get("ts", 0)) <= window_seconds]

    n = len(events)
    rate_limits  = [e for e in events if e.get("outcome") == "rate_limit"]
    server_errs  = [e for e in events if e.get("outcome") == "server_error"]
    timeouts     = [e for e in events if e.get("outcome") == "timeout"]
    successes    = [e for e in events if e.get("outcome") == "ok"]
    latencies    = [e["latency_ms"] for e in successes
                    if isinstance(e.get("latency_ms"), (int, float))]
    latencies.sort()
    median_latency = latencies[len(latencies)//2] if latencies else None

    # Read configured provider for the suggestion default
    suggested_provider = ""
    try:
        from .db import DB_PATH, Config, make_engine
        from sqlalchemy.orm import Session
        path = Path(os.environ.get("ORG_LLM_DB") or str(DB_PATH))
        if path.exists():
            engine = make_engine(path)
            with Session(engine) as s:
                row = s.get(Config, "cloud_provider")
                if row: suggested_provider = row.value or ""
    except Exception:
        pass

    if not suggested_provider:
        suggested_provider = "openrouter"   # most flexible default

    suggested_models: list[str] = []
    p = PROVIDER_MAP.get(suggested_provider)
    if p:
        suggested_models = list(p.paid_examples)

    reasons: list[str] = []
    score = 0   # higher = more urgent

    # Rate-limit pressure: any non-trivial ratio is worth flagging
    if n >= 5 and rate_limits:
        rl_ratio = len(rate_limits) / n
        if rl_ratio >= 0.20:
            reasons.append(f"Rate-limited on {len(rate_limits)}/{n} cloud calls "
                            f"in the last {int(window_seconds/86400)}d "
                            f"({rl_ratio*100:.0f}%) — paid tier removes the cap.")
            score += 3
        elif rl_ratio >= 0.05:
            reasons.append(f"Some rate limits ({len(rate_limits)}/{n}, "
                            f"{rl_ratio*100:.0f}%) — borderline; paid would be faster.")
            score += 1

    # Server / availability noise
    avail_failures = len(server_errs) + len(timeouts)
    if n >= 5 and avail_failures / n >= 0.10:
        reasons.append(f"{avail_failures}/{n} cloud calls hit server errors or "
                        "timeouts — free tiers de-prioritise during congestion.")
        score += 2

    # Latency
    if median_latency is not None and median_latency > 4000:
        reasons.append(f"Median cloud latency is {median_latency:.0f} ms — paid "
                        "endpoints (Anthropic Sonnet, OpenAI) typically <1 s.")
        score += 1

    # Fixer benchmark
    if fixer_top_accuracy is not None and fixer_top_accuracy < 0.70:
        reasons.append(f"Best free-tier fixer model scores only "
                        f"{fixer_top_accuracy*100:.0f}% on canonical fix scenarios — "
                        "paid models (Claude / GPT-5) would clear 90%+.")
        score += 3

    if score >= 5:
        severity = "strongly"
    elif score >= 3:
        severity = "recommend"
    elif score >= 1:
        severity = "consider"
    else:
        severity = "ok"

    return UpgradeRecommendation(
        should_upgrade=(score >= 1),
        severity=severity,
        reasons=reasons,
        suggested_provider=suggested_provider,
        suggested_models=suggested_models,
        metrics={
            "events":        n,
            "rate_limits":   len(rate_limits),
            "server_errors": len(server_errs),
            "timeouts":      len(timeouts),
            "successes":     len(successes),
            "median_latency_ms": median_latency,
            "score":         score,
        },
    )

# cloud.py:1 ends here
