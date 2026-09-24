/* exported speakdTestAuthorize */
// The test bridge's gate, kept apart so that the unit tests run the very
// code the bridge does (test/bridge-authorize.test.ts). A plain script: the
// bridge's bootstrap loads it into its own scope.
//
// A request is let through only when it carries the token setup-profile.sh
// wrote into the profile (X-Speakd-Test-Token), and names this port on
// loopback as its Host, which a page reaching the port through DNS
// rebinding cannot. Returns null to let it through, or why it was refused.
function speakdTestAuthorize(headers, token, port) {
  if (typeof token !== "string" || token.length < 16) {
    return "no test token set up: run test-live/setup-profile.sh";
  }
  const host = String(headers.host || "").toLowerCase();
  if (host !== `127.0.0.1:${port}` && host !== `localhost:${port}`) {
    return "wrong Host";
  }
  const given = String(headers["x-speakd-test-token"] || "");
  // Every character compared, whatever the first difference.
  let differ = given.length !== token.length ? 1 : 0;
  for (let i = 0; i < token.length; i++) {
    differ |= token.charCodeAt(i) ^ given.charCodeAt(i % Math.max(given.length, 1));
  }
  return differ === 0 ? null : "missing or wrong X-Speakd-Test-Token";
}
