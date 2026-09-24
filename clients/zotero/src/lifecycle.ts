// Taking the running plugin apart, in the order that lets every reader's
// last word reach speakd.
//
// Pure: index.ts hands in the parts it built at startup.

export interface RunningParts {
  unobserve(): void;
  controls: { unregister(): void };
  /** Stops every reader's read; resolves once what that asked of speakd has been sent. */
  takeover: { uninstall(): Promise<void> | void };
  link: { close(): void };
}

/** A reader, as a quitting Zotero sees it. */
export interface Holder {
  readonly sourceId: string;
  /** Whether the daemon may hold something of its channel's. */
  readonly holding: boolean;
}

/**
 * Zotero is quitting: there is no time to take anything apart, and no
 * waiting on answers, but speakd must not go on reading a closed Zotero's
 * PDF. A hush for each reader that may have something queued, sent and not
 * awaited; best effort, since the process may end before it is out.
 */
export function hushOnQuit(
  readers: Iterable<Holder>,
  call: (verb: string, sourceId: string, payload: Record<string, unknown>) => Promise<unknown>,
): void {
  for (const reader of readers) {
    if (!reader.holding) continue;
    try {
      call("hush", reader.sourceId, {}).catch(() => {});
    } catch {
      // The next reader's hush is still worth sending.
    }
  }
}

/** How long a shutdown waits for the readers' hushes before closing the link anyway. */
export const SETTLE_MS = 2000;

/**
 * Undo everything, closing the link last. Stopping a read queues its hush
 * behind anything the channel already has on the way; closing the link
 * first would leave that hush with no client to go through, and speakd
 * reading on -- a section or two -- after the plugin is gone.
 */
export async function dismantle(parts: RunningParts, settleMs = SETTLE_MS): Promise<void> {
  parts.unobserve();
  parts.controls.unregister();
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([
      new Promise<void>((resolve) => resolve(parts.takeover.uninstall())).catch(() => {}),
      new Promise<void>((resolve) => (timer = setTimeout(resolve, settleMs))),
    ]);
  } finally {
    clearTimeout(timer);
    parts.link.close();
  }
}
