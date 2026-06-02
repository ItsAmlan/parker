# Parker Dashboard 🚀

[![Release Version](https://img.shields.io/badge/release-v1.0.0-blue.svg)](https://github.com/your-username/parker)
[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg)](https://python.org)
[![Platform Support](https://img.shields.io/badge/OS-Debian%20%7C%20Ubuntu-orange.svg)](https://wiki.debian.org/Derivatives)
[![FastAPI Web Framework](https://img.shields.io/badge/FastAPI-005571?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

A modern web-based control panel for [parker.py](../parker.py) — an automated domain parking utility that provisions DNS records, web server configs, SSL certificates, email authentication, and incoming IMAP mailboxes in a single interactive session.

Built with **FastAPI** and a real-time **WebSocket pseudo-terminal**, Parker Dashboard lets you run the full provisioning workflow from your browser instead of SSH. Designed for secure deployment behind **Cloudflare Zero Trust** on Debian-based Linux servers (Debian, Ubuntu, etc.).


## What Parker Does

When you enter a domain, `parker.py` walks through these steps automatically:

1. **DNS Setup** — Creates or reuses a Cloudflare zone, adds CNAME records (with optional `www` variant).
2. **Mail Authentication** — Generates DKIM keys and creates SPF, DKIM, DMARC TXT records, and an MX record for the domain.
3. **Incoming Mail Setup** *(optional)* — Provisions IMAP mailboxes (e.g. `support@`, `contact@`) via Dovecot with bcrypt-hashed passwords, Postfix virtual delivery, and Maildir storage.
4. **Project Scaffolding** — Sets up the project directory with optional boilerplate (Custom PHP, WordPress, or React + Vite + Express). TailwindCSS is automatically installed and configured for React projects.
5. **Nginx Configuration** — Generates and deploys server blocks with PHP-FPM or reverse proxy support.
6. **SSL Provisioning** — Obtains and installs Let's Encrypt certificates via Certbot.

The dashboard streams every step in real time and presents interactive quick-action buttons for each prompt.

## Features

- **Interactive Terminal** — Full PTY session streamed over WebSocket. Every prompt from `parker.py` appears in the browser with context-aware quick-answer buttons (Yes/No, project type selection, etc.).
- **Dry Run Mode** — Test the entire provisioning flow without touching DNS, filesystem, or services.
- **Automatic Rollback** — If any step fails, `parker.py` reverses all changes made during that run (DNS records, files, symlinks, configs).
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
| `nginx` | Serves websites and reverse-proxies React apps |
| `certbot` + `python3-certbot-nginx` | Automated Let's Encrypt SSL certificates |
| `opendkim` + `opendkim-tools` | DKIM key generation and mail signing |
| `opendmarc` | DMARC policy verification for incoming mail |
| `postfix` + `postfix-policyd-spf-python` | Mail transport agent (outbound relay + incoming virtual delivery) |
| `dovecot-core` + `dovecot-imapd` | IMAP server for incoming mail (Thunderbird, etc.) |
| `python3` + `python3-venv` | Runs both `parker.py` and the dashboard |

### Node.js (Optional — only for React + Vite + Express projects)

If you plan to use the React + Vite project type, install Node.js via the official NodeSource repository:

```bash
curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
sudo apt install -y nodejs
```

Verify with `node -v` and `npm -v`.

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

Parker generates Nginx configs that reference a PHP snippet. Create it if it doesn't exist:

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
WEBROOT=/bws/phoenix                  # Optional: base path where websites are deployed (defaults to /bws/phoenix)
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
- **Domain Validation** — The frontend enforces `[A-Za-z0-9.-]+` on domain input before sending it to the backend.
- **Automatic Rollback** — If provisioning fails at any step, `parker.py` reverses all DNS records, files, Nginx symlinks, Dovecot users, and Postfix virtual maps created during that run.
- **Password Hashing** — Mailbox passwords are hashed with bcrypt (`BLF-CRYPT`) via `doveadm pw` before being written to `/etc/dovecot/users`. Plain-text passwords are never stored.
- **TLS Enforced** — Dovecot requires SSL (`ssl = required`), and the submission port enforces STARTTLS. Plain-text IMAP on port 143 is available but only upgrades via STARTTLS.
