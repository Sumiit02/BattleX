import os
import sqlite3

try:
    import psycopg2
except ImportError:
    psycopg2 = None

from werkzeug.security import generate_password_hash

DB_NAME = os.getenv("DB_NAME", "gamezone.db")
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)


def add_admin():
    username = os.getenv("ADMIN_USERNAME", "admin1")
    email = os.getenv("ADMIN_EMAIL", "admin1@example.com")
    password = os.getenv("ADMIN_PASSWORD", "V9!kQ2@xL7#pM4s")
    admin_code = os.getenv("ADMIN_CODE", "74829")
    phone = os.getenv("ADMIN_PHONE", "9876543210")
    password_hash = generate_password_hash(password)

    if DATABASE_URL.startswith("postgresql://"):
        if psycopg2 is None:
            raise RuntimeError("psycopg2 is required for PostgreSQL. Install dependencies first.")
        sslmode = os.getenv("PGSSLMODE", "require")
        conn = psycopg2.connect(DATABASE_URL, sslmode=sslmode)
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE username = %s AND role = 'admin'", (username,))
        cur.execute(
            """
            INSERT INTO users (username, email, password, role, game_id, phone, admin_code)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (username, email, password_hash, 'admin', None, phone, admin_code)
        )
        conn.commit()
        conn.close()
    else:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE username = ? AND role = 'admin'", (username,))
        cur.execute(
            """
            INSERT INTO users (username, email, password, role, game_id, phone, admin_code)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (username, email, password_hash, 'admin', None, phone, admin_code)
        )
        conn.commit()
        conn.close()

    print("Admin user reset and added successfully.")
    print(f"Username: {username}")
    print(f"Admin Code: {admin_code}")

if __name__ == "__main__":
    add_admin()