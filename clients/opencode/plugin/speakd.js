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
    try {
      fs.mkdirSync(created);
    } catch (err) {
      // A concurrent process may have created the component between the
      // probe and here; that is the directory existing, not an error.
      if (err.code !== "EEXIST") throw err;
    }
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

// ---- what is spoken ----
//
// OpenCode streams a text part's growth through `message.part.updated`; each
// event carries the part's accumulated text. Speaking each delta would cut
// words in half, so parts are buffered and enqueued whole when they finish
// -- a text block's end, which is usually before the tool calls that follow
// it. A part is spoken at most once; sub-agent output and `ignored` parts
// are never spoken.

class SessionState {
  constructor() {
    this.mainAgent = null;
    this.userAgents = new Map(); // messageID -> agent, for user messages
    this.messages = new Map(); // messageID -> { agent, parts, spoken }
  }
}

export class Speaker {
  constructor({ send, log = () => {} }) {
    this.send = send; // ({verb, source, payload}) -> Promise<reason|null>
    this.log = log;
    this.sessions = new Map(); // sessionID -> SessionState
  }

  _session(sessionID) {
    let state = this.sessions.get(sessionID);
    if (!state) {
      state = new SessionState();
      this.sessions.set(sessionID, state);
    }
    return state;
  }

  _main(state) {
    return state.mainAgent ?? "build";
  }

  _entry(state, messageID) {
    let entry = state.messages.get(messageID);
    if (!entry) {
      entry = { agent: null, parts: new Map(), spoken: new Set() };
      state.messages.set(messageID, entry);
    }
    return entry;
  }

  onChatMessage(sessionID, agent) {
    // The first `chat.message` for a session names its main agent; user
    // prompts routed to sub-agents do not come through this hook.
    const state = this._session(sessionID);
    if (!state.mainAgent && agent) state.mainAgent = agent;
  }

  onMessageUpdated(info) {
    if (info.role === "user") {
      const state = this._session(info.sessionID);
      state.userAgents.set(info.id, info.agent);
      return;
    }
    const state = this._session(info.sessionID);
    const entry = this._entry(state, info.id);
    // `agent` is present in OpenCode's stored messages though absent from the
    // SDK's published types; the parent user message is the fallback.
    entry.agent = info.agent ?? state.userAgents.get(info.parentID) ?? this._main(state);
    if (entry.agent !== this._main(state)) {
      entry.parts.clear();
      return;
    }
    if (info.time && info.time.completed != null) {
      this._flushAll(state, info.sessionID, info.id, entry);
    }
  }

  onPartUpdated(sessionID, part) {
    if (part.type !== "text") return;
    const state = this._session(sessionID);
    const entry = this._entry(state, part.messageID);
    if (!entry.parts.has(part.id) && entry.spoken.has(part.id)) return;
    if (part.ignored) {
      entry.parts.delete(part.id);
      return;
    }
    if (entry.agent !== null && entry.agent !== this._main(state)) return;
    entry.parts.set(part.id, part.text);
    if (part.time && part.time.end != null && entry.agent !== null) {
      this._flush(state, sessionID, part.messageID, entry, part.id);
    }
  }

  onPartRemoved(sessionID, part) {
    if (part.type !== "text") return;
    const state = this._session(sessionID);
    const entry = state.messages.get(part.messageID);
    if (entry) entry.parts.delete(part.id);
  }

  onSessionIdle(sessionID) {
    const state = this._session(sessionID);
    for (const [messageID, entry] of state.messages) {
      // Parts whose message was never classified (agent === null) are dropped, not spoken: they could be sub-agent output in disguise.
      if (entry.agent !== null && entry.agent === this._main(state)) {
        this._flushAll(state, sessionID, messageID, entry);
      }
    }
    this.send({
      verb: "enqueue",
      source: `opencode:${sessionID}`,
      payload: { text: "finished", kind: "attention", unless_briefed: true, only_in_mode: "brief" },
    });
  }

  _flush(state, sessionID, messageID, entry, partID) {
    const text = entry.parts.get(partID);
    if (text == null) return;
    entry.parts.delete(partID);
    entry.spoken.add(partID);
    const body = text.trim();
    if (body) {
      this.send({
        verb: "enqueue",
        source: `opencode:${sessionID}`,
        payload: { text: body, kind: "response" },
      });
    }
  }

  _flushAll(state, sessionID, messageID, entry) {
    for (const partID of [...entry.parts.keys()]) {
      this._flush(state, sessionID, messageID, entry, partID);
    }
  }
}

// ---- control signals and labels ----
//
// What only OpenCode itself can know, mirrored from the Claude hooks: a
// prompt was submitted (hush, register), the user's permission is needed
// (attention, spoken in every mode), and what the session is called.

export class Signals {
  constructor({ send, register, log = () => {} }) {
    this.send = send;
    this.register = register;
    this.log = log;
    this.sessions = new Map(); // sessionID -> { title, directory }
    this.labels = new Map(); // sessionID -> last label sent
  }

  _session(sessionID) {
    let state = this.sessions.get(sessionID);
    if (!state) {
      state = { title: null, directory: "" };
      this.sessions.set(sessionID, state);
    }
    return state;
  }

  onChatMessage(sessionID) {
    const state = this._session(sessionID);
    this.register(sessionID, { cwd: state.directory });
    this.send({ verb: "hush", source: `opencode:${sessionID}`, payload: { new_turn: true } });
  }

  onPermissionAsked(event) {
    // An event, not the `permission.ask` hook: that hook is declared in the
    // plugin types but never invoked in OpenCode 1.x. Only the session id is
    // read, which every delivered payload shape carries.
    const properties = event.properties ?? {};
    const sessionID = properties.sessionID;
    if (!sessionID) return;
    this.send({
      verb: "enqueue",
      source: `opencode:${sessionID}`,
      payload: { text: "OpenCode needs your permission.", kind: "attention" },
    });
  }

  onSession(info) {
    const state = this._session(info.id);
    state.title = info.title || null;
    state.directory = info.directory || "";
    const label = info.title
      ? `OpenCode · ${info.title}`
      : info.directory
        ? `OpenCode · ${path.basename(info.directory)}`
        : "OpenCode";
    if (this.labels.get(info.id) === label) return;
    this.labels.set(info.id, label);
    this.send({
      verb: "set_label",
      source: `opencode:${info.id}`,
      payload: { label },
    }).then((reason) => {
      if (reason != null && this.labels.get(info.id) === label) this.labels.delete(info.id);
    });
  }
}

// ---- the plugin ----

export function buildPlugin({ client, send, registerFn = register } = {}) {
  const log = async (line) => {
    try {
      if (client && client.app && client.app.log) {
        await client.app.log({ body: { service: "speakd", level: "warn", message: String(line) } });
      }
    } catch {
      // A log that cannot be written is not worth an error in the host.
    }
  };

  const realSend =
    send ??
    (({ verb, source, payload }) =>
      sendRequest(verb, source, payload, { timeoutMs: SOCKET_TIMEOUT_MS }).then((reason) => {
        if (reason != null) log(reason);
        return reason;
      }));

  const speaker = new Speaker({ send: realSend, log });
  const signals = new Signals({ send: realSend, register: registerFn, log });

  return {
    "chat.message": async (input) => {
      try {
        speaker.onChatMessage(input.sessionID, input.agent);
        signals.onChatMessage(input.sessionID);
      } catch (err) {
        log(`chat.message failed: ${err}`);
      }
    },
    event: async ({ event }) => {
      try {
        switch (event.type) {
          case "message.updated":
            speaker.onMessageUpdated(event.properties.info);
            break;
          case "message.part.updated": {
            const part = event.properties.part;
            if (!part) break;
            speaker.onPartUpdated(part.sessionID ?? event.properties.sessionID, part);
            break;
          }
          case "message.part.removed": {
            const part = event.properties.part;
            if (!part) break;
            speaker.onPartRemoved(part.sessionID ?? event.properties.sessionID, part);
            break;
          }
          case "session.status":
            // `session.idle` is deprecated; a status of idle is the same
            // signal, and it is the spelling that survives into v2.
            if (event.properties.status && event.properties.status.type === "idle") {
              speaker.onSessionIdle(event.properties.sessionID);
            }
            break;
          case "permission.asked":
            signals.onPermissionAsked(event);
            break;
          case "session.created":
          case "session.updated":
            signals.onSession(event.properties.info);
            break;
        }
      } catch (err) {
        log(`event ${event && event.type} failed: ${err}`);
      }
    },
  };
}

export const SpeakdPlugin = async ({ client } = {}) => buildPlugin({ client });

// The v1 object entrypoint: opencode reads the default export as {id, server}.
// A bare function default misses `readV1Plugin` and falls into the legacy
// path, which treats EVERY export as a plugin and fails on the first
// non-function one (SOCKET_TIMEOUT_MS) with "Plugin export is not a function".
export default { id: "speakd", server: SpeakdPlugin };
