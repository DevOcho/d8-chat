# End-to-End Tests (Playwright)

Browser-driven smoke tests that drive the real app: log in, send a message,
upload an image, render a code fence, etc. They run **against a live
deployment** (your local `auto` cluster, or a CI test environment), not
against an in-process Flask app.

These are kept separate from the unit/integration suite because they need
the browser binaries, a running app, and external services (Postgres,
Valkey, MinIO).

---

## What's covered

| File | Tests |
|---|---|
| `test_auth.py` | Login success/failure, logout, forgot-password (no enumeration), forgot-password link visible |
| `test_messaging.py` | Send a plain message, fenced code blocks render as `<pre><code>` (and don't execute scripts), inline code, URL linkification with `target=_blank rel=noopener` |
| `test_attachments.py` | PNG image upload renders inline; PDF and TXT attachments render as a download link; CSRF on the bypass-HTMX `fetch()` upload still works |
| `test_admin.py` | Admin creates a user through the HTMX admin UI; admin dashboard loads (verifies vendored Chart.js works) |
| `test_security_headers.py` | `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Content-Security-Policy` (with no third-party origins; strict `script-src 'self'` on the local cluster), HSTS over HTTPS, tighter CSP on `/api/v1/*` |
| `test_realtime.py` | **Two users, two browsers, no reloads.** Channel message arrives live; typing indicator clears when the typist leaves mid-word; unread bold appears live and reading in one tab clears it in the user's other tab (read sync); `@mention` badge; DM started from the "new message" search appears in the recipient's sidebar, badges, then delivers live; thread reply lands in the other user's open thread panel and bumps the reply count; reactions (picker + pill toggle) with a per-viewer "you reacted" highlight; edit and delete propagate; poll appears and vote tallies update; messages missed while the socket was down are backfilled on reconnect and live delivery resumes |
| `test_search.py` | Global search finds a message and jumps to it in its channel; another user's private (notes-to-self) message never shows up in your results |
| `test_pager.py` | On-call pager: profile → Notifications → "Test my pager" raises the full-screen alert over the WebSocket; "Stop test" clears it |
| `test_presence.py` | The web activity heartbeat (`POST /profile/activity`) is accepted |
| (every test) | CSP violations are recorded in every browser context (`conftest.py`); a test fails on a violation of a directive listed in `CSP_FAIL_DIRECTIVES` (`script-src*` since CSP phase 4), and the run ends with a burn-down summary of everything the strict policy would still block |
| `test_accessibility.py` | axe-core (WCAG 2.1 A + AA) finds no violations on login, forgot-password, a channel (light and dark), the quick switcher, the new-message modal, the profile panel, Unreads, Threads, search results and the admin pages. Needs `npm install` (axe-core is a dev dependency); skipped without it |

The audit specifically flagged image/code-block/attachment paths as
historically brittle, so those get extra coverage. Realtime multi-user
behaviour is where production bugs have come from, so `test_realtime.py` is
the bulk of the suite.

### Known app bugs (strict `xfail`)

None right now. When a test documents an unfixed bug, mark it
`@pytest.mark.xfail(strict=True, reason=...)`: it reports `x` while the bug
exists and turns into a **failure** (XPASS) once it's fixed — then delete the
marker.

Regressions this suite caught when it was written (all fixed, now plain
tests): the activity heartbeat was rejected by CSRF (`test_presence.py`); the
composer's poll button inherited `hx-swap="none"` from `#message-form`, so the
poll modal never loaded (`test_poll_appears_and_tallies_update_live`); and a
live reaction pill arrived highlighted as "you reacted" for every viewer
(`test_live_reaction_highlight_is_per_viewer`).

### Not covered (deliberately)

- **Real incident paging** (Uptime Kuma DOWN → page the on-call → Acknowledge
  clears it on every device): needs the webhook key and would page the seeded
  on-call people and post into `#devops-uptime`. Covered by
  `tests/test_oncall.py`; the e2e test only rings a test page.
- **Channel create/join/leave, private channels, member management**: each
  run would leave new channels in a shared dev DB.
- **Presence dots, Unreads/Threads views, quote-reply, forward, profile
  settings**: lower value than the realtime paths above (the heartbeat that
  drives presence is covered by `test_presence.py`).
- **Mobile API / FCM push**: not a browser flow; covered by the unit suite.

---

## One-time setup

Install Playwright's browser binary (Chromium is the default — Firefox and
WebKit are also available):

```bash
bin/python -m playwright install chromium
```

If you want to run against the local k3s cluster's mkcert cert without
installing the CA into the test browser profile, the conftest already sets
`ignore_https_errors=True`. That's safe for local; tests against a real test
cluster behind a real cert don't need it.

---

## Running

The tests are gated behind a marker (`e2e`) and excluded from the default
`pytest` invocation. Opt in explicitly — pass `-o addopts=` to override the
default exclusion in `pytest.ini`:

```bash
pytest -o addopts= -m e2e tests/e2e/                   # all e2e tests
pytest -o addopts= -m e2e tests/e2e/test_messaging.py  # one file
pytest -o addopts= -m e2e -k "code_block"              # match on test name
pytest -o addopts= -m e2e --headed                     # watch the browser
pytest -o addopts= -m e2e --slowmo 500                 # 500ms between actions
```

If you don't want to repeat `-o addopts=` every time, alias it:

```bash
alias e2e='bin/python -m pytest -o addopts= -m e2e tests/e2e/'
```

### Required environment variables

| Var | Default | Notes |
|---|---|---|
| `E2E_BASE_URL` | `https://d8-chat.local` | Root URL of the app under test. |
| `E2E_ADMIN_USERNAME` | `admin` | Existing admin account. |
| `E2E_ADMIN_PASSWORD` | _(none — required)_ | Whatever you set `INITIAL_ADMIN_PASSWORD` to during `init_db.py`, or what that script printed/wrote to `instance/admin_credentials.txt`. |

### Local against `auto`

```bash
auto start                                   # bring the cluster up if it isn't
export E2E_ADMIN_PASSWORD="$(awk '/^password:/ {print $2}' instance/admin_credentials.txt)"
bin/python -m pytest -o addopts= -m e2e tests/e2e/
```

On a DB seeded with `auto seed`, the admin password is `d8_admin`.

### Logins are rationed (rate limit)

`POST /login` is rate limited to **5/min and 50/hour per client IP** — and
every browser on your machine shares that IP through the ingress. So the
suite logs in through the form only **5 times per run**: the admin and two
throwaway users (`e2e_alice_<hex>`, `e2e_bob_<hex>`, created once per session
through the admin endpoint) each log in once, and every later browser context
reuses those cookies (`storage_state`); the other two are `test_auth.py`
exercising the form itself. If a run trips the per-minute window (two runs
back to back, or you logging in by hand at the same moment), the login helper
waits out the server's `Retry-After` once. If the *hourly* budget is spent it
fails fast with a message saying so — wait for the window to reset.

### CI (Jenkins → k3s test environment)

The CI pipeline spins up a fresh k3s test environment and runs the migrations
+ init_db. It should set:

```
E2E_BASE_URL=https://d8-chat.<test-cluster-host>
E2E_ADMIN_USERNAME=admin
E2E_ADMIN_PASSWORD=<value used as INITIAL_ADMIN_PASSWORD during init>
```

…and then invoke `pytest -m e2e tests/e2e/`.

---

## Authoring conventions

- Use the helpers in `helpers.py` (`open_general_channel`, `send_message`,
  `attach_file`, `wait_for_message_with_text`) instead of duplicating
  selectors. Selectors are the brittle part — keep them in one place.
- Prefer `expect(...).to_be_visible()` over manual sleeps. Playwright will
  auto-wait up to the default timeout.
- Tests must be safe to run against a database that already has data —
  use random suffixes (`secrets.token_hex(4)`) for usernames / channel
  names so two runs in a row both succeed.
- Don't assume you're starting on a particular channel; navigate
  explicitly via `open_general_channel(page)` or `page.goto(...)`.
- Keep tests independent — every test that needs a logged-in session
  should request the `logged_in_page` fixture (admin) or `chat_as(user_a)` /
  `chat_as(user_b)` rather than relying on a prior test's state. Never log in
  through the form outside `test_auth.py` (see the rate limit above).
- Message text must be unique per run: use `unique_text(...)`.
- Realtime tests must synchronise on the app, never sleep. Pub/sub is
  at-most-once, so a broadcast that races a subscribe is simply lost:
  - `chat_as(...)` returns only once the page's socket is registered
    server-side (it waits for the user's own `presence_update` frame).
  - after opening a conversation in two browsers, call
    `typing_barrier(typer, watcher, typer_username)`: the watcher seeing
    "<typer> is typing" proves both sockets are subscribed.
  - to check unread state, move the observer to another conversation first
    and barrier there (see `bob_away_from_general` in `test_realtime.py`) —
    a socket still subscribed to a channel gets no badges for it.

---

## Fixtures

`fixtures/` ships tiny but valid binary samples used by the upload tests:

- `tiny.png` — 1×1 transparent PNG (69 bytes), real magic bytes so
  the server-side libmagic content-sniffer accepts it as `image/png`.
- `tiny.pdf` — minimal valid PDF (303 bytes), `application/pdf`.
- `tiny.txt` — a one-line text file.

These all pass the upload validator (`app/services/upload_validation.py`).
Don't replace them with files that lie about their type — those would be
correctly *rejected* and the tests would fail.
