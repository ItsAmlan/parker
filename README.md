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
2. **Cloudflare Lookup** *(read-only)* — Finds the DNS zone (by asking Cloudflare, so multi-part endings such as `example.co.nz` or `example.com.br` resolve to the right zone), or asks whether to create one.
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

### Nginx defaults

Generated configs also include:

- **gzip** for text, JSON, JavaScript, SVG and fonts (also for proxied apps that don't compress themselves).
- **Security headers** (`X-Content-Type-Options`, `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy`) on nginx-served sites (PHP/WordPress/static). Proxied Node.js apps keep control of their own headers, so none are added there. Disable with `NGINX_SECURITY_HEADERS=0`.
- **Static SPA**: one-year caching for the fingerprinted `/assets/` directory (a missing asset returns 404 instead of `index.html`).
- **Proxy timeouts** of 300s so idle WebSocket connections aren't cut after the 60s default.
- **`listen [::]:80`** only when the host can open IPv6 sockets (`NGINX_IPV6=auto|1|0`).

### Project record (`.parker.json`)

Parker writes a small `.parker.json` in each project directory (type, port, hostnames). Re-running for the same domain defaults to those answers, and `--list` reads it. It is safe to commit or to add to your project's `.gitignore`.

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

#### If certificate validation fails

Before running certbot, Parker checks **every hostname** the way Let's Encrypt will: it requests the challenge file *by name over plain HTTP*, through DNS (and through Cloudflare if the record is proxied), without following redirects. If something is wrong it says what, in plain words, and the final summary repeats **what Let's Encrypt itself reported** per hostname, with a hint. Typical causes:

| You see | Cause | Fix |
|---|---|---|
| "HTTP is redirected to HTTPS (Cloudflare…)" | Cloudflare's **Always Use HTTPS** (or a redirect rule) sends the validation request to HTTPS, where no certificate exists yet | Cloudflare → Rules → Configuration Rules: turn off *Always Use HTTPS* for `/.well-known/acme-challenge/*` (or set the records to **DNS-only** until the certificate exists) |
| "Cloudflare answered 52x" | Cloudflare cannot reach this server on port 80 | Point the record at this server; open port 80 |
| "answered 404 … a DIFFERENT server" | The name resolves to another server than this one | Check the DNS record / `CNAME_TARGET` |
| "answered 403" | A WAF/firewall rule, or nginx cannot read the challenge directory | `namei -l <project>/.well-known/acme-challenge`: every directory on the path needs search permission for the nginx user |
| "does not resolve" / `NXDOMAIN` | No DNS record for that exact name (often `www`) | Create it; give DNS a moment (Parker retries only this case) |
| "timed out" / "could not connect" | Port 80 is not reachable from the internet | Open it in the firewall / cloud security group |
| "rate limiting" | Let's Encrypt allows only about **5 failed validations per hour per name** | Fix the cause, wait, and test with the dry-run command below |

#### DNS must work *publicly* before certbot is even tried

Let's Encrypt does not use this server's resolver. It asks public validating resolvers for **both the A and the AAAA record** of every name, and a `SERVFAIL` on either one fails the validation, even when the A record is fine. So before calling certbot Parker asks public resolvers (Cloudflare and Google, over DNS-over-HTTPS) the same two questions for every hostname.

If they say the name is broken, Parker **skips certbot entirely**, because every failed attempt counts against the hourly limit. The site itself is still set up over HTTP, and the summary says what the resolvers answered and which commands to run. If the domain already had a certificate, the run is rolled back instead of downgrading it. If no public resolver can be reached (for example an egress firewall), that is not evidence of a problem, and certbot is attempted as before.

| Public DNS says | Usual cause | Fix |
|---|---|---|
| `SERVFAIL` (`No Reachable Authority`) | The nameservers at the **registrar** are not the ones Cloudflare assigned to the zone (a new zone, or a domain moved from another host), the zone is still `pending`, or a stale **DNSSEC `DS` record** from the previous DNS host is left at the registrar | Set exactly the Cloudflare nameservers at the registrar; remove any old DS record (or turn DNSSEC off at the old host) and wait for propagation |
| `NXDOMAIN` | No record for that exact name, often `www` | Create it |
| "no A or AAAA record" | The name exists but points nowhere | Add an A/AAAA record or a CNAME |

Check it yourself:

```bash
dig NS example.com +short                  # must be the nameservers Cloudflare assigned
dig @1.1.1.1 example.com A                 # status must be NOERROR, not SERVFAIL
dig @1.1.1.1 example.com AAAA
dig @1.1.1.1 www.example.com A
```

When Parker creates a new Cloudflare zone, it checks the registrar's nameservers again after you press Enter and warns if they still differ. When it creates DNS records itself it waits (up to 90 seconds) for public DNS to catch up; with `--no-dns` it checks once.

Parker does **not** retry a failure that retrying cannot fix (a redirect, 403/404, unreachable port): every failed attempt counts against that hourly limit, and repeating one would lock the name out for an hour even after you fix the cause. Only DNS-propagation failures are retried.

To test after fixing something, without spending the limit (this uses Let's Encrypt's *staging* server):

```bash
sudo certbot certonly --dry-run --webroot --webroot-path /path/to/project -d example.com -d www.example.com
```

Then issue and install for real with the command Parker prints in its summary. The full log is `/var/log/letsencrypt/letsencrypt.log`.

The dashboard streams every step in real time and presents interactive quick-action buttons for each prompt.

## Features

- **Command-line automation** — Every question has a flag; `--yes` runs without prompts (see [Command-Line Usage](#command-line-usage)). `--list` and `--remove` manage what Parker created.
- **Interactive Terminal** — Full PTY session streamed over WebSocket. Every prompt from `parker.py` appears in the browser with context-aware quick-answer buttons (Yes/No, project type selection, etc.).
- **Dry Run Mode** — Test the entire provisioning flow without touching DNS, filesystem, or services.
- **Validate First, Change Later** — All prompts, validation and preflight checks happen before the first change. Invalid input re-prompts instead of aborting halfway.
- **Automatic Rollback** — If any step fails, `parker.py` reverses all changes made during that run (DNS records, files, symlinks, configs) and reloads Nginx with the restored configuration. Config files are written atomically.
- **SSL Never Breaks a Live Site** — If certbot fails on a new domain, the site stays up over HTTP and the exact command to retry is printed. If the domain already had a certificate, the run is rolled back so HTTPS is never downgraded.
- **Duplicate Detection** — Before provisioning begins, parker checks for existing Nginx configs, project directories, and SSL certificates. If the domain is already parked, you're prompted before anything is modified.
- **Audit Log** — Every real run is appended to `/var/log/parker.log` (mode 600; typed passwords are never logged).
- **Dark / Light / System Theme** — Glassmorphism-styled UI with persistent theme preference.
- **Zero Trust Ready** — Binds to `127.0.0.1` and is exposed exclusively through a Cloudflare Tunnel with access policies.

---

## Command-Line Usage

Run interactively (what the dashboard does), or pass answers as flags:

```bash
# Next.js app on port 3000, DNS via Cloudflare, no prompts at all
sudo venv/bin/python3 parker.py --yes --domain shop.example.com --no-www --type node --port 3000 --no-dns

# Static SPA with an Express API on 4000
sudo venv/bin/python3 parker.py --domain app.example.com --type static --port 4000

sudo venv/bin/python3 parker.py --list                       # what Parker manages
sudo venv/bin/python3 parker.py --remove shop.example.com    # undo (asks first; --yes to skip)
sudo venv/bin/python3 parker.py --dry-run ...                # show everything, change nothing
```

| Flag | Meaning |
|---|---|
| `--domain D`, `--www` / `--no-www` | Domain to park; add the `www` variant or not |
| `--type T` | `1`/`php`, `2`/`wordpress`, `3`/`static`, `4`/`node` (also `next`, `nuxt`) |
| `--dir PATH` | Project directory (must be inside `WEBROOT`; default `WEBROOT/<domain>`) |
| `--port N` | App port (`node`) or `/api/` backend port (`static`) |
| `--owner USER[:GROUP]` | Owner of newly created directories (default: `PROJECT_OWNER`) |
| `--no-dns` | Skip Cloudflare DNS and mail DNS |
| `--mail-dns` / `--no-mail-dns` | SPF/DKIM/DMARC/MX (opt-in with `--yes`) |
| `--no-ssl` | Skip certbot |
| `--force` | Accept re-provisioning, duplicate hostnames and shared ports |
| `--yes`, `-y` | Never prompt. Flags and defaults are used; anything missing is an error. |
| `--dry-run` | Show what would happen without changing anything |
| `--list` | List Parker-managed sites (read-only) |
| `--remove D` | Remove a site: nginx config + symlink, its certificate (`--keep-cert` to keep it) and, with `--remove-dns`, its Cloudflare CNAMEs |

Notes:

- With `--yes`, a missing Cloudflare zone is an error (creating one needs a manual nameserver change): pass `--no-dns`, or create the zone first.
- Mailboxes need passwords, so they are only ever collected interactively.
- `--remove` never deletes project files or mail configuration (DKIM keys, mailboxes, mail DNS records). It restores the nginx config if the reload would fail.
- The dashboard only runs `parker.py` and `parker.py --dry-run` (see the sudoers rule below), so `--list`, `--remove` and all flags are CLI-only.

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

### Quick start (installer)

`install.sh` sets up the dashboard end to end: service user, virtualenv, sudo rule, systemd service and log rotation. It is idempotent, so running it again is also the upgrade procedure.

```bash
sudo git clone <your-repo-url> /opt/parker
cd /opt/parker
sudo ./install.sh --install-packages       # nginx, certbot, python3-venv, ... (Debian/Ubuntu)
```

Install it somewhere only root can modify (`/opt/parker` is a good choice): the checkout **is** the installation.

What it does, in order:

| Step | Details |
|---|---|
| Preflight | root, Python 3.10+, systemd, free port, complete checkout, safe install path |
| Packages *(with `--install-packages`)* | `nginx certbot python3-certbot-nginx python3 python3-venv curl sudo`; plus the mail stack with `--with-mail` |
| Service user | `parker`: system user, no home, `nologin` shell |
| Virtualenv | `venv/` created **by root**; `pip install -r requirements.txt` |
| Permissions | Everything `sudo` runs as root is made root-owned and the script **verifies the service user cannot modify it** (see the note below) |
| `.env` | Created from `.env.example` (asks for your Cloudflare token, account ID, SSL email, CNAME target, mail host and web root when run interactively), mode **600 root:root**. An existing `.env` is never overwritten. Warns about missing settings and **stops if `.env` cannot be read**. |
| Dashboard settings | `/etc/parker/parker-ui.env` (no secrets) and an install record |
| PHP snippet | If `snippets/php8.5.conf` is missing and a PHP-FPM socket exists, it creates the snippet |
| Sudo rule | `/etc/sudoers.d/parker`, validated with `visudo -c` before it is installed |
| systemd | `parker-ui.service` (enabled and started), `/etc/logrotate.d/parker` for the audit log |
| Verification | Waits for the dashboard to answer, then proves the sudo rule allows exactly the two intended commands and nothing else |

Options:

| Option | Meaning |
|---|---|
| `--install-packages` | `apt-get install` the web stack (only what is missing) |
| `--with-mail` | Also install Postfix/Dovecot/OpenDKIM/OpenDMARC packages and create the `vmail` user (uid 5000), `/var/mail/vhosts` and the empty map files. **The mail server configuration stays manual**: see the sections above. |
| `--env-file PATH` | Use a prepared `.env` (only if none exists yet) |
| `--public-url URL` | Public dashboard URL, allowed as WebSocket origin (see *Cloudflare Tunnel Setup*) |
| `--cloudflared` | Install the Cloudflare Tunnel connector; asks for the tunnel token (or set `CLOUDFLARED_TOKEN`) |
| `--user NAME`, `--port N` | Service user (default `parker`) and port (default `9000`; always bound to `127.0.0.1`) |
| `--no-start` | Install without starting the service |
| `-y`, `--yes` | Never prompt |
| `-n`, `--dry-run` | Show every action and generated file; change nothing |

```bash
sudo ./install.sh --check        # verify an existing installation (read-only)
sudo ./install.sh --uninstall    # remove service, sudo rule, log rotation, settings (add --purge for venv + user)
git pull && sudo ./install.sh    # upgrade
```

`--uninstall` never touches `.env`, project files, nginx sites, certificates or mail data.

#### Upgrading an existing deployment

`install.sh` **does not download or update code**: it installs whatever is in the checkout it runs from. Update the code first, then run it.

```bash
cd /opt/parker && git pull          # as the user who owns the checkout, then:
sudo ./install.sh --dry-run         # review
sudo ./install.sh
```

If your checkout is still owned by the old `parker` service user, `sudo git pull` is refused ("dubious ownership"). Pull as the owner (`sudo -u parker git pull`) and let the installer take over ownership; **don't** work around the error with `git config --global safe.directory`, because root would then run git in a repository the service user can write to. After the first install the checkout (including `.git`) is root-owned and a plain `sudo git pull` works.

| What happens on an upgrade | |
|---|---|
| Refreshed | virtualenv dependencies, the sudo rule, `parker-ui.service`, log rotation; the service is restarted |
| Backed up first | any existing unit/sudoers/logrotate file that differs is copied to `/etc/parker/backups/` (customisations are never silently lost; `--uninstall` keeps these) |
| Repaired | files, **including `.git`**, still owned by the service user are handed to root. A virtualenv that the service user owned is **rebuilt from scratch** (its contents can't be trusted, and the installer runs it as root), so expect it to re-download the dependencies. |
| Kept as is | `.env` (never overwritten, never merged: **compare it with `.env.example`: `WEBROOT` and `CNAME_TARGET` must now be set explicitly** (older versions silently fell back to built-in values, so add the values you were actually using); it becomes root-only, so dashboard settings such as `PARKER_VENV_PYTHON` or `PARKER_ALLOWED_ORIGINS` must move to `/etc/parker/parker-ui.env`; the installer warns) |
| **Not** changed | nginx sites that Parker already created, certificates, DNS, mail. Existing sites keep their old config (no gzip/security headers/ACME-in-project path) until you re-run Parker for that domain with `--force`. Sites made by the old version carry no "Managed by Parker" marker, so `--list` does not show them and `--remove` needs `--force`. |

> **Why the installer is strict about ownership.** The dashboard user may run exactly two commands as root through sudo: `parker.py` and `parker.py --dry-run`. If that user could modify `parker.py`, the Python interpreter or the venv, it could become root. So everything `sudo` runs must be owned by root and not writable by the service user. Older instructions that said `chown -R parker:parker /path/to/parker` created exactly that hole: the installer detects it, repairs it (`chown root:root`), and refuses to continue if it cannot make the install safe (for example when a parent directory is writable by the service user).

> **`.env` is root-only** (it holds your Cloudflare token). The dashboard never reads it; its own settings live in `/etc/parker/parker-ui.env`. Edit `.env` with `sudo nano /opt/parker/.env`.

### Configuration (`.env`)

The installer creates `.env`; you can also edit it afterwards.

```env
CLOUDFLARE_API_TOKEN=your_cloudflare_api_token
CLOUDFLARE_ACCOUNT_ID=your_cloudflare_account_id
CNAME_TARGET=server.example.com       # hostname new sites' DNS records point to: THIS server's public hostname
DEFAULT_SSL_EMAIL=ssl@example.com
WEBROOT=/var/www                      # base directory for project directories
MAIL_HOSTNAME=mail.example.com        # only for mail setup
DKIM_SELECTOR=default                 # only for mail setup
MX_HOSTNAME=mail.example.com          # optional: defaults to MAIL_HOSTNAME when missing or empty
PHP_FPM_SNIPPET=snippets/php8.5.conf  # optional: Nginx PHP-FPM include snippet (this is the default)
```

**There are no built-in values for settings that belong to your server.** Parker used to fall back to hostnames and paths baked into the code; those silently ended up in DNS records and on disk of servers they were never meant for. Now a run that needs a setting stops, **before changing anything**, and names it:

| Setting | Needed for | If missing |
|---|---|---|
| `WEBROOT` | every provisioning run | the run stops before the first question |
| `CNAME_TARGET` (`DEFAULT_CNAME_TARGET` is accepted too) | creating (or, with `--remove-dns`, removing) DNS records | stops; use `--no-dns` to provision without DNS |
| `MAIL_HOSTNAME`, `DKIM_SELECTOR` | mail authentication (SPF/DKIM/DMARC/MX) | stops when you choose mail setup |
| `MX_HOSTNAME` | the MX record | optional: falls back to `MAIL_HOSTNAME` |

Values left as the examples from `.env.example` (anything containing `yourdomain.com` or starting with `your_`) count as **not set**, so copying the example file verbatim cannot put placeholder hostnames into your DNS. `install.sh` and `install.sh --check` warn about missing settings, so you find out at install time instead of at the first run.

**An unreadable `.env` stops everything.** If `.env` exists but cannot be read (wrong permissions, it is a directory, bad encoding), Parker prints what is wrong and exits instead of continuing with missing or default settings. A normal user hitting a root-only `.env` is told to use `sudo`. (The dashboard itself deliberately cannot read `.env`; see above.)

Trailing `# comments` and quoted values are supported. More optional settings (`PROJECT_OWNER`, `NGINX_SECURITY_HEADERS`, `NGINX_IPV6`, `PARKER_LOG_FILE`, `PARKER_ALLOWED_ORIGINS`) are documented in [.env.example](.env.example).

> **Note:** `PARKER_SCRIPT_PATH` and `PARKER_VENV_PYTHON` are auto-derived from the project directory layout by the dashboard. You only need to set them if your `parker.py` or venv lives outside the standard structure.

After the installer finishes, test the CLI:

```bash
sudo /opt/parker/venv/bin/python3 /opt/parker/parker.py --dry-run
```

### Manual installation

Everything the installer does, step by step. Use `/opt/parker` (or another root-owned location) and run all of this as root.

**1. Clone and create the virtualenv as root**

```bash
sudo git clone <your-repo-url> /opt/parker
cd /opt/parker
sudo python3 -m venv venv
sudo venv/bin/pip install -r requirements.txt
```

**2. Configure, root-only**

```bash
sudo install -m 600 -o root -g root .env.example .env
sudo nano .env
```

**3. Create a dedicated service user. Do not give it ownership of the install.**

```bash
sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin parker
```

> ⚠️ Do **not** `chown -R parker:parker` the install. The code under it runs as root via sudo; the service user must only be able to read it. Verify with `sudo -u parker test -w /opt/parker/parker.py && echo WRITABLE` (it must print nothing).

**4. Sudo rule**: exactly these two commands (sudo matches the full argument string, so no other flag is allowed):

```bash
sudo visudo -f /etc/sudoers.d/parker
```

```
parker ALL=(root) NOPASSWD: /opt/parker/venv/bin/python3 /opt/parker/parker.py
parker ALL=(root) NOPASSWD: /opt/parker/venv/bin/python3 /opt/parker/parker.py --dry-run
```

**5. systemd service**: create `/etc/systemd/system/parker-ui.service`:

```ini
[Unit]
Description=Parker Dashboard - Domain Parking Web UI
After=network.target nginx.service

[Service]
Type=simple
User=parker
Group=parker
WorkingDirectory=/opt/parker/parker-ui
EnvironmentFile=-/etc/parker/parker-ui.env
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/opt/parker/venv/bin/uvicorn main:app --host 127.0.0.1 --port 9000
Restart=always
RestartSec=5
TimeoutStopSec=60
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Do not add sandboxing options (`NoNewPrivileges`, `ProtectSystem`, ...): the `sudo parker.py` child inherits them, and they would break sudo or make `/etc` read-only for provisioning runs.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now parker-ui
sudo systemctl status parker-ui
```

---

## Cloudflare Tunnel Setup

The dashboard binds to `127.0.0.1:9000` and must **never** be exposed directly to the internet. Use a Cloudflare Tunnel to securely expose it behind Zero Trust authentication.

> **Shortcut:** create the tunnel in the Zero Trust dashboard (*Networks → Tunnels*, public hostname → `http://127.0.0.1:9000`), copy its connector token, and run `sudo CLOUDFLARED_TOKEN=<token> ./install.sh --yes --cloudflared`. The Access policy (step 7) is still manual.

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

The terminal WebSocket also checks the browser's `Origin` header against the `Host` it was reached on, so a malicious page in your logged-in browser cannot open a root provisioning session (cross-site WebSocket hijacking). If your tunnel or proxy rewrites the `Host` header, list the dashboard's public URL in `.env`:

```env
PARKER_ALLOWED_ORIGINS=https://parker.yourdomain.com
```

---

## Directory Structure

```
/path/to/parker/
├── README.md               # This file
├── .env                    # Shared credentials (API tokens, config)
├── .env.example            # Template for .env with all supported variables
├── .gitignore              # Git ignore rules
├── install.sh              # Dashboard installer / --check / --uninstall
├── parker.py               # CLI provisioning script (runs as root)
├── requirements.txt        # Runtime dependencies
├── requirements-dev.txt    # + pytest, httpx (for the test suite)
├── pytest.ini
├── tests/                  # Test suite (see Development)
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
- **PTY Isolation** — Each WebSocket session spawns an isolated pseudo-terminal process. Disconnecting the browser (or stopping the service) sends it SIGTERM, which `parker.py` treats like Ctrl-C: it **rolls back** what it had done. The dashboard waits up to 30 seconds for that rollback before resorting to SIGKILL, and the rollback itself ignores further signals.
- **Root-owned Code** — Everything sudo runs as root is owned by root and not writable by the service user (`install.sh` enforces and `--check` re-verifies this). `.env` is mode 600 root.
- **Domain Validation** — The frontend enforces `[A-Za-z0-9.-]+`, and `parker.py` itself validates the hostname (labels, length, TLD) and keeps project directories strictly inside `WEBROOT`.
- **Automatic Rollback** — If provisioning fails at any step, `parker.py` reverses all DNS records, directories, Nginx configs and symlinks, Dovecot users, and Postfix virtual maps created during that run, then reloads Nginx.
- **WebSocket Origin Check** — The terminal WebSocket rejects connections whose `Origin` is missing or foreign (see the Cloudflare Tunnel section).
- **Hidden Passwords** — Mailbox passwords are read without echo (the dashboard masks its input box too), passed to `doveadm` on stdin rather than the command line, and never written to the audit log.
- **Password Hashing** — Mailbox passwords are hashed with bcrypt (`BLF-CRYPT`) via `doveadm pw` before being written to `/etc/dovecot/users`. Plain-text passwords are never stored.
- **TLS Enforced** — Dovecot requires SSL (`ssl = required`), and the submission port enforces STARTTLS. Plain-text IMAP on port 143 is available but only upgrades via STARTTLS.

---

## Development

```bash
python3 -m venv venv && venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest
```

The suite needs no root and touches nothing on your machine: filesystem locations are redirected into a temp directory, and nginx/certbot/systemd/Dovecot/OpenDKIM commands and the Cloudflare API are faked. If `nginx` is installed, every generated config is also validated with the real `nginx -t`. Tests cover config generation for each project type, flags and `--yes`, rollback (including on SIGTERM/SIGHUP), SSL failure handling, mail/DNS, `--list`/`--remove`, the audit log, the dashboard's WebSocket origin check, and `install.sh` (argument validation, dry-run output, the generated unit and sudo rule, `.env` handling; shellcheck-clean). A few installer tests need root (real `sudo`, `useradd`, `chown`) and are skipped for normal users, so CI runs the rest. CI runs it on every push (`.github/workflows/tests.yml`).
