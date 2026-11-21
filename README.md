# Devin-Nerdearla-Reto: EIRA secure delivery of Pap smear results

Hackathon prototype (Challenge 4): an alternative to WhatsApp for reviewing, authorizing, notifying and delivering Pap smear results. It runs on one PC on the local network and works **without internet**. See [PLAN.md](PLAN.md) for the full plan.

## Run it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python seed.py                          # resets the DB and loads fictitious data
.venv/bin/flask --app app run --host 0.0.0.0      # http://localhost:5000
```

`--host 0.0.0.0` lets other computers or phones on the same network open it at `http://<this-PC-IP>:5000`.

Run the end-to-end test with `.venv/bin/python test_flow.py`.

## Demo accounts (fictitious)

| Who | How to log in |
|---|---|
| Physician | `/login`, user `medica` or `medico`, password `demo1234` |
| Patient | `/patient/login`, CURP + the 6-digit code shown in **Notificaciones** (the simulated SMS) |

Patients (in `seed.py`), all with `phone_1`:
- **EIRA-0001 Maria Perez** (`phone_1` only): portal download flow
- **EIRA-0002 Ana Gomez** (`phone_2` + email): shows SMS to two numbers plus an email
- **EIRA-0003 Rosa Quispe** (`phone_1` is a neighbor's phone): health promoter notice and assisted delivery

Sample PDFs are in `sample/`.

## Demo script

1. Physician: **Subir PDF** → `sample/resultado_maria.pdf`
2. Link it: try a wrong birth date (rejected), then CURP `PELM850412MDFRPR01` + `1985-04-12`
3. Tick the review checklist → **Autorizar y notificar**
4. **Notificaciones**: the SMS has a code but no name and no diagnosis → **Sincronizar**
5. Patient portal: CURP + code → download (the link expires in 5 minutes)
6. Repeat with `resultado_rosa.pdf` → **Imprimir aviso para promotora** → assisted delivery at the counter
7. **Panel**, **Registro** (audit log) and **Exportar CSV**

## Implemented vs simulated

| Implemented | Simulated / not included |
|---|---|
| Upload with PDF check; files **encrypted at rest** (Fernet) | SMS and email sending (the outbox shows what would be sent) |
| Patient registration, search and editing (required fields, 8 affiliations, no duplicate ID EIRA or CURP) | |
| Linking with identity check (CURP + birth date) | Real patient data (all fictitious) |
| Review checklist + clinical authorization; rejection with reason | HTTPS, hosting, key management (the key sits in `storage/`) |
| Workflow enforced in a single `transition()` function | Separate lab and EIRA-admin roles (only `physician` and `patient`) |
| Roles: `physician`, `patient`; 30-minute sessions | Rate limiting of login attempts |
| Patient portal with **expiring download links** (5 min) | |
| Health promoter notice + assisted delivery | |
| **Append-only audit log** (the database blocks edits and deletes) | |
| Dashboard by status, CSV export, `backup.sh` | |

## Files

```
app.py        routes (physician + patient portal)
models.py     SQLite schema, statuses, transition()
security.py   roles, encryption, download tokens, pickup codes
notify.py     outbox + simulated sender
seed.py       fictitious data + sample PDFs
test_flow.py  end-to-end test
templates/    pages (Spanish); static/style.css (local, no CDN)
backup.sh     copy of app.db + storage/ to backups/
```
