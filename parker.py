#!/usr/bin/env python3

import os
import re
import sys
import time
import json
import uuid
import shlex
import shutil
import socket
import tempfile
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import connection

def load_env(file_path=".env"):
    """Simple native .env loader to avoid extra dependencies."""
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip()

# Load environment variables from the script directory, regardless of service cwd.
load_env(Path(__file__).resolve().with_name(".env"))

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

BASE_DIR = os.getenv("WEBROOT", "/bws/phoenix")


# Global Dry Run Flag
DRY_RUN = "--dry-run" in sys.argv

CLOUDFLARE_API_TOKEN = os.getenv(
    "CLOUDFLARE_API_TOKEN",
    ""
)

CLOUDFLARE_ACCOUNT_ID = os.getenv(
    "CLOUDFLARE_ACCOUNT_ID",
    ""
)

DEFAULT_CNAME_TARGET = "server.bws.link"

MAIL_HOSTNAME = os.getenv(
    "MAIL_HOSTNAME",
    "mail.bws.link"
)

DKIM_SELECTOR = os.getenv(
    "DKIM_SELECTOR",
    "mail"
)

ENABLE_MAIL_SETUP = True

MX_HOSTNAME = os.getenv(
    "MX_HOSTNAME",
    MAIL_HOSTNAME
)

PHP_FPM_SNIPPET = os.getenv(
    "PHP_FPM_SNIPPET",
    "snippets/php8.5.conf"
)

# Dovecot / Virtual Mailbox Paths
DOVECOT_USERS_FILE = "/etc/dovecot/users"
POSTFIX_VIRTUAL_DOMAINS = "/etc/postfix/virtual_domains"
POSTFIX_VIRTUAL_MAILBOX_MAPS = "/etc/postfix/virtual_mailbox_maps"
VMAIL_BASE = "/var/mail/vhosts"



NGINX_SITES_AVAILABLE = "/etc/nginx/sites-available"
NGINX_SITES_ENABLED = "/etc/nginx/sites-enabled"

DEFAULT_SSL_EMAIL = os.getenv(
    "DEFAULT_SSL_EMAIL",
    ""
)

def validate_path(path):
    """Ensures the path is within BASE_DIR or allowed system config areas."""
    abs_path = os.path.abspath(path)
    allowed_areas = [
        os.path.abspath(BASE_DIR),
        os.path.abspath("/etc/nginx"),
        os.path.abspath("/etc/opendkim"),
        os.path.abspath("/etc/postfix"),
        os.path.abspath("/etc/dovecot"),
        os.path.abspath(VMAIL_BASE),
        os.path.abspath(tempfile.gettempdir())
    ]
    
    if not any(abs_path == area or abs_path.startswith(area + os.sep) for area in allowed_areas):
        raise PermissionError(f"🔒 Security Violation: Path {abs_path} is outside allowed areas.")

# =========================================================
# ROLLBACK SYSTEM
# =========================================================

class ParkerError(Exception):
    """A problem that should abort the run (and roll back anything already changed)."""

class CloudflareError(ParkerError):
    """The Cloudflare API could not be queried or rejected a request."""

class RollbackStack:
    def __init__(self):
        self.tasks = []
        self.backups = set()

    def add(self, func, *args, label=None):
        if DRY_RUN:
            return  # nothing is changed in a dry run, so there is nothing to undo
        self.tasks.append((func, args, label))

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

    prompt = question

    if default:
        prompt += f" [{default}]"

    prompt += ": "

    val = input(prompt).strip()

    if not val and default is not None:
        return default

    return val

def ask_yes_no(question, default="y"):

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

    ssl_dir = f"/etc/letsencrypt/live/{domain}"
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

def ensure_directory(path, track_rollback=False, mode=None):
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

    if mode is not None:
        # Explicit chmod: the process umask must not make web content unreadable to nginx.
        for created in [p, *p.parents]:
            os.chmod(created, mode)
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

MULTI_LEVEL_TLDS = [
    "co.in",
    "org.in",
    "net.in",
    "firm.in",
    "gen.in",
    "ind.in",
    "co.uk",
    "org.uk",
    "gov.uk",
    "ac.uk",
    "com.au",
    "net.au",
    "org.au",
]

def extract_root_domain(domain):

    domain = domain.lower().strip()

    for tld in MULTI_LEVEL_TLDS:

        if domain.endswith("." + tld):

            parts = domain.split(".")
            tld_parts = tld.split(".")

            required_parts = len(tld_parts) + 1

            return ".".join(parts[-required_parts:])

    parts = domain.split(".")

    if len(parts) >= 2:
        return ".".join(parts[-2:])

    return domain

def get_subdomain_part(domain):

    root = extract_root_domain(domain)

    if domain == root:
        return None

    suffix = "." + root

    if domain.endswith(suffix):
        return domain[:-len(suffix)]

    return None

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

        return data["result"]

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

    key_dir = f"/etc/opendkim/keys/{domain}"

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
        "/etc/opendkim/key.table",
        (
            f"{DKIM_SELECTOR}._domainkey.{domain} "
            f"{domain}:{DKIM_SELECTOR}:"
            f"/etc/opendkim/keys/{domain}/{DKIM_SELECTOR}.private"
        ),
        track_rollback=True
    )

    append_unique_line(
        "/etc/opendkim/signing.table",
        f"*@{domain} {DKIM_SELECTOR}._domainkey.{domain}",
        track_rollback=True
    )

def get_dkim_record(domain):

    if DRY_RUN:
        return "v=DKIM1; k=rsa; p=DRYRUN_PLACEHOLDER_KEY"

    path = f"/etc/opendkim/keys/{domain}/{DKIM_SELECTOR}.txt"

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

def setup_mail_dns(
    cf,
    zone_id,
    domain
):

    print("\n📧 Configuring mail authentication...")

    generate_dkim(domain)

    configure_opendkim_domain(domain)

    dkim_value = get_dkim_record(domain)

    cf.create_dns_record(
        zone_id=zone_id,
        record_type="TXT",
        record_name=domain,
        content=(
            f"v=spf1 "
            f"a:{MAIL_HOSTNAME} "
            f"mx "
            f"~all"
        )
    )

    cf.create_dns_record(
        zone_id=zone_id,
        record_type="TXT",
        record_name=f"_dmarc.{domain}",
        content=(
            "v=DMARC1; "
            "p=quarantine; "
            "adkim=s; "
            "aspf=s"
        )
    )

    if dkim_value:
        cf.create_dns_record(
            zone_id=zone_id,
            record_type="TXT",
            record_name=f"{DKIM_SELECTOR}._domainkey.{domain}",
            content=dkim_value
        )
    else:
        print("⚠ DKIM key could not be read. Skipping DKIM DNS record.")

    # MX record — points domain to the mail server for incoming mail
    cf.create_dns_record(
        zone_id=zone_id,
        record_type="MX",
        record_name=domain,
        content=MX_HOSTNAME,
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

    result = subprocess.run(
        ["doveadm", "pw", "-s", "BLF-CRYPT", "-p", plain_password],
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

        password = ask(f"Enter password for {email}")

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
    }}"""

def generate_nginx_config(
    domains,
    project_type,
    project_root,
    public_html=None,
    port=None,
    primary_domain=None
):
    """Renders the (HTTP only) server block. certbot adds the HTTPS parts afterwards."""

    kind = project_kind(project_type)
    server_names = " ".join(domains)
    log_name = primary_domain or domains[0]

    blocks = [f"""    listen 80;

    server_name {server_names};

    access_log /var/log/nginx/{log_name}.access.log;
    error_log  /var/log/nginx/{log_name}.error.log;

    client_max_body_size 100M;""",

    # certbot --webroot writes challenges here, inside the project (not /var/www).
    f"""    # Let's Encrypt HTTP-01 challenge. The webroot lives in the project directory.
    location ^~ /.well-known/acme-challenge/ {{
        root {project_root};
        default_type "text/plain";
        try_files $uri =404;
    }}"""]

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

def verify_acme_challenge_path(domain, project_root):
    """
    Drops a probe file into the challenge directory and requests it through the local
    nginx, proving the .well-known location really serves from the project directory.
    Diagnostic only: failure warns but does not stop the run.
    """
    challenge_dir = acme_challenge_dir(project_root)

    if DRY_RUN:
        print(f"🔎 [DRY RUN] Would verify http://{domain}/.well-known/acme-challenge/ serves from {challenge_dir}")
        return True

    token = f"parker-check-{uuid.uuid4().hex}"
    probe = os.path.join(challenge_dir, token)
    outcome = "no response"

    try:
        with open(probe, "w") as f:
            f.write(token)
        os.chmod(probe, 0o644)

        session = requests.Session()
        session.trust_env = False  # never route this loopback probe through a proxy

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
                print(f"✅ ACME challenge path is served from {challenge_dir}")
                return True

            outcome = f"HTTP {r.status_code}"
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass

    print(f"⚠ ACME challenge probe failed ({outcome}); certbot may not be able to validate this domain.")
    return False

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

def setup_ssl(domains, project_root):
    """Obtains and installs the certificate. Returns True on success."""

    if not command_exists("certbot"):
        print("⚠ Certbot not installed.")
        return False

    cmd = certbot_command(domains, project_root)

    for attempt in range(1, SSL_ATTEMPTS + 1):
        result = run(cmd, check=False)

        if result.returncode == 0:
            return True

        if attempt < SSL_ATTEMPTS:
            print(f"\n⚠ certbot failed (attempt {attempt}/{SSL_ATTEMPTS}). Retrying in {SSL_RETRY_DELAY}s...")
            if not DRY_RUN:
                time.sleep(SSL_RETRY_DELAY)

    return False

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
    zone: Optional[dict] = None
    create_zone: bool = False
    configure_mail_dns: bool = False
    setup_incoming: bool = False
    mailboxes: list = field(default_factory=list)
    had_ssl: bool = False
    warnings: list = field(default_factory=list)

    @property
    def kind(self):
        return project_kind(self.project_type)

    @property
    def dns_enabled(self):
        return bool(self.zone or self.create_zone)

def prompt_domain():
    while True:
        domain = ask("Enter domain or subdomain").lower().strip()
        if is_valid_domain(domain):
            return domain
        print("⚠ Invalid domain. Use a full hostname such as example.com or app.example.com.")

def prompt_port(question, default=None, exclude_domain=None):
    """Asks for a local TCP port, rejecting invalid and reserved values."""
    while True:
        raw = ask(question, default=str(default) if default else None)

        if not raw.isdigit() or not 1024 <= int(raw) <= 65535:
            print("⚠ Enter a port between 1024 and 65535.")
            continue

        port = int(raw)

        if port in RESERVED_PORTS:
            print(f"⚠ Port {port} is used by {RESERVED_PORTS[port]}. Choose another port.")
            continue

        users = find_port_users(exclude_domain, port) if exclude_domain else []
        if users:
            print(f"⚠ Port {port} is already proxied to by: {', '.join(users)}")
            if not ask_yes_no("Use it anyway?", default="n"):
                continue

        return port

def prompt_project_type(detected_type):
    print("\nProject Types:")
    for number, info in PROJECT_TYPES.items():
        print(f"{number}. {info['label']}")

    while True:
        raw = ask(
            "Choose project type",
            default=str(detected_type) if detected_type else None
        )
        if raw.isdigit() and int(raw) in PROJECT_TYPES:
            return int(raw)
        print(f"⚠ Enter a number between 1 and {len(PROJECT_TYPES)}.")

def prompt_project_dir(domain):
    default_dir = os.path.join(BASE_DIR, domain)
    base = os.path.abspath(BASE_DIR)

    while True:
        path = os.path.abspath(ask("Project directory", default=default_dir))

        try:
            validate_path(path)
        except PermissionError:
            print(f"⚠ The project directory must be inside {base}.")
            continue

        if path == base:
            print(f"⚠ Choose a directory inside {base}, not {base} itself.")
            continue

        if os.path.exists(path) and not os.path.isdir(path):
            print(f"⚠ {path} exists and is not a directory.")
            continue

        return path

def plan_domains():
    log_step("Step 1: Domain Analysis", "Collecting and analyzing domain details...")

    domain = prompt_domain()

    root_domain = extract_root_domain(domain)
    subdomain_part = get_subdomain_part(domain)

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
        if not ask_yes_no("\nProceed with re-provisioning anyway?", default="n"):
            print("\n👋 Exiting. No changes were made.")
            sys.exit(0)

    plan.had_ssl = os.path.exists(f"/etc/letsencrypt/live/{domain}")

    conflicts = find_server_name_conflicts(domain, domains)
    if conflicts:
        print("\n⚠ Other nginx sites already answer for these hostnames:")
        for name, hosts in conflicts.items():
            print(f"  - {name}: {', '.join(sorted(hosts))}")
        if not ask_yes_no("Continue anyway? (nginx will ignore the duplicate names)", default="n"):
            print("\n👋 Exiting. No changes were made.")
            sys.exit(0)

    return plan

def plan_dns(plan):
    """Read-only Cloudflare lookup; decides whether DNS/mail-DNS steps will run."""
    log_step("Step 2: Cloudflare Lookup", "Checking DNS zone on Cloudflare (read-only)...")

    reason = None

    if not CLOUDFLARE_API_TOKEN:
        reason = "CLOUDFLARE_API_TOKEN is not set."
    else:
        try:
            zone = CloudflareManager().get_zone(plan.root_domain)
        except ParkerError as e:
            reason = f"Cloudflare lookup failed: {e}"
        else:
            if zone:
                print("\n✅ Cloudflare Zone Found")
                plan.zone = zone
                return

            print("\n⚠ Zone not found in Cloudflare.")

            if plan.subdomain_part:
                reason = "Cannot create DNS for a subdomain because the parent zone does not exist."
            elif ask_yes_no("Create new Cloudflare zone?"):
                plan.create_zone = True
                return
            else:
                reason = "No Cloudflare zone will be created."

    print(f"⚠ {reason}")
    if not ask_yes_no("Continue without DNS changes?", default="n"):
        print("\n👋 Exiting. No changes were made.")
        sys.exit(0)

    plan.warnings.append(f"DNS was not configured: {reason}")

def plan_project(plan):
    log_step("Step 3: Project Directory", "Assigning the project directory and site type (nothing is created or built)...")

    default_root = os.path.join(BASE_DIR, plan.domain)

    detected_type = None
    if os.path.isdir(default_root) and not is_directory_empty(default_root):
        print(f"\n✅ Existing project found at {default_root}")
        detected_type = detect_project_type(default_root)
        if detected_type:
            print(f"🔍 Auto-detected project type: {PROJECT_TYPES[detected_type]['label']}")

    plan.project_type = prompt_project_type(detected_type)
    plan.project_root = prompt_project_dir(plan.domain)

    if plan.kind in ("php", "static"):
        plan.public_html = os.path.join(plan.project_root, "public_html")

    previous_port = existing_proxy_port(plan.domain)

    if plan.kind == "proxy":
        plan.port = prompt_port(
            "Port your app listens on",
            default=previous_port or DEFAULT_APP_PORT,
            exclude_domain=plan.domain
        )
    elif plan.kind == "static":
        if ask_yes_no("Proxy /api/ to a backend service (e.g. Express)?", default="y" if previous_port else "n"):
            plan.port = prompt_port(
                "Backend port",
                default=previous_port,
                exclude_domain=plan.domain
            )

def plan_mail(plan):
    if not ENABLE_MAIL_SETUP or not plan.dns_enabled:
        return

    if plan.subdomain_part:
        print("\nℹ Subdomain detected. Skipping mail authentication (root domain setup only).")
        return

    log_step("Step 3b: Mail Options", "Choosing mail authentication and mailboxes...")

    if not ask_yes_no("Configure SPF/DKIM/DMARC?"):
        return

    plan.configure_mail_dns = True

    if ask_yes_no("Set up incoming mail (IMAP mailboxes)?", default="n"):
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

    notes = []

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

# =========================================================
# EXECUTION
# =========================================================

def setup_project_directories(plan):
    ensure_directory(plan.project_root, track_rollback=True, mode=0o755)

    if plan.public_html:
        ensure_directory(plan.public_html, track_rollback=True, mode=0o755)

    ensure_directory(acme_challenge_dir(plan.project_root), track_rollback=True, mode=0o755)

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

    zone_id = zone["id"]

    print("\n🌐 Creating DNS records...\n")

    for d in plan.domains:
        cf.create_site_cname_record(
            zone_id=zone_id,
            zone_name=plan.root_domain,
            record_name=d,
            content=DEFAULT_CNAME_TARGET,
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

    ssl_ok = False

    if plan.dns_enabled:
        print("\n⏳ Waiting 30 seconds for DNS propagation before SSL setup...")
        if not DRY_RUN:
            time.sleep(30)

    verify_acme_challenge_path(plan.domain, plan.project_root)

    print("\n🔐 Setting up SSL...\n")

    ssl_ok = setup_ssl(plan.domains, plan.project_root)

    if not ssl_ok:
        manual = " ".join(shlex.quote(part) for part in certbot_command(plan.domains, plan.project_root))

        if plan.had_ssl:
            # The site was already serving HTTPS; never leave it downgraded.
            raise ParkerError(
                "certbot failed for a domain that already had a certificate. "
                "Rolling back so the existing HTTPS configuration is preserved."
            )

        plan.warnings.append(
            "SSL was NOT configured; the site is served over HTTP only. "
            f"Once DNS resolves to this server, run:\n      sudo {manual}"
        )

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
    else:
        ssl_status = "enabled" if ssl_ok else "not configured"
    print(f"SSL          : {ssl_status}")

    if plan.kind == "proxy":
        print(
            f"\nNext: deploy your app into {plan.project_root} and start it on port {plan.port} "
            f"(bind to 127.0.0.1). nginx answers 502 until it is running."
        )
    elif plan.kind == "php" and plan.project_type == 2:
        print(f"\nNext: place WordPress in {plan.public_html}.")
    elif plan.public_html:
        print(f"\nNext: deploy your site files into {plan.public_html}.")

    if plan.warnings:
        print("\nWarnings:")
        for warning in plan.warnings:
            print(f"  - {warning}")

# =========================================================
# MAIN
# =========================================================

def main():

    ensure_root()

    try:
        print("\n========================================")
        print(" Parker: Domain Parking Utility 🚀")
        if DRY_RUN:
            print(" [!] DRY RUN MODE ENABLED")
            print(" [!] No changes will be made")
        print("========================================\n")

        # Phase 1: ask everything and validate everything. Nothing is modified.
        plan = plan_domains()
        plan_dns(plan)
        plan_project(plan)
        plan_mail(plan)
        preflight(plan)
        show_plan(plan)

        if not ask_yes_no("\nProceed with these settings?"):
            print("\n👋 Exiting. No changes were made.")
            sys.exit(0)

        # Phase 2: apply. Any failure rolls back everything done in this run.
        ssl_ok = provision(plan)

        print_summary(plan, ssl_ok)

        rollback_stack.cleanup()

    except (Exception, KeyboardInterrupt) as e:
        if isinstance(e, KeyboardInterrupt):
            print("\n\n🛑 Execution interrupted by user.")
        else:
            print(f"\n❌ ERROR: {e}")
        rollback_stack.run()
        sys.exit(1)

if __name__ == "__main__":
    main()
