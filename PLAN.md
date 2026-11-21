# Plan: Secure delivery of Pap smear results (EIRA, Challenge 4): 3-hour hackathon, 2 people

## Context
Today EIRA receives printed Pap smear results from the lab, scans them and sends them over WhatsApp (300+ per month). When the WhatsApp account gets blocked, delivery stops. Many patients have **no mobile data, no personal phone, or poor connectivity**, so the solution can't depend on a smartphone app or a stable internet connection.
Goal: in 3 hours, show **one complete critical workflow working end to end**: upload PDF → link to patient with identity check → review + clinical authorization → notification without clinical content → authenticated access or assisted delivery → dashboard and audit log. Clearly separate what is *implemented* from what is *simulated*.

**Scope decision: only 2 roles, `physician` and `patient`.** The physician does every staff step (upload, link, review/authorize, assisted delivery). The lab and EIRA-admin roles from the challenge are left for the pilot (see "Real pilot needs").

---

## Recommended delivery model (patients without data or a phone)
Use several channels, not just an app:
1. **SMS to a basic phone** (works with no data and no smartphone). Always sent to `phone_1`, which every patient has (it may be a relative's or neighbor's phone), plus `phone_2` if there is one. Message is generic: *"EIRA: you have a document available. Code: 4821. Call/visit your health post."* No diagnosis and no patient name.
2. **Assisted pickup at the health post or EIRA office**: the physician looks up the patient, verifies identity (ID number + date of birth + pickup code), **prints** the result or shows it on screen, and records "assisted delivery" in the audit log.
3. **Community health promoter**: if the patient doesn't pick up the result (the SMS went to a shared phone, or was never read), a printed notice goes to the promoter, who uses `address_1` / `address_2` to find the patient. Delivery is recorded the same way as assisted pickup.
4. **Email / web portal**, only for patients who have access: login with ID number + one-time code; PDF download through a short-lived link (expires in minutes, requires a session, never a permanent public link).
5. **Offline-first operation**: the app runs on **one PC on EIRA's local network** (LAN), so the whole workflow keeps working without internet. Notifications wait in an *outbox* table and go out when there's a connection (that's the sync).

## Stack (fast, free, open source, runs on modest hardware)
| Layer | Choice | Why |
|---|---|---|
| Backend | **Python + Flask** (or FastAPI) | Quick to write, both of you can work in it |
| Database | **SQLite** (one file) | No server, easy backups, runs offline |
| UI | **Jinja server-side templates + Pico.css** (plus HTMX if there's time) | No JS build step; light pages for old PCs and phones |
| Encryption at rest | `cryptography` (Fernet) for the PDFs | Stored PDFs are encrypted on disk |
| Auth | Flask-Login sessions + roles; hashed passwords (werkzeug) | Role-based access control |
| Notifications | `outbox` table + simulated sender (log / console screen) | Real version: SMS gateway (Twilio or a local provider) + SMTP |
| Export | CSV/JSON endpoint | Interoperability in open formats |
| Backup | `backup.sh` script: copy of `app.db` + `storage/` | Operations |

No licensing costs; the only recurring cost in a real pilot would be SMS (pay per message).

**Decision: Flask over Supabase.** Supabase cloud needs internet, and self-hosting it (~10 Docker containers) is too heavy for 3 hours and modest hardware. Flask + SQLite runs fully offline on one laptop or LAN PC, which directly covers the connectivity requirement.

## Data model (keep it minimal)
**Roles:** `physician` (username + password) and `patient` (ID number + pickup code, no password).

- `users` (id, username, full_name, role: `physician`, password_hash): physicians only
- `patients`
  - `id_eira`: primary key, EIRA's own patient ID
  - `full_name`, `national_id`, `birth_date`
  - `phone_1` (**required**; may be a shared phone, e.g. a relative's or neighbor's), `phone_2` (optional)
  - `address_1`, `address_2`: for health promoter visits
  - `email` (optional)
  - `affiliation`: one of 8 options: `INSABI`, `IMSS`, `ISSSTE`, `PEMEX`, `SEDENA`, `SEMAR`, `OTHER`, `NONE` (enforced with a database check)
  - `affiliation_number`
- `results` (id, id_eira?, file_path_encrypted, status, uploaded_by, pickup_code_hash, timestamps)
  - `id_eira` stays empty until the result is linked to a patient
  - Who reviewed and authorized is **not** stored here; it lives in `audit_log`
- `audit_log` (id, ts, actor, result_id, action, detail): **append-only**
  - `ts` = timestamp (when it happened)
  - `actor` = who did it: a physician's username, or the patient's `id_eira`
- `outbox` (id, result_id, channel, to, body, status: pending|sent, created_at)

**Notification channels** are worked out from the patient's data (no "preferred channel" field):
- Always: SMS to `phone_1`.
- Also, if present: SMS to `phone_2` and an email to `email`.
- Fallback: if the result is still not delivered after N days (or the SMS fails), the physician prints a notice for the health promoter, who visits `address_1` / `address_2` (assisted). For the demo this is a "Print notice" button on results stuck in `NOTIFIED`.

**Status machine** (enforced in a single function `transition(result, new_status, actor)` that checks the role and writes the audit log entry):
`UPLOADED → LINKED (identity verified) → AUTHORIZED (reviewed + authorized) → NOTIFIED → DELIVERED`
Plus reject (with a reason) from `LINKED` → goes back to `UPLOADED` and clears `id_eira`.

## Screens / routes (only the ones needed for the demo)
1. `/login`: physician logs in with username and password; `/patient/login`: patient logs in with ID number + pickup code
2. `/upload` (physician): upload PDF → encrypted → `UPLOADED`
3. `/results/<id>/link` (physician): search patient, confirm ID number + date of birth (identity check) → `LINKED`
4. `/results/<id>/authorize` (physician): view PDF inline, tick a short review checklist (right patient, legible, complete), then authorize or reject → `AUTHORIZED` → generates pickup code + creates outbox message → `NOTIFIED`
5. `/outbox` (physician): "simulated SMS/email" screen showing what would be sent, plus a "sync" button
6. `/patient`: patient sees their documents; `/download/<token>` decrypts and streams only with a valid session plus a short-lived token
7. `/results/<id>/assisted` (physician): assisted delivery at the counter: verify ID number + date of birth (+ code if they have it) → print → `DELIVERED (assisted)`
8. `/dashboard` (physician): counts per status, table with filters, audit log per result
9. `/export.csv` (physician): export results, status and affiliation (no clinical content)

## Split for 2 people
**Person A: backend and security**
- Flask skeleton, SQLite models, seed script (1–2 physicians plus 3 fictitious patients, all with `phone_1`: one with only `phone_1`, one with `phone_2` + `email`, one whose `phone_1` is a neighbor's phone and who will be delivered through the promoter)
- `transition()` + role decorator + audit log
- PDF encryption/decryption, short-lived download token
- Outbox, pickup code generation (stored hashed), CSV export, `backup.sh`

**Person B: UI, flow and pitch**
- Base template with Pico.css, large text, high contrast, simple Spanish copy
- Upload, link, review/authorize, patient view, assisted delivery and dashboard screens
- Sample PDF ("FICTITIOUS RESULT"), demo script
- README: architecture, **Implemented vs Simulated** table, what a real pilot would need

Agree on these together in the first 20 minutes: names of tables, statuses and routes (this plan already proposes them).

## 3-hour timeline
| Time | Both / A / B |
|---|---|
| 0:00–0:20 | Together: create repo structure, `requirements.txt`, agree on the model, `PLAN.md` |
| 0:20–1:40 | A: models, auth, transition, encryption, outbox. B: templates and screens with fake data |
| 1:40–2:15 | Integration: run the full flow end to end, fix bugs |
| 2:15–2:40 | Dashboard, export, audit log visible, README with Implemented/Simulated |
| 2:40–3:00 | Rehearse the demo (one fictitious patient with a phone, one without a phone → assisted delivery) |

**Cut rule:** if you're short on time, drop HTMX, the patient email channel and CSV export first. Never drop the full flow, the audit log or assisted delivery.

## Proposed structure
```
app.py          # routes
models.py       # SQLite + transition()
security.py     # roles, encryption, tokens
notify.py       # outbox + simulated sender
seed.py         # fictitious users/patients
templates/      # Jinja + Pico.css
storage/        # encrypted PDFs (gitignored)
backup.sh
README.md / PLAN.md
```

## Cross-cutting requirements → how they're covered
- **Accessibility**: simple HTML, large text, few clicks, assisted delivery for low digital literacy
- **Connectivity**: runs on a LAN with no internet; outbox syncs when the connection comes back; SMS without data
- **Privacy**: notifications without diagnosis, roles, PDFs encrypted at rest, audit log, expiring links, minimal data
- **Operations**: one-file DB, backup script, seed for training, only one staff role to train
- **Sustainability**: everything open source, no licenses; only cost is SMS in production
- **Interoperability**: CSV/JSON export with `id_eira` and `affiliation`, so results can be matched with IMSS, ISSSTE and other institutions (future: FHIR DiagnosticReport)

## Implemented vs Simulated (for the pitch)
- Implemented: upload, encryption, linking with ID check, review/authorization, 2 roles (physician, patient), audit log, authenticated download, assisted delivery, dashboard, export
- Simulated: SMS/email sending (outbox on screen), real patient identity (fictitious data), HTTPS/hosting
- Real pilot needs: SMS gateway + SMTP, HTTPS on a local server, key management, legal agreements/consent, staff training, backup policy, testing with real users, **separate lab and EIRA-admin roles** so the administrative review and the clinical authorization are done by different people

## Verification
1. `pip install -r requirements.txt && python seed.py && flask run`
2. Log in as physician → upload sample PDF → check that the file in `storage/` is **not** a readable PDF (encrypted)
3. Link with wrong ID number or date of birth (must fail) → correct one → `LINKED`
4. Review checklist + authorize → `/outbox` shows the SMS with no diagnosis and no name
5. Patient → log in with ID number + code → download works; reuse the link after it expires → denied; another patient's URL → 403
6. Patient who never logs in → "Print notice" for the promoter (shows address, no diagnosis) → assisted delivery → `DELIVERED`
7. `/dashboard` and the audit log show every step with actor and time; `/export.csv` downloads
8. Try to skip a step (authorize before linking) → rejected
9. Try to save a patient with an affiliation outside the list → rejected by the database
10. Try to save a patient without `phone_1` → rejected by the database
