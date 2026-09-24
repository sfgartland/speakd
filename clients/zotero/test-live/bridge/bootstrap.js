/* global Services, PathUtils, IOUtils, speakdTestAuthorize */
// A test-only bridge: POST /speakd-test/eval with a JavaScript function body
// (it may use await), and get back {ok, value} or {ok: false, error}. It exists
// so that a throwaway Zotero profile can be driven from outside for live tests
// of the speakd reader plugin, because Marionette waits for a browser window
// that Zotero never opens.
//
// It is code execution with chrome privileges, so it answers only a request
// carrying the token setup-profile.sh wrote to <profile>/speakd-test-token
// (mode 0600), in the X-Speakd-Test-Token header, with a Host of this port on
// loopback (authorize.js). No token file, no evaluation. It must still never
// be installed in a real profile.
const PATH = "/speakd-test/eval";
const TOKEN_FILE = "speakd-test-token";

/** The token, read afresh for every request: a new setup takes effect at once. */
async function readToken() {
  try {
    const path = PathUtils.join(Zotero.Profile.dir, TOKEN_FILE);
    return (await IOUtils.readUTF8(path)).trim();
  } catch (e) {
    return null;
  }
}

function Endpoint() {}
Endpoint.prototype = {
  supportedMethods: ["POST"],
  supportedDataTypes: ["text/plain", "application/json"],
  permitBookmarklet: false,
  async init(request) {
    const refused = speakdTestAuthorize(request.headers || {}, await readToken(), Zotero.Prefs.get("httpServer.port"));
    if (refused !== null) {
      Zotero.debug(`speakd test bridge: refused a request: ${refused}`, 2);
      return [403, "application/json", JSON.stringify({ ok: false, error: `refused: ${refused}` })];
    }
    try {
      const body = typeof request.data === "string" ? request.data : JSON.stringify(request.data);
      const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
      const value = await new AsyncFunction("Zotero", body)(Zotero);
      return [200, "application/json", JSON.stringify({ ok: true, value: value === undefined ? null : value })];
    } catch (e) {
      return [200, "application/json", JSON.stringify({ ok: false, error: String(e), stack: String(e && e.stack) })];
    }
  },
};

function startup({ rootURI }) {
  Services.scriptloader.loadSubScript(`${rootURI}authorize.js`, globalThis);
  Zotero.Server.Endpoints[PATH] = Endpoint;
}
function shutdown() {
  delete Zotero.Server.Endpoints[PATH];
}
function install() {}
function uninstall() {}
