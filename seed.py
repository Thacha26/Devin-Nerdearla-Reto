"""Reset the database and load fictitious demo data.

    python seed.py

Creates physician users, three fictitious patients and sample PDFs in sample/.
WARNING: deletes app.db and every encrypted PDF in storage/.
"""
import glob
import json
import os
import sqlite3

from werkzeug.security import generate_password_hash

from models import init_db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("EIRA_DB", os.path.join(BASE_DIR, "app.db"))
STORAGE_DIR = os.environ.get("EIRA_STORAGE", os.path.join(BASE_DIR, "storage"))
SAMPLE_DIR = os.path.join(BASE_DIR, "sample")

DEMO_PASSWORD = "demo1234"

USERS = [
    ("medica", "Dra. Ficticia Uno"),
    ("medico", "Dr. Ficticio Dos"),
]

# Every patient has phone_1 (required). Patient 3's phone_1 is a neighbor's phone:
# she is the demo case for the health-promoter notice + assisted delivery.
PATIENTS = [
    {
        "id_eira": "EIRA-0001", "full_name": "Maria Perez Lopez", "national_id": "PELM850412MDFRPR01",
        "birth_date": "1985-04-12", "phone_1": "55 5555 0001", "phone_2": None,
        "address_1": "Calle Ficticia 12, Col. Centro", "address_2": None, "email": None,
        "affiliation": "IMSS", "affiliation_number": "12345678901",
    },
    {
        "id_eira": "EIRA-0002", "full_name": "Ana Gomez Ruiz", "national_id": "GORA790930MDFMZN02",
        "birth_date": "1979-09-30", "phone_1": "55 5555 0002", "phone_2": "55 5555 0102",
        "address_1": "Av. Inventada 300", "address_2": "Depto 4B", "email": "ana.gomez@example.org",
        "affiliation": "ISSSTE", "affiliation_number": "ISS-998877",
    },
    {
        "id_eira": "EIRA-0003", "full_name": "Rosa Quispe Mamani", "national_id": "QUMR920105MDFSSS03",
        "birth_date": "1992-01-05", "phone_1": "55 5555 0003 (vecina)", "phone_2": None,
        "address_1": "Paraje Los Pinos s/n, Localidad Ejemplo", "address_2": "Casa azul junto a la escuela",
        "email": None, "affiliation": "NONE", "affiliation_number": None,
    },
]


PROFILES_PATH = os.path.join(BASE_DIR, "data", "cytology_profiles.json")
SPANISH_PATH = os.path.join(BASE_DIR, "data", "cytology_form_es.json")


def load_spanish(form):
    """Spanish labels/options for the PDFs; every field and option must be translated."""
    with open(SPANISH_PATH, encoding="utf-8") as fh:
        es = json.load(fh)
    for spec in form:
        tr = es["fields"].get(spec["key"])
        if tr is None:
            raise ValueError("Missing Spanish label for " + spec["key"])
        missing = [o for o in spec.get("options", []) if o not in tr.get("options", {})]
        if missing:
            raise ValueError("Missing Spanish options for {}: {}".format(spec["key"], missing))
    return es


def load_profiles():
    """Load the cytology profiles and check them against the form skeleton and PATIENTS."""
    with open(PROFILES_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    form = {f["key"]: f for f in data["form"]}
    patients = {p["id_eira"]: p for p in PATIENTS}
    for prof in data["profiles"]:
        values = prof["values"]
        name = prof["profile"]
        if set(values) != set(form):
            raise ValueError("{}: fields differ from the form: {}".format(name, set(values) ^ set(form)))
        for key, value in values.items():
            spec = form[key]
            if value is None:
                if not spec.get("nullable"):
                    raise ValueError("{}: {} is required".format(name, key))
            elif spec["type"] == "option" and value not in spec["options"]:
                raise ValueError("{}: {} has invalid option {!r}".format(name, key, value))
            elif spec["type"] == "option_list" and any(v not in spec["options"] for v in value):
                raise ValueError("{}: {} has an invalid option".format(name, key))
        p = patients.get(values["2_id_eira"])
        if p is None or (p["full_name"], p["national_id"], p["birth_date"], p["affiliation"]) != (
                values["3_full_name"], values["4_national_id"], values["5_birth_date"], values["7_affiliation"]):
            raise ValueError("{}: identification does not match the patient in seed.py".format(name))
    return data


def profile_lines(form, values, es):
    """Report lines in Spanish. Profile values stay in English in the JSON and are translated here."""
    lines = [es["title"], ""]
    for spec in form:
        if spec["field"] in es["sections"]:
            lines += ["", es["sections"][spec["field"]]]
        tr = es["fields"][spec["key"]]
        options = tr.get("options", {})
        value = values[spec["key"]]
        if isinstance(value, list):
            value = "; ".join(options[v] for v in value) or es["none"]
        elif value is None:
            value = "-"
        else:
            value = options.get(value, value)
        lines.append("{}. {}: {}".format(spec["field"], tr["label"], value))
    lines += ["", es["footer"]]
    return lines


def make_pdf(lines):
    """Tiny valid one-page PDF, no external libraries. Text is encoded as Windows-1252
    (WinAnsiEncoding) so Spanish accents and ñ display correctly."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    text = " ".join("({}) Tj T*".format(esc(line)) for line in lines)
    stream = "BT /F1 10 Tf 50 760 Td 13 TL {} ET".format(text)
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        " /Resources << /Font << /F1 5 0 R >> >> >>",
        "<< /Length {} >>\nstream\n{}\nendstream".format(len(stream), stream),
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += "{} 0 obj\n{}\nendobj\n".format(i, obj).encode("cp1252")
    xref = len(out)
    out += "xref\n0 {}\n0000000000 65535 f \n".format(len(objects) + 1).encode()
    for off in offsets:
        out += "{:010d} 00000 n \n".format(off).encode()
    out += "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{}\n%%EOF\n".format(len(objects) + 1, xref).encode()
    return out


def write_samples():
    """One sample PDF per cytology profile, named after the patient's first name."""
    data = load_profiles()
    es = load_spanish(data["form"])
    os.makedirs(SAMPLE_DIR, exist_ok=True)
    for prof in data["profiles"]:
        values = prof["values"]
        pdf = make_pdf(profile_lines(data["form"], values, es))
        name = "resultado_{}.pdf".format(values["3_full_name"].split()[0].lower())
        with open(os.path.join(SAMPLE_DIR, name), "wb") as fh:
            fh.write(pdf)


def populate(db_path):
    """Create tables and insert demo physicians and patients (no file changes)."""
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    pw_hash = generate_password_hash(DEMO_PASSWORD)
    conn.executemany(
        "INSERT INTO users (username, full_name, role, password_hash) VALUES (?, ?, 'physician', ?)",
        [(u, n, pw_hash) for u, n in USERS],
    )
    columns = list(PATIENTS[0])
    conn.executemany(
        "INSERT INTO patients ({}) VALUES ({})".format(", ".join(columns), ", ".join(":" + c for c in columns)),
        PATIENTS,
    )
    conn.commit()
    conn.close()


def main():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    os.makedirs(STORAGE_DIR, exist_ok=True)
    for path in glob.glob(os.path.join(STORAGE_DIR, "*.bin")):
        os.remove(path)

    populate(DB_PATH)
    write_samples()

    print("Base de datos lista:", DB_PATH)
    print("Médicos (contraseña '{}'): {}".format(DEMO_PASSWORD, ", ".join(u for u, _ in USERS)))
    print("PDFs de prueba en:", SAMPLE_DIR)


if __name__ == "__main__":
    main()
