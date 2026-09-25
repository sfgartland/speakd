import { describe, expect, it } from "vitest";

import { Signals } from "../plugin/speakd.js";

function build() {
  const sent = [];
  const registrations = [];
  const signals = new Signals({
    send: (request) => {
      sent.push(request);
      return Promise.resolve(null);
    },
    register: (sessionID, options) => registrations.push([sessionID, options]),
  });
  return { signals, sent, registrations };
}

describe("signals", () => {
  it("a prompt registers the session and hushes it with a new turn", async () => {
    const { signals, sent, registrations } = build();
    signals.onSession({ id: "ses_1", title: "", directory: "/home/me/p" });
    sent.length = 0;
    signals.onChatMessage("ses_1");
    expect(registrations).toEqual([["ses_1", { cwd: "/home/me/p" }]]);
    expect(sent).toEqual([
      { verb: "hush", source: "opencode:ses_1", payload: { new_turn: true } },
    ]);
  });

  it("a permission ask is spoken on the session's channel in every mode", async () => {
    const { signals, sent } = build();
    signals.onPermissionAsked({
      type: "permission.asked",
      properties: { sessionID: "ses_1", permission: "bash" },
    });
    expect(sent).toEqual([
      {
        verb: "enqueue",
        source: "opencode:ses_1",
        payload: { text: "OpenCode needs your permission.", kind: "attention" },
      },
    ]);
  });

  it("a permission event without a session is skipped, not thrown", async () => {
    const { signals, sent } = build();
    expect(() => signals.onPermissionAsked({ type: "permission.asked", properties: {} })).not.toThrow();
    expect(sent).toHaveLength(0);
  });

  it("the label is sent once per change, title first", async () => {
    const { signals, sent } = build();
    signals.onSession({ id: "ses_1", title: "", directory: "/home/me/repo" });
    expect(sent).toEqual([
      { verb: "set_label", source: "opencode:ses_1", payload: { label: "OpenCode · repo" } },
    ]);
    sent.length = 0;
    signals.onSession({ id: "ses_1", title: "", directory: "/home/me/repo" });
    expect(sent).toHaveLength(0);
    signals.onSession({ id: "ses_1", title: "Fix the build", directory: "/home/me/repo" });
    expect(sent).toEqual([
      { verb: "set_label", source: "opencode:ses_1", payload: { label: "OpenCode · Fix the build" } },
    ]);
  });
});
