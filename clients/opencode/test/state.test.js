import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";

import { register, stateDir } from "../plugin/speakd.js";

function tempState() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "speakd-opencode-state-"));
}

describe("registration files", () => {
  it("writes the schema the Python registry reads", () => {
    const root = tempState();
    const env = { ...process.env, SPEAKD_STATE_DIR: root };
    register("ses_abc123", { cwd: "/home/me/p", agentPid: 4242, env });
    const files = fs.readdirSync(stateDir(env));
    expect(files).toHaveLength(1);
    const body = JSON.parse(
      fs.readFileSync(path.join(stateDir(env), files[0]), "utf-8"),
    );
    expect(body.session_id).toBe("ses_abc123");
    expect(body.transcript).toBe("");
    expect(body.cwd).toBe("/home/me/p");
    expect(body.client).toBe("opencode");
    expect(body.agent_pid).toBe(4242);
    expect(typeof body.touched).toBe("number");
  });

  it("a path-shaped session id stays inside the directory", () => {
    const root = tempState();
    const env = { ...process.env, SPEAKD_STATE_DIR: root };
    register("../../evil", { cwd: "/w", agentPid: 1, env });
    const files = fs.readdirSync(stateDir(env));
    expect(files).toHaveLength(1);
    expect(files[0].startsWith(".._.._evil-")).toBe(true);
    expect(fs.existsSync(path.join(root, "evil.session.json"))).toBe(false);
  });

  it("creates every missing level of the state directory", () => {
    const root = tempState();
    const env = { ...process.env, SPEAKD_STATE_DIR: path.join(root, "missing", "deeper") };
    register("ses_1", { cwd: "/w", env });
    const directory = path.join(root, "missing", "deeper", "opencode");
    const files = fs.readdirSync(directory);
    expect(files).toHaveLength(1);
    expect(files[0]).toMatch(/\.session\.json$/);
    const body = JSON.parse(fs.readFileSync(path.join(directory, files[0]), "utf-8"));
    expect(body.session_id).toBe("ses_1");
  });

  it("never throws, even into an unwritable directory", () => {
    const env = { SPEAKD_STATE_DIR: "/proc/forbidden/speakd" };
    expect(() => register("ses_1", { cwd: "/w", env })).not.toThrow();
  });
});
