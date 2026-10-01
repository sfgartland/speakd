// Getting a PDF's segments for an export: the text exactly as Zotero's Read
// Aloud would read it, captured through the takeover with nothing played
// (reader-takeover.ts `capture`, session.ts `ReaderSession.capture`).
//
// A reader already open on the PDF is used as it is, if the plugin adopted
// it. Otherwise the PDF is opened in a background tab just for this, and
// that tab closed again afterwards, however the capture went.
//
// `captureSegments` is pure, over a `CaptureHost`; `zoteroCaptureHost` is
// that host in Zotero.

import type { Adoption } from "../bar";
import type { CaptureResult } from "../session";

/* eslint-disable @typescript-eslint/no-explicit-any */
type Any = any;

export type { CaptureResult, CapturedSegment } from "../session";

/**
 * How long a reader opened for an export may take to be adopted: until its
 * `_initPromise`, which loads the PDF itself. The segmentation that follows
 * has its own, longer wait (session.ts `CAPTURE_WAIT_MS`).
 */
export const ADOPTION_WAIT_MS = 60_000;

/** What a capture needs of a reader's handle: reader-takeover.ts's ReaderHandle. */
export interface CaptureHandle {
  readonly adoption: Adoption;
  capture(): Promise<CaptureResult>;
  onChange(listener: () => void): () => void;
}

/** Zotero's readers, as a capture needs them. `R` is a reader. */
export interface CaptureHost<R> {
  /** A reader open on the attachment, in a tab or a window, or null. */
  find(itemID: number): R | null;
  /** Open the attachment in a new background tab. */
  open(itemID: number): Promise<R | null>;
  close(reader: R): void;
  handleFor(reader: R): CaptureHandle;
  /** Run `task` after `ms`. Returns a cancel. */
  later(task: () => void, ms: number): () => void;
}

/** Why a reader in this state gives no segments. */
export function adoptionProblem(adoption: Adoption): CaptureResult {
  const error =
    adoption.kind === "refused"
      ? adoption.reason
      : adoption.kind === "unadopted"
        ? "this tab was open before speakd's plugin started: reopen it"
        : "the reader is not ready";
  return { ok: false, problem: { kind: "zotero", error } };
}

const failed = (error: string): CaptureResult => ({ ok: false, problem: { kind: "zotero", error } });

/** The adoption once it is no longer pending, or null if it stays pending past `ADOPTION_WAIT_MS`. */
function adopted<R>(host: CaptureHost<R>, handle: CaptureHandle): Promise<Adoption | null> {
  if (handle.adoption.kind !== "pending") return Promise.resolve(handle.adoption);
  return new Promise((resolve) => {
    const done = (adoption: Adoption | null) => {
      unsubscribe();
      cancel();
      resolve(adoption);
    };
    const unsubscribe = handle.onChange(() => {
      if (handle.adoption.kind !== "pending") done(handle.adoption);
    });
    const cancel = host.later(() => done(null), ADOPTION_WAIT_MS);
  });
}

/**
 * The segments of PDF attachment `itemID`, in reading order, each with its
 * text, page and paragraph anchor. Never rejects: a failure is a problem.
 * After a capture that worked, `whileOpen` runs on the reader before a
 * reader opened for this is closed, so that a large PDF is opened once.
 */
export async function captureSegments<R>(
  itemID: number,
  host: CaptureHost<R>,
  whileOpen?: (reader: R) => Promise<void>,
): Promise<CaptureResult> {
  let reader = host.find(itemID);
  let opened = false;
  // A reader opened before the plugin started -- a tab Zotero restored --
  // stays Zotero's own; another, opened now, is the plugin's.
  if (reader !== null && host.handleFor(reader).adoption.kind === "unadopted") reader = null;
  if (reader === null) {
    try {
      reader = await host.open(itemID);
    } catch (error) {
      return failed(`the PDF could not be opened: ${String(error)}`);
    }
    if (reader === null) return failed("the PDF could not be opened");
    opened = true;
  }
  try {
    const handle = host.handleFor(reader);
    const adoption = await adopted(host, handle);
    if (adoption === null) return failed("the PDF did not open in time");
    if (adoption.kind !== "ready") return adoptionProblem(adoption);
    const result = await handle.capture();
    if (result.ok && whileOpen) await whileOpen(reader);
    return result;
  } catch (error) {
    return failed(`capturing the segments failed: ${String(error)}`);
  } finally {
    if (opened) host.close(reader);
  }
}

/** The host in Zotero, over the plugin's takeover. */
export function zoteroCaptureHost(takeover: { handleFor(reader: unknown): CaptureHandle }): CaptureHost<Any> {
  const reader = Zotero.Reader as Any;
  return {
    // A tab whose reader was dropped (closed, or unloaded) is not one.
    find: (itemID) => reader._readers.find((candidate: Any) => candidate.itemID === itemID && !candidate._isTabClosed) ?? null,
    // `allowDuplicate`: without it, an unloaded tab of the PDF -- one
    // restored from the last session -- would be selected instead, in the
    // foreground, and nothing returned.
    open: async (itemID) => (await reader.open(itemID, null, { openInBackground: true, allowDuplicate: true })) ?? null,
    close: (opened) => {
      if (opened.tabID) opened._window.Zotero_Tabs.close(opened.tabID);
    },
    handleFor: (opened) => takeover.handleFor(opened),
    later: (task, ms) => {
      const timer = setTimeout(task, ms);
      return () => clearTimeout(timer);
    },
  };
}
