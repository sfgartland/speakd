// Defaults for the plugin's preferences, which Zotero loads from the
// plugin's root. The pane is addon/content/preferences.xhtml.
pref("extensions.speakd-reader.port", 8642);
pref("extensions.speakd-reader.token", "");
// How a read treats footnotes (src/footnotes.ts): drop the bare citations and
// read the rest at their section's end (classify), drop every footnote, or
// keep and defer every one.
pref("extensions.speakd-reader.footnote_policy", "classify");
// The audiobook export dialog's starting points (src/export/dialog.ts).
// Formats are mp3, opus or m4b; the destination is attach or folder; an
// empty folder means ask.
pref("extensions.speakd-reader.export_format_article", "mp3");
pref("extensions.speakd-reader.export_format_book", "m4b");
pref("extensions.speakd-reader.export_destination", "attach");
pref("extensions.speakd-reader.export_folder", "");
pref("extensions.speakd-reader.export_skip_references", true);
// The audiobook renders the plugin has started and not yet attached or
// reported, as JSON (src/export/jobs.ts). Not a setting: the daemon's jobs
// do not say which item they are for, so this is how a render that goes on
// while Zotero is closed is found again on the next start.
pref("extensions.speakd-reader.render_jobs", "[]");
