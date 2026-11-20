"""Esquema SQLite (archivo, índice FTS, grafo y cola de revisión)."""

import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS editions (
  section TEXT NOT NULL, date TEXT NOT NULL,           -- date: YYYY-MM-DD
  status TEXT NOT NULL DEFAULT 'pending',              -- ok | missing | failed | pending
  attempts INTEGER NOT NULL DEFAULT 0,
  pdf_path TEXT, pdf_sha256 TEXT, n_notices INTEGER, last_error TEXT, checked_at TEXT,
  PRIMARY KEY (section, date)
);
CREATE TABLE IF NOT EXISTS notices (
  id TEXT PRIMARY KEY,                                 -- seccion/idBORA/AAAAMMDD
  section TEXT NOT NULL, bora_id TEXT NOT NULL, date TEXT NOT NULL,
  jurisdiction TEXT NOT NULL DEFAULT 'nacional',
  title TEXT, rubro TEXT, url TEXT NOT NULL,
  text TEXT, text_source TEXT,                         -- html | ocr | none
  raw_path TEXT, sha256 TEXT, duplicate_of TEXT,
  status TEXT NOT NULL DEFAULT 'listed',               -- listed | fetched | failed
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS notices_date ON notices(section, date);
CREATE INDEX IF NOT EXISTS notices_sha ON notices(sha256);
CREATE VIRTUAL TABLE IF NOT EXISTS notices_fts USING fts5(
  id UNINDEXED, title, text, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS searches (
  key TEXT PRIMARY KEY, params TEXT NOT NULL, n_results INTEGER, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS search_hits (
  key TEXT NOT NULL, notice_id TEXT NOT NULL, PRIMARY KEY (key, notice_id)
);
CREATE TABLE IF NOT EXISTS extractions (
  notice_id TEXT NOT NULL, kind TEXT NOT NULL, model TEXT, data TEXT NOT NULL, created_at TEXT,
  PRIMARY KEY (notice_id, kind)
);
CREATE TABLE IF NOT EXISTS organisms (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY,
  cuit TEXT UNIQUE, cuit_source TEXT,                  -- notice id where the CUIT was read
  name TEXT NOT NULL, key TEXT NOT NULL,
  objeto TEXT, objeto_tech INTEGER, objeto_source TEXT,
  origin TEXT,                                         -- award | partner_link
  depth INTEGER, expanded_at TEXT
);
CREATE INDEX IF NOT EXISTS companies_key ON companies(key);
CREATE TABLE IF NOT EXISTS company_names (
  company_id INTEGER NOT NULL, key TEXT NOT NULL, name TEXT NOT NULL, PRIMARY KEY (company_id, key)
);
CREATE TABLE IF NOT EXISTS awards (
  id INTEGER PRIMARY KEY,
  notice_id TEXT NOT NULL, organism_id INTEGER, company_id INTEGER,
  awardee_name TEXT, cuit_raw TEXT, cuit_valid INTEGER,
  procedure TEXT, act_date TEXT, object TEXT, amount TEXT,
  is_tech INTEGER, tech_reason TEXT,
  status TEXT NOT NULL,                                -- confirmed | pending_review | rejected
  evidence TEXT, evidence_score REAL,
  UNIQUE (notice_id, awardee_name)
);
CREATE TABLE IF NOT EXISTS persons (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL, key TEXT NOT NULL,
  dni TEXT UNIQUE                                      -- uso interno para cotejo; nunca sale por la API
);
CREATE INDEX IF NOT EXISTS persons_key ON persons(key);
CREATE TABLE IF NOT EXISTS roles (
  id INTEGER PRIMARY KEY,
  notice_id TEXT NOT NULL, company_id INTEGER NOT NULL,
  person_id INTEGER, candidate_person_id INTEGER,
  mention_name TEXT NOT NULL, mention_key TEXT NOT NULL,
  role TEXT, act_date TEXT,
  dni_status TEXT NOT NULL,                            -- ok | missing | illegible | inconsistent
  match TEXT NOT NULL,                                 -- dni | pending_review | confirmed | rejected | unmatched
  evidence TEXT, evidence_score REAL,
  UNIQUE (notice_id, company_id, mention_key, role)
);
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                                  -- name_match | dni_inconsistent | award_tech | cuit_missing
  ref_table TEXT NOT NULL, ref_id INTEGER NOT NULL,
  summary TEXT, status TEXT NOT NULL DEFAULT 'pending', decided_at TEXT, note TEXT,
  UNIQUE (kind, ref_table, ref_id)
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, kind TEXT, started_at TEXT, finished_at TEXT, summary TEXT
);
"""


def connect(path=None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=60, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    return con


@contextmanager
def tx(con: sqlite3.Connection):
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
