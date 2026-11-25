"""Offline tests for coupon parsing and the cart optimizer (no Kroger access needed)."""

import pytest

from kroger_cart.coupon_parser import normalize_upc, parse_coupon, parse_rule
from kroger_cart.models import Product, RuleKind
from kroger_cart.optimizer import build_plan, coupon_savings


@pytest.mark.parametrize(
	'raw, kind, fields',
	[
		(
			{'title': 'Save $1.00 on 2', 'requirementDescription': 'when you buy TWO (2) Pillsbury products'},
			RuleKind.AMOUNT_OFF,
			{'amount': 1.0, 'quantity': 2},
		),
		({'shortDescription': 'SAVE $0.75 on any ONE (1) Tide Pods'}, RuleKind.AMOUNT_OFF, {'amount': 0.75, 'quantity': 1}),
		({'title': 'Save 50¢', 'shortDescription': 'Save 50¢ on Kroger Bread'}, RuleKind.AMOUNT_OFF, {'amount': 0.5}),
		({'title': 'Save 25%', 'shortDescription': 'Save 25% on Simple Truth Coffee'}, RuleKind.PERCENT_OFF, {'percent': 0.25}),
		(
			{'title': 'Buy 2 Get 1 Free', 'shortDescription': 'Buy 2, Get 1 FREE Cereal'},
			RuleKind.BOGO,
			{'quantity': 3, 'free_units': 1},
		),
		({'title': 'FREE Kroger Ice Cream', 'requirementDescription': 'max value $4.99'}, RuleKind.FREE, {'amount': 4.99}),
		({'title': '2 for $5', 'shortDescription': '2 for $5 Doritos'}, RuleKind.N_FOR, {'quantity': 2, 'price': 5.0}),
		(
			{'title': 'Buy 1', 'displayDescription': "$1.99 Lay's, Kettle Cooked or Tostitos"},
			RuleKind.FIXED_PRICE,
			{'price': 1.99},
		),
		(
			{'title': 'Save $5.00 When You Spend $25.00', 'upcs': ['0001111060933']},
			RuleKind.SPEND_THRESHOLD,
			{'amount': 5.0, 'threshold': 25.0},
		),
		(
			{'title': 'Save $30.00 When You Spend $75.00', 'requirementQuantity': 75},
			RuleKind.ORDER_THRESHOLD,
			{'amount': 30.0, 'threshold': 75.0},
		),
		({'title': '4X Fuel Points', 'shortDescription': 'Earn 4x fuel points on gift cards'}, RuleKind.NON_MONETARY, {}),
		({'title': 'Mystery', 'value': 2, 'requirementQuantity': 3}, RuleKind.AMOUNT_OFF, {'amount': 2.0, 'quantity': 3}),
		({'title': 'Mystery'}, RuleKind.UNKNOWN, {}),
	],
)
def test_parse_rule(raw, kind, fields):
	rule = parse_rule(raw)
	assert rule.kind == kind, rule
	for key, value in fields.items():
		assert getattr(rule, key) == pytest.approx(value), (key, rule)


def test_parse_coupon_fields_and_upcs():
	c = parse_coupon(
		{
			'id': 9,
			'krogerCouponNumber': '800',
			'brandName': 'Tide',
			'title': 'Save $1.00',
			'addedToCard': True,
			'upcs': ['37000123', {'upc': '037000123'}],
		}
	)
	assert c.id == '9' and c.clipped and c.brand == 'Tide'
	assert c.upcs == [normalize_upc('37000123')] and len(c.upcs[0]) == 13


def _p(upc, price, promo=0.0):
	return Product(upc=normalize_upc(upc), description=f'item {upc}', regular_price=price, promo_price=promo)


def _catalog(*items):
	return {p.upc: p for p in items}


def _c(cid, upcs, **raw):
	return parse_coupon({'id': cid, 'upcs': upcs, **raw})


def test_savings_capped_at_price():
	assert coupon_savings(_c('a', ['1'], title='Save $3.00'), _p('1', 2.0)) == 2.0
	assert coupon_savings(_c('a', ['1'], title='Save $1.00 on 2'), _p('1', 2.0, promo=1.5)) == 1.0


def test_picks_cheapest_net_product_per_coupon():
	products = _catalog(_p('1', 4.0), _p('2', 2.5), _p('3', 9.0))
	plan = build_plan([_c('a', ['1', '2', '3'], title='Save $1.00 on 2')], products)
	assert [(line.product.upc, line.quantity) for line in plan.lines] == [(normalize_upc('2'), 2)]
	assert plan.total == pytest.approx(4.0)


def test_units_are_not_shared_between_coupons():
	products = _catalog(_p('1', 3.0))
	coupons = [_c('a', ['1'], title='Save $1.00'), _c('b', ['1'], title='Save $0.50')]
	plan = build_plan(coupons, products)
	assert plan.lines[0].quantity == 2 and plan.savings == pytest.approx(1.5)


def test_spend_threshold_couples_coupons():
	# Brand coupon: save $2 on 1 of X. Brand spend coupon: $3 off when you spend $10 on X.
	products = _catalog(_p('11', 1.0), _p('22', 4.0))
	coupons = [
		_c('item', ['11', '22'], title='Save $2.00'),
		_c('spend', ['11', '22'], title='Save $3.00 when you spend $10.00'),
	]
	plan = build_plan(coupons, products)
	spent = sum(line.product.price * line.quantity for line in plan.lines)
	assert spent >= 10
	assert {a.coupon.id for a in plan.applied} == {'item', 'spend'}
	# 10 x $1 item, $2 + $3 off -> $5. (Using the $4 item for the $2 coupon would cost more.)
	assert plan.total == pytest.approx(5.0)


def test_unusable_coupons_are_reported_not_dropped_silently():
	coupons = [
		_c('fuel', [], title='4x Fuel Points'),
		_c('nostore', ['999'], title='Save $1.00'),
		_c('bad', ['1'], title='$5.99 Steak'),
	]
	plan = build_plan(coupons, _catalog(_p('1', 3.0)))
	assert not plan.lines
	assert {s.coupon.id for s in plan.skipped} == {'fuel', 'nostore', 'bad'}


def test_infeasible_threshold_is_dropped():
	coupons = [_c('a', ['1'], title='Save $1.00'), _c('big', ['1'], title='Save $5 when you spend $9999')]
	plan = build_plan(coupons, _catalog(_p('1', 0.5)))
	assert [s.coupon.id for s in plan.skipped] == ['big']
	assert plan.total == pytest.approx(0.0)


def test_max_net_filter():
	coupons = [_c('pricey', ['1'], title='Save $1.00'), _c('cheap', ['2'], title='Save $1.00')]
	plan = build_plan(coupons, _catalog(_p('1', 20.0), _p('2', 1.2)), max_net_per_coupon=5)
	assert [a.coupon.id for a in plan.applied] == ['cheap']
