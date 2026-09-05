"""
app.py
PayFlow - a small backend service that simulates payment transfers between
users, built to demonstrate reliability/concurrency concepts relevant to
payments engineering roles.

Run locally:
    export DATABASE_URL=postgresql://user:pass@localhost:5432/payflow
    export JWT_SECRET_KEY=change-me
    flask --app app run --debug

Key concepts demonstrated (worth being able to explain in an interview):
1. Atomic transfers - a transfer either fully succeeds or fully fails.
   We use a single DB transaction with row-level locking (SELECT FOR UPDATE)
   so two simultaneous transfers on the same account can't corrupt the balance.
2. Idempotency - retrying the same transfer request (same Idempotency-Key
   header) does not double-charge the sender.
3. Basic fraud check - transfers above a threshold, or too many transfers
   in a short window, get flagged instead of auto-approved.
4. Rate limiting - protects the API from abuse/hammering.
"""

import os
import uuid
from datetime import datetime, timedelta

from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_jwt_extended import (
    JWTManager, create_access_token, jwt_required, get_jwt_identity
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy.exc import IntegrityError

from models import db, User, Transaction, TransactionStatus, IdempotencyKey

# ---- Config -----------------------------------------------------------

FRAUD_AMOUNT_THRESHOLD = 100000 * 100  # e.g. 100,000 rupees, in paise
FRAUD_VELOCITY_WINDOW_MIN = 5
FRAUD_VELOCITY_MAX_COUNT = 5  # more than 5 transfers in 5 min -> flag

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///payflow_dev.db"  # sqlite fallback for quick local testing
)
app.config["JWT_SECRET_KEY"] = os.environ.get("JWT_SECRET_KEY", "dev-secret-change-me")

db.init_app(app)
jwt = JWTManager(app)
limiter = Limiter(get_remote_address, app=app, default_limits=["200 per hour"])
with app.app_context():
    db.create_all()


# ---- Auth routes -------------------------------------------------------

@app.post("/register")
def register():
    data = request.get_json(force=True)
    username = data.get("username")
    password = data.get("password")

    if not username or not password:
        return jsonify(error="username and password required"), 400

    user = User(username=username, password_hash=generate_password_hash(password))
    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(error="username already taken"), 409

    return jsonify(id=user.id, username=user.username), 201


@app.post("/login")
def login():
    data = request.get_json(force=True)
    user = User.query.filter_by(username=data.get("username")).first()

    if not user or not check_password_hash(user.password_hash, data.get("password", "")):
        return jsonify(error="invalid credentials"), 401

    token = create_access_token(identity=str(user.id))
    return jsonify(access_token=token)


# ---- Account routes ------------------------------------------------

@app.post("/deposit")
@jwt_required()
def deposit():
    """Add funds to the logged-in user's own account (simulates loading money)."""
    data = request.get_json(force=True)
    amount = data.get("amount")

    if not isinstance(amount, int) or amount <= 0:
        return jsonify(error="amount must be a positive integer (paise)"), 400

    user_id = int(get_jwt_identity())
    user = db.session.get(User, int(get_jwt_identity()))
    user.balance += amount
    user.version += 1
    db.session.commit()

    return jsonify(balance=user.balance)


@app.get("/balance")
@jwt_required()
def balance():
    user = db.session.get(User, get_jwt_identity())
    return jsonify(balance=user.balance)


@app.get("/transactions")
@jwt_required()
def transaction_history():
    user_id = int(get_jwt_identity())
    txns = Transaction.query.filter(
        (Transaction.sender_id == user_id) | (Transaction.receiver_id == user_id)
    ).order_by(Transaction.created_at.desc()).all()

    return jsonify([t.to_dict() for t in txns])


# ---- The core transfer endpoint ----------------------------------------

@app.post("/transfer")
@jwt_required()
@limiter.limit("20 per minute")  # protects against retry storms / abuse
def transfer():
    """
    Transfers `amount` from the logged-in user to `receiver_username`.

    Requires an `Idempotency-Key` header. If the same key is sent again
    (e.g. the client retried after a timeout), we return the original
    result instead of transferring the money a second time.
    """
    idem_key = request.headers.get("Idempotency-Key")
    if not idem_key:
        return jsonify(error="Idempotency-Key header is required"), 400

    # --- Idempotency check: have we already processed this exact request? ---
    existing = IdempotencyKey.query.get(idem_key)
    if existing:
        txn = db.session.get(Transaction, existing.transaction_id)
        return jsonify(txn.to_dict()), 200

    data = request.get_json(force=True)
    receiver_username = data.get("receiver_username")
    amount = data.get("amount")
    sender_id = int(get_jwt_identity())

    if not isinstance(amount, int) or amount <= 0:
        return jsonify(error="amount must be a positive integer (paise)"), 400

    receiver = User.query.filter_by(username=receiver_username).first()
    if not receiver:
        return jsonify(error="receiver not found"), 404
    if receiver.id == sender_id:
        return jsonify(error="cannot transfer to yourself"), 400

    # --- Atomic section: lock both rows so concurrent transfers can't race ---
    # SELECT ... FOR UPDATE blocks other transactions from reading/writing
    # these rows until we commit or rollback. This prevents the classic
    # "two simultaneous transfers both read balance=100, both proceed"
    # race condition. Always lock in a consistent order (lower id first)
    # to avoid deadlocks between two transfers going opposite directions.
    ids_in_order = sorted([sender_id, receiver.id])
    locked_users = {
        u.id: u for u in User.query
            .filter(User.id.in_(ids_in_order))
            .with_for_update()
            .all()
    }
    sender = locked_users[sender_id]
    receiver = locked_users[receiver.id]

    if sender.balance < amount:
        txn = Transaction(
            sender_id=sender.id, receiver_id=receiver.id, amount=amount,
            status=TransactionStatus.FAILED, idempotency_key=idem_key,
        )
        db.session.add(txn)
        db.session.commit()
        return jsonify(error="insufficient funds", transaction=txn.to_dict()), 402

    status = _fraud_check(sender.id, amount)

    if status == TransactionStatus.SUCCESS:
        sender.balance -= amount
        receiver.balance += amount
        sender.version += 1
        receiver.version += 1
    # If flagged, we record the transaction but do NOT move funds yet -
    # in a real system this would go to a review queue.

    txn = Transaction(
        sender_id=sender.id, receiver_id=receiver.id, amount=amount,
        status=status, idempotency_key=idem_key,
    )
    db.session.add(txn)
    db.session.flush()  # get txn.id before commit

    db.session.add(IdempotencyKey(key=idem_key, transaction_id=txn.id))
    db.session.commit()

    status_code = 201 if status == TransactionStatus.SUCCESS else 202
    return jsonify(txn.to_dict()), status_code


def _fraud_check(sender_id: int, amount: int) -> TransactionStatus:
    """
    Very simple placeholder fraud rules:
    1. Amount above threshold -> flag.
    2. Too many transfers from this sender in the last N minutes -> flag.

    In a real system this would be a separate service/ruleset, possibly
    ML-driven, but this shows the shape of the idea.
    """
    if amount >= FRAUD_AMOUNT_THRESHOLD:
        return TransactionStatus.FLAGGED

    window_start = datetime.utcnow() - timedelta(minutes=FRAUD_VELOCITY_WINDOW_MIN)
    recent_count = Transaction.query.filter(
        Transaction.sender_id == sender_id,
        Transaction.created_at >= window_start,
    ).count()

    if recent_count >= FRAUD_VELOCITY_MAX_COUNT:
        return TransactionStatus.FLAGGED

    return TransactionStatus.SUCCESS


# ---- Local dev entrypoint ----------------------------------------------

if __name__ == "__main__":
    app.run(debug=True)
