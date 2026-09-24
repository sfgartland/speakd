// A test-only bridge: POST /speakd-test/eval with a JavaScript function body
// (it may use await), and get back {ok, value} or {ok: false, error}. It exists
// so that a throwaway Zotero profile can be driven from outside for live tests
// of the speakd reader plugin, because Marionette waits for a browser window
// that Zotero never opens. It must never be installed in a real profile: it is
// arbitrary code execution for anything that can reach the port.
const PATH = "/speakd-test/eval";

function Endpoint() {}
Endpoint.prototype = {
  supportedMethods: ["POST"],
  supportedDataTypes: ["text/plain", "application/json"],
  permitBookmarklet: false,
  async init(request) {
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

function startup() {
  Zotero.Server.Endpoints[PATH] = Endpoint;
}
function shutdown() {
  delete Zotero.Server.Endpoints[PATH];
}
function install() {}
function uninstall() {}
