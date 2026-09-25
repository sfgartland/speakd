import net from "node:net";
import os from "node:os";
import path from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { enqueueText, hushChannel, request } from "../plugin/speakd.js";

function listening(server, socket) {
  return new Promise((resolve) => server.listen(socket, resolve));
}

function closed(server) {
  return new Promise((resolve) => server.close(resolve));
}

describe("the socket client", () => {
  let sockets = [];

  afterAll(async () => {
    await Promise.all(sockets.map(closed));
    sockets = [];
  });

  it("sends one JSON line and resolves the reply", async () => {
    const socket = path.join(os.tmpdir(), `speakd-opencode-${process.pid}-${Math.random()}.sock`);
    const server = net.createServer((conn) => {
      conn.on("data", () => {
        conn.write(JSON.stringify({ ok: true, data: { spoken: true }, error: "" }) + "\n");
      });
    });
    sockets.push(server);
    await listening(server, socket);
    const reply = await request(JSON.stringify({ verb: "status", source_id: "", payload: {} }), {
      socket,
    });
    expect(reply).toEqual({ ok: true, body: { ok: true, data: { spoken: true }, error: "" } });
  });

  it("reports no daemon when nothing listens", async () => {
    const reply = await request(JSON.stringify({ verb: "status", source_id: "", payload: {} }), {
      socket: path.join(os.tmpdir(), "speakd-opencode-absent.sock"),
      timeoutMs: 500,
    });
    expect(reply.ok).toBe(false);
    expect(reply.error).toMatch(/no daemon/);
  });

  it("enqueueText puts text, kind and flags in the payload", async () => {
    const socket = path.join(os.tmpdir(), `speakd-opencode-${process.pid}-${Math.random()}.sock`);
    let raw = "";
    const server = net.createServer((conn) => {
      conn.on("data", (chunk) => {
        raw += chunk;
      });
    });
    sockets.push(server);
    await listening(server, socket);
    await enqueueText("opencode:ses_1", "Hello.", {
      kind: "attention",
      flags: { unless_briefed: true },
      socket,
      timeoutMs: 100,
    });
    expect(JSON.parse(raw.split("\n")[0])).toEqual({
      verb: "enqueue",
      source_id: "opencode:ses_1",
      payload: { text: "Hello.", kind: "attention", unless_briefed: true },
    });
  });

  it("blank text is never sent", async () => {
    const reason = await enqueueText("opencode:ses_1", "   ", {
      socket: path.join(os.tmpdir(), "speakd-opencode-absent.sock"),
      timeoutMs: 200,
    });
    expect(reason).toBeNull();
  });

  it("hushChannel sends new_turn only when asked", async () => {
    const socket = path.join(os.tmpdir(), `speakd-opencode-${process.pid}-${Math.random()}.sock`);
    let raw = "";
    const server = net.createServer((conn) => {
      conn.on("data", (chunk) => {
        raw += chunk;
      });
    });
    sockets.push(server);
    await listening(server, socket);
    await hushChannel("opencode:ses_1", { new_turn: true, socket, timeoutMs: 100 });
    expect(JSON.parse(raw.split("\n")[0])).toEqual({
      verb: "hush",
      source_id: "opencode:ses_1",
      payload: { new_turn: true },
    });
  });
});
