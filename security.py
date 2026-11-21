"""Roles, encryption at rest, short-lived download tokens, pickup codes."""
import os
import secrets
import uuid
from functools import wraps

from cryptography.fernet import Fernet
from flask import abort, current_app, redirect, session, url_for
from itsdangerous import URLSafeTimedSerializer

KEY_FILE = "fernet.key"
SECRET_FILE = "flask_secret"


def _read_or_create(path, factory):
    if not os.path.exists(path):
        with open(path, "wb") as fh:
            fh.write(factory())
        os.chmod(path, 0o600)
    with open(path, "rb") as fh:
        return fh.read()


def load_or_create_secret(storage_dir):
    return _read_or_create(os.path.join(storage_dir, SECRET_FILE), lambda: secrets.token_hex(32).encode())


def _fernet():
    # PROTOTYPE: the key lives next to the data. A real pilot needs separate key
    # management (e.g. key on a different device / OS keyring) and a key backup plan.
    key = _read_or_create(os.path.join(current_app.config["STORAGE_DIR"], KEY_FILE), Fernet.generate_key)
    return Fernet(key)


def save_encrypted(data):
    name = uuid.uuid4().hex + ".bin"
    with open(os.path.join(current_app.config["STORAGE_DIR"], name), "wb") as fh:
        fh.write(_fernet().encrypt(data))
    return name


def load_decrypted(name):
    with open(os.path.join(current_app.config["STORAGE_DIR"], name), "rb") as fh:
        return _fernet().decrypt(fh.read())


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            role = session.get("role")
            if role is None:
                target = "patient_login" if roles == ("patient",) else "login"
                return redirect(url_for(target))
            if role not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapper

    return decorator


def actor():
    """Label used in the audit log for whoever is logged in."""
    return "{}:{}".format(session.get("role", "anon"), session.get("actor_id", "?"))


def _serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="download")


def make_download_token(result_id, patient_id):
    return _serializer().dumps({"r": result_id, "p": patient_id})


def read_download_token(token):
    """Raises itsdangerous.SignatureExpired / BadSignature if invalid."""
    return _serializer().loads(token, max_age=current_app.config["DOWNLOAD_TOKEN_MAX_AGE"])


def generate_pickup_code():
    return "{:06d}".format(secrets.randbelow(10**6))
