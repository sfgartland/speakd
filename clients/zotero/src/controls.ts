// The plugin's UI in the reader: "Read selection" and "Read from here" in
// the text selection popup, and a small control bar in the reader's toolbar.
// Both through Zotero's official reader event hooks (internals §6).
//
// The bar shows what bar.ts works out from the daemon's state; it never
// guesses. Its buttons act on this reader's channel only, except speed,
// which is the listener's own and global.

import { describeBar, nextSpeed, type BarView } from "./bar";
import type { Link } from "./link";
import type { ReaderHandle, Takeover } from "./reader-takeover";

/* eslint-disable @typescript-eslint/no-explicit-any */
declare const Components: any;
type Any = any;

const STYLE_ID = "speakd-reader-controls-style";
const STYLE = `
.speakd-bar { display: flex; align-items: center; gap: 1px; margin-inline-end: 8px; }
.speakd-bar .speakd-button { width: auto; min-width: 26px; padding: 0 5px; font-size: 13px; line-height: 1; }
.speakd-bar .speakd-button:disabled { opacity: 0.35; }
.speakd-bar .speakd-speed { font-size: 11px; min-width: 3em; text-align: center; font-variant-numeric: tabular-nums; }
.speakd-bar .speakd-status { font-size: 11px; max-width: 18em; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; opacity: 0.8; margin-inline-start: 4px; }
.speakd-bar[data-kind="bad-token"] .speakd-status,
.speakd-bar[data-kind="no-daemon"] .speakd-status,
.speakd-bar[data-kind="refused"] .speakd-status,
.speakd-bar[data-kind="problem"] .speakd-status { color: #c0392b; opacity: 1; }
`;

export interface ControlsOptions {
  pluginID: string;
  takeover: Takeover;
  link: Link;
  warn(message: string, error?: unknown): void;
}

interface Bar {
  root: Any;
  dispose(): void;
}

export class Controls {
  private readonly options: ControlsOptions;
  private readonly bars = new Set<Bar>();
  private readonly handlers: [string, (event: Any) => void][] = [];

  constructor(options: ControlsOptions) {
    this.options = options;
  }

  register(): void {
    const on = (type: string, handler: (event: Any) => void) => {
      const safe = (event: Any) => {
        try {
          handler(event);
        } catch (error) {
          this.options.warn(`speakd reader: ${type} failed`, error);
        }
      };
      this.handlers.push([type, safe]);
      (Zotero.Reader as Any).registerEventListener(type, safe, this.options.pluginID);
    };
    on("renderToolbar", (event) => this.renderToolbar(event));
    on("renderTextSelectionPopup", (event) => this.renderSelectionPopup(event));
  }

  unregister(): void {
    for (const [type, handler] of this.handlers) {
      try {
        (Zotero.Reader as Any).unregisterEventListener(type, handler);
      } catch {
        // Zotero drops a plugin's listeners on shutdown anyway.
      }
    }
    this.handlers.length = 0;
    for (const bar of [...this.bars]) bar.dispose();
  }

  // ---- the selection popup ----

  private renderSelectionPopup(event: Any): void {
    const { reader, doc, params, append } = event;
    const handle = this.options.takeover.handleFor(reader);
    if (handle.adoption.kind !== "ready") return;
    const annotation = Components.utils.waiveXrays(params)?.annotation;
    const position = annotation ? Components.utils.waiveXrays(annotation).position : null;
    const text = annotation ? String(Components.utils.waiveXrays(annotation).text ?? "") : "";
    const button = (label: string, action: () => void) => {
      const element = doc.createElement("button");
      element.className = "toolbar-button wide-button";
      element.textContent = label;
      element.addEventListener("click", () => this.act(action));
      return element;
    };
    append(
      button("Read selection", () => handle.startRead(position, { kind: "selection", text })),
      button("Read from here", () => handle.startRead(position, { kind: "here" })),
    );
  }

  // ---- the control bar ----

  private renderToolbar(event: Any): void {
    const { reader, doc, append } = event;
    const handle = this.options.takeover.handleFor(reader);
    const link = this.options.link;
    if (!doc.getElementById(STYLE_ID)) {
      const style = doc.createElement("style");
      style.id = STYLE_ID;
      style.textContent = STYLE;
      (doc.head ?? doc.documentElement).appendChild(style);
    }

    const root = doc.createElement("div");
    root.className = "speakd-bar";
    root.setAttribute("role", "group");
    root.setAttribute("aria-label", "speakd");
    let view: BarView | null = null;
    const button = (glyph: string, label: string, action: () => void) => {
      const element = doc.createElement("button");
      element.className = "toolbar-button speakd-button";
      element.textContent = glyph;
      element.title = label;
      element.setAttribute("aria-label", label);
      element.tabIndex = -1;
      element.addEventListener("click", () => this.act(action));
      root.appendChild(element);
      return element;
    };
    const play = button("▶", "Read from here", () => {
      if (view?.play.action === "pause") handle.setPaused(true);
      else if (view?.play.action === "resume") handle.setPaused(false);
      else handle.startRead(null, { kind: "here" });
    });
    const back = button("←", "Back one sentence", () => handle.skip(-1));
    const ahead = button("→", "Forward one sentence", () => handle.skip(1));
    const slower = button("−", "Slower", () => this.speed(handle, -1));
    const speed = doc.createElement("span");
    speed.className = "speakd-speed";
    speed.title = "speakd's speed, for everything it reads";
    root.appendChild(speed);
    const faster = button("+", "Faster", () => this.speed(handle, 1));
    const stop = button("■", "Stop", () => handle.stop());
    const status = doc.createElement("span");
    status.className = "speakd-status";
    status.setAttribute("aria-live", "polite");
    root.appendChild(status);

    const render = () => {
      if (Components.utils.isDeadWrapper(root)) {
        bar.dispose();
        return;
      }
      const session = handle.session;
      view = describeBar({
        adoption: handle.adoption,
        stream: link.state.stream,
        problem: session?.problem ?? null,
        ownChannel: handle.sourceId,
        speakingChannel: link.state.speakingChannel,
        paused: link.state.paused,
        reading: session?.channel.reading ?? false,
        speed: link.state.speed,
      });
      root.dataset.kind = view.kind;
      const playLabel = { start: "Read from here", pause: "Pause", resume: "Resume" }[view.play.action];
      play.textContent = view.play.action === "pause" ? "❚❚" : "▶";
      play.title = playLabel;
      play.setAttribute("aria-label", playLabel);
      play.disabled = !view.play.enabled;
      back.disabled = !view.back.enabled;
      ahead.disabled = !view.ahead.enabled;
      slower.disabled = !view.slower.enabled;
      faster.disabled = !view.faster.enabled;
      stop.disabled = !view.stop.enabled;
      speed.textContent = view.speed;
      status.textContent = view.status;
      status.title = view.status;
    };
    const safeRender = () => {
      try {
        render();
      } catch (error) {
        this.options.warn("speakd reader: drawing the bar failed", error);
      }
    };
    const unsubscribe = [link.onChange(safeRender), handle.onChange(safeRender)];
    const bar: Bar = {
      root,
      dispose: () => {
        if (!this.bars.delete(bar)) return;
        for (const off of unsubscribe) off();
        try {
          if (!Components.utils.isDeadWrapper(root)) root.remove();
        } catch {
          // Gone with its document.
        }
      },
    };
    this.bars.add(bar);
    safeRender();
    append(root);
  }

  private speed(handle: ReaderHandle, direction: 1 | -1): void {
    const link = this.options.link;
    void link.call("set_speed", handle.sourceId, { speed: nextSpeed(link.state.speed, direction) });
  }

  private act(action: () => void): void {
    try {
      action();
    } catch (error) {
      this.options.warn("speakd reader: a control failed", error);
    }
  }
}
