import { describe, expect, it } from "vitest";

import { Speaker } from "../plugin/speakd.js";

function build() {
  const sent = [];
  const speaker = new Speaker({
    send: (request) => {
      sent.push(request);
      return Promise.resolve(null);
    },
  });
  return { speaker, sent };
}

function text(sessionID, messageID, partID, value, extra = {}) {
  return {
    id: partID,
    sessionID,
    messageID,
    type: "text",
    text: value,
    ...extra,
  };
}

describe("the speaker", () => {
  it("speaks a text part whole when it finishes", async () => {
    const { speaker, sent } = build();
    speaker.onChatMessage("ses_1", "build");
    speaker.onMessageUpdated({
      id: "m1", sessionID: "ses_1", role: "assistant", agent: "build",
      parentID: "", time: { created: 1 },
    });
    speaker.onPartUpdated("ses_1", text("ses_1", "m1", "p1", "Hello."));
    speaker.onPartUpdated("ses_1", text("ses_1", "m1", "p1", "Hello. World.", { time: { start: 0, end: 12 } }));
    expect(sent).toEqual([
      { verb: "enqueue", source: "opencode:ses_1", payload: { text: "Hello. World.", kind: "response" } },
    ]);
  });

  it("never speaks a part twice", async () => {
    const { speaker, sent } = build();
    speaker.onChatMessage("ses_1", "build");
    speaker.onMessageUpdated({
      id: "m1", sessionID: "ses_1", role: "assistant", agent: "build",
      parentID: "", time: { created: 1, completed: 2 },
    });
    speaker.onPartUpdated("ses_1", text("ses_1", "m1", "p1", "Done.", { time: { start: 0, end: 5 } }));
    speaker.onPartUpdated("ses_1", text("ses_1", "m1", "p1", "Done.", { time: { start: 0, end: 5 } }));
    speaker.onMessageUpdated({
      id: "m1", sessionID: "ses_1", role: "assistant", agent: "build",
      parentID: "", time: { created: 1, completed: 2 },
    });
    expect(sent).toHaveLength(1);
  });

  it("buffers before the message is known and flushes after classification", async () => {
    const { speaker, sent } = build();
    speaker.onChatMessage("ses_1", "build");
    speaker.onPartUpdated("ses_1", text("ses_1", "m1", "p1", "Early.", { time: { start: 0, end: 6 } }));
    expect(sent).toHaveLength(0);
    speaker.onMessageUpdated({
      id: "m1", sessionID: "ses_1", role: "assistant", agent: "build",
      parentID: "", time: { created: 1, completed: 2 },
    });
    expect(sent).toEqual([
      { verb: "enqueue", source: "opencode:ses_1", payload: { text: "Early.", kind: "response" } },
    ]);
  });

  it("skips sub-agent messages and dropped ignored parts", async () => {
    const { speaker, sent } = build();
    speaker.onChatMessage("ses_1", "build");
    speaker.onMessageUpdated({
      id: "m1", sessionID: "ses_1", role: "assistant", agent: "general",
      parentID: "", time: { created: 1, completed: 2 },
    });
    speaker.onPartUpdated("ses_1", text("ses_1", "m1", "p1", "Sub-agent noise."));
    speaker.onMessageUpdated({
      id: "m2", sessionID: "ses_1", role: "assistant", agent: "build",
      parentID: "", time: { created: 1, completed: 2 },
    });
    speaker.onPartUpdated("ses_1", { ...text("ses_1", "m2", "p2", "Skip me."), ignored: true });
    expect(sent).toHaveLength(0);
  });

  it("idle flushes what remains and then sends the finished backstop", async () => {
    const { speaker, sent } = build();
    speaker.onChatMessage("ses_1", "build");
    speaker.onMessageUpdated({
      id: "m1", sessionID: "ses_1", role: "assistant", agent: "build",
      parentID: "", time: { created: 1 },
    });
    speaker.onPartUpdated("ses_1", text("ses_1", "m1", "p1", "Left over."));
    speaker.onSessionIdle("ses_1");
    expect(sent).toEqual([
      { verb: "enqueue", source: "opencode:ses_1", payload: { text: "Left over.", kind: "response" } },
      {
        verb: "enqueue", source: "opencode:ses_1",
        payload: { text: "finished", kind: "attention", unless_briefed: true, only_in_mode: "brief" },
      },
    ]);
  });
});
