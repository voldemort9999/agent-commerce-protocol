"""SPEC.md ka literal Python translation. Ye models hi contract hain -
galat shape yahin reject ho jati hai, endpoint tak pahunchti hi nahi.
FastAPI inse /docs pe live contract bhi bana deta hai.
"""
from typing import Any, Literal

from pydantic import BaseModel, Field, model_serializer

SPEC_VERSION = "1.0"

OrderStatus = Literal["created", "paid", "confirmed", "shipped", "delivered",
                      "cancelled", "refunded", "failed"]
PaymentMode = Literal["payment_link", "checkout", "cod"]
# `refund_pending` aur `refund_failed` SPEC v1.6 me jude. "Paisa wapas bhej diya" aur
# "paisa wapas aa gaya" do alag baatein hain, aur purane enum me doosri wali kehne ka
# koi tareeka hi nahi tha - to cancel karte hi order "refunded" ho jata tha jabki
# provider ne "pending" kaha hota tha.
PaymentState = Literal["pending", "paid", "failed",
                       "refund_pending", "refunded", "refund_failed"]


class Spec(BaseModel):
    spec_version: str = SPEC_VERSION


# ---------- 3. manifest ----------
class Shipping(BaseModel):
    pincode_required: bool
    flat_paise: int | None = None
    free_above_paise: int | None = None


class Policies(BaseModel):
    cancel_window_hours: int
    max_qty_per_variant: int


class CatalogInfo(BaseModel):
    product_count: int | None = None
    last_updated_at: str | None = None


class Manifest(Spec):
    merchant_id: str
    name: str
    currency: str
    categories: list[str]
    payment_modes: list[PaymentMode]
    shipping: Shipping
    policies: Policies
    catalog: CatalogInfo


# ---------- 4. catalog ----------
class PriceRange(BaseModel):
    min: int
    max: int


class VariantOption(BaseModel):
    name: str
    values: list[str]


class CatalogProduct(BaseModel):
    product_id: str
    title: str
    description: str
    category: str
    tags: list[str] = []
    brand: str | None = None
    images: list[str] = []
    price_range_paise: PriceRange
    in_stock: bool
    variant_options: list[VariantOption] = []
    variant_count: int = Field(ge=1)  # D-11
    rating_avg: float | None = None
    rating_count: int | None = None
    updated_at: str


class CatalogPage(Spec):
    products: list[CatalogProduct]
    cursor: str | None = None
    has_more: bool


# ---------- 5. product detail ----------
class Variant(BaseModel):
    variant_id: str
    sku: str | None = None
    options: dict[str, str] = {}
    price_paise: int
    mrp_paise: int | None = None
    stock: int
    images: list[str] = []


class Review(BaseModel):
    author: str
    rating: float
    title: str | None = None
    body: str
    created_at: str


class Delivery(BaseModel):
    """SPEC 5: `serviceable: false` par `shipping_paise` aur `eta_days` **omit** hote hain.

    Pehle ye `null` jate the. Farq chhota dikhta hai aur nahi hai: do merchants ne do
    alag shapes diye (ek `null` ke saath, do bina key ke), aur ek consumer jo
    `delivery["eta_days"]` padhta hai wo ek dukaan par chalta hai aur doosri par girta
    hai. `exclude_none` yahi karta hai — jo value hai hi nahi wo bheji hi nahi jati.
    """
    serviceable: bool
    pincode: str
    shipping_paise: int | None = None
    eta_days: int | None = None

    @model_serializer
    def _omit_absent(self):
        # `model_dump` ko override karna yahan KAAM NAHI karta: Pydantic v2 nested model
        # ko parent ke serializer se likhta hai, child ka `model_dump` bulaya hi nahi
        # jata. Ye ek chup-chaap na-lagne wala fix tha — code theek dikhta tha aur output
        # waisa ka waisa rehta tha.
        out = {"serviceable": self.serviceable, "pincode": self.pincode}
        if self.shipping_paise is not None:
            out["shipping_paise"] = self.shipping_paise
        if self.eta_days is not None:
            out["eta_days"] = self.eta_days
        return out


class ProductDetail(Spec):
    product_id: str
    title: str
    description: str
    category: str
    tags: list[str] = []
    brand: str | None = None
    images: list[str] = []
    attributes: dict[str, str] = {}
    variants: list[Variant] = Field(min_length=1)
    rating_avg: float | None = None
    rating_count: int | None = None
    reviews: list[Review] = Field(default=[], max_length=5)
    related_product_ids: list[str] = Field(default=[], max_length=10)
    delivery: Delivery | None = None
    updated_at: str


# ---------- 6. create order ----------
class OrderItemIn(BaseModel):
    variant_id: str
    qty: int = Field(ge=1)
    expected_price_paise: int


class Contact(BaseModel):
    name: str
    phone: str
    email: str | None = None


class Address(BaseModel):
    line1: str
    line2: str | None = None
    city: str
    state: str
    pincode: str
    country: str = "IN"


class CreateOrder(BaseModel):
    items: list[OrderItemIn] = Field(min_length=1)
    expected_items_total_paise: int
    contact: Contact
    address: Address
    payment_mode: PaymentMode
    coupon_code: str | None = None


class OrderLine(BaseModel):
    variant_id: str
    title: str
    qty: int
    unit_price_paise: int
    line_total_paise: int


class PaymentOut(BaseModel):
    mode: PaymentMode
    link_url: str | None = None
    razorpay_order_id: str | None = None
    # SPEC v1.4: checkout mode me public key_id lazmi hai. Akela order id pay nahi hota -
    # har checkout client ko wo key chahiye jo account pehchanti hai. Secret kabhi nahi.
    razorpay_key_id: str | None = None
    expires_at: str | None = None


class OrderDelivery(BaseModel):
    """Order banate waqt ka delivery waada. Ye LIVE estimate nahi hai - wo `promised_by`
    ki poori keemat hi khatam kar deta: agent baad me poochhta *"kab aayega"* aur har
    baar aaj se teen din ka naya jawab milta, chahe order haftey pehle bana ho."""
    eta_days: int
    promised_by: str
    pincode: str


class OrderCreated(Spec):
    order_id: str
    status: OrderStatus
    items: list[OrderLine]
    items_total_paise: int
    shipping_paise: int
    discount_paise: int
    final_total_paise: int
    currency: str
    payment: PaymentOut
    delivery: OrderDelivery | None = None
    cancellable_until: str
    created_at: str


# ---------- 7. order status ----------
class PaymentStatus(BaseModel):
    mode: PaymentMode
    state: PaymentState
    razorpay_payment_id: str | None = None
    paid_at: str | None = None


class TimelineEntry(BaseModel):
    status: OrderStatus
    at: str
    note: str | None = None


class Refund(BaseModel):
    state: str
    amount_paise: int
    razorpay_refund_id: str | None = None
    expected_by: str | None = None
    # Refund fail ho sakta hai (test mode me paylater refundable nahi hai). Wajah pehle
    # model se chup-chaap gir jati thi, jabki "refund failure is surfaced, never masked"
    # is project ka likha hua faisla hai.
    error: str | None = None


class OrderView(Spec):
    order_id: str
    status: OrderStatus
    items: list[OrderLine]
    items_total_paise: int
    shipping_paise: int
    discount_paise: int
    final_total_paise: int
    payment: PaymentStatus
    refund: Refund | None = None
    delivery: OrderDelivery | None = None
    timeline: list[TimelineEntry]
    cancellable: bool
    # `null` jab window band ho chuki ho: `cancellable: false` ke saath ek future date
    # rakhna do ulte jawab ek saath dena hai.
    cancellable_until: str | None = None


# ---------- 8. cancel ----------
class CancelRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=200)


class CancelResponse(Spec):
    order_id: str
    status: OrderStatus
    refund: Refund | None = None
    cancelled_at: str


# ---------- 2.6 error envelope ----------
class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorBody
