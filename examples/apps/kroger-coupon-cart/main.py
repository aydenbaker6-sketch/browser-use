"""Build the cheapest Kroger cart that uses all of your digital coupons.

	uv run main.py                     # clipped coupons -> cheapest cart -> (confirm) -> add to your Kroger cart
	uv run main.py --include-unclipped # also consider every available coupon, clip the ones the cart uses
	uv run main.py --max-net 2         # skip coupons whose cheapest use still costs more than $2
	uv run main.py --reuse-data        # re-optimise from the last download, no browser scraping
	uv run main.py --no-cart           # just print/save the plan

See README.md for the one-time Kroger developer-app setup.
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from kroger_cart.coupon_parser import parse_coupon
from kroger_cart.kroger_api import KrogerAPI
from kroger_cart.kroger_site import KrogerBlocked, KrogerSite
from kroger_cart.models import Coupon, Plan, Product
from kroger_cart.optimizer import build_plan

logger = logging.getLogger('kroger_cart')
PRICE_CACHE_TTL_S = 6 * 3600


def parse_args() -> argparse.Namespace:
	ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	ap.add_argument(
		'--banner', default=os.environ.get('KROGER_BANNER', 'kroger.com'), help='kroger.com, ralphs.com, frysfood.com, ...'
	)
	ap.add_argument('--include-unclipped', action='store_true', help='also use coupons not yet on your card (they get clipped)')
	ap.add_argument(
		'--max-net', type=float, default=None, help='skip a coupon if its cheapest use still costs more than this ($)'
	)
	ap.add_argument('--max-upcs-per-coupon', type=int, default=300, help='cap qualifying products priced per coupon')
	ap.add_argument('--options-per-coupon', type=int, default=25, help='cheapest products per coupon given to the optimizer')
	ap.add_argument('--modality', default='PICKUP', choices=['PICKUP', 'DELIVERY'])
	ap.add_argument('--zip', default=None, help='use the store nearest this ZIP if the site did not report one')
	ap.add_argument(
		'--reuse-data', action='store_true', help='skip scraping, reuse kroger_data/coupons.json (+ fresh price cache)'
	)
	ap.add_argument('--no-cart', action='store_true', help='do not add anything to your Kroger cart')
	ap.add_argument('--yes', '-y', action='store_true', help='do not ask before clipping / adding to cart')
	ap.add_argument('--data-dir', type=Path, default=Path('kroger_data'))
	ap.add_argument('-v', '--verbose', action='store_true')
	return ap.parse_args()


async def scrape_coupons(site: KrogerSite, include_unclipped: bool) -> tuple[list[dict[str, Any]], str]:
	await site.wait_for_user_login()
	await site.capture_store_context()
	raws = await site.list_coupons(('active',))
	print(f'  Found {len(raws)} coupons clipped to your card.')
	if not raws and not include_unclipped:
		print('  (0 clipped coupons usually means you are not signed in, or you have not clipped any - try --include-unclipped.)')
	if include_unclipped:
		extra = await site.list_coupons(('unclipped',))
		print(f'  Found {len(extra)} more available (unclipped) coupons.')
		raws += extra

	missing = [r for r in raws if not r.get('upcs')]
	if missing:
		print(f'  Looking up qualifying products for {len(missing)} coupons (paced to stay polite)...')
	for i, raw in enumerate(missing, 1):
		detail = await site.coupon_details(raw)
		if detail.get('upcs'):
			raw['upcs'] = detail['upcs']
			for key, value in detail.items():
				raw.setdefault(key, value)
		_progress(i, len(missing))
		await asyncio.sleep(0.4)
	return raws, site.location_id


async def price_products(
	api: KrogerAPI, coupons: list[Coupon], location_id: str, cache_file: Path, cap: int
) -> dict[str, Product]:
	wanted = {u for c in coupons for u in c.upcs[:cap]}
	cache = _read_json(cache_file) or {}
	fresh = cache.get('location_id') == location_id and time.time() - cache.get('fetched_at', 0) < PRICE_CACHE_TTL_S
	products = {u: Product(**p) for u, p in cache.get('products', {}).items()} if fresh else {}
	known = set(cache.get('looked_up', [])) if fresh else set()
	todo = sorted(wanted - known)
	if todo:
		print(f'  Pricing {len(todo)} products at store {location_id} via the Kroger API...')
		products.update(await api.get_products(todo, location_id, on_progress=_progress))
	_write_json(
		cache_file,
		{
			'location_id': location_id,
			'fetched_at': cache.get('fetched_at') if fresh and not todo else time.time(),
			'looked_up': sorted(known | wanted),
			'products': {u: p.model_dump() for u, p in products.items()},
		},
	)
	return products


async def main() -> int:
	args = parse_args()
	logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format='%(levelname)s %(name)s: %(message)s')
	load_dotenv()
	data = args.data_dir
	data.mkdir(parents=True, exist_ok=True)
	coupons_file, prices_file = data / 'coupons.json', data / 'prices.json'

	api = KrogerAPI(
		client_id=os.environ.get('KROGER_CLIENT_ID', ''),
		client_secret=os.environ.get('KROGER_CLIENT_SECRET', ''),
		redirect_uri=os.environ.get('KROGER_REDIRECT_URI', 'http://localhost:8765/callback'),
		token_file=data / 'kroger_tokens.json',
	)
	site: KrogerSite | None = None
	try:
		if args.reuse_data:
			saved = _read_json(coupons_file)
			if not saved:
				print(f'No saved coupons at {coupons_file}; run once without --reuse-data.')
				return 1
			raws, location_id = saved['coupons'], saved['location_id']
		else:
			site = await KrogerSite(args.banner, data / 'chrome-profile').__aenter__()
			raws, location_id = await scrape_coupons(site, args.include_unclipped)
			_write_json(coupons_file, {'location_id': location_id, 'fetched_at': time.time(), 'coupons': raws})

		if not location_id and args.zip:
			location_id = await api.find_location(args.zip)
		if not location_id:
			print('Could not tell which store you use. Pick a store on kroger.com, or pass --zip 12345.')
			return 1

		coupons = _dedupe([parse_coupon(r) for r in raws])
		products = await price_products(api, coupons, location_id, prices_file, args.max_upcs_per_coupon)
		print(f'  {len(coupons)} coupons, {len(products)} qualifying products priced at your store. Optimizing...')
		plan = build_plan(coupons, products, max_options_per_coupon=args.options_per_coupon, max_net_per_coupon=args.max_net)

		print_plan(plan)
		save_plan(plan, data)
		if not plan.lines or args.no_cart:
			return 0

		to_clip = [a.coupon for a in plan.applied if not a.coupon.clipped]
		question = f'\nAdd these {sum(line.quantity for line in plan.lines)} items to your Kroger cart'
		question += f' and clip {len(to_clip)} coupons? [y/N] ' if to_clip else '? [y/N] '
		if not args.yes and input(question).strip().lower() not in ('y', 'yes'):
			print('Nothing changed. The plan is saved in', data / 'cart.csv')
			return 0

		if site is None:
			site = await KrogerSite(args.banner, data / 'chrome-profile').__aenter__()
			if to_clip:
				await site.wait_for_user_login()
				await site.capture_store_context()
		if to_clip:
			await clip_coupons(site, to_clip)
		assert site.page is not None
		token = await api.user_token(site.page)
		await api.add_to_cart(token, plan.lines, args.modality)
		print(f'\nDone - {len(plan.lines)} products added to your cart. Check out at https://www.{args.banner}/cart')
		print('Note: the Cart API can only ADD items, so anything already in your cart is still there.')
		return 0
	except KrogerBlocked as e:
		print(f'\n{e}')
		return 3
	finally:
		await api.aclose()
		if site is not None:
			await site.__aexit__(None, None, None)


async def clip_coupons(site: KrogerSite, coupons: list[Coupon]) -> None:
	print(f'  Clipping {len(coupons)} coupons...')
	for i, coupon in enumerate(coupons, 1):
		outcome = await site.clip(coupon.id)
		if outcome == 'card_full':
			print(
				'\n  Your card is full (Kroger allows 250 clipped coupons). Unclip some on kroger.com; the rest were not clipped.'
			)
			return
		if outcome not in ('clipped', 'already'):
			print(f'\n  Could not clip "{coupon.label}": {outcome}')
		_progress(i, len(coupons))
		await asyncio.sleep(0.5)


# ---------------------------------------------------------------------- output


def print_plan(plan: Plan) -> None:
	print('\n' + '=' * 96)
	print(f'{"Qty":>3}  {"Item":<52} {"Each":>7} {"Line":>8}  Coupons')
	print('-' * 96)
	for line in plan.lines:
		p = line.product
		name = f'{p.description} {p.size}'.strip()[:52]
		print(f'{line.quantity:>3}  {name:<52} {p.price:>7.2f} {p.price * line.quantity:>8.2f}  {len(line.coupon_ids) or "-"}')
	print('-' * 96)
	print(f'{"Shelf price":>64} {plan.subtotal:>8.2f}')
	print(f'{"Coupon savings":>64} {-plan.savings:>8.2f}')
	print(f'{"YOU PAY (before tax)":>64} {plan.total:>8.2f}')
	print(f'\n  {len(plan.applied)} coupons used, {len(plan.skipped)} could not be used.')
	reasons: dict[str, int] = {}
	for s in plan.skipped:
		reasons[s.reason] = reasons.get(s.reason, 0) + 1
	for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
		print(f'    {count:>4} x {reason}')


def save_plan(plan: Plan, data: Path) -> None:
	with open(data / 'cart.csv', 'w', newline='', encoding='utf-8') as f:
		w = csv.writer(f)
		w.writerow(['quantity', 'upc', 'description', 'size', 'unit_price', 'line_total', 'coupons_used', 'url'])
		by_id = {a.coupon.id: a.coupon.label for a in plan.applied}
		for line in plan.lines:
			p = line.product
			used = ' | '.join(by_id.get(c, c) for c in line.coupon_ids)
			w.writerow(
				[line.quantity, p.upc, p.description, p.size, f'{p.price:.2f}', f'{p.price * line.quantity:.2f}', used, p.url]
			)
	with open(data / 'coupons_report.csv', 'w', newline='', encoding='utf-8') as f:
		w = csv.writer(f)
		w.writerow(['status', 'savings', 'coupon', 'expires', 'reason_or_product'])
		for a in plan.applied:
			w.writerow(['used', f'{a.savings:.2f}', a.coupon.label, a.coupon.expires, a.upc or 'spend/order threshold'])
		for s in plan.skipped:
			w.writerow(['skipped', '', s.coupon.label, s.coupon.expires, s.reason])
	print(f'\n  Saved {data / "cart.csv"} and {data / "coupons_report.csv"}')


def _dedupe(coupons: list[Coupon]) -> list[Coupon]:
	seen: dict[str, Coupon] = {}
	for c in coupons:
		if c.id and c.id not in seen:
			seen[c.id] = c
	return list(seen.values())


def _progress(done: int, total: int) -> None:
	sys.stdout.write(f'\r    {done}/{total}')
	if done >= total:
		sys.stdout.write('\n')
	sys.stdout.flush()


def _read_json(path: Path) -> Any:
	try:
		return json.loads(path.read_text(encoding='utf-8'))
	except (OSError, json.JSONDecodeError):
		return None


def _write_json(path: Path, obj: Any) -> None:
	path.write_text(json.dumps(obj, indent=1), encoding='utf-8')


if __name__ == '__main__':
	sys.exit(asyncio.run(main()))
