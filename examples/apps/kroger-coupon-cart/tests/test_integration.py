"""Exercise the real browser + HTTP plumbing against a local fake of kroger.com / api.kroger.com.

Nothing is mocked: a real Chromium is launched over CDP and talks to pytest-httpserver.
"""

import json
import os
import shutil
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from kroger_cart.coupon_parser import parse_coupon
from kroger_cart.kroger_api import KrogerAPI
from kroger_cart.kroger_site import KrogerSite
from kroger_cart.models import CartLine, Product

CHROME = os.environ.get('KROGER_CHROME') or ('/opt/pw-browsers/chromium' if os.path.exists('/opt/pw-browsers/chromium') else None)
CHROME = CHROME or shutil.which('chromium') or shutil.which('google-chrome')

STORE_HEADERS = {
	'x-facility-id': '01400943',
	'x-laf-object': '[{"modality":{"type":"PICKUP"}}]',
	'x-modality': '{"type":"PICKUP","locationId":"01400943"}',
	'x-modality-type': 'PICKUP',
	'x-kroger-channel': 'WEB',
}

COUPONS_PAGE = f"""<html><body>coupons<script>
fetch('/atlas/v1/savings-coupons/v2/coupons?projections=coupons.compact&filter.status=unclipped&page.size=24&page.offset=0',
      {{headers: {json.dumps(STORE_HEADERS)}}});
</script></body></html>"""


def _coupon(i: int) -> dict:
	return {
		'id': str(i),
		'krogerCouponNumber': f'800{i}',
		'title': 'Save $1.00 on 2',
		'brandName': f'Brand{i}',
		'addedToCard': True,
	}


def fake_site(request: Request) -> Response:
	if request.path == '/savings/cl/coupons/':
		return Response(COUPONS_PAGE, content_type='text/html')
	if request.path == '/atlas/v1/savings-coupons/v1/coupons':
		return Response('{"errors":"This endpoint has been deprecated"}', status=410)
	if request.path == '/atlas/v1/savings-coupons/v2/coupons':
		if any(request.headers.get(k) != v for k, v in STORE_HEADERS.items()):
			return Response('', status=400)  # what the real API does without store context
		if number := request.args.get('filter.krogerCouponNumber'):
			return Response(json.dumps({'data': {'coupons': [{**_coupon(int(number[3:])), 'upcs': ['0001111060933']}]}}))
		if request.args.get('projections') == 'coupons.full':
			return Response('', status=400)  # force the compact fallback path
		offset, size = int(request.args['page.offset']), int(request.args['page.size'])
		total = 130
		page = [_coupon(i) for i in range(offset, min(offset + size, total))]
		return Response(
			json.dumps({'data': {'coupons': page}, 'meta': {'coupons': {'page': {'hasMore': offset + size < total}}}})
		)
	if request.path == '/atlas/v1/savings-coupons/v1/clip-unclip':
		body = request.get_json()
		if body['couponId'] == 'full':
			return Response('{"errors":{"code":"TooManyCouponsOnCard"}}', status=422)
		return Response('', status=200)
	return Response('<html><body>home</body></html>', content_type='text/html')


@pytest.fixture
def site_server(httpserver: HTTPServer) -> HTTPServer:
	httpserver.expect_request('').respond_with_handler(fake_site)
	httpserver.expect_request('/', method='POST').respond_with_handler(fake_site)
	return httpserver


@pytest.mark.skipif(not CHROME, reason='no chromium binary available')
async def test_site_enumerates_and_clips_through_the_page(site_server: HTTPServer, tmp_path: Path):
	base = site_server.url_for('/').rstrip('/')
	async with KrogerSite(
		profile_dir=tmp_path / 'profile', chrome=CHROME, base_url=base, extra_args=['--headless=new', '--no-sandbox']
	) as site:
		headers = await site.capture_store_context(settle_ms=10_000)
		assert headers == STORE_HEADERS
		assert site.location_id == '01400943'

		raws = await site.list_coupons(('active',))
		assert [r['id'] for r in raws] == [str(i) for i in range(130)]  # two pages, v1 410 + full 400 fallbacks

		detail = await site.coupon_details(raws[7])
		assert parse_coupon(detail).upcs == ['0001111060933']

		assert await site.clip('7') == 'clipped'
		assert await site.clip('full') == 'card_full'


async def test_official_api_prices_and_cart(httpserver: HTTPServer, tmp_path: Path):
	httpserver.expect_request('/v1/connect/oauth2/token', method='POST').respond_with_json(
		{'access_token': 'app', 'expires_in': 1800}
	)

	def products(request: Request) -> Response:
		assert request.headers['Authorization'] == 'Bearer app'
		assert request.args['filter.locationId'] == '01400943'
		data = []
		for upc in request.args['filter.productId'].split(','):
			if upc.endswith('9'):
				continue  # not sold at this store
			item = {'size': '12 oz', 'price': {'regular': 3.49, 'promo': 2.99}, 'inventory': {'stockLevel': 'HIGH'}}
			data.append({'upc': upc, 'productId': upc, 'description': f'Thing {upc}', 'items': [item]})
		return Response(json.dumps({'data': data}))

	httpserver.expect_request('/v1/products').respond_with_handler(products)
	httpserver.expect_request(
		'/v1/cart/add', method='PUT', json={'items': [{'upc': '0000000000001', 'quantity': 2, 'modality': 'PICKUP'}]}
	).respond_with_data('', status=204)

	api = KrogerAPI('id', 'secret', 'http://localhost:8765/callback', tmp_path / 't.json', api_base=httpserver.url_for('/v1'))
	try:
		upcs = [str(i) for i in range(1, 120)]  # > 2 batches of 50
		got = await api.get_products(upcs, '01400943')
		assert len(got) == 119 - sum(u.endswith('9') for u in upcs)
		assert got['0000000000001'].price == 2.99

		line = CartLine(product=Product(upc='0000000000001', regular_price=1.0), quantity=2)
		await api.add_to_cart('user-token', [line])
	finally:
		await api.aclose()
	httpserver.check_assertions()
