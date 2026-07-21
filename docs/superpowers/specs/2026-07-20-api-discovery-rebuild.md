# Spec: AffiliateFinder discovery rebuild on the official Creator Marketplace API

**Date:** 2026-07-20 (rev 3 — same day: architecture reworked to a **dedicated
minimal-scope TikTok app with auth inside AffiliateFinder**; rev 2's sheet-transport
export and rev 1's hosted bridge both dropped) · **Author:** J + Claude ·
**Status:** DRAFT — for J's review
**Repo:** `Serigamateam123/AffiliateFinder` · **Reference code:** `serigama-command-center`
(`lib/ingestion/tiktok/{signer,oauth,client}.ts` — patterns to mirror, not a runtime
dependency)

---

## 1. Background and problem

AffiliateFinder supports Nurin's affiliate outreach (currently ~100 DMs/week → ~10% →
~50 affiliates/month; target 100/month). Its current harvest module scrapes the
**unfiltered default list** of TikTok's Find Creators page (~120 rows of whatever the
recommendation sort serves), then filters after the fact — a worse pool than Nurin gets
filtering manually in the Affiliate Center UI. The scraper is also fragile (positional
DOM parsing, silent fail-open defaults — see `CODE-REVIEW-2026-07-20.md`) and cannot see
exact GMV for the ~40% of creators TikTok displays as "RM10K+".

A live probe on 2026-07-20 (via the command center's `tiktok_affiliate` app) confirmed
the official creator-search API covers her criteria and **returns exact GMV even where
the UI hides it**. Discovery is rebuilt on this API — called directly from
AffiliateFinder through its **own dedicated, read-only TikTok app** — and scraping
retires.

**The tool's single job after this rebuild:** put more *qualified, not-yet-contacted*
creators in front of Nurin per hour.

## 2. Goals and non-goals

**Goals**
- G1. Discovery by Nurin's criteria applied **at the source** (followers, GMV, items
  sold, category), from a pool of thousands, not the default-sort top 120 — on demand,
  self-serve, no J in the loop.
- G2. **Dedupe against Nurin's outreach Google Sheet** — never surface a creator she has
  already contacted, and never re-surface one already in the local store.
- G3. Exact GMV on every candidate — the "uncertain / hidden-GMV" bucket disappears.
- G4. Every failure is **loud and fail-closed**: no silent empty results, no fabricated
  defaults, no unfiltered results masquerading as filtered ones.
- G5. Transferable: self-contained on one Mac, documented well enough to survive
  Nurin's exit (Sept 2026).

**Non-goals (explicitly out of scope)**
- Sending messages via the API. The messaging scopes stay OFF this app entirely (§4);
  beyond that, the API-IM route is unverified — API-sent IMs may land in the same Shop
  inbox creators ignore (the Target-Collaboration problem). The existing personal-DM
  pre-fill (`messenger.py`) stays as-is; the human stays on the Send button.
- Auto-follow-ups, pipeline tracking — Nurin's Google Sheet already owns that.
- Referral/inbound acquisition — process changes, not code.
- Hosting anything, anywhere. No servers, no schedulers, no second machine in the loop.

## 3. What stays, what goes

| Component | Fate |
|---|---|
| `scraper.py` + `browser_profile/` (harvest Chrome) | **Retire.** Delete the harvest path once discovery ships. |
| `messenger.py` + `browser_profile_dm/` (DM pre-fill) | **Keep unchanged.** Proven channel. |
| `ui.html` dashboard (classify / filter / DM buttons) | **Keep**, with changes in §8. |
| `creators.json` store | **Keep**, schema additions in §7.2. |
| Command center | **No runtime role.** Its TikTok client code is the reference implementation only. |
| Code-review Batch 1 fixes (atomic write, store lock, XSS escaping, CORS hook removal, upsert guard) | **Fold into this rebuild** — they land in the files being touched anyway. |

## 4. Architecture: dedicated minimal-scope app, auth lives in AffiliateFinder

**Decision: register a NEW TikTok Partner Center app used only by AffiliateFinder,
holding only `seller.creator_marketplace.read`. AffiliateFinder implements TikTok
signing and token management in Python and calls the API directly.**

```
Nurin's Mac — AffiliateFinder (self-contained)
  ui.html ──► ui_server.py ──► tiktok_api.py ──► TikTok creator-search API
                    │                │             (dedicated read-only app)
                    │                └─ tokens.json (this app's own token chain)
                    └─► sheet dedupe (read-only) ──► Nurin's outreach Google Sheet
```

Why this shape (first-principles, 2026-07-20 discussion):
- **Blast radius is capped by scope, not by hope.** The only credential on Nurin's Mac
  belongs to an app that can *search creators* — nothing else. Worst-case leak is a
  nuisance, revoked in one minute (deauthorize the app in Seller Center / rotate its
  secret). The existing `tiktok_affiliate` app's credentials (messages.write,
  collaboration.write, finance.info) never leave J's machine.
- **Token safety.** TikTok **rotates and consumes the refresh token on every refresh**
  (verified in the command center's `oauth.ts`, which single-flights refreshes for
  exactly this reason). One refresh chain must have exactly one owner. A dedicated app
  gives AffiliateFinder its own chain — no race with the command center, whose ingest
  (daily MER) must never be collateral damage.
- **Self-serve and on-demand.** Discovery is a button Nurin clicks, with criteria she
  edits — no dependency on J's Mac being awake, no daily batch, no transport machinery.
- **Continuity.** One self-contained app on one Mac is transferable in September; a
  pipeline spanning two Macs and a spreadsheet is not.
- **Bonus:** a separate app has its own 10,000 req/day quota — discovery never contends
  with command-center API usage.

**Prerequisite ceremony (J-owned, one-time — O5):** create the app in Partner Center;
request only `seller.creator_marketplace.read`; authorize it against the MY shop; run
AffiliateFinder's bootstrap (§4.1) with the auth code. Known gotchas from the
2026-07-17 ceremony: newly granted scopes propagate per-scope and not instantly
(transient 105005 right after auth ≠ failure — retry ~1 min before concluding); a token
refresh never picks up scopes added later (re-auth required); the authorize link needs
the service_id, which is not the app key.

### 4.1 Auth module (`tiktok_api.py` + `bootstrap_auth.py`)

Mirrors the command center's vetted implementation (`signer.ts`, `oauth.ts`):

- **Signing:** TikTok Shop v2 HMAC-SHA256 request signature (app secret over
  path+params+body), `x-tts-access-token` header, `shop_cipher` query param.
- **Bootstrap (one-time):** `python bootstrap_auth.py <auth_code>` exchanges the code
  at `auth.tiktok-shops.com`, prints `granted_scopes`, and **aborts without saving if
  `seller.creator_marketplace.read` is absent** (fail-closed). Fetches and stores
  `shop_cipher`.
- **Token store:** `tokens.json` in the app dir — `0600` permissions, gitignored
  (extend the existing `.gitignore` alongside the browser profiles). Holds app key,
  app secret, access/refresh tokens + expiries, shop_cipher.
- **Refresh discipline (the two non-negotiables, straight from `oauth.ts`):**
  1. **Single-flight:** one refresh at a time (a process-wide lock — Flask runs
     threaded). Concurrent callers await the in-flight refresh; they never fire their
     own with the same soon-to-be-consumed token.
  2. **Atomic persistence before use:** the rotated tokens are written via
     temp-file + `os.replace` *before* the new access token is used. If the write
     fails, that is a **loud, blocking error** telling the user to re-run bootstrap —
     the old refresh token is already consumed, and pretending otherwise strands the
     app un-recoverably later.
- Refresh proactively when the access token is within 5 minutes of expiry; on 401-class
  envelope errors, refresh once and retry once — never loop.

Alternatives considered (kept for the record):
- **Rev 2 — scheduled export on J's Mac, Google Sheet as transport.** Zero credentials
  on Nurin's Mac, but five moving parts across two machines, a daily batch instead of
  on-demand, and J permanently in the loop. Dropped: the dedicated app shrinks the
  credential risk enough that self-contained wins.
- **Rev 1 — hosted bridge endpoint.** Dead: the command center is not deployed and
  never will be.
- **Hetzner Singapore service** — the durable home if this tool ever grows multi-user;
  an upgrade path, not v1.
- **Rejected outright: sharing the `tiktok_affiliate` app's credentials.** Refresh
  rotation makes two owners of one chain a guaranteed eventual outage (taking the
  command center's ingest down with it), and its write scopes on an intern's laptop are
  an unacceptable blast radius.

## 5. API contract — verified facts (live probe, 2026-07-20)

Endpoint: `POST /affiliate_seller/202508/marketplace_creators/search`
(scope `seller.creator_marketplace.read`, quota **10,000 req/day per app**, `page_size`
must be **12 or 20**, cursor = `page_token` + response `search_key` echoed back on later
pages, data window = last 30 days).

**Server-side filters (all verified):**

| Body field | Type | Values |
|---|---|---|
| `gmv_ranges` | []enum | `GMV_RANGE_0_100`, `GMV_RANGE_100_1000`, `GMV_RANGE_1000_10000`, `GMV_RANGE_10000_AND_ABOVE` |
| `units_sold_ranges` | []enum | `UNITS_SOLD_RANGE_0_10`, `UNITS_SOLD_RANGE_10_100`, `UNITS_SOLD_RANGE_100_1000`, `UNITS_SOLD_RANGE_1000_AND_ABOVE` |
| `follower_demographics.age_ranges` | []enum | `AGE_RANGE_18_24` … `AGE_RANGE_55_AND_ABOVE` (audience age, **not** creator follower count) |
| `follower_demographics` gender | object | audience gender split |
| `category` | []object | category IDs from the Get Categories API |
| `keyword` | string | matches username/nickname |
| `advanced_filters` | object | MY: `creator_level` `Lv. 0`–`Lv. 8` only (enumerate via `POST /affiliate_seller/202601/marketplace_creators/search/filter`) |

**No server-side follower-count filter exists.** Follower count is applied client-side
(the response includes exact `follower_count`).

**Response per creator (verified fields):** `creator_open_id`, `username`, `nickname`,
`avatar.url`, `follower_count`, `gmv.amount` (exact, even when UI shows "RM10K+"),
`gmv_range` (the display bucket), `live_gmv`, `video_gmv`, `avg_ec_video_view_count`,
`avg_ec_live_uv`, `category_ids`, `top_follower_demographics`, `selection_region`.
Per-creator detail: `GET /affiliate_seller/202508/marketplace_creators/{creator_user_id}`.

**⚠ Gotcha 1 — silently ignored fields.** Unknown body fields are accepted without
error (verified: `follower_count_ranges` at any nesting is ignored). Only known enum
fields validate (error 36009004 lists allowed values). **Therefore the client MUST
conformance-check every page**: assert returned rows actually satisfy the requested
buckets (§9-F2). Never trust that a filter took effect.

**⚠ Gotcha 2 — bucket metric ≠ returned metric.** `gmv_ranges` filters on TikTok's
bucket metric; returned `gmv.amount` is last-30-day GMV — they disagree slightly at
boundaries (verified: `GMV_RANGE_0_100` returned a row with amount 131). Conformance
checks compare against the **bucket** (`gmv_range`); exact-threshold filtering uses
`gmv.amount` client-side. Currency labels in responses are unreliable (`USD` label on
RM-bucketed values) — treat amounts as RM (`selection_region` MY) and record this
assumption in the code.

## 6. Discovery flow (on-demand, in AffiliateFinder)

Triggered by the **Discover** button (§8), which shows a criteria form pre-filled from
`config.json`: `min_followers`, `max_followers` (optional), `min_gmv` (RM),
`min_units_sold`, `category_ids`, plus `want` (candidates to fetch: 20/60/100). Nurin
edits and saves — defaults seeded from her current thresholds (O2). Missing or
non-numeric criteria → blocking validation error, never silent defaults.

1. **Build the exclusion set** — normalized handles from the outreach sheet (§7.1)
   ∪ handles + `creator_open_id`s already in `creators.json`.
   Normalization: lowercase, strip leading `@`, trim. Sheet unreachable → **abort**
   (§9-F3).
2. **Map exact thresholds to the superset of enum buckets** (e.g. `min_gmv: 1000` →
   `[GMV_RANGE_1000_10000, GMV_RANGE_10000_AND_ABOVE]`).
3. **Page** (`page_size: 20`, `search_key` cursor); per page: conformance-check
   (§9-F2), apply exact client-side cuts (`follower_count`, `gmv.amount`), drop
   excluded creators. Continue until `want` new qualified creators are collected or a
   **hard page budget** (default 50 pages ≈ 0.5% of daily quota) is exhausted.
4. **Upsert** qualified creators into `creators.json` (key `creator_open_id`, §7.2)
   and show the summary line:
   `"38 new · 14 skipped (already contacted) · 8 skipped (already in list) [22 pages]"`.

## 7. Data contracts

### 7.1 Outreach Google Sheet (hers, read-only to this app)

One agreed column holds the creator handle. Tab + column are configured once in
`config.json`; a mismatch is a **blocking error**, not an empty exclusion set (§9-F3).
Access mechanism (O3): Google service account with read-only share on the sheet
(preferred — the sheet stays private). The CSV-export-link fallback requires
link-sharing the outreach data and is accepted only if J explicitly okays that
trade-off.

### 7.2 `creators.json` row additions

`creator_open_id` (new upsert key; fallback normalized handle for legacy rows),
`gmv_exact` (RM), `video_gmv`, `live_gmv`, `avg_video_views`, `avg_live_uv`,
`source: "api"`, `discovered_at` (ISO date). Legacy scraped rows keep `gmv_is_floor`;
API rows never set it — the "uncertain" classification applies only to legacy rows and
ages out.

## 8. Dashboard changes (`ui.html` + `ui_server.py`)

- Replace **Harvest** with **Discover**: criteria form (§6) + the post-run summary line.
- Show `gmv_exact` in the GMV column; "RM10K+ (uncertain)" rendering only for legacy rows.
- **Auth status surface:** when the token store is missing, unreadable, or the refresh
  chain is broken, every discovery attempt shows the exact re-bootstrap instruction —
  never a generic "something went wrong".
- Every §9 error renders as a visible red status line with the actual reason — never an
  empty table under a green status.
- DM pre-fill flow unchanged; `creator_open_id` stored for future use.

## 9. Failure modes — all fail closed

| # | Failure | Behavior |
|---|---|---|
| F1 | TikTok API error / quota exhausted (45101004) | Discovery aborts; error shown verbatim with the retry-tomorrow hint. Nothing partial is hidden. |
| F2 | **Conformance check fails** (returned rows violate requested buckets → a filter was silently ignored) | Discovery aborts naming the filter. Non-conforming rows are never stored or shown. |
| F3 | Outreach sheet unreachable, or configured tab/column missing | Discovery **blocked** with explicit error before any API call. No "proceed anyway" in v1 — surfacing already-contacted creators is the one thing this tool must never do. Existing local list remains usable. |
| F4 | Page budget exhausted before `want` | Partial success, honestly labeled: `"found 23 of 60 — criteria may be too narrow"`. Never padded with non-qualifying rows. |
| F5 | Zero qualified rows | Explicit `"0 creators matched your criteria"` — distinguishable from every failure above. |
| F6 | Store write fails | Atomic write (temp + `os.replace`) + surfaced error; `creators.json` never truncated or silently reset (code-review findings 1/11 land here). |
| F7 | Token refresh fails, or **persisting rotated tokens fails** | Loud, blocking error with the re-bootstrap runbook step. Never retried in a loop (the old refresh token is consumed; retries make it worse). |
| F8 | Bootstrap: granted scopes lack `seller.creator_marketplace.read` | Bootstrap aborts and saves nothing, printing the granted list. (Transient 105005 within ~1 min of auth: retry per the §4 gotcha before concluding failure.) |

## 10. Compliance guardrail (P2)

The DM template is external-facing copy for a MAB-regulated product. Out of app scope to
enforce, but: J reviews and approves the current template as part of rollout, and the
README states template changes require J's sign-off (banned superlatives in any
language, no condition claims).

## 11. Open questions (block the implementation plan, not the spec review)

- ~~O1: command-center reachability~~ — resolved 2026-07-20: not deployed, never will
  be. Superseded by the dedicated-app decision (§4).
- **O2:** Nurin's exact thresholds: min followers, min GMV, min items sold, target
  categories, anything else she filters on today. (Seeds `config.json` defaults.)
- **O3:** Sheet access — confirm service-account setup, and which tab/column holds
  handles. Bonus: does the sheet distinguish declined / no-reply / converted? (Useful
  later for targeting analysis; not required for dedupe.)
- **O4:** Category IDs for her target categories — one-time lookup via Get Categories
  during build.
- **O5:** The Partner Center ceremony (J-owned prerequisite): create the dedicated app,
  request `seller.creator_marketplace.read` only, authorize the MY shop. Confirm
  whether the scope grant needs TikTok review lead time (the 2026-07-17 grant on the
  affiliate app suggests the path is quick, but that app was pre-existing).

## 12. Milestones & acceptance

**M1 — Auth + API client (`tiktok_api.py`, `bootstrap_auth.py`):** signing, bootstrap,
token store, single-flight refresh, atomic rotation persistence, search + pagination +
conformance checks. Accept: bootstrap with a scope-less auth code aborts saving nothing
(F8); a live search with test criteria returns ≥20 rows all satisfying the buckets; a
deliberately unknown filter field in a test triggers the F2 abort; killing the process
mid-refresh leaves `tokens.json` either old-and-consumed-with-loud-error or
new-and-valid — never corrupt.

**M2 — Discovery integration:** Discover flow + sheet dedupe + store/schema/UI changes
+ Batch-1 safety fixes + scraper removal. Accept: a discovery run against the live
sheet shows the dedupe summary; a creator on the Outreach tab never appears; killing
the network mid-run leaves `creators.json` intact; F3/F4/F5 render as specified.

**M3 — Handover:** README rewritten (setup, the one-time auth ceremony, config,
runbook: re-bootstrap steps, credential revocation steps, failure-message glossary
mapping every error string to what-to-do), walkthrough with Nurin + Liza. Accept: Liza
runs a discovery + DM cycle without help — the continuity test for September.

Delivery as PRs to `Serigamateam123/AffiliateFinder`, reviewed by J. (No command-center
changes required.)
