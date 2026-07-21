# Affiliate Creator Finder

## 1. What this is

A local dashboard that finds TikTok creators worth inviting into Serigama's
affiliate program, checks each one against the outreach sheet so nobody gets
contacted twice, and pre-fills a DM you send yourself with one click.

**What changed from the old scraper version, and why.** The old app drove a
real Chrome window through Affiliate Center's "Find creators" page and read
whatever the page happened to render. That worked, but it was fragile: TikTok
signs every request from that page with obfuscated JavaScript
(`msToken`/`X-Bogus`/`X-Gnarly`), so the only reliable way in was a real
browser doing real scrolling — one layout change or bot-check away from
breaking. It also couldn't filter *at the source*: it fetched whatever the
page showed and filtered afterwards, and it could only ever show GMV as
"RM10K+" for the roughly 4-in-10 creators TikTok hides the exact figure for.

This version calls TikTok's **official creator-search API** directly
(signed, server-to-server, no browser needed for discovery):

- **Source filtering** — GMV and units-sold minimums are sent to TikTok as
  part of the search request, so the API only returns creators that already
  clear the bar, instead of the app fetching everything and filtering after.
- **Mostly-exact GMV** — the API returns the real number for most creators,
  including many the old scraped UI showed only as "RM10K+". For the rest
  (roughly half of live rows) TikTok still hides the exact figure; those show
  as a "≥" floor, same as before.
- **No DOM fragility** — discovery no longer depends on Affiliate Center's
  page layout or a signed-in browser session at all.

DMing still opens a real TikTok browser window and pre-fills the message —
that part hasn't changed, and **the human always clicks Send.** Sending
DMs automatically would break TikTok's terms of service and risk the account
the business runs on, for very little gain over a button that removes the
searching and the typing. See §7 for the message content itself.

---

## 2. Setup

**Who does this: J or Nurin.** One-time, per machine.

1. Clone (or copy) this folder.
2. Double-click **`Start Affiliate Finder.command`**. First run only, it
   creates a Python virtual environment and installs dependencies — this
   takes a minute or two and only happens once. It then opens the dashboard
   at `http://localhost:7374`.

   If you'd rather do that first step by hand instead of double-clicking:

   ```bash
   python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
   ./venv/bin/python ui_server.py
   ```

3. Copy `config.example.json` to `config.json`:

   ```bash
   cp config.example.json config.json
   ```

   Open `config.json` and fill in the `sheet` section — see §4 for how to
   get those values. Leave `criteria.category_ids` empty; it's reserved for
   a filter this app doesn't support yet.

4. **The `criteria` numbers in `config.example.json` are seed/placeholder
   values, not real thresholds.** Update `min_followers`, `min_gmv`, and
   `min_units_sold` to whatever Nurin's actual qualifying bar is — either by
   editing `config.json` directly, or via the **"Discovery criteria"**
   section in the dashboard once it's running (there's a *Save criteria*
   button there).

Discovery won't run at all until `config.json` exists and the TikTok
auth ceremony below has been completed once.

---

## 3. One-time TikTok auth (J does this)

**Who does this: J.** This is a one-time ceremony per TikTok app, and it
needs Partner Center + Seller Center admin access, so it isn't something to
hand to an operator. It only needs to be redone if the app's credentials are
revoked (§8) or the granted scope changes.

This is the exact ceremony from `bootstrap_auth.py`'s own docstring:

1. **Partner Center:** create the app, request **only**
   `seller.creator_marketplace.read`. Don't request more than that scope.

2. Run:

   ```bash
   ./venv/bin/python bootstrap_auth.py --print-auth-url <service_id>
   ```

   `service_id` is on the app's detail page in Partner Center — **it is NOT
   the App Key.** Using the App Key here will not work.

3. Open the printed link **as the shop's Seller Center account**, approve
   the app, and copy the `code=` value from the redirect URL. (Landing on a
   404 page there is normal — the code is in the URL regardless.) This code
   is **single-use and expires in about 30 minutes**, so do step 4 promptly.

4. Run:

   ```bash
   ./venv/bin/python bootstrap_auth.py <code> --app-key K --app-secret S --shop-cipher C
   ```

   - `K` and `S` are the app's App Key and App Secret from Partner Center.
   - `shop_cipher` (`C`) is the tricky part: affiliate-scoped apps like this
     one usually **cannot discover their own shop_cipher** (TikTok returns
     error `105005` if you try). The good news is shop_cipher is **portable
     across apps for the same shop** (verified live 2026-07-06) — copy it
     from the command center's `oauth_credentials` row and pass it here with
     `--shop-cipher`, or add it afterwards with:

     ```bash
     ./venv/bin/python bootstrap_auth.py --set-shop-cipher <cipher>
     ```

     Without a shop_cipher, every affiliate-search call fails with TikTok
     error `106013`.

**Scope gate (fail-closed).** If TikTok reports that the granted scopes
don't include `seller.creator_marketplace.read`, `bootstrap_auth.py` saves
**nothing** — those tokens would be useless anyway, and you'd need to
re-authorize from step 2 regardless (a token *refresh* never picks up a
scope added later; only a fresh authorization does).

**Scope propagation note.** Right after a fresh authorization, TikTok can
keep returning error `105005` ("scope not found") for up to roughly a
minute before the new scope actually takes effect — this is normal, not a
failure. `bootstrap_auth.py` runs a live search probe at the end of the
ceremony and retries automatically on `105005` (observed ~45 seconds in
practice, waits up to 90 seconds before giving up). If it does give up,
just wait a minute and re-run the probe by re-running step 4's command, or
`bootstrap_auth.py --set-shop-cipher <cipher>` again if the cipher was
already saved.

---

## 4. Sheet access

**Who does this: J or Nurin, with a Google account that has admin rights on
the outreach sheet.** One-time, per machine.

The outreach sheet is how this tool knows who's already been contacted — it
never keeps its own separate "do not contact" list. The tool only ever
*reads* one column from it.

The outreach file can be a **native Google Sheet or an Excel (.xlsx) file
uploaded to Drive** — the tool detects which and reads it the right way.
(Nurin's is an .xlsx.)

1. In Google Cloud Console, create a **service account** (or reuse an
   existing project's): IAM & Admin → Service Accounts → Create Service
   Account. Enable **both the Google Sheets API and the Google Drive API**
   on that project if they aren't already.
2. Create a JSON key for that service account and download it. Save it into
   this app's folder as **`service_account.json`** (the exact filename
   `config.json`'s `sheet.service_account_file` points to).
3. Open the outreach sheet in Google Sheets, click **Share**, and share it
   with the service account's email address (it looks like
   `something@project-id.iam.gserviceaccount.com` — find it in the
   downloaded JSON key or the Cloud Console) as **Viewer**. Viewer is
   enough and is intentional — this tool never writes to the sheet.
4. In `config.json`, set `sheet.sheet_id` (the long ID in the sheet's URL,
   between `/d/` and `/edit`), `sheet.outreach_tab` (the tab name), and
   `sheet.handle_column` / `sheet.handle_header` (which column holds
   contacted handles, and the exact text of its header cell).

The header cell doesn't have to be the very first row — a few banner rows
above it are fine (the tool scans the first 10 rows for the exact header).

The tool reads exactly that one column, checks that the configured header
appears in it, and **refuses to run discovery at all** if it doesn't
match or the column looks empty — see §6 for what that looks like and what
to do about it. This is deliberate: surfacing a creator who's already been
contacted is the one mistake this tool must never make, so when it's in
doubt about the sheet, it stops rather than guesses.

---

## 5. Daily use (Nurin)

**Who does this: Nurin (or whoever holds the role after her).**

1. Open the dashboard — double-click **`Start Affiliate Finder.command`** if
   it isn't already running, then go to `http://localhost:7374`.
2. Click **"🔎 Discover creators"**. This asks TikTok's API for creators
   matching the criteria in the "Discovery criteria" section (adjust and
   **Save criteria** first if needed), skips anyone already in the outreach
   sheet or already stored locally, and adds the rest to the table. The
   status line reports how many were added, how many were skipped because
   they're already contacted, and how many were already known.
3. **Review the table.** Confirmed matches are listed first; creators TikTok
   hid the GMV for are marked **GMV HIDDEN** and listed after — their real
   GMV is at least what's shown, possibly much more (toggle **"Show
   hidden-GMV creators"** to hide them instead).
4. Click **Message** on a creator you want to reach. This opens their DM in
   the app's own browser window with the draft pre-filled — **read it, then
   click Send yourself in that window.** The tool never sends on its own.
5. **Log it in the sheet.** After sending, add that creator's handle to the
   outreach sheet's configured column (§4) yourself. This is the only step
   that isn't automatic, and it matters: **anyone already in the sheet's
   handle column will never be surfaced again** by "Discover creators" — so
   if a handle isn't logged there, the tool has no way of knowing they've
   already been messaged, and may show them again on a future run.

---

## 6. Failure-message glossary

If the dashboard shows an error, find the closest match below. "Who fixes
it" tells you whether it's a config/sheet fix you can do yourself, or
something that needs J.

| What you see (distinctive part of the message) | What it means | Who fixes it |
|---|---|---|
| `config.json is missing` | The app has no `config.json` yet. | Nurin — copy `config.example.json` to `config.json` (§2). |
| `tokens.json is missing` | The TikTok auth ceremony has never been run on this machine. | J — run the §3 ceremony. |
| `token refresh rejected` | TikTok rejected the saved refresh token (it expired, or was consumed by another run). | J — re-run the §3 ceremony from step 2. |
| `PERSISTING ROTATED TOKENS FAILED` | TikTok issued a new token but this machine couldn't save it to disk (disk full, permissions, etc). The credential TikTok just issued is now the **only** valid one. | J — fix the disk problem, then re-run the §3 ceremony. |
| `expected header` (in a sheet range) | `config.json`'s `sheet.outreach_tab` / `handle_column` / `handle_header` don't point at the column you think they do. | Nurin — fix the tab/column/header values in `config.json` (§4). |
| `tab '…' not found in the outreach file` | The outreach file's tab was renamed (the error lists the tabs that exist). | Nurin — set `sheet.outreach_tab` to the current tab name (§4). |
| `Drive API HTTP …` / `Drive download HTTP …` | Google refused the file lookup or download — usually the Drive API isn't enabled on the project, or the file is no longer shared with the service account. | Nurin — re-check §4 steps 1 and 3; escalate to J if it persists. |
| `unsupported outreach file type` | The outreach file was replaced with something that isn't a Google Sheet or .xlsx. | Stop and tell J. |
| `0 contacted handles` / `refusing to run discovery without a dedupe source` (sheet) | The configured column looks empty — almost certainly the wrong column, not an actually-empty sheet. | Nurin — fix the tab/column in `config.json` (§4). |
| `TikTok error 45101004` | Daily API quota reached. | Nobody — just try again tomorrow. |
| `filter was not applied` | TikTok's search results didn't respect the GMV filter this run — the app aborts rather than show unfiltered results. This means TikTok changed how the API behaves. | Stop and tell J — this needs a code fix, not a config fix. |
| `creators.json is unreadable` / `refusing to treat it as empty` (store) | The local creator store file is corrupted. | Stop, **do not delete `creators.json`**, tell J. |
| `service account file missing` | `service_account.json` isn't in the app folder. | Nurin — see §4 step 2. |
| `Google auth failed` | The service account key is invalid, revoked, or the Sheets/Drive APIs aren't enabled for that Google Cloud project. | Nurin first (re-check §4); escalate to J if it persists. |

---

## 7. DM message policy

**The DM template is external-facing copy for a MAB-regulated product.** It
is not just internal wording — it is what a creator sees from Serigama.
Because of that:

**Any change to the DM template requires J's sign-off before it's used.**
That includes:

- No banned superlatives, in any language — Malay or English (e.g. claims
  like "the best", "no. 1", "guaranteed") are the kind of thing that gets
  flagged in this space.
- No condition or health claims of any kind — this is a health/wellness
  brand, and implying the product treats or cures anything is out of
  bounds regardless of how it's phrased.

The template lives in the dashboard's **"DM message"** section and is saved
to `settings.json`. If you want to tweak wording, draft the change, send it
to J for approval, and only then click **Save** in that section. Use
**"Preview on top creator"** to see exactly what a real message will look
like — with names, GMV, etc. — filled in before it goes live.

---

## 8. Credential revocation (runbook)

Use this if TikTok credentials for this app may have leaked, or an operator
leaves and access needs to be cut immediately.

**Who does this: J.**

1. **Cut off TikTok's side** — either:
   - Deauthorize the app for this shop in **Seller Center** (under Apps &
     Permissions), or
   - Rotate the app's secret in **Partner Center**.

   Either one invalidates the credentials this app is using immediately.

2. **Delete the local tokens** so a stale copy can't be reused:

   ```bash
   rm tokens.json
   ```

3. **Re-bootstrap when it's needed again** — run the full §3 ceremony from
   the top with fresh (or rotated) app credentials.

---

## 9. Handover note

This app's ownership is up for re-decision by **September 2026**, when
Nurin's internship ends. Whoever inherits it — Liza or otherwise — needs
nothing beyond **sections 2 through 8 above** to run it day to day:

- §2–§4 are one-time setup (mostly J, once per machine).
- §5 is the entire daily workflow (Nurin's role: discover, review, message,
  log in the sheet).
- §6–§8 cover what to do when something goes wrong, and how to shut off
  access if it ever needs to be.

If ownership changes hands, the main things the incoming operator needs
from J are: Partner Center / Seller Center access (for §3 and §8, which
stay J's responsibility regardless of who runs the daily flow), and
whoever currently holds edit rights on the outreach sheet granting the
service account's Viewer access again if it's ever re-created (§4).
