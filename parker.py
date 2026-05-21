#!/usr/bin/env python3

import os
import re
import sys
import time
import json
import shutil
import socket
import tarfile
import tempfile
import subprocess
from pathlib import Path

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

BASE_DIR = "/bws/phoenix"

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
        os.path.abspath(tempfile.gettempdir())
    ]
    
    if not any(abs_path.startswith(area) for area in allowed_areas):
        raise PermissionError(f"🔒 Security Violation: Path {abs_path} is outside allowed areas.")

# =========================================================
# ROLLBACK SYSTEM
# =========================================================

class RollbackStack:
    def __init__(self):
        self.tasks = []
        self.backups = set()

    def add(self, func, *args, label=None):
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

def detect_project_type(project_root):
    """Attempts to auto-detect the project type based on file markers."""
    # Check for WordPress
    if os.path.exists(os.path.join(project_root, "public_html", "wp-config.php")):
        return 2
    
    # Check for React (as per setup_react structure)
    if os.path.exists(os.path.join(project_root, "app", "package.json")):
        return 3
    
    # Check for general PHP (if .php files exist in public_html)
    public_html = os.path.join(project_root, "public_html")
    if os.path.exists(public_html):
        try:
            for file in os.listdir(public_html):
                if file.endswith(".php"):
                    return 1
        except Exception:
            pass
                
    return None

def restore_backup(backup_path, original_path):
    """Restores a file from a backup."""
    if DRY_RUN:
        print(f" 🔄 [DRY RUN] Would restore original file: {original_path}")
        return

    if os.path.exists(backup_path):
        print(f" 🔄 Restoring original file: {original_path}")
        shutil.move(backup_path, original_path)

def ensure_directory(path, track_rollback=False):
    validate_path(path)
    p = Path(path)
    if not p.exists():
        if DRY_RUN:
            print(f"📂 [DRY RUN] Would create directory: {path}")
            return

        p.mkdir(parents=True, exist_ok=True)
        if track_rollback:
            rollback_stack.add(shutil.rmtree, path, label=f"Remove directory {path}")
    elif not p.is_dir():
        # Exists but not a directory - this is an error state
        raise FileExistsError(f"{path} exists and is not a directory.")

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
            rollback_stack.add(os.remove, path, label=f"Delete new file {path}")

    if DRY_RUN:
        print(f"📝 [DRY RUN] Would write content to: {path}")
        return

    with open(path, "w") as f:
        f.write(content)

def append_unique_line(path, line, track_rollback=False):
    validate_path(path)
    existing = ""

    if os.path.exists(path):
        with open(path, "r") as f:
            existing = f.read()

    if line not in existing:
        if track_rollback and os.path.exists(path):
            backup_path = f"{path}.phoenix_bak"
            # Only create one backup per run for the same file
            if not os.path.exists(backup_path):
                if DRY_RUN:
                    print(f"📂 [DRY RUN] Would back up {path} to {backup_path}")
                else:
                    shutil.copy2(path, backup_path)
                
                rollback_stack.add_backup(backup_path)
                rollback_stack.add(restore_backup, backup_path, path, label=f"Restore system config {path}")

        if DRY_RUN:
            print(f"📝 [DRY RUN] Would append line to: {path}")
            return

        with open(path, "a") as f:
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
            return None

        data = r.json()

        if not data.get("success"):
            self.print_api_error(r)
            return None

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
        track_rollback=True
    ):
        if DRY_RUN:
            print(f"🌐 [DRY RUN] Would create DNS record: {record_type} {record_name} -> {content}")
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

    run(["systemctl", "restart", "opendkim"])
    run(["systemctl", "restart", "postfix"])

    print("✅ Mail authentication configured.")

# =========================================================
# WORDPRESS
# =========================================================

def setup_wordpress(project_root):

    public_html = os.path.join(project_root, "public_html")
    ensure_directory(public_html)

    if DRY_RUN:
        print(f"⬇ [DRY RUN] Would download and extract WordPress to {project_root}")
        return

    temp_dir = tempfile.mkdtemp()

    archive_path = os.path.join(
        temp_dir,
        "wordpress.tar.gz"
    )

    print("⬇ Downloading WordPress...")

    r = requests.get(
        "https://wordpress.org/latest.tar.gz",
        stream=True
    )

    with open(archive_path, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)

    print("📦 Extracting WordPress...")

    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(temp_dir)

    wp_dir = os.path.join(
        temp_dir,
        "wordpress"
    )

    for item in os.listdir(wp_dir):

        src = os.path.join(wp_dir, item)
        dst = os.path.join(public_html, item)

        if os.path.isdir(src):
            shutil.copytree(
                src,
                dst,
                dirs_exist_ok=True
            )
        else:
            shutil.copy2(src, dst)

    shutil.rmtree(temp_dir)

    print("✅ WordPress ready.")

# =========================================================
# REACT + VITE
# =========================================================

def setup_react(project_root):

    if not command_exists("npm"):
        print("❌ npm not found.")
        sys.exit(1)

    app_dir = os.path.join(
        project_root,
        "app"
    )

    public_html = os.path.join(
        project_root,
        "public_html"
    )

    print("⚛ Creating React + Vite app...")

    run([
        "npm",
        "create",
        "vite@latest",
        "app",
        "--",
        "--template",
        "react"
    ], cwd=project_root)

    run(["npm", "install"], cwd=app_dir)

    print("🎨 Installing TailwindCSS...")

    run([
        "npm",
        "install",
        "-D",
        "tailwindcss",
        "@tailwindcss/vite"
    ], cwd=app_dir)

    vite_config = os.path.join(
        app_dir,
        "vite.config.js"
    )

    if os.path.exists(vite_config):

        with open(vite_config, "r") as f:
            content = f.read()

        if "@tailwindcss/vite" not in content:

            content = content.replace(
                "import react from '@vitejs/plugin-react'",
                "import react from '@vitejs/plugin-react'\nimport tailwindcss from '@tailwindcss/vite'"
            )

            content = content.replace(
                "plugins: [react()]",
                "plugins: [react(), tailwindcss()]"
            )

            write_file(vite_config, content)

    css_file = os.path.join(
        app_dir,
        "src",
        "index.css"
    )

    if not DRY_RUN and os.path.exists(css_file):
        with open(css_file, "a") as f:
            f.write("\n@import 'tailwindcss';\n")

    print("🏗 Building frontend...")

    run(["npm", "run", "build"], cwd=app_dir)

    dist_dir = os.path.join(
        app_dir,
        "dist"
    )

    if os.path.exists(public_html):
        backup_name = f"public_html.bak.{int(time.time())}"
        backup_path = os.path.join(project_root, backup_name)
        
        if DRY_RUN:
            print(f"📦 [DRY RUN] Would back up existing public_html to {backup_name}")
        else:
            print(f"📦 Backing up existing public_html to {backup_name}...")
            shutil.move(public_html, backup_path)

    if DRY_RUN:
        print(f"🏗 [DRY RUN] Would copy built files from {dist_dir} to {public_html}")
    else:
        shutil.copytree(
            dist_dir,
            public_html
        )

    print("✅ React app ready.")

# =========================================================
# NGINX
# =========================================================

def generate_nginx_config(
    domains,
    public_html,
    project_type,
    express_port=None,
    primary_domain=None
):

    server_names = " ".join(domains)
    log_name = primary_domain or domains[0]

    if project_type == 3:

        if express_port:

            react_location = f"""
    location /api/ {{
        proxy_pass http://127.0.0.1:{express_port}/;

        proxy_http_version 1.1;

        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        proxy_set_header Host $host;

        proxy_cache_bypass $http_upgrade;
    }}
"""
        else:
            react_location = ""

        location_block = f"""
    location / {{
        try_files $uri /index.html;
    }}

{react_location}
"""

    else:

        location_block = f"""
    location / {{
        try_files $uri $uri/ /index.php?$query_string;
    }}
"""

    php_block = ""

    if project_type in [1, 2]:

        php_block = """
    include snippets/php8.5.conf;
"""

    return f"""
server {{

    listen 80;

    server_name {server_names};

    root {public_html};

    index index.php index.html index.htm;

    access_log /var/log/nginx/{log_name}.access.log;
    error_log  /var/log/nginx/{log_name}.error.log;

    client_max_body_size 100M;

{location_block}

{php_block}

    location ~ /\\.ht {{
        deny all;
    }}

    location = /favicon.ico {{
        access_log off;
        log_not_found off;
    }}

    location = /robots.txt {{
        access_log off;
        log_not_found off;
    }}
}}
"""

def cleanup_nginx_symlink(enabled_path):
    if os.path.islink(enabled_path):
        os.unlink(enabled_path)

def setup_nginx(domain, config):

    filename = f"{domain}.conf"

    available_path = os.path.join(
        NGINX_SITES_AVAILABLE,
        filename
    )

    enabled_path = os.path.join(
        NGINX_SITES_ENABLED,
        filename
    )

    write_file(
        available_path,
        config,
        track_rollback=True
    )

    if not os.path.exists(enabled_path):
        if DRY_RUN:
            print(f"🔗 [DRY RUN] Would create symlink: {available_path} -> {enabled_path}")
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

def setup_ssl(domains):

    if not command_exists("certbot"):
        print("⚠ Certbot not installed.")
        return

    cmd = [
        "certbot",
        "--nginx",
        "--redirect",
        "--agree-tos",
        "--non-interactive",
        "-m",
        DEFAULT_SSL_EMAIL
    ]

    for d in domains:
        cmd.extend(["-d", d])

    run(cmd)

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

        log_step("Step 1: Domain Analysis", "Collecting and analyzing domain details...")

        domain = ask(
            "Enter domain or subdomain"
        ).lower().strip()

        root_domain = extract_root_domain(domain)
        subdomain_part = get_subdomain_part(domain)

        www_default = "n" if subdomain_part else "y"

        use_www = ask_yes_no(
            "Add www variant too?",
            default=www_default
        )

        domains = [domain]

        if use_www and not domain.startswith("www."):
            domains.append(f"www.{domain}")

        print(f"\nRoot Domain : {root_domain}")

        if subdomain_part:
            print(f"Subdomain   : {subdomain_part}")
        else:
            print("Subdomain   : None")

        print(f"Targets     : {', '.join(domains)}")

        log_step("Step 2: Cloudflare Setup", "Configuring DNS records and zones on Cloudflare...")

        cf = CloudflareManager()

        zone = cf.get_zone(root_domain)

        zone_id = None

        if zone:

            print("\n✅ Cloudflare Zone Found")

            zone_id = zone["id"]

            print("🌐 Creating DNS entries...")

            for d in domains:

                cf.create_site_cname_record(
                    zone_id=zone_id,
                    zone_name=root_domain,
                    record_name=d,
                    content=DEFAULT_CNAME_TARGET,
                    proxied=True
                )

        else:

            print("\n⚠ Zone not found in Cloudflare.")

            if subdomain_part:

                print(
                    "❌ Cannot create DNS for subdomain "
                    "because parent zone does not exist."
                )

                sys.exit(1)

            else:

                if ask_yes_no(
                    "Create new Cloudflare zone?"
                ):

                    zone = cf.create_zone(
                        root_domain
                    )

                    if zone:

                        print("\n✅ Zone created.")

                        print("\n⚠ Update Nameservers To:\n")

                        for ns in zone["name_servers"]:
                            print(f" - {ns}")

                        print("")
                        ask("Press Enter once you have updated the nameservers at your domain registrar")

                        zone_id = zone["id"]

                        print("\n🌐 Creating DNS records...\n")

                        for d in domains:

                            cf.create_site_cname_record(
                                zone_id=zone_id,
                                zone_name=root_domain,
                                record_name=d,
                                content=DEFAULT_CNAME_TARGET,
                                proxied=True
                            )

        if ENABLE_MAIL_SETUP and zone_id:

            if subdomain_part is None:
                if ask_yes_no(
                    "Configure SPF/DKIM/DMARC?"
                ):
                    log_step("Step 3: Mail Authentication", "Setting up SPF, DKIM, and DMARC for email reliability...")
                    setup_mail_dns(
                        cf,
                        zone_id,
                        root_domain
                    )
            else:
                print("\nℹ Subdomain detected. Skipping mail authentication (root domain setup only).")

        log_step("Step 4: Project Directory Setup", "Preparing the local filesystem and boilerplate...")

        project_root = os.path.join(
            BASE_DIR,
            domain
        )

        public_html = os.path.join(
            project_root,
            "public_html"
        )

        # Auto-detect existing project and type
        existing = False
        detected_type = None
        if os.path.exists(project_root) and not is_directory_empty(project_root):
            existing = True
            print(f"\n✅ Auto-detected existing project at {project_root}")
            print("🚀 Skipping boilerplate setup to protect existing files.")
            detected_type = detect_project_type(project_root)
            if detected_type:
                type_names = {1: "Custom PHP", 2: "WordPress", 3: "React + Vite"}
                print(f"🔍 Auto-detected project type: {type_names[detected_type]}")

        print("\nProject Types:")
        print("1. PHP based Custom Site")
        print("2. Wordpress")
        print("3. React + Vite + Express")

        project_type = int(
            ask("Choose project type", default=str(detected_type) if detected_type else None)
        )

        # Only track rollback if we actually create the directory right now
        ensure_directory(project_root, track_rollback=not existing)

        if existing:

            ensure_directory(public_html)

            print(
                f"\n✅ Existing project directory ready:\n"
                f"{project_root}"
            )

        else:

            if project_type == 1:

                ensure_directory(public_html)

                print(
                    "\n✅ Empty public_html created."
                )

            else:

                boilerplate = ask_yes_no(
                    "Setup boilerplate?"
                )

                if boilerplate:

                    if project_type == 2:
                        setup_wordpress(project_root)

                    elif project_type == 3:
                        setup_react(project_root)

                else:
                    ensure_directory(public_html)

        express_port = None

        if project_type == 3:

            if ask_yes_no(
                "Will there be an ExpressJS backend?"
            ):

                express_port = ask(
                    "Enter ExpressJS backend port"
                )

        log_step("Step 5: Nginx Configuration", "Generating and deploying Nginx server blocks...")

        nginx_config = generate_nginx_config(
            domains=domains,
            public_html=public_html,
            project_type=project_type,
            express_port=express_port
        )

        setup_nginx(
            domain,
            nginx_config
        )

        log_step("Step 6: SSL Certificate Setup", "Securing the site with Let's Encrypt SSL...")

        print("\n⏳ Waiting 30 seconds for DNS propagation before SSL setup...")
        time.sleep(30)

        print("\n🔐 Setting up SSL...\n")

        setup_ssl(domains)

        print("\n========================================")
        print(" ✅ Setup Completed Successfully")
        print("========================================\n")

        print(f"Project Root : {project_root}")
        print(f"Public Root  : {public_html}")
        print(f"Domains      : {', '.join(domains)}")

        rollback_stack.cleanup()

    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        rollback_stack.run()
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\n🛑 Execution interrupted by user.")
        rollback_stack.run()
        sys.exit(1)

if __name__ == "__main__":
    main()