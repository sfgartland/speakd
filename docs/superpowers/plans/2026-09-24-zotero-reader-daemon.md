# Zotero Reader, Phase 1: the Daemon Side — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Everything the Zotero plugin needs from speakd: a built-in transform-free `pdf` profile, and an authenticated loopback HTTP+SSE transport over the same `Request`/`Response` handler the Unix socket serves.

**Architecture:** `transport_http.py`, stdlib `http.server`, is an adapter from `POST /v1/<verb>` to `Daemon.handle` and from `EventBus` to a server-sent-events stream. `http_token.py` creates and reads the per-user bearer token. `__main__.serve` starts the HTTP server beside the socket server, and stops it at the same point.

**Spec:** `docs/design/2026-09-24-zotero-reader-design.md` (its *Revision, 2026-09-24* is binding).

## Global Constraints

- Standard library only.
- Bind `127.0.0.1` only. The port comes from `SPEAKD_HTTP_PORT` (default 8642; `0` disables the transport).
- Every request needs `Authorization: Bearer <token>`: 401 otherwise. The `Host` header must be `127.0.0.1:<port>` or `localhost:<port>`: 421 otherwise. No CORS headers, ever.
- Verbs over HTTP: `enqueue cancel hush pause resume seek replay set_label set_speed status`. Anything else is 404, and `set_capabilities` in particular is never exposed.
- The token lives at `$XDG_CONFIG_HOME/speakd/http-token`, mode 0600, created on first start.
- A slow SSE reader must never stall speech: each client gets a bounded queue, and overflowing it drops that client.

## Review Focus

- A request with a body that isn't JSON, or isn't an object, should get 400 with a JSON error, and the server keeps serving.
- An SSE client that disconnects mid-stream should be unsubscribed from the bus, not leaked. Tested in Task 3.
- A request with no `Content-Length`, or an oversized body over 1 MiB, should get 411 or 413, not a hang. Tested in Task 3.
- A token file with the wrong mode, or one that isn't readable, should be recreated, or refused with a clear message, not silently world-readable. Tested in Task 2.
- A port already in use should make the daemon log it and run without HTTP, not crash. Tested in Task 4.

### Task 1: the built-in `pdf` profile
- [ ] Test in `tests/test_profiles.py`: `load_profiles(missing_path)["pdf"]` has `transforms == ()`, and a user file's `[pdf]` section overrides it.
- [ ] Add `PDF_PROFILE = Profile(name="pdf", transforms=())` beside `NOTIFICATION_PROFILE`, with a comment on why (sentence-exact spans for highlighting), and register it in `load_profiles`' built-ins.
- [ ] Commit.

### Task 2: the HTTP token
- [ ] Tests in `tests/test_http_token.py`:
  - `ensure(path)` creates a 43-character urlsafe token with mode 0600 and returns it.
  - A second call returns the same token.
  - A file that is group- or world-readable is re-chmodded to 0600.
  - An empty file is regenerated.
  - `speakctl http-token` prints it.
- [ ] Write `src/speakd/http_token.py` with `token_path()`, `ensure(path=None) -> str` and `read(path=None) -> str | None`, and add the CLI subcommand.
- [ ] Commit.

### Task 3: `transport_http.py`
- [ ] Tests in `tests/test_transport_http.py`, against a real `HttpServer` on port 0 with a Daemon, FakeEngine and a pausable player:
  - With no or wrong token: 401.
  - With the right token and a bad `Host`: 421.
  - `POST /v1/status`: 200 with the status data.
  - `POST /v1/enqueue {"source_id": "zotero:K", "payload": {...}}`: spoken, and visible in status.
  - `POST /v1/set_capabilities`: 404. An unknown verb: 404.
  - A non-JSON or non-object body: 400, and the server still answers next time.
  - No `Content-Length`: 411. A body over 1 MiB: 413.
  - `GET /v1/health`: `{"ok": true}`.
  - `GET /v1/events`: a stream whose `data:` lines carry `{"event","source_id","data"}`, the same shape as the socket's.
  - A client that closes has its subscription removed (`bus.subscriber_count()` drops back).
  - A client that never reads is dropped when its queue overflows, and speech continues.
  - The response never includes `Access-Control-Allow-Origin`.
- [ ] Implement `HttpServer(port, handler, bus, token, host="127.0.0.1")` with `.start()`, `.stop()` and `.port`, using `ThreadingHTTPServer` with daemon threads. SSE: queue per client, `maxsize=1024`, a `": ping"` comment every 15 s, `Cache-Control: no-cache`.
- [ ] Commit.

### Task 4: serve it
- [ ] Tests:
  - `__main__.http_port()` reads `SPEAKD_HTTP_PORT` (defaulting to 8642), and treats `0` or garbage as disabled with a message.
  - `start_http(daemon)` returns None and logs when the port is taken.
- [ ] Wire it into `serve()`: start after `server.start()`, stop right after `server.stop()`.
- [ ] Commit.

### Task 5: document it
- [ ] Add a README section, "Reading PDFs in Zotero", covering the transport, the token and the port. The plugin section follows in phase 2.
- [ ] Commit, then run the full suite, ruff and mypy.
