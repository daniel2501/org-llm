// @ts-nocheck
//
// Same JSX-runtime/types reconciliation note as src/slots.tsx applies:
// tsconfig sets jsxImportSource: "solid-js" because @opentui/solid 0.2.0
// ships only types for jsx-runtime. solid-js's IntrinsicElements is
// DOM-shaped so opentui props (fg, ascii_font) type-error; runtime is
// fine because opencode's TUI host wires opentui's Solid renderer at
// launch.
//
/**
 * @org-llm/opencode-plugin — sidebar status panel (Phase 17).
 *
 * Replaces opencode's default `sidebar_content` widget set with a
 * live org-llm panel:
 *   • Vault counts (nodes, embedded %, last gather)
 *   • Active palette + knob mix
 *   • MCP server status + tool count
 *   • Hardware probe (free RAM / VRAM)
 *   • Recent SensorLog alerts (last 5; only alert/critical-level)
 *   • Common feature links: doctor / library / insights / wiki
 *
 * Reads .opencode/sidebar-status.json (written by `org-llm launch`,
 * re-written by the auto-embedder daemon on every cycle so the panel
 * stays fresh without a relaunch).
 *
 * Layout assumes opencode's sidebar width (~30 cols). Long labels
 * truncate at 28 chars; numbers right-align via spacing.
 */

import { createSignal, onCleanup } from "solid-js";

interface VaultStats {
  n_files?: number;
  n_nodes?: number;
  n_embedded?: number;
  pct_embedded?: number;
  org_dir?: string;
}

interface KnobLevel {
  name: string;
  level: number;
}

interface SensorAlert {
  ts: number;
  probe: string;
  status: string;
  message: string;
}

interface FeatureLink {
  name: string;
  title: string;
  slash: string;
  hint: string;
}

interface SidebarStatus {
  generated_at?: number;
  workspace?: string;
  vault?: VaultStats;
  active?: { palette?: string; knobs?: KnobLevel[] };
  mcp?: { server?: string; tool_count?: number; configured?: boolean };
  hardware?: { free_ram_gb?: number; vram_gb?: number | null };
  sensors?: { recent_alerts?: SensorAlert[] };
  links?: FeatureLink[];
}

const REFRESH_INTERVAL_MS = 15_000;

async function loadStatus(directory: string): Promise<SidebarStatus> {
  const path = `${directory}/.opencode/sidebar-status.json`;
  try {
    const file = Bun.file(path);
    if (!(await file.exists())) return {};
    return (await file.json()) as SidebarStatus;
  } catch {
    return {};
  }
}

function fmtAge(ts: number | undefined): string {
  if (!ts) return "—";
  const ageSec = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (ageSec < 60) return `${ageSec}s ago`;
  if (ageSec < 3600) return `${Math.floor(ageSec / 60)}m ago`;
  if (ageSec < 86400) return `${Math.floor(ageSec / 3600)}h ago`;
  return `${Math.floor(ageSec / 86400)}d ago`;
}

function statusFg(status: string): string {
  switch (status) {
    case "critical": return "error";
    case "alert":    return "warning";
    case "watch":    return "info";
    default:          return "textMuted";
  }
}

export function registerSidebar(api: TuiPluginApi): void {
  const directory = api.state.path.directory;

  // Reactive state — Solid signal so JSX re-renders when status
  // refreshes from the file.
  const [status, setStatus] = createSignal<SidebarStatus>({});

  // Initial load + refresh tick. The interval is light (15s) and
  // only file I/O — opencode's bun runtime handles the async fine.
  void loadStatus(directory).then(setStatus);
  const tick = setInterval(() => {
    void loadStatus(directory).then(setStatus);
  }, REFRESH_INTERVAL_MS);

  // Lifecycle: clean up the interval when the plugin disposes
  // (avoid leaking ticks across opencode reloads). lifecycle.onDispose
  // exists per @opencode-ai/plugin/tui.d.ts.
  api.lifecycle?.onDispose?.(() => clearInterval(tick));

  api.slots.register({
    order: 1000,
    slots: {
      sidebar_content: () => {
        const s = status();
        const v = s.vault ?? {};
        const a = s.active ?? {};
        const m = s.mcp ?? {};
        const h = s.hardware ?? {};
        const alerts = s.sensors?.recent_alerts ?? [];
        const links = s.links ?? [];

        return (
          <box flexDirection="column" paddingTop={1} paddingLeft={1} paddingRight={1}>
            {/* ── Vault block ────────────────────────────── */}
            <text fg="primary">▌ vault</text>
            <text>
              <text fg="textMuted">  nodes  </text>
              <text>{v.n_nodes ?? "—"}</text>
            </text>
            <text>
              <text fg="textMuted">  embed  </text>
              <text>{v.pct_embedded ?? "—"}%</text>
              <text fg="textMuted">  ({v.n_embedded ?? "—"}/{v.n_nodes ?? "—"})</text>
            </text>
            <text>
              <text fg="textMuted">  files  </text>
              <text>{v.n_files ?? "—"}</text>
            </text>
            <text>
              <text fg="textMuted">  fresh  </text>
              <text fg="info">{fmtAge(s.generated_at)}</text>
            </text>

            {/* ── Active block ───────────────────────────── */}
            <text fg="primary">{"\n"}▌ active</text>
            <text>
              <text fg="textMuted">  palette </text>
              <text fg="secondary">{a.palette ?? "classic"}</text>
            </text>
            {(a.knobs ?? []).slice(0, 4).map((k) => (
              <text>
                <text fg="textMuted">  {k.name.padEnd(8)}</text>
                <text fg="accent">{"·".repeat(k.level)}</text>
                <text fg="textMuted">{"·".repeat(Math.max(0, 3 - k.level))}</text>
              </text>
            ))}

            {/* ── MCP + Hardware ─────────────────────────── */}
            <text fg="primary">{"\n"}▌ mcp / hw</text>
            <text>
              <text fg="textMuted">  tools  </text>
              <text fg={m.configured ? "success" : "warning"}>
                {m.tool_count ?? "—"}
              </text>
            </text>
            {h.free_ram_gb != null && (
              <text>
                <text fg="textMuted">  ram    </text>
                <text>{h.free_ram_gb}gb free</text>
              </text>
            )}
            {h.vram_gb != null && (
              <text>
                <text fg="textMuted">  vram   </text>
                <text>{h.vram_gb}gb</text>
              </text>
            )}

            {/* ── Recent sensor alerts ───────────────────── */}
            {alerts.length > 0 && (
              <>
                <text fg="primary">{"\n"}▌ alerts</text>
                {alerts.slice(0, 3).map((alert) => (
                  <text>
                    <text fg={statusFg(alert.status)}>
                      ▲ {alert.probe.padEnd(8)}
                    </text>
                    <text fg="textMuted">{fmtAge(alert.ts)}</text>
                  </text>
                ))}
              </>
            )}

            {/* ── Common feature links ───────────────────── */}
            <text fg="primary">{"\n"}▌ jump</text>
            {links.map((link) => (
              <text>
                <text fg="accent">{link.slash.padEnd(11)}</text>
                <text fg="textMuted">{link.title.split(" — ")[1] ?? link.title}</text>
              </text>
            ))}
          </box>
        );
      },
    },
  } as unknown as Parameters<typeof api.slots.register>[0]);
}
