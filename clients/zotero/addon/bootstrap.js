/* global Services, APP_SHUTDOWN */
// Zotero's entry points for a bootstrapped plugin. They do no work of their
// own: they load the bundled script into a scope of its own and hand each
// lifecycle call to the hooks it exports (src/index.ts).

var scope = null;

function install() {}

async function startup({ id, version, rootURI }, reason) {
  scope = { rootURI };
  Services.scriptloader.loadSubScript(`${rootURI}content/scripts/speakd-reader.js`, scope);
  await scope.SpeakdReader.startup({ id, version, rootURI }, reason);
}

function onMainWindowLoad({ window }) {
  scope?.SpeakdReader.onMainWindowLoad?.(window);
}

function onMainWindowUnload({ window }) {
  scope?.SpeakdReader.onMainWindowUnload?.(window);
}

function shutdown(data, reason) {
  // On application shutdown nothing needs undoing: the process is ending.
  if (reason === APP_SHUTDOWN) {
    return;
  }
  scope?.SpeakdReader.shutdown(data, reason);
  scope = null;
}

function uninstall() {}
