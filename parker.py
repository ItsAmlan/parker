#!/usr/bin/env python3

import os
import re
import sys
import pwd
import signal
import grp
import time
import json
import uuid
import shlex
import shutil
import socket
import getpass
import tempfile
import argparse
import datetime
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import connection

def parse_env_line(line):
    """
    Parses one .env line into (key, value), or None for blanks/comments.
    Supports `export KEY=v`, quoted values, and trailing ` # comments` on unquoted values.
    NOTE: parker-ui/main.py carries an identical copy (tests keep them in sync).
    """
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None

    if line.startswith("export "):
        line = line[len("export "):].lstrip()

    key, value = line.split("=", 1)
    key, value = key.strip(), value.strip()

    if not key:
        return None

    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        if end != -1:
            return key, value[1:end]
    elif value.startswith("#"):
        value = ""
    else:
        value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()

    return key, value

class EnvFileError(Exception):
    """The .env file exists but cannot be read."""

def load_env(file_path=".env"):
    """
    Simple native .env loader to avoid extra dependencies.

    A missing file is fine (settings may come from the environment), but a file that exists
    and cannot be read (permissions, it is a directory, bad encoding) raises EnvFileError:
    carrying on would silently run with the wrong settings.
    """
    try:
        with open(file_path, "r") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return
    except (OSError, UnicodeDecodeError) as e:
        reason = e.strerror if isinstance(e, OSError) and e.strerror else type(e).__name__
        raise EnvFileError(
            f"Cannot read {file_path}: {reason}. Parker will not run with default settings "
            f"instead. If the file is root-only, run Parker as root (sudo); otherwise fix its "
            f"permissions or contents."
        ) from e

    for line in lines:
        parsed = parse_env_line(line)
        if parsed:
            os.environ[parsed[0]] = parsed[1]

# Load environment variables from the script directory, regardless of service cwd.
ENV_FILE = Path(__file__).resolve().with_name(".env")
ENV_LOAD_ERROR = None
try:
    load_env(ENV_FILE)
except EnvFileError as _env_error:
    # Reported (and the run stopped) by main(), so --help and imports still work.
    ENV_LOAD_ERROR = str(_env_error)

# Force IPv4 for all requests using this adapter
class ForcedIP4Adapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        return super().init_poolmanager(*args, **kwargs)

def _allowed_gai_family():
    return socket.AF_INET

# Monkey-patch urllib3 to force IPv4 globally for this session
connection.allowed_gai_family = _allowed_gai_family

# =========================================================
# CONFIGURATION
# =========================================================

def is_placeholder(value):
    """True for the example values shipped in .env.example (never real configuration)."""
    v = value.strip().lower()
    return v.startswith("your_") or "yourdomain.com" in v

def _setting(name):
    """A setting from .env/the environment. Empty and placeholder values count as unset."""
    value = os.getenv(name, "").strip()
    return "" if is_placeholder(value) else value

# Settings that belong to YOUR server. There are deliberately no built-in defaults for them:
# a baked-in hostname or path silently ends up in DNS records and on disk of a server it was
# never meant for. Parker asks for what a run needs and stops if it is missing.
BASE_DIR = _setting("WEBROOT")                      # base directory for project directories

# Global run flags, set from the command line in main().
DRY_RUN = False
# True with --yes: never prompt; use flags/defaults and fail on anything missing.
NON_INTERACTIVE = False

CLOUDFLARE_API_TOKEN = _setting("CLOUDFLARE_API_TOKEN")
CLOUDFLARE_ACCOUNT_ID = _setting("CLOUDFLARE_ACCOUNT_ID")

CNAME_TARGET = _setting("CNAME_TARGET")             # hostname new sites' CNAME records point to
MAIL_HOSTNAME = _setting("MAIL_HOSTNAME")           # this server's mail hostname (SPF)
DKIM_SELECTOR = _setting("DKIM_SELECTOR")           # DKIM selector name

ENABLE_MAIL_SETUP = True

# MX target: its own setting, or the mail hostname when that is missing or empty.
MX_HOSTNAME = _setting("MX_HOSTNAME") or MAIL_HOSTNAME

PHP_FPM_SNIPPET = os.getenv(
    "PHP_FPM_SNIPPET",
    "snippets/php8.5.conf"
)

# Dovecot / Virtual Mailbox Paths
DOVECOT_USERS_FILE = "/etc/dovecot/users"
POSTFIX_VIRTUAL_DOMAINS = "/etc/postfix/virtual_domains"
POSTFIX_VIRTUAL_MAILBOX_MAPS = "/etc/postfix/virtual_mailbox_maps"
VMAIL_BASE = "/var/mail/vhosts"
OPENDKIM_DIR = "/etc/opendkim"



NGINX_SITES_AVAILABLE = "/etc/nginx/sites-available"
NGINX_SITES_ENABLED = "/etc/nginx/sites-enabled"

# Where certbot keeps certificate lineages (one directory per certificate name).
LETSENCRYPT_LIVE = "/etc/letsencrypt/live"

DEFAULT_SSL_EMAIL = _setting("DEFAULT_SSL_EMAIL")

# Optional "user" or "user:group" that should own newly created project directories.
PROJECT_OWNER = os.getenv("PROJECT_OWNER", "")

# Append-only audit log of every run (console output, without typed secrets).
PARKER_LOG_FILE = os.getenv("PARKER_LOG_FILE", "/var/log/parker.log")

# Baseline response headers added to generated nginx configs (set to 0 to disable).
NGINX_SECURITY_HEADERS = os.getenv("NGINX_SECURITY_HEADERS", "1").lower() not in ("0", "false", "no", "off")

# "auto" enables `listen [::]:80` only when the host has IPv6; "1"/"0" force it.
NGINX_IPV6 = os.getenv("NGINX_IPV6", "auto").lower()

def validate_path(path):
    """Ensures the path is within BASE_DIR or allowed system config areas."""
    abs_path = os.path.abspath(path)
    allowed_areas = [
        os.path.abspath("/etc/nginx"),
        os.path.abspath(OPENDKIM_DIR),
        os.path.abspath("/etc/postfix"),
        os.path.abspath("/etc/dovecot"),
        os.path.abspath(VMAIL_BASE),
        os.path.abspath(tempfile.gettempdir())
    ]
    if BASE_DIR:  # unset must not turn into "the current directory"
        allowed_areas.append(os.path.abspath(BASE_DIR))

    if not any(abs_path == area or abs_path.startswith(area + os.sep) for area in allowed_areas):
        raise PermissionError(f"🔒 Security Violation: Path {abs_path} is outside allowed areas.")

# =========================================================
# ROLLBACK SYSTEM
# =========================================================

class ParkerError(Exception):
    """A problem that should abort the run (and roll back anything already changed)."""

class CloudflareError(ParkerError):
    """The Cloudflare API could not be queried or rejected a request."""

# .env name -> (module attribute, what it is). Used to explain exactly what is missing.
REQUIRED_SETTINGS = {
    "WEBROOT": ("BASE_DIR", "base directory for project directories, e.g. /var/www"),
    "CNAME_TARGET": ("CNAME_TARGET", "hostname new DNS records point to (this server), e.g. server.example.com"),
    "MAIL_HOSTNAME": ("MAIL_HOSTNAME", "this server's mail hostname, e.g. mail.example.com"),
    "DKIM_SELECTOR": ("DKIM_SELECTOR", "DKIM selector name, e.g. default"),
}

def missing_settings(*names):
    return [name for name in names if not globals()[REQUIRED_SETTINGS[name][0]]]

def require_settings(*names, needed_for, instead=""):
    """
    Stops (before anything is changed) when a setting this run needs is not configured.
    Unset, empty and the .env.example placeholders all count as missing.
    """
    missing = missing_settings(*names)
    if not missing:
        return

    one = len(missing) == 1
    where = f"{ENV_FILE} (the file does not exist)" if not ENV_FILE.exists() else str(ENV_FILE)
    lines = [
        f"{', '.join(missing)} {'is' if one else 'are'} not set in {where} "
        f"(or still {'the placeholder' if one else 'placeholders'} from .env.example), "
        f"but {needed_for} needs {'it' if one else 'them'}.",
        "Add:",
    ]
    lines += [f"    {name}=...   # {REQUIRED_SETTINGS[name][1]}" for name in missing]
    if instead:
        lines.append(instead)
    raise ParkerError("\n  ".join(lines))

class RollbackStack:
    def __init__(self):
        self.tasks = []
        self.backups = set()

    def add(self, func, *args, label=None):
        if DRY_RUN:
            return None  # nothing is changed in a dry run, so there is nothing to undo
        task = (func, args, label)
        self.tasks.append(task)
        return task

    def remove(self, task):
        """Forget a task (e.g. once the change it undoes must no longer be undone)."""
        if task in self.tasks:
            self.tasks.remove(task)

    def add_backup(self, path):
        """Track a backup file for cleanup."""
        self.backups.add(path)

    def run(self):
        if not self.tasks:
            return
        print("\n" + "!" * 40)
        print(" 🚨 FAILURE DETECTED: ROLLING BACK CHANGES")
        print("!" * 40)
        for func, args, label in reversed(self.tasks):
            if label:
                print(f" 🔄 Undoing: {label}...")
            try:
                func(*args)
            except Exception as e:
                print(f" ⚠ Failed to undo {label or 'task'}: {e}")
        print("\n ✅ Rollback complete.")

    def cleanup(self):
        """Remove temporary backup files after success."""
        if not self.backups:
            return
        print("\n🧹 Cleaning up temporary backups...")
        for path in self.backups:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception as e:
                    print(f" ⚠ Failed to remove backup {path}: {e}")
        print("✅ Cleanup complete.")

rollback_stack = RollbackStack()

# =========================================================
# HELPERS
# =========================================================

def log_step(step_name, description):
    """Prints a prominent step header."""
    print(f"\n--- [ {step_name} ] ---")
    print(f"👉 {description}")

def ensure_root():

    if os.geteuid() != 0:
        print("❌ Please run this script as root or using sudo.")
        sys.exit(1)

def ask(question, default=None):

    if NON_INTERACTIVE:
        if default is None:
            raise ParkerError(f"Non-interactive mode: no value or flag provided for: {question}")
        return default

    prompt = question

    if default:
        prompt += f" [{default}]"

    prompt += ": "

    val = input(prompt).strip()

    if not val and default is not None:
        return default

    return val

def ask_secret(question):
    """Like ask(), but the typed value is not echoed and never logged."""

    if NON_INTERACTIVE:
        raise ParkerError(f"Non-interactive mode cannot prompt for a secret: {question}")

    return getpass.getpass(f"{question}: ").strip()

def ask_yes_no(question, default="y"):

    if NON_INTERACTIVE:
        return default.lower() in ("y", "yes")

    while True:

        val = input(
            f"{question} (y/n) [{default}]: "
        ).strip().lower()

        if not val:
            val = default.lower()

        if val in ["y", "yes"]:
            return True

        if val in ["n", "no"]:
            return False

        print("Please enter y or n.")

def run(cmd, cwd=None, check=True):
    if DRY_RUN:
        print(f"\n[DRY RUN] Would run: {' '.join(cmd)} (cwd: {cwd or 'default'})\n")
        return subprocess.CompletedProcess(cmd, 0)

    print(f"\n[RUNNING] {' '.join(cmd)}\n")

    return subprocess.run(
        cmd,
        cwd=cwd,
        check=check
    )

class TeeStream:
    """Mirrors console output into the audit log, one timestamped line at a time."""

    def __init__(self, stream, log_file):
        self._stream = stream
        self._log = log_file
        self._partial = ""

    def write(self, data):
        self._stream.write(data)
        self._partial += data
        *lines, self._partial = self._partial.split("\n")
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        for line in lines:
            line = line.rstrip("\r")
            if line.strip():
                self._log.write(f"{stamp} {line}\n")
        self._log.flush()
        return len(data)

    def flush(self):
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)

def start_audit_log(argv):
    """Best effort: a missing/unwritable log must never stop a provisioning run."""
    if not PARKER_LOG_FILE:
        return None

    try:
        fd = os.open(PARKER_LOG_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        log_file = os.fdopen(fd, "a")
    except OSError:
        return None

    operator = os.environ.get("SUDO_USER") or os.environ.get("USER") or "unknown"
    log_file.write(
        f"{datetime.datetime.now().isoformat(timespec='seconds')} "
        f"===== parker run by {operator}: {' '.join(argv)} =====\n"
    )
    sys.stdout = TeeStream(sys.stdout, log_file)
    return log_file

def stop_audit_log(log_file):
    if log_file is None:
        return
    if isinstance(sys.stdout, TeeStream):
        sys.stdout = sys.stdout._stream
    log_file.close()

def run_capture(cmd, cwd=None):
    """
    Like run(check=False), but also returns the command's output (still shown live).
    Returns (returncode, output).
    """
    if DRY_RUN:
        print(f"\n[DRY RUN] Would run: {' '.join(cmd)} (cwd: {cwd or 'default'})\n")
        return 0, ""

    print(f"\n[RUNNING] {' '.join(cmd)}\n")

    proc = subprocess.Popen(
        cmd, cwd=cwd, text=True, bufsize=1,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    lines = []
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        lines.append(line)
    proc.wait()

    return proc.returncode, "".join(lines)

def is_directory_empty(path):
    """Checks if a directory is empty."""
    if not os.path.exists(path):
        return True
    return len(os.listdir(path)) == 0

def check_existing_parking(domain):
    """Check if a domain appears to be already parked based on local indicators."""
    indicators = []

    nginx_config = os.path.join(NGINX_SITES_AVAILABLE, f"{domain}.conf")
    if os.path.exists(nginx_config):
        indicators.append(f"  \u2705 Nginx config: {nginx_config}")

    nginx_enabled = os.path.join(NGINX_SITES_ENABLED, f"{domain}.conf")
    if os.path.islink(nginx_enabled):
        indicators.append(f"  \u2705 Site enabled: {nginx_enabled}")

    project_root = os.path.join(BASE_DIR, domain)
    if os.path.exists(project_root) and not is_directory_empty(project_root):
        indicators.append(f"  \u2705 Project directory: {project_root}")

    ssl_dir = os.path.join(LETSENCRYPT_LIVE, domain)
    if os.path.exists(ssl_dir):
        indicators.append(f"  \u2705 SSL certificate: {ssl_dir}")

    return indicators

def restore_backup(backup_path, original_path):
    """Restores a file from a backup."""
    if DRY_RUN:
        print(f" 🔄 [DRY RUN] Would restore original file: {original_path}")
        return

    if os.path.exists(backup_path):
        print(f" 🔄 Restoring original file: {original_path}")
        shutil.move(backup_path, original_path)

def ensure_directory(path, track_rollback=False, mode=None, owner=None):
    validate_path(path)
    p = Path(path)

    if p.exists():
        if not p.is_dir():
            # Exists but not a directory - this is an error state
            raise FileExistsError(f"{path} exists and is not a directory.")
        return

    if DRY_RUN:
        print(f"📂 [DRY RUN] Would create directory: {path}")
        return

    # Remember the topmost directory we are about to create, so rollback removes
    # everything this call added and nothing that was already there.
    first_missing = p
    while not first_missing.parent.exists():
        first_missing = first_missing.parent

    p.mkdir(parents=True, exist_ok=True)

    # Only directories created by this call are touched, never pre-existing ones.
    for created in [p, *p.parents]:
        if mode is not None:
            # Explicit chmod: the process umask must not make web content unreadable to nginx.
            os.chmod(created, mode)
        if owner is not None:
            os.chown(created, owner[0], owner[1])
        if created == first_missing:
            break

    if track_rollback:
        rollback_stack.add(shutil.rmtree, str(first_missing), label=f"Remove directory {first_missing}")

def command_exists(command):

    return shutil.which(command) is not None

def write_file(path, content, track_rollback=False):
    validate_path(path)
    backup_path = f"{path}.phoenix_bak"
    existed = os.path.exists(path)

    if track_rollback:
        if existed:
            if DRY_RUN:
                print(f"📂 [DRY RUN] Would back up {path} to {backup_path}")
            else:
                shutil.copy2(path, backup_path)

            rollback_stack.add_backup(backup_path)
            rollback_stack.add(restore_backup, backup_path, path, label=f"Restore original file {path}")
        else:
            rollback_stack.add(remove_if_exists, path, label=f"Delete new file {path}")

    if DRY_RUN:
        print(f"📝 [DRY RUN] Would write content to: {path}")
        return

    # Write to a temp file and rename: a crash never leaves a half-written config.
    tmp_path = f"{path}.parker_tmp"
    try:
        with open(tmp_path, "w") as f:
            f.write(content)
        if existed:
            shutil.copymode(path, tmp_path)
        else:
            os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    finally:
        remove_if_exists(tmp_path)

def remove_if_exists(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass

def append_unique_line(path, line, track_rollback=False):
    validate_path(path)
    existing = ""

    if os.path.exists(path):
        with open(path, "r") as f:
            existing = f.read()

    # Compare whole lines: "example.com" must not match "notexample.com".
    if line in existing.splitlines():
        return

    if track_rollback:
        backup_path = f"{path}.phoenix_bak"
        if os.path.exists(path):
            # Only create one backup per run for the same file
            if backup_path not in rollback_stack.backups:
                if DRY_RUN:
                    print(f"📂 [DRY RUN] Would back up {path} to {backup_path}")
                else:
                    shutil.copy2(path, backup_path)

                rollback_stack.add_backup(backup_path)
                rollback_stack.add(restore_backup, backup_path, path, label=f"Restore system config {path}")
        else:
            rollback_stack.add(remove_if_exists, path, label=f"Delete new file {path}")

    if DRY_RUN:
        print(f"📝 [DRY RUN] Would append line to: {path}")
        return

    with open(path, "a") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")  # never glue onto an unterminated last line
        f.write(line + "\n")

# =========================================================
# DOMAIN HELPERS
# =========================================================

# Registry suffixes under which the registrable domain has THREE labels (example.co.uk).
MULTI_LEVEL_TLDS = [
    "co.in", "org.in", "net.in", "firm.in", "gen.in", "ind.in",
    "co.uk", "org.uk", "gov.uk", "ac.uk", "me.uk", "ltd.uk", "plc.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "id.au",
    "co.nz", "org.nz", "net.nz", "ac.nz", "govt.nz",
    "co.za", "org.za", "net.za", "gov.za",
    "com.br", "net.br", "org.br", "gov.br",
    "com.mx", "org.mx", "gob.mx",
    "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp",
    "co.kr", "or.kr", "go.kr",
    "com.cn", "net.cn", "org.cn", "gov.cn",
    "com.hk", "com.tw", "com.sg", "com.my", "com.ph", "com.pk", "com.vn", "co.id",
    "com.tr", "com.ar", "com.co", "com.pe", "com.ng", "co.ke", "co.il", "co.th",
]

# Under a two-letter country TLD these second-level labels almost always mark a registry
# suffix too (co.xx, com.xx, ...). Covers countries missing from the list above.
GENERIC_SECOND_LEVELS = {
    "co", "com", "org", "net", "gov", "edu", "ac", "or", "ne", "go", "gob", "mil",
    "sch", "ltd", "plc", "nom",
}

def is_public_suffix_guess(name):
    """True for names like "co.uk" that are registry suffixes, not registrable domains."""
    labels = name.lower().split(".")
    if name.lower() in MULTI_LEVEL_TLDS:
        return True
    return len(labels) == 2 and len(labels[1]) == 2 and labels[0] in GENERIC_SECOND_LEVELS

def extract_root_domain(domain):
    """Best guess at the registrable domain. Cloudflare has the final say: see find_zone()."""
    domain = domain.lower().strip()
    parts = domain.split(".")

    if len(parts) >= 3 and is_public_suffix_guess(".".join(parts[-2:])):
        return ".".join(parts[-3:])

    if len(parts) >= 2:
        return ".".join(parts[-2:])

    return domain

def get_subdomain_part(domain, root=None):
    root = root or extract_root_domain(domain)

    if domain == root:
        return None

    suffix = "." + root

    if domain.endswith(suffix):
        return domain[:-len(suffix)]

    return None

def zone_candidates(domain):
    """Zone names to try, the usual guess first, then every longer suffix (longest first)."""
    labels = domain.lower().strip(".").split(".")
    candidates = [extract_root_domain(domain)]

    for i in range(len(labels) - 1):          # never the bare TLD
        name = ".".join(labels[i:])
        if name not in candidates and not is_public_suffix_guess(name):
            candidates.append(name)

    return candidates

def find_zone(cf, domain):
    """
    Finds the Cloudflare zone that serves `domain`. The suffix list cannot know every registry
    (example.co.nz, example.com.br, ...), so instead of trusting a guess this asks Cloudflare.
    Returns (zone, zone_name); (None, best_guess) when the account has no such zone.
    Raises CloudflareError when the API itself fails.
    """
    for name in zone_candidates(domain):
        zone = cf.get_zone(name)
        if zone:
            return zone, name

    return None, extract_root_domain(domain)

# =========================================================
# CLOUDFLARE
# =========================================================

class CloudflareManager:

    BASE_URL = "https://api.cloudflare.com/client/v4"

    def __init__(self):

        self.headers = {
            "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
            "Content-Type": "application/json"
        }

    def print_api_error(self, response):
        try:
            data = response.json()
        except ValueError:
            print(f"Cloudflare response: {response.text[:500]}")
            return

        errors = data.get("errors") or []
        if errors:
            for error in errors:
                code = error.get("code", "unknown")
                message = error.get("message", "Unknown Cloudflare error")
                print(f"Cloudflare error {code}: {message}")
        else:
            print(json.dumps(data, indent=2))

    def get_zone(self, root_domain):

        url = f"{self.BASE_URL}/zones"

        params = {
            "name": root_domain
        }

        print(f"🔍 Searching for Cloudflare zone: {root_domain}...")

        start_time = time.time()
        try:
            r = requests.get(
                url,
                headers=self.headers,
                params=params,
                timeout=10
            )
            duration = time.time() - start_time
            print(f"⏱  API Request took {duration:.2f}s")
            r.raise_for_status()
        except Exception as e:
            print(f"⚠ Cloudflare API Error: {e}")
            if 'r' in locals():
                self.print_api_error(r)
            raise CloudflareError(str(e))

        data = r.json()

        if not data.get("success"):
            self.print_api_error(r)
            raise CloudflareError("Cloudflare API reported failure")

        if not data.get("result"):
            return None

        return data["result"][0]

    def create_zone(self, root_domain):

        if DRY_RUN:
            print(f"🌐 [DRY RUN] Would create Cloudflare zone: {root_domain}")
            return {"id": "dry-run-zone-id", "name_servers": ["ns1.dryrun.com", "ns2.dryrun.com"]}

        url = f"{self.BASE_URL}/zones"

        payload = {
            "account": {
                "id": CLOUDFLARE_ACCOUNT_ID
            },
            "name": root_domain,
            "type": "full"
        }

        print(f"🌐 Creating new Cloudflare zone: {root_domain}...")

        start_time = time.time()
        try:
            r = requests.post(
                url,
                headers=self.headers,
                json=payload,
                timeout=10
            )
            duration = time.time() - start_time
            print(f"⏱  API Request took {duration:.2f}s")
            r.raise_for_status()
        except Exception as e:
            print(f"⚠ Cloudflare API Error: {e}")
            if 'r' in locals():
                self.print_api_error(r)
            return None

        data = r.json()

        if not data.get("success"):
            self.print_api_error(r)
            return None

        zone = data["result"]

        # Undo only until the nameservers are switched: after that, deleting the
        # zone would take the domain offline (see setup_dns).
        zone["_rollback_task"] = rollback_stack.add(
            self.delete_zone,
            zone["id"],
            label=f"Delete Cloudflare zone {root_domain}"
        )

        return zone

    def delete_zone(self, zone_id):
        url = f"{self.BASE_URL}/zones/{zone_id}"
        try:
            r = requests.delete(url, headers=self.headers, timeout=10)
            return r.json().get("success", False)
        except Exception as e:
            print(f"⚠ Cloudflare API Error (Delete zone): {e}")
            return False

    def create_dns_record(
        self,
        zone_id,
        record_type,
        record_name,
        content,
        proxied=False,
        ttl=1,
        priority=None,
        track_rollback=True
    ):
        if DRY_RUN:
            extra = f" (priority={priority})" if priority is not None else ""
            print(f"🌐 [DRY RUN] Would create DNS record: {record_type} {record_name} -> {content}{extra}")
            return "dry-run-id"

        url = f"{self.BASE_URL}/zones/{zone_id}/dns_records"

        payload = {
            "type": record_type,
            "name": record_name,
            "content": content,
            "ttl": ttl
        }

        if record_type in ["A", "AAAA", "CNAME"]:
            payload["proxied"] = proxied

        if record_type == "MX" and priority is not None:
            payload["priority"] = priority

        start_time = time.time()
        try:
            r = requests.post(
                url,
                headers=self.headers,
                json=payload,
                timeout=10
            )
            duration = time.time() - start_time
            print(f"⏱  API Request took {duration:.2f}s")
            r.raise_for_status()
        except Exception as e:
            print(f"⚠ Cloudflare API Error: {e}")
            return None

        data = r.json()

        if not data.get("success"):

            print(
                f"⚠ Failed creating "
                f"{record_type} record for "
                f"{record_name}"
            )

            print(json.dumps(data, indent=2))

            return None # Return None on failure

        record_id = data["result"]["id"]

        print(
            f"✅ DNS added: "
            f"{record_type} {record_name} (ID: {record_id})"
        )

        if track_rollback:
            rollback_stack.add(
                self.delete_dns_record, 
                zone_id, 
                record_id, 
                label=f"Delete DNS record {record_name} ({record_type})"
            )

        return record_id

    def list_dns_records(self, zone_id, record_name):
        if DRY_RUN:
            print(f"🔍 [DRY RUN] Would check existing DNS records for: {record_name}")
            return []

        url = f"{self.BASE_URL}/zones/{zone_id}/dns_records"

        params = {
            "name": record_name
        }

        print(f"🔍 Checking existing DNS records for: {record_name}...")

        start_time = time.time()
        try:
            r = requests.get(
                url,
                headers=self.headers,
                params=params,
                timeout=10
            )
            duration = time.time() - start_time
            print(f"⏱  API Request took {duration:.2f}s")
            r.raise_for_status()
        except Exception as e:
            print(f"⚠ Cloudflare API Error: {e}")
            if 'r' in locals():
                self.print_api_error(r)
            return None

        data = r.json()

        if not data.get("success"):
            self.print_api_error(r)
            return None

        return data.get("result", [])

    def get_matching_wildcard_name(self, record_name, zone_name):
        record_name = record_name.lower().strip(".")
        zone_name = zone_name.lower().strip(".")

        if record_name == zone_name:
            return None

        suffix = "." + zone_name
        if not record_name.endswith(suffix):
            return None

        relative_name = record_name[:-len(suffix)]
        labels = relative_name.split(".")

        if not labels or not labels[0]:
            return None

        parent_labels = labels[1:]
        if parent_labels:
            return f"*.{'.'.join(parent_labels)}.{zone_name}"

        return f"*.{zone_name}"

    def has_web_dns_record(self, zone_id, record_name):
        records = self.list_dns_records(zone_id, record_name)

        if records is None:
            return False

        web_record_types = {"A", "AAAA", "CNAME"}

        for record in records:
            if record.get("type") in web_record_types:
                print(
                    f"✅ Existing DNS found: "
                    f"{record.get('type')} {record_name}"
                )
                return True

        return False

    def create_site_cname_record(
        self,
        zone_id,
        zone_name,
        record_name,
        content,
        proxied=True,
        ttl=1
    ):
        if self.has_web_dns_record(zone_id, record_name):
            print(f"⏭ Skipping DNS create for {record_name}; exact web record already exists.")
            return None

        wildcard_name = self.get_matching_wildcard_name(record_name, zone_name)

        if wildcard_name and self.has_web_dns_record(zone_id, wildcard_name):
            print(
                f"⏭ Skipping DNS create for {record_name}; "
                f"matching wildcard {wildcard_name} already exists."
            )
            return None

        return self.create_dns_record(
            zone_id=zone_id,
            record_type="CNAME",
            record_name=record_name,
            content=content,
            proxied=proxied,
            ttl=ttl
        )

    def delete_dns_record(self, zone_id, record_id):
        url = f"{self.BASE_URL}/zones/{zone_id}/dns_records/{record_id}"
        start_time = time.time()
        try:
            r = requests.delete(url, headers=self.headers, timeout=10)
            duration = time.time() - start_time
            print(f"⏱  API Request (Delete) took {duration:.2f}s")
            return r.json().get("success", False)
        except Exception as e:
            print(f"⚠ Cloudflare API Error (Delete): {e}")
            return False

# =========================================================
# MAIL SETUP
# =========================================================

def generate_dkim(domain):

    key_dir = os.path.join(OPENDKIM_DIR, "keys", domain)

    if os.path.exists(os.path.join(key_dir, f"{DKIM_SELECTOR}.private")):
        print(f"ℹ DKIM key for {domain} already exists. Reusing it.")
        return

    ensure_directory(key_dir, track_rollback=True)

    run([
        "opendkim-genkey",
        "-d", domain,
        "-s", DKIM_SELECTOR,
        "-D", key_dir
    ])

    run([
        "chown",
        "-R",
        "opendkim:opendkim",
        key_dir
    ])

def configure_opendkim_domain(domain):

    append_unique_line(
        os.path.join(OPENDKIM_DIR, "key.table"),
        (
            f"{DKIM_SELECTOR}._domainkey.{domain} "
            f"{domain}:{DKIM_SELECTOR}:"
            f"{OPENDKIM_DIR}/keys/{domain}/{DKIM_SELECTOR}.private"
        ),
        track_rollback=True
    )

    append_unique_line(
        os.path.join(OPENDKIM_DIR, "signing.table"),
        f"*@{domain} {DKIM_SELECTOR}._domainkey.{domain}",
        track_rollback=True
    )

def get_dkim_record(domain):

    if DRY_RUN:
        return "v=DKIM1; k=rsa; p=DRYRUN_PLACEHOLDER_KEY"

    path = os.path.join(OPENDKIM_DIR, "keys", domain, f"{DKIM_SELECTOR}.txt")

    with open(path, "r") as f:
        content = f.read()

    content = content.replace("\n", "")

    match = re.search(
        r'p=([A-Za-z0-9+/=]+)',
        content
    )

    if not match:
        return None

    return (
        "v=DKIM1; k=rsa; p="
        + match.group(1)
    )

def txt_value(record):
    """TXT contents as stored by Cloudflare (which wraps them in quotes)."""
    return (record.get("content") or "").strip().strip('"')

def ensure_dns_record(cf, zone_id, record_type, record_name, content, is_equivalent, priority=None):
    """
    Creates the record unless an equivalent one already exists. Duplicates are not
    just untidy here: two SPF (or DMARC) records make both invalid.
    is_equivalent(record) decides what counts as "already there".
    """
    existing = cf.list_dns_records(zone_id, record_name) or []

    for record in existing:
        if record.get("type") == record_type and is_equivalent(record):
            print(f"⏭ Skipping {record_type} {record_name}; an equivalent record already exists.")

            if record_type == "TXT" and txt_value(record) != content:
                print(f"⚠ The existing {record_name} record differs from what Parker would create. Review it:")
                print(f"     existing: {txt_value(record)}")
                print(f"     Parker  : {content}")
            return None

    return cf.create_dns_record(
        zone_id=zone_id,
        record_type=record_type,
        record_name=record_name,
        content=content,
        priority=priority
    )

def setup_mail_dns(
    cf,
    zone_id,
    domain
):

    print("\n📧 Configuring mail authentication...")

    generate_dkim(domain)

    configure_opendkim_domain(domain)

    dkim_value = get_dkim_record(domain)

    ensure_dns_record(
        cf, zone_id, "TXT", domain,
        content=(
            f"v=spf1 "
            f"a:{MAIL_HOSTNAME} "
            f"mx "
            f"~all"
        ),
        is_equivalent=lambda r: txt_value(r).lower().startswith("v=spf1")
    )

    ensure_dns_record(
        cf, zone_id, "TXT", f"_dmarc.{domain}",
        content=(
            "v=DMARC1; "
            "p=quarantine; "
            "adkim=s; "
            "aspf=s"
        ),
        is_equivalent=lambda r: txt_value(r).upper().startswith("V=DMARC1")
    )

    if dkim_value:
        ensure_dns_record(
            cf, zone_id, "TXT", f"{DKIM_SELECTOR}._domainkey.{domain}",
            content=dkim_value,
            is_equivalent=lambda r: txt_value(r).upper().startswith("V=DKIM1")
        )
    else:
        print("⚠ DKIM key could not be read. Skipping DKIM DNS record.")

    # MX record — points domain to the mail server for incoming mail
    ensure_dns_record(
        cf, zone_id, "MX", domain,
        content=MX_HOSTNAME,
        is_equivalent=lambda r: (r.get("content") or "").rstrip(".").lower() == MX_HOSTNAME.rstrip(".").lower(),
        priority=10
    )

    run(["systemctl", "restart", "opendkim"])
    run(["systemctl", "restart", "postfix"])

    print("✅ Mail authentication configured.")

# =========================================================
# INCOMING MAIL / DOVECOT
# =========================================================

def hash_password(plain_password):
    """Hash a password using doveadm for Dovecot BLF-CRYPT."""
    if DRY_RUN:
        return "{BLF-CRYPT}$2b$12$DRYRUN_PLACEHOLDER"

    # The password goes through stdin (twice: password + retype), never argv,
    # so it cannot be read from the process list.
    result = subprocess.run(
        ["doveadm", "pw", "-s", "BLF-CRYPT"],
        input=f"{plain_password}\n{plain_password}\n",
        capture_output=True,
        text=True,
        check=True
    )
    return result.stdout.strip()

def collect_mailboxes(domain):
    """Prompts for mailboxes to create. Read-only: nothing is written until provisioning."""

    mailboxes = []

    existing_users = read_text(DOVECOT_USERS_FILE).splitlines()

    while True:
        local_part = ask(
            "Enter mailbox local part (e.g. support, contact) or leave empty to finish"
        ).strip().lower()

        if not local_part:
            break

        # Basic validation for the local part
        if not re.match(r'^[a-z0-9._-]+$', local_part):
            print("\u26a0 Invalid local part. Only lowercase letters, digits, dots, hyphens and underscores are allowed.")
            continue

        email = f"{local_part}@{domain}"

        # Match the exact user field, not a suffix of another address.
        if any(line.startswith(f"{email}:") for line in existing_users):
            print(f"\u26a0 Mailbox {email} already exists. Skipping.")
            continue

        if any(queued[1] == email for queued in mailboxes):
            print(f"\u26a0 Mailbox {email} is already queued. Skipping.")
            continue

        password = ask_secret(f"Enter password for {email}")

        if not password:
            print("\u26a0 Password cannot be empty. Skipping this mailbox.")
            continue

        if len(password) < 8:
            print("\u26a0 Password must be at least 8 characters. Skipping this mailbox.")
            continue

        mailboxes.append((local_part, email, password))
        print(f"  \U0001f4ec Queued: {email}")

    return mailboxes

def setup_incoming_mail(domain, mailboxes):
    """Provision incoming mailboxes for a domain via Dovecot + Postfix virtual maps."""

    # Ensure the domain is in Postfix virtual_domains
    validate_path(POSTFIX_VIRTUAL_DOMAINS)
    append_unique_line(POSTFIX_VIRTUAL_DOMAINS, domain, track_rollback=True)

    if not mailboxes:
        print("\u2139 No mailboxes to create.")
        run(["systemctl", "reload", "postfix"])
        return

    print(f"\n\U0001f4e7 Creating {len(mailboxes)} mailbox(es) for {domain}...")

    for local_part, email, password in mailboxes:
        # 1. Hash the password
        print(f"  \U0001f511 Hashing password for {email}...")
        hashed = hash_password(password)

        # 2. Append to Dovecot users file
        validate_path(DOVECOT_USERS_FILE)
        append_unique_line(
            DOVECOT_USERS_FILE,
            f"{email}:{hashed}",
            track_rollback=True
        )

        # 3. Append to Postfix virtual_mailbox_maps
        validate_path(POSTFIX_VIRTUAL_MAILBOX_MAPS)
        append_unique_line(
            POSTFIX_VIRTUAL_MAILBOX_MAPS,
            f"{email}    {domain}/{local_part}/",
            track_rollback=True
        )

        # 4. Create the Maildir directory
        maildir_path = os.path.join(VMAIL_BASE, domain, local_part)
        validate_path(maildir_path)
        ensure_directory(maildir_path, track_rollback=True)

        if not DRY_RUN:
            # Create Maildir subdirectories
            for subdir in ["cur", "new", "tmp"]:
                sub_path = os.path.join(maildir_path, subdir)
                os.makedirs(sub_path, exist_ok=True)

            # Set ownership to vmail
            run(["chown", "-R", "5000:5000", maildir_path])

        print(f"  \u2705 Mailbox created: {email}")

    # 5. Rebuild Postfix lookup table
    run(["postmap", POSTFIX_VIRTUAL_MAILBOX_MAPS])

    # 6. Restart services
    run(["systemctl", "restart", "dovecot"])
    run(["systemctl", "restart", "postfix"])

    print(f"\n\u2705 Incoming mail configured for {domain}.")
    print("\n\U0001f4cb Thunderbird / Mail Client Settings:")
    print(f"   IMAP Server : {MX_HOSTNAME}")
    print(f"   IMAP Port   : 993 (SSL/TLS)")
    print(f"   SMTP Server : {MX_HOSTNAME}")
    print(f"   SMTP Port   : 587 (STARTTLS)")
    print(f"   Username    : Full email address (e.g. {mailboxes[0][1]})")
    print(f"   Auth        : Normal password")


# =========================================================
# PROJECT TYPES
# =========================================================
# Parker never creates or builds projects. It only assigns a project
# directory and wires nginx to it according to the project type.
#
#   kind "php"    -> public_html served through PHP-FPM
#   kind "static" -> public_html served as a static site / SPA (optional /api proxy)
#   kind "proxy"  -> whole site reverse-proxied to a local port
#                    (Next.js, Nuxt, Remix, Express, ...)

PROJECT_TYPES = {
    1: {"label": "PHP based Custom Site", "kind": "php"},
    2: {"label": "WordPress", "kind": "php"},
    3: {"label": "Static SPA (React / Vite build)", "kind": "static"},
    4: {"label": "Node.js app on a port (Next.js / Nuxt / Express)", "kind": "proxy"},
}

# Ports that must never be used as an application upstream.
RESERVED_PORTS = {
    8891: "OpenDKIM milter",
    8893: "OpenDMARC milter",
    9000: "Parker dashboard",
}

DEFAULT_APP_PORT = 3000

# Dependencies that mark a project as a server-side Node.js app.
NODE_SERVER_DEPENDENCIES = {
    "next", "nuxt", "@remix-run/node", "@remix-run/serve", "@sveltejs/kit",
    "express", "fastify", "koa", "@nestjs/core", "@hapi/hapi",
}
NODE_STATIC_DEPENDENCIES = {"vite", "react-scripts", "@vitejs/plugin-react"}

def project_kind(project_type):
    return PROJECT_TYPES[project_type]["kind"]

def read_package_dependencies(project_root):
    """Returns the dependency names of a package.json found in the project, if any."""
    for candidate in (project_root, os.path.join(project_root, "app")):
        pkg_path = os.path.join(candidate, "package.json")
        if not os.path.isfile(pkg_path):
            continue
        try:
            with open(pkg_path, "r") as f:
                pkg = json.load(f)
        except (OSError, ValueError):
            continue
        deps = set()
        for key in ("dependencies", "devDependencies"):
            section = pkg.get(key)
            if isinstance(section, dict):
                deps.update(section.keys())
        return deps
    return set()

def detect_project_type(project_root):
    """Best-effort guess of the project type from files already in the project directory."""
    public_html = os.path.join(project_root, "public_html")

    if os.path.exists(os.path.join(public_html, "wp-config.php")):
        return 2

    deps = read_package_dependencies(project_root)
    if deps & NODE_SERVER_DEPENDENCIES:
        return 4
    if deps & NODE_STATIC_DEPENDENCIES:
        return 3

    if os.path.isdir(public_html):
        try:
            if any(name.endswith(".php") for name in os.listdir(public_html)):
                return 1
        except OSError:
            pass

    return None

# =========================================================
# PROJECT METADATA, OWNERSHIP AND PORTS
# =========================================================

MANIFEST_NAME = ".parker.json"

def manifest_path(project_root):
    return os.path.join(project_root, MANIFEST_NAME)

def read_manifest(project_root):
    """What a previous Parker run recorded for this project ({} if nothing usable)."""
    try:
        with open(manifest_path(project_root), "r") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}

def write_manifest(plan):
    """Records the choices made for this project, so re-runs can default to them."""
    data = {
        "version": 1,
        "domain": plan.domain,
        "domains": plan.domains,
        "project_type": plan.project_type,
        "project_type_label": PROJECT_TYPES[plan.project_type]["label"],
        "project_root": plan.project_root,
        "public_html": plan.public_html,
        "port": plan.port,
        "updated": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    write_file(
        manifest_path(plan.project_root),
        json.dumps(data, indent=2) + "\n",
        track_rollback=True
    )

def resolve_owner(spec):
    """Turns "user" or "user:group" into (uid, gid). Raises ParkerError if unknown."""
    if not spec:
        return None

    user_name, _, group_name = spec.partition(":")

    try:
        user = pwd.getpwnam(user_name)
    except KeyError:
        raise ParkerError(f"Owner user '{user_name}' does not exist.")

    gid = user.pw_gid

    if group_name:
        try:
            gid = grp.getgrnam(group_name).gr_gid
        except KeyError:
            raise ParkerError(f"Owner group '{group_name}' does not exist.")

    return (user.pw_uid, gid)

def port_is_listening(port):
    """True if something accepts connections on 127.0.0.1:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0

def port_is_bindable(port):
    """True if nothing on this host currently holds the port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True

def suggest_free_port(exclude_domain=None, start=3000, span=500):
    """
    First port from `start` that no other nginx site proxies to, that is not
    reserved, and that nothing on this host is using right now.
    """
    taken = set(RESERVED_PORTS)

    for _, text in other_nginx_configs(exclude_domain or ""):
        taken.update(find_proxy_ports(text))

    for port in range(start, start + span):
        if port not in taken and port_is_bindable(port):
            return port

    return start

DNS_WAIT_TIMEOUT = 90
DNS_WAIT_INTERVAL = 5

def wait_for_dns(domains, timeout=None, interval=None):
    """
    Polls until every hostname resolves (instead of sleeping a fixed time), so
    certbot is not started before the new records are visible. Returns True if
    all resolved; False on timeout (the caller carries on: certbot retries).
    """
    timeout = DNS_WAIT_TIMEOUT if timeout is None else timeout
    interval = DNS_WAIT_INTERVAL if interval is None else interval

    if DRY_RUN:
        print(f"⏳ [DRY RUN] Would wait for DNS to resolve: {', '.join(domains)}")
        return True

    pending = set(domains)
    deadline = time.time() + timeout

    print(f"\n⏳ Waiting for DNS to resolve (up to {timeout}s): {', '.join(sorted(pending))}")

    while True:
        for name in sorted(pending):
            try:
                socket.getaddrinfo(name, 80, socket.AF_INET)
                pending.discard(name)
            except socket.gaierror:
                pass

        if not pending:
            print("✅ DNS resolves.")
            return True

        if time.time() >= deadline:
            print(f"⚠ Still not resolving after {timeout}s: {', '.join(sorted(pending))}")
            return False

        time.sleep(interval)

# =========================================================
# NGINX
# =========================================================

def acme_challenge_dir(project_root):
    """Directory (inside the project) where certbot drops HTTP-01 challenge files."""
    return os.path.join(project_root, ".well-known", "acme-challenge")

def php_snippet_path():
    if os.path.isabs(PHP_FPM_SNIPPET):
        return PHP_FPM_SNIPPET
    return os.path.join("/etc/nginx", PHP_FPM_SNIPPET)

def proxy_location(location, port, strip_prefix=False):
    """A reverse-proxy location block. strip_prefix drops the matched prefix (e.g. /api/)."""
    upstream = f"http://127.0.0.1:{port}" + ("/" if strip_prefix else "")
    return f"""    location {location} {{
        proxy_pass {upstream};

        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        proxy_cache_bypass $http_upgrade;

        # Long read timeout so idle WebSocket connections are not cut after 60s.
        proxy_connect_timeout 10s;
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;
    }}"""

GZIP_BLOCK = """    gzip on;
    gzip_vary on;
    gzip_proxied any;
    gzip_comp_level 5;
    gzip_min_length 256;
    gzip_types text/plain text/css text/xml text/javascript application/javascript
               application/json application/xml application/rss+xml application/wasm
               image/svg+xml font/ttf font/otf;"""

SECURITY_HEADERS_BLOCK = """    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;"""

def nginx_ipv6_enabled():
    if NGINX_IPV6 in ("1", "true", "yes", "on"):
        return True
    if NGINX_IPV6 in ("0", "false", "no", "off"):
        return False
    # auto: mirror what nginx does. Without IPv6 support socket() fails and
    # `listen [::]:80` would make nginx -t reject the whole config.
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM):
            return True
    except OSError:
        return False

def generate_nginx_config(
    domains,
    project_type,
    project_root,
    public_html=None,
    port=None,
    primary_domain=None,
    ipv6=None,
    security_headers=None
):
    """Renders the (HTTP only) server block. certbot adds the HTTPS parts afterwards."""

    kind = project_kind(project_type)
    server_names = " ".join(domains)
    log_name = primary_domain or domains[0]

    if ipv6 is None:
        ipv6 = nginx_ipv6_enabled()
    if security_headers is None:
        security_headers = NGINX_SECURITY_HEADERS

    listen = "    listen 80;"
    if ipv6:
        listen += "\n    listen [::]:80;"

    blocks = [f"""{listen}

    server_name {server_names};

    access_log /var/log/nginx/{log_name}.access.log;
    error_log  /var/log/nginx/{log_name}.error.log;

    client_max_body_size 100M;""",

    GZIP_BLOCK,

    # certbot --webroot writes challenges here, inside the project (not /var/www).
    f"""    # Let's Encrypt HTTP-01 challenge. The webroot lives in the project directory.
    location ^~ /.well-known/acme-challenge/ {{
        root {project_root};
        default_type "text/plain";
        try_files $uri =404;
    }}"""]

    # Proxied apps set their own security headers; adding ours would duplicate
    # (or conflict with) theirs, so only nginx-served sites get the baseline.
    if security_headers and kind != "proxy":
        blocks.append(SECURITY_HEADERS_BLOCK)

    if kind in ("php", "static"):
        blocks.append(f"""    root {public_html};

    index index.php index.html index.htm;""")

    if kind == "php":
        blocks.append("""    location / {
        try_files $uri $uri/ /index.php?$query_string;
    }""")
        blocks.append(f"    include {PHP_FPM_SNIPPET};")

    elif kind == "static":
        if port:
            blocks.append(proxy_location("/api/", port, strip_prefix=True))
        # Vite/CRA fingerprint their asset filenames, so they can be cached for a year.
        # A missing asset must 404 rather than fall back to index.html.
        blocks.append("""    location /assets/ {
        expires 1y;
        access_log off;
        try_files $uri =404;
    }""")
        blocks.append("""    location / {
        try_files $uri /index.html;
    }""")

    elif kind == "proxy":
        blocks.append(proxy_location("/", port))

    blocks.append(r"""    location ~ /\.ht {
        deny all;
    }""")

    # A proxied app serves its own favicon/robots.txt, so nginx must not intercept them.
    if kind != "proxy":
        blocks.append("""    location = /favicon.ico {
        access_log off;
        log_not_found off;
    }

    location = /robots.txt {
        access_log off;
        log_not_found off;
    }""")

    body = "\n\n".join(blocks)

    return f"""# Managed by Parker
# Project: {project_root}
server {{
{body}
}}
"""

def find_proxy_ports(config_text):
    return [int(p) for p in re.findall(r"proxy_pass\s+http://127\.0\.0\.1:(\d+)", config_text)]

def read_text(path):
    try:
        with open(path, "r") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""

def existing_proxy_port(domain):
    """Port this domain already proxies to (used as a default when re-provisioning)."""
    ports = find_proxy_ports(read_text(os.path.join(NGINX_SITES_AVAILABLE, f"{domain}.conf")))
    return ports[0] if ports else None

def other_nginx_configs(domain):
    """Enabled site configs belonging to other domains."""
    own = f"{domain}.conf"
    if not os.path.isdir(NGINX_SITES_ENABLED):
        return
    for name in sorted(os.listdir(NGINX_SITES_ENABLED)):
        if name == own or name.startswith("."):
            continue
        path = os.path.join(NGINX_SITES_ENABLED, name)
        if os.path.isfile(path):
            yield name, read_text(path)

def find_port_users(domain, port):
    """Other nginx configs that already proxy to this port."""
    return [name for name, text in other_nginx_configs(domain) if port in find_proxy_ports(text)]

def find_server_name_conflicts(domain, domains):
    """Other nginx configs that already answer for any of these hostnames."""
    wanted = set(domains)
    conflicts = {}
    for name, text in other_nginx_configs(domain):
        for match in re.findall(r"^\s*server_name\s+([^;]+);", text, flags=re.M):
            shared = wanted & set(match.split())
            if shared:
                conflicts.setdefault(name, set()).update(shared)
    return conflicts

def reload_nginx_quietly():
    """Used during rollback: reload nginx only if the (restored) config is valid."""
    test = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
    if test.returncode != 0:
        print(f" ⚠ nginx -t still fails after rollback, not reloading:\n{test.stderr.strip()}")
        return
    subprocess.run(["systemctl", "reload", "nginx"], check=True)

def cleanup_nginx_symlink(enabled_path):
    if os.path.islink(enabled_path):
        os.unlink(enabled_path)

def setup_nginx(domain, config):

    filename = f"{domain}.conf"

    available_path = os.path.join(NGINX_SITES_AVAILABLE, filename)
    enabled_path = os.path.join(NGINX_SITES_ENABLED, filename)

    if os.path.lexists(enabled_path):
        if not os.path.islink(enabled_path):
            raise ParkerError(f"{enabled_path} exists and is not a symlink; refusing to overwrite it.")
        if os.path.realpath(enabled_path) != os.path.realpath(available_path):
            raise ParkerError(
                f"{enabled_path} points to {os.path.realpath(enabled_path)}, "
                f"not {available_path}; refusing to change it."
            )

    # Rollback runs newest-first, so registering this first makes nginx reload
    # last, after the files below have been restored.
    rollback_stack.add(reload_nginx_quietly, label="Reload nginx with the previous configuration")

    write_file(
        available_path,
        config,
        track_rollback=True
    )

    if not os.path.lexists(enabled_path):
        if DRY_RUN:
            print(f"🔗 [DRY RUN] Would create symlink: {enabled_path} -> {available_path}")
        else:
            os.symlink(available_path, enabled_path)
            rollback_stack.add(cleanup_nginx_symlink, enabled_path, label=f"Remove Nginx symlink {enabled_path}")

    print("🧪 Testing nginx config...")

    run(["nginx", "-t"])

    print("🔄 Reloading nginx...")

    run(["systemctl", "reload", "nginx"])

# =========================================================
# CERTBOT
# =========================================================

SSL_ATTEMPTS = 3
SSL_RETRY_DELAY = 30
ACME_PROBE_ATTEMPTS = 5

ACME_PUBLIC_TIMEOUT = 8
CLOUDFLARE_ORIGIN_ERRORS = {
    520: "returned an unexpected response", 521: "refused the connection",
    522: "timed out connecting", 523: "was unreachable", 524: "timed out answering",
    525: "failed the TLS handshake", 526: "has an invalid TLS certificate",
}

def explain_public_response(status, headers):
    """
    Plain-language reading of what a plain-HTTP request for the challenge file returned when
    sent the way Let's Encrypt sends it (through DNS, and through Cloudflare if proxied).
    Returns None when the response is fine.
    """
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    server = headers.get("server", "")
    via_cloudflare = "cloudflare" in server.lower() or "cf-ray" in headers
    location = headers.get("location", "")

    if status in (301, 302, 303, 307, 308):
        if location.lower().startswith("https://"):
            who = "Cloudflare, e.g. 'Always Use HTTPS' or a redirect rule" if via_cloudflare else "a redirect rule"
            return (
                f"HTTP is redirected to HTTPS ({who}) before any certificate exists. Let's Encrypt follows "
                f"the redirect, and the HTTPS side cannot answer yet. Exempt /.well-known/acme-challenge/* "
                f"from the redirect (Cloudflare: Rules > Configuration Rules > turn off 'Always Use HTTPS' "
                f"for that path), or switch the records to DNS-only until the certificate exists."
            )
        return f"it redirects to {location or 'another URL'}; the challenge file must be served at that address too."

    if via_cloudflare and status in CLOUDFLARE_ORIGIN_ERRORS:
        return (
            f"Cloudflare answered {status}: the origin {CLOUDFLARE_ORIGIN_ERRORS[status]}. Check that the DNS "
            f"record points at THIS server and that port 80 is open to Cloudflare."
        )

    if status in (401, 403):
        who = "Cloudflare (WAF, bot or access rules?)" if via_cloudflare else (server or "the web server")
        return (
            f"{who} answered {status} (forbidden). Either a firewall/WAF/access rule blocks the request, or "
            f"nginx cannot read the challenge file: every directory on the path needs search permission "
            f"for the nginx user (check with: namei -l <project>/.well-known/acme-challenge)."
        )

    if status == 404:
        return (
            f"{'a server behind Cloudflare' if via_cloudflare else (server or 'a server')} answered 404: it "
            f"is reachable but does not have the challenge file. Most likely the name points at a "
            f"DIFFERENT server than this one (check the DNS record / CNAME_TARGET), or another nginx "
            f"site is answering for this name."
        )

    if status >= 500:
        return f"the server answered {status} (server error), so it cannot serve the challenge."

    return f"unexpected answer HTTP {status}."

def check_public_acme_path(domain, token):
    """
    Requests the challenge URL the way Let's Encrypt will: by name, over plain HTTP, through the
    system resolver, without following redirects. Returns (ok, advice).
    """
    url = f"http://{domain}/.well-known/acme-challenge/{token}"

    session = requests.Session()
    session.trust_env = False
    try:
        r = session.get(url, timeout=ACME_PUBLIC_TIMEOUT, allow_redirects=False)
    except requests.exceptions.Timeout:
        return False, (
            f"no answer from {domain} on port 80 (timed out). Is port 80 open to the internet "
            f"(firewall / cloud security group), and does the name point at this server?"
        )
    except requests.exceptions.ConnectionError as e:
        text = str(e).lower()
        if "name or service not known" in text or "nameresolution" in text or "no address" in text or "temporary failure" in text:
            return False, f"{domain} does not resolve (yet): there is no usable DNS record for this exact name."
        return False, f"could not connect to {domain} on port 80 ({type(e).__name__}): firewall, or the name points elsewhere."
    except requests.RequestException as e:
        return False, f"request to {domain} failed: {e}"

    if r.status_code == 200 and r.text == token:
        return True, None

    return False, explain_public_response(r.status_code, dict(r.headers))

def verify_acme_challenge_path(domains, project_root):
    """
    Pre-flight for HTTP-01. For every hostname:
      1. the LOCAL nginx must serve the challenge from the project directory, and
      2. the same file must be reachable BY NAME over HTTP, the way Let's Encrypt asks.
    Nothing here stops the run; it returns [(domain, advice)] for whatever looks wrong, so the
    cause is known (and reported) before certbot spends one of Let's Encrypt's limited
    failed-validation attempts.
    """
    challenge_dir = acme_challenge_dir(project_root)

    if DRY_RUN:
        for d in domains:
            print(f"🔎 [DRY RUN] Would verify http://{d}/.well-known/acme-challenge/ is served from {challenge_dir} and reachable by name")
        return []

    token = f"parker-check-{uuid.uuid4().hex}"
    probe = os.path.join(challenge_dir, token)
    problems = []

    try:
        with open(probe, "w") as f:
            f.write(token)
        os.chmod(probe, 0o644)

        session = requests.Session()
        session.trust_env = False  # never route this loopback probe through a proxy

        for domain in domains:
            outcome = "no response"
            local_ok = False

            # A reload returns before the new workers take over, so retry briefly.
            for attempt in range(ACME_PROBE_ATTEMPTS):
                if attempt:
                    time.sleep(1)
                try:
                    r = session.get(
                        f"http://127.0.0.1/.well-known/acme-challenge/{token}",
                        headers={"Host": domain},
                        timeout=5,
                        allow_redirects=False
                    )
                except requests.RequestException as e:
                    outcome = str(e)
                    continue

                if r.status_code == 200 and r.text == token:
                    local_ok = True
                    break

                outcome = f"HTTP {r.status_code}"

            if not local_ok:
                problems.append((domain, (
                    f"nginx on THIS server does not serve the challenge file for {domain} ({outcome}). "
                    f"Check the server block and that the nginx user can read {challenge_dir} "
                    f"(namei -l {challenge_dir})."
                )))
                print(f"⚠ {domain}: local nginx does not serve the challenge ({outcome})")
                continue

            ok, advice = check_public_acme_path(domain, token)
            if ok:
                print(f"✅ {domain}: the challenge is reachable by name, as Let's Encrypt will request it")
            else:
                problems.append((domain, advice))
                print(f"⚠ {domain}: not reachable the way Let's Encrypt will request it: {advice}")
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass

    return problems

def certbot_command(domains, project_root):
    # webroot authenticator: challenges go to <project>/.well-known/acme-challenge.
    # nginx installer: certbot still adds the HTTPS server block and redirect.
    cmd = [
        "certbot", "run",
        "--authenticator", "webroot",
        "--installer", "nginx",
        "--webroot-path", project_root,
        "--redirect",
        "--expand",
        "--agree-tos",
        "--non-interactive",
    ]

    if DEFAULT_SSL_EMAIL:
        cmd.extend(["-m", DEFAULT_SSL_EMAIL])
    else:
        cmd.append("--register-unsafely-without-email")

    for d in domains:
        cmd.extend(["-d", d])

    return cmd

CERTBOT_PROBLEM_RE = re.compile(
    r"Domain:\s*(?P<domain>\S+)\s*\n\s*Type:\s*(?P<type>\S+)\s*\n\s*Detail:\s*(?P<detail>.+?)(?:\n\s*\n|\Z)",
    re.S,
)

def parse_certbot_failures(output):
    """
    What Let's Encrypt actually reported, from certbot's output:
    [{"domain", "type", "detail"}]. Rate limits arrive in a different shape and are added too.
    """
    failures = [
        {"domain": m["domain"], "type": m["type"].lower(), "detail": " ".join(m["detail"].split())}
        for m in CERTBOT_PROBLEM_RE.finditer(output or "")
    ]

    limit = re.search(r"(too many (failed authorizations|certificates|requests)[^\n]*|rateLimited[^\n]*)", output or "", re.I)
    if limit:
        failures.append({"domain": "(account)", "type": "ratelimited", "detail": " ".join(limit.group(0).split())})

    return failures

def explain_acme_detail(failure):
    """A short, plain-language reading of one reported problem (None if nothing useful to add)."""
    detail = failure["detail"].lower()
    kind = failure["type"]

    if kind == "ratelimited" or "too many" in detail or "ratelimited" in detail:
        return ("Let's Encrypt is rate limiting this name (about 5 failed validations per hour). Fix the cause, "
                "wait, and test with the --dry-run command below, which does not count.")
    if kind == "dns" or "nxdomain" in detail or "no valid a" in detail or "servfail" in detail:
        return ("no DNS record is visible for this exact name (yet). Make sure the record exists "
                "(including www) and give DNS a moment.")
    if "caa" in detail:
        return "a CAA DNS record forbids Let's Encrypt from issuing for this name."
    if "timeout" in detail or "connection refused" in detail or "connection reset" in detail or "no route" in detail:
        return ("Let's Encrypt could not connect to port 80. Open it in the firewall / cloud security group, "
                "and check the name points at this server.")
    if "invalid response" in detail or kind == "unauthorized":
        match = re.search(r"\b(30[1278]|40[134]|5\d\d)\b", detail)
        status = int(match.group(1)) if match else None
        if status:
            return explain_public_response(status, {"location": "https://" if status in (301, 302, 303, 307, 308) else ""})
    return None

def is_retryable(failures):
    """
    Retrying only helps while DNS is still propagating. A deterministic failure (redirect, 403, 404,
    unreachable) fails identically every time, and each failed validation counts against Let's
    Encrypt's small hourly limit: retrying would lock the name out for an hour.
    Unknown (nothing parsed) is retried, as it is more likely transient than a validation failure.
    """
    if not failures:
        return True
    return all(
        f["type"] == "dns" or "nxdomain" in f["detail"].lower() or "servfail" in f["detail"].lower()
        for f in failures
    )

def format_failures(failures):
    lines = []
    for f in failures:
        lines.append(f"      - {f['domain']}: {f['detail']}")
        hint = explain_acme_detail(f)
        if hint:
            lines.append(f"        -> {hint}")
    return "\n".join(lines)

def certbot_test_command(domains, project_root):
    """Authentication-only dry run against Let's Encrypt's STAGING server: free of the hourly limit."""
    cmd = ["certbot", "certonly", "--dry-run", "--webroot", "--webroot-path", project_root,
           "--agree-tos", "--non-interactive"]
    cmd += ["-m", DEFAULT_SSL_EMAIL] if DEFAULT_SSL_EMAIL else ["--register-unsafely-without-email"]
    for d in domains:
        cmd += ["-d", d]
    return cmd

def setup_ssl(domains, project_root):
    """
    Obtains and installs the certificate. Returns (ok, failures), failures being what Let's
    Encrypt reported. Retries only while the failure can still be DNS propagation.
    """

    if not command_exists("certbot"):
        print("⚠ Certbot not installed.")
        return False, []

    cmd = certbot_command(domains, project_root)
    failures = []

    for attempt in range(1, SSL_ATTEMPTS + 1):
        returncode, output = run_capture(cmd)

        if returncode == 0:
            return True, []

        failures = parse_certbot_failures(output)

        if not is_retryable(failures):
            print("\n⚠ certbot failed for a reason that retrying cannot fix; not retrying "
                  "(each failed attempt counts against Let's Encrypt's hourly limit).")
            break

        if attempt < SSL_ATTEMPTS:
            print(f"\n⚠ certbot failed (attempt {attempt}/{SSL_ATTEMPTS}). Retrying in {SSL_RETRY_DELAY}s...")
            if not DRY_RUN:
                time.sleep(SSL_RETRY_DELAY)

    return False, failures


# =========================================================
# PLANNING (prompts + read-only checks, nothing is modified)
# =========================================================

DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$"
)

def is_valid_domain(value):
    return bool(DOMAIN_RE.match(value))

PROJECT_TYPE_ALIASES = {
    "php": 1,
    "wordpress": 2, "wp": 2,
    "static": 3, "spa": 3, "vite": 3, "react": 3,
    "node": 4, "nodejs": 4, "next": 4, "nextjs": 4, "nuxt": 4, "proxy": 4,
}

@dataclass
class ProvisionPlan:
    domain: str
    root_domain: str
    subdomain_part: Optional[str]
    domains: list
    project_type: int = 0
    project_root: str = ""
    public_html: Optional[str] = None
    port: Optional[int] = None          # whole site (proxy) or /api/ backend (static)
    owner_spec: str = ""                # "user[:group]" for newly created directories
    owner: Optional[tuple] = None       # resolved (uid, gid)
    zone: Optional[dict] = None
    create_zone: bool = False
    configure_mail_dns: bool = False
    setup_incoming: bool = False
    mailboxes: list = field(default_factory=list)
    skip_ssl: bool = False
    had_ssl: bool = False
    warnings: list = field(default_factory=list)

    @property
    def kind(self):
        return project_kind(self.project_type)

    @property
    def dns_enabled(self):
        return bool(self.zone or self.create_zone)

def decline(message, hint=None):
    """
    Stops the run at a confirmation that was declined. Interactively that is a
    clean exit; in --yes mode it is an error, so automation notices.
    """
    if NON_INTERACTIVE:
        raise ParkerError(f"{message}{' ' + hint if hint else ''}")

    print("\n👋 Exiting. No changes were made.")
    sys.exit(0)

def prompt_validated(question, validate, default=None, preset=None):
    """
    Asks until validate(raw) accepts. validate returns the value or raises
    ValueError(message). A preset (from a command-line flag) is validated once and
    a bad one is an error; so is any invalid answer in --yes mode.
    """
    if preset is not None:
        try:
            return validate(str(preset))
        except ValueError as e:
            raise ParkerError(f"{question}: {e}")

    while True:
        raw = ask(question, default=default)

        try:
            return validate(raw)
        except ValueError as e:
            if NON_INTERACTIVE:
                raise ParkerError(f"{question}: {e}")
            print(f"⚠ {e}")

def parse_domain(raw):
    value = raw.lower().strip()
    if not is_valid_domain(value):
        raise ValueError("Invalid domain. Use a full hostname such as example.com or app.example.com.")
    return value

def parse_project_type(raw):
    key = raw.strip().lower()
    if key.isdigit() and int(key) in PROJECT_TYPES:
        return int(key)
    if key in PROJECT_TYPE_ALIASES:
        return PROJECT_TYPE_ALIASES[key]
    raise ValueError(f"Enter a number between 1 and {len(PROJECT_TYPES)}.")

def parse_port(raw):
    if not raw.isdigit() or not 1024 <= int(raw) <= 65535:
        raise ValueError("Enter a port between 1024 and 65535.")

    port = int(raw)

    if port in RESERVED_PORTS:
        raise ValueError(f"Port {port} is used by {RESERVED_PORTS[port]}. Choose another port.")

    return port

def parse_project_dir(raw):
    base = os.path.abspath(BASE_DIR)
    path = os.path.abspath(raw)

    if not path.startswith(base + os.sep):
        raise ValueError(f"The project directory must be inside {base} (and not {base} itself).")

    if os.path.exists(path) and not os.path.isdir(path):
        raise ValueError(f"{path} exists and is not a directory.")

    return path

def prompt_port(question, default=None, exclude_domain=None, preset=None, force=False):
    """Asks for a local TCP port; a port another site already proxies to needs confirmation."""
    while True:
        port = prompt_validated(
            question,
            parse_port,
            default=str(default) if default else None,
            preset=preset
        )

        users = find_port_users(exclude_domain, port) if exclude_domain else []

        if not users or force:
            return port

        print(f"⚠ Port {port} is already proxied to by: {', '.join(users)}")

        if ask_yes_no("Use it anyway?", default="n"):
            return port

        if NON_INTERACTIVE or preset is not None:
            raise ParkerError(f"Port {port} is already used by {', '.join(users)}. Use --force to share it.")

        preset = None

def prompt_project_type(detected_type, preset=None):
    print("\nProject Types:")
    for number, info in PROJECT_TYPES.items():
        print(f"{number}. {info['label']}")

    return prompt_validated(
        "Choose project type",
        parse_project_type,
        default=str(detected_type) if detected_type else None,
        preset=preset
    )

def prompt_project_dir(domain, preset=None):
    return prompt_validated(
        "Project directory",
        parse_project_dir,
        default=os.path.join(BASE_DIR, domain),
        preset=preset
    )

def plan_domains(args):
    log_step("Step 1: Domain Analysis", "Collecting and analyzing domain details...")

    domain = prompt_validated("Enter domain or subdomain", parse_domain, preset=args.domain)

    root_domain = extract_root_domain(domain)
    subdomain_part = get_subdomain_part(domain)

    if args.www is not None:
        use_www = args.www
    else:
        use_www = ask_yes_no(
            "Add www variant too?",
            default="n" if subdomain_part else "y"
        )

    domains = [domain]

    if use_www and not domain.startswith("www."):
        domains.append(f"www.{domain}")

    print(f"\nRoot Domain : {root_domain}")
    print(f"Subdomain   : {subdomain_part or 'None'}")
    print(f"Targets     : {', '.join(domains)}")

    plan = ProvisionPlan(
        domain=domain,
        root_domain=root_domain,
        subdomain_part=subdomain_part,
        domains=domains
    )

    # Early detection: check if this domain is already parked
    parked_indicators = check_existing_parking(domain)
    if parked_indicators:
        print("\n⚠ This domain appears to already be parked:")
        for indicator in parked_indicators:
            print(indicator)
        if not args.force and not ask_yes_no("\nProceed with re-provisioning anyway?", default="n"):
            decline("This domain is already parked.", "Use --force to re-provision it.")

    plan.had_ssl = os.path.exists(os.path.join(LETSENCRYPT_LIVE, domain))

    conflicts = find_server_name_conflicts(domain, domains)
    if conflicts:
        print("\n⚠ Other nginx sites already answer for these hostnames:")
        for name, hosts in conflicts.items():
            print(f"  - {name}: {', '.join(sorted(hosts))}")
        if not args.force and not ask_yes_no("Continue anyway? (nginx will ignore the duplicate names)", default="n"):
            decline("Other nginx sites already use these hostnames.", "Use --force to continue.")

    return plan

def plan_dns(plan, args):
    """Read-only Cloudflare lookup; decides whether DNS/mail-DNS steps will run."""
    log_step("Step 2: Cloudflare Lookup", "Checking DNS zone on Cloudflare (read-only)...")

    reason = None

    if args.no_dns:
        print("ℹ DNS skipped (--no-dns).")
        plan.warnings.append("DNS was not configured (--no-dns).")
        return

    if not CLOUDFLARE_API_TOKEN:
        reason = "CLOUDFLARE_API_TOKEN is not set."
    else:
        try:
            zone, zone_name = find_zone(CloudflareManager(), plan.domain)
        except ParkerError as e:
            reason = f"Cloudflare lookup failed: {e}"
        else:
            if zone:
                print(f"\n✅ Cloudflare Zone Found: {zone_name}")
                plan.zone = zone
                # The zone, not a guess, defines what the root domain is.
                plan.root_domain = zone_name
                plan.subdomain_part = get_subdomain_part(plan.domain, zone_name)
                require_settings("CNAME_TARGET", needed_for="creating DNS records",
                                 instead="Or pass --no-dns to skip DNS.")
                return

            print("\n⚠ Zone not found in Cloudflare.")

            if plan.subdomain_part:
                reason = "Cannot create DNS for a subdomain because the parent zone does not exist."
            elif NON_INTERACTIVE:
                # Creating a zone needs a manual nameserver change part-way through.
                reason = "Zone not found, and a zone cannot be created without interaction."
            elif ask_yes_no("Create new Cloudflare zone?"):
                require_settings("CNAME_TARGET", needed_for="creating DNS records",
                                 instead="Or pass --no-dns to skip DNS.")
                plan.create_zone = True
                return
            else:
                reason = "No Cloudflare zone will be created."

    print(f"⚠ {reason}")
    if not ask_yes_no("Continue without DNS changes?", default="n"):
        decline(reason, "Use --no-dns to continue without DNS changes.")

    plan.warnings.append(f"DNS was not configured: {reason}")

def plan_project(plan, args):
    log_step("Step 3: Project Directory", "Assigning the project directory and site type (nothing is created or built)...")

    default_root = os.path.join(BASE_DIR, plan.domain)

    manifest = read_manifest(default_root)
    detected_type = None
    previous_port = None

    if manifest.get("project_type") in PROJECT_TYPES:
        detected_type = manifest["project_type"]
        print(f"\n✅ Found Parker project record in {default_root}")
    elif os.path.isdir(default_root) and not is_directory_empty(default_root):
        print(f"\n✅ Existing project found at {default_root}")
        detected_type = detect_project_type(default_root)

    if detected_type:
        print(f"🔍 Previous/detected project type: {PROJECT_TYPES[detected_type]['label']}")

    if isinstance(manifest.get("port"), int):
        previous_port = manifest["port"]

    previous_port = existing_proxy_port(plan.domain) or previous_port

    plan.project_type = prompt_project_type(detected_type, preset=args.project_type)
    plan.project_root = prompt_project_dir(plan.domain, preset=args.dir)

    if plan.kind in ("php", "static"):
        plan.public_html = os.path.join(plan.project_root, "public_html")

    if plan.kind == "proxy":
        plan.port = prompt_port(
            "Port your app listens on",
            default=previous_port or suggest_free_port(plan.domain, start=DEFAULT_APP_PORT),
            exclude_domain=plan.domain,
            preset=args.port,
            force=args.force
        )
    elif plan.kind == "static":
        wants_api = args.port is not None or ask_yes_no(
            "Proxy /api/ to a backend service (e.g. Express)?",
            default="y" if previous_port else "n"
        )
        if wants_api:
            plan.port = prompt_port(
                "Backend port",
                default=previous_port or suggest_free_port(plan.domain, start=4000),
                exclude_domain=plan.domain,
                preset=args.port,
                force=args.force
            )
    elif args.port is not None:
        plan.warnings.append("--port was ignored: PHP/WordPress projects do not use a port.")

    plan.owner_spec = args.owner or PROJECT_OWNER
    plan.skip_ssl = args.no_ssl

def plan_mail(plan, args):
    if not ENABLE_MAIL_SETUP or not plan.dns_enabled:
        return

    if plan.subdomain_part:
        print("\nℹ Subdomain detected. Skipping mail authentication (root domain setup only).")
        return

    log_step("Step 3b: Mail Options", "Choosing mail authentication and mailboxes...")

    # In --yes mode mail is opt-in via --mail-dns; interactively it defaults to yes.
    if args.mail_dns is not None:
        wants_mail = args.mail_dns
    elif NON_INTERACTIVE:
        wants_mail = False
    else:
        wants_mail = ask_yes_no("Configure SPF/DKIM/DMARC?")

    if not wants_mail:
        return

    require_settings("MAIL_HOSTNAME", "DKIM_SELECTOR", needed_for="mail authentication (SPF/DKIM/DMARC/MX)",
                     instead="Or skip mail setup (answer no / --no-mail-dns). MX_HOSTNAME is optional: it defaults to MAIL_HOSTNAME.")

    plan.configure_mail_dns = True

    # Mailboxes need passwords, so they are only ever collected interactively.
    if not NON_INTERACTIVE and ask_yes_no("Set up incoming mail (IMAP mailboxes)?", default="n"):
        plan.setup_incoming = True
        plan.mailboxes = collect_mailboxes(plan.root_domain)

def preflight(plan):
    """Fails fast, before any change, on anything that would break the run midway."""
    errors = []

    if not command_exists("nginx"):
        errors.append("nginx is not installed.")

    for directory in (NGINX_SITES_AVAILABLE, NGINX_SITES_ENABLED):
        if not os.path.isdir(directory):
            errors.append(f"{directory} does not exist.")

    if plan.kind == "php" and not os.path.isfile(php_snippet_path()):
        errors.append(
            f"PHP-FPM snippet {php_snippet_path()} not found "
            f"(set PHP_FPM_SNIPPET in .env or create the snippet)."
        )

    if plan.configure_mail_dns and not command_exists("opendkim-genkey"):
        errors.append("opendkim-genkey not found (install opendkim-tools) but mail authentication was requested.")

    if plan.mailboxes:
        for tool in ("doveadm", "postmap"):
            if not command_exists(tool):
                errors.append(f"{tool} not found but incoming mailboxes were requested.")

    try:
        plan.owner = resolve_owner(plan.owner_spec)
    except ParkerError as e:
        errors.append(str(e))

    notes = []

    if plan.skip_ssl:
        notes.append("SSL will be skipped (--no-ssl).")
    else:
        if not command_exists("certbot"):
            notes.append("certbot is not installed; SSL will be skipped.")

        if not DEFAULT_SSL_EMAIL:
            notes.append("DEFAULT_SSL_EMAIL is not set; certbot will register without an email address.")

    if errors and DRY_RUN:
        notes.extend(f"(dry run) {error}" for error in errors)
        errors = []

    if errors:
        print("\n❌ Preflight checks failed:")
        for error in errors:
            print(f"  - {error}")
        raise ParkerError("Fix the problems above and run Parker again. No changes were made.")

    for note in notes:
        print(f"⚠ {note}")

def show_plan(plan):
    print("\n----------------------------------------")
    print(" Review")
    print("----------------------------------------")
    print(f"Domains        : {', '.join(plan.domains)}")
    print(f"Project type   : {PROJECT_TYPES[plan.project_type]['label']}")
    print(f"Project dir    : {plan.project_root}")

    if plan.public_html:
        print(f"Document root  : {plan.public_html}")

    if plan.owner_spec:
        print(f"Owner          : {plan.owner_spec}  (newly created directories)")

    if plan.kind == "proxy":
        print(f"Upstream       : http://127.0.0.1:{plan.port}  (all requests)")
    elif plan.port:
        print(f"API upstream   : http://127.0.0.1:{plan.port}  (/api/)")

    print(f"ACME webroot   : {plan.project_root}  (.well-known/acme-challenge)")
    print(f"Nginx config   : {os.path.join(NGINX_SITES_AVAILABLE, plan.domain + '.conf')}")

    if plan.zone:
        print("DNS            : existing Cloudflare zone")
    elif plan.create_zone:
        print("DNS            : create new Cloudflare zone")
    else:
        print("DNS            : not configured")

    if plan.configure_mail_dns:
        print(f"Mail           : SPF/DKIM/DMARC/MX for {plan.root_domain}")
    if plan.mailboxes:
        print(f"Mailboxes      : {', '.join(m[1] for m in plan.mailboxes)}")

    ssl_label = "skipped (--no-ssl)" if plan.skip_ssl else "Let's Encrypt via certbot"
    print(f"SSL            : {ssl_label}")

# =========================================================
# EXECUTION
# =========================================================

def setup_project_directories(plan):
    ensure_directory(plan.project_root, track_rollback=True, mode=0o755, owner=plan.owner)

    if plan.public_html:
        ensure_directory(plan.public_html, track_rollback=True, mode=0o755, owner=plan.owner)

    ensure_directory(acme_challenge_dir(plan.project_root), track_rollback=True, mode=0o755, owner=plan.owner)

def setup_dns(plan):
    cf = CloudflareManager()
    zone = plan.zone

    if plan.create_zone:
        zone = cf.create_zone(plan.root_domain)

        if not zone:
            raise ParkerError("Cloudflare zone could not be created.")

        print("\n✅ Zone created.")
        print("\n⚠ Update Nameservers To:\n")

        for ns in zone["name_servers"]:
            print(f" - {ns}")

        print("")
        ask("Press Enter once you have updated the nameservers at your domain registrar")

        # From here the registrar points at this zone; deleting it on a later
        # failure would take the domain offline, so it must survive a rollback.
        rollback_stack.remove(zone.get("_rollback_task"))

    zone_id = zone["id"]

    print("\n🌐 Creating DNS records...\n")

    for d in plan.domains:
        cf.create_site_cname_record(
            zone_id=zone_id,
            zone_name=plan.root_domain,
            record_name=d,
            content=CNAME_TARGET,
            proxied=True
        )

    if plan.configure_mail_dns:
        log_step("Mail Authentication", "Setting up SPF, DKIM, and DMARC for email reliability...")
        setup_mail_dns(cf, zone_id, plan.root_domain)

        if plan.setup_incoming:
            log_step("Incoming Mail", "Provisioning IMAP mailboxes via Dovecot...")
            setup_incoming_mail(plan.root_domain, plan.mailboxes)

def provision(plan):
    log_step("Step 4: Project Directory Setup", "Creating the project directory (existing files are never touched)...")
    setup_project_directories(plan)
    write_manifest(plan)

    log_step("Step 5: Nginx Configuration", "Generating and deploying the Nginx server block...")

    nginx_config = generate_nginx_config(
        domains=plan.domains,
        project_type=plan.project_type,
        project_root=plan.project_root,
        public_html=plan.public_html,
        port=plan.port
    )

    if DRY_RUN:
        print("\n[DRY RUN] Generated nginx config:\n")
        print(nginx_config)

    setup_nginx(plan.domain, nginx_config)

    if plan.dns_enabled:
        log_step("Step 6: DNS & Mail", "Configuring Cloudflare DNS records and mail...")
        setup_dns(plan)

    log_step("Step 7: SSL Certificate Setup", "Securing the site with Let's Encrypt SSL...")

    if plan.skip_ssl:
        print("ℹ SSL skipped (--no-ssl).")
        return False

    if plan.dns_enabled:
        wait_for_dns(plan.domains)

    precheck = verify_acme_challenge_path(plan.domains, plan.project_root)

    print("\n🔐 Setting up SSL...\n")

    ssl_ok, failures = setup_ssl(plan.domains, plan.project_root)

    if not ssl_ok:
        manual = " ".join(shlex.quote(part) for part in certbot_command(plan.domains, plan.project_root))
        test = " ".join(shlex.quote(part) for part in certbot_test_command(plan.domains, plan.project_root))
        reported = format_failures(failures)

        if plan.had_ssl:
            # The site was already serving HTTPS; never leave it downgraded.
            raise ParkerError(
                "certbot failed for a domain that already had a certificate. "
                "Rolling back so the existing HTTPS configuration is preserved."
                + (f"\n  Let's Encrypt said:\n{reported}" if reported else "")
            )

        message = ["SSL was NOT configured; the site is served over HTTP only."]
        if reported:
            message.append("Let's Encrypt said:\n" + reported)
        if precheck:
            message.append("Parker's pre-check found:\n" + "\n".join(
                f"      - {domain}: {advice}" for domain, advice in precheck))
        message.append(
            "Once fixed, test WITHOUT using Let's Encrypt's failed-validation limit:\n"
            f"      sudo {test}\n"
            f"    then issue and install the certificate:\n      sudo {manual}"
        )
        plan.warnings.append("\n    ".join(message))

    return ssl_ok

def print_summary(plan, ssl_ok):
    print("\n========================================")
    if plan.warnings:
        print(" ⚠ Setup Completed With Warnings")
    else:
        print(" ✅ Setup Completed Successfully")
    print("========================================\n")

    print(f"Project Root : {plan.project_root}")

    if plan.public_html:
        print(f"Public Root  : {plan.public_html}")

    if plan.kind == "proxy":
        print(f"Upstream     : http://127.0.0.1:{plan.port}")

    print(f"ACME Webroot : {plan.project_root}")
    print(f"Domains      : {', '.join(plan.domains)}")

    if DRY_RUN:
        ssl_status = "would be requested (dry run)"
    elif plan.skip_ssl:
        ssl_status = "skipped (--no-ssl)"
    else:
        ssl_status = "enabled" if ssl_ok else "not configured"
    print(f"SSL          : {ssl_status}")

    if plan.port and not DRY_RUN:
        if port_is_listening(plan.port):
            print(f"App          : ✅ responding on 127.0.0.1:{plan.port}")
        else:
            print(f"App          : nothing is listening on 127.0.0.1:{plan.port} yet (nginx answers 502 until it is)")

    if plan.kind == "proxy":
        print(
            f"\nNext: deploy your app into {plan.project_root} and start it on port {plan.port} "
            f"(bind to 127.0.0.1)."
        )
    elif plan.kind == "php" and plan.project_type == 2:
        print(f"\nNext: place WordPress in {plan.public_html}.")
    elif plan.public_html:
        print(f"\nNext: deploy your site files into {plan.public_html}.")

    if plan.warnings:
        print("\nWarnings:")
        for warning in plan.warnings:
            print(f"  - {warning}")

def run_provisioning(args):
    # Before the first question: every run assigns project directories.
    require_settings("WEBROOT", needed_for="assigning project directories")

    # Phase 1: ask everything and validate everything. Nothing is modified.
    plan = plan_domains(args)
    plan_dns(plan, args)
    plan_project(plan, args)
    plan_mail(plan, args)
    preflight(plan)
    show_plan(plan)

    if not ask_yes_no("\nProceed with these settings?"):
        decline("Not confirmed.")

    # Phase 2: apply. Any failure rolls back everything done in this run.
    ssl_ok = provision(plan)

    print_summary(plan, ssl_ok)

    rollback_stack.cleanup()

# =========================================================
# LIST / REMOVE
# =========================================================

MANAGED_MARKER = "# Managed by Parker"

def read_site_info(domain):
    """What we know about an existing Parker-managed site, from its nginx config + manifest."""
    conf = os.path.join(NGINX_SITES_AVAILABLE, f"{domain}.conf")
    text = read_text(conf)

    project = re.search(r"^# Project: (.+)$", text, flags=re.M)
    project_root = project.group(1).strip() if project else ""

    names = sorted({
        name
        for group in re.findall(r"^\s*server_name\s+([^;]+);", text, flags=re.M)
        for name in group.split()
    })

    manifest = read_manifest(project_root) if project_root else {}
    type_id = manifest.get("project_type")
    ports = find_proxy_ports(text)

    return {
        "domain": domain,
        "conf": conf,
        "exists": os.path.exists(conf),
        "managed": MANAGED_MARKER in text,
        "enabled": os.path.islink(os.path.join(NGINX_SITES_ENABLED, f"{domain}.conf")),
        "project_root": project_root,
        "names": names,
        "type": PROJECT_TYPES[type_id]["label"] if type_id in PROJECT_TYPES else "unknown",
        "port": ports[0] if ports else None,
        "ssl": os.path.exists(os.path.join(LETSENCRYPT_LIVE, domain)),
    }

def list_sites():
    """Prints every Parker-managed site. Read-only (no root needed)."""
    sites = []

    if os.path.isdir(NGINX_SITES_AVAILABLE):
        for name in sorted(os.listdir(NGINX_SITES_AVAILABLE)):
            if name.endswith(".conf"):
                info = read_site_info(name[:-len(".conf")])
                if info["managed"]:
                    sites.append(info)

    if not sites:
        print("No Parker-managed sites found.")
        return

    print(f"{'DOMAIN':32} {'TYPE':50} {'PORT':6} {'ENABLED':8} {'SSL':4} PROJECT")
    for info in sites:
        print(
            f"{info['domain']:32} {info['type']:50} {str(info['port'] or '-'):6} "
            f"{'yes' if info['enabled'] else 'NO':8} {'yes' if info['ssl'] else 'no':4} "
            f"{info['project_root'] or '-'}"
        )

def remove_nginx_site(domain):
    """Removes the site's config and symlink; restores both if nginx rejects the result."""
    available = os.path.join(NGINX_SITES_AVAILABLE, f"{domain}.conf")
    enabled = os.path.join(NGINX_SITES_ENABLED, f"{domain}.conf")

    if os.path.lexists(enabled) and not os.path.islink(enabled):
        raise ParkerError(f"{enabled} is not a symlink; refusing to remove it.")

    # Registered first so nginx reloads last, after both files are restored.
    rollback_stack.add(reload_nginx_quietly, label="Reload nginx with the previous configuration")

    if os.path.islink(enabled):
        target = os.readlink(enabled)
        if DRY_RUN:
            print(f"🔗 [DRY RUN] Would remove symlink: {enabled}")
        else:
            os.unlink(enabled)
            rollback_stack.add(os.symlink, target, enabled, label=f"Restore nginx symlink {enabled}")

    backup = f"{available}.phoenix_bak"
    if DRY_RUN:
        print(f"🗑 [DRY RUN] Would remove: {available}")
    else:
        shutil.copy2(available, backup)
        rollback_stack.add_backup(backup)
        rollback_stack.add(restore_backup, backup, available, label=f"Restore {available}")
        os.remove(available)

    print("🧪 Testing nginx config...")
    run(["nginx", "-t"])

    print("🔄 Reloading nginx...")
    run(["systemctl", "reload", "nginx"])

def remove_certificate(domain):
    """Deletes the Let's Encrypt lineage so renewals stop. Returns True on success."""
    if not command_exists("certbot"):
        print("⚠ certbot not installed; certificate not deleted.")
        return False

    result = run(["certbot", "delete", "--cert-name", domain, "--non-interactive"], check=False)
    return result.returncode == 0

def remove_dns_records(domain, names):
    """Deletes the CNAMEs Parker creates (pointing at CNAME_TARGET). Nothing else."""
    cf = CloudflareManager()
    zone, _ = find_zone(cf, domain)

    if not zone:
        print("⚠ Cloudflare zone not found; no DNS records removed.")
        return

    for name in names:
        for record in cf.list_dns_records(zone["id"], name) or []:
            if record.get("type") == "CNAME" and (record.get("content") or "").lower() == CNAME_TARGET.lower():
                if DRY_RUN:
                    print(f"🌐 [DRY RUN] Would delete DNS record: CNAME {name}")
                elif cf.delete_dns_record(zone["id"], record["id"]):
                    print(f"✅ Deleted DNS record: CNAME {name}")
                else:
                    print(f"⚠ Could not delete DNS record: CNAME {name}")

def remove_site(args):
    domain = parse_domain(args.remove)
    info = read_site_info(domain)

    if not info["exists"]:
        raise ParkerError(f"No nginx config found for {domain} ({info['conf']}).")

    if not info["managed"] and not args.force:
        raise ParkerError(f"{info['conf']} was not created by Parker. Use --force to remove it anyway.")

    log_step("Remove Site", f"Removing {domain} (project files are never deleted)...")

    names = info["names"] or [domain]

    print(f"\nNginx config : {info['conf']}")
    print(f"Hostnames    : {', '.join(names)}")
    print(f"Project dir  : {info['project_root'] or '-'}  (left untouched)")

    delete_cert = False
    if info["ssl"] and not args.keep_cert:
        delete_cert = ask_yes_no("Also delete the Let's Encrypt certificate?", default="y")

    delete_dns = args.remove_dns
    if not delete_dns and CLOUDFLARE_API_TOKEN and CNAME_TARGET and not NON_INTERACTIVE:
        delete_dns = ask_yes_no(f"Also delete the Cloudflare CNAME records (-> {CNAME_TARGET})?", default="n")

    if delete_dns:
        # Only Parker's own CNAMEs (those pointing at CNAME_TARGET) may be deleted; without it
        # they cannot be told apart from anyone else's. Checked before anything is removed.
        require_settings("CNAME_TARGET", needed_for="removing DNS records")

    if not NON_INTERACTIVE and not ask_yes_no("\nProceed with removal?", default="n"):
        decline("Removal not confirmed.")

    remove_nginx_site(domain)

    warnings = []

    if delete_cert and not remove_certificate(domain):
        warnings.append(f"The certificate was not deleted. Run: sudo certbot delete --cert-name {domain}")

    if delete_dns:
        if not CLOUDFLARE_API_TOKEN:
            warnings.append("CLOUDFLARE_API_TOKEN is not set; DNS records were not removed.")
        else:
            try:
                remove_dns_records(domain, names)
            except ParkerError as e:
                warnings.append(f"DNS records were not removed: {e}")

    rollback_stack.cleanup()

    print("\n========================================")
    print(" ⚠ Site Removed With Warnings" if warnings else " ✅ Site Removed")
    print("========================================\n")
    print("Left untouched: project files, mail configuration (DKIM keys, mailboxes, mail DNS).")

    for warning in warnings:
        print(f"⚠ {warning}")

# =========================================================
# MAIN
# =========================================================

def build_parser():
    parser = argparse.ArgumentParser(
        prog="parker.py",
        description="Park a domain: assign a project directory and configure nginx, "
                    "Let's Encrypt, Cloudflare DNS and mail for it. Without flags Parker "
                    "asks for everything interactively.",
    )

    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--list", action="store_true", help="list Parker-managed sites and exit")
    modes.add_argument("--remove", metavar="DOMAIN", help="remove a Parker-managed site (nginx, certificate, optionally DNS)")

    parser.add_argument("--dry-run", action="store_true", help="show what would happen without changing anything")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="non-interactive: never prompt, use flags and defaults, fail if something required is missing")
    parser.add_argument("--force", action="store_true",
                        help="accept re-provisioning, duplicate hostnames and shared ports")

    provision_group = parser.add_argument_group("provisioning answers")
    provision_group.add_argument("--domain", help="domain or subdomain to park")
    provision_group.add_argument("--www", action=argparse.BooleanOptionalAction, default=None,
                                 help="add (or not) the www variant")
    provision_group.add_argument("--type", dest="project_type",
                                 help="1|php, 2|wordpress, 3|static, 4|node (Next.js/Nuxt/Express)")
    provision_group.add_argument("--dir", help="project directory (must be inside WEBROOT)")
    provision_group.add_argument("--port", type=int,
                        help="app port (node) or /api/ backend port (static)")
    provision_group.add_argument("--owner", metavar="USER[:GROUP]",
                                 help="owner for newly created project directories (default: PROJECT_OWNER)")
    provision_group.add_argument("--no-dns", action="store_true", help="skip Cloudflare DNS and mail DNS")
    provision_group.add_argument("--mail-dns", action=argparse.BooleanOptionalAction, default=None,
                                 help="configure SPF/DKIM/DMARC/MX (with --yes this is opt-in)")
    provision_group.add_argument("--no-ssl", action="store_true", help="skip certbot")

    removal_group = parser.add_argument_group("removal options")
    removal_group.add_argument("--keep-cert", action="store_true", help="keep the Let's Encrypt certificate")
    removal_group.add_argument("--remove-dns", action="store_true", help="also delete the Cloudflare CNAME records")

    return parser

INTERRUPT_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

def _signal_as_interrupt(signum, frame):
    raise KeyboardInterrupt

def install_interrupt_handlers():
    """
    Treat SIGTERM/SIGHUP like Ctrl-C so they trigger the rollback. A `systemctl stop`,
    a dashboard tab that was closed, or a dropped terminal all arrive as these signals;
    by default they would kill the run halfway through, with nothing undone.
    """
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        previous[sig] = signal.signal(sig, _signal_as_interrupt)
    return previous

def restore_interrupt_handlers(previous):
    for sig, handler in previous.items():
        signal.signal(sig, handler)

def ignore_interrupts():
    """Called before a rollback: it must run to completion, however many signals arrive."""
    for sig in INTERRUPT_SIGNALS:
        signal.signal(sig, signal.SIG_IGN)

def main(argv=None):
    global DRY_RUN, NON_INTERACTIVE

    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)

    DRY_RUN = args.dry_run
    NON_INTERACTIVE = args.yes

    if ENV_LOAD_ERROR:
        # Never carry on with missing settings: that is how a server ends up configured
        # with values it was not meant to have. (Checked before root, so a normal user who
        # simply cannot read a root-only .env is told what to do.)
        print(f"❌ ERROR: {ENV_LOAD_ERROR}")
        sys.exit(1)

    if args.list:
        list_sites()
        return

    ensure_root()

    rollback_stack.tasks.clear()
    rollback_stack.backups.clear()

    # A dry run promises to change nothing, which includes the audit log.
    log_file = None if DRY_RUN else start_audit_log(argv)

    previous_handlers = install_interrupt_handlers()

    try:
        print("\n========================================")
        print(" Parker: Domain Parking Utility 🚀")
        if DRY_RUN:
            print(" [!] DRY RUN MODE ENABLED")
            print(" [!] No changes will be made")
        print("========================================\n")

        if args.remove:
            remove_site(args)
        else:
            run_provisioning(args)

    except (Exception, KeyboardInterrupt) as e:
        if isinstance(e, KeyboardInterrupt):
            print("\n\n🛑 Execution interrupted by user.")
        else:
            print(f"\n❌ ERROR: {e}")
        ignore_interrupts()
        rollback_stack.run()
        sys.exit(1)

    finally:
        restore_interrupt_handlers(previous_handlers)
        stop_audit_log(log_file)

if __name__ == "__main__":
    main()
