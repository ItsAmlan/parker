# Parker Dashboard 🚀

[![Release Version](https://img.shields.io/badge/release-v1.0.0-blue.svg)](https://github.com/your-username/parker)
[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg)](https://python.org)
[![Platform Support](https://img.shields.io/badge/OS-Debian%20%7C%20Ubuntu-orange.svg)](https://wiki.debian.org/Derivatives)
[![FastAPI Web Framework](https://img.shields.io/badge/FastAPI-005571?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

A modern web-based control panel for [parker.py](../parker.py) — an automated domain parking utility that provisions DNS records, Nginx configs, SSL certificates, email authentication, and incoming IMAP mailboxes in a single interactive session.

Parker **does not create or build projects**. It assigns a project directory and wires Nginx to it for the project type you choose — so the server doesn't need Node.js, npm, or any build tooling.

Built with **FastAPI** and a real-time **WebSocket pseudo-terminal**, Parker Dashboard lets you run the full provisioning workflow from your browser instead of SSH. Designed for secure deployment behind **Cloudflare Zero Trust** on Debian-based Linux servers (Debian, Ubuntu, etc.).


## What Parker Does

When you enter a domain, `parker.py` first asks every question and runs read-only checks (**nothing is modified yet**), shows a review of what it is about to do, and only then applies the changes:

1. **Domain Analysis** — Validates the domain, offers the optional `www` variant, and detects domains that are already parked.
2. **Cloudflare Lookup** *(read-only)* — Finds the DNS zone, or asks whether to create one.
3. **Project Directory** — Assigns the project directory (default `WEBROOT/<domain>`) and the project type. Existing files are never touched.
4. **Mail Options** *(optional)* — Chooses SPF/DKIM/DMARC and any IMAP mailboxes (e.g. `support@`, `contact@`).
5. **Review & Confirm** — Preflight checks run (nginx, certbot, PHP snippet, mail tools), then the full plan is shown for approval.
6. **Apply** — Creates the directories, deploys the Nginx server block, creates DNS records and mail config, then obtains the SSL certificate.

### Project Types

| Type | Nginx behaviour |
|---|---|
| **PHP based Custom Site** | Serves `<project>/public_html` through PHP-FPM (`PHP_FPM_SNIPPET`). |
| **WordPress** | Same as PHP. Place WordPress in `<project>/public_html` yourself. |
| **Static SPA (React / Vite build)** | Serves `<project>/public_html` with SPA fallback to `index.html`. Optionally proxies `/api/` to a backend port. |
| **Node.js app on a port** | Reverse-proxies **every** request to `127.0.0.1:<port>`. Meant for Next.js, Nuxt, Remix, Express and similar servers you run yourself (pm2, systemd, …). |

Ports are validated (1024–65535), reserved ports (OpenDKIM/OpenDMARC milters, this dashboard) are rejected, and a port already proxied to by another site asks for confirmation. When re-provisioning, the project type and port are auto-detected from `package.json` and the existing Nginx config.

### Let's Encrypt webroot (`.well-known`)

Every generated Nginx config serves the ACME challenge from **inside the project directory**, not from `/var/www`:

```nginx
location ^~ /.well-known/acme-challenge/ {
    root /path/to/project;          # challenge files live in <project>/.well-known/acme-challenge/
    default_type "text/plain";
    try_files $uri =404;
}
```

Parker creates that directory and runs certbot with the matching webroot (`certbot run --authenticator webroot --installer nginx --webroot-path <project>`), so certificate **renewals** keep using the project directory too. Before calling certbot, Parker drops a probe file into the challenge directory and requests it through the local Nginx to prove the path is actually served.

The dashboard streams every step in real time and presents interactive quick-action buttons for each prompt.

## Features

- **Interactive Terminal** — Full PTY session streamed over WebSocket. Every prompt from `parker.py` appears in the browser with context-aware quick-answer buttons (Yes/No, project type selection, etc.).
- **Dry Run Mode** — Test the entire provisioning flow without touching DNS, filesystem, or services.
- **Validate First, Change Later** — All prompts, validation and preflight checks happen before the first change. Invalid input re-prompts instead of aborting halfway.
- **Automatic Rollback** — If any step fails, `parker.py` reverses all changes made during that run (DNS records, files, symlinks, configs) and reloads Nginx with the restored configuration. Config files are written atomically.
- **SSL Never Breaks a Live Site** — If certbot fails on a new domain, the site stays up over HTTP and the exact command to retry is printed. If the domain already had a certificate, the run is rolled back so HTTPS is never downgraded.
- **Duplicate Detection** — Before provisioning begins, parker checks for existing Nginx configs, project directories, and SSL certificates. If the domain is already parked, you're prompted before anything is modified.
- **Dark / Light / System Theme** — Glassmorphism-styled UI with persistent theme preference.
- **Zero Trust Ready** — Binds to `127.0.0.1` and is exposed exclusively through a Cloudflare Tunnel with access policies.

---

## Prerequisites

Parker is built for **Debian-based Linux** servers (Debian, Ubuntu, etc.). The following must be installed and configured before deployment.

### System Packages

```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx \
                    opendkim opendkim-tools opendmarc \
                    postfix postfix-policyd-spf-python \
                    dovecot-core dovecot-imapd \
                    python3 python3-venv curl
```

| Package | Purpose |
|---|---|
| `nginx` | Serves websites and reverse-proxies Node.js apps |
| `certbot` + `python3-certbot-nginx` | Automated Let's Encrypt SSL certificates |
| `opendkim` + `opendkim-tools` | DKIM key generation and mail signing |
| `opendmarc` | DMARC policy verification for incoming mail |
| `postfix` + `postfix-policyd-spf-python` | Mail transport agent (outbound relay + incoming virtual delivery) |
| `dovecot-core` + `dovecot-imapd` | IMAP server for incoming mail (Thunderbird, etc.) |
| `python3` + `python3-venv` | Runs both `parker.py` and the dashboard |

### Node.js (Not required)

Parker never runs `npm`, so Node.js does not need to be installed for the `parker` user. For **Node.js app** projects, Parker only writes the Nginx reverse-proxy; you deploy and run the app yourself (for example with pm2 or a systemd unit) and have it listen on `127.0.0.1:<port>` — e.g. `next start -H 127.0.0.1 -p 3000`. Nginx answers `502` until the app is running.

### Cloudflare Account

You need a [Cloudflare](https://dash.cloudflare.com/) account with:

- **Account ID** — Found on the right sidebar of any domain's Overview page.
- **API Token** — Create one at [dash.cloudflare.com/profile/api-tokens](https://dash.cloudflare.com/profile/api-tokens) with the following permissions:
  - `Zone : Zone : Read`
  - `Zone : DNS : Edit`
  - `Zone : Zone : Edit` (only if you want Parker to create new zones)

---

## Server Configuration

### Nginx

Ensure the `sites-available` / `sites-enabled` directory structure is in place (default on Debian/Ubuntu):

```bash
ls /etc/nginx/sites-available /etc/nginx/sites-enabled
```

For **PHP / WordPress** projects, Parker's Nginx configs reference a PHP snippet (checked during preflight, so a missing snippet stops the run before anything changes). Create it if it doesn't exist:

```bash
sudo nano /etc/nginx/snippets/php8.5.conf
```

```nginx
location ~ \.php$ {
    include snippets/fastcgi-php.conf;
    fastcgi_pass unix:/run/php/php8.4-fpm.sock;
    # Update the socket path to match your installed PHP version.
}
```

### OpenDKIM

Ensure the key and signing table files exist:

```bash
sudo mkdir -p /etc/opendkim/keys
sudo touch /etc/opendkim/key.table /etc/opendkim/signing.table
sudo chown -R opendkim:opendkim /etc/opendkim
```

Configure `/etc/opendkim.conf`:

```ini
Syslog                  yes
SyslogSuccess           yes
Canonicalization        relaxed/simple
Mode                    sv
OversignHeaders         From
UserID                  opendkim
UMask                   002
Socket                  inet:8891@localhost
PidFile                 /run/opendkim/opendkim.pid
KeyTable                /etc/opendkim/key.table
SigningTable            refile:/etc/opendkim/signing.table
InternalHosts           /etc/opendkim/trusted.hosts
```

Create `/etc/opendkim/trusted.hosts`:

```
127.0.0.1
::1
localhost
```

### OpenDMARC

Configure `/etc/opendmarc.conf`:

```ini
AuthservID              mail.yourdomain.com
RejectFailures          false
Socket                  inet:8893@localhost
SPFSelfValidate         true
IgnoreAuthenticatedClients true
```

Both OpenDKIM and OpenDMARC run as milters. Postfix connects to them via the milter configuration (see below).

### Postfix

Configure `/etc/postfix/main.cf` with the following key directives:

```ini
# HOSTNAME
myhostname = mail.yourdomain.com
myorigin = /etc/mailname

# NETWORK
inet_interfaces = all
inet_protocols = ipv4

# LOCAL DELIVERY
mydestination = localhost
mynetworks = 127.0.0.0/8 [::1]/128

# TLS (use your mail hostname's Let's Encrypt certificate)
smtpd_tls_cert_file = /etc/letsencrypt/live/mail.yourdomain.com/fullchain.pem
smtpd_tls_key_file = /etc/letsencrypt/live/mail.yourdomain.com/privkey.pem
smtpd_tls_security_level = may
smtp_tls_security_level = may

# MILTERS (OpenDKIM on 8891, OpenDMARC on 8893)
milter_default_action = accept
milter_protocol = 6
smtpd_milters = inet:localhost:8891, inet:localhost:8893
non_smtpd_milters = inet:localhost:8891, inet:localhost:8893

# SMTP RESTRICTIONS
smtpd_relay_restrictions =
    permit_mynetworks,
    permit_sasl_authenticated,
    reject_unauth_destination

smtpd_recipient_restrictions =
    permit_mynetworks,
    permit_sasl_authenticated,
    reject_unauth_destination

# VIRTUAL MAILBOX DELIVERY (for incoming mail via Dovecot)
virtual_mailbox_domains = /etc/postfix/virtual_domains
virtual_mailbox_maps = hash:/etc/postfix/virtual_mailbox_maps
virtual_transport = virtual
virtual_mailbox_base = /var/mail/vhosts
virtual_uid_maps = static:5000
virtual_gid_maps = static:5000

# SASL AUTH via Dovecot (for Thunderbird sending)
smtpd_sasl_type = dovecot
smtpd_sasl_path = private/auth
smtpd_sasl_auth_enable = yes
```

Enable the submission port (587) for authenticated clients in `/etc/postfix/master.cf`:

```ini
submission inet n       -       y       -       -       smtpd
  -o syslog_name=postfix/submission
  -o smtpd_tls_security_level=encrypt
  -o smtpd_sasl_auth_enable=yes
  -o smtpd_tls_auth_only=yes
  -o smtpd_relay_restrictions=permit_sasl_authenticated,reject
  -o milter_macro_daemon_name=ORIGINATING
```

Create the empty virtual mailbox files (parker.py populates these automatically):

```bash
sudo touch /etc/postfix/virtual_domains /etc/postfix/virtual_mailbox_maps
sudo postmap /etc/postfix/virtual_mailbox_maps
```

### Dovecot (IMAP)

Dovecot provides IMAP access to incoming mailboxes. Parker provisions per-domain mailboxes during setup, but the core Dovecot configuration must be done once beforehand.

#### 1. Create the virtual mail user

```bash
sudo groupadd -g 5000 vmail
sudo useradd -u 5000 -g vmail -s /usr/sbin/nologin -d /var/mail/vhosts -m vmail
sudo mkdir -p /var/mail/vhosts
sudo chown -R vmail:vmail /var/mail/vhosts
```

#### 2. Set protocols in `/etc/dovecot/dovecot.conf`

Append at the end:

```ini
protocols = imap
listen = *, ::
```

#### 3. Configure mail location in `/etc/dovecot/conf.d/10-mail.conf`

```ini
mail_location = maildir:/var/mail/vhosts/%d/%n
mail_uid = 5000
mail_gid = 5000
mail_privileged_group = vmail
first_valid_uid = 5000
last_valid_uid = 5000
```

#### 4. Switch to passwd-file auth in `/etc/dovecot/conf.d/10-auth.conf`

```ini
disable_plaintext_auth = yes
auth_mechanisms = plain login

# Comment out system auth, enable passwd-file:
#!include auth-system.conf.ext
!include auth-passwdfile.conf.ext
```

#### 5. Configure `/etc/dovecot/conf.d/auth-passwdfile.conf.ext`

```ini
passdb {
  driver = passwd-file
  args = scheme=BLF-CRYPT username_format=%u /etc/dovecot/users
}

userdb {
  driver = static
  args = uid=vmail gid=vmail home=/var/mail/vhosts/%d/%n
}
```

#### 6. Create the empty users file

```bash
sudo touch /etc/dovecot/users
sudo chown root:dovecot /etc/dovecot/users
sudo chmod 640 /etc/dovecot/users
```

Parker appends `user@domain:{BLF-CRYPT}hash` lines here during mailbox provisioning.

#### 7. Configure SSL in `/etc/dovecot/conf.d/10-ssl.conf`

```ini
ssl = required
ssl_cert = </etc/letsencrypt/live/mail.yourdomain.com/fullchain.pem
ssl_key = </etc/letsencrypt/live/mail.yourdomain.com/privkey.pem
ssl_min_protocol = TLSv1.2
```

#### 8. Add Postfix auth socket in `/etc/dovecot/conf.d/10-master.conf`

Inside the `service auth { }` block, uncomment and configure:

```ini
unix_listener /var/spool/postfix/private/auth {
    mode = 0660
    user = postfix
    group = postfix
}
```

#### 9. Enable and start Dovecot

```bash
sudo systemctl enable dovecot
sudo systemctl restart dovecot
sudo systemctl restart postfix
```

#### 10. Verify

```bash
sudo doveconf -n          # Check config syntax
sudo ss -tlnp | grep 993  # Confirm IMAPS is listening
sudo ss -tlnp | grep 587  # Confirm submission is listening
```

### Firewall (Optional)

If you use UFW, allow the mail ports:

```bash
sudo ufw allow 25/tcp     # SMTP (inbound delivery)
sudo ufw allow 587/tcp    # Submission (authenticated sending)
sudo ufw allow 993/tcp    # IMAPS (Dovecot)
```

---

## Installation

### 1. Clone the Repository

Choose any directory on your server. All examples below use `/path/to/parker` — substitute your actual path.

```bash
sudo mkdir -p /path/to/parker
sudo git clone <your-repo-url> /path/to/parker
```

### 2. Create the Python Virtual Environment

```bash
cd /path/to/parker
python3 -m venv venv
source venv/bin/activate
pip install requests fastapi "uvicorn[standard]" jinja2
deactivate
```

### 3. Configure Environment Variables

Copy the example environment file and fill in your values:

```bash
cp /path/to/parker/.env.example /path/to/parker/.env
nano /path/to/parker/.env
```

```env
CLOUDFLARE_API_TOKEN=your_cloudflare_api_token
CLOUDFLARE_ACCOUNT_ID=your_cloudflare_account_id
DEFAULT_SSL_EMAIL=ssl@yourdomain.com
MAIL_HOSTNAME=mail.yourdomain.com
DKIM_SELECTOR=default
MX_HOSTNAME=mail.yourdomain.com       # MX record target for incoming mail (defaults to MAIL_HOSTNAME if omitted)
WEBROOT=/bws/phoenix                  # Optional: base path for project directories (defaults to /bws/phoenix)
PHP_FPM_SNIPPET=snippets/php8.5.conf  # Optional: Nginx PHP-FPM include snippet (defaults to snippets/php8.5.conf)
```

> **Note:** `PARKER_SCRIPT_PATH` and `PARKER_VENV_PYTHON` are auto-derived from the project directory layout by the dashboard. You only need to set them in `.env` if your `parker.py` or venv lives outside the standard structure.



### 4. Verify CLI Works

Test `parker.py` directly in dry-run mode:

```bash
sudo /path/to/parker/venv/bin/python3 /path/to/parker/parker.py --dry-run
```

---

## Production Deployment

### 1. Create a Dedicated Service User

```bash
sudo useradd -r -s /usr/sbin/nologin parker
sudo chown -R parker:parker /path/to/parker
```

### 2. Configure Sudo Privileges

The dashboard runs as the `parker` user but needs root to execute `parker.py` (which modifies Nginx configs, generates DKIM keys, and runs Certbot). Grant passwordless sudo for only this specific command:

```bash
sudo visudo -f /etc/sudoers.d/parker
```

```
parker ALL=(ALL) NOPASSWD: /path/to/parker/venv/bin/python3 /path/to/parker/parker.py
parker ALL=(ALL) NOPASSWD: /path/to/parker/venv/bin/python3 /path/to/parker/parker.py --dry-run
```

### 3. Create the Systemd Service

Create `/etc/systemd/system/parker-ui.service`:

```ini
[Unit]
Description=Parker Dashboard - Domain Parking Web UI
After=network.target nginx.service

[Service]
Type=simple
User=parker
Group=parker
WorkingDirectory=/path/to/parker/parker-ui
ExecStart=/path/to/parker/venv/bin/uvicorn main:app --host 127.0.0.1 --port 9000
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable parker-ui
sudo systemctl start parker-ui
sudo systemctl status parker-ui
```

---

## Cloudflare Tunnel Setup

The dashboard binds to `127.0.0.1:9000` and must **never** be exposed directly to the internet. Use a Cloudflare Tunnel to securely expose it behind Zero Trust authentication.

### 1. Install cloudflared

Add the official Cloudflare repository and install:

```bash
sudo mkdir -p --mode=0755 /usr/share/keyrings

curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null

echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list

sudo apt update
sudo apt install -y cloudflared
```

Verify the installation:

```bash
cloudflared --version
```

### 2. Authenticate cloudflared

```bash
sudo cloudflared tunnel login
```

This opens a browser to authorize cloudflared with your Cloudflare account. The resulting certificate is saved to `/root/.cloudflared/cert.pem`.

### 3. Create the Tunnel

```bash
sudo cloudflared tunnel create parker
```

Note the **Tunnel ID** from the output (e.g., `a1b2c3d4-...`).

### 4. Configure the Tunnel

Create `/etc/cloudflared/config.yml`:

```yaml
tunnel: <TUNNEL_ID>
credentials-file: /root/.cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: parker.yourdomain.com
    service: http://127.0.0.1:9000
  - service: http_status:404
```

### 5. Add the DNS Record

```bash
sudo cloudflared tunnel route dns parker parker.yourdomain.com
```

### 6. Run as a Service

```bash
sudo cloudflared service install
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
```

### 7. Add a Zero Trust Access Policy (**Critical**)

In the [Cloudflare Zero Trust Dashboard](https://one.dash.cloudflare.com/):

1. Go to **Access → Applications → Add an application**.
2. Choose **Self-hosted**, set the domain to `parker.yourdomain.com`.
3. Add a policy requiring authentication — for example:
   - **One-Time PIN** sent to your email address.
   - **GitHub / Google SSO** for your team.
4. Save and test access by visiting `parker.yourdomain.com` in a browser.

> ⚠️ **Without an Access Policy, anyone with the URL can provision domains on your server.**

---

## Directory Structure

```
/path/to/parker/
├── README.md               # This file
├── .env                    # Shared credentials (API tokens, config)
├── .env.example            # Template for .env with all supported variables
├── .gitignore              # Git ignore rules
├── parker.py               # CLI provisioning script (runs as root)
├── venv/                   # Python virtual environment
└── parker-ui/
    ├── main.py             # FastAPI application (reads ../.env dynamically)
    ├── templates/
    │   └── index.html      # Dashboard UI (single-page)
    └── static/
        └── ui-mockup.svg   # Design reference
```

---

## Thunderbird / Mail Client Settings

After provisioning a domain with incoming mailboxes, configure your mail client:

| Setting | Value |
|---|---|
| **IMAP Server** | `mail.yourdomain.com` (your `MX_HOSTNAME`) |
| **IMAP Port** | `993` (SSL/TLS) |
| **SMTP Server** | `mail.yourdomain.com` (your `MX_HOSTNAME`) |
| **SMTP Port** | `587` (STARTTLS) |
| **Username** | Full email address (e.g. `support@example.com`) |
| **Password** | The password set during parker.py provisioning |
| **Authentication** | Normal password |

Parker prints these settings at the end of every mailbox provisioning run.

---

## Security Notes

- **Localhost Only** — The dashboard binds to `127.0.0.1` and is never directly reachable from the network. All external access goes through the Cloudflare Tunnel.
- **Minimal Sudo Surface** — The sudoers rule only allows executing `parker.py` via the project's venv Python, with or without `--dry-run`. No other commands are permitted.
- **PTY Isolation** — Each WebSocket session spawns an isolated pseudo-terminal process. Disconnecting the browser terminates the process within 3 seconds.
- **Domain Validation** — The frontend enforces `[A-Za-z0-9.-]+`, and `parker.py` itself validates the hostname (labels, length, TLD) and keeps project directories strictly inside `WEBROOT`.
- **Automatic Rollback** — If provisioning fails at any step, `parker.py` reverses all DNS records, directories, Nginx configs and symlinks, Dovecot users, and Postfix virtual maps created during that run, then reloads Nginx.
- **Password Hashing** — Mailbox passwords are hashed with bcrypt (`BLF-CRYPT`) via `doveadm pw` before being written to `/etc/dovecot/users`. Plain-text passwords are never stored.
- **TLS Enforced** — Dovecot requires SSL (`ssl = required`), and the submission port enforces STARTTLS. Plain-text IMAP on port 143 is available but only upgrades via STARTTLS.
