// The plugin's lifecycle hooks, called by addon/bootstrap.js. Everything the
// plugin does will be started from here and undone in `shutdown`, so that
// disabling it leaves Zotero as it found it.

interface StartupData {
  id: string;
  version: string;
  rootURI: string;
}

export async function startup(data: StartupData, _reason: number): Promise<void> {
  // Zotero may still be starting when a plugin is; nothing it offers is
  // safe to touch before this resolves.
  await Zotero.initializationPromise;
  Zotero.debug(`speakd reader loaded (${data.version})`);
}

export function shutdown(_data: StartupData, _reason: number): void {
  Zotero.debug("speakd reader unloaded");
}
