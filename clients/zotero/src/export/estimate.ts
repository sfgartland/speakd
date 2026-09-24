// Estimating how long a render will take and how long the result will be,
// for the dialog's estimate line.
//
// Pure arithmetic and formatting: character count in, a rounded English
// phrase out. The daemon reports a measured real-time factor (how many
// seconds of wall clock a second of audio costs to render) in `status`;
// callers pass that when they have it, and the default otherwise.

// Kokoro speaks at roughly 15 characters per second of audio at speed 1.0 --
// a rough English-prose average, good enough for an estimate line, not a
// promise.
export const DEFAULT_CHARS_PER_SECOND = 15;

/** A render that takes about as long as the audio itself, absent a measurement. */
export const DEFAULT_RTF = 1;

/**
 * How many seconds of wall-clock time a render of `characterCount`
 * characters is expected to take.
 *
 * `speed` divides the character rate (a faster voice reads the same text
 * in less audio), and `rtf` (real-time factor) multiplies the result,
 * since rendering can take more or less wall-clock time than the audio it
 * produces.
 */
export function estimateSeconds(characterCount: number, speed = 1, rtf = DEFAULT_RTF): number {
  const audioSeconds = characterCount / (DEFAULT_CHARS_PER_SECOND * speed);
  return audioSeconds * rtf;
}

/**
 * Formats a duration in seconds as a rough English phrase for the dialog,
 * such as "about 1 h 20 min", "about 45 min" or "about 30 s". Always
 * rounds, and never reports zero for a positive duration.
 */
export function formatEstimate(seconds: number): string {
  const totalSeconds = Math.max(1, Math.round(seconds));
  if (totalSeconds < 60) return `about ${totalSeconds} s`;

  const totalMinutes = Math.round(totalSeconds / 60);
  if (totalMinutes < 60) return `about ${totalMinutes} min`;

  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return minutes === 0 ? `about ${hours} h` : `about ${hours} h ${minutes} min`;
}
