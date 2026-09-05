# PayFlow — Backend Transaction Processing API

A small Flask API that simulates payment transfers between users, built to
demonstrate backend reliability concepts relevant to payments engineering:
atomic transactions, row-level locking, idempotency, and basic fraud checks.

## Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Quick local run (uses SQLite, no setup needed):
flask --app app run --debug

# For Postgres instead (closer to production setups):
export DATABASE_URL=postgresql://user:pass@localhost:5432/payflow
export JWT_SECRET_KEY=some-random-secret
flask --app app run --debug
```

On first run, the app auto-creates tables via `db.create_all()`.

## Try it out

```bash
# Register two users
curl -X POST localhost:5000/register -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"pass123"}'
curl -X POST localhost:5000/register -H "Content-Type: application/json" \
  -d '{"username":"bob","password":"pass123"}'

# Log in as alice, grab the access_token from the response
curl -X POST localhost:5000/login -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"pass123"}'

# Deposit funds into alice's account (paise, so 100000 = ₹1000)
curl -X POST localhost:5000/deposit -H "Content-Type: application/json" \
  -H "Authorization: Bearer <TOKEN>" -d '{"amount": 100000}'

# Transfer to bob
curl -X POST localhost:5000/transfer -H "Content-Type: application/json" \
  -H "Authorization: Bearer <TOKEN>" -H "Idempotency-Key: abc-123" \
  -d '{"receiver_username": "bob", "amount": 5000}'

# Retry the SAME request (same Idempotency-Key) - notice bob is NOT
# charged twice; you get back the original transaction.
```

## What this project demonstrates 

**1. Atomic, race-safe transfers**
`transfer()` locks both the sender's and receiver's rows with
`SELECT ... FOR UPDATE` before touching balances, inside a single DB
transaction. This means two simultaneous transfer requests on the same
account can't both read a stale balance and overdraw it. Rows are locked
in a consistent order (lower user ID first) specifically to avoid deadlocks
if two transfers are happening in opposite directions at once.

Run `test_concurrency.py` to see this in action — it fires several
simultaneous transfer requests and shows the balance never goes negative.

**2. Idempotency**
Every transfer request must carry an `Idempotency-Key` header. If a
client's request times out and it retries with the *same* key, the server
recognizes it's already processed that exact request and returns the
original result instead of transferring the money again. This mirrors how
real payment gateways (Stripe, Razorpay, Juspay itself) handle retries
safely.

**3. Money stored as integers, not floats**
Balances and amounts are stored in the smallest currency unit (paise) as
integers. Floats introduce rounding errors that are unacceptable in
financial systems.

**4. Append-only transaction ledger**
Balances are only ever changed alongside a new `Transaction` row, so there's
always a full audit trail — you can reconstruct any balance from transaction
history alone.

**5. Basic fraud/risk placeholder**
`_fraud_check()` flags transfers above a size threshold or too many
transfers in a short window, instead of blindly auto-approving everything.
This is a toy version of what a real risk engine does — worth mentioning
you understand *why* payments companies need this layer, even if this
implementation is simple.

**6. Rate limiting**
`/transfer` is rate-limited per client to blunt abuse/retry storms.

## Possible extensions 

- Move idempotency key checking to happen *before* acquiring locks, with a
  short-lived Redis cache instead of a DB table, for lower latency.
- Add a webhook/event system to notify on transaction status changes.
- Split the fraud check into an async job so it doesn't block the request.
- Add pagination to `/transactions`.
- Write proper unit tests (pytest) instead of the manual concurrency script.
