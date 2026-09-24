// The takeover of Zotero 10's Read Aloud: the one module that touches
// Zotero's internals. Everything it reaches for is named in `probe` lists
// below, checked when a reader opens; a miss refuses that reader, loudly,
// and leaves it as Zotero made it. It must never throw into Zotero: a throw
// from the remote-interface factory kills the tab (internals §1.2), so every
// hook is wrapped.
//
// What it does, per internals §8:
//   - wraps `Zotero.Reader._readers.push`, and gives each new PDF reader a
//     remote interface whose catalogue adds speakd to Zotero's own voices;
//   - once the reader is up, forces remote voices to load (speakd is one,
//     logged in or not), and guards `_createController`: while the plugin's
//     own read is wanted, speakd's voice is selected, and speakd's voice
//     hands Zotero a controller of ours instead of Zotero's player;
//   - builds that controller in the reader's compartment, around a
//     session.ts ControllerCore.
//
// The reader's Read Aloud popup is hidden (not closed: closing it ends the
// read) while the plugin's own read runs. Zotero's own Read Aloud, started
// with Zotero's own voice, is left alone.
//
// Line references (X:, B:) are to docs/design/2026-09-24-zotero-10-read-aloud-internals.md.

import type { Adoption } from "./bar";
import { Channel } from "./channel";
import type { Link } from "./link";
import { ReaderSession, type ControllerCore, type Intent, type SegmentInfo } from "./session";
import { SPEAKD_VOICE_ID, mergeVoices, silentWav } from "./voices";

/* eslint-disable @typescript-eslint/no-explicit-any */
declare const Components: any;
type Any = any;

const Cu = (): Any => Components.utils;
const waive = <T>(value: T): Any => (value === null || value === undefined ? value : Cu().waiveXrays(value));

/** The class on the reader document's root while the plugin's own read runs. */
const TAKEOVER_CLASS = "speakd-takeover";
const STYLE_ID = "speakd-reader-style";
const STYLE = `
html.${TAKEOVER_CLASS} .read-aloud-popup { display: none !important; }
`;

/** How long Zotero's own voice catalogue may take before speakd is offered alone. */
const NATIVE_VOICES_TIMEOUT_MS = 8000;

const HIGHLIGHT_PREF = "reader.readAloud.highlightGranularity";
const VOICES_PREF = "reader.readAloudVoices";

export interface TakeoverOptions {
  link: Link;
  log(message: string): void;
  /** Something is wrong: said in Zotero's error console as well as the debug log. */
  warn(message: string, error?: unknown): void;
}

type Probe = readonly [name: string, check: () => unknown];

/** The first probe that fails, or null. A probe that throws fails. */
function firstMissing(probes: readonly Probe[]): string | null {
  for (const [name, check] of probes) {
    try {
      if (!check()) return name;
    } catch {
      return name;
    }
  }
  return null;
}

const isFunction = (value: unknown): boolean => typeof value === "function";

/** A reader, as the plugin's controls see it. */
export interface ReaderHandle {
  readonly adoption: Adoption;
  /** `zotero:<itemKey>`, or "" for a reader the plugin does not read. */
  readonly sourceId: string;
  readonly session: ReaderSession | null;
  /** Start the plugin's own read at `position` (a PDF position), or from the selection, saved or visible position when null. */
  startRead(position: unknown, intent: Intent): void;
  stop(): void;
  /** Pause or resume, through Zotero so its state follows. */
  setPaused(paused: boolean): void;
  /** One sentence back (-1) or ahead (1). */
  skip(direction: 1 | -1): void;
  /** Hear the handle's state change. Returns an unsubscribe. */
  onChange(listener: () => void): () => void;
}

/** A reader opened before the plugin started, or of a kind it does not read. */
class Unadopted implements ReaderHandle {
  readonly sourceId = "";
  readonly session = null;
  constructor(readonly adoption: Adoption) {}
  startRead(): void {}
  stop(): void {}
  setPaused(): void {}
  skip(): void {}
  onChange(): () => void {
    return () => {};
  }
}

/** One reader the plugin adopted. */
class Adopted implements ReaderHandle {
  private readonly takeover: Takeover;
  private readonly reader: Any;
  private _adoption: Adoption = { kind: "pending" };
  readonly sourceId: string;
  session: ReaderSession | null = null;

  private win: Any = null;
  private ir: Any = null;
  private manager: Any = null;
  private voicesSaved: { value: string | undefined } | null = null;
  private granularityForced = false;
  private hooked = false;
  private patchedVoices = new Set<Any>();
  private unsubscribeLink: (() => void) | null = null;
  private unload: (() => void) | null = null;
  private readonly changeListeners = new Set<() => void>();
  private closed = false;

  constructor(takeover: Takeover, reader: Any) {
    this.takeover = takeover;
    this.reader = reader;
    this.sourceId = `zotero:${String(reader._item?.key ?? "")}`;
  }

  get adoption(): Adoption {
    return this._adoption;
  }

  get tabID(): string | undefined {
    return this.reader.tabID;
  }

  private get log() {
    return this.takeover.options;
  }

  // ---- adoption ----

  /** At `_readers.push`: before `_open` reads the remote interface (X:267). */
  adopt(): void {
    const reader = this.reader;
    const missing = firstMissing([
      ["reader._window", () => reader._window],
      ["reader._initPromise", () => isFunction(reader._initPromise?.then)],
      ["reader._getReadAloudRemoteInterface", () => isFunction(reader._getReadAloudRemoteInterface) && reader._getReadAloudRemoteInterface.length === 1],
      ["reader._setReadAloudStatus", () => isFunction(reader._setReadAloudStatus)],
      ["reader._item.key", () => typeof reader._item?.key === "string"],
    ]);
    if (missing !== null) {
      this.refuse(`missing ${missing}`);
      return;
    }
    if (reader._type !== "pdf") {
      this._adoption = { kind: "refused", reason: "speakd reads PDFs only" };
      return;
    }
    const native = reader._getReadAloudRemoteInterface;
    // An instance property: at push time `_internalReader` does not exist
    // yet, so the Proxy writes it to the ReaderInstance itself (X:76-98).
    reader._getReadAloudRemoteInterface = (targetWindow: Any) => {
      let nativeInterface: Any = null;
      try {
        nativeInterface = native.call(reader, targetWindow);
      } catch (error) {
        this.log.warn("Zotero's own Read Aloud interface failed", error);
      }
      try {
        return this.makeInterface(targetWindow, nativeInterface);
      } catch (error) {
        this.log.warn("speakd's Read Aloud interface failed; Zotero's is used", error);
        return nativeInterface;
      }
    };
    reader._initPromise.then(
      () => this.guard("installing the reader hooks", () => this.installIframeHooks()),
      (error: unknown) => this.refuse(`the reader did not open: ${String(error)}`),
    );
  }

  /** After `_initPromise`: `_internalReader` and its Read Aloud manager exist (X:650). */
  private installIframeHooks(): void {
    if (this.closed) return;
    const reader = this.reader;
    const win = reader._iframeWindow;
    const ir = waive(reader._internalReader);
    const m = waive(ir?._readAloudManager);
    const missing = firstMissing([
      ["reader._iframeWindow", () => win && !Cu().isDeadWrapper(win)],
      ["_internalReader", () => ir],
      ["_internalReader._enableReadAloud", () => ir._enableReadAloud === true],
      ["_internalReader._readAloudManager", () => m],
      ["_internalReader.startReadAloudAtPosition", () => isFunction(ir.startReadAloudAtPosition)],
      ["_internalReader.toggleReadAloudPopup", () => isFunction(ir.toggleReadAloudPopup)],
      ["_internalReader.toggleReadAloudPaused", () => isFunction(ir.toggleReadAloudPaused)],
      ["_internalReader.setReadAloudHighlightGranularity", () => isFunction(ir.setReadAloudHighlightGranularity)],
      ["_internalReader._lockPositionToReadAloud", () => isFunction(ir._lockPositionToReadAloud)],
      ["_internalReader._state.readAloudState", () => {
        const state = waive(ir._state?.readAloudState);
        return state && "popupOpen" in state && "highlightGranularity" in state;
      }],
      ["_internalReader._state.loggedIn", () => "loggedIn" in waive(ir._state)],
      ["_internalReader._primaryView.setReadAloudState", () => isFunction(waive(ir._primaryView)?.setReadAloudState)],
      ["manager._createController", () => isFunction(m._createController)],
      ["manager._createController calls voice.getController", () => {
        const source = String(m._createController);
        return source.includes("getController(") && source.includes("'ActiveSegmentChange'");
      }],
      ["manager.setSegments calls _createController", () => String(m.setSegments).includes("_createController()")],
      ["manager.loadVoices", () => isFunction(m.loadVoices)],
      ["manager._voice", () => "_voice" in m],
      ["manager.selectVoice", () => isFunction(m.selectVoice)],
      ["manager.selectedVoiceID", () => "selectedVoiceID" in m],
      ["manager.allVoices", () => "allVoices" in m],
      ["manager.segments, activeSegment, active, paused", () => ["segments", "activeSegment", "active", "paused"].every((name) => name in m)],
      ["manager.pause, play, deactivate, skipAhead, skipBack", () => ["pause", "play", "deactivate", "skipAhead", "skipBack"].every((name) => isFunction(m[name]))],
      [`pref ${HIGHLIGHT_PREF}`, () => Zotero.Prefs.get(HIGHLIGHT_PREF) !== undefined],
      ["window.EventTarget, Event, Promise, Blob", () => ["EventTarget", "Event", "Promise", "Blob"].every((name) => isFunction(win[name]))],
    ]);
    if (missing !== null) {
      this.refuse(`missing ${missing}`);
      return;
    }
    this.win = win;
    this.ir = ir;
    this.manager = m;

    const document = win.document;
    if (!document.getElementById(STYLE_ID)) {
      const style = document.createElement("style");
      style.id = STYLE_ID;
      style.textContent = STYLE;
      document.documentElement.appendChild(style);
    }

    const originalLoad = m.loadVoices;
    const originalCreate = m._createController;
    // speakd is a remote voice, and remote voices load only for a user
    // logged in to Zotero (B:84271). Load them always; `getVoices` asks
    // Zotero's server only for a user who is.
    m.loadVoices = Cu().exportFunction(
      (..._args: unknown[]) => {
        try {
          return originalLoad.call(m, true);
        } catch (error) {
          this.log.warn("loading voices failed", error);
          return win.Promise.resolve();
        }
      },
      win,
    );
    m._createController = Cu().exportFunction(() => {
      try {
        if (this.guardVoice()) return undefined;
      } catch (error) {
        this.log.warn("the controller guard failed; Zotero's voice is used", error);
      }
      return originalCreate.call(m);
    }, win);
    this.hooked = true;

    const session = new ReaderSession(
      new Channel({ client: this.takeover.options.link, sourceId: this.sourceId, label: this.title() }),
      {
        defer: (task) => {
          Promise.resolve()
            .then(task)
            .catch((error) => this.log.warn("a deferred task failed", error));
        },
        takeover: (active) => this.guard("the takeover", () => this.setTakeover(active)),
        mirrorPause: (paused) =>
          this.guard("mirroring the pause", () => {
            if (m.active && m.paused !== paused) ir.toggleReadAloudPaused(paused);
          }),
      },
    );
    this.session = session;
    session.onChange(() => this.changed());
    this.unsubscribeLink = this.takeover.options.link.onEvent((event) => session.handleEvent(event));

    const onUnload = () => this.close();
    win.addEventListener("unload", onUnload);
    this.unload = () => {
      try {
        if (!Cu().isDeadWrapper(win)) win.removeEventListener("unload", onUnload);
      } catch {
        // The window is gone, and its listener with it.
      }
    };

    this._adoption = { kind: "ready" };
    this.log.log(`adopted the reader for ${this.sourceId}`);
    this.changed();
  }

  /**
   * The `_createController` guard. True when it has done the work itself:
   * speakd's voice was selected, which built the controller already.
   */
  private guardVoice(): boolean {
    const m = this.manager;
    const voice = waive(m._voice);
    const session = this.session;
    if (!voice || !session) return false;
    if (voice.id !== SPEAKD_VOICE_ID) {
      if (session.wanted) {
        const ours = Array.from(waive(m.allVoices) ?? []).some((candidate: Any) => waive(candidate).id === SPEAKD_VOICE_ID);
        if (!ours) {
          this.log.warn("speakd's voice is not in Zotero's list; the read is left to Zotero's voice");
          return false;
        }
        // Selecting persists the voice for the document's language
        // (B:82286-82300); what was there is put back when the read ends.
        if (this.voicesSaved === null) this.voicesSaved = { value: Zotero.Prefs.get(VOICES_PREF) as string | undefined };
        // Re-enters `_createController` with speakd's voice (B:82522-82537).
        m.selectVoice(SPEAKD_VOICE_ID);
        return true;
      }
      this.restoreGranularity();
      return false;
    }
    if (!this.patchedVoices.has(voice)) {
      voice.getController = Cu().exportFunction(
        (segments: Any, back: Any, forward: Any) => this.makeController(voice, segments, back, forward),
        this.win,
      );
      this.patchedVoices.add(voice);
    }
    // In `word` mode Zotero draws only word highlights, which speakd does
    // not give: no highlight at all (B:76507-76510).
    const state = waive(this.ir._state.readAloudState);
    if (state.highlightGranularity === "word") {
      this.ir.setReadAloudHighlightGranularity("sentence");
      this.granularityForced = true;
    }
    return false;
  }

  // ---- the controller (internals §8.3) ----

  private makeController(voice: Any, segments: Any, back: Any, forward: Any): Any {
    const win = this.win;
    const session = this.session;
    try {
      if (!session) throw new Error("no session");
      const list = waive(segments);
      const infos: SegmentInfo[] = [];
      for (let index = 0; index < list.length; index++) {
        const segment = waive(list[index]);
        infos.push({ text: String(segment.text ?? ""), anchor: typeof segment.anchor === "string" ? segment.anchor : null });
      }
      const target = new win.EventTarget();
      const sink = {
        emit: (type: string, index: number | null) => {
          if (Cu().isDeadWrapper(win)) return;
          const event = new win.Event(type);
          // The same object from Zotero's array: views compare by identity (B:76449).
          waive(event).segment = index === null ? null : list[index];
          target.dispatchEvent(event);
        },
      };
      const core = session.createController(
        infos,
        typeof back === "number" ? back : null,
        typeof forward === "number" ? forward : null,
        sink,
        // The manager's own array, read through the manager: the arguments
        // come wrapped afresh on every call, and would never compare equal.
        this.manager._segments,
      );
      this.fillController(target, core, list);
      return target;
    } catch (error) {
      // Zotero reads the controller at once; there must be one. Zotero's
      // own for this voice asks `getAudio`, which says "unknown" for
      // speakd's: an error in the popup, and no sound.
      this.log.warn("building speakd's controller failed", error);
      return Object.getPrototypeOf(voice).getController.call(voice, segments, back, forward);
    }
  }

  /** The controller contract the manager uses (internals §7 row 23), on a content EventTarget. */
  private fillController(target: Any, core: ControllerCore, list: Any): void {
    const win = this.win;
    const exported = waive(target);
    const safe =
      <A extends unknown[], R>(name: string, fn: (...args: A) => R, fallback: R) =>
      (...args: A): R => {
        try {
          return fn(...args);
        } catch (error) {
          this.log.warn(`the controller's ${name} failed`, error);
          return fallback;
        }
      };
    const resolved = () => win.Promise.resolve();
    const accessor = (name: string, get: () => unknown, set?: (value: Any) => void) => {
      Object.defineProperty(exported, name, {
        configurable: true,
        enumerable: true,
        get: Cu().exportFunction(safe(`${name} getter`, get, null), win),
        set: Cu().exportFunction(safe(`${name} setter`, set ?? (() => {}), undefined), win),
      });
    };
    accessor("paused", () => core.paused, (value) => (core.paused = value === true));
    accessor("speed", () => core.speed, (value) => {
      if (typeof value === "number") core.speed = value;
    });
    accessor("buffering", () => false);
    accessor("error", () => core.error);
    accessor("lastSkipGranularity", () => core.lastSkipGranularity);
    accessor("minutesRemaining", () => null);
    accessor("hasStandardMinutesRemaining", () => false);
    accessor("activeTimestampIndex", () => null);
    const method = (name: string, fn: (...args: Any[]) => unknown, fallback: unknown = undefined) => {
      exported[name] = Cu().exportFunction(safe(name, fn, fallback), win);
    };
    method("skipAhead", (granularity, accelerate) => core.skipAhead(String(granularity), accelerate === true));
    method("skipBack", (granularity, accelerate) => core.skipBack(String(granularity), accelerate === true));
    method("getSegmentToAnnotate", () => list[core.segmentToAnnotate()] ?? null, null);
    method("retry", () => {
      core.sink.emit("ErrorCleared", core.segmentToAnnotate());
      core.retry();
    });
    method("syncActiveWordToPlayback", () => {});
    method("refreshCreditsRemaining", resolved);
    method("resetCredits", resolved);
    method("getTimestampsForSegment", () => null, null);
    method("destroy", () => core.destroy());
  }

  // ---- the remote interface (internals §2, §8.1) ----

  private makeInterface(win: Any, native: Any): Any {
    const clone = (value: unknown) => Cu().cloneInto(value, win);
    const resolve = (value: unknown) => new win.Promise((ok: Any) => ok(clone(value)));
    const noCredits = { standardCreditsRemaining: null, premiumCreditsRemaining: null };
    const offering = () => this._adoption.kind === "ready";
    return {
      getVoices: () =>
        new win.Promise((ok: Any) => {
          let answered = false;
          const answer = (nativeAnswer: unknown) => {
            if (answered) return;
            answered = true;
            try {
              ok(clone(mergeVoices(nativeAnswer, offering())));
            } catch (error) {
              this.log.warn("answering getVoices failed", error);
              ok(clone({ error: "unknown" }));
            }
          };
          // Zotero's server is asked only for a user logged in to it: the
          // same as Zotero does, now that remote voices always load.
          const loggedIn = (() => {
            try {
              return waive(this.reader._internalReader)?._state?.loggedIn === true;
            } catch {
              return false;
            }
          })();
          if (!native || !loggedIn) {
            answer(null);
            return;
          }
          const timer = setTimeout(() => answer(null), NATIVE_VOICES_TIMEOUT_MS);
          try {
            native.getVoices().then(
              (result: unknown) => {
                clearTimeout(timer);
                let copy: unknown = null;
                try {
                  copy = JSON.parse(JSON.stringify(waive(result)));
                } catch {
                  // Unreadable: speakd alone.
                }
                answer(copy);
              },
              () => {
                clearTimeout(timer);
                answer(null);
              },
            );
          } catch {
            clearTimeout(timer);
            answer(null);
          }
        }),
      getAudio: (segment: unknown, impl: Any) => {
        const id = waive(impl)?.id;
        if (id === SPEAKD_VOICE_ID || (!native && segment === "sample")) {
          if (segment === "sample") {
            // Picking speakd in Zotero's list plays a sample (B:38585-38593): silence.
            const bytes = silentWav();
            const audio = new win.Blob([new win.Uint8Array(bytes)], { type: "audio/wav" });
            return new win.Promise((ok: Any) => ok(clone({ audio })));
          }
          this.log.warn("Zotero asked speakd's voice for audio: the takeover was bypassed");
          return resolve({ audio: null, error: "unknown" });
        }
        if (native) return native.getAudio(segment, impl);
        return resolve({ audio: null, error: "unknown" });
      },
      getCreditsRemaining: () => (native ? native.getCreditsRemaining() : resolve(noCredits)),
      resetCredits: () => (native ? native.resetCredits() : resolve(noCredits)),
    };
  }

  // ---- the plugin's own read ----

  startRead(position: unknown, intent: Intent): void {
    this.guard("starting a read", () => {
      const { ir, manager: m, session } = this;
      if (this._adoption.kind !== "ready" || !session) return;
      // Zotero's own voice is reading: end that first, so speakd's read
      // starts from a controller of its own rather than Zotero's `play`.
      if (m.active && !session.live) ir.toggleReadAloudPopup(false);
      session.want(intent);
      ir.startReadAloudAtPosition(position ?? null);
    });
  }

  stop(): void {
    this.guard("stopping", () => {
      this.session?.stop();
      this.closeReadAloud();
    });
  }

  setPaused(paused: boolean): void {
    this.guard("pausing", () => {
      const { ir, manager: m, session } = this;
      if (!session) return;
      if (m.active && session.live) {
        ir.toggleReadAloudPaused(paused);
      } else if (paused) {
        void session.channel.pause();
      } else {
        void session.channel.resume();
      }
    });
  }

  skip(direction: 1 | -1): void {
    this.guard("skipping", () => {
      const { ir, manager: m, session } = this;
      if (!session) return;
      if (m.active && session.live) {
        ir._lockPositionToReadAloud();
        if (direction === 1) m.skipAhead("sentence", false);
        else m.skipBack("sentence", false);
      } else {
        void session.channel.skip(direction);
      }
    });
  }

  onChange(listener: () => void): () => void {
    this.changeListeners.add(listener);
    return () => this.changeListeners.delete(listener);
  }

  private setTakeover(active: boolean): void {
    const root = this.win?.document?.documentElement;
    if (active) {
      root?.classList.add(TAKEOVER_CLASS);
      return;
    }
    root?.classList.remove(TAKEOVER_CLASS);
    this.closeReadAloud();
    if (this.voicesSaved !== null) {
      const { value } = this.voicesSaved;
      this.voicesSaved = null;
      if (value === undefined) Zotero.Prefs.clear(VOICES_PREF);
      else Zotero.Prefs.set(VOICES_PREF, value);
    }
    this.restoreGranularity();
  }

  /** Close Zotero's Read Aloud: the one way to clear its highlight (B:76434). */
  private closeReadAloud(): void {
    const ir = this.ir;
    if (!ir || Cu().isDeadWrapper(this.win)) return;
    if (waive(ir._state.readAloudState).popupOpen) ir.toggleReadAloudPopup(false);
  }

  private restoreGranularity(): void {
    if (!this.granularityForced) return;
    this.granularityForced = false;
    const wanted = Zotero.Prefs.get(HIGHLIGHT_PREF);
    if (typeof wanted === "string") this.ir.setReadAloudHighlightGranularity(wanted);
  }

  // ---- ending ----

  /** The tab closed or the window unloaded: hush once, and let go. */
  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.guard("closing", () => this.session?.close());
    this.unsubscribeLink?.();
    this.unload?.();
    this.takeover.forget(this);
    this.changed();
    this.changeListeners.clear();
  }

  /** The plugin is shutting down: stop its read, undo every hook, and let go. */
  uninstall(): void {
    this.guard("uninstalling", () => {
      if (this.session?.live || this.session?.wanted) this.stop();
      const win = this.win;
      if (this.hooked && win && !Cu().isDeadWrapper(win)) {
        // Own properties shadowing the prototype's: deleting them restores Zotero's.
        delete this.manager.loadVoices;
        delete this.manager._createController;
        for (const voice of this.patchedVoices) delete voice.getController;
        win.document.getElementById(STYLE_ID)?.remove();
        win.document.documentElement.classList.remove(TAKEOVER_CLASS);
      }
      this.patchedVoices.clear();
      // The interface already handed to the reader stays there (X:648);
      // this only stops a reopened one from getting it.
      delete this.reader._getReadAloudRemoteInterface;
    });
    this.close();
  }

  private refuse(reason: string): void {
    this._adoption = { kind: "refused", reason };
    this.log.warn(`speakd reader refuses ${this.sourceId}: ${reason}. It stays Zotero's own.`);
    this.changed();
  }

  private title(): string {
    try {
      const item = this.reader._item;
      return String(item.parentItem?.getDisplayTitle() || item.getDisplayTitle() || item.key);
    } catch {
      return this.sourceId;
    }
  }

  private guard(what: string, action: () => void): void {
    try {
      action();
    } catch (error) {
      this.log.warn(`speakd reader: ${what} failed`, error);
    }
  }

  private changed(): void {
    for (const listener of [...this.changeListeners]) {
      try {
        listener();
      } catch (error) {
        this.log.warn("a reader listener failed", error);
      }
    }
  }
}

export class Takeover {
  readonly options: TakeoverOptions;
  private readonly adopted = new Map<string, Adopted>();
  private originalPush: Any = null;
  private readers: Any = null;
  private notifierID: string | null = null;

  constructor(options: TakeoverOptions) {
    this.options = options;
  }

  /** Adopt every reader opened from now on. Readers already open stay Zotero's own. */
  install(): void {
    const readers = (Zotero.Reader as Any)._readers;
    if (!Array.isArray(readers)) {
      this.options.warn("speakd reader: Zotero.Reader._readers is missing; no reader will be read aloud");
      return;
    }
    const takeover = this;
    const original = readers.push;
    this.readers = readers;
    this.originalPush = original;
    readers.push = function (this: Any[], ...pushed: Any[]) {
      for (const reader of pushed) {
        try {
          takeover.adopt(reader);
        } catch (error) {
          takeover.options.warn("speakd reader: adopting a reader failed", error);
        }
      }
      return original.apply(this, pushed);
    };
    // A closed tab's reader is uninitialised and dropped (X:2802-2808),
    // with no event of the reader's own; Zotero's notifier says so.
    this.notifierID = Zotero.Notifier.registerObserver(
      {
        notify: (event: string, type: string, ids: unknown[]) => {
          if (type !== "tab" || event !== "close") return;
          for (const record of [...this.adopted.values()]) {
            if (ids.includes(record.tabID)) record.close();
          }
        },
      },
      ["tab"],
      "speakd-reader",
    );
  }

  uninstall(): void {
    if (this.readers && this.originalPush) {
      // The wrapper is an own property over Array.prototype.push.
      delete this.readers.push;
      if (this.readers.push !== this.originalPush) this.readers.push = this.originalPush;
    }
    this.readers = null;
    this.originalPush = null;
    if (this.notifierID !== null) Zotero.Notifier.unregisterObserver(this.notifierID);
    this.notifierID = null;
    for (const record of [...this.adopted.values()]) record.uninstall();
    this.adopted.clear();
  }

  /** The handle for a reader, whichever side of Zotero's Proxy it is (X:76-98). */
  handleFor(reader: Any): ReaderHandle {
    const id = reader?._instanceID;
    const record = typeof id === "string" ? this.adopted.get(id) : undefined;
    return record ?? new Unadopted({ kind: "unadopted" });
  }

  /** Every adopted reader's handle. */
  handles(): ReaderHandle[] {
    return [...this.adopted.values()];
  }

  forget(record: Adopted): void {
    for (const [id, candidate] of this.adopted) {
      if (candidate === record) this.adopted.delete(id);
    }
  }

  private adopt(reader: Any): void {
    const id = reader?._instanceID;
    if (typeof id !== "string" || this.adopted.has(id)) return;
    const record = new Adopted(this, reader);
    this.adopted.set(id, record);
    record.adopt();
  }
}
