"""Read (and optionally clip) your digital coupons through kroger.com's own internal API.

Kroger has no public coupon API. The website's SPA calls `/atlas/v1/savings-coupons/...`
endpoints that sit behind Akamai Bot Manager, which drops non-browser TLS fingerprints
before an HTTP status is even returned. So instead of an HTTP client this:

1. launches your real Chrome/Edge as a plain subprocess with a dedicated profile
   (nothing automates the launch, so `navigator.webdriver` stays false),
2. lets you sign in by hand (captcha / 2FA / whatever Kroger throws at you),
3. attaches over CDP, loads the coupons page and copies the store-context headers the
   SPA itself sends (x-laf-object, x-facility-id, ...), and
4. issues every JSON call with `fetch()` from inside that page, so cookies, TLS,
   referer and sec-fetch-* are exactly the browser's.

Endpoint knowledge comes from community reverse engineering (unofficial, may change).
"""

import asyncio
import json
import logging
import os
import random
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlencode

from playwright.async_api import Browser, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

logger = logging.getLogger(__name__)

COUPONS_PAGE = '/savings/cl/coupons/'
COUPONS_API_MARKER = '/atlas/v1/savings-coupons/'
ENUMERATE_PATHS = ('/atlas/v1/savings-coupons/v2/coupons', '/atlas/v1/savings-coupons/v1/coupons')
CLIP_PATH = '/atlas/v1/savings-coupons/v1/clip-unclip'
CONTEXT_HEADERS = ('x-facility-id', 'x-laf-object', 'x-modality', 'x-modality-type', 'x-kroger-channel')
PAGE_SIZE = 100
MAX_PAGES = 50
DELAY_S = (0.3, 0.9)  # pacing between sequential calls; parallel bursts are what bot detection looks for

CHROME_CANDIDATES = (
	r'C:\Program Files\Google\Chrome\Application\chrome.exe',
	r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
	os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe'),
	r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
	r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
	'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
	'google-chrome',
	'google-chrome-stable',
	'chromium',
	'chromium-browser',
	'microsoft-edge',
)

_FETCH_JS = """async ({url, method, headers, body}) => {
	const r = await fetch(url, {method, headers, body, credentials: 'include'});
	return {status: r.status, text: await r.text()};
}"""


class KrogerBlocked(RuntimeError):
	"""Akamai refused us (rate limit / bot block). Waiting is the fix, not retrying harder."""


class KrogerSite:
	def __init__(
		self,
		banner: str = 'kroger.com',
		profile_dir: Path = Path('kroger_data/chrome-profile'),
		chrome: str | None = None,
		base_url: str | None = None,
		extra_args: list[str] | None = None,
	):
		self.base = (base_url or f'https://www.{banner.removeprefix("www.")}').rstrip('/')
		self.profile_dir = profile_dir.resolve()
		self.chrome = chrome or os.environ.get('KROGER_CHROME')
		self.extra_args = extra_args or []
		self.headers: dict[str, str] = {}
		self._proc: subprocess.Popen | None = None
		self._pw: Playwright | None = None
		self._browser: Browser | None = None
		self.page: Page | None = None

	# ------------------------------------------------------------------ lifecycle

	async def __aenter__(self) -> Self:
		self.profile_dir.mkdir(parents=True, exist_ok=True)
		port = _free_port()
		binary = _find_chrome(self.chrome)
		logger.info('Launching %s', binary)
		self._proc = subprocess.Popen(  # noqa: ASYNC220 - one-off launch, returns immediately
			[
				binary,
				f'--remote-debugging-port={port}',
				f'--user-data-dir={self.profile_dir}',
				'--no-first-run',
				'--no-default-browser-check',
				*self.extra_args,
				f'{self.base}/',
			],
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
		)
		await asyncio.to_thread(_await_cdp, port, self._proc)
		self._pw = await async_playwright().start()
		self._browser = await self._pw.chromium.connect_over_cdp(f'http://127.0.0.1:{port}')
		ctx = self._browser.contexts[0]
		self.page = ctx.pages[0] if ctx.pages else await ctx.new_page()
		return self

	async def __aexit__(self, *exc: object) -> None:
		try:
			if self._browser:
				await self._browser.close()
			if self._pw:
				await self._pw.stop()
		finally:
			if self._proc and self._proc.poll() is None:
				self._proc.terminate()
				try:
					self._proc.wait(timeout=10)
				except subprocess.TimeoutExpired:
					self._proc.kill()

	# ------------------------------------------------------------------ session

	async def wait_for_user_login(self) -> None:
		"""Block until the human says they're signed in with their store selected."""
		print(
			'\n  A browser window opened on Kroger.\n'
			'  1. Sign in to your Kroger account (the session is remembered for next time).\n'
			'  2. Make sure your store is selected (top of the page).\n'
			'  3. Come back here and press Enter.\n'
		)
		await asyncio.to_thread(input, '  Press Enter once you are signed in... ')

	async def capture_store_context(self, settle_ms: int = 12_000) -> dict[str, str]:
		"""Load the coupons page and copy the store headers the site's own coupon request carries."""
		page = self._require_page()
		captured: dict[str, str] = {}
		pending: list[asyncio.Task] = []

		async def grab(request: Any) -> None:
			try:
				headers = await request.all_headers()
			except PlaywrightError:  # browser may be navigating/closing
				headers = request.headers
			if not captured and headers.get('x-laf-object'):
				captured.update({k: v for k, v in headers.items() if k in CONTEXT_HEADERS})

		def on_request(request: Any) -> None:
			if COUPONS_API_MARKER in request.url and not captured:
				pending.append(asyncio.ensure_future(grab(request)))

		page.on('request', on_request)
		try:
			await page.goto(self.base + COUPONS_PAGE, wait_until='domcontentloaded')
			deadline = time.monotonic() + settle_ms / 1000
			while not captured and time.monotonic() < deadline:
				await asyncio.sleep(0.5)
			await asyncio.gather(*pending, return_exceptions=True)
		finally:
			page.remove_listener('request', on_request)
		self._raise_if_blocked(await page.content())
		if not captured:
			raise RuntimeError(
				'The coupons page never made its coupon API request. Scroll the coupons page in the browser window, '
				'make sure a store is selected, then try again.'
			)
		self.headers = captured
		logger.info('Store context captured for location %s', self.location_id)
		return captured

	@property
	def location_id(self) -> str:
		"""8-digit Kroger location id (also what the official Products/Cart API calls locationId)."""
		if fid := self.headers.get('x-facility-id'):
			return fid
		try:
			modality = json.loads(self.headers.get('x-modality', '{}'))
			return str(modality.get('locationId', ''))
		except json.JSONDecodeError:
			return ''

	# ------------------------------------------------------------------ coupons

	async def list_coupons(self, statuses: tuple[str, ...] = ('active',)) -> list[dict[str, Any]]:
		"""All coupons with the given statuses. 'active' = clipped to your card, 'unclipped' = available."""
		last_error = ''
		for path in ENUMERATE_PATHS:
			for projection in ('coupons.full', 'coupons.compact'):
				found: list[dict[str, Any]] = []
				for page_no in range(MAX_PAGES):
					query = [('projections', projection)] + [('filter.status', s) for s in statuses]
					query += [('page.size', PAGE_SIZE), ('page.offset', page_no * PAGE_SIZE)]
					status, payload, text = await self._fetch(f'{path}?{urlencode(query)}')
					if status != 200 or not isinstance(payload, dict):
						last_error = f'{path} [{projection}] -> HTTP {status}: {text[:200]}'
						logger.debug(last_error)
						break
					batch = _dig(payload, 'data', 'coupons') or []
					found.extend(batch)
					if not batch or not _dig(payload, 'meta', 'coupons', 'page', 'hasMore'):
						logger.info('Enumerated %d coupons via %s (%s)', len(found), path, projection)
						return found
					await _pause()
				else:
					return found
		raise RuntimeError(f'Could not list coupons. Last response: {last_error}')

	async def coupon_details(self, coupon: dict[str, Any]) -> dict[str, Any]:
		"""Full projection of one coupon (includes qualifying UPCs). Returns {} if unavailable."""
		number = coupon.get('krogerCouponNumber') or coupon.get('id')
		ctype = str(coupon.get('type') or 'standard').lower()
		for path in ENUMERATE_PATHS:
			query = urlencode([('filter.krogerCouponNumber', number), ('filter.type', ctype), ('projections', 'coupons.full')])
			status, payload, _ = await self._fetch(f'{path}?{query}')
			if status == 200 and isinstance(payload, dict):
				matches = _dig(payload, 'data', 'coupons') or []
				if matches:
					return matches[0]
		return {}

	async def clip(self, coupon_id: str) -> str:
		"""Clip one coupon to the card. Returns 'clipped', 'already', 'card_full' or an error string."""
		status, _, text = await self._fetch(CLIP_PATH, method='POST', body={'action': 'CLIP', 'couponId': coupon_id})
		if status in (200, 204):
			return 'clipped'
		if 'CouponAlreadyAdded' in text:
			return 'already'
		if 'TooManyCouponsOnCard' in text:
			return 'card_full'
		if status == 401:
			raise RuntimeError('Kroger says you are not signed in (HTTP 401). Sign in in the browser window and rerun.')
		return f'HTTP {status}: {text[:120]}'

	# ------------------------------------------------------------------ plumbing

	async def _fetch(self, path: str, method: str = 'GET', body: dict | None = None) -> tuple[int, Any, str]:
		page = self._require_page()
		assert self.headers, 'call capture_store_context() first'
		headers = {'accept': 'application/json, text/plain, */*', **self.headers}
		if body is not None:
			headers['content-type'] = 'application/json'
		result = await page.evaluate(
			_FETCH_JS,
			{
				'url': self.base + path,
				'method': method,
				'headers': headers,
				'body': json.dumps(body) if body is not None else None,
			},
		)
		status, text = int(result['status']), str(result['text'])
		if status == 429 or 'cpr_chlge' in text[:500]:
			raise KrogerBlocked(
				'Kroger rate-limited this session (HTTP 429). Wait ~15 minutes and rerun with --reuse-data if possible.'
			)
		self._raise_if_blocked(text[:2000])
		try:
			payload = json.loads(text) if text else None
		except json.JSONDecodeError:
			payload = None
		return status, payload, text

	@staticmethod
	def _raise_if_blocked(body: str) -> None:
		for marker in ('Access Denied', 'edgesuite', 'Too many requests'):
			if marker in body:
				raise KrogerBlocked(f'Kroger/Akamai refused the request ({marker}). Wait a while before trying again.')

	def _require_page(self) -> Page:
		assert self.page is not None, 'use `async with KrogerSite(...)`'
		return self.page


async def _pause() -> None:
	await asyncio.sleep(random.uniform(*DELAY_S))


def _dig(obj: Any, *keys: str) -> Any:
	for key in keys:
		if not isinstance(obj, dict):
			return None
		obj = obj.get(key)
	return obj


def _find_chrome(override: str | None) -> str:
	for candidate in (override, *CHROME_CANDIDATES) if override else CHROME_CANDIDATES:
		if not candidate:
			continue
		if os.path.isfile(candidate):
			return candidate
		if found := shutil.which(candidate):
			return found
	raise FileNotFoundError('Could not find Google Chrome or Microsoft Edge. Set KROGER_CHROME to the browser .exe path.')


def _free_port() -> int:
	with socket.socket() as sock:
		sock.bind(('127.0.0.1', 0))
		return sock.getsockname()[1]


def _await_cdp(port: int, proc: subprocess.Popen, timeout_s: float = 30) -> None:
	deadline = time.monotonic() + timeout_s
	while time.monotonic() < deadline:
		if proc.poll() is not None:
			raise RuntimeError(
				f'The browser exited immediately (status {proc.returncode}). '
				'If Chrome was already open with this profile, close it and retry.'
			)
		try:
			with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version', timeout=1):
				return
		except OSError:
			time.sleep(0.25)
	raise RuntimeError(f'The browser did not open its debugging port within {timeout_s:.0f}s')
