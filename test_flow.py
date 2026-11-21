"""End-to-end smoke test of the critical workflow.

    python -m pytest -q test_flow.py     (or)     python test_flow.py
Uses a temporary database and storage folder; never touches app.db.
"""
import os
import re
import sqlite3
import tempfile
import time

_tmp = tempfile.mkdtemp()
os.environ["EIRA_DB"] = os.path.join(_tmp, "test.db")
os.environ["EIRA_STORAGE"] = os.path.join(_tmp, "storage")
os.environ["EIRA_TOKEN_SECONDS"] = "1"

import seed  # noqa: E402  (must come after the env vars)

seed.main()

from app import app  # noqa: E402

SAMPLE = os.path.join(seed.SAMPLE_DIR, "resultado_maria.pdf")
MARIA = seed.PATIENTS[0]
ROSA = seed.PATIENTS[2]


def physician():
    c = app.test_client()
    r = c.post("/login", data={"username": "medica", "password": seed.DEMO_PASSWORD})
    assert r.status_code == 302
    return c


def upload(c):
    with open(SAMPLE, "rb") as fh:
        r = c.post("/upload", data={"file": (fh, "resultado_maria.pdf")}, content_type="multipart/form-data")
    return int(r.headers["Location"].rsplit("/", 1)[1])


def status(rid):
    with sqlite3.connect(os.environ["EIRA_DB"]) as db:
        return db.execute("SELECT status FROM results WHERE id = ?", (rid,)).fetchone()[0]


def link_and_authorize(c, rid, patient):
    c.post("/results/{}/link".format(rid), data={"national_id": patient["national_id"], "birth_date": patient["birth_date"]})
    assert status(rid) == "LINKED"
    c.post("/results/{}/authorize".format(rid),
           data={"decision": "approve", "right_patient": "on", "legible": "on", "clinical_ok": "on"})
    assert status(rid) == "NOTIFIED"


def latest_code(rid):
    with sqlite3.connect(os.environ["EIRA_DB"]) as db:
        body = db.execute("SELECT body FROM outbox WHERE result_id = ? ORDER BY id DESC", (rid,)).fetchone()[0]
    return re.search(r"\b(\d{6})\b", body).group(1)


def test_portal_flow():
    c = physician()
    rid = upload(c)
    assert status(rid) == "UPLOADED"

    # Stored file is encrypted, not a readable PDF
    with sqlite3.connect(os.environ["EIRA_DB"]) as db:
        stored = db.execute("SELECT stored_path FROM results WHERE id = ?", (rid,)).fetchone()[0]
    with open(os.path.join(os.environ["EIRA_STORAGE"], stored), "rb") as fh:
        assert not fh.read().startswith(b"%PDF")

    # Can't authorize before linking
    c.post("/results/{}/authorize".format(rid), data={"decision": "approve", "right_patient": "on", "legible": "on", "clinical_ok": "on"})
    assert status(rid) == "UPLOADED"

    # Wrong birth date fails identity check
    c.post("/results/{}/link".format(rid), data={"national_id": MARIA["national_id"], "birth_date": "2000-01-01"})
    assert status(rid) == "UPLOADED"

    # Incomplete checklist is refused
    c.post("/results/{}/link".format(rid), data={"national_id": MARIA["national_id"], "birth_date": MARIA["birth_date"]})
    c.post("/results/{}/authorize".format(rid), data={"decision": "approve", "legible": "on"})
    assert status(rid) == "LINKED"
    c.post("/results/{}/authorize".format(rid), data={"decision": "approve", "right_patient": "on", "legible": "on", "clinical_ok": "on"})
    assert status(rid) == "NOTIFIED"

    # SMS has no name and no clinical content
    with sqlite3.connect(os.environ["EIRA_DB"]) as db:
        bodies = [b for (b,) in db.execute("SELECT body FROM outbox WHERE result_id = ?", (rid,))]
    assert bodies and all("Maria" not in b and "Papanicolaou" not in b for b in bodies)

    # Patient logs in and downloads
    p = app.test_client()
    assert p.post("/patient/login", data={"national_id": MARIA["national_id"], "code": "000000"}).status_code == 200  # wrong code
    assert p.post("/patient/login", data={"national_id": MARIA["national_id"], "code": latest_code(rid)}).status_code == 302
    link = re.search(rb'href="(/download/[^"]+)"', p.get("/patient").data).group(1).decode()
    r = p.get(link)
    assert r.status_code == 200 and r.data.startswith(b"%PDF")
    assert status(rid) == "DELIVERED"

    # Link expires
    time.sleep(2.1)
    assert p.get(link).status_code == 302

    # Another patient can't use it
    other = app.test_client()
    with other.session_transaction() as s:
        s.update(role="patient", id_eira=ROSA["id_eira"], actor_id=ROSA["id_eira"], name="x")
    fresh = re.search(rb'href="(/download/[^"]+)"', p.get("/patient").data).group(1).decode()
    assert other.get(fresh).status_code == 403

    # Patients can't reach staff pages
    assert p.get("/dashboard").status_code == 403


def test_assisted_flow():
    c = physician()
    rid = upload(c)
    link_and_authorize(c, rid, ROSA)
    assert b"Aviso para promotora" in c.get("/results/{}/notice".format(rid)).data
    c.post("/results/{}/assisted".format(rid), data={"national_id": ROSA["national_id"], "birth_date": "1990-01-01"})
    assert status(rid) == "NOTIFIED"
    c.post("/results/{}/assisted".format(rid), data={"national_id": ROSA["national_id"], "birth_date": ROSA["birth_date"]})
    assert status(rid) == "DELIVERED"
    assert c.get("/export.csv").status_code == 200


def test_database_rules():
    with sqlite3.connect(os.environ["EIRA_DB"]) as db:
        for bad in [("X1", "AAA", "", "IMSS"), ("X2", "BBB", "55 1", "FAKE")]:  # no phone_1 / bad affiliation
            try:
                db.execute("INSERT INTO patients (id_eira, full_name, national_id, birth_date, phone_1, affiliation)"
                           " VALUES (?, 'x', ?, '2000-01-01', ?, ?)", bad)
                raise AssertionError("should have failed: {}".format(bad))
            except sqlite3.IntegrityError:
                pass
        try:
            db.execute("DELETE FROM audit_log")
            raise AssertionError("audit_log should be append-only")
        except sqlite3.IntegrityError:
            pass


if __name__ == "__main__":
    test_portal_flow()
    test_assisted_flow()
    test_database_rules()
    print("OK: all flows passed")
