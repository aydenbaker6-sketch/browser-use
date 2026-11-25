"""Minimum-cost cart that redeems every usable coupon, as a mixed-integer linear program.

Decision variables
	x_u  in Z>=0  units of product u in the cart
	w_cu in {0,1} item coupon c is redeemed on product u (buying q_c units of it)

minimise   sum_u p_u x_u  -  sum_{c,u} s_cu w_cu
subject to sum_u w_cu = 1                       for every item coupon c   (use every coupon)
           x_u - sum_c q_c w_cu >= 0            for every product u       (a unit carries at most one coupon)
           sum_{u in S_c} p_u x_u >= T_c        for every "spend $T on X" coupon
           sum_u p_u x_u >= T_o                 for every "$off an order of $T" coupon

where s_cu = savings of coupon c on q_c units of u, already capped at the units' price
(Kroger never pays you to take an item). Spend/order coupons contribute a constant
saving, so they only enter as covering constraints.

Without threshold coupons the program is separable and the optimum is simply the
cheapest-net product per coupon; the thresholds are what couple coupons together
(a unit bought for one coupon also counts toward a brand's "spend $15" coupon),
which is why this is an ILP and not a sort. Solved with HiGHS via scipy.optimize.milp.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

from kroger_cart.models import AppliedCoupon, CartLine, Coupon, Plan, Product, RuleKind, SkippedCoupon

logger = logging.getLogger(__name__)

ITEM_KINDS = {RuleKind.AMOUNT_OFF, RuleKind.PERCENT_OFF, RuleKind.FIXED_PRICE, RuleKind.N_FOR, RuleKind.BOGO, RuleKind.FREE}


def coupon_savings(coupon: Coupon, product: Product) -> float:
	"""Dollars saved by redeeming `coupon` once on `coupon.rule.quantity` units of `product`."""
	rule = coupon.rule
	q, p = rule.quantity, product.price
	gross = q * p
	match rule.kind:
		case RuleKind.AMOUNT_OFF:
			saving = rule.amount
		case RuleKind.PERCENT_OFF:
			saving = rule.percent * gross
		case RuleKind.FIXED_PRICE:
			saving = q * max(0.0, p - rule.price)
		case RuleKind.N_FOR:
			saving = gross - rule.price
		case RuleKind.BOGO:
			saving = rule.free_units * p
		case RuleKind.FREE:
			saving = min(p, rule.amount) * q if rule.amount else gross
		case _:
			saving = 0.0
	return round(max(0.0, min(saving, gross)), 2)


@dataclass
class _Option:
	coupon_idx: int
	upc: str
	savings: float
	net: float


def build_plan(
	coupons: list[Coupon],
	products: dict[str, Product],
	*,
	max_options_per_coupon: int = 25,
	max_net_per_coupon: float | None = None,
	time_limit_s: float = 120,
) -> Plan:
	"""Return the cheapest cart that redeems as many of `coupons` as is feasible."""
	assert max_options_per_coupon >= 1
	skipped: list[SkippedCoupon] = []
	item: list[Coupon] = []
	spend: list[Coupon] = []
	order: list[Coupon] = []
	options: list[_Option] = []

	for coupon in coupons:
		kind = coupon.rule.kind
		if kind in (RuleKind.NON_MONETARY, RuleKind.UNKNOWN):
			reason = (
				'no dollar value (fuel points etc.)' if kind == RuleKind.NON_MONETARY else 'could not understand the coupon text'
			)
			skipped.append(SkippedCoupon(coupon=coupon, reason=reason))
			continue
		if kind == RuleKind.ORDER_THRESHOLD:
			order.append(coupon)
			continue
		priced = [products[u] for u in coupon.upcs if u in products and products[u].in_stock]
		if not priced:
			reason = 'no qualifying products listed' if not coupon.upcs else 'no qualifying product is sold/priced at your store'
			skipped.append(SkippedCoupon(coupon=coupon, reason=reason))
			continue
		if kind == RuleKind.SPEND_THRESHOLD:
			spend.append(coupon)
			continue
		assert kind in ITEM_KINDS, kind
		mine = []
		for product in priced:
			saving = coupon_savings(coupon, product)
			if saving > 0:
				mine.append(_Option(len(item), product.upc, saving, round(coupon.rule.quantity * product.price - saving, 2)))
		if not mine:
			skipped.append(SkippedCoupon(coupon=coupon, reason='saves nothing at current shelf prices'))
			continue
		mine.sort(key=lambda o: (o.net, -o.savings))
		if max_net_per_coupon is not None and mine[0].net > max_net_per_coupon:
			skipped.append(
				SkippedCoupon(
					coupon=coupon, reason=f'cheapest use still costs ${mine[0].net:.2f} (> --max-net ${max_net_per_coupon:.2f})'
				)
			)
			continue
		item.append(coupon)
		options.extend(mine[:max_options_per_coupon])

	# Threshold coupons can make the program infeasible (e.g. too few qualifying products in the pool);
	# drop the most demanding one and retry rather than failing the whole run.
	while True:
		result = _solve(item, spend, order, options, products, max_options_per_coupon, time_limit_s)
		if result is not None:
			break
		thresholds = spend + order
		assert thresholds, 'item-coupon-only program is always feasible'
		worst = max(thresholds, key=lambda c: c.rule.threshold)
		(spend if worst in spend else order).remove(worst)
		skipped.append(SkippedCoupon(coupon=worst, reason=f'could not reach the ${worst.rule.threshold:.2f} spend threshold'))

	x, chosen = result
	coupon_ids: dict[str, list[str]] = defaultdict(list)
	applied: list[AppliedCoupon] = []
	for opt in chosen:
		coupon = item[opt.coupon_idx]
		coupon_ids[opt.upc].append(coupon.id)
		applied.append(AppliedCoupon(coupon=coupon, savings=opt.savings, upc=opt.upc, units=coupon.rule.quantity))
	for coupon in spend + order:
		applied.append(AppliedCoupon(coupon=coupon, savings=coupon.rule.amount))

	lines = [CartLine(product=products[u], quantity=n, coupon_ids=coupon_ids.get(u, [])) for u, n in x.items() if n > 0]
	lines.sort(key=lambda line: (-len(line.coupon_ids), line.product.description))
	subtotal = round(sum(line.product.price * line.quantity for line in lines), 2)
	savings = round(sum(a.savings for a in applied), 2)
	plan = Plan(lines=lines, applied=applied, skipped=skipped, subtotal=subtotal, savings=min(savings, subtotal))
	assert plan.total >= 0
	return plan


def _solve(
	item: list[Coupon],
	spend: list[Coupon],
	order: list[Coupon],
	options: list[_Option],
	products: dict[str, Product],
	pool_per_threshold: int,
	time_limit_s: float,
) -> tuple[dict[str, int], list[_Option]] | None:
	# Product pool: everything an item coupon could use, plus the cheapest few qualifying
	# products of every spend coupon (padding a threshold is always done with cheap stuff).
	pool: dict[str, None] = {o.upc: None for o in options}
	for coupon in spend:
		cheapest = sorted((u for u in coupon.upcs if u in products), key=lambda u: products[u].price)
		pool.update(dict.fromkeys(cheapest[:pool_per_threshold]))
	if order and not pool:
		pool.update(dict.fromkeys(sorted(products, key=lambda u: products[u].price)[:pool_per_threshold]))
	upcs = list(pool)
	if not upcs:
		return {}, []
	col = {u: i for i, u in enumerate(upcs)}
	n_x, n_w = len(upcs), len(options)
	n = n_x + n_w
	price = np.array([products[u].price for u in upcs])

	cost = np.concatenate([price, -np.array([o.savings for o in options])])
	rows = len(item) + n_x + len(spend) + len(order)
	A = lil_matrix((rows, n))
	lb, ub = np.zeros(rows), np.full(rows, np.inf)

	for j, o in enumerate(options):
		A[o.coupon_idx, n_x + j] = 1
	lb[: len(item)] = ub[: len(item)] = 1

	units_needed = np.zeros(n_x)
	for j, o in enumerate(options):
		q = item[o.coupon_idx].rule.quantity
		A[len(item) + col[o.upc], n_x + j] = -q
		units_needed[col[o.upc]] += q
	for i in range(n_x):
		A[len(item) + i, i] = 1

	base = len(item) + n_x
	for k, coupon in enumerate(spend):
		qualifying = set(coupon.upcs)
		for u in upcs:
			if u in qualifying:
				A[base + k, col[u]] = products[u].price
		lb[base + k] = coupon.rule.threshold
	base += len(spend)
	for k, coupon in enumerate(order):
		A[base + k, :n_x] = price
		lb[base + k] = coupon.rule.threshold

	# Upper bound on units: whatever the coupons need plus room to pad a threshold.
	biggest_threshold = max([c.rule.threshold for c in spend + order], default=0)
	pad = np.ceil(biggest_threshold / np.maximum(price, 0.01))
	x_ub = units_needed + np.minimum(pad, 50)
	bounds = Bounds(np.zeros(n), np.concatenate([x_ub, np.ones(n_w)]))

	res = milp(
		cost,
		constraints=LinearConstraint(A.tocsr(), lb, ub),
		integrality=np.ones(n),
		bounds=bounds,
		options={'time_limit': time_limit_s, 'disp': False},
	)
	if res.x is None:
		logger.debug('MILP status %s: %s', res.status, res.message)
		return None
	if res.status != 0:
		logger.warning('Solver stopped early (%s); using the best cart found so far', res.message)
	sol = np.rint(res.x).astype(int)
	x = {u: int(sol[col[u]]) for u in upcs}
	chosen = [o for j, o in enumerate(options) if sol[n_x + j] == 1]
	assert len(chosen) == len(item), 'every item coupon must be redeemed exactly once'
	return x, chosen
