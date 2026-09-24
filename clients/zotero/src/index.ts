// The plugin's lifecycle hooks, called by addon/bootstrap.js. Everything the
// plugin does is started from here and undone in `shutdown`, so that
// disabling it leaves Zotero as it found it.

import { Controls } from "./controls";
import { dismantle, hushOnQuit } from "./lifecycle";
import { Link, type AbortLike } from "./link";
import { observeConfig, readConfig } from "./prefs";
import { Takeover } from "./reader-takeover";
import { SpeakdClient, type FetchLike } from "./speakd";

/* eslint-disable @typescript-eslint/no-explicit-any */
declare const Services: any;
type Any = any;

interface StartupData {
  id: string;
  version: string;
  rootURI: string;
}

interface Running {
  link: Link;
  takeover: Takeover;
  controls: Controls;
  unobserve: () => void;
  quitting: boolean;
}

let running: Running | null = null;

function log(message: string): void {
  Zotero.debug(`speakd reader: ${message}`);
}

function warn(message: string, error?: unknown): void {
  const detail =
    error === undefined ? "" : `: ${error instanceof Error ? `${error.message}\n${error.stack ?? ""}` : String(error)}`;
  Zotero.debug(`speakd reader: ${message}${detail}`, 1);
  Zotero.logError(new Error(`speakd reader: ${message}${detail}`));
}

/**
 * The plugin sandbox has no AbortController (internals §6); a window does.
 * The main window's is used, or the hidden window's before there is one.
 */
function makeAbort(): AbortLike {
  const win: Any = Zotero.getMainWindow() ?? Services.appShell.hiddenDOMWindow;
  return new win.AbortController();
}

export async function startup(data: StartupData, _reason: number): Promise<void> {
  // Zotero may still be starting when a plugin is; nothing it offers is
  // safe to touch before this resolves.
  await Zotero.initializationPromise;
  const sandboxFetch = fetch as unknown as FetchLike;
  const link = new Link({
    makeClient: (config) => {
      const client = new SpeakdClient({ ...config, fetch: (url, init) => sandboxFetch(url, init) });
      return {
        // Every verb, in Zotero's debug log: what was asked of speakd, and what it said.
        call: async (verb: string, sourceId: string, payload: Record<string, unknown> = {}) => {
          const result = await client.call(verb, sourceId, payload);
          const shown = JSON.stringify(payload);
          log(`${verb} ${sourceId} ${shown.length > 120 ? `${shown.slice(0, 120)}…` : shown} -> ${result.kind}`);
          return result;
        },
        events: client.events.bind(client),
      };
    },
    makeAbort,
  });
  link.configure(readConfig());
  const unobserveConfig = observeConfig(() => link.configure(readConfig()));
  // A quit is heard here, while the network is still up: by the time
  // bootstrap.js hears APP_SHUTDOWN, it has been torn down.
  const quitObserver = { observe: () => quit() };
  Services.obs.addObserver(quitObserver, "quit-application-granted");
  const unobserve = () => {
    unobserveConfig();
    Services.obs.removeObserver(quitObserver, "quit-application-granted");
  };

  const takeover = new Takeover({ link, log, warn });
  takeover.install();
  const controls = new Controls({ pluginID: data.id, takeover, link, warn });
  controls.register();
  running = { link, takeover, controls, unobserve, quitting: false };

  try {
    await Zotero.PreferencePanes.register({
      pluginID: data.id,
      src: `${data.rootURI}content/preferences.xhtml`,
      label: "speakd reader",
    } as Any);
  } catch (error) {
    warn("the preference pane could not be registered", error);
  }

  // For scripting, and for checking the plugin from Zotero's console: the
  // readers it adopted, and the daemon link. Removed on shutdown.
  (Zotero as Any).SpeakdReader = {
    version: data.version,
    link,
    handleFor: (reader: unknown) => takeover.handleFor(reader),
    handles: () => takeover.handles(),
  };
  log(`loaded (${data.version})`);
}

/**
 * Zotero is quitting ("quit-application-granted", and bootstrap.js's
 * APP_SHUTDOWN after it): nothing is taken apart, but no reader's read
 * should outlive Zotero, nor its voice pref stay speakd's. Once;
 * synchronous, and never throws.
 */
export function quit(): void {
  const current = running;
  if (current === null || current.quitting) return;
  current.quitting = true;
  try {
    current.takeover.quit();
    hushOnQuit(
      current.takeover.handles().map((handle) => ({
        sourceId: handle.sourceId,
        holding: handle.session?.channel.holding ?? false,
      })),
      (verb, sourceId, payload) => current.link.call(verb, sourceId, payload),
    );
    log("quitting: hushed what was reading");
  } catch (error) {
    warn("quitting failed", error);
  }
}

export async function shutdown(_data: StartupData, _reason: number): Promise<void> {
  const current = running;
  running = null;
  delete (Zotero as Any).SpeakdReader;
  if (current === null) return;
  // Zotero awaits a plugin's shutdown (plugins.js `_callMethod`), so a
  // read's hush can finish on the link before it closes.
  await dismantle(current);
  log("unloaded");
}
