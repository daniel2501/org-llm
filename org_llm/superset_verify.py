"""Headless-render verification for org-llm Superset charts.

Renders every chart in a running Superset via Playwright + headless
Chromium, scans the rendered DOM for known error markers, and
returns a per-chart pass/fail report.

Why this exists
---------------

The chart-data API can return HTTP 200 with a clean SQL result while
the React/echarts client still fails to render — wrong form_data
shape (e.g. heatmap_v2 needing singular `metric` + explicit `x_axis`
instead of `metrics: [...]`), wrong viz_type for the data shape,
control-panel required fields missing. Pure API-level checks miss
this whole class of bug. The 2026-05-07 chart-redesign round shipped
two of these silently before getting caught visually.

This module shells out to a small Playwright runner *inside the
Superset venv* (which already has Playwright + Chromium for the
upstream thumbnail/screenshot endpoints) so org-llm itself doesn't
take a 200 MB dependency. Asymmetric per the hosting proposal —
nothing is pulled at base install; verify only runs if Superset is
already installed.

CLI: org-llm superset verify [--url URL] [--user U] [--password P]
                              [--dashboard SLUG] [--screenshots DIR]
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_SUPERSET_VENV = Path.home() / ".local" / "share" / "org-llm" / "superset-venv"

# DOM substrings Superset injects when a chart can't render. Some come
# from echarts-plugin error states, some from Superset's wrapper
# components. Grow this list as new failure modes surface.
ERROR_MARKERS: tuple[str, ...] = (
    "Data Error",
    "Add required control values",
    "Cannot read properties of undefined",
    "Datetime column not provided",
    "Unexpected error",
    "An error occurred",
    "rendering failed",
    "There is no data",
)


class SupersetVenvNotFound(RuntimeError):
    """Raised when the Superset venv with Playwright isn't reachable."""


@dataclass(frozen=True)
class ChartResult:
    chart_id: int
    name: str
    error: str  # empty string = passed; non-empty = the marker that fired
    screenshot_path: Optional[str] = None

    @property
    def ok(self) -> bool:
        return not self.error


def _runner_script() -> str:
    """The Playwright runner executed inside the Superset venv.

    Kept as a string so the only thing the venv needs is the
    Playwright module — no relative imports, no org-llm package on
    sys.path. Reads (url, user, pass, dashboard, screenshot_dir,
    timeout_ms) from argv.
    """
    return r"""
import asyncio, json, os, re, sys
import requests
from playwright.async_api import async_playwright

BASE = sys.argv[1]
USER = sys.argv[2]
PASS = sys.argv[3]
DASH_SLUG = sys.argv[4] or None        # "" → all dashboards
SHOT_DIR = sys.argv[5] or None         # "" → no screenshots
TIMEOUT_MS = int(sys.argv[6])
ERROR_MARKERS = json.loads(sys.argv[7])

if SHOT_DIR:
    os.makedirs(SHOT_DIR, exist_ok=True)

# Form-POST login (the API /security/login Bearer token works for
# JSON endpoints but not the SPA explore page; SPA needs a session
# cookie). Get CSRF from GET /login/ then POST it.
sess = requests.Session()
r = sess.get(f"{BASE}/login/", timeout=10)
csrf_match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', r.text)
csrf = csrf_match.group(1) if csrf_match else ""
sess.post(
    f"{BASE}/login/",
    data={"username": USER, "password": PASS, "csrf_token": csrf},
    allow_redirects=True,
    timeout=10,
)
cookies = [
    {"name": c.name, "value": c.value, "domain": "127.0.0.1", "path": "/"}
    for c in sess.cookies
]

# Bearer token for API calls (chart list / dashboard list).
token_resp = sess.post(
    f"{BASE}/api/v1/security/login",
    json={"username": USER, "password": PASS, "provider": "db", "refresh": True},
    timeout=10,
).json()
TOKEN = token_resp["access_token"]
hdrs = {"Authorization": f"Bearer {TOKEN}"}

# Build the list of charts to verify. Filter to one dashboard if
# DASH_SLUG is set; otherwise verify every chart in the workspace.
if DASH_SLUG:
    charts = sess.get(
        f"{BASE}/api/v1/dashboard/{DASH_SLUG}/charts", headers=hdrs, timeout=10
    ).json()["result"]
else:
    charts = sess.get(
        f"{BASE}/api/v1/chart/?q=(page_size:200)", headers=hdrs, timeout=10
    ).json()["result"]


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        results = []
        for c in charts:
            cid = c.get("id") or c.get("slice_id")
            name = c.get("slice_name") or c.get("name") or f"chart-{cid}"
            url = f"{BASE}/explore/?slice_id={cid}&standalone=1"
            err = ""
            try:
                await page.goto(url, wait_until="networkidle", timeout=TIMEOUT_MS)
                await page.wait_for_timeout(2500)
                txt = await page.content()
                for m in ERROR_MARKERS:
                    if m in txt:
                        err = m
                        break
            except Exception as e:
                err = f"navigation failed: {type(e).__name__}: {str(e)[:120]}"

            shot_path = ""
            if SHOT_DIR:
                shot_path = os.path.join(SHOT_DIR, f"chart-{cid:03d}.png")
                try:
                    await page.screenshot(path=shot_path)
                except Exception:
                    shot_path = ""

            results.append({
                "chart_id": cid,
                "name": name,
                "error": err,
                "screenshot_path": shot_path or None,
            })
        await browser.close()
    return results


print(json.dumps(asyncio.run(main())))
"""


def _superset_python(venv: Path) -> Path:
    """Resolve the Python interpreter inside the Superset venv."""
    candidate = venv / "bin" / "python"
    if not candidate.exists():
        raise SupersetVenvNotFound(
            f"Superset venv not found at {venv}. Set ORG_LLM_SUPERSET_VENV "
            f"or install Superset per docs/wiki/superset.org § Hosting & install."
        )
    return candidate


def verify(
    url: str = "http://localhost:8088",
    user: str = "admin",
    password: str = "admin",
    dashboard_slug: Optional[str] = None,
    screenshot_dir: Optional[Path] = None,
    timeout_ms: int = 30_000,
    venv: Optional[Path] = None,
) -> list[ChartResult]:
    """Render every chart in Superset and return per-chart results.

    Raises SupersetVenvNotFound if the Playwright-equipped Superset
    venv isn't reachable. All other failures (login, API, navigation)
    are captured per-chart in ChartResult.error so the report is
    always actionable.
    """
    venv = venv or Path(
        os.environ.get("ORG_LLM_SUPERSET_VENV", str(DEFAULT_SUPERSET_VENV))
    )
    py = _superset_python(venv)

    argv = [
        str(py),
        "-c",
        _runner_script(),
        url,
        user,
        password,
        dashboard_slug or "",
        str(screenshot_dir) if screenshot_dir else "",
        str(timeout_ms),
        json.dumps(list(ERROR_MARKERS)),
    ]
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout_ms / 1000 * 12
    )
    if proc.returncode != 0:
        # Surface the runner's stderr so the caller sees auth / API
        # failures explicitly instead of an empty result list.
        raise RuntimeError(
            f"verify runner exited {proc.returncode}: "
            f"{proc.stderr.strip()[:500]}"
        )
    raw = json.loads(proc.stdout)
    return [
        ChartResult(
            chart_id=r["chart_id"],
            name=r["name"],
            error=r["error"],
            screenshot_path=r.get("screenshot_path"),
        )
        for r in raw
    ]


def superset_venv_present(venv: Optional[Path] = None) -> bool:
    """Quick check used by the CLI before invoking verify, so the
    error message is friendlier than a stack trace."""
    venv = venv or Path(
        os.environ.get("ORG_LLM_SUPERSET_VENV", str(DEFAULT_SUPERSET_VENV))
    )
    return (venv / "bin" / "python").exists()
