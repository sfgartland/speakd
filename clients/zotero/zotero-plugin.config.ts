import { defineConfig } from "zotero-plugin-scaffold";

// The plugin's identity lives here and in addon/manifest.json only. The
// scaffold writes `id` and `update_url` into the built manifest itself, so
// they are given once, here, rather than kept in step by hand.
export default defineConfig({
  source: ["src", "addon"],
  dist: ".scaffold/build",
  name: "speakd reader",
  id: "speakd-reader@speakd",
  namespace: "speakd-reader",
  xpiName: "speakd-reader",
  // A placeholder in the repository, so the manifest carries the update_url
  // Zotero requires. Nothing is published there yet; the plugin is installed
  // from the built .xpi by hand.
  updateURL: "https://raw.githubusercontent.com/sfgartland/speakd/main/clients/zotero/update.json",
  xpiDownloadLink: "https://github.com/sfgartland/speakd/releases/download/zotero-v{{version}}/{{xpiName}}.xpi",
  build: {
    assets: ["addon/**/*.*"],
    // One Fluent file, for the export pane's header (addon/locale). Its file
    // and message names already carry the plugin's name, so the scaffold is
    // not to prefix them again; no preferences typings either.
    fluent: { dts: false, prefixFluentMessages: false, prefixLocaleFiles: false },
    prefs: { dts: false },
    esbuildOptions: [
      {
        entryPoints: ["src/index.ts"],
        bundle: true,
        // One classic script, loaded by bootstrap.js with loadSubScript into
        // a scope of its own: the global name is how bootstrap finds the
        // startup and shutdown hooks inside it.
        format: "iife",
        globalName: "SpeakdReader",
        target: "firefox140",
        outfile: ".scaffold/build/addon/content/scripts/speakd-reader.js",
      },
    ],
  },
});
