// @ts-nocheck
//
// Same JSX-runtime / @ts-nocheck rationale as src/slots.tsx + panel.tsx.
//
/**
 * @org-llm/opencode-plugin — route-preview ghost text (Stage 1).
 *
 * Renders a one-line "what would happen if I submit this right
 * now?" hint inside the <Prompt hint={…}> slot. Polls the prompt
 * input at ~80ms; scores against routes.json (emitted by
 * cli.py:_emit_routes_json at launch). The plugin is a renderer;
 * Python remains authoritative.
 *
 * NOT autocomplete — there's no acceptance gesture in Stage 1.
 * Tab/right-arrow accept needs an upstream opencode PR adding
 * `onInput` + a Prompt-scoped key hook. Stage 1 is purely passive.
 *
 * Why static imports (per the panel.tsx memory rule): the bun ESM
 * runtime opencode uses doesn't support `require()`. Adjacent file
 * panel.tsx already uses solid-js this way safely; we mirror.
 */

import {
  readFileSync as _readFileSync,
} from "node:fs";
import {
  createSignal as _createSignal,
  onMount     as _onMount,
  onCleanup   as _onCleanup,
} from "solid-js";

// ── Types (mirror cli.py:_emit_routes_json payload shape) ─────────

interface AgentRoute {
  birth_name:  string;
  aliases:     string[];
  triggers:    string[];
  model_role:  string;
  pack:        string;
  description: string;
}

interface RecipeRoute {
  name:    string;
  pattern: string;   // regex source (case-insensitive at scoring time)
  target:  string;
  tier:    string;
}

interface PrefixRoute {
  token:  string;
  effect: string;
}

interface Routes {
  generated_at: number;
  agents:       AgentRoute[];
  recipes:      RecipeRoute[];
  prefixes:     PrefixRoute[];
  slashes:      string[];
}

type Preview =
  | { kind: "empty" }
  | { kind: "agent";  agent: AgentRoute; via: "explicit" | "trigger";
                       matchedTrigger?: string; score: number }
  | { kind: "recipe"; recipe: RecipeRoute }
  | { kind: "prefix"; prefix: PrefixRoute }
  | { kind: "slash";  command: string }
  | { kind: "default"; reason: string };

// ── Routes loader (one-shot at mount; routes.json is launch-time) ──

let _orgLlmDiskWarned = false;
function _diskWarn(file: string, err: unknown): void {
  if (_orgLlmDiskWarned) return;
  _orgLlmDiskWarned = true;
  try {
    process.stderr.write(
      `[org-llm] route-preview disk read failed (${file}): ${
        (err as any)?.message ?? err}\n`);
  } catch { /* */ }
}

function readRoutes(): Routes | null {
  try {
    const home   = process.env.HOME ?? "";
    const orgDir = process.env.ORG_LLM_ORG_DIR ?? `${home}/org`;
    const path   = `${orgDir}/.opencode/routes.json`;
    return JSON.parse(_readFileSync(path, "utf8"));
  } catch (err) {
    _diskWarn("routes.json", err);
    return null;
  }
}

// ── Scoring (mirrors cli.py:route_prompt + proxy chain order) ──────

function findAgent(name: string, agents: AgentRoute[]): AgentRoute | null {
  const lc = name.toLowerCase();
  for (const a of agents) {
    if (a.birth_name.toLowerCase() === lc) return a;
    for (const al of a.aliases) {
      if (al.toLowerCase() === lc) return a;
    }
  }
  return null;
}

function scoreInput(input: string, routes: Routes): Preview {
  const trimmed = input.trim();
  if (!trimmed) return { kind: "empty" };

  // 1. typed prefixes (longest token first to avoid `?:` shadowing
  //    `??:explain` — which the proxy chain orders explicitly).
  const sortedPrefixes = [...routes.prefixes].sort(
    (a, b) => b.token.length - a.token.length);
  for (const p of sortedPrefixes) {
    if (p.token === "@" || p.token === "~") {
      // Special-cased below — these need char-class checks, not
      // a bare prefix match.
      continue;
    }
    if (trimmed.startsWith(p.token)) return { kind: "prefix", prefix: p };
  }

  // 2. slash commands (head-token match)
  if (trimmed.startsWith("/")) {
    const head = trimmed.slice(1).split(/\s/)[0];
    const exact = routes.slashes.find(s => s === head);
    if (exact) return { kind: "slash", command: exact };
    const partial = routes.slashes.find(s => s.startsWith(head));
    if (partial) return { kind: "slash", command: partial };
  }

  // 3. explicit @<agent>
  const at = trimmed.match(/^@([\w-]+)/);
  if (at) {
    const agent = findAgent(at[1], routes.agents);
    if (agent) {
      return {
        kind: "agent", agent, via: "explicit",
        score: Number.POSITIVE_INFINITY,
      };
    }
    // unknown @<name> — fall through; user will see "no route".
  }

  // 4. ~<n> replay prefix
  if (/^~\d+\b/.test(trimmed)) {
    const p = routes.prefixes.find(x => x.token === "~");
    if (p) return { kind: "prefix", prefix: p };
  }

  // 5. recipe regex (pre-fetch wins over agent triggers — matches
  //    proxy order where intercept_agent_prefix runs match_recipe
  //    before falling through to trigger-routing).
  for (const r of routes.recipes) {
    try {
      if (new RegExp(r.pattern, "i").test(trimmed)) {
        return { kind: "recipe", recipe: r };
      }
    } catch { /* malformed regex from upstream — skip */ }
  }

  // 6. trigger-substring scoring (case-insensitive count;
  //    tie-break by declaration order in routes.agents).
  const lower = trimmed.toLowerCase();
  let best: { agent: AgentRoute; score: number; matched: string } | null = null;
  for (const a of routes.agents) {
    let score = 0;
    let firstMatched = "";
    for (const t of a.triggers) {
      if (lower.includes(t.toLowerCase())) {
        score++;
        if (!firstMatched) firstMatched = t;
      }
    }
    if (score > 0 && (!best || score > best.score)) {
      best = { agent: a, score, matched: firstMatched };
    }
  }
  if (best) {
    return {
      kind: "agent", agent: best.agent, via: "trigger",
      matchedTrigger: best.matched, score: best.score,
    };
  }

  return { kind: "default", reason: "no trigger match" };
}

// ── Hint rendering (one line; theme-coloured) ──────────────────────

function renderHint(p: Preview): string {
  switch (p.kind) {
    case "empty":
      return "";
    case "agent": {
      const handle = p.agent.aliases[0] ?? p.agent.birth_name;
      const trail = p.via === "trigger"
        ? `  via "${p.matchedTrigger}" (${p.score})`
        : "";
      return `→ @${handle} (${p.agent.birth_name})${trail}`;
    }
    case "recipe":
      return `→ recipe ${p.recipe.name}  pre-fetch · no LLM call`;
    case "prefix":
      return `→ prefix ${p.prefix.token}  ${p.prefix.effect}`;
    case "slash":
      return `→ slash /${p.command}  local`;
    case "default":
      return `→ no route — default @crew`;
    default:
      return "";
  }
}

// ── Component ──────────────────────────────────────────────────────

/**
 * RoutePreview — Solid component to mount inside <Prompt hint={...}>.
 *
 * Args:
 *   getRef  — function returning the current TuiPromptRef; same
 *             pattern as auto-session.tsx's getPromptRef.
 *   theme   — opencode's resolved theme (ctx.theme.current). Used
 *             for fg/dim coloring.
 *
 * Behaviour:
 *   - Reads routes.json once at mount (launch-time emission).
 *   - Polls ref.current.input at 80ms; only re-scores on input
 *     change (debounced by string equality).
 *   - Skips polling when ref is unfocused (no UI change needed).
 *   - Renders empty when input is empty (so the prompt looks clean
 *     before the user types).
 */
export function RoutePreview(props: {
  getRef: () => any;
  theme:  any;
}): any {
  let routes: Routes | null = null;
  let lastInput = "";   // sentinel — guaranteed != any real input
  const [hint, setHint] = _createSignal<string>("");

  const tick = () => {
    if (!routes) return;
    const ref = props.getRef?.();
    if (!ref) return;
    // Only update when focused — saves cycles on dialog/help screens.
    if (typeof ref.focused === "boolean" && !ref.focused) return;
    const input = ref.current?.input ?? "";
    if (input === lastInput) return;
    lastInput = input;
    setHint(renderHint(scoreInput(input, routes)));
  };

  _onMount(() => {
    routes = readRoutes();
    if (!routes) return;
    tick();
    const id = setInterval(tick, 80);
    _onCleanup(() => clearInterval(id));
  });

  return (
    <text fg={props.theme?.textMuted ?? props.theme?.dim ?? "#888888"}>
      {hint()}
    </text>
  );
}
