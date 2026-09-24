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
  // But speakd is not: a reader mid-read is hushed, best effort, and not
  // waited for.
  if (reason === APP_SHUTDOWN) {
    try {
      scope?.SpeakdReader.quit?.();
    } catch {
      // Zotero is quitting regardless.
    }
    return;
  }
  const current = scope;
  scope = null;
  // A promise: Zotero waits for it, so a read's hush is sent before the
  // plugin's code goes away.
  return current?.SpeakdReader.shutdown(data, reason);
}

function uninstall() {}
