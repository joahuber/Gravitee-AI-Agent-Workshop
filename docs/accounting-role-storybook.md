# Storybook — The "accounting" Role

A guided walkthrough of the **accounting** role feature: a role that can **read every
booking** but **cannot modify any booking it does not own**. Only the guest (owner) — or
a hotel admin — may modify a booking.

All authorization is enforced **at the Gravitee gateway**, keyed on the **operation
invoked**. The `acme-hotel-api` backend is intentionally "dumb": it returns the full data
set and performs no authorization itself.

---

## The story

| Actor | Reads | Creates | Modifies / Cancels |
|-------|-------|---------|--------------------|
| **Guest** (e.g. `louis.litt`) | only their own bookings | only for themselves | only their own bookings |
| **Accounting** (`accountant`) | **every** booking | only for themselves | **nothing** (unless they are also the guest) |
| **Anonymous** (no token) | nothing — blocked at the gateway | nothing — blocked at the gateway | nothing — blocked at the gateway |

> **Create rule:** anyone may create a booking, but only **for themselves** — the
> `guest_email` in the request must equal the caller's authenticated email. So the
> accounting user cannot create a booking on behalf of `louis.litt`.

### How it works

```
AI Agent ──(MCP tool call, delegated JWT)──▶ MCP Server API (/hotels-mcp)
                                                   │  translates tool → HTTP method+path
                                                   ▼
                                        ACME Hotels API (/hotels)  ◀── policy enforcement point
                                          ├─ Public plan (KEY_LESS)
                                          │    └─ Block Anonymous Booking Access  → 403 on /bookings
                                          └─ AI Agent plan (JWT)
                                               ├─ request:  Interrupt (delegated-access check)
                                               ├─ request:  Transform Headers (X-User-Email)
                                               ├─ request:  FGA Write Authorization   ← NEW (PATCH/DELETE)
                                               ├─ request:  Self-Booking Authorization ← NEW (POST: guest_email == caller)
                                               └─ response: FGA Response Filter        (GET list, can_view)
                                                   │
                                                   ▼
                                        acme-hotel-api  (returns ALL bookings, no auth)
```

Authorization decisions come from **OpenFGA**, queried through Gravitee Access
Management's AuthZen evaluation endpoint. The model:

```
type system
  relations
    define accounting: [user]

type booking
  relations
    define can_cancel: owner or admin from hotel
    define can_modify: owner or admin from hotel
    define can_view:   owner or admin from hotel or accounting from system
    define owner:      [user, hotel#guest]
    define hotel:      [hotel]
    define system:     [system]
```

- `can_view` includes `accounting from system` → accounting sees everything.
- `can_modify` / `can_cancel` **exclude** accounting → accounting cannot change bookings,
  but if they happen to be the `owner`, the `owner` branch lets them through.

---

## Endpoints & credentials used in this storybook

| Thing | Value |
|-------|-------|
| Backend API (direct) | `http://localhost:8000` |
| APIM Gateway (path `/hotels/…`) | `http://localhost:8082` |
| AM Gateway (OAuth + AuthZen) | `http://localhost:8092` |
| MCP server OAuth client | `hotel-mcp` / `hotel-mcp` |
| Guest user | `louis.litt@littwheelerwilliamsbennett.com` / `HelloWorld@123` |
| Accounting user | `mike.ross@littwheelerwilliamsbennett.com` / `HelloWorld@123` |
| Seed bookings | `BK-0001` … `BK-0008` |

> `BK-0005` is intentionally seeded **without** an `owner` tuple, to demonstrate that a
> guest can be denied `can_view` on a booking — while accounting still sees it.

---

## Part 1 — `curl` walkthrough

These are the exact requests run to verify the feature. Run them from any shell with
`curl` and `python3` available, after `docker compose up -d --build`.

### Test 1 — the backend is "dumb" and returns **all** bookings

The `acme-hotel-api` no longer filters by user; the gateway is the policy enforcement
point.

```bash
curl -s http://localhost:8000/bookings \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('count:',len(d)); print('ids:',[b['id'] for b in d])"
```

Expected:

```
count: 8
ids: ['BK-0001', 'BK-0002', 'BK-0003', 'BK-0004', 'BK-0005', 'BK-0006', 'BK-0007', 'BK-0008']
```

### Test 2 — anonymous access to bookings is blocked at the gateway

The Public (KEY_LESS) plan carries a `Block Anonymous Booking Access` interrupt scoped to
`/bookings`.

```bash
curl -s -o /tmp/kl.txt -w "HTTP %{http_code}\n" http://localhost:8082/hotels/bookings
cat /tmp/kl.txt
```

Expected:

```
HTTP 403
{"message":"Access denied: booking endpoints require an authenticated, delegated access token issued to the AI Agent on behalf of a user.","http_status_code":403}
```

### Test 2c — public hotel search still works (guard is scoped to `/bookings` only)

```bash
curl -s -o /dev/null -w "HTTP %{http_code}\n" "http://localhost:8082/hotels/hotels?city=Paris"
```

Expected:

```
HTTP 200
```

### Test 3 — authorization decisions via AuthZen (the engine the gateway calls)

First get a service token (the same `client_credentials` token the gateway policies use):

```bash
TOKEN=$(curl -s -u hotel-mcp:hotel-mcp -d grant_type=client_credentials \
  http://localhost:8092/gravitee/oauth/token \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
echo "token: ${TOKEN:0:12}..."
```

Helper that evaluates one `(user, action, booking)` decision:

```bash
eval_fga() {
  local user="$1" action="$2" booking="$3"
  local dec=$(curl -s -X POST http://localhost:8092/gravitee/access/v1/evaluation \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"subject\":{\"type\":\"user\",\"id\":\"$user\"},\"resource\":{\"type\":\"booking\",\"id\":\"$booking\"},\"action\":{\"name\":\"$action\"}}" \
    | python3 -c "import sys,json;print(json.load(sys.stdin).get('decision'))")
  printf "  %-28s %-12s %-9s -> %s\n" "$user" "$action" "$booking" "$dec"
}
```

#### 3a — Read all: accounting `can_view` every booking

```bash
for b in BK-0001 BK-0002 BK-0003 BK-0004 BK-0005 BK-0006 BK-0007 BK-0008; do
  eval_fga mike.ross@littwheelerwilliamsbennett.com can_view $b
done
```

Expected — **all `True`**:

```
  mike.ross@littwheelerwilliamsbennett.com       can_view     BK-0001   -> True
  mike.ross@littwheelerwilliamsbennett.com       can_view     BK-0002   -> True
  mike.ross@littwheelerwilliamsbennett.com       can_view     BK-0003   -> True
  mike.ross@littwheelerwilliamsbennett.com       can_view     BK-0004   -> True
  mike.ross@littwheelerwilliamsbennett.com       can_view     BK-0005   -> True
  mike.ross@littwheelerwilliamsbennett.com       can_view     BK-0006   -> True
  mike.ross@littwheelerwilliamsbennett.com       can_view     BK-0007   -> True
  mike.ross@littwheelerwilliamsbennett.com       can_view     BK-0008   -> True
```

#### 3b — Guest scoping preserved (john sees own, not BK-0005)

```bash
eval_fga louis.litt@littwheelerwilliamsbennett.com can_view BK-0001
eval_fga louis.litt@littwheelerwilliamsbennett.com can_view BK-0005
```

Expected:

```
  louis.litt@littwheelerwilliamsbennett.com         can_view     BK-0001   -> True
  louis.litt@littwheelerwilliamsbennett.com         can_view     BK-0005   -> False
```

#### 3c — Accounting cannot modify or cancel

```bash
eval_fga mike.ross@littwheelerwilliamsbennett.com can_modify BK-0002
eval_fga mike.ross@littwheelerwilliamsbennett.com can_cancel BK-0002
```

Expected:

```
  mike.ross@littwheelerwilliamsbennett.com       can_modify   BK-0002   -> False
  mike.ross@littwheelerwilliamsbennett.com       can_cancel   BK-0002   -> False
```

#### 3d — Owner (john) can modify and cancel his own booking

```bash
eval_fga louis.litt@littwheelerwilliamsbennett.com can_modify BK-0002
eval_fga louis.litt@littwheelerwilliamsbennett.com can_cancel BK-0002
```

Expected:

```
  louis.litt@littwheelerwilliamsbennett.com         can_modify   BK-0002   -> True
  louis.litt@littwheelerwilliamsbennett.com         can_cancel   BK-0002   -> True
```

### (Optional) Confirm the new policies are live in the deployed gateway

```bash
# Find the API id
docker exec gio-apim-management-api sh -c '
  T=$(echo -n admin:admin | base64)
  curl -s -H "Authorization: Basic $T" \
    "http://localhost:8083/management/v2/environments/DEFAULT/apis?perPage=50"' \
| python3 -c "import sys,json;a=[x for x in json.load(sys.stdin)['data'] if x['name']=='ACME Hotels API'][0];print(a['id'])" > /tmp/apiid
API_ID=$(cat /tmp/apiid)

# List plan flows + policies
docker exec gio-apim-management-api sh -c "
  T=\$(echo -n admin:admin | base64)
  curl -s -H \"Authorization: Basic \$T\" \
    'http://localhost:8083/management/v2/environments/DEFAULT/apis/$API_ID/plans?perPage=50'" \
| python3 -c "
import sys,json
for p in json.load(sys.stdin)['data']:
    print('PLAN:',p['name'],'|',(p.get('security') or {}).get('type'))
    for f in p.get('flows',[]):
        for ph in ('request','response'):
            for s in f.get(ph,[]):
                print('   ',ph,'->',s.get('name'),'(',s.get('policy'),')')
"
```

Expected — both new policies present:

```
PLAN: Public | KEY_LESS
    request -> Block Anonymous Booking Access ( policy-interrupt )
PLAN: AI Agent | JWT
    request -> Interrupt ( policy-interrupt )
    request -> Transform Headers ( transform-headers )
    request -> FGA Write Authorization ( groovy )
    request -> Self-Booking Authorization ( groovy )
    response -> FGA Response Filter ( groovy )
```

---

## Part 2 — Browser walkthrough (full JWT-plane, end-to-end)

The `curl` tests above verify the decisions and the keyless guard directly. To see the
**delegated-JWT** path live — the AI Agent acting on behalf of a logged-in user — drive it
through the website. Every step is visualized in the embedded **AI Agent Inspector**.

### Setup

1. Make sure the stack is up: `docker compose up -d --build`.
2. Open the **ACME Hotels Website** → **http://localhost:8002**
   The page shows the **AI Agent Inspector** on the left and the website + chat on the
   right. Each request appears as a real-time sequence diagram.

### Scene 1 — Accounting reads **all** bookings

1. Log in as the **accounting** user:
   - Email: `mike.ross@littwheelerwilliamsbennett.com`
   - Password: `HelloWorld@123`
2. In the chat, ask: **"List all bookings."**
3. ✅ **Expected:** the agent returns **all 8 bookings** (BK-0001 … BK-0008), including
   ones owned by other guests and `BK-0005`.
   In the inspector, watch the `listBookings` tool call → `GET /hotels/bookings` → the
   **FGA Response Filter** evaluate `can_view` per booking (all pass via
   `accounting from system`).

### Scene 2 — Accounting is **denied** modifying a booking it doesn't own

1. Still logged in as `mike.ross@littwheelerwilliamsbennett.com`, ask:
   **"Change the number of guests on booking BK-0002 to 1."**
2. ✅ **Expected:** the request is **rejected with 403**. In the inspector the
   `updateBooking` tool call → `PATCH /hotels/bookings/BK-0002` is stopped by the
   **FGA Write Authorization** policy (`can_modify` → `false`) *before* it reaches the
   backend. The agent reports it is not allowed to modify that booking.

### Scene 3 — Accounting is **denied** creating a booking for someone else

1. Still logged in as `mike.ross@littwheelerwilliamsbennett.com`, ask:
   **"Book the deluxe room at Grand London for Louis Litt (louis.litt@littwheelerwilliamsbennett.com) from
   2026-07-01 to 2026-07-02."**
2. ✅ **Expected:** **403**. The `createBooking` tool call → `POST /hotels/bookings`
   is stopped by the **Self-Booking Authorization** policy, because the body's
   `guest_email` (`louis.litt@littwheelerwilliamsbennett.com`) does not match the caller
   (`mike.ross@littwheelerwilliamsbennett.com`). The agent reports it can only create bookings for the
   signed-in user.

   > This is the exact request that previously went through:
   > ```json
   > { "bodySchema": { "check_in": "2026-07-01", "check_out": "2026-07-02",
   >   "hotel_id": "grand-london", "guest_name": "Louis Litt",
   >   "guest_email": "louis.litt@littwheelerwilliamsbennett.com", "room_type": "deluxe" } }
   > ```

3. Now ask the accountant to book **for themselves**:
   **"Book the deluxe room at Grand London for me from 2026-07-01 to 2026-07-02."**
   ✅ **Expected:** **succeeds** — `guest_email` is `mike.ross@littwheelerwilliamsbennett.com`, matching the
   caller, so the policy passes and the booking is created.

### Scene 4 — A guest **can** modify their **own** booking

1. Log out, then log in as the **guest** user:
   - Email: `louis.litt@littwheelerwilliamsbennett.com`
   - Password: `HelloWorld@123`
2. Ask: **"Show my bookings."**
   ✅ **Expected:** only **john's own** bookings (`BK-0001`, `BK-0002`) — not other
   guests', and not `BK-0005`.
3. Ask: **"Change booking BK-0002 to 1 guest."**
   ✅ **Expected:** **succeeds**. `PATCH /hotels/bookings/BK-0002` passes
   `FGA Write Authorization` because john is the `owner`, and the backend applies the
   update.

### Where to look under the hood

- **Gravitee AM Console** → http://localhost:8081 (`admin` / `adminadmin`):
  the **OpenFGA Authorization Model** and **Authorization Tuples** (including the
  `system:acme` / `accounting` tuples).
- **Gravitee APIM Console** → http://localhost:8084 (`admin` / `admin`):
  open **ACME Hotels API** → **Plans** to see the `Block Anonymous Booking Access`,
  `FGA Write Authorization`, and `FGA Response Filter` policies.
- **AI Agent Inspector (standalone)** → http://localhost:9002
- **MCP Inspector** → http://localhost:6274

---

## What enforces what — summary

| Operation | MCP tool | HTTP at gateway | Enforced by | Rule |
|-----------|----------|-----------------|-------------|------|
| Read list | `listBookings` | `GET /hotels/bookings` | **FGA Response Filter** (response) | keep booking if `can_view` |
| Create | `createBooking` | `POST /hotels/bookings` | **Self-Booking Authorization** (request) | allow if `guest_email` == caller email |
| Modify | `updateBooking` | `PATCH /hotels/bookings/:id` | **FGA Write Authorization** (request) | allow if `can_modify` |
| Cancel | `cancelBooking` | `DELETE /hotels/bookings/:id` | **FGA Write Authorization** (request) | allow if `can_cancel` |
| Anything anonymous | — | `* /hotels/bookings*` (KEY_LESS) | **Block Anonymous Booking Access** (request) | always 403 |