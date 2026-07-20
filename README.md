# Affiliate Creator Finder

Pulls creators out of TikTok Affiliate Center, then filters and ranks them on
five criteria — including one Seller Center can't filter on at all.

## Run

Double-click **`Start Affiliate Finder.command`**. It opens at
http://localhost:7374.

First run only, if you'd rather do it by hand:

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/python ui_server.py
```

## Using it

1. **Get creators from TikTok** — opens a Chrome window, loads Affiliate Center →
   Find creators, scrolls until it has the number you asked for, and stores what
   it finds. The first run stops at a login page: sign into Seller Center in that
   window and it carries on. The login is remembered in `browser_profile/`, so
   later runs go straight through.
2. Fill in whichever of the five boxes you care about — blank means "don't care".
3. **Find creators.** Rank by any column; **Copy handles** puts the shortlist on
   your clipboard.

## The five criteria

| Field | Source |
|---|---|
| GMV at least (RM) | scraped |
| GMV per customer (RM) | **derived** — see below |
| Creators to reach | caps how many come back |
| Followers at least | scraped |
| Items sold at least | scraped |

The four thresholds are ANDed — a creator has to satisfy every box you fill in,
so you are always filtering on all of them at once. Blank means "don't care".
Inputs accept the formats Seller Center itself renders: `50K`, `1.2M`,
`RM45,000`, `1,200`.

### Rank by

Separate from filtering: this only orders the results. **All 4 combined** is the
default and scores each creator by percentile on GMV, GMV per customer,
followers and items sold, then averages the four — shown in the Score column,
where 100 means best on everything.

Percentiles rather than raw values, because one creator with a huge GMV would
otherwise flatten the scale for everyone else. Scored across the whole stored
pool, so a creator's score doesn't shift when you change thresholds.

This is what surfaces creators who are good all round but top of no single
column — a RM161/customer seller with a small following ranks above a
similar-GMV creator selling at RM23, which a GMV-only sort gets backwards.
The single-column options are still there when you want them.

## DMing creators

Each row has a **DM** button. It copies a draft personalised to that creator and
opens their TikTok profile in a new tab — click **Message** there and paste.

Edit the draft under **DM message**; `{nickname}`, `{category}`, `{items_sold}`,
`{followers}`, `{gmv}`, `{gmv_per_customer}`, `{level}` and `{handle}` fill in
per creator. It's saved to `settings.json`. An unrecognised token is left
visible as `{typo}` rather than silently blanked, so mistakes surface in the
preview instead of in someone's inbox. Creators with hidden GMV render as
"over RM10,000" — never a fabricated exact figure.

**Why it opens the profile rather than the DM directly.** TikTok does have a
deep link — `tiktok.com/messages?u=<id>` — but it needs the creator's TikTok
user id, and the id on the Affiliate Center row is a *different* id space. Feed
it an affiliate id and TikTok silently drops the parameter and lands on your
inbox. Resolving handle → user id means loading each profile, which trips
tiktok.com's bot check ("Please wait…"). Opening the profile is one extra click
and always works.

**Sending stays manual, deliberately.** Batch invite in Affiliate Center already
does sanctioned bulk outreach (50 at a time, plus a "Not invited in past 90
days" filter). Automating DM sends on tiktok.com would break TikTok's ToS and
risk the seller account the business runs on — for very little gain over a
button that removes the searching and the typing.

## Two things the numbers won't tell you

**"GMV per customer" is really per *item*.** TikTok publishes GMV and units sold
but never a distinct-customer count, so this is `GMV ÷ items sold` — average
price per item, not spend per buyer. They agree only when each buyer takes one
unit. It's still the most useful column here: it separates creators shifting
volume on cheap product from creators moving high-ticket, and Seller Center
can't sort on it.

**Roughly 4 in 10 creators have their GMV hidden.** TikTok shows `RM10K+`
instead of a figure. That number is a *floor* — the truth is somewhere above it,
possibly far above. Those rows are tagged `GMV HIDDEN`, display as `≥RM10,000`,
and never show a fabricated exact value.

This matters when filtering. A hidden-GMV creator that fails a GMV threshold
might genuinely pass it, so rather than dropping them the app sorts them into
**uncertain**: listed after the confirmed matches, and hideable via *Show
hidden-GMV creators*. Followers and items sold are exact, so those gate hard.

Unknown: which time window "GMV" covers. Seller Center's tooltip wouldn't render,
so it isn't documented here rather than guessed at.

## Layout

| File | Role |
|---|---|
| `ui_server.py` | Flask app — store, filtering, ranking, harvest jobs |
| `ui.html` | the dashboard |
| `scraper.py` | Playwright: drives Chrome, reads Find creators |
| `creators.json` | the store (upserted by handle, so re-harvests don't duplicate) |
| `browser_profile/` | keeps you logged into Seller Center |

Scraping goes through a real browser on purpose: every `affiliate.tiktok.com`
API call is signed with `msToken` / `X-Bogus` / `X-Gnarly`, generated by
obfuscated ByteDance JavaScript. Replaying them from Python isn't practical, so
the app reads what the page renders. It also has to use real mouse-wheel input —
the lazy-loading list ignores scripted scrolling.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/creators` | everything stored |
| POST | `/api/creators` | upsert rows |
| DELETE | `/api/creators` | wipe the store |
| POST | `/api/search` | the five knobs, plus `sort` and `include_uncertain` |
| POST | `/api/harvest` | start a scrape (`region`, `target`) |
| GET | `/api/harvest/status` | progress of the running scrape |
