# Parker Dashboard 🚀

[![Release Version](https://img.shields.io/badge/release-v1.0.0-blue.svg)](https://github.com/your-username/parker)
[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg)](https://python.org)
[![Platform Support](https://img.shields.io/badge/OS-Ubuntu%2024.04%20LTS-orange.svg)](https://ubuntu.com)
[![FastAPI Web Framework](https://img.shields.io/badge/FastAPI-005571?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

A modern web-based control panel for [parker.py](../parker.py) — an automated domain parking utility that provisions DNS records, web server configs, SSL certificates, and email authentication in a single interactive session.

Built with **FastAPI** and a real-time **WebSocket pseudo-terminal**, Parker Dashboard lets you run the full provisioning workflow from your browser instead of SSH. Designed for secure deployment behind **Cloudflare Zero Trust** on Ubuntu 24.04 LTS servers.


## What Parker Does

When you enter a domain, `parker.py` walks through these steps automatically:

1. **DNS Setup** — Creates or reuses a Cloudflare zone, adds CNAME records (with optional `www` variant).
2. **Mail Authentication** — Generates DKIM keys and creates SPF, DKIM, and DMARC TXT records for outbound email (send-only via Postfix).
3. **Project Scaffolding** — Sets up the project directory with optional boilerplate (Custom PHP, WordPress, or React + Vite + TailwindCSS).
4. **Nginx Configuration** — Generates and deploys server blocks with PHP-FPM or reverse proxy support.
5. **SSL Provisioning** — Obtains and installs Let's Encrypt certificates via Certbot.

The dashboard streams every step in real time and presents interactive quick-action buttons for each prompt.

## Features

- **Interactive Terminal** — Full PTY session streamed over WebSocket. Every prompt from `parker.py` appears in the browser with context-aware quick-answer buttons (Yes/No, project type selection, etc.).
- **Dry Run Mode** — Test the entire provisioning flow without touching DNS, filesystem, or services.
- **Automatic Rollback** — If any step fails, `parker.py` reverses all changes made during that run (DNS records, files, symlinks, configs).
- **Dark / Light / System Theme** — Glassmorphism-styled UI with persistent theme preference.
- **Zero Trust Ready** — Binds to `127.0.0.1` and is exposed exclusively through a Cloudflare Tunnel with access policies.

---

## Prerequisites

Parker is built for **Ubuntu 24.04 LTS** servers. The following must be installed and configured before deployment.

### System Packages

```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx \
                    opendkim opendkim-tools postfix \
                    python3 python3-venv curl
```

| Package | Purpose |
|---|---|
| `nginx` | Serves websites and reverse-proxies React apps |
| `certbot` + `python3-certbot-nginx` | Automated Let's Encrypt SSL certificates |
| `opendkim` + `opendkim-tools` | DKIM key generation and mail signing |
| `postfix` | Outbound-only mail relay (contact forms, notifications) |
| `python3` + `python3-venv` | Runs both `parker.py` and the dashboard |

### Node.js (Optional — only for React + Vite projects)

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

Ensure the `sites-available` / `sites-enabled` directory structure is in place (default on Ubuntu):

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

Verify that Postfix is configured to use OpenDKIM as a milter in `/etc/postfix/main.cf`:

```ini
milter_default_action = accept
milter_protocol = 6
smtpd_milters = inet:localhost:8891
non_smtpd_milters = inet:localhost:8891
```

And that OpenDKIM listens on the same socket in `/etc/opendkim.conf`:

```ini
Socket    inet:8891@localhost
KeyTable  /etc/opendkim/key.table
SigningTable  refile:/etc/opendkim/signing.table
```

### Postfix (Send-Only)

If you only need outbound mail (recommended), lock down Postfix:

```bash
sudo postconf -e "inet_interfaces = loopback-only"
sudo systemctl restart postfix
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
pip install requests fastapi uvicorn jinja2
deactivate
```

### 3. Configure Environment Variables

Create `/path/to/parker/.env`:

```env
CLOUDFLARE_API_TOKEN=your_cloudflare_api_token
CLOUDFLARE_ACCOUNT_ID=your_cloudflare_account_id
DEFAULT_SSL_EMAIL=ssl@yourdomain.com
MAIL_HOSTNAME=mail.yourdomain.com
DKIM_SELECTOR=default
```

> **Note:** `PARKER_SCRIPT_PATH` and `PARKER_VENV_PYTHON` are auto-derived from the project directory layout. You only need to set them in `.env` if your `parker.py` or venv lives outside the standard structure.

Symlink it into the UI directory so both `parker.py` and `main.py` read the same file:

```bash
ln -s /path/to/parker/.env /path/to/parker/parker-ui/.env
```

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
├── parker.py               # CLI provisioning script (runs as root)
├── venv/                   # Python virtual environment
└── parker-ui/
    ├── .env -> ../.env     # Symlink to shared credentials
    ├── main.py             # FastAPI application
    ├── templates/
    │   └── index.html      # Dashboard UI (single-page)
    └── static/
        └── ui-mockup.svg   # Design reference
```

---

## Security Notes

- **Localhost Only** — The dashboard binds to `127.0.0.1` and is never directly reachable from the network. All external access goes through the Cloudflare Tunnel.
- **Minimal Sudo Surface** — The sudoers rule only allows executing `parker.py` via the project's venv Python, with or without `--dry-run`. No other commands are permitted.
- **PTY Isolation** — Each WebSocket session spawns an isolated pseudo-terminal process. Disconnecting the browser terminates the process within 3 seconds.
- **Domain Validation** — The frontend enforces `[A-Za-z0-9.-]+` on domain input before sending it to the backend.
- **Automatic Rollback** — If provisioning fails at any step, `parker.py` reverses all DNS records, files, and Nginx symlinks created during that run.
