import sqlite3

conn = sqlite3.connect("betterwallet.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT id, registration_type, payment_status FROM cac_registrations WHERE registration_type = ?",
    ("trademark",)
).fetchall()

for r in rows:
    print(dict(r))
