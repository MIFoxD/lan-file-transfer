"""SQLite 数据访问层：schema 初始化 + users/files/downloads 查询辅助。"""

import os
import sqlite3
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
FILES_DIR = os.path.join(DATA_DIR, "files")
DB_PATH = os.path.join(DATA_DIR, "app.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  nickname   TEXT NOT NULL COLLATE NOCASE UNIQUE,
  token      TEXT NOT NULL UNIQUE,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
  id            TEXT PRIMARY KEY,
  uploader_id   INTEGER NOT NULL REFERENCES users(id),
  recipient_id  INTEGER NOT NULL REFERENCES users(id),
  original_name TEXT NOT NULL,
  stored_name   TEXT NOT NULL,
  size          INTEGER NOT NULL,
  mime          TEXT NOT NULL DEFAULT 'application/octet-stream',
  status        TEXT NOT NULL DEFAULT 'active',
  created_at    INTEGER NOT NULL,
  deleted_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_files_recipient ON files(recipient_id, status);
CREATE INDEX IF NOT EXISTS idx_files_uploader  ON files(uploader_id, status);

CREATE TABLE IF NOT EXISTS downloads (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  file_id       TEXT NOT NULL REFERENCES files(id),
  downloader_id INTEGER NOT NULL REFERENCES users(id),
  downloaded_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_downloads_file  ON downloads(file_id);
CREATE INDEX IF NOT EXISTS idx_downloads_dedup ON downloads(file_id, downloader_id, downloaded_at);
"""


def init_db():
    """创建数据目录并幂等初始化 schema，每次启动时调用。"""
    os.makedirs(FILES_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def connect():
    """每请求一个连接，配合 threaded 模式避免跨线程共享。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def now():
    return int(time.time())


# ---------- users ----------

def create_user(conn, nickname, token):
    cur = conn.execute(
        "INSERT INTO users (nickname, token, created_at) VALUES (?, ?, ?)",
        (nickname, token, now()),
    )
    conn.commit()
    return cur.lastrowid


def get_user_by_token(conn, token):
    return conn.execute("SELECT * FROM users WHERE token = ?", (token,)).fetchone()


def get_user_by_id(conn, user_id):
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def list_users(conn):
    return conn.execute("SELECT id, nickname, created_at FROM users ORDER BY nickname").fetchall()


# ---------- files ----------

def create_file(conn, file_id, uploader_id, recipient_id, original_name, stored_name, size, mime):
    conn.execute(
        """INSERT INTO files
           (id, uploader_id, recipient_id, original_name, stored_name, size, mime, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (file_id, uploader_id, recipient_id, original_name, stored_name, size, mime, now()),
    )
    conn.commit()


def get_file(conn, file_id):
    return conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()


def list_inbox(conn, user_id):
    """我收到的 active 文件（含我是否已下载），时间倒序。"""
    return conn.execute(
        """SELECT f.*, u.nickname AS uploader_nickname,
                  (SELECT MAX(d.downloaded_at) FROM downloads d
                    WHERE d.file_id = f.id AND d.downloader_id = ?) AS my_downloaded_at
           FROM files f JOIN users u ON u.id = f.uploader_id
           WHERE f.recipient_id = ? AND f.status = 'active'
           ORDER BY f.created_at DESC""",
        (user_id, user_id),
    ).fetchall()


def list_outbox(conn, user_id):
    """我发出的 active 文件，时间倒序。"""
    return conn.execute(
        """SELECT f.*, u.nickname AS recipient_nickname
           FROM files f JOIN users u ON u.id = f.recipient_id
           WHERE f.uploader_id = ? AND f.status = 'active'
           ORDER BY f.created_at DESC""",
        (user_id,),
    ).fetchall()


def soft_delete_file(conn, file_id):
    conn.execute(
        "UPDATE files SET status = 'deleted', deleted_at = ? WHERE id = ?",
        (now(), file_id),
    )
    conn.commit()


# ---------- downloads ----------

def record_download(conn, file_id, downloader_id):
    ts = now()
    conn.execute(
        "INSERT INTO downloads (file_id, downloader_id, downloaded_at) VALUES (?, ?, ?)",
        (file_id, downloader_id, ts),
    )
    conn.commit()
    return ts


def recent_download_exists(conn, file_id, downloader_id, within_seconds=60):
    """去重：60 秒内同一下载者的重复请求不重复通知。"""
    row = conn.execute(
        """SELECT 1 FROM downloads
           WHERE file_id = ? AND downloader_id = ? AND downloaded_at > ?
           LIMIT 1""",
        (file_id, downloader_id, now() - within_seconds),
    ).fetchone()
    return row is not None


def list_downloads_for_file(conn, file_id):
    return conn.execute(
        """SELECT d.downloaded_at, u.id AS downloader_id, u.nickname AS downloader_nickname
           FROM downloads d JOIN users u ON u.id = d.downloader_id
           WHERE d.file_id = ?
           ORDER BY d.downloaded_at DESC""",
        (file_id,),
    ).fetchall()
