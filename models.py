"""SQLite data layer + the status machine (single source of truth for the workflow)."""
import sqlite3
from datetime import datetime

from flask import current_app, g

STATUSES = ["UPLOADED", "LINKED", "AUTHORIZED", "NOTIFIED", "DELIVERED"]

STATUS_LABELS = {
    "UPLOADED": "Subido",
    "LINKED": "Vinculado a paciente",
    "AUTHORIZED": "Revisado y autorizado",
    "NOTIFIED": "Paciente notificada",
    "DELIVERED": "Entregado",
}

ROLE_LABELS = {
    "physician": "Médica/o",
    "patient": "Paciente",
    "system": "Sistema",
}

AFFILIATIONS = ["INSABI", "IMSS", "ISSSTE", "PEMEX", "SEDENA", "SEMAR", "OTHER", "NONE"]

# (from_status, to_status) -> (roles allowed, audit action)
TRANSITIONS = {
    ("UPLOADED", "LINKED"): ({"physician"}, "Vinculado a paciente (identidad verificada)"),
    ("LINKED", "AUTHORIZED"): ({"physician"}, "Revisado y autorizado"),
    ("LINKED", "UPLOADED"): ({"physician"}, "Rechazado en revisión"),
    ("AUTHORIZED", "NOTIFIED"): ({"system"}, "Notificación encolada"),
    ("NOTIFIED", "DELIVERED"): ({"physician", "patient"}, "Resultado entregado"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('physician')),
    password_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS patients (
    id_eira TEXT PRIMARY KEY,
    full_name TEXT NOT NULL,
    national_id TEXT UNIQUE NOT NULL,
    birth_date TEXT NOT NULL,            -- YYYY-MM-DD
    phone_1 TEXT NOT NULL CHECK (trim(phone_1) <> ''),
    phone_2 TEXT,
    address_1 TEXT,
    address_2 TEXT,
    email TEXT,
    affiliation TEXT NOT NULL CHECK (affiliation IN
        ('INSABI', 'IMSS', 'ISSSTE', 'PEMEX', 'SEDENA', 'SEMAR', 'OTHER', 'NONE')),
    affiliation_number TEXT
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY,
    id_eira TEXT REFERENCES patients(id_eira),   -- empty until linked
    original_filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,                   -- encrypted file name inside storage/
    status TEXT NOT NULL,
    uploaded_by INTEGER REFERENCES users(id),
    pickup_code_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,                    -- timestamp
    actor TEXT NOT NULL,                 -- 'physician:<username>' | 'patient:<id_eira>' | 'system:...'
    result_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT
);

-- The audit log is append-only: the database itself refuses edits and deletes.
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    result_id INTEGER REFERENCES results(id),
    channel TEXT NOT NULL,               -- 'sms' | 'email'
    recipient TEXT NOT NULL,
    body TEXT NOT NULL,                  -- never contains clinical information or the patient's name
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    sent_at TEXT
);
"""


class TransitionError(Exception):
    pass


def now():
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def init_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def log(db, actor, result_id, action, detail=""):
    db.execute(
        "INSERT INTO audit_log (ts, actor, result_id, action, detail) VALUES (?, ?, ?, ?, ?)",
        (now(), actor, result_id, action, detail),
    )
    db.commit()


def transition(db, result_id, new_status, actor_role, actor, detail="", **fields):
    """Move a result to a new status if the workflow and the role allow it.

    Every successful transition is written to the audit log. `fields` are extra
    columns to update (always passed by our own code, never from user input keys).
    """
    row = db.execute("SELECT status FROM results WHERE id = ?", (result_id,)).fetchone()
    if row is None:
        raise TransitionError("El resultado no existe.")
    rule = TRANSITIONS.get((row["status"], new_status))
    if rule is None:
        raise TransitionError(
            "Paso no permitido: {} → {}.".format(
                STATUS_LABELS.get(row["status"], row["status"]),
                STATUS_LABELS.get(new_status, new_status),
            )
        )
    roles, action = rule
    if actor_role not in roles:
        raise TransitionError("El rol {} no puede realizar: {}.".format(ROLE_LABELS.get(actor_role, actor_role), action))

    updates = dict(fields, status=new_status, updated_at=now())
    columns = ", ".join("{} = ?".format(col) for col in updates)
    db.execute("UPDATE results SET {} WHERE id = ?".format(columns), (*updates.values(), result_id))
    log(db, actor, result_id, action, detail)  # commits
