"""
============================================================
Forklift AI Safety System — Security Manager (v3)
============================================================
CHANGE LOG vs the "final" repo version reviewed:

  Previous behaviour: security.json stored every role's password
  in PLAINTEXT. Anyone with read access to the project directory
  (which the deployment notes themselves say gets chmod 777'd at
  points) could read every password and escalate straight to
  Administrator — able to disable the hardware coil, blind the
  detector by raising confidence, or repoint com_port at a
  non-existent device to silently defeat the safety system.

  v3 behaviour:
    - Passwords are stored as PBKDF2-HMAC-SHA256 hashes (200,000
      iterations, random 16-byte salt per role) using only the
      Python standard library (hashlib + secrets) — no new
      dependency to install or audit on the industrial PC.
    - Verification is constant-time (hmac.compare_digest).
    - Any legacy plaintext security.json found on disk is migrated
      to hashed records in place, transparently, on first run.
    - Every seeded or migrated account is flagged must_change=True
      so the forced-default password cannot persist indefinitely.
    - A rolling lockout (5 failed attempts / 5 minutes per role)
      blocks brute-force guessing, including of the correct
      password while locked out.

  Public API is UNCHANGED (verify_password, change_password,
  create_file_if_not_exists) so ai_gui_system.py's existing call
  sites keep working with no edits required. Two new methods are
  added — must_change_password(role) and lockout_seconds_remaining
  (role) — for the GUI to optionally surface (see UPGRADE_REPORT.md
  for the one-line login-flow hook that uses them).
============================================================
"""

import os
import json
import tempfile
import hashlib
import hmac
import secrets
import time

PBKDF2_ITERATIONS    = 200_000
SALT_BYTES           = 16
LOCKOUT_MAX_ATTEMPTS = 5
LOCKOUT_WINDOW_SECONDS = 300


class SecurityManager:
    """
    Manages security authentication, password hashing/verification, and
    password storage persistence for a 5-level access control matrix.
    """

    def __init__(self):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.file_name = os.path.join(script_dir, "security.json")
        self.default_passwords = {
            "Operator": "",
            "Technician": "tech",
            "Supervisor": "1111",
            "Engineer": "admin",
            "Administrator": "superadmin",
            "Admin": "superadmin",
        }
        # In-memory only — never persisted. role -> list[monotonic failure timestamps]
        self._failed_attempts = {}
        self.create_file_if_not_exists()

    # ==========================================
    # HASHING PRIMITIVES
    # ==========================================
    @staticmethod
    def _hash_password(password, salt=None):
        if salt is None:
            salt = secrets.token_bytes(SALT_BYTES)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
        )
        return {
            "hash": digest.hex(),
            "salt": salt.hex(),
            "iterations": PBKDF2_ITERATIONS,
        }

    @staticmethod
    def _verify(password, record):
        if not isinstance(record, dict) or "hash" not in record or "salt" not in record:
            return False
        salt = bytes.fromhex(record["salt"])
        iterations = int(record.get("iterations", PBKDF2_ITERATIONS))
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations
        )
        return hmac.compare_digest(digest.hex(), record["hash"])

    # ==========================================
    # FILE I/O
    # ==========================================
    def _atomic_write(self, data):
        dir_name = os.path.dirname(self.file_name)
        temp_fd, temp_path = tempfile.mkstemp(dir=dir_name, prefix=".tmp_sec_")
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.file_name)
        except Exception as e:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            raise e

    def _seed_record(self, plaintext_password):
        rec = self._hash_password(plaintext_password)
        rec["must_change"] = True
        return rec

    def create_file_if_not_exists(self):
        if not os.path.exists(self.file_name):
            try:
                data = {role: self._seed_record(pwd) for role, pwd in self.default_passwords.items()}
                self._atomic_write(data)
            except Exception:
                pass
            return

        # Existing file — migrate legacy plaintext records and/or add
        # any missing default roles, transparently and in place.
        try:
            with open(self.file_name, "r", encoding="utf-8") as file:
                data = json.load(file)
        except Exception:
            return

        updated = False
        for role, value in list(data.items()):
            if isinstance(value, str):
                # Legacy plaintext record -> migrate to a hashed one.
                # Functional login continues to work with the same
                # password until the operator changes it.
                data[role] = self._seed_record(value)
                updated = True

        for role, pwd in self.default_passwords.items():
            if role not in data:
                data[role] = self._seed_record(pwd)
                updated = True

        if updated:
            try:
                self._atomic_write(data)
            except Exception:
                pass

    def _load_records(self):
        try:
            with open(self.file_name, "r", encoding="utf-8") as file:
                return json.load(file)
        except Exception:
            return {role: self._seed_record(pwd) for role, pwd in self.default_passwords.items()}

    def _resolve_role_record(self, records, role):
        rec = records.get(role)
        if rec is None:
            if role == "Admin":
                rec = records.get("Administrator")
            elif role == "Administrator":
                rec = records.get("Admin")
        return rec

    # ==========================================
    # LOCKOUT TRACKING (in-memory, per ACCOUNT — not per role string)
    # ==========================================
    @staticmethod
    def _lockout_key(role):
        """Admin and Administrator resolve to the same underlying password
        record (see _resolve_role_record), so they must share one lockout
        counter too — otherwise an attacker exhausts 5 attempts as "Admin"
        and simply keeps guessing the identical account as "Administrator",
        doubling the effective brute-force budget against the top role."""
        if role in ("Admin", "Administrator"):
            return "Administrator"
        return role

    def _is_locked_out(self, role):
        key = self._lockout_key(role)
        now = time.monotonic()
        attempts = [t for t in self._failed_attempts.get(key, []) if now - t < LOCKOUT_WINDOW_SECONDS]
        self._failed_attempts[key] = attempts
        return len(attempts) >= LOCKOUT_MAX_ATTEMPTS

    def _record_failure(self, role):
        key = self._lockout_key(role)
        self._failed_attempts.setdefault(key, []).append(time.monotonic())

    def _clear_failures(self, role):
        self._failed_attempts[self._lockout_key(role)] = []

    def lockout_seconds_remaining(self, role):
        """Returns how many seconds remain before `role` can try again, or 0."""
        key = self._lockout_key(role)
        now = time.monotonic()
        attempts = [t for t in self._failed_attempts.get(key, []) if now - t < LOCKOUT_WINDOW_SECONDS]
        if len(attempts) < LOCKOUT_MAX_ATTEMPTS:
            return 0
        oldest_in_window = min(attempts)
        remaining = LOCKOUT_WINDOW_SECONDS - (now - oldest_in_window)
        return max(0, int(remaining))

    # ==========================================
    # PUBLIC API (unchanged signatures)
    # ==========================================
    def load_passwords(self):
        """Kept for backward compatibility. Returns the raw on-disk
        records (now hashed, not plaintext) — any caller expecting
        plaintext values here was already a latent security issue,
        and none of ai_gui_system.py's call sites use this method."""
        return self._load_records()

    def verify_password(self, role, password):
        if self._is_locked_out(role):
            return False
        records = self._load_records()
        rec = self._resolve_role_record(records, role)
        ok = self._verify(password, rec)
        if ok:
            self._clear_failures(role)
        else:
            self._record_failure(role)
        return ok

    def must_change_password(self, role):
        """True if this role is still on a seeded/migrated default and
        has never had change_password() called on it. Call this right
        after a successful verify_password() and prompt a forced
        change — see UPGRADE_REPORT.md for the exact GUI hook."""
        records = self._load_records()
        rec = self._resolve_role_record(records, role)
        if not isinstance(rec, dict):
            return False
        return bool(rec.get("must_change", False))

    def change_password(self, role, new_password):
        records = self._load_records()
        new_record = self._hash_password(new_password)
        new_record["must_change"] = False
        records[role] = new_record
        if role == "Admin":
            records["Administrator"] = dict(new_record)
        elif role == "Administrator":
            records["Admin"] = dict(new_record)
        self._atomic_write(records)
        self._clear_failures(role)
