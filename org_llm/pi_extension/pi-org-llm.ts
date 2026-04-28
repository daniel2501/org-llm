// pi-org-llm.ts — bridge org-llm's MCP server into Pi's extension API.
//
// What it does:
//   1. Spawns `org-llm mcp` as a child process over stdio.
//   2. Speaks the JSON-RPC 2.0 / MCP 2024-11-05 protocol on the wire.
//   3. Discovers the server's tools (tools/list) and registers each one
//      with Pi via pi.registerTool() — including the JSON Schema, so
//      Pi's typed tool-call narrowing works.
//   4. Forwards Pi tool-call args → MCP tools/call, joins the
//      content[] array into a single string for Pi.
//   5. Subscribes to MCP notifications/progress so a long-running tool
//      (org_llm_index_vault, embed_pending, dbt_build, …) streams its
//      progress through Pi's per-call onProgress callback.
//   6. Injects the MCP server's `instructions` field into the system
//      prompt PER TURN via before_agent_start — opencode only reads
//      it once at launch, so this is one of the wins we get from Pi.
//   7. Surfaces a status-line + a small widget showing the number of
//      tools loaded + the last activity timestamp.
//   8. Registers a /org-llm-tools slash command so the user can list
//      what's available without scrolling.
//
// Use:
//   pi -e ~/.pi/extensions/pi-org-llm.ts
// or stack with other extensions:
//   pi -e ~/.pi/extensions/pi-org-llm.ts -e ~/.pi/extensions/foo.ts
//
// Environment overrides:
//   ORG_LLM_BIN          path to the org-llm binary (default: PATH lookup)
//   ORG_LLM_DB           passed through to the MCP server unchanged
//   ORG_LLM_PI_DEBUG     "1" to log every JSON-RPC frame to stderr
//
// Pi's extension API is fluid; we use optional chaining everywhere so
// any host method that doesn't exist in your Pi version no-ops cleanly
// rather than crashing at startup.

import { spawn, ChildProcess } from "node:child_process";
import { createInterface } from "node:readline";

// ─── JSON-RPC types ───────────────────────────────────────────────────────────

type JsonRpcId = number | string;

interface JsonRpcRequest {
  jsonrpc: "2.0";
  id: JsonRpcId;
  method: string;
  params?: unknown;
}

interface JsonRpcResponse {
  jsonrpc: "2.0";
  id: JsonRpcId;
  result?: unknown;
  error?: { code: number; message: string; data?: unknown };
}

interface JsonRpcNotification {
  jsonrpc: "2.0";
  method: string;
  params?: unknown;
}

interface MCPTool {
  name: string;
  description?: string;
  inputSchema?: unknown;
}

interface ProgressEvent {
  progressToken?: string | number;
  progress?: number;
  total?: number;
  message?: string;
}

interface PendingCall {
  resolve: (value: unknown) => void;
  reject: (err: Error) => void;
}

// ─── The extension ───────────────────────────────────────────────────────────

export default async function orgLlmExtension(pi: any, ctx: any) {
  const debug = process.env.ORG_LLM_PI_DEBUG === "1";
  const log = (...args: unknown[]) => {
    if (debug) console.error("[pi-org-llm]", ...args);
  };

  // 1. Spawn org-llm mcp as the JSON-RPC peer.
  const bin = process.env.ORG_LLM_BIN || "org-llm";
  let proc: ChildProcess;
  try {
    proc = spawn(bin, ["mcp"], {
      stdio: ["pipe", "pipe", "inherit"],
      env: process.env,
    });
  } catch (err) {
    ctx.ui?.notify?.(
      `pi-org-llm: failed to spawn ${bin}: ${(err as Error).message}`,
    );
    return;
  }

  let serverDead = false;
  proc.on("exit", (code) => {
    serverDead = true;
    log(`server exited with code ${code}`);
    ctx.ui?.notify?.(`pi-org-llm: org-llm mcp exited (code ${code})`);
  });
  proc.on("error", (err) => {
    serverDead = true;
    ctx.ui?.notify?.(`pi-org-llm: server error: ${err.message}`);
  });

  // 2. JSON-RPC client over newline-delimited stdio (MCP 2024-11-05).
  let nextId = 1;
  const pending = new Map<JsonRpcId, PendingCall>();
  const progressSubscribers = new Map<string, (e: ProgressEvent) => void>();

  const reader = createInterface({
    input: proc.stdout!,
    crlfDelay: Infinity,
  });
  reader.on("line", (line: string) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    let msg: any;
    try {
      msg = JSON.parse(trimmed);
    } catch {
      // Some MCP servers print non-JSON to stdout when misconfigured.
      log("non-JSON line:", trimmed.slice(0, 200));
      return;
    }
    log("←", msg);
    if ("id" in msg && (msg.result !== undefined || msg.error !== undefined)) {
      const r = msg as JsonRpcResponse;
      const p = pending.get(r.id);
      if (!p) {
        log("orphan response", r.id);
        return;
      }
      pending.delete(r.id);
      if (r.error)
        p.reject(
          new Error(`MCP ${r.error.code}: ${r.error.message ?? "error"}`),
        );
      else p.resolve(r.result);
      return;
    }
    // Notification handling
    const n = msg as JsonRpcNotification;
    if (n.method === "notifications/progress") {
      const params = (n.params as ProgressEvent) || {};
      const token = String(params.progressToken ?? "");
      const sub = progressSubscribers.get(token);
      sub?.(params);
      return;
    }
    if (n.method === "notifications/message") {
      const data = (n.params as any)?.data;
      if (data) ctx.ui?.notify?.(`org-llm: ${String(data).slice(0, 200)}`);
      return;
    }
    log("unhandled notification:", n.method);
  });

  function send(method: string, params?: unknown): Promise<any> {
    return new Promise((resolve, reject) => {
      if (serverDead) {
        reject(new Error("org-llm mcp server is not running"));
        return;
      }
      const id = nextId++;
      pending.set(id, { resolve, reject });
      const frame: JsonRpcRequest = { jsonrpc: "2.0", id, method, params };
      log("→", frame);
      proc.stdin!.write(JSON.stringify(frame) + "\n");
    });
  }

  function notify(method: string, params?: unknown): void {
    if (serverDead) return;
    const frame: JsonRpcNotification = { jsonrpc: "2.0", method, params };
    log("→ notify", frame);
    proc.stdin!.write(JSON.stringify(frame) + "\n");
  }

  // 3. MCP handshake.
  let initResult: any;
  try {
    initResult = await send("initialize", {
      protocolVersion: "2024-11-05",
      capabilities: { roots: {}, sampling: {} },
      clientInfo: { name: "pi-org-llm", version: "0.1.0" },
    });
    notify("notifications/initialized");
  } catch (err) {
    ctx.ui?.notify?.(
      `pi-org-llm: handshake failed: ${(err as Error).message}`,
    );
    return;
  }

  const serverInstructions: string =
    typeof initResult?.instructions === "string"
      ? initResult.instructions
      : "";

  // 4. Inject the server's instructions into the system prompt PER TURN.
  //    This is the per-turn dynamic injection opencode can't do.
  if (serverInstructions) {
    pi.events?.on?.("before_agent_start", (event: any) => {
      try {
        const existing = String(event.systemPrompt || "");
        if (!existing.includes("org-llm")) {
          event.systemPrompt = existing + "\n\n" + serverInstructions;
        }
      } catch {
        // Pi's event shape varies; bail silently rather than break the turn.
      }
    });
  }

  // 5. List + register tools.
  let toolsList: { tools?: MCPTool[] } = {};
  try {
    toolsList = await send("tools/list", {});
  } catch (err) {
    ctx.ui?.notify?.(
      `pi-org-llm: tools/list failed: ${(err as Error).message}`,
    );
  }
  const tools: MCPTool[] = toolsList.tools || [];

  for (const tool of tools) {
    pi.registerTool?.({
      name: `org_llm_${tool.name}`,
      description:
        tool.description?.split("\n\n")[0] ||
        `org-llm MCP tool: ${tool.name}`,
      inputSchema: tool.inputSchema || {
        type: "object",
        properties: {},
      },
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      execute: async (args: any, opts: any = {}) => {
        const token = `${tool.name}-${Date.now()}-${Math.random()
          .toString(36)
          .slice(2, 6)}`;
        if (opts.onProgress) {
          progressSubscribers.set(token, (e) => {
            opts.onProgress({
              progress: e.progress,
              total: e.total,
              message: e.message,
            });
          });
        }
        try {
          const result = await send("tools/call", {
            name: tool.name,
            arguments: args,
            _meta: { progressToken: token },
          });
          // MCP returns content as a list of content parts; join the text
          // ones for Pi which expects a string return from execute.
          const content = (result as any)?.content;
          if (!Array.isArray(content)) {
            return typeof result === "string"
              ? result
              : JSON.stringify(result, null, 2);
          }
          return content
            .map((c: any) =>
              c?.type === "text" ? c.text : JSON.stringify(c),
            )
            .join("\n");
        } finally {
          progressSubscribers.delete(token);
        }
      },
    });
  }

  // 6. Status line surfacing — themed in LCARS orange to match the
  //    rest of org-llm. setStatus is best-effort: any Pi version that
  //    doesn't expose it just no-ops via optional chaining.
  ctx.ui?.setStatus?.({
    text: `org-llm ⊳ ${tools.length} MCP tool${tools.length === 1 ? "" : "s"}`,
    style: { fg: "#FF9900", bold: true },
  });

  // 7. Slash command — quick "what's available?" that doesn't go through
  //    the LLM. Mirrors opencode's /menu.
  pi.registerCommand?.({
    name: "org-llm-tools",
    description: "List org-llm MCP tools loaded into this session",
    handler: async () => {
      if (tools.length === 0)
        return "No org-llm tools loaded. Is the server running?";
      const grouped: Record<string, string[]> = {};
      for (const t of tools) {
        const family = t.name.split("_")[0] || "misc";
        (grouped[family] ||= []).push(t.name);
      }
      const lines: string[] = [
        `org-llm: ${tools.length} MCP tool${tools.length === 1 ? "" : "s"} registered as org_llm_*`,
      ];
      for (const family of Object.keys(grouped).sort()) {
        lines.push(
          `  ${family}: ${grouped[family].sort().join(", ")}`,
        );
      }
      return lines.join("\n");
    },
  });

  // 8. /org-llm — convenience: free-form intent → org_llm_org_llm_run
  //    so the user can fire any org-llm CLI verb in one go.
  pi.registerCommand?.({
    name: "org-llm",
    description:
      "Run any allow-listed org-llm CLI verb via org_llm_run (auto-fix).",
    handler: async (input: string) => {
      const callable = pi.tools?.org_llm_org_llm_run;
      if (!callable?.execute) {
        return (
          "org_llm_org_llm_run not registered. Try /org-llm-tools to " +
          "see what IS available."
        );
      }
      return callable.execute({ command_string: input || "" });
    },
  });

  // 9. Cleanup on Pi shutdown — ensure the MCP server exits cleanly so
  //    its writer thread stops and the SQLite connections close.
  const cleanup = () => {
    try {
      notify("notifications/exit");
    } catch {
      /* ignored */
    }
    setTimeout(() => {
      try {
        proc.kill("SIGTERM");
      } catch {
        /* ignored */
      }
    }, 200);
    setTimeout(() => {
      try {
        proc.kill("SIGKILL");
      } catch {
        /* ignored */
      }
    }, 1000);
  };
  pi.events?.on?.("session_end", cleanup);
  process.on("exit", cleanup);
  process.on("SIGINT", () => {
    cleanup();
    process.exit(130);
  });

  ctx.ui?.notify?.(
    `org-llm: ${tools.length} tool${tools.length === 1 ? "" : "s"} ready (try /org-llm-tools)`,
  );
}
