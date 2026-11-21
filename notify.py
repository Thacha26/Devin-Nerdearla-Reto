"""Notification outbox. Messages are queued offline and 'sent' on sync.

SIMULATED: nothing leaves the machine. A real pilot plugs an SMS gateway and an
SMTP server into `send_pending`. Message bodies never include clinical content
or the patient's name (phone_1 may be a shared phone).
"""
from models import now

SMS_TEXT = "EIRA: hay un documento disponible. Codigo de retiro: {code}. Ingrese al portal o acerquese a su puesto de salud con su identificacion."
EMAIL_TEXT = "Hola. EIRA tiene un documento disponible para usted. Codigo de retiro: {code}. Ingrese al portal con su identificacion y este codigo, o acerquese a su puesto de salud."


def queue_notification(db, result_id, patient, code):
    """Always SMS to phone_1; also phone_2 and email when present. Returns channels used."""
    messages = [("sms", patient["phone_1"], SMS_TEXT)]
    if patient["phone_2"]:
        messages.append(("sms", patient["phone_2"], SMS_TEXT))
    if patient["email"]:
        messages.append(("email", patient["email"], EMAIL_TEXT))

    db.executemany(
        "INSERT INTO outbox (result_id, channel, recipient, body, created_at) VALUES (?, ?, ?, ?, ?)",
        [(result_id, channel, to, text.format(code=code), now()) for channel, to, text in messages],
    )
    db.commit()
    return ", ".join("{} {}".format(channel, to) for channel, to, _ in messages)


def send_pending(db):
    """Simulated sync: mark every pending message as sent. Returns how many."""
    cur = db.execute("UPDATE outbox SET status = 'sent', sent_at = ? WHERE status = 'pending'", (now(),))
    db.commit()
    return cur.rowcount
