"""Pydantic models shared by the scraper, the price lookup and the optimizer."""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class RuleKind(str, Enum):
	AMOUNT_OFF = 'amount_off'  # "Save $1.00 on 2"
	PERCENT_OFF = 'percent_off'  # "Save 25% on 1"
	FIXED_PRICE = 'fixed_price'  # "$1.99 Lay's" -> each unit costs at most $1.99
	N_FOR = 'n_for'  # "2 for $5" -> q units cost at most $5 total
	BOGO = 'bogo'  # "Buy 2 Get 1 Free"
	FREE = 'free'  # "FREE Kroger Ice Cream"
	SPEND_THRESHOLD = 'spend_threshold'  # "Save $5 when you spend $25 on <brand>"
	ORDER_THRESHOLD = 'order_threshold'  # "$10 off a Pickup order of $50+" (no qualifying UPCs)
	NON_MONETARY = 'non_monetary'  # fuel points etc. - no dollar value
	UNKNOWN = 'unknown'


class CouponRule(BaseModel):
	"""What a coupon actually does, normalised out of Kroger's prose."""

	model_config = ConfigDict(extra='forbid')

	kind: RuleKind
	quantity: int = Field(default=1, ge=1)  # units that must be bought for one redemption
	amount: float = 0.0  # dollars off (AMOUNT_OFF / SPEND / ORDER), max value for FREE (0 = uncapped)
	percent: float = 0.0  # 0..1 for PERCENT_OFF
	price: float = 0.0  # unit price for FIXED_PRICE, total price for N_FOR
	free_units: int = 0  # BOGO: how many of `quantity` are free
	threshold: float = 0.0  # SPEND / ORDER: dollars that must be spent


class Coupon(BaseModel):
	model_config = ConfigDict(extra='ignore')

	id: str
	coupon_number: str = ''
	brand: str = ''
	title: str = ''
	description: str = ''
	requirement: str = ''
	expires: str = ''
	type: str = 'standard'
	clipped: bool = False
	upcs: list[str] = Field(default_factory=list)
	rule: CouponRule
	raw: dict = Field(default_factory=dict, exclude=True)

	@property
	def label(self) -> str:
		return f'{self.brand}: {self.description or self.title}'.strip(': ')


class Product(BaseModel):
	model_config = ConfigDict(extra='ignore')

	upc: str
	description: str = ''
	brand: str = ''
	size: str = ''
	regular_price: float
	promo_price: float = 0.0
	in_stock: bool = True

	@property
	def price(self) -> float:
		"""Shelf price with the loyalty-card sale price applied when there is one."""
		if 0 < self.promo_price < self.regular_price:
			return self.promo_price
		return self.regular_price

	@property
	def url(self) -> str:
		return f'https://www.kroger.com/p/-/{self.upc}'


class CartLine(BaseModel):
	product: Product
	quantity: int
	coupon_ids: list[str] = Field(default_factory=list)


class AppliedCoupon(BaseModel):
	coupon: Coupon
	savings: float
	upc: str | None = None  # product the coupon was spent on (None for spend / order thresholds)
	units: int = 0


class SkippedCoupon(BaseModel):
	coupon: Coupon
	reason: str


class Plan(BaseModel):
	lines: list[CartLine]
	applied: list[AppliedCoupon]
	skipped: list[SkippedCoupon]
	subtotal: float
	savings: float

	@property
	def total(self) -> float:
		return round(self.subtotal - self.savings, 2)
