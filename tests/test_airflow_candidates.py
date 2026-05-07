"""Agent-managed headless tests for the eleven Airflow candidates.

Each test simulates one candidate use case from
=docs/wiki/agent-time-awareness.org= § Real candidates and emits
a =Verdict= row recording which Airflow primitives the shape
genuinely exercises.  An agent (today: =@geordi=) runs::

    pytest tests/test_airflow_candidates.py -q

then narrates the per-candidate verdicts the harness collects.
The session-end fixture writes the verdict table to
=tests/_artifacts/airflow_verdicts.json= for the report doc.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.airflow_candidate_harness import (
    DAG,
    Backfill,
    Sensor,
    VERDICTS,
    Verdict,
    record,
    retry,
)


# ── 1. Cross-vault federation ────────────────────────────────────────────────


def test_01_cross_vault_federation():
    """Three vaults, each gated by a sensor, joined by a DAG."""
    online = {"household": True, "work": True, "personal": True}
    sensors = {v: Sensor(lambda v=v: online[v]) for v in online}
    dag = DAG("federation")
    for v in online:
        dag.add(f"sense_{v}", lambda r, v=v: sensors[v].wait())
    dag.add(
        "join",
        lambda r: [v for v in online if r[f"sense_{v}"]],
        deps=[f"sense_{v}" for v in online],
    )
    out = dag.run()
    assert sorted(out["join"]) == ["household", "personal", "work"]
    record(Verdict(1, "cross-vault federation", ["DAG", "Sensor"], out["join"]))


# ── 2. DB ↔ vault re-render under Phase 25 ──────────────────────────────────


def test_02_db_vault_render_phase25():
    """Detect dirty rows → render → verify; conflict triggers retry."""
    attempts = {"render": 0}

    def render(_i: int) -> int:
        attempts["render"] += 1
        if attempts["render"] < 2:
            raise RuntimeError("write conflict — vault edited mid-render")
        return 42

    dag = DAG("phase25-render")
    dag.add("detect_dirty", lambda r: [1, 2, 3])
    dag.add("render", lambda r: retry(render, attempts=3), deps=["detect_dirty"])
    dag.add("verify", lambda r: r["render"] == 42, deps=["render"])
    out = dag.run()
    assert out["verify"] is True
    assert attempts["render"] == 2
    record(Verdict(2, "DB ↔ vault re-render (Phase 25)", ["DAG", "Retry"], attempts))


# ── 3. External-data ingest fan-in ──────────────────────────────────────────


def test_03_external_ingest_fanin():
    """Six sources, each flaky in its own way; join in fan-in node."""
    sources = ["calendar", "email", "rss", "finance", "health", "weather"]
    flake = {s: 0 for s in sources}

    def fetch(s: str) -> dict[str, str]:
        def _attempt(i: int) -> dict[str, str]:
            flake[s] += 1
            if i == 0 and s in {"email", "finance"}:
                raise RuntimeError(f"{s} 503")
            return {s: "ok"}

        sense = Sensor(lambda s=s: True)
        assert sense.wait()
        return retry(_attempt, attempts=3)

    dag = DAG("ingest")
    for s in sources:
        dag.add(f"fetch_{s}", lambda r, s=s: fetch(s))
    dag.add(
        "fanin",
        lambda r: {k: v for s in sources for k, v in r[f"fetch_{s}"].items()},
        deps=[f"fetch_{s}" for s in sources],
    )
    out = dag.run()
    assert set(out["fanin"]) == set(sources)
    assert flake["email"] == 2 and flake["finance"] == 2  # both retried once
    record(Verdict(3, "external-data ingest fan-in", ["DAG", "Sensor", "Retry"], list(out["fanin"])))


# ── 4. Cross-app egress (org → dbt → Superset → digest) ─────────────────────


def test_04_cross_app_egress():
    """Linear DAG: extract → dbt build → publish → email/push."""
    dag = DAG("egress")
    dag.add("extract_org", lambda r: ["row1", "row2"])
    dag.add("dbt_build", lambda r: {"rows": len(r["extract_org"])}, deps=["extract_org"])
    dag.add("publish_superset", lambda r: f"chart://rows-{r['dbt_build']['rows']}", deps=["dbt_build"])
    dag.add(
        "send_digest",
        lambda r: {"to": "user@host", "url": r["publish_superset"]},
        deps=["publish_superset"],
    )
    out = dag.run()
    assert out["send_digest"]["url"] == "chart://rows-2"
    record(Verdict(4, "cross-app egress (org → dbt → Superset → digest)", ["DAG"], out["send_digest"]))


# ── 5. Heavy daily snapshot rolls ───────────────────────────────────────────


def test_05_heavy_snapshot_rolls():
    """Multi-source snapshot with retries on each source."""
    sources = [f"src_{i}" for i in range(5)]
    counts: dict[str, int] = {}

    def snap(s: str) -> int:
        def _attempt(i: int) -> int:
            if i == 0 and s == "src_3":
                raise RuntimeError("src_3 timeout")
            return 100 + int(s[-1])

        return retry(_attempt, attempts=2)

    dag = DAG("snap")
    for s in sources:
        dag.add(f"snap_{s}", lambda r, s=s: snap(s))
    dag.add(
        "roll",
        lambda r: sum(r[f"snap_{s}"] for s in sources),
        deps=[f"snap_{s}" for s in sources],
    )
    out = dag.run()
    assert out["roll"] == sum(100 + i for i in range(5))
    record(Verdict(5, "heavy daily snapshot rolls", ["DAG", "Retry"], out["roll"]))


# ── 6. Agor multi-day campaign orchestration ────────────────────────────────


def test_06_agor_campaign():
    """research → spec → impl → test → review with retry on impl flake."""
    impl_attempts = {"n": 0}

    def impl(_i: int) -> str:
        impl_attempts["n"] += 1
        if impl_attempts["n"] < 3:
            raise RuntimeError("model returned malformed JSON")
        return "code-v3"

    dag = DAG("agor-campaign")
    dag.add("research", lambda r: "findings")
    dag.add("spec", lambda r: f"spec[{r['research']}]", deps=["research"])
    dag.add("impl", lambda r: retry(impl, attempts=4), deps=["spec"])
    dag.add("test", lambda r: r["impl"] == "code-v3", deps=["impl"])
    dag.add("review", lambda r: "approved" if r["test"] else "blocked", deps=["test"])
    out = dag.run()
    assert out["review"] == "approved"
    assert impl_attempts["n"] == 3
    record(Verdict(6, "Agor multi-day campaign", ["DAG", "Retry"], out["review"]))


# ── 7. Phase 29 — self-coded tools runtime ──────────────────────────────────


def test_07_self_coded_tools_runtime():
    """Mass scheduling of N promoted tools, each with retry on flake."""
    tools = [f"tool_{i:02d}" for i in range(20)]
    runs: dict[str, str] = {}
    flake = {t: 0 for t in tools}

    def call(t: str) -> str:
        def _attempt(i: int) -> str:
            flake[t] += 1
            if i == 0 and t.endswith("_07"):
                raise RuntimeError("flake")
            return "ok"

        return retry(_attempt, attempts=2)

    for t in tools:
        runs[t] = call(t)
    assert all(v == "ok" for v in runs.values())
    assert flake["tool_07"] == 2
    record(
        Verdict(
            7,
            "Phase 29 — self-coded tools runtime",
            ["Retry"],
            f"{len(tools)} tools, 1 flake retried",
            notes="DAG not strictly required; observability + retry are the load-bearing primitives.",
        )
    )


# ── 8. Embedding refresh + vault re-index ───────────────────────────────────


def test_08_embedding_refresh_backfill():
    """Backfill embedding regen across the past 30 day-partitions."""
    days = [f"2026-04-{d:02d}" for d in range(1, 31)]
    bf = Backfill(lambda d: {"day": d, "vec_dim": 768})
    out = bf.replay(days)
    assert len(out) == 30
    assert out["2026-04-15"]["vec_dim"] == 768
    record(Verdict(8, "embedding refresh + vault re-index", ["Backfill"], f"{len(out)} day-partitions replayed"))


# ── 9. Multi-device fleet coordination ──────────────────────────────────────


def test_09_multi_device_fleet():
    """Laptop nightly with phone fallback when laptop offline."""
    state = {"laptop": False, "phone": True}
    laptop_sensor = Sensor(lambda: state["laptop"], max_polls=2)
    phone_sensor = Sensor(lambda: state["phone"], max_polls=2)

    dag = DAG("fleet")
    dag.add("try_laptop", lambda r: laptop_sensor.wait())
    dag.add(
        "fallback_phone",
        lambda r: phone_sensor.wait() if not r["try_laptop"] else False,
        deps=["try_laptop"],
    )
    dag.add(
        "result",
        lambda r: "laptop" if r["try_laptop"] else ("phone" if r["fallback_phone"] else "none"),
        deps=["try_laptop", "fallback_phone"],
    )
    out = dag.run()
    assert out["result"] == "phone"
    record(Verdict(9, "multi-device fleet coordination", ["DAG", "Sensor"], out["result"]))


# ── 10. Provenance / vault-history backfill ─────────────────────────────────


def test_10_provenance_backfill():
    """Replay an enrichment pipeline over historical commit hashes."""
    commits = [f"deadbeef{i:02d}" for i in range(12)]
    bf = Backfill(lambda c: {"commit": c, "enriched": True})
    out = bf.replay(commits)
    assert len(out) == len(commits)
    assert all(v["enriched"] for v in out.values())
    record(Verdict(10, "provenance / vault-history backfill", ["Backfill"], f"{len(out)} commits replayed"))


# ── 11. A/B / multivariate fan-out at scale ─────────────────────────────────


def test_11_ab_fanout_at_scale():
    """One fan-out task spawns N variant judges; collect verdicts."""
    variants = [f"variant_{i}" for i in range(8)]
    dag = DAG("ab-fanout")
    dag.add("seed", lambda r: variants)
    for v in variants:
        dag.add(f"judge_{v}", lambda r, v=v: {"variant": v, "score": hash(v) % 100}, deps=["seed"])
    dag.add(
        "rank",
        lambda r: sorted(
            (r[f"judge_{v}"] for v in variants),
            key=lambda d: d["score"],
            reverse=True,
        ),
        deps=[f"judge_{v}" for v in variants],
    )
    out = dag.run()
    assert len(out["rank"]) == len(variants)
    assert out["rank"][0]["score"] >= out["rank"][-1]["score"]
    record(Verdict(11, "A/B / multivariate fan-out at scale", ["DAG"], f"top variant: {out['rank'][0]['variant']}"))


# ── Verdict dump (agent-readable artifact) ──────────────────────────────────


@pytest.fixture(scope="module", autouse=True)
def _dump_verdicts_at_end():
    yield
    if not VERDICTS:
        return
    artifacts = Path(__file__).parent / "_artifacts"
    artifacts.mkdir(exist_ok=True)
    out = artifacts / "airflow_verdicts.json"
    out.write_text(
        json.dumps(
            [
                {
                    "n": v.n,
                    "name": v.name,
                    "primitives": v.primitives,
                    "sample_output": _coerce(v.sample_output),
                    "notes": v.notes,
                }
                for v in sorted(VERDICTS, key=lambda v: v.n)
            ],
            indent=2,
        )
    )


def _coerce(x):
    """Best-effort JSON-friendly coercion."""
    try:
        json.dumps(x)
        return x
    except TypeError:
        return repr(x)
