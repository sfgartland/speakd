import { describe, expect, it } from "vitest";

import { buildPlugin } from "../plugin/speakd.js";

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
