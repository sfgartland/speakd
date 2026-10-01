import { describe, expect, it } from "vitest";

import SpeakdPlugin, { buildPlugin } from "../plugin/speakd.js";

function build() {
  const sent = [];
  const registrations = [];
  const logs = [];
  const plugin = buildPlugin({
    client: { app: { log: async (line) => logs.push(line) } },
    send: (request) => {
      sent.push(request);
      return Promise.resolve(null);
    },
    registerFn: (sessionID, options) => registrations.push([sessionID, options]),
  });
  return { plugin, sent, registrations, logs };
}

describe("the plugin entry", () => {
  it("looks up an existing child session before handling its first event", async () => {
    const sent = [];
    const registrations = [];
    const plugin = buildPlugin({
      client: {
        app: { log: async () => {} },
        session: { get: async ({ path }) => ({ data: { id: path.id, parentID: "main" } }) },
      },
      send: (request) => { sent.push(request); return Promise.resolve(null); },
      registerFn: (sessionID) => registrations.push(sessionID),
    });
    await plugin["chat.message"]({ sessionID: "child", agent: "build" });
    await plugin.event({ event: { type: "message.updated", properties: { info: {
      id: "m1", sessionID: "child", role: "assistant", agent: "build",
    } } } });
    await plugin.event({ event: { type: "message.part.updated", properties: { part: {
      id: "p1", messageID: "m1", sessionID: "child", type: "text",
      text: "Child speech", time: { end: 3 },
    } } } });
    expect(sent).toEqual([]);
    expect(registrations).toEqual([]);
  });

  it("does not speak or register a child session using the build agent", async () => {
    const { plugin, sent, registrations } = build();
    await plugin.event({ event: { type: "session.created", properties: { info: {
      id: "child", parentID: "main", title: "Child task",
    } } } });
    await plugin["chat.message"]({ sessionID: "child", agent: "build" });
    await plugin.event({ event: { type: "message.updated", properties: { info: {
      id: "m1", sessionID: "child", role: "assistant", agent: "build",
    } } } });
    await plugin.event({ event: { type: "message.part.updated", properties: { part: {
      id: "p1", messageID: "m1", sessionID: "child", type: "text",
      text: "Child speech", time: { end: 3 },
    } } } });
    await plugin.event({ event: { type: "session.status", properties: {
      sessionID: "child", status: { type: "idle" },
    } } });
    expect(sent).toEqual([]);
    expect(registrations).toEqual([]);
  });

  it("removes buffered text using OpenCode's part removal IDs", async () => {
    const { plugin, sent } = build();
    await plugin["chat.message"]({ sessionID: "ses_1", agent: "build" });
    await plugin.event({ event: { type: "message.updated", properties: { info: {
      id: "m1", sessionID: "ses_1", role: "assistant", agent: "build",
    } } } });
    await plugin.event({ event: { type: "message.part.updated", properties: { part: {
      id: "p1", messageID: "m1", sessionID: "ses_1", type: "text", text: "Removed text",
    } } } });
    await plugin.event({ event: { type: "message.part.removed", properties: {
      sessionID: "ses_1", messageID: "m1", partID: "p1",
    } } });
    await plugin.event({ event: { type: "session.status", properties: {
      sessionID: "ses_1", status: { type: "idle" },
    } } });
    expect(sent.filter((request) => request.payload.text === "Removed text")).toEqual([]);
  });

  it("the default export is the v1 object entrypoint opencode loads", async () => {
    expect(SpeakdPlugin.server).toBeInstanceOf(Function);
    const hooks = await SpeakdPlugin.server({ client: { app: { log: async () => {} } } });
    expect(typeof hooks["chat.message"]).toBe("function");
    expect(typeof hooks.event).toBe("function");
  });

  it("a chat.message hushes, registers and sets the main agent", async () => {
    const { plugin, sent, registrations } = build();
    await plugin["chat.message"]({ sessionID: "ses_1", agent: "build" });
    await plugin.event({ event: { type: "message.updated", properties: { info: { id: "m1", sessionID: "ses_1", role: "assistant", agent: "build", parentID: "", time: { created: 1 } } } } });
    await plugin.event({ event: { type: "message.part.updated", properties: { part: { id: "p1", sessionID: "ses_1", messageID: "m1", type: "text", text: "Hi.", time: { start: 0, end: 3 } } } } });
    expect(registrations).toEqual([["ses_1", { cwd: "" }]]);
    expect(sent).toEqual([
      { verb: "hush", source: "opencode:ses_1", payload: { new_turn: true } },
      { verb: "enqueue", source: "opencode:ses_1", payload: { text: "Hi.", kind: "response" } },
    ]);
  });

  it("a session status of idle sends the finished backstop", async () => {
    const { plugin, sent } = build();
    await plugin.event({ event: { type: "session.status", properties: { sessionID: "ses_1", status: { type: "idle" } } } });
    expect(sent).toEqual([
      {
        verb: "enqueue",
        source: "opencode:ses_1",
        payload: { text: "finished", kind: "attention", unless_briefed: true, only_in_mode: "brief" },
      },
    ]);
  });

  it("a busy session status does nothing", async () => {
    const { plugin, sent } = build();
    await plugin.event({ event: { type: "session.status", properties: { sessionID: "ses_1", status: { type: "busy" } } } });
    expect(sent).toHaveLength(0);
  });

  it("a permission.asked event is spoken as attention", async () => {
    const { plugin, sent } = build();
    await plugin.event({ event: { type: "permission.asked", properties: { sessionID: "ses_1", permission: "bash" } } });
    expect(sent).toEqual([
      {
        verb: "enqueue",
        source: "opencode:ses_1",
        payload: { text: "OpenCode needs your permission.", kind: "attention" },
      },
    ]);
  });

  it("malformed events are logged, never thrown", async () => {
    const { plugin, logs } = build();
    await expect(plugin.event({ event: { type: "session.status", properties: null } })).resolves.toBeUndefined();
    await expect(plugin.event({ event: null })).resolves.toBeUndefined();
    expect(logs.length).toBeGreaterThan(0);
  });
});
