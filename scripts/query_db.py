"""Direct DB query script for Fly.io console."""
import sqlite3

DB = '/app/dba.db'
conn = sqlite3.connect(DB)
cur = conn.cursor()

# List tables
cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
tables = [r[0] for r in cur.fetchall()]
print("TABLES:", tables)

conn.close()
