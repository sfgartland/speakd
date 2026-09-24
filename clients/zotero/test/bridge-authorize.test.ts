import { describe, expect, it } from "vitest";
import source from "../test-live/bridge/authorize.js?raw";

// The live-test bridge's gate (test-live/bridge/authorize.js): a plain
// script, as the bridge's bootstrap loads it, evaluated here the same way.
type Authorize = (headers: Record<string, string | undefined>, token: string | null, port: number) => string | null;
const authorize = new Function(`${source}\nreturn speakdTestAuthorize;`)() as Authorize;

const TOKEN = "3f9c1a7e5b2d4c6e8f0a1b3c5d7e9f1a";
const good = { host: "127.0.0.1:23129", "x-speakd-test-token": TOKEN };

describe("the test bridge's gate", () => {
  it("lets through a request to its own port that carries the token", () => {
    expect(authorize(good, TOKEN, 23129)).toBeNull();
    expect(authorize({ ...good, host: "localhost:23129" }, TOKEN, 23129)).toBeNull();
  });

  it("refuses a request with no token, or the wrong one", () => {
    expect(authorize({ host: good.host }, TOKEN, 23129)).not.toBeNull();
    expect(authorize({ ...good, "x-speakd-test-token": "" }, TOKEN, 23129)).not.toBeNull();
    expect(authorize({ ...good, "x-speakd-test-token": TOKEN.slice(1) }, TOKEN, 23129)).not.toBeNull();
    expect(authorize({ ...good, "x-speakd-test-token": `${TOKEN.slice(0, -1)}0` }, TOKEN, 23129)).not.toBeNull();
  });

  it("refuses everything when no token was set up", () => {
    expect(authorize(good, null, 23129)).not.toBeNull();
    expect(authorize({ ...good, "x-speakd-test-token": "" }, "", 23129)).not.toBeNull();
  });

  it("refuses a Host that is not this port on loopback: DNS rebinding, or another port", () => {
    for (const host of [undefined, "evil.example:23129", "127.0.0.1", "127.0.0.1:23119", "localhost:23129.evil.example", "[::1]:23129"]) {
      expect(authorize({ ...good, host }, TOKEN, 23129)).not.toBeNull();
    }
  });
});
