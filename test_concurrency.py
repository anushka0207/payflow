"""
test_concurrency.py

Fires multiple simultaneous transfer requests from the same sender to prove
the API doesn't let a balance go negative or process more money than
actually exists in the account (the exact race condition row-locking
prevents).

Usage:
    1. Run the Flask app in one terminal: flask --app app run
    2. Register two users (alice, bob) and log alice in to get a token.
    3. Deposit e.g. 1000 (paise) into alice's account.
    4. Fill in ACCESS_TOKEN below and run: python test_concurrency.py

Expected result: only as many transfers succeed as the balance allows -
the rest correctly fail with "insufficient funds", and the final balance
never goes negative. Try commenting out `.with_for_update()` in app.py and
re-running this to SEE the race condition happen (educational, don't
ship without the lock!).
"""

import threading
import requests
import uuid

BASE_URL = "http://localhost:5000"
ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJmcmVzaCI6ZmFsc2UsImlhdCI6MTc4ODQ3MjM5NiwianRpIjoiYTg4MThhYjQtOTk4MC00OTZiLThmZWQtZmExN2VlNmY0NGJkIiwidHlwZSI6ImFjY2VzcyIsInN1YiI6IjEiLCJuYmYiOjE3ODg0NzIzOTYsImNzcmYiOiI3MmI0OGUxNi0zMTU3LTRiMmYtYTJkNS0zOGM4NWFlMjY5YTEiLCJleHAiOjE3ODg0NzMyOTZ9.KFAzm6f3qyaDHCSJZO05GNceVN-wdweq7WdbMNaPh3s"
NUM_CONCURRENT_REQUESTS = 10
TRANSFER_AMOUNT = 200  # paise, per request

results = []
lock = threading.Lock()


def do_transfer():
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Idempotency-Key": str(uuid.uuid4()),  # unique per request on purpose
    }
    resp = requests.post(
        f"{BASE_URL}/transfer",
        json={"receiver_username": "bob", "amount": TRANSFER_AMOUNT},
        headers=headers,
    )
    with lock:
        results.append((resp.status_code, resp.json()))


def main():
    threads = [threading.Thread(target=do_transfer) for _ in range(NUM_CONCURRENT_REQUESTS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if r[0] == 201]
    failures = [r for r in results if r[0] == 402]

    print(f"Fired {NUM_CONCURRENT_REQUESTS} concurrent transfers of {TRANSFER_AMOUNT} paise each.")
    print(f"Succeeded: {len(successes)} | Failed (insufficient funds): {len(failures)}")
    print("Check alice's final /balance - it should never go negative.")


if __name__ == "__main__":
    main()
