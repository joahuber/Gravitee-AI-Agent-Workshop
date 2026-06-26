"""ACME Hotel API — hotel search and booking management."""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field, EmailStr
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger("uvicorn.error")

DATA_DIR = Path(__file__).parent / "data"


# ═══════════════════════════════════════════════════════════════════════════
#  Models
# ═══════════════════════════════════════════════════════════════════════════

class RoomType(BaseModel):
    type: str = Field(..., description="Room type key (e.g. standard, deluxe, suite)", examples=["deluxe"])
    name: str = Field(..., description="Display name", examples=["Park View Deluxe"])
    description: str = Field(..., description="Room description", examples=["Spacious 45m² room with panoramic views."])
    price_per_night: float = Field(..., description="Nightly rate in USD", examples=[480.00])
    capacity: int = Field(..., description="Maximum number of guests", examples=[2])
    count: int = Field(..., description="Total rooms of this type", examples=[15])


class Review(BaseModel):
    author: str = Field(..., description="Reviewer name", examples=["Sarah M."])
    rating: int = Field(..., ge=1, le=5, description="Rating from 1 to 5", examples=[5])
    title: str = Field(..., description="Review title", examples=["Absolutely stunning"])
    comment: str = Field(..., description="Review text")
    date: str = Field(..., description="Review date (YYYY-MM-DD)", examples=["2024-11-15"])


class HotelContact(BaseModel):
    phone: str = Field(..., description="Phone number", examples=["+44 20 7123 4567"])
    email: str = Field(..., description="Reservation email", examples=["reservations@grandlondon.com"])


class Hotel(BaseModel):
    """Full hotel details including rooms, reviews and contact info."""
    id: str = Field(..., description="Unique hotel identifier", examples=["grand-london"])
    name: str = Field(..., description="Hotel name", examples=["The Grand London"])
    city: str = Field(..., description="City", examples=["London"])
    country: str = Field(..., description="Country", examples=["United Kingdom"])
    address: str = Field(..., description="Full street address")
    stars: int = Field(..., ge=1, le=5, description="Star rating (1–5)", examples=[5])
    description: str = Field(..., description="Detailed hotel description")
    check_in_time: str = Field(..., description="Check-in time (HH:MM)", examples=["15:00"])
    check_out_time: str = Field(..., description="Check-out time (HH:MM)", examples=["11:00"])
    amenities: list[str] = Field(..., description="List of amenities", examples=[["free_wifi", "spa", "restaurant"]])
    room_types: list[RoomType] = Field(..., description="Available room categories")
    rating: float = Field(..., ge=0, le=5, description="Average guest rating", examples=[4.8])
    review_count: int = Field(..., description="Total number of reviews", examples=[2847])
    reviews: list[Review] = Field(..., description="Selected guest reviews")
    contact: HotelContact


class HotelSummary(BaseModel):
    """Lightweight hotel listing without reviews."""
    id: str
    name: str
    city: str
    country: str
    address: str
    stars: int
    description: str
    check_in_time: str
    check_out_time: str
    amenities: list[str]
    room_types: list[RoomType]
    rating: float
    review_count: int
    contact: HotelContact


class BookingStatus(str, Enum):
    confirmed = "confirmed"
    completed = "completed"
    cancelled = "cancelled"


class Booking(BaseModel):
    """A hotel room booking."""
    id: str = Field(..., description="Booking reference", examples=["BK-0001"])
    hotel_id: str = Field(..., description="Hotel identifier", examples=["grand-london"])
    hotel_name: str = Field(..., description="Hotel name (denormalised for convenience)", examples=["The Grand London"])
    guest_name: str = Field(..., description="Guest full name", examples=["Louis Litt"])
    guest_email: EmailStr = Field(..., description="Guest email address", examples=["louis.litt@littwheelerwilliamsbennett.com"])
    room_type: str = Field(..., description="Room type booked", examples=["deluxe"])
    check_in: date = Field(..., description="Check-in date", examples=["2025-06-15"])
    check_out: date = Field(..., description="Check-out date", examples=["2025-06-22"])
    guests: int = Field(..., ge=1, description="Number of guests", examples=[2])
    status: BookingStatus = Field(..., description="Booking status", examples=["confirmed"])
    total_price: float = Field(..., description="Total price in USD", examples=[3360.00])
    notes: str = Field("", description="Special requests or notes")
    created_at: datetime = Field(..., description="Booking creation timestamp")
    created_by_user: Optional[str] = Field(
        None,
        description="Identity of the user who created the booking (for an agent call, the user the agent acted on behalf of).",
        examples=["louis.litt@littwheelerwilliamsbennett.com"],
    )
    created_by_agent: Optional[str] = Field(
        None,
        description="Identity of the AI agent that created the booking on the user's behalf, or null when a user created it directly.",
        examples=["hotel-ai-agent"],
    )


class BookingCreate(BaseModel):
    """Request body to create a new booking."""
    hotel_id: str = Field(..., description="Hotel identifier", examples=["grand-london"])
    guest_name: str = Field(..., description="Guest full name", examples=["Louis Litt"])
    guest_email: EmailStr = Field(..., description="Guest email address", examples=["louis.litt@littwheelerwilliamsbennett.com"])
    room_type: str = Field(..., description="Room type to book", examples=["deluxe"])
    check_in: date = Field(..., description="Check-in date (YYYY-MM-DD)", examples=["2025-06-15"])
    check_out: date = Field(..., description="Check-out date (YYYY-MM-DD)", examples=["2025-06-22"])
    guests: int = Field(1, ge=1, description="Number of guests", examples=[2])
    notes: str = Field("", description="Special requests or notes", examples=["Late check-out requested."])


class BookingUpdate(BaseModel):
    """Request body to modify an existing booking. All fields optional."""
    room_type: Optional[str] = Field(None, description="New room type", examples=["suite"])
    check_in: Optional[date] = Field(None, description="New check-in date", examples=["2025-06-16"])
    check_out: Optional[date] = Field(None, description="New check-out date", examples=["2025-06-23"])
    guests: Optional[int] = Field(None, ge=1, description="Updated guest count", examples=[3])
    notes: Optional[str] = Field(None, description="Updated notes")


# ═══════════════════════════════════════════════════════════════════════════
#  Data loading
# ═══════════════════════════════════════════════════════════════════════════

def _load_hotels() -> dict[str, Hotel]:
    raw = yaml.safe_load((DATA_DIR / "hotels.yaml").read_text())
    hotels = {}
    for h in raw["hotels"]:
        h["room_types"] = [RoomType(**rt) for rt in h["room_types"]]
        h["reviews"] = [Review(**r) for r in h.get("reviews", [])]
        h["contact"] = HotelContact(**h["contact"])
        hotels[h["id"]] = Hotel(**h)
    return hotels


def _load_bookings(hotels: dict[str, Hotel]) -> dict[str, Booking]:
    raw = yaml.safe_load((DATA_DIR / "bookings.yaml").read_text())
    today = date.today()
    bookings = {}
    for b in raw["bookings"]:
        hotel = hotels.get(b["hotel_id"])
        hotel_name = hotel.name if hotel else b["hotel_id"]
        check_in = today + timedelta(days=b["check_in_offset_days"])
        check_out = today + timedelta(days=b["check_out_offset_days"])
        booking = Booking(
            id=b["id"],
            hotel_id=b["hotel_id"],
            hotel_name=hotel_name,
            guest_name=b["guest_name"],
            guest_email=b["guest_email"],
            room_type=b["room_type"],
            check_in=check_in,
            check_out=check_out,
            guests=b["guests"],
            status=b["status"],
            total_price=b["total_price"],
            notes=b.get("notes", ""),
            created_at=datetime.now(),
            created_by_user=b.get("created_by_user"),
            created_by_agent=b.get("created_by_agent"),
        )
        bookings[booking.id] = booking
    return bookings


# In-memory stores
_hotels: dict[str, Hotel] = _load_hotels()
_bookings: dict[str, Booking] = {}
_booking_counter: int = 0


def _init_bookings():
    global _bookings, _booking_counter
    _bookings = _load_bookings(_hotels)
    _booking_counter = len(_bookings)


_init_bookings()
logger.info(f"Loaded {len(_hotels)} hotels and {len(_bookings)} seed bookings")


def _next_booking_id() -> str:
    global _booking_counter
    _booking_counter += 1
    return f"BK-{_booking_counter:04d}"


def _calculate_price(hotel: Hotel, room_type_key: str, check_in: date, check_out: date) -> float:
    nights = (check_out - check_in).days
    for rt in hotel.room_types:
        if rt.type == room_type_key:
            return round(rt.price_per_night * nights, 2)
    return 0.0


def _hotel_summary(h: Hotel) -> HotelSummary:
    return HotelSummary(**h.model_dump(exclude={"reviews"}))


# ═══════════════════════════════════════════════════════════════════════════
#  OpenFGA authorization (relationship tuples)
# ═══════════════════════════════════════════════════════════════════════════
#
# The Gravitee gateway enforces booking visibility with an OpenFGA `can_view`
# check (owner or admin-from-hotel). New bookings must register the same
# relationship tuples the seed data uses, otherwise they get filtered out of
# listBookings even though they exist. The API owns the bookings, so it writes
# the tuples on creation. Failures are logged but never block the booking.

OPENFGA_API_URL = os.getenv("OPENFGA_API_URL", "http://openfga:8080").rstrip("/")
OPENFGA_STORE_NAME = os.getenv("OPENFGA_STORE_NAME", "Hotel Booking Authorization")
# Singleton "system" object that every booking is linked to. The accounting role
# is granted at this level so it resolves to can_view on every booking.
OPENFGA_SYSTEM_ID = os.getenv("OPENFGA_SYSTEM_ID", "acme")
# Actor (act.sub) that identifies the AI agent in delegated tokens. The agent's
# booking price limit is enforced here, via the OpenFGA `booking_creator` relation
# (an ABAC condition: price <= limit, with the limit stored in the tuple).
OPENFGA_AGENT_ID = os.getenv("OPENFGA_AGENT_ID", "hotel-ai-agent")
# Price threshold for the controlling-role audit report (agent-created bookings above it).
AUDIT_PRICE_THRESHOLD = float(os.getenv("AUDIT_PRICE_THRESHOLD", "3000"))

_fga_store_id: Optional[str] = None


def _fga_request(method: str, path: str, body: Optional[dict] = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{OPENFGA_API_URL}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read() or b"{}")


def _resolve_fga_store_id() -> str:
    """Resolve the OpenFGA store id by name (cached after first lookup)."""
    global _fga_store_id
    if _fga_store_id:
        return _fga_store_id
    stores = _fga_request("GET", "/stores").get("stores", [])
    store = next((s for s in stores if s.get("name") == OPENFGA_STORE_NAME), None)
    if not store:
        raise RuntimeError(f"OpenFGA store '{OPENFGA_STORE_NAME}' not found")
    _fga_store_id = store["id"]
    return _fga_store_id


def _write_booking_tuples(booking: Booking) -> None:
    """Register owner + hotel + system tuples so the gateway's can_view check allows
    the guest (owner) and hotel admins to see the new booking, and the accounting
    role (granted at system level) can read it too. Mirrors seed data."""
    store_id = _resolve_fga_store_id()
    _fga_request("POST", f"/stores/{store_id}/write", {
        "writes": {"tuple_keys": [
            {"user": f"user:{booking.guest_email}", "relation": "owner", "object": f"booking:{booking.id}"},
            {"user": f"hotel:{booking.hotel_id}", "relation": "hotel", "object": f"booking:{booking.id}"},
            {"user": f"system:{OPENFGA_SYSTEM_ID}", "relation": "system", "object": f"booking:{booking.id}"},
        ]},
    })


async def _register_booking_authorization(booking: Booking) -> None:
    """Best-effort OpenFGA registration; never fails the booking request."""
    try:
        await run_in_threadpool(_write_booking_tuples, booking)
        logger.info(f"OpenFGA: registered owner/hotel tuples for {booking.id}")
    except (urllib.error.URLError, RuntimeError, OSError, ValueError) as exc:
        logger.warning(f"OpenFGA: failed to register tuples for {booking.id}: {exc}")


def _jwt_claims(authorization: Optional[str]) -> dict:
    """Read the claims from the gateway-propagated JWT.

    The gateway has already validated the signature (propagateAuthHeader), so the
    backend only needs to read the claims — it base64-decodes the payload segment."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return {}
    try:
        payload = authorization.split(None, 1)[1].split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, IndexError, TypeError):
        return {}


def _caller_identity(authorization: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Return (user_email, agent_id) for the caller. user_email is the user the
    request acts for; agent_id is the delegation actor (act.sub) when an AI agent
    is acting on the user's behalf, otherwise None."""
    claims = _jwt_claims(authorization)
    agent = (claims.get("act") or {}).get("sub")
    return claims.get("email"), agent


def _fga_check(user: str, relation: str, obj: str, context: Optional[dict] = None) -> bool:
    """Evaluate an OpenFGA relationship, optionally with ABAC condition context."""
    store_id = _resolve_fga_store_id()
    body: dict = {"tuple_key": {"user": user, "relation": relation, "object": obj}}
    if context:
        body["context"] = context
    resp = _fga_request("POST", f"/stores/{store_id}/check", body)
    return bool(resp.get("allowed", False))


async def _agent_may_book_at_price(actor: str, price: float) -> bool:
    """Ask OpenFGA whether the agent may create a booking at this price. The limit
    lives in the `booking_creator` tuple's condition context (ABAC). Fails closed
    (deny) if the decision cannot be obtained."""
    try:
        return await run_in_threadpool(
            _fga_check, f"agent:{actor}", "booking_creator",
            f"system:{OPENFGA_SYSTEM_ID}", {"price": price},
        )
    except (urllib.error.URLError, RuntimeError, OSError, ValueError) as exc:
        logger.warning(f"OpenFGA: price-limit check failed for agent:{actor} at {price}: {exc}")
        return False


# ═══════════════════════════════════════════════════════════════════════════
#  FastAPI application
# ═══════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="ACME Hotel API",
    version="1.0.0",
    description=(
        "A comprehensive hotel booking API providing hotel search, detailed "
        "information, and full booking management. Designed to be consumed by "
        "developers and MCP tool servers."
    ),
)


# ── Health ────────────────────────────────────────────────────────────────

@app.get("/health", tags=["System"], summary="Health check",
         operation_id="getHealthStatus",
         description="Returns API health status. Used by container orchestration and load balancers.")
async def health():
    return {"status": "healthy"}


# ── Hotels ────────────────────────────────────────────────────────────────

@app.get(
    "/hotels",
    response_model=list[HotelSummary],
    tags=["Hotels"],
    summary="Search hotels",
    operation_id="searchHotels",
    description=(
        "Search and filter hotels across all cities. Returns hotel summaries "
        "(without full reviews). All filters are optional and combine with AND logic. "
        "Use `q` for free-text search across name, city, country, and description."
    ),
)
async def search_hotels(
    q: Optional[str] = Query(None, description="Free-text search across name, city, country, and description"),
    city: Optional[str] = Query(None, description="Filter by city name (case-insensitive)", examples=["Paris"]),
    country: Optional[str] = Query(None, description="Filter by country (case-insensitive)", examples=["France"]),
    min_price: Optional[float] = Query(None, ge=0, description="Minimum nightly price (USD) across any room type"),
    max_price: Optional[float] = Query(None, ge=0, description="Maximum nightly price (USD) across any room type"),
    min_rating: Optional[float] = Query(None, ge=0, le=5, description="Minimum guest rating (0–5)"),
    stars: Optional[int] = Query(None, ge=1, le=5, description="Exact star rating (1–5)"),
    amenity: Optional[list[str]] = Query(None, description="Required amenities (all must be present)", examples=[["spa", "swimming_pool"]]),
):
    results = list(_hotels.values())

    if q:
        q_lower = q.lower()
        results = [
            h for h in results
            if q_lower in h.name.lower()
            or q_lower in h.city.lower()
            or q_lower in h.country.lower()
            or q_lower in h.description.lower()
        ]
    if city:
        results = [h for h in results if h.city.lower() == city.lower()]
    if country:
        results = [h for h in results if h.country.lower() == country.lower()]
    if min_rating is not None:
        results = [h for h in results if h.rating >= min_rating]
    if stars is not None:
        results = [h for h in results if h.stars == stars]
    if amenity:
        amenity_set = set(a.lower() for a in amenity)
        results = [h for h in results if amenity_set.issubset(set(a.lower() for a in h.amenities))]
    if min_price is not None or max_price is not None:
        filtered = []
        for h in results:
            prices = [rt.price_per_night for rt in h.room_types]
            lowest, highest = min(prices), max(prices)
            if min_price is not None and highest < min_price:
                continue
            if max_price is not None and lowest > max_price:
                continue
            filtered.append(h)
        results = filtered

    return [_hotel_summary(h) for h in results]


@app.get(
    "/hotels/{hotel_id}",
    response_model=Hotel,
    tags=["Hotels"],
    summary="Get hotel details",
    operation_id="getHotelById",
    description="Returns full hotel details including room types, reviews, and contact information.",
)
async def get_hotel(hotel_id: str):
    hotel = _hotels.get(hotel_id)
    if not hotel:
        raise HTTPException(status_code=404, detail=f"Hotel '{hotel_id}' not found")
    return hotel


@app.get(
    "/hotels/{hotel_id}/reviews",
    response_model=list[Review],
    tags=["Hotels"],
    summary="Get hotel reviews",
    operation_id="getHotelReviews",
    description="Returns all guest reviews for a specific hotel.",
)
async def get_hotel_reviews(hotel_id: str):
    hotel = _hotels.get(hotel_id)
    if not hotel:
        raise HTTPException(status_code=404, detail=f"Hotel '{hotel_id}' not found")
    return hotel.reviews


# ── Bookings ──────────────────────────────────────────────────────────────

@app.get(
    "/bookings",
    response_model=list[Booking],
    tags=["Bookings"],
    summary="List bookings",
    operation_id="listBookings",
    description=(
        "Returns all bookings. Authorization is enforced at the gateway: an OpenFGA "
        "`can_view` response filter narrows the list per caller (a guest sees their "
        "own bookings, the accounting role sees every booking)."
    ),
)
async def list_bookings():
    # The API intentionally returns the full set; the gateway's FGA response filter
    # is the policy enforcement point and decides which bookings each caller may see.
    return list(_bookings.values())


@app.get(
    "/bookings/{booking_id}",
    response_model=Booking,
    tags=["Bookings"],
    summary="Get booking details",
    operation_id="getBookingById",
    description="Returns full details of a specific booking by its reference ID.",
)
async def get_booking(booking_id: str):
    booking = _bookings.get(booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail=f"Booking '{booking_id}' not found")
    return booking


@app.post(
    "/bookings",
    response_model=Booking,
    status_code=201,
    tags=["Bookings"],
    summary="Create a booking",
    operation_id="createBooking",
    description=(
        "Create a new hotel room booking. The total price is calculated from the room "
        "type's nightly rate and the stay duration. When the caller is the AI agent, the "
        "booking price is authorized against the OpenFGA spending limit. Validates hotel "
        "existence, room type, dates, and guest capacity."
    ),
)
async def create_booking(
    body: BookingCreate,
    authorization: Optional[str] = Header(None, include_in_schema=False),
):
    hotel = _hotels.get(body.hotel_id)
    if not hotel:
        raise HTTPException(status_code=404, detail=f"Hotel '{body.hotel_id}' not found")

    room = next((rt for rt in hotel.room_types if rt.type == body.room_type), None)
    if not room:
        available = [rt.type for rt in hotel.room_types]
        raise HTTPException(status_code=400, detail=f"Room type '{body.room_type}' not found. Available: {available}")

    if body.check_out <= body.check_in:
        raise HTTPException(status_code=400, detail="check_out must be after check_in")

    if body.guests > room.capacity:
        raise HTTPException(status_code=400, detail=f"Room type '{body.room_type}' has a maximum capacity of {room.capacity} guests")

    total = _calculate_price(hotel, body.room_type, body.check_in, body.check_out)

    # Who is creating this booking? The acting user (always) and, when an AI agent is
    # acting on the user's behalf (delegated token), the agent identity too.
    user_email, agent = _caller_identity(authorization)

    # Spending limit: if this request is the AI agent acting on a user's behalf, ask
    # OpenFGA whether the agent may book at this (server-computed) price — an ABAC
    # condition price <= limit, with the limit stored in the booking_creator tuple.
    # Humans are not subject to this limit. Enforced here in the backend.
    if agent == OPENFGA_AGENT_ID and not await _agent_may_book_at_price(agent, total):
        raise HTTPException(
            status_code=403,
            detail=f"The AI agent is not authorized to create a booking priced at {total} (exceeds the configured spending limit).",
        )
    booking = Booking(
        id=_next_booking_id(),
        hotel_id=body.hotel_id,
        hotel_name=hotel.name,
        guest_name=body.guest_name,
        guest_email=body.guest_email,
        room_type=body.room_type,
        check_in=body.check_in,
        check_out=body.check_out,
        guests=body.guests,
        status=BookingStatus.confirmed,
        total_price=total,
        notes=body.notes,
        created_at=datetime.now(),
        created_by_user=user_email,
        created_by_agent=agent,
    )
    _bookings[booking.id] = booking
    creator = f"agent '{agent}' on behalf of '{user_email}'" if agent else f"user '{user_email}'"
    logger.info(f"Created booking {booking.id} at {hotel.name} for guest {body.guest_email} (created by {creator})")
    await _register_booking_authorization(booking)
    return booking


@app.patch(
    "/bookings/{booking_id}",
    response_model=Booking,
    tags=["Bookings"],
    summary="Update a booking",
    operation_id="updateBooking",
    description=(
        "Modify an existing booking. Only confirmed bookings can be updated. "
        "Send only the fields you want to change. The total price is "
        "automatically recalculated if dates or room type change."
    ),
)
async def update_booking(booking_id: str, body: BookingUpdate):
    booking = _bookings.get(booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail=f"Booking '{booking_id}' not found")
    if booking.status != BookingStatus.confirmed:
        raise HTTPException(status_code=400, detail=f"Cannot modify a {booking.status.value} booking")

    hotel = _hotels.get(booking.hotel_id)

    if body.room_type is not None:
        room = next((rt for rt in hotel.room_types if rt.type == body.room_type), None) if hotel else None
        if not room:
            raise HTTPException(status_code=400, detail=f"Room type '{body.room_type}' not found")
        booking.room_type = body.room_type

    if body.check_in is not None:
        booking.check_in = body.check_in
    if body.check_out is not None:
        booking.check_out = body.check_out
    if booking.check_out <= booking.check_in:
        raise HTTPException(status_code=400, detail="check_out must be after check_in")

    if body.guests is not None:
        room = next((rt for rt in hotel.room_types if rt.type == booking.room_type), None) if hotel else None
        if room and body.guests > room.capacity:
            raise HTTPException(status_code=400, detail=f"Room type '{booking.room_type}' max capacity is {room.capacity}")
        booking.guests = body.guests

    if body.notes is not None:
        booking.notes = body.notes

    # Recalculate price
    if hotel:
        booking.total_price = _calculate_price(hotel, booking.room_type, booking.check_in, booking.check_out)

    logger.info(f"Updated booking {booking_id}")
    return booking


@app.delete(
    "/bookings/{booking_id}",
    response_model=Booking,
    tags=["Bookings"],
    summary="Cancel a booking",
    operation_id="cancelBooking",
    description="Cancel an existing booking. Only confirmed bookings can be cancelled. Returns the updated booking with status 'cancelled'.",
)
async def cancel_booking(booking_id: str):
    booking = _bookings.get(booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail=f"Booking '{booking_id}' not found")
    if booking.status == BookingStatus.cancelled:
        raise HTTPException(status_code=400, detail="Booking is already cancelled")
    if booking.status == BookingStatus.completed:
        raise HTTPException(status_code=400, detail="Cannot cancel a completed booking")

    booking.status = BookingStatus.cancelled
    logger.info(f"Cancelled booking {booking_id}")
    return booking


# ── Audit (controlling role) ────────────────────────────────────────────────

@app.get(
    "/audit-report",
    response_model=list[Booking],
    tags=["Audit"],
    summary="Audit agent over-limit bookings",
    operation_id="auditReport",
    description=(
        "Controlling report: lists every booking created by an AI agent whose total "
        f"price exceeds {AUDIT_PRICE_THRESHOLD:.0f}. Access is restricted to the "
        "'controlling' role, enforced at the gateway (OpenFGA)."
    ),
)
async def audit_report():
    # Authorization (the 'controlling' role) is enforced at the gateway; the API
    # just returns the agent-created, over-limit bookings.
    return [
        b for b in _bookings.values()
        if b.created_by_agent and b.total_price > AUDIT_PRICE_THRESHOLD
    ]


# ═══════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════

def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
