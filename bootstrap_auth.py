"""One-time auth for AffiliateFinder's dedicated TikTok app.

Ceremony (J-owned, spec §4/O5):
  1. Partner Center: create the app, request ONLY seller.creator_marketplace.read.
  2. ./venv/bin/python bootstrap_auth.py --print-auth-url <service_id>
     (service_id is on the app's detail page — it is NOT the App Key)
  3. Open the link as the shop's Seller Center account, approve, copy `code=` from
     the redirect URL (single-use, expires ~30 min).
  4. ./venv/bin/python bootstrap_auth.py <code> --app-key K --app-secret S
         [--shop-cipher C]
     shop_cipher: affiliate-scoped apps usually CANNOT discover it themselves
     (105005 on /authorization/*). The cipher is portable across apps for the
     same shop (verified live 2026-07-06) — copy it from the command center's
     oauth_credentials row and pass it here, or add it later with
     --set-shop-cipher.

Scope gate (fail-closed): if TikTok reports granted_scopes without
seller.creator_marketplace.read, NOTHING is saved — the tokens would be
useless, and a re-auth is needed after fixing scopes anyway (a token refresh
never picks up scopes added later).
"""
import argparse, sys, time

import requests

import tiktok_api as t

REQUIRED_SCOPE = "seller.creator_marketplace.read"
SELLER_AUTH_HOST = "https://services.tiktokshop.com"


def auth_url(service_id):
    return f"{SELLER_AUTH_HOST}/open/authorize?service_id={service_id}"


def exchange(auth_code, app_key, app_secret, http_get=requests.get):
    r = http_get(f"{t.AUTH_HOST}/api/v2/token/get", params={
        "app_key": app_key, "app_secret": app_secret,
        "auth_code": auth_code, "grant_type": "authorized_code"}, timeout=30)
    if r.status_code != 200:
        raise t.AuthError(f"token exchange failed: HTTP {r.status_code}")
    j = r.json()
    d = j.get("data") or {}
    if j.get("code") != 0 or not d.get("access_token") or not d.get("refresh_token"):
        raise t.AuthError(
            f"token exchange rejected (code={j.get('code')}: {j.get('message', '')}). "
            "The auth_code is single-use and short-lived — generate a fresh "
            "authorization link and retry.")
    scopes = d.get("granted_scopes")
    if scopes is not None and REQUIRED_SCOPE not in scopes:
        raise t.AuthError(
            f"granted scopes {scopes} do not include {REQUIRED_SCOPE} — NOT saving these "
            "tokens (they would be useless). Add the scope in Partner Center, then "
            "re-authorize from step 2 (a refresh never picks up new scopes).")
    now = time.time()
    return {
        "app_key": app_key, "app_secret": app_secret,
        "access_token": d["access_token"],
        "access_token_expires_at": t.resolve_expiry(d.get("access_token_expire_in", 24 * 3600), now),
        "refresh_token": d["refresh_token"],
        "refresh_token_expires_at": t.resolve_expiry(d.get("refresh_token_expire_in", 365 * 24 * 3600), now),
        "shop_cipher": "", "region": "MY",
        "granted_scopes": scopes or [],
    }


def probe(deadline_sec=90):
    """Live search probe. Retries 105005 briefly: scope propagation after a fresh
    authorization is per-scope and not instant (observed ~45s on 2026-07-17)."""
    start = time.time()
    while True:
        try:
            data = t.search_creators_page({})
            n = len(data.get("creators") or [])
            print(f"✔ live probe OK — search returned {n} creators")
            return
        except t.ApiError as e:
            if e.code == 105005 and time.time() - start < deadline_sec:
                print("  … scope not propagated yet (105005), retrying in 10s")
                time.sleep(10)
                continue
            raise


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("auth_code", nargs="?")
    p.add_argument("--print-auth-url", metavar="SERVICE_ID")
    p.add_argument("--app-key")
    p.add_argument("--app-secret")
    p.add_argument("--shop-cipher")
    p.add_argument("--set-shop-cipher", metavar="CIPHER")
    a = p.parse_args(argv)

    if a.print_auth_url:
        print(f"Open as the shop's Seller Center account:\n\n  {auth_url(a.print_auth_url)}\n")
        print("After approving, copy the `code=` value from the redirect URL "
              "(a 404 page there is normal) and run:\n"
              "  ./venv/bin/python bootstrap_auth.py <code> --app-key K --app-secret S --shop-cipher C")
        return

    if a.set_shop_cipher:
        rec = t.load_tokens()
        rec["shop_cipher"] = a.set_shop_cipher
        t.save_tokens(rec)
        print("✔ shop_cipher saved")
        probe()
        return

    if not (a.auth_code and a.app_key and a.app_secret):
        p.error("need <auth_code> --app-key --app-secret (or --print-auth-url / --set-shop-cipher)")

    rec = exchange(a.auth_code, a.app_key, a.app_secret)
    if a.shop_cipher:
        rec["shop_cipher"] = a.shop_cipher
    t.save_tokens(rec)
    print(f"✔ tokens saved to {t.TOKENS_JSON} (scopes: {', '.join(rec['granted_scopes']) or 'not reported'})")

    if not rec["shop_cipher"]:
        print("⚠ no shop_cipher yet — affiliate_seller calls REQUIRE it (error 106013).\n"
              "  Copy it from the command center's oauth_credentials row (it is portable\n"
              "  across apps for the same shop) and run:\n"
              "  ./venv/bin/python bootstrap_auth.py --set-shop-cipher <cipher>")
        sys.exit(1)
    probe()


if __name__ == "__main__":
    main()
