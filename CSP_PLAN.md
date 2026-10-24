# CSP_PLAN.md — dropping `'unsafe-inline'` (and `'unsafe-eval'`)

Status (2026-10-24): **Phases 0–6 done**: the full target policy (no
`'unsafe-inline'`, no `'unsafe-eval'`, no style attributes) is enforced on the
local cluster and report-only elsewhere until each environment's soak is clean
(see "Enforcing in a deployed environment"). One phase per
commit, each one test-gated, each one shippable on its own. Built from a full
inventory of the templates and scripts plus the graphify map in `graphify-out/`
(see "What the graph showed"; `graphify-out/` is gitignored, rebuild it with
`/graphify`).

| Phase | Commit |
|---|---|
| 0 Safety net | d5dfa64 |
| 1 Page-level scripts | fa7c13e |
| 2 Chat partials | 086a4ae |
| 3 Admin pages | 5680b05 |
| 4 htmx hardening + enforce scripts | e55db1e |
| 5 `<style>` elements | 8e68c69 |
| 6 `style` attributes | fd160ef |
| 7 Extras (frame/worker/manifest) | (this commit) |

---

## Goal

Today (`app/__init__.py` `_build_csp`):

```
script-src 'self' 'unsafe-inline' 'unsafe-eval'
style-src  'self' 'unsafe-inline'
```

`'unsafe-inline'` in `script-src` means the CSP does nothing against XSS: any
injected `<script>` or `onerror=` runs. (The stored XSS fixed in f89079f would
have been blocked by a strict policy.) `'unsafe-eval'` exists only because htmx
evaluates `hx-on` attributes with `new Function()`.

End state:

```
script-src      'self'
style-src       'self'
style-src-elem  'self' 'sha256-<emoji-picker shadow style>'
style-src-attr  'none'
object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'
(+ the existing img/font/media/connect directives, + report-to)
```

No nonces: everything inline moves into files under `static/js` / `style.css`,
so the policy stays a static string (cacheable, no per-request templating, no
nonce plumbing into htmx-swapped fragments).

---

## Inventory (2026-10-24)

| What | Count | Where |
|---|---|---|
| Inline `<script>` blocks | 15 | `base.html` (2: CSRF hook, theme), `index.html` (password toggle), the 6 composer partials (`initializeEditor` dispatch), `create_poll_modal.html` (add option), `admin/dashboard_content.html` (chart), `admin/oncall_policy.html`, `admin/oncall_schedule.html`, `edit_message_form.html`, `websocket_test.html` |
| Inline event handlers | 9 | 5× `onsubmit="return confirm(…)"` (`admin/edit_channel`, `admin/oncall_schedule` ×3, `admin/oncall_policy`), `onchange="this.form.submit()"` (`admin/edit_channel`), avatar upload `onchange` (`profile_header`), sound preview `onchange` (`profile_details`), `onsubmit="return false"` (`chat.html` search) |
| `hx-on` attributes (need `'unsafe-eval'`) | 4 | copy button (`message_toolbar`), `dm_user_results`, after-save in `chat_input_edit` / `chat_input_thread_edit` |
| `<style>` blocks | 2 | `chat.html`, `admin/dashboard_content.html` |
| `style="…"` attributes | 80 | ~35 `display:none` / wysiwyg-vs-markdown toggles, 8 `cursor:pointer`, ~10 widths, 2 dynamic poll bars (`width: {{ vote_percentage }}%`), avatar sizes (`_avatar_display`), image box (`message.html`) |
| Inline styles built in JS | 2 | `chat.js` pending-message avatar, emoji suggestion span |
| Vendor `<style>` injection | 2 | htmx indicator styles (`includeIndicatorStyles`), emoji-picker-element (static `<style>` in its shadow root) |
| `hx-trigger` filters / `hx-vars` / `js:` vals / `javascript:` URLs | 0 | — |
| Inline markup with handlers/styles built in Python | 0 | — |

Not affected: Bootstrap, Chart.js and our own code set styles through the CSSOM
(`el.style.x = …`), which CSP allows. The mobile API already has its own
`default-src 'none'` policy. nginx (devops repo) sets no CSP of its own.

### What the graph showed

- `base.html` is the root of every HTML page (`chat.html`, `index.html`,
  `errors/error.html`, `admin/layout.html`), so anything moved out of it is
  covered once.
- The admin pages (`admin/layout.html` → `#admin-content` swaps) never load
  `chat.js`; they need their own small `admin.js`.
- The composer partials are swapped in by htmx from four routes
  (`get_default_chat_input`, `get_thread_chat_input`, `load_message_for_edit`,
  `load_message_for_thread_edit`); their inline init script is the reason
  swapped content must run scripts at all.
- Dead templates found along the way (nothing renders or links them):
  `login.html`, `websocket_test.html`, `partials/manage_members_modal.html`,
  `partials/invite_form.html`, and `partials/edit_message_form.html` (only
  rendered by `messages.get_edit_message_form`, which nothing calls).
  Deleting them removes 3 of the 15 inline scripts for free.

---

## Phases

Each phase ends with: `bin/pytest`, `npm test`, the e2e suite (including the
CSP fixture from Phase 0 and the axe checks), pre-commit, and one commit.

### Phase 0 — Safety net (no behavior change)

1. **Report endpoint.** `POST /csp-report`, CSRF-exempt, rate-limited (per IP),
   body capped (~16 KB), accepts `application/csp-report` and
   `application/reports+json`. Logs one structured `csp_violation` line
   (directive, blocked URI, source file:line, sample, page). Drops obvious
   noise (`chrome-extension:`, `moz-extension:`, `safari-extension:`).
2. **Report-Only header.** Send `Content-Security-Policy-Report-Only` with the
   *target* strict policy next to the current enforced one, plus `report-to` /
   `Reporting-Endpoints` and legacy `report-uri`. A `CSP_MODE` setting
   (`legacy` | `report` | `enforce`) picks what's enforced, so enforcement can be
   rolled back with an env change instead of a code change.
3. **E2E violation recorder.** An init script in every Playwright context
   collects `securitypolicyviolation` events (they fire for report-only too).
   An autouse fixture writes them to a per-run summary; it starts in
   **record** mode (a live burn-down list), and each later phase switches the
   directives it has cleaned up to **fail**.
4. **Delete the dead templates** listed above and the unused
   `get_edit_message_form` route (+ its test, if any).
5. Unit tests: header values per `CSP_MODE`, report endpoint parsing/limits.

### Phase 1 — Page-level scripts into files

1. `base.html`: CSRF hook → `static/js/csrf.js` (after htmx); theme IIFE →
   `static/js/theme.js` in `<head>` (tiny, blocking, so no theme flash).
2. `index.html`: password visibility toggle → `static/js/login.js`.
3. `chat.html`: search form `onsubmit="return false"` → `chat.js`;
   `<style>` block → a static hide rule in `style.css` plus the per-viewer
   "show on my own messages" rule, which `chat.js` adds through the CSSOM
   (it needs the viewer's id; CSSOM changes are not subject to CSP).
4. Tests: JS tests for the CSRF header on non-GET htmx requests and the theme
   bootstrap; existing e2e login/auth tests.

### Phase 2 — Chat partials without inline script or `hx-on`

1. **Composer init.** Replace the six `<script>dispatch initializeEditor</script>`
   blocks with a marker (`<form id="message-form…" data-editor="{suffix}">`);
   `chat.js` initializes forms from `htmx.onLoad` plus one pass at load, keeping
   the existing one-editor-per-form guard.
2. **Poll modal.** "Add option" → delegated `[data-poll-add-option]` handler.
3. **`hx-on` (4):**
   - copy button → `[data-copy-message]` delegated click
   - `dm_user_results` → `[data-focus-composer-after-modal]`
   - edit / thread-edit after-save → `data-after-save="default-composer"` /
     `"thread-composer:<parent_id>"`, handled in one `htmx:afterRequest` listener
4. **Profile handlers.** Avatar upload `onchange` → `[data-autosubmit]`; sound
   preview → `[data-preview-sound]`.
5. **JS-built inline styles (2)** → CSS classes.
6. Tests: one JS test per replaced behavior (editor initializes after an htmx
   swap, copy, after-save reload, add option, autosubmit, sound preview); update
   the fixture-drift hooks.

### Phase 3 — Admin pages

1. New `static/js/admin.js`, loaded by `admin/layout.html`, initializing from
   `htmx.onLoad` (content swaps into `#admin-content`).
2. `onsubmit="return confirm(…)"` (5) → `data-confirm="…"` + one delegated
   submit handler (see Decision 2). `onchange="this.form.submit()"` →
   `[data-autosubmit]`.
3. `oncall_policy` (`syncLevelTarget`) and `oncall_schedule` scripts → functions
   in `admin.js`, bound by data attributes.
4. Dashboard chart: data → `<script type="application/json" id="dashboard-chart-data">`
   (a data block, never executed, so CSP allows it); drawing code → `admin.js`;
   `<style>` block → `style.css`.
5. Tests: existing e2e dashboard-chart and admin tests; add an e2e for an
   on-call override delete (exercises `data-confirm`) and the level-target
   toggle.

### Phase 4 — Harden htmx, then enforce `script-src 'self'`

1. `<meta name="htmx-config" content='{"allowEval": false, "allowScriptTags": false, "includeIndicatorStyles": false}'>`
   in `base.html`; copy htmx's `.htmx-indicator` rules into `style.css`.
   With `allowScriptTags: false`, no swapped fragment can run a script even if
   one slips back in.
2. **Regression guard** (pytest): fail on any template `<script>` without `src`
   (except `type="application/json"`), any `on[a-z]+=` attribute, `hx-on`,
   `hx-vars`, `js:` in `hx-vals`, or `hx-trigger` filters; and on JS/Python that
   builds `style=` / `on*=` attributes into HTML strings.
3. **Enforce** `script-src 'self'` (no `'unsafe-inline'`, no `'unsafe-eval'`)
   in dev; keep `style-src 'self' 'unsafe-inline'` enforced for now (the strict
   style policy stays report-only).
4. The e2e recorder fails on any `script-src` violation from here on.
5. Rollout: dev → chat.dev (soak) → prod with `CSP_MODE=report` for the soak
   window (Decision 3), then `enforce`.

### Phase 5 — `<style>` elements strict

1. Both template `<style>` blocks are already gone (Phases 1, 3).
2. emoji-picker-element's shadow `<style>` is static per version: allow it by
   hash (`EMOJI_PICKER_STYLE_HASH`). `tests/js/emoji-picker-csp.test.js`
   loads the vendored bundle in jsdom, hashes that style text and compares,
   so a vendor upgrade fails CI instead of silently unstyling the picker.
3. Policy: `style-src 'self' 'unsafe-inline'; style-src-elem 'self' 'sha256-…'; style-src-attr 'unsafe-inline'`
   (interim). The split is required: a hash in a source list makes
   `'unsafe-inline'` ignored, which would block every `style=` attribute; so
   the `style-src` fallback (for browsers without `-elem`/`-attr`) keeps
   `'unsafe-inline'` and no hash until Phase 6.
   `style-src-elem` / `-attr` need Chrome 75+, Firefox 108+, Safari 15.4+.

### Phase 6 — `style` attributes → classes (drop `style-src-attr`)

1. `display:none` and the wysiwyg/markdown toggles → classes chosen in Jinja.
   Use a non-`!important` hide class (or toggle classes in JS): Bootstrap's
   `[hidden] { display: none !important }` beats the `el.style.display = …`
   that `chat.js` uses, so the `hidden` attribute is not a drop-in.
2. `cursor:pointer`, fixed widths/max-widths → a handful of utility classes.
3. Dynamic values: poll bars carry `data-width-percent`, applied through the
   CSSOM by `ui.js` on `htmx.onLoad`; avatars use size classes
   (`.avatar-24/32/36/40/60`, a guard test checks every size used has one);
   the single-image box needs no CSS value at all: its `<img>` carries the
   display size as `width`/`height` attributes and `.attachment-grid-sized`
   lets it shrink keeping that ratio.
4. Policy: `style-src-attr 'none'`. The e2e recorder fails on any style
   violation.

What it took beyond the templates:
- `.is-hidden` (not `!important`) for what `chat.js` toggles through
  `el.style.display`, and an `isShown()` helper for the four places it read
  `style.display` to decide what's showing.
- htmx's settle step copied the old element's `style` attribute onto its
  replacement (`attributesToSettle` included `style`); with chat.js styling
  through the CSSOM that attribute exists, so the copy was blocked. The
  `htmx-config` meta drops `style` from `attributesToSettle`.
- emoji-picker-element binds `style="${…}"` in its templates with
  `setAttribute`. The vendored bundle carries a one-line `D8-PATCH` to set
  those through `el.style.cssText` instead (CSSOM, allowed); its header says
  how to re-apply it on upgrade.

Burn-down after Phase 4 (e2e run, strict policy, report-only):
`style-src-attr` from htmx (~3100: htmx re-applies our templates' `style=`
attributes when it settles a swap; goes away with them), from `/chat` (~570,
the page's own `style=`), and from emoji-picker-element (~400: it sets
`style` attributes in its shadow DOM, so `style-src-attr 'none'` would break
it; Phase 6 must check whether those are attribute writes or CSSOM, and may
need a `style-src-attr` exception or a picker patch). `style-src-elem` from
emoji-picker (its shadow `<style>`, Phase 5's hash), 6 `inline`, and 2 from
`bootstrap.bundle.min.js` (to identify in Phase 5).

### Phase 7 — Optional extras (each its own small decision)

- Done: `frame-src 'none'`, `worker-src 'self'`, `manifest-src 'self'` (in
  every mode; nothing is framed, no workers run).
- Not done: `upgrade-insecure-requests` — blocked until the dev object store
  is served over HTTPS (dev MinIO is `http://`).
- Not done: `require-trusted-types-for 'script'` — htmx swaps via `innerHTML`
  and would need a Trusted Types policy of its own.

---

## Rollout and rollback

- Every environment runs report-only for the strict policy from Phase 0 on, so
  the burn-down is visible before anything is enforced.
- Enforcement is per directive group (scripts in Phase 4, `<style>` in 5,
  attributes in 6) and per environment via `CSP_MODE`.
- Rollback: set `CSP_MODE=report` (back to report-only) and restart the pods;
  no code change.
- Watch: the `csp_violation` log lines after each step; zero
  first-party violations for the soak window is the gate to enforce.

## Risks

| Risk | Mitigation |
|---|---|
| A swapped fragment's script silently stops running | Phase 2 removes them all; Phase 4's guard test fails CI if one returns; `allowScriptTags: false` makes it explicit |
| Report noise from browser extensions | filter extension schemes; judge by first-party source files |
| emoji-picker upgrade changes its style hash | Phase 5 hash test fails CI on upgrade |
| Old Safari (<15.4) ignores `style-src-elem/attr` | falls back to `style-src`; acceptable for our users, revisit if reports show otherwise |
| Admin pages miss shared chat.js helpers | `admin.js` is self-contained; the confirm modal moves to a shared module if Decision 2 picks it |
| Jinja-driven initial state (wysiwyg toggle) flashes | classes chosen server-side, not by JS after load |

## Decisions (2026-10-24)

1. **How far:** all the way (through Phase 6), Phases 0–4 first.
2. **Admin confirmations:** the in-app confirm modal, shared with the chat page
   (`ConfirmDialog` in `ui.js`, markup in `partials/confirm_modal.html`).
3. **Soak window:** one week of `CSP_MODE=report` per environment before
   `enforce`, so a full on-call rotation (weekly handoff, overrides, pages)
   and the less common admin flows have run at least once.
4. **Where reports go:** app logs only (`csp_violation` lines).

## Enforcing in a deployed environment

1. Deploy with `CSP_MODE=report` (the default). The strict policy is sent as
   Report-Only: browsers enforce nothing new, they only report.
2. Soak a week. Watch the app's logs for `csp_violation` lines, e.g.
   `kubectl logs deploy/d8-chat --since=24h | grep csp_violation`
   (adjust the deployment name per environment). Extension noise is already
   dropped by `/csp-report`; each line names the directive, the source
   file:line and a sample of what was blocked.
3. None for the week → set `CSP_MODE=enforce` and restart. Only `style-src*`
   ones (cosmetic) → `CSP_MODE=enforce-scripts` gets the script protection
   now, and `enforce` follows the fix. Any `script-src*` one → fix it first.
   Rollback is setting it back to `report`.

## Effort

| Phase | Size |
|---|---|
| 0 | medium (endpoint, header modes, e2e recorder) |
| 1 | small |
| 2 | medium (composer init path is the delicate part) |
| 3 | medium |
| 4 | small (+ soak time) |
| 5 | small |
| 6 | medium (80 attributes, mostly mechanical) |
| 7 | small each |
