"""
models.py
Database models for PayFlow.

Design notes (things worth explaining in an interview):
- Balance is stored as Integer (paise/cents), never Float. Floats cause rounding
  errors in money math — this is a classic real-world payments gotcha.
- Transaction rows are immutable (append-only ledger). We never edit balances
  directly without a matching transaction row, so we always have an audit trail.
- IdempotencyKey table prevents duplicate transfers if a client retries a
  request (e.g. due to a network timeout) after the server already processed it.
"""

from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import enum

db = SQLAlchemy()


class TransactionStatus(str, enum.Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    FLAGGED = "FLAGGED"  # held for manual review (fraud check)


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    # Stored in smallest currency unit (paise) to avoid float rounding errors.
    balance = db.Column(db.Integer, nullable=False, default=0)

    # Optimistic locking: every update increments this. If two requests read
    # the same version and both try to write, the second one fails and retries.
    version = db.Column(db.Integer, nullable=False, default=0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<User {self.username} balance={self.balance}>"


class Transaction(db.Model):
    __tablename__ = "transactions"

    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    receiver_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    amount = db.Column(db.Integer, nullable=False)  # paise
    status = db.Column(db.Enum(TransactionStatus), nullable=False)
    idempotency_key = db.Column(db.String(120), unique=True, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "sender_id": self.sender_id,
            "receiver_id": self.receiver_id,
            "amount": self.amount,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
        }


class IdempotencyKey(db.Model):
    """
    Stores every idempotency key we've seen, mapped to the transaction it
    produced. If a client retries the same request (same key), we return the
    original result instead of processing the transfer again.
    """
    __tablename__ = "idempotency_keys"

    key = db.Column(db.String(120), primary_key=True)
    transaction_id = db.Column(db.Integer, db.ForeignKey("transactions.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
