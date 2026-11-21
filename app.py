"""EIRA – secure delivery of Pap smear results (hackathon prototype).

Run:  python seed.py && flask --app app run --host 0.0.0.0
Roles: physician (all staff steps) and patient (portal).
"""
import csv
import io
import os
import sqlite3
from datetime import timedelta

from flask import Flask, Response, abort, flash, redirect, render_template, request, send_file, session, url_for
from itsdangerous import BadSignature, SignatureExpired
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import notify
import security
from models import AFFILIATIONS, ROLE_LABELS, STATUS_LABELS, STATUSES, TransitionError, close_db, get_db, init_db, log, now, transition
from security import actor, role_required

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config.update(
    DATABASE=os.environ.get("EIRA_DB", os.path.join(BASE_DIR, "app.db")),
    STORAGE_DIR=os.environ.get("EIRA_STORAGE", os.path.join(BASE_DIR, "storage")),
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,  # 10 MB per PDF
    DOWNLOAD_TOKEN_MAX_AGE=int(os.environ.get("EIRA_TOKEN_SECONDS", 300)),  # 5 minutes
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
os.makedirs(app.config["STORAGE_DIR"], exist_ok=True)
app.secret_key = security.load_or_create_secret(app.config["STORAGE_DIR"])
init_db(app.config["DATABASE"])  # CREATE TABLE IF NOT EXISTS – safe on every start
app.teardown_appcontext(close_db)

REVIEW_CHECKLIST = [
    ("right_patient", "El nombre y CURP del PDF coinciden con la paciente vinculada"),
    ("legible", "El documento es legible y está completo"),
    ("clinical_ok", "Autorizo clínicamente la entrega a la paciente"),
]


@app.context_processor
def inject_labels():
    return {"STATUS_LABELS": STATUS_LABELS, "ROLE_LABELS": ROLE_LABELS, "STATUSES": STATUSES}


@app.after_request
def no_cache(resp):
    # Results must not stay in the browser cache of a shared computer.
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------- helpers ----------

def get_result(rid):
    row = get_db().execute("SELECT * FROM results WHERE id = ?", (rid,)).fetchone()
    if row is None:
        abort(404)
    return row


def get_patient(id_eira):
    if id_eira is None:
        return None
    return get_db().execute("SELECT * FROM patients WHERE id_eira = ?", (id_eira,)).fetchone()


def try_transition(rid, new_status, ok_message, **kwargs):
    try:
        transition(get_db(), rid, new_status, session["role"], actor(), **kwargs)
        flash(ok_message, "ok")
        return True
    except TransitionError as exc:
        flash(str(exc), "error")
        return False


def pdf_response(rid, data, as_attachment):
    return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=as_attachment,
                     download_name="resultado-{}.pdf".format(rid))


# ---------- login ----------

@app.route("/")
def index():
    targets = {"physician": "dashboard", "patient": "patient_home"}
    return redirect(url_for(targets.get(session.get("role"), "login")))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        db = get_db()
        username = request.form.get("username", "").strip()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.clear()
            session.permanent = True
            session.update(role=user["role"], user_id=user["id"], actor_id=user["username"], name=user["full_name"])
            log(db, actor(), None, "Inicio de sesión")
            return redirect(url_for("index"))
        log(db, "anon:" + username, None, "Inicio de sesión fallido")
        flash("Usuario o contraseña incorrectos.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    patient = session.get("role") == "patient"
    if session.get("role"):
        log(get_db(), actor(), None, "Cierre de sesión")
    session.clear()
    return redirect(url_for("patient_login" if patient else "login"))


# ---------- physician: upload, dashboard, workflow ----------

@app.route("/upload", methods=["GET", "POST"])
@role_required("physician")
def upload():
    if request.method == "POST":
        db = get_db()
        f = request.files.get("file")
        data = f.read() if f else b""
        if not f or not f.filename.lower().endswith(".pdf") or not data.startswith(b"%PDF"):
            flash("Seleccione un archivo PDF válido.", "error")
            return redirect(url_for("upload"))
        stored = security.save_encrypted(data)
        cur = db.execute(
            "INSERT INTO results (original_filename, stored_path, status, uploaded_by, created_at, updated_at)"
            " VALUES (?, ?, 'UPLOADED', ?, ?, ?)",
            (secure_filename(f.filename), stored, session["user_id"], now(), now()),
        )
        log(db, actor(), cur.lastrowid, "PDF subido y cifrado")
        flash("Resultado #{} subido. Ahora vincúlelo a la paciente.".format(cur.lastrowid), "ok")
        return redirect(url_for("result_detail", rid=cur.lastrowid))
    return render_template("upload.html")


@app.route("/dashboard")
@role_required("physician")
def dashboard():
    db = get_db()
    counts = {s: 0 for s in STATUSES}
    for row in db.execute("SELECT status, COUNT(*) AS n FROM results GROUP BY status"):
        counts[row["status"]] = row["n"]
    status = request.args.get("status")
    query = "SELECT r.*, p.full_name AS patient_name FROM results r LEFT JOIN patients p ON p.id_eira = r.id_eira"
    params = ()
    if status in STATUSES:
        query += " WHERE r.status = ?"
        params = (status,)
    results = db.execute(query + " ORDER BY r.updated_at DESC, r.id DESC", params).fetchall()
    return render_template("dashboard.html", counts=counts, results=results, current=status)


@app.route("/results/<int:rid>")
@role_required("physician")
def result_detail(rid):
    r = get_result(rid)
    audit = get_db().execute("SELECT * FROM audit_log WHERE result_id = ? ORDER BY id", (rid,)).fetchall()
    return render_template("result.html", r=r, patient=get_patient(r["id_eira"]), audit=audit,
                           checklist=REVIEW_CHECKLIST)


@app.route("/results/<int:rid>/pdf")
@role_required("physician")
def staff_pdf(rid):
    r = get_result(rid)
    log(get_db(), actor(), rid, "PDF visto por personal")
    return pdf_response(rid, security.load_decrypted(r["stored_path"]), as_attachment=False)


@app.route("/results/<int:rid>/link", methods=["POST"])
@role_required("physician")
def link_result(rid):
    db = get_db()
    national_id = request.form.get("national_id", "").strip()
    birth_date = request.form.get("birth_date", "").strip()
    patient = db.execute("SELECT * FROM patients WHERE national_id = ?", (national_id,)).fetchone()
    if patient is None or patient["birth_date"] != birth_date:
        log(db, actor(), rid, "Verificación de identidad fallida")
        flash("CURP y fecha de nacimiento no coinciden con ninguna paciente.", "error")
    else:
        try_transition(rid, "LINKED", "Vinculado a {}.".format(patient["full_name"]),
                       detail="id_eira " + patient["id_eira"], id_eira=patient["id_eira"])
    return redirect(url_for("result_detail", rid=rid))


@app.route("/results/<int:rid>/authorize", methods=["POST"])
@role_required("physician")
def authorize_result(rid):
    db = get_db()
    if request.form.get("decision") != "approve":
        reason = request.form.get("reason", "").strip() or "Sin motivo"
        try_transition(rid, "UPLOADED", "Rechazado: el resultado vuelve a vincularse.",
                       detail="Motivo: " + reason, id_eira=None)
        return redirect(url_for("result_detail", rid=rid))

    missing = [label for key, label in REVIEW_CHECKLIST if request.form.get(key) != "on"]
    if missing:
        flash("Complete la revisión: " + "; ".join(missing), "error")
        return redirect(url_for("result_detail", rid=rid))

    code = security.generate_pickup_code()
    if try_transition(rid, "AUTHORIZED", "Autorizado. Notificaciones encoladas.",
                      detail="Checklist de revisión completo", pickup_code_hash=generate_password_hash(code)):
        patient = get_patient(get_result(rid)["id_eira"])
        channels = notify.queue_notification(db, rid, patient, code)
        transition(db, rid, "NOTIFIED", "system", "system:notificaciones", detail=channels)
    return redirect(url_for("result_detail", rid=rid))


@app.route("/results/<int:rid>/notice")
@role_required("physician")
def print_notice(rid):
    """Printable notice for the health promoter (no diagnosis, no pickup code)."""
    r = get_result(rid)
    if r["status"] != "NOTIFIED":
        flash("Solo se imprime aviso para resultados notificados y aún no entregados.", "error")
        return redirect(url_for("result_detail", rid=rid))
    log(get_db(), actor(), rid, "Aviso impreso para promotora de salud")
    return render_template("notice.html", r=r, patient=get_patient(r["id_eira"]), today=now()[:10])


@app.route("/results/<int:rid>/assisted", methods=["POST"])
@role_required("physician")
def assisted_delivery(rid):
    """In-person delivery: ID card is checked physically, so DNI + birth date are required;
    the pickup code is optional (patients may not have received the SMS), but must match if given."""
    db = get_db()
    r = get_result(rid)
    patient = get_patient(r["id_eira"])
    national_id = request.form.get("national_id", "").strip()
    birth_date = request.form.get("birth_date", "").strip()
    code = request.form.get("code", "").strip()
    ok = (
        patient is not None
        and patient["national_id"] == national_id
        and patient["birth_date"] == birth_date
        and (not code or check_password_hash(r["pickup_code_hash"] or "", code))
    )
    if not ok:
        log(db, actor(), rid, "Entrega asistida: verificación fallida")
        flash("Los datos no coinciden. No entregue el resultado.", "error")
    else:
        detail = "Entrega asistida ({})".format("con código" if code else "sin código, identificación presentada")
        try_transition(rid, "DELIVERED", "Identidad verificada. Puede imprimir el resultado.", detail=detail)
    return redirect(url_for("result_detail", rid=rid))


# ---------- physicians ----------

MIN_PASSWORD_LENGTH = 8


@app.route("/physicians")
@role_required("physician")
def physicians():
    rows = get_db().execute("SELECT id, username, full_name FROM users ORDER BY full_name").fetchall()
    return render_template("physicians.html", physicians=rows)


@app.route("/physicians/new", methods=["GET", "POST"])
@role_required("physician")
def physician_new():
    form = {"username": "", "full_name": ""}
    errors = []
    if request.method == "POST":
        form = {"username": request.form.get("username", "").strip().lower(),
                "full_name": request.form.get("full_name", "").strip()}
        password = request.form.get("password", "")
        if not form["username"]:
            errors.append("Falta: Usuario")
        elif not form["username"].replace(".", "").replace("_", "").isalnum():
            errors.append("El usuario solo puede tener letras, números, punto o guion bajo.")
        if not form["full_name"]:
            errors.append("Falta: Nombre completo")
        if len(password) < MIN_PASSWORD_LENGTH:
            errors.append("La contraseña debe tener al menos {} caracteres.".format(MIN_PASSWORD_LENGTH))
        elif password != request.form.get("password_confirm", ""):
            errors.append("Las contraseñas no coinciden.")
        if not errors:
            db = get_db()
            try:
                db.execute(
                    "INSERT INTO users (username, full_name, role, password_hash) VALUES (?, ?, 'physician', ?)",
                    (form["username"], form["full_name"], generate_password_hash(password)),
                )
                db.commit()
            except sqlite3.IntegrityError:
                errors.append("Ya existe ese usuario.")
            else:
                log(db, actor(), None, "Médica/o registrada/o", "usuario " + form["username"])
                flash("Cuenta de {} creada. Comparta la contraseña en persona.".format(form["full_name"]), "ok")
                return redirect(url_for("physicians"))
    return render_template("physician_form.html", form=form, errors=errors, min_length=MIN_PASSWORD_LENGTH)


# ---------- patients ----------

PATIENT_FIELDS = ["id_eira", "full_name", "national_id", "birth_date", "phone_1", "phone_2",
                  "address_1", "address_2", "email", "affiliation", "affiliation_number"]
REQUIRED_PATIENT_FIELDS = {
    "id_eira": "ID EIRA", "full_name": "Nombre completo", "national_id": "CURP",
    "birth_date": "Fecha de nacimiento", "phone_1": "Teléfono 1", "affiliation": "Afiliación",
}


@app.route("/patients")
@role_required("physician")
def patients():
    q = request.args.get("q", "").strip()
    query = "SELECT p.*, (SELECT COUNT(*) FROM results r WHERE r.id_eira = p.id_eira) AS n_results FROM patients p"
    params = ()
    if q:
        query += " WHERE p.full_name LIKE ? OR p.national_id LIKE ? OR p.id_eira LIKE ?"
        params = ("%{}%".format(q),) * 3
    rows = get_db().execute(query + " ORDER BY p.full_name", params).fetchall()
    return render_template("patients.html", patients=rows, q=q)


def read_patient_form():
    """Read and validate the patient form. Returns (form, errors)."""
    form = {f: request.form.get(f, "").strip() for f in PATIENT_FIELDS}
    form["national_id"] = form["national_id"].upper()
    form["id_eira"] = form["id_eira"].upper()
    errors = ["Falta: " + label for f, label in REQUIRED_PATIENT_FIELDS.items() if not form[f]]
    if form["affiliation"] and form["affiliation"] not in AFFILIATIONS:
        errors.append("Afiliación no válida.")
    if form["email"] and "@" not in form["email"]:
        errors.append("Correo no válido.")
    return form, errors


@app.route("/patients/<id_eira>/edit", methods=["GET", "POST"])
@role_required("physician")
def patient_edit(id_eira):
    db = get_db()
    patient = get_patient(id_eira)
    if patient is None:
        abort(404)
    form, errors = dict(patient), []
    if request.method == "POST":
        form, errors = read_patient_form()
        form["id_eira"] = patient["id_eira"]  # primary key, referenced by results: never changes
        if not errors:
            changed = [f for f in PATIENT_FIELDS if (form[f] or None) != patient[f]]
            if not changed:
                flash("No hubo cambios.", "ok")
                return redirect(url_for("patients"))
            editable = [f for f in PATIENT_FIELDS if f != "id_eira"]
            try:
                db.execute(
                    "UPDATE patients SET {} WHERE id_eira = :id_eira".format(", ".join("{0} = :{0}".format(f) for f in editable)),
                    {f: (form[f] or None) for f in PATIENT_FIELDS},
                )
                db.commit()
            except sqlite3.IntegrityError:
                errors.append("Ya existe otra paciente con esa CURP.")
            else:
                # Log which fields changed, not their values (data minimization).
                log(db, actor(), None, "Paciente editada", "id_eira {}: {}".format(id_eira, ", ".join(changed)))
                flash("Datos de {} actualizados.".format(form["full_name"]), "ok")
                return redirect(url_for("patients"))
    return render_template("patient_form.html", form=form, errors=errors, affiliations=AFFILIATIONS,
                           editing=True, next_result=None)


@app.route("/patients/new", methods=["GET", "POST"])
@role_required("physician")
def patient_new():
    form = {f: "" for f in PATIENT_FIELDS}
    errors = []
    if request.method == "POST":
        form, errors = read_patient_form()
        if not errors:
            db = get_db()
            values = {f: (form[f] or None) for f in PATIENT_FIELDS}
            try:
                db.execute(
                    "INSERT INTO patients ({}) VALUES ({})".format(
                        ", ".join(PATIENT_FIELDS), ", ".join(":" + f for f in PATIENT_FIELDS)),
                    values,
                )
                db.commit()
            except sqlite3.IntegrityError:
                errors.append("Ya existe una paciente con ese ID EIRA o esa CURP.")
            else:
                log(db, actor(), None, "Paciente registrada", "id_eira " + form["id_eira"])
                flash("Paciente {} registrada.".format(form["full_name"]), "ok")
                back = request.args.get("next_result", type=int)
                return redirect(url_for("result_detail", rid=back) if back else url_for("patients"))
    return render_template("patient_form.html", form=form, errors=errors, affiliations=AFFILIATIONS,
                           next_result=request.args.get("next_result", type=int))


# ---------- notifications, audit, export ----------

@app.route("/outbox")
@role_required("physician")
def outbox():
    messages = get_db().execute("SELECT * FROM outbox ORDER BY id DESC").fetchall()
    return render_template("outbox.html", messages=messages)


@app.route("/outbox/sync", methods=["POST"])
@role_required("physician")
def outbox_sync():
    db = get_db()
    n = notify.send_pending(db)
    log(db, actor(), None, "Sincronización de notificaciones", "{} mensajes enviados (simulado)".format(n))
    flash("{} mensajes enviados (simulado).".format(n), "ok")
    return redirect(url_for("outbox"))


@app.route("/audit")
@role_required("physician")
def audit():
    entries = get_db().execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 500").fetchall()
    return render_template("audit.html", entries=entries)


@app.route("/export.csv")
@role_required("physician")
def export_csv():
    db = get_db()
    rows = db.execute(
        "SELECT r.id, r.id_eira, p.affiliation, p.affiliation_number, r.status, r.created_at, r.updated_at"
        " FROM results r LEFT JOIN patients p ON p.id_eira = r.id_eira ORDER BY r.id"
    ).fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["result_id", "id_eira", "affiliation", "affiliation_number", "status", "created_at", "updated_at"])
    writer.writerows([tuple(row) for row in rows])
    log(db, actor(), None, "Exportación CSV", "{} filas".format(len(rows)))
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=eira-resultados.csv"})


# ---------- patient portal ----------

@app.route("/patient/login", methods=["GET", "POST"])
def patient_login():
    if request.method == "POST":
        db = get_db()
        national_id = request.form.get("national_id", "").strip()
        code = request.form.get("code", "").strip()
        patient = db.execute("SELECT * FROM patients WHERE national_id = ?", (national_id,)).fetchone()
        valid = False
        if patient:
            hashes = db.execute(
                "SELECT pickup_code_hash FROM results WHERE id_eira = ? AND status IN ('NOTIFIED', 'DELIVERED')",
                (patient["id_eira"],),
            ).fetchall()
            valid = any(h["pickup_code_hash"] and check_password_hash(h["pickup_code_hash"], code) for h in hashes)
        if valid:
            session.clear()
            session.permanent = True
            session.update(role="patient", id_eira=patient["id_eira"], actor_id=patient["id_eira"],
                           name=patient["full_name"])
            log(db, actor(), None, "Ingreso de paciente al portal")
            return redirect(url_for("patient_home"))
        log(db, "anon:paciente", None, "Ingreso de paciente fallido")
        flash("Identificación o código incorrectos.", "error")
    return render_template("patient_login.html")


@app.route("/patient")
@role_required("patient")
def patient_home():
    rows = get_db().execute(
        "SELECT id, status, updated_at FROM results WHERE id_eira = ? AND status IN ('NOTIFIED', 'DELIVERED')"
        " ORDER BY id DESC",
        (session["id_eira"],),
    ).fetchall()
    docs = [dict(row, token=security.make_download_token(row["id"], session["id_eira"])) for row in rows]
    return render_template("patient.html", docs=docs, minutes=app.config["DOWNLOAD_TOKEN_MAX_AGE"] // 60)


@app.route("/download/<token>")
@role_required("patient")
def patient_download(token):
    db = get_db()
    try:
        data = security.read_download_token(token)
    except SignatureExpired:
        flash("El enlace venció. Use el nuevo enlace de esta página.", "error")
        return redirect(url_for("patient_home"))
    except BadSignature:
        abort(403)
    r = get_result(data["r"])
    if data["p"] != session["id_eira"] or r["id_eira"] != session["id_eira"] \
            or r["status"] not in ("NOTIFIED", "DELIVERED"):
        log(db, actor(), data["r"], "Intento de acceso denegado")
        abort(403)
    if r["status"] == "NOTIFIED":
        transition(db, r["id"], "DELIVERED", "patient", actor(), detail="Descarga por portal")
    log(db, actor(), r["id"], "Descargado por paciente")
    return pdf_response(r["id"], security.load_decrypted(r["stored_path"]), as_attachment=True)


if __name__ == "__main__":
    app.run(debug=True)
