import sqlite3
from werkzeug.security import check_password_hash

DB_NAME = "gamezone.db"

def test_admin_login(username, password, admin_code):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username = ? AND role = 'admin'", (username,))
    user = cur.fetchone()
    conn.close()
    if not user:
        print("No such admin user found.")
        return False
    print(f"DB user: {user}")
    if check_password_hash(user[3], password) and (user[7] == admin_code):
        print("Admin login successful!")
        return True
    else:
        print("Admin login failed: wrong password or admin code.")
        return False

if __name__ == "__main__":
    # Try the credentials you provided
    test_admin_login('admin1', 'ADMINPASS123', 'ADMIN2025')