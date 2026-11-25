"""Kroger's official public API (developer.kroger.com): store prices and add-to-cart.

- Products API with a client-credentials token (scope product.compact) gives the
  regular and loyalty-promo price of a UPC *at your store*.
- Cart API with a user token (authorization-code flow, scope cart.basic:write) adds
  items to your real kroger.com cart. It cannot read or clear the cart.

The OAuth consent page is opened in the same browser window you're already signed in
with, and the redirect back to http://localhost is intercepted there, so no local web
server is needed.
"""

import asyncio
import base64
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from playwright.async_api import Page

from kroger_cart.coupon_parser import normalize_upc
from kroger_cart.models import CartLine, Product

logger = logging.getLogger(__name__)

API = 'https://api.kroger.com/v1'
BATCH = 50  # filter.productId accepts up to 50 comma-separated ids


class KrogerAPI:
	def __init__(self, client_id: str, client_secret: str, redirect_uri: str, token_file: Path, api_base: str = API):
		assert client_id and client_secret, 'KROGER_CLIENT_ID / KROGER_CLIENT_SECRET are required'
		self.client_id, self.client_secret, self.redirect_uri = client_id, client_secret, redirect_uri
		self.token_file = token_file
		self.api = api_base.rstrip('/')
		self.http = httpx.AsyncClient(timeout=30)
		self._app_token: tuple[str, float] | None = None

	async def aclose(self) -> None:
		await self.http.aclose()

	# ------------------------------------------------------------------ auth

	def _basic(self) -> dict[str, str]:
		raw = f'{self.client_id}:{self.client_secret}'.encode()
		return {'Authorization': 'Basic ' + base64.b64encode(raw).decode()}

	async def _token(self, data: dict[str, str]) -> dict[str, Any]:
		resp = await self.http.post(f'{self.api}/connect/oauth2/token', data=data, headers=self._basic())
		if resp.status_code != 200:
			raise RuntimeError(f'Kroger token request failed (HTTP {resp.status_code}): {resp.text[:300]}')
		return resp.json()

	async def app_token(self) -> str:
		if self._app_token and self._app_token[1] > time.time() + 60:
			return self._app_token[0]
		tok = await self._token({'grant_type': 'client_credentials', 'scope': 'product.compact'})
		self._app_token = (tok['access_token'], time.time() + int(tok.get('expires_in', 1800)))
		return self._app_token[0]

	async def user_token(self, page: Page) -> str:
		"""A cart-capable token: reuse/refresh the saved one, else run the consent flow in `page`."""
		saved = self._load_tokens()
		if saved.get('access_token') and saved.get('expires_at', 0) > time.time() + 60:
			return saved['access_token']
		if saved.get('refresh_token'):
			try:
				return self._save_tokens(
					await self._token({'grant_type': 'refresh_token', 'refresh_token': saved['refresh_token']})
				)
			except RuntimeError as e:
				logger.info('Refresh token rejected (%s); asking for consent again', e)

		query = urlencode(
			{'scope': 'cart.basic:write', 'response_type': 'code', 'client_id': self.client_id, 'redirect_uri': self.redirect_uri}
		)
		code: dict[str, str] = {}

		async def capture(route: Any) -> None:
			params = parse_qs(urlparse(route.request.url).query)
			code['code'] = params.get('code', [''])[0]
			code['error'] = params.get('error_description', params.get('error', ['']))[0]
			await route.fulfill(
				status=200,
				content_type='text/html',
				body='<h2>Kroger cart access granted - you can go back to the terminal.</h2>',
			)

		pattern = self.redirect_uri.split('?')[0] + '*'
		await page.route(pattern, capture)
		try:
			print('\n  Approve "cart access" for your Kroger developer app in the browser window (sign in if asked).')
			await page.goto(f'{self.api}/connect/oauth2/authorize?{query}')
			deadline = time.monotonic() + 300
			while not code and time.monotonic() < deadline:
				await page.wait_for_timeout(500)
		finally:
			await page.unroute(pattern)
		if not code.get('code'):
			raise RuntimeError(f'Did not get cart permission from Kroger: {code.get("error") or "timed out"}')
		tok = await self._token({'grant_type': 'authorization_code', 'code': code['code'], 'redirect_uri': self.redirect_uri})
		return self._save_tokens(tok)

	def _load_tokens(self) -> dict[str, Any]:
		try:
			return json.loads(self.token_file.read_text())
		except (OSError, json.JSONDecodeError):
			return {}

	def _save_tokens(self, tok: dict[str, Any]) -> str:
		tok['expires_at'] = time.time() + int(tok.get('expires_in', 1800))
		self.token_file.parent.mkdir(parents=True, exist_ok=True)
		self.token_file.write_text(json.dumps(tok))
		try:
			self.token_file.chmod(0o600)
		except OSError:
			pass
		return tok['access_token']

	# ------------------------------------------------------------------ products

	async def get_products(self, upcs: list[str], location_id: str, on_progress: Any = None) -> dict[str, Product]:
		"""Price every UPC at `location_id`. UPCs not sold there are simply absent from the result."""
		assert location_id, 'a store location id is required for prices'
		upcs = list(dict.fromkeys(normalize_upc(u) for u in upcs if u))
		out: dict[str, Product] = {}
		for i in range(0, len(upcs), BATCH):
			chunk = upcs[i : i + BATCH]
			for item in await self._products_chunk(chunk, location_id):
				if product := _to_product(item):
					out[product.upc] = product
			if on_progress:
				on_progress(min(i + BATCH, len(upcs)), len(upcs))
		return out

	async def _products_chunk(self, chunk: list[str], location_id: str) -> list[dict[str, Any]]:
		params = {'filter.productId': ','.join(chunk), 'filter.locationId': location_id, 'filter.limit': str(BATCH)}
		for attempt in range(4):
			resp = await self.http.get(
				f'{self.api}/products', params=params, headers={'Authorization': f'Bearer {await self.app_token()}'}
			)
			if resp.status_code == 200:
				return resp.json().get('data', [])
			if resp.status_code in (429, 500, 502, 503, 504):
				await asyncio.sleep(2**attempt)
				continue
			if resp.status_code == 400 and len(chunk) > 1:  # one malformed id poisons the batch; bisect it
				mid = len(chunk) // 2
				return await self._products_chunk(chunk[:mid], location_id) + await self._products_chunk(chunk[mid:], location_id)
			logger.debug('products %s -> HTTP %s %s', chunk[:3], resp.status_code, resp.text[:200])
			return []
		return []

	async def find_location(self, zip_code: str) -> str:
		resp = await self.http.get(
			f'{self.api}/locations',
			params={'filter.zipCode.near': zip_code, 'filter.limit': '1'},
			headers={'Authorization': f'Bearer {await self.app_token()}'},
		)
		data = resp.json().get('data', []) if resp.status_code == 200 else []
		if not data:
			raise RuntimeError(f'No Kroger store found near {zip_code}')
		return data[0]['locationId']

	# ------------------------------------------------------------------ cart

	async def add_to_cart(self, token: str, lines: list[CartLine], modality: str = 'PICKUP') -> None:
		items = [{'upc': line.product.upc, 'quantity': line.quantity, 'modality': modality} for line in lines]
		for i in range(0, len(items), 25):
			resp = await self.http.put(
				f'{self.api}/cart/add', json={'items': items[i : i + 25]}, headers={'Authorization': f'Bearer {token}'}
			)
			if resp.status_code not in (200, 204):
				raise RuntimeError(f'Adding to cart failed (HTTP {resp.status_code}): {resp.text[:300]}')


def _to_product(item: dict[str, Any]) -> Product | None:
	variants = item.get('items') or []
	if not variants:
		return None
	v = variants[0]
	price = v.get('price') or {}
	regular = float(price.get('regular') or 0)
	if regular <= 0:
		return None  # no price at this store -> not orderable here
	stock = (v.get('inventory') or {}).get('stockLevel', '')
	return Product(
		upc=normalize_upc(item.get('upc') or item.get('productId')),
		description=item.get('description', ''),
		brand=item.get('brand', ''),
		size=v.get('size', ''),
		regular_price=regular,
		promo_price=float(price.get('promo') or 0),
		in_stock=stock != 'TEMPORARILY_OUT_OF_STOCK',
	)
