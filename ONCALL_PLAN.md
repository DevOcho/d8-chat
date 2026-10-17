# On-Call Paging Plan

Status (2026-10-10): **Built, all four phases (backend).** Web pager is live; the
mobile *app* still has to implement Phase 4's client side against the API
documented in `MOBILE_WEBSOCKET_API_DOCS.md` §6. Not yet deployed to prod.

### Shipped: where things live

| Piece | Location |
|---|---|
| Models | `app/models.py`: `UserGroup`, `OnCallSchedule`, `OnCallScheduleMember`, `OnCallOverride`, `EscalationPolicy`, `EscalationLevel`, `Incident`, `IncidentPage`, `IncidentEvent` |
| Migrations | `0006_uptime_bot.py` (bot + channel), `0007_oncall.py` (tables + default rotation/policy) |
| Core logic | `app/services/oncall_service.py` (rotation math, escalation, ticker, Uptime Kuma handling) |
| Groups / SSO sync | `app/groups.py`, `app/sso.py` (`groups_claim`) |
| Webhook + mobile API | `app/blueprints/api_v1.py` (`/integrations/uptime-kuma`, `/oncall/*`) |
| Web endpoints | `app/blueprints/oncall.py` (ack, resolve, active pages, test page) |
| Admin UI | `app/blueprints/admin_oncall.py`, `templates/admin/oncall*.html` |
| Web pager | `static/js/pager.js`, overlay in `chat.html`, card `partials/incident_card.html` |
| Push | `push_service.send_page` / `send_page_cleared` |
| Tests | `tests/test_oncall.py` |

### Production setup checklist
1. Set `UPTIME_KUMA_WEBHOOK_KEY` (a random 32+ char secret) in the prod env, and
   `UPTIME_KUMA_CHANNEL` if it shouldn't be `devops-uptime`.
2. Run `auto migrate d8-chat` / `./smalls.py migrate` (0006 + 0007).
3. In Authentik, create groups **`devops-lead`** (Robert), **`director`**
   (Emilio), and **`ceo`** (Kenny). The default `profile` scope mapping already
   sends `groups` in the ID token. Group membership syncs **at each SSO login**,
   so each person must log out and back in once after being added.
4. Admin → On-Call → DevOps Primary: add the rotation members in order.
5. Everyone in the chain: profile → Notifications → **Test my pager**.
6. In Uptime Kuma, add a Webhook notification (URL, `application/json`,
   Additional Headers `{"Authorization": "Bearer <key>"}`) and apply it to the
   monitors. Keep Telegram on until the mobile app ships Phase 4.

## Goal

Uptime Kuma posts monitor events into **#devops-uptime**. A **DOWN** event opens an
incident and pages the on-call engineer with an aggressive, looping alarm. If nobody
acknowledges within 10 minutes it escalates: on-call engineer → team lead → director →
CEO. Anyone in the chain can acknowledge to stop the paging. An **UP** event for the
same monitor auto-resolves the incident.

Uptime Kuma keeps sending to Telegram in parallel until mobile paging (Phase 4) ships.

## Settled decisions

1. **Only Uptime Kuma DOWN events page.** Human messages in #devops-uptime never page,
   including `@channel` / `@here` (the app supports those two; there is no `@all`).
   Those keep their existing mention behavior (sound + desktop notification + badge).
   PENDING and MAINTENANCE heartbeats never page.
2. **Escalation is cumulative.** Every level that has been paged keeps ringing until
   someone acknowledges. Level 1 is expected to engage; reaching level 2+ means
   something is wrong.
3. **After the last level (CEO), keep ringing everyone paged so far** until someone
   acknowledges. Prod is down; nobody gets dropped.
4. **Weekly rotation**, handing off **Monday 08:00 Atlantic** (`America/Santo_Domingo`,
   UTC-4, no DST). Business hours and after hours page the same way. Vacations are
   handled with **overrides** (see "Rotation and vacations").
   **If nobody is on call**, the page starts at the **director**, then escalates to
   the CEO.
5. **Anyone in the escalation chain can acknowledge.** The chain is every rotation
   member plus every user named on a level. Acknowledging stops paging for everyone.
   Resolution: automatic on the monitor's UP event, or manual via a Resolve button.
6. **Admins manage swaps and time off** (no self-service overrides).
7. **No re-paging** of an incident that's acknowledged but never resolved (for now).
8. **Levels 2-4 target groups**, not hard-coded users: `devops-lead` (Robert),
   `director` (Emilio), `ceo` (Kenny). Groups come from the Authentik `groups`
   claim at SSO login; admins can also set local groups in the user editor.
   Workspace admins can always acknowledge, so an incident can't get stuck.

## Existing building blocks to reuse

| Need | Existing pattern |
|---|---|
| Inbound webhook as a bot user | `POST /api/v1/internal/notify` + `helpdesk-bot` (`app/blueprints/api_v1.py:1564`, migration `0003_helpdesk_channel.py`, `init_db.py:170`) |
| Push a frame to one user, cluster-wide | `chat_manager.send_to_user()` → Redis `user:{id}` |
| Client handling of JSON WS frames | `htmx:wsAfterMessage` handler in `app/static/js/chat.js:2114` (`sound`, `notification`, `presence_update`, …) |
| Interactive widget inside a message, live-updated | Polls: `Poll.message` FK, `message.html:116`, `message_poll_oob_update.html` |
| Audio allowed to play later | First-click audio priming in `chat.js:1457` |
| Backfill after reconnect (pub/sub is at-most-once) | `GET /chat/conversations/<id>/messages/since/<last_id>` (`chat.js:1630`) |
| Background thread per worker | Listener thread start in `app/__init__.py:842` |
| Mobile push | `push_service.send_to_user()` (FCM, iOS via APNs bridge) |
| Admin pages | `admin_bp` + `@admin_required`, nav in `app/templates/admin/layout.html` |
| Audit trail | `app/audit.py` → `AuditLog` |

Runtime constraint: prod runs **gunicorn with 4 gevent workers** (`Dockerfile.prod`),
possibly multiple replicas. Anything timer-based must be safe when every worker runs it.

---

## Phase 1: Uptime Kuma → #devops-uptime (no paging)

Ships on its own; useful immediately next to Telegram.

**Endpoint:** `POST /api/v1/integrations/uptime-kuma`
- Auth: `Authorization: Bearer <UPTIME_KUMA_WEBHOOK_KEY>` via Uptime Kuma's "Additional
  Headers", compared with `hmac.compare_digest`. Unset key → always 401 (same as helpdesk).
- Rate limit `120 per minute` by remote address. CSRF already exempt (api_v1 blueprint).
- Body is Uptime Kuma's native JSON webhook (Request Body = `application/json`):

  ```json
  {
    "heartbeat": {"monitorID": 12, "status": 0, "msg": "timeout of 48000ms exceeded",
                  "time": "2026-10-10 14:31:02.123", "important": true},
    "monitor":   {"id": 12, "name": "Acme site", "url": "https://acme.example", "type": "http"},
    "msg": "[Acme site] [🔴 Down] timeout of 48000ms exceeded"
  }
  ```

  `heartbeat.status`: 0 = DOWN, 1 = UP, 2 = PENDING, 3 = MAINTENANCE. The "Test"
  button in Uptime Kuma sends `heartbeat` and `monitor` as `null` with only `msg`.
  Post it as an info message so the integration can be verified end to end.
- Posts through `chat_service.handle_new_message()` + broadcast +
  `send_notifications_for_new_message()`, exactly like `internal_notify`.

**Message format** (markdown):
- `🔴 **DOWN**: Acme site (https://acme.example): timeout of 48000ms exceeded`
- `🟢 **UP**: Acme site`
- `🟡 **PENDING**` / `🔧 **MAINTENANCE**` posted as plain info

**Provisioning:** migration `0006_uptime_bot.py` + `init_db.py`:
- `uptime-bot` user (no password, workspace member)
- `#devops-uptime` channel + its `Conversation`; bot is a member
- Channel name configurable via `UPTIME_KUMA_CHANNEL` (default `devops-uptime`)

**Config:** `UPTIME_KUMA_WEBHOOK_KEY`, `UPTIME_KUMA_CHANNEL` in `config.py`,
`example.env`, and `.auto/deployment.yaml` (dev value); a real secret in prod.

**Uptime Kuma setup:** Settings → Notifications → Setup Notification → Type
**Webhook** → Post URL `https://<chat-host>/api/v1/integrations/uptime-kuma` →
Request Body `application/json` → Additional Headers
`{"Authorization": "Bearer <key>"}` → apply to the monitors.

**Tests:** auth (missing/wrong key), each status, test-notification payload, malformed
body, missing channel/bot.

---

## Phase 2: Schedules and escalation policy (admin)

### Models (migration `0007_oncall.py`)

| Model | Fields |
|---|---|
| `OnCallSchedule` | `name`, `timezone` (e.g. `America/Chicago`), `handoff_weekday` (0=Mon), `handoff_time` (e.g. 09:00), `rotation_start` (a handoff instant the rotation is anchored to) |
| `OnCallScheduleMember` | `schedule`, `user`, `position` |
| `OnCallOverride` | `schedule`, `start_at`, `end_at`, `user` (**nullable** = time off with no cover), `reason`, `created_by` |
| `EscalationPolicy` | `name`, `channel` (#devops-uptime), `no_oncall_start_level` (default: the director's level) |
| `EscalationLevel` | `policy`, `level` (1..n), `delay_minutes` (after the previous level fired; L1 = 0), `target_schedule` (nullable FK), `target_user` (nullable FK); exactly one target set |

Default policy for #devops-uptime:

| Level | Target | Fires at |
|---|---|---|
| 1 | Schedule "DevOps primary" (current on-call) | +0 min |
| 2 | Team lead (user) | +10 min |
| 3 | Director (user) | +20 min |
| 4 | CEO (user) | +30 min |

New models go in `init_db.py` `ALL_MODELS` and the `tests/conftest.py` table list;
bump `smalls_version` in `config.ini`.

### Who is on call at time *t*

```
override = most recently created override with start_at <= t < end_at
if override exists: return override.user          # may be None (time off, no cover)
members = schedule members ordered by position (active users only)
if not members: return None
weeks = number of handoff boundaries between rotation_start and t   # computed in the schedule's timezone, so DST is handled
return members[weeks % len(members)]
```

Editing the roster re-anchors `rotation_start` to the current week's handoff and rotates
the list so **the person on call this week doesn't change mid-week**. The admin UI shows
a preview of the next weeks before saving.

### Rotation and vacations

The rotation never shifts. Overrides patch specific windows on top of it.

Example: rotation Alice → Bob → Carol (Carol is the team lead), Monday 09:00 handoff.

| Week of | Rotation says | Bob is on vacation Nov 9–16: options |
|---|---|---|
| Nov 2 | Alice | |
| Nov 9 | Bob | **A. Swap:** Carol takes Nov 9 and Bob takes Nov 16. Two overrides; everyone still does equal turns. **B. Cover:** Alice covers Nov 9. One override; Alice does an extra week. **C. No cover:** override with nobody, so that week pages the director first, then the CEO. |
| Nov 16 | Carol | (swap: Bob) |
| Nov 23 | Alice | |

UI helpers: **"Swap weeks with…"** (creates both overrides) and **"Time off"** (pick a
cover or leave nobody). Partial-week overrides work too (e.g. one evening).

### Admin UI (`/admin/oncall`)
- Schedule: roster order (drag or up/down), timezone, handoff day/time, overrides list,
  **"On call now"** and a **next 8 weeks** preview with overrides applied.
- Escalation policy: levels, targets, delays, no-on-call start level.
- New "On-Call" entry in `admin/layout.html`. All changes go through `audit()`.

Small extra: an "On call" badge next to the current on-call engineer's name in the
sidebar/profile.

---

## Phase 3: Incidents, escalation engine, web pager

### Models (migration `0008_incidents.py`)

| Model | Fields |
|---|---|
| `Incident` | `policy`, `dedupe_key` (`uptime-kuma:<monitor_id>`), `title`, `source_url`, `state` (`triggered` / `acknowledged` / `resolved`), `current_level`, `next_escalation_at`, `last_page_sent_at`, `acknowledged_by`, `acknowledged_at`, `resolved_by` (null = auto), `resolved_at`, `message` (FK to the DOWN message) |
| `IncidentPage` | `incident`, `user`, `level`, `paged_at`: who is being rung. Drives "my active pages". |
| `IncidentEvent` | `incident`, `kind` (`triggered`, `paged`, `escalated`, `acknowledged`, `resolved`, `repeat_down`), `user`, `details`: the timeline |

### Webhook behavior (extends Phase 1)
- **DOWN:** if an open incident (triggered or acknowledged) has the same `dedupe_key`,
  record `repeat_down` and don't page again (covers Uptime Kuma's "resend every N
  checks"). Otherwise create the incident, attach it to the DOWN message, and fire level 1.
- **UP:** resolve the open incident for that monitor, clear everyone's alarm, update the
  card, and post `🟢 UP: Acme site (down 6m, acknowledged by Bob)`.
- **PENDING / MAINTENANCE:** message only.

### Escalation rules

```
fire_level(incident, n):
    targets = users resolved for level n            # schedule → on-call now; user → that user if active
    if level n targets a schedule and targets is empty:
        jump to policy.no_oncall_start_level         # nobody on call → director, then CEO
    new = targets − users already paged on this incident
    if new is empty and n is not the last level:
        fire_level(incident, n + 1) right away      # e.g. team lead is on call this week, so L2 adds nobody
    create IncidentPage rows for `new`, send them the page
    next_escalation_at = now + level[n+1].delay_minutes, or null at the last level
```

Everyone with an `IncidentPage` row keeps ringing until the incident leaves `triggered`.

When the team lead is the on-call engineer, level 2 adds nobody, so the director is
paged at +10 minutes instead of +20. Change this if the team lead should still get the
second 10 minutes.

### Engine (multi-worker safe)
- An `oncall_ticker` thread per worker, started beside the listener in `create_app()`,
  ticks every ~15s inside an app context (same DB cleanup as `background.py`).
- Escalation is a compare-and-set, so exactly one worker wins each step:
  `UPDATE incident SET current_level = n+1 … WHERE id = ? AND state = 'triggered'
   AND current_level = n AND next_escalation_at <= now`. Only the winner calls `fire_level`.
- **Re-send:** about every 60s (also compare-and-set on `last_page_sent_at`) re-send the
  page frame to everyone paged. Pub/sub can drop frames; this makes delivery self-healing.
- State lives in the DB, so a worker restart or deploy loses nothing.
- Core logic is a pure function `run_escalations(now)` so tests can pass in a time.

### Acknowledge / resolve
- `POST /oncall/incidents/<id>/ack`: allowed for anyone in the chain (all rotation
  members + level users). `triggered → acknowledged`, sends `page_cleared` to everyone
  paged, updates the card in the channel through an out-of-band swap, posts
  `✅ Acknowledged by Bob`.
- `POST /oncall/incidents/<id>/resolve`: same permission; manual resolve.
- `GET /oncall/pages/active`: the caller's incidents still `triggered`. Called on page
  load and after every WebSocket reconnect so a missed frame can't silence a page.

### Web pager (client)
- New JSON frames: `{"type": "page", "incident": {...}}` and
  `{"type": "page_cleared", "incident_id": N}`, handled next to `sound` and
  `notification` in `chat.js`.
- **PagerManager:**
  - Full-screen red modal listing the active incidents, each with **Acknowledge** and
    a link to the message.
  - A loud two-tone siren built with the Web Audio API that loops until cleared. No
    audio file to license, and it ignores the user's notification-sound preference.
  - Flashing tab title and favicon.
  - Desktop `Notification` with `requireInteraction: true`.
- Pages ignore presence (busy/away) and `exclude_channel`. Viewing #devops-uptime
  doesn't silence a page.
- Incident card in the DOWN message (rendered like polls in `message.html`): state
  pill, who's been paged, Acknowledge/Resolve buttons, live-updated.
- **"Test my pager"** in the user menu (and "Send test page to…" for admins): sends a
  `page` frame with `test: true` to verify the siren and notifications on that machine.
  No incident row is created.
- A small banner when audio isn't unlocked yet ("Click anywhere to enable on-call
  alerts") for on-call users.

### Known web limitation
A web page can only ring someone whose d8-chat tab is open with the computer awake.
Escalation covers that, but the phone is the real pager, so Telegram stays on until
Phase 4.

### Tests
Schedule resolution (rotation, overrides, no cover, DST week, roster re-anchor),
`fire_level` (cumulative, skip duplicate level, no-on-call jump), ticker compare-and-set
(two simulated workers, one escalation), dedupe of repeat DOWNs, UP auto-resolve,
acknowledgement permissions, and active pages after reconnect.

---

## Phase 4: Mobile

- API (Bearer token): `GET /api/v1/oncall/incidents/active`,
  `POST /api/v1/oncall/incidents/<id>/ack`, `POST /api/v1/oncall/incidents/<id>/resolve`,
  `GET /api/v1/oncall/schedule` (who's on call, my upcoming weeks).
- WebSocket: `page` / `page_cleared` events in `api_data`.
- FCM: pages go out as **high priority** with incident data and **skip** the
  `PUSH_ACTIVE_MAX_AGE` "active on web" check. Re-send on the same ~60s cadence
  until acknowledged.
- Android: a dedicated high-importance notification channel with an alarm sound; consider a
  full-screen intent.
- iOS: playing through silent / Do Not Disturb needs Apple's **Critical Alerts**
  entitlement. Apple reviews it manually and it takes weeks, so **apply early**.
- Document in `MOBILE_WEBSOCKET_API_DOCS.md`. Then turn off Telegram.

---

## Open questions
None blocking. Possible follow-ups: self-service swaps, re-paging stale
acknowledged incidents, a Telegram/SMS fallback channel, and a pager on admin pages
(today it only rings on the chat page).
