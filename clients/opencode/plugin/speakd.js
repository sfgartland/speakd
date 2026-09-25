// Speak an OpenCode session through the speakd daemon.
//
// One dependency-free file, loaded by OpenCode as a plugin. It talks the
// daemon's JSON-lines protocol over the Unix socket, writes the registration
// files `speakd-mcp` reads to find its session, and reacts to OpenCode's
// hooks and events. Nothing here may break OpenCode: every handler is
// guarded, every socket call bounded, and a missing daemon is silence.

// ---- the socket ----

import net from "node:net";
import path from "node:path";

export const SOCKET_TIMEOUT_MS = 2000;

export function socketPath(env = process.env) {
  // Mirrors speakd.paths.default_socket_path.
  const base = env.XDG_RUNTIME_DIR || env.TMPDIR || "/tmp";
  return path.join(base, "speakd", "speakd.sock");
}

export function request(line, { socket = socketPath(), timeoutMs = SOCKET_TIMEOUT_MS } = {}) {
  return new Promise((resolve) => {
    const client = net.createConnection({ path: socket });
    let settled = false;
    const settle = (reply) => {
      if (settled) return;
      settled = true;
      client.destroy();
      resolve(reply);
    };
    client.setTimeout(timeoutMs);
    client.on("connect", () => client.write(line + "\n"));
    client.on("timeout", () => settle({ ok: false, error: `timed out after ${timeoutMs}ms` }));
    client.on("error", (err) =>
      settle({ ok: false, error: `no daemon at ${socket} (${err.code ?? err.message})` }),
    );
    let buffer = "";
    client.on("data", (chunk) => {
      buffer += chunk.toString("utf-8");
      const end = buffer.indexOf("\n");
      if (end === -1) return;
      const raw = buffer.slice(0, end);
      buffer = buffer.slice(end + 1);
      try {
        settle({ ok: true, body: JSON.parse(raw) });
      } catch {
        settle({ ok: false, error: `unreadable reply: ${raw.slice(0, 80)}` });
      }
    });
    client.on("end", () => settle({ ok: false, error: "daemon closed the connection" }));
  });
}

export function sendRequest(verb, source, payload, options = {}) {
  return request(JSON.stringify({ verb, source_id: source, payload }), options).then((reply) => {
    if (reply.ok === false) return reply.error;
    if (reply.body && reply.body.ok === false) return reply.body.error || "refused";
    return null;
  });
}

export function enqueueText(source, text, { kind = "response", flags = {}, ...options } = {}) {
  if (!String(text).trim()) return Promise.resolve(null);
  return sendRequest("enqueue", source, { text, kind, ...flags }, options);
}

export function hushChannel(source, { new_turn = false, ...options } = {}) {
  return sendRequest("hush", source, new_turn ? { new_turn: true } : {}, options);
}

export function setLabelChannel(source, label, options = {}) {
  return sendRequest("set_label", source, { label }, options);
}

// ---- the registration file ----
//
// The same file `speakd.clients.registry` writes and reads, in the same
// state directory scheme, so `speakd-mcp` can find this session by process
// ancestry. The slug must match Python's `_slug` exactly.

import fs from "node:fs";
import crypto from "node:crypto";

export function stateDir(env = process.env) {
  // Mirrors speakd.paths.client_state_dir for client "opencode".
  const override = env.SPEAKD_STATE_DIR;
  if (override) return path.join(override, "opencode");
  const xdg = env.XDG_STATE_HOME;
  const base = xdg
    ? path.join(xdg, "speakd")
    : path.join(env.HOME ?? "/tmp", ".local", "state", "speakd");
  return path.join(base, "opencode");
}

export function slug(sessionID) {
  const safe = sessionID.replace(/[^A-Za-z0-9_.-]/g, "_").slice(0, 80);
  const digest = crypto.createHash("sha256").update(sessionID, "utf-8").digest("hex").slice(0, 12);
  return `${safe}-${digest}`;
}

function makeDirectory(directory) {
  // Component-wise, not mkdirSync(..., { recursive: true }): on hosts where
  // mkdir reports ENOENT for a permission failure (SELinux over /proc),
  // recursive mkdir retries forever and this hook would hang OpenCode.
  const missing = [];
  let probe = directory;
  while (!isDirectory(probe)) {
    missing.push(path.basename(probe));
    probe = path.dirname(probe);
  }
  let created = probe;
  while (missing.length) {
    created = path.join(created, missing.pop());
    fs.mkdirSync(created);
  }
}

function isDirectory(p) {
  try {
    return fs.statSync(p).isDirectory();
  } catch {
    return false;
  }
}

export function register(sessionID, { cwd = "", agentPid = process.pid, env = process.env } = {}) {
  try {
    const directory = stateDir(env);
    makeDirectory(directory);
    const body = {
      session_id: sessionID,
      transcript: "",
      cwd: cwd || process.cwd(),
      touched: Date.now() / 1000,
      client: "opencode",
      agent_pid: agentPid,
    };
    fs.writeFileSync(path.join(directory, `${slug(sessionID)}.session.json`), JSON.stringify(body));
  } catch {
    // A registration that cannot be written costs silence, nothing more.
  }
}
