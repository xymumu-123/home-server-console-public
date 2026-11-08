# -*- coding: utf-8 -*-
"""SQLite 共享层: 连接、建表、KV 操作 (app.py 与各路由模块共用)"""
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "data.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_conn()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS samples (
               ts REAL PRIMARY KEY,
               cpu REAL, mem REAL,
               cpu_temp REAL, mb_temp REAL,
               cpu_power REAL, est_power REAL,
               net_up REAL, net_down REAL)"""
    )
    for name in ("r1m", "r15m", "r1h"):
        conn.execute(
            f"""CREATE TABLE IF NOT EXISTS {name} (
                   ts REAL PRIMARY KEY,
                   cpu REAL, mem REAL,
                   cpu_temp REAL, mb_temp REAL,
                   cpu_power REAL, est_power REAL,
                   net_up REAL, net_down REAL)"""
        )
    conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS watch_progress (
               path TEXT PRIMARY KEY,
               position REAL DEFAULT 0,
               duration REAL DEFAULT 0,
               updated_at REAL)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS shares (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               token TEXT UNIQUE,
               token_hash TEXT,
               path TEXT,
               label TEXT,
               created REAL,
               expires REAL,
               pw_hash TEXT,
               downloads INTEGER DEFAULT 0)"""
    )
    conn.commit()
    conn.close()


def kv_get(conn, key):
    row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return row[0] if row else None


def kv_set(conn, key, value):
    conn.execute(
        "INSERT INTO kv (k, v) VALUES (?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, str(value)),
    )
