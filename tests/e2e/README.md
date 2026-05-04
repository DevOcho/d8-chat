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
| `test_admin.py` | Admin creates a user; admin dashboard loads (verifies vendored Chart.js works) |
| `test_security_headers.py` | `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Content-Security-Policy` (with no third-party origins), HSTS over HTTPS, tighter CSP on `/api/v1/*` |

The audit specifically flagged image/code-block/attachment paths as
historically brittle, so those get extra coverage.

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
bin/python -m pytest -m e2e tests/e2e/
```

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
  should request the `logged_in_page` fixture rather than relying on a
  prior test's state.

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
