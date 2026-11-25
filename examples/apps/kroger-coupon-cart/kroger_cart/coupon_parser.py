"""Turn Kroger's raw coupon JSON (mostly prose) into a structured CouponRule.

Kroger's compact coupon projection carries no numeric discount - the amount lives in
`shortDescription` / `title` text. Some projections do expose `value` and
`requirementQuantity`, so those are used when present and the regexes are the fallback.
Anything that can't be understood is tagged UNKNOWN and reported, never guessed at.
"""

import re
from typing import Any

from kroger_cart.models import Coupon, CouponRule, RuleKind

_WORDS = {
	'a': 1,
	'an': 1,
	'one': 1,
	'two': 2,
	'three': 3,
	'four': 4,
	'five': 5,
	'six': 6,
	'seven': 7,
	'eight': 8,
	'nine': 9,
	'ten': 10,
	'twelve': 12,
}
_NUM = r'(\d+|' + '|'.join(_WORDS) + r')(?:\s*\(\d+\))?'  # "TWO (2)" style
_MONEY = r'\$\s*(\d+(?:\.\d{1,2})?)'


def _n(token: str) -> int:
	token = token.lower()
	return int(token) if token.isdigit() else _WORDS[token]


def _first(raw: dict[str, Any], *keys: str, default: Any = None) -> Any:
	for key in keys:
		value = raw.get(key)
		if value not in (None, '', []):
			return value
	return default


def _to_float(value: Any) -> float | None:
	try:
		return float(value) if value is not None and not isinstance(value, bool) else None
	except (TypeError, ValueError):
		return None


def normalize_upc(upc: str | int) -> str:
	"""Kroger's product API wants 13-digit, zero-padded UPCs."""
	digits = re.sub(r'\D', '', str(upc))
	return digits.zfill(13)[-13:] if digits else ''


def _extract_upcs(raw: dict[str, Any]) -> list[str]:
	found = _first(raw, 'upcs', 'upcList', 'qualifyingUpcs', 'productUpcs', default=[])
	out: list[str] = []
	for item in found:
		value = item.get('upc') or item.get('gtin13') if isinstance(item, dict) else item
		if value and (upc := normalize_upc(value)):
			out.append(upc)
	return list(dict.fromkeys(out))


def _quantity_from_text(text: str) -> int | None:
	for pattern in (
		rf'\bon\s+{_NUM}\b',  # "Save $1.00 on 2"
		rf'\bbuy\s+{_NUM}\b',  # "when you buy TWO (2)"
		rf'\bpurchase\s+(?:of\s+)?{_NUM}\b',
		rf'\bany\s+{_NUM}\b',
	):
		if m := re.search(pattern, text, re.IGNORECASE):
			return _n(m.group(1))
	return None


def parse_rule(raw: dict[str, Any]) -> CouponRule:
	title = str(_first(raw, 'title', default=''))
	short = str(_first(raw, 'shortDescription', default=''))
	display = str(_first(raw, 'displayDescription', default=''))
	requirement = str(_first(raw, 'requirementDescription', default=''))
	text = f'{title} {short} {requirement}'.replace('\n', ' ')
	low = text.lower()

	field_value = _to_float(_first(raw, 'value', 'savings', 'couponValue', 'amount')) or 0.0
	field_qty = _to_float(_first(raw, 'requirementQuantity', 'requiredQuantity', 'minimumQuantity'))
	field_qty = int(field_qty) if field_qty and field_qty >= 1 and field_qty == int(field_qty) else None
	has_upcs = bool(_extract_upcs(raw))

	if re.search(r'fuel\s*points?|\bpoints\b|\d+x\s+fuel', low) and not re.search(r'save\s*\$|\$\s*\d+(?:\.\d+)?\s*off', low):
		return CouponRule(kind=RuleKind.NON_MONETARY)

	# "Save $5 when you spend $25" / "$30 off your first Pickup order of $75+"
	spend = re.search(rf'spend\s+{_MONEY}|order\s+of\s+{_MONEY}|orders?\s+{_MONEY}\s*\+|purchase\s+of\s+{_MONEY}', low)
	if spend:
		threshold = float(next(g for g in spend.groups() if g))
		amount = re.search(rf'(?:save|get)\s+{_MONEY}|{_MONEY}\s*off', low)
		value = float(next(g for g in amount.groups() if g)) if amount else field_value
		if value > 0 and threshold > 0:
			kind = RuleKind.SPEND_THRESHOLD if has_upcs else RuleKind.ORDER_THRESHOLD
			return CouponRule(kind=kind, amount=value, threshold=threshold)

	# "Buy 2 Get 1 Free", "Buy One, Get One FREE"
	if m := re.search(rf'buy\s+{_NUM},?\s+get\s+{_NUM}\s+free', low):
		buy, free = _n(m.group(1)), _n(m.group(2))
		return CouponRule(kind=RuleKind.BOGO, quantity=buy + free, free_units=free)

	qty = field_qty or _quantity_from_text(text) or 1

	if m := re.search(r'(\d+(?:\.\d+)?)\s*%\s*off|save\s+(\d+(?:\.\d+)?)\s*%', low):
		pct = float(m.group(1) or m.group(2)) / 100
		if 0 < pct <= 1:
			return CouponRule(kind=RuleKind.PERCENT_OFF, quantity=qty, percent=pct)

	# "2 for $5"
	if m := re.search(rf'\b{_NUM}\s+for\s+{_MONEY}', low):
		n = _n(m.group(1))
		return CouponRule(kind=RuleKind.N_FOR, quantity=max(n, 1), price=float(m.group(2)))

	if m := re.search(
		rf'save\s+(?:up\s+to\s+)?{_MONEY}|{_MONEY}\s*off|save\s+(\d+)\s*(?:¢|cents?\b)|(\d+)\s*(?:¢|cents?)\s+off', low
	):
		dollars, off, cents, cents_off = m.groups()
		value = float(dollars or off) if (dollars or off) else int(cents or cents_off) / 100
		return CouponRule(kind=RuleKind.AMOUNT_OFF, quantity=qty, amount=value)

	if re.search(r'\bfree\b', low):
		cap = re.search(rf'(?:up\s+to|max(?:imum)?(?:\s+value)?(?:\s+of)?)\s+{_MONEY}', low)
		return CouponRule(kind=RuleKind.FREE, quantity=qty, amount=float(cap.group(1)) if cap else 0.0)

	# "$1.99 Lay's, Kettle Cooked or Tostitos Cantina" / "Only $2.49" / "for $3.99"
	if (m := re.match(rf'\s*{_MONEY}\b', display) or re.match(rf'\s*{_MONEY}\b', title)) or (
		m := re.search(rf'\b(?:only|just|for)\s+{_MONEY}', low)
	):
		return CouponRule(kind=RuleKind.FIXED_PRICE, quantity=qty, price=float(m.group(1)))

	if field_value > 0:
		return CouponRule(kind=RuleKind.AMOUNT_OFF, quantity=qty, amount=field_value)

	return CouponRule(kind=RuleKind.UNKNOWN, quantity=qty)


def parse_coupon(raw: dict[str, Any]) -> Coupon:
	assert isinstance(raw, dict), f'coupon must be a dict, got {type(raw)}'
	status = str(_first(raw, 'status', default='')).lower()
	return Coupon(
		id=str(_first(raw, 'id', 'couponId', 'krogerCouponNumber', default='')),
		coupon_number=str(_first(raw, 'krogerCouponNumber', default='')),
		brand=str(_first(raw, 'brandName', 'brand', default='')),
		title=str(_first(raw, 'title', default='')),
		description=str(_first(raw, 'shortDescription', 'displayDescription', default='')),
		requirement=str(_first(raw, 'requirementDescription', default='')),
		expires=str(_first(raw, 'expirationDate', 'displayEndDate', default='')),
		type=str(_first(raw, 'type', default='standard')).lower(),
		clipped=bool(raw.get('addedToCard')) or status in ('active', 'clipped'),
		upcs=_extract_upcs(raw),
		rule=parse_rule(raw),
		raw=raw,
	)
