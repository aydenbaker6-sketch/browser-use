# Kroger Coupon Cart

Reads every digital coupon on your Kroger account and builds the **cheapest possible cart that redeems all of them**. Then, if you say yes, it adds that cart to your real kroger.com cart.

Works for any Kroger banner (Kroger, Ralphs, Fry's, King Soopers, Fred Meyer, Smith's, Dillons, ...). Prices come from **your** store.

```
================================================================================================
Qty  Item                                                    Each     Line  Coupons
------------------------------------------------------------------------------------------------
  2  Pillsbury Crescent Rolls 8 oz                           2.29     4.58  1
  1  Tide Pods Travel Pack 3 ct                              1.49     1.49  1
  ...
------------------------------------------------------------------------------------------------
                                                     Shelf price   112.40
                                                  Coupon savings   -61.75
                                            YOU PAY (before tax)    50.65

  57 coupons used, 9 could not be used.
       6 x no qualifying product is sold/priced at your store
       3 x no dollar value (fuel points etc.)
```

## How it works

1. **Coupons.** Kroger has no public coupon API, and kroger.com sits behind Akamai bot protection, which blocks normal HTTP clients. So the program opens your own Chrome (or Edge) with a separate profile. You sign in by hand, and the program then reads your coupons by calling the site's own internal coupon API from inside that page, just as the coupons page does.
2. **Prices.** Every product a coupon applies to is priced at your store with Kroger's official [Products API](https://developer.kroger.com). The regular price is used, or the loyalty sale price when there is one.
3. **Optimizing.** The cart is solved as an integer linear program (HiGHS, through `scipy.optimize.milp`):
   - every usable coupon must be redeemed,
   - each unit you buy can carry only one coupon,
   - "Save $5 when you spend $25 on X" coupons force enough qualifying spend,
   - "$10 off an order of $50+" coupons force a big enough cart,
   - a coupon never saves more than the item costs.

   The program minimizes what you pay: `shelf total − coupon savings`. If you only had per-item coupons, this would just be "take the cheapest qualifying item for each coupon". The spend thresholds tie coupons together: something you buy for one coupon also counts toward a brand's "spend $15" coupon. That's why it's an ILP and not a sort.
4. **Cart.** Items are added with Kroger's official Cart API. You approve "cart access" once in the browser.

## One-time setup (Windows)

1. **Get the code:** download this folder (or `git clone` the repo) and open `examples/apps/kroger-coupon-cart`.
2. **Create a free Kroger developer app.** The program needs this for prices and for adding to your cart.
   - Go to https://developer.kroger.com, sign in, then open **My Apps → Create App**.
   - Environment: **Production**. APIs: **Products**, **Cart**, **Locations**.
   - Redirect URI: `http://localhost:8765/callback`
   - Copy the **Client ID** and **Client Secret**.
3. **Double-click `run.bat`.** The first time, it installs [uv](https://docs.astral.sh/uv/) and opens `.env` in Notepad. Paste your Client ID and Secret, save, and run `run.bat` again.

   (Or, by hand: `copy .env.example .env`, edit it, then `uv run main.py`.)

You need Google Chrome or Microsoft Edge installed. The program uses your installed browser, so there's no separate browser download.

## Using it

```bat
run.bat                          :: clipped coupons -> cheapest cart -> asks before touching your cart
run.bat --include-unclipped      :: also use every available coupon, and clip the ones the cart needs
run.bat --max-net 1.50           :: skip coupons whose cheapest use still costs you more than $1.50
run.bat --no-cart                :: only make the plan, don't add anything
run.bat --reuse-data             :: re-optimize from the last download (no scraping)
run.bat --banner ralphs.com      :: a different Kroger banner
```

The first run opens a browser window on kroger.com. Sign in, make sure your store is selected, and press Enter in the terminal. The sign-in is remembered in `kroger_data/chrome-profile` for next time.

Outputs, all in `kroger_data/`:

- `cart.csv`: the shopping list (qty, item, price, which coupons it uses, product link)
- `coupons_report.csv`: every coupon, used or skipped, with the reason
- `coupons.json` / `prices.json`: the raw downloads (prices are cached for 6 hours)

## About "use all my coupons"

The cheapest cart that uses *every* coupon is often not the cheapest way to shop. A "$1 off a $9 roast" coupon still costs you $8. `--max-net X` drops those coupons. Use `--max-net 0` to keep only coupons that make items free.

Coupons are skipped, and listed in `coupons_report.csv`, when:

- they have no dollar value (fuel points),
- the program can't understand their wording,
- none of their products is sold at your store,
- a spend threshold can't be reached.

## Caveats (read these)

- **Unofficial coupon API.** The coupon part depends on kroger.com's internal endpoints (`/atlas/v1/savings-coupons/v2/coupons`). They are undocumented, and Kroger moved them from v1 to v2 as recently as Nov 2026. If they change, the coupon step breaks first. Run with `-v` and look at `kroger_data/coupons.json`.
- **Coupon wording is prose.** Kroger mostly doesn't give a numeric discount, so the amount is parsed from text like "Save $1.00 on 2" or "Buy 2 Get 1 Free". Unusual wording falls back to "unknown" and is skipped rather than guessed.
- **Assumptions:**
  - one digital coupon per unit,
  - spend-threshold coupons count the pre-coupon price, including items that also carry a coupon,
  - each coupon is redeemed once,
  - prices are the API's regular or promo price, without tax, bottle deposits or pickup fees.

  Check the final total at checkout.
- **The Cart API can only add.** It can't read or clear your cart, so empty your cart first if you want only this list in it.
- **Be gentle.** Requests are paced. If Kroger rate-limits you (HTTP 429), wait about 15 minutes. Use `--reuse-data` to re-optimize without hitting the site again.
- Kroger allows at most 250 clipped coupons per card.

## Tests

```bash
uv run --group dev pytest
```

The tests are offline: coupon parsing, the optimizer, and a real headless Chromium run against a local fake of the Kroger site and API.
