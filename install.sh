#!/usr/bin/env bash
#
# Parker dashboard installer (parker-ui): service user, virtualenv, sudoers rule,
# systemd service, log rotation and, optionally, system packages, mail prerequisites
# and a Cloudflare Tunnel connector.
#
#   sudo ./install.sh [options]        install or upgrade (safe to re-run)
#   sudo ./install.sh --check          verify an existing installation
#   sudo ./install.sh --uninstall      remove the service (add --purge for user + venv)
#
# Run it from the checkout you want to install: that directory IS the installation
# (a root-owned location such as /opt/parker is recommended). Re-running after a
# `git pull` is the upgrade procedure.
#
# Security model (why the script is picky):
#   * The dashboard user can run exactly two commands as root through sudo
#     (`parker.py` and `parker.py --dry-run`). If that user could modify parker.py,
#     the python interpreter or the venv, that would be a trivial root escalation, so
#     the installer makes everything sudo runs root-owned and verifies the service
#     user cannot write to it.
#   * .env holds the Cloudflare token and is root-only. The dashboard never needs it.
#
# Test hooks (not for normal use): PARKER_SYSTEMD_DIR, PARKER_SUDOERS_DIR,
# PARKER_CONF_DIR, PARKER_LOGROTATE_DIR, PARKER_NGINX_DIR, PARKER_SYSTEMD_RUNTIME_DIR.

set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

SYSTEMD_DIR="${PARKER_SYSTEMD_DIR:-/etc/systemd/system}"
SUDOERS_DIR="${PARKER_SUDOERS_DIR:-/etc/sudoers.d}"
CONF_DIR="${PARKER_CONF_DIR:-/etc/parker}"
LOGROTATE_DIR="${PARKER_LOGROTATE_DIR:-/etc/logrotate.d}"
NGINX_DIR="${PARKER_NGINX_DIR:-/etc/nginx}"
SYSTEMD_RUNTIME_DIR="${PARKER_SYSTEMD_RUNTIME_DIR:-/run/systemd/system}"

SERVICE_NAME="parker-ui"
SERVICE_USER="parker"
PORT=9000
MODE="install"

DRY_RUN=0
ASSUME_YES=0
INSTALL_PACKAGES=0
WITH_MAIL=0
SETUP_CLOUDFLARED=0
NO_START=0
PURGE=0
ENV_FILE=""
PUBLIC_URL=""
CLOUDFLARED_TOKEN="${CLOUDFLARED_TOKEN:-}"

BASE_PACKAGES=(nginx certbot python3-certbot-nginx python3 python3-venv curl sudo)
MAIL_PACKAGES=(opendkim opendkim-tools opendmarc postfix postfix-policyd-spf-python dovecot-core dovecot-imapd)

WARNINGS=()
FAILURES=0
BACKUP_STAMP=$(date +%Y%m%d-%H%M%S)

# --------------------------------------------------------------------------- output

if [[ -t 1 ]]; then
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_BOLD=$'\033[1m'; C_OFF=$'\033[0m'
else
  C_RED=""; C_GREEN=""; C_YELLOW=""; C_BOLD=""; C_OFF=""
fi

step() { printf '\n%s==> %s%s\n' "$C_BOLD" "$*" "$C_OFF"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    %s✔%s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
warn() { printf '    %s!%s %s\n' "$C_YELLOW" "$C_OFF" "$*"; WARNINGS+=("$*"); }
bad()  { printf '    %s✘%s %s\n' "$C_RED" "$C_OFF" "$*"; FAILURES=$((FAILURES + 1)); }
die()  { printf '\n%sERROR:%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage: sudo ./install.sh [options]

Modes
  (default)             install or upgrade the Parker dashboard
  --check               verify an existing installation (read-only)
  --uninstall           remove service, sudoers rule, log rotation and config
                        (--purge: also delete the venv and the service user)

Install options
  --user NAME           service user to create/use                  [parker]
  --port N              dashboard port (always bound to 127.0.0.1)  [9000]
  --install-packages    apt-get install nginx, certbot, python3-venv, ...
  --with-mail           also install Postfix/Dovecot/OpenDKIM/OpenDMARC packages
                        and create the vmail user and empty map files. The mail
                        server configuration itself stays manual (see README).
  --env-file PATH       use PATH as .env (if .env does not exist yet)
  --public-url URL      public URL of the dashboard (https://parker.example.com);
                        allowed as WebSocket origin if your proxy rewrites Host
  --cloudflared         install the Cloudflare Tunnel connector (asks for the
                        tunnel token; or set CLOUDFLARED_TOKEN in the environment)
  --no-start            install everything but do not start the service
  -y, --yes             never prompt; use defaults
  -n, --dry-run         show what would be done, change nothing
  -h, --help            show this help

Examples
  sudo ./install.sh --install-packages
  sudo ./install.sh --yes --env-file /root/parker.env --public-url https://parker.example.com
  sudo CLOUDFLARED_TOKEN=eyJ... ./install.sh --yes --cloudflared
EOF
}

# --------------------------------------------------------------------------- helpers

run() {
  if (( DRY_RUN )); then
    printf '    [dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

have() { command -v "$1" >/dev/null 2>&1; }

# verdict "pass message" "fail message" LEVEL cmd...   (LEVEL: bad | warn | info)
verdict() {
  local pass_msg=$1 fail_msg=$2 level=$3
  shift 3
  if "$@"; then ok "$pass_msg"; else "$level" "$fail_msg"; fi
}

os_release_field() { sed -n "s/^$1=//p" /etc/os-release 2>/dev/null | head -n 1 | tr -d '"'; }

is_tty() { [[ -t 0 && -t 1 ]]; }

# confirm "question" [y|n]  -> 0 for yes. Non-interactive runs take the default.
confirm() {
  local question=$1 default=${2:-y} reply
  if (( ASSUME_YES )) || ! is_tty; then
    [[ $default == y ]]
    return
  fi
  read -r -p "    ${question} [${default}]: " reply || reply=""
  reply=${reply:-$default}
  [[ $reply =~ ^[Yy] ]]
}

prompt_value() {      # prompt_value "label" "default"  -> echoes the value
  local label=$1 default=${2:-} reply
  if (( ASSUME_YES )) || ! is_tty; then
    printf '%s' "$default"
    return
  fi
  read -r -p "    ${label}${default:+ [$default]}: " reply || reply=""
  printf '%s' "${reply:-$default}"
}

prompt_secret() {     # prompt_secret "label"  -> echoes the value (input hidden)
  local label=$1 reply
  if (( ASSUME_YES )) || ! is_tty; then
    return 0
  fi
  read -r -s -p "    ${label}: " reply || reply=""
  printf '\n' >&2
  printf '%s' "$reply"
}

# write_file PATH MODE OWNER:GROUP   (content on stdin). Atomic; skips identical content.
write_file() {
  local path=$1 mode=$2 owner=$3 content tmp
  content=$(cat)
  if [[ -f $path ]] && [[ "$(cat "$path")" == "$content" ]]; then
    ok "unchanged: $path"
    return 0
  fi
  # Never silently destroy a file someone may have customised: keep a copy. Backups live in
  # one directory outside the config dirs that scan their contents (a stray .bak in
  # /etc/logrotate.d would be parsed as a second config).
  local backup=""
  if [[ -f $path && $path != "$CONF_DIR"/* ]]; then
    backup=$CONF_DIR/backups/$(basename "$path").$BACKUP_STAMP
  fi

  if (( DRY_RUN )); then
    [[ -n $backup ]] && printf '    [dry-run] back up existing %s to %s\n' "$path" "$backup"
    printf '    [dry-run] write %s (mode %s, owner %s):\n' "$path" "$mode" "$owner"
    printf '%s\n' "$content" | sed 's/^/        | /'
    return 0
  fi
  if [[ -n $backup ]]; then
    mkdir -p "$CONF_DIR/backups"
    chmod 700 "$CONF_DIR/backups"
    cp -p "$path" "$backup"
    warn "replaced an existing $path; the previous version is saved as $backup"
  fi
  mkdir -p "$(dirname "$path")"
  tmp=$(mktemp "${path}.XXXXXX")
  printf '%s\n' "$content" > "$tmp"
  chmod "$mode" "$tmp"
  chown "$owner" "$tmp"
  mv -f "$tmp" "$path"
  ok "wrote $path"
}

# .env reading/writing with the same rules as parker.py's loader (comments, quotes).
envtool() {
  python3 - "$@" <<'PY'
import os, re, sys

def parse(line):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    if line.startswith("export "):
        line = line[7:].lstrip()
    key, value = line.split("=", 1)
    key, value = key.strip(), value.strip()
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        if end != -1:
            return key, value[1:end]
    elif value.startswith("#"):
        value = ""
    else:
        value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
    return key, value

action, path, key = sys.argv[1:4]
try:
    lines = open(path).read().splitlines()
except FileNotFoundError:
    lines = []
except (OSError, UnicodeDecodeError) as e:
    sys.exit("cannot read %s: %s" % (path, getattr(e, "strerror", None) or type(e).__name__))

if action == "get":
    found = ""
    for line in lines:
        parsed = parse(line)
        if parsed and parsed[0] == key:
            found = parsed[1]
    print(found)
elif action == "set":
    value = os.environ["PARKER_ENV_VALUE"]
    if '"' in value and "'" in value:
        sys.exit("value cannot contain both quote characters")
    if re.search(r"\s#|^\s|\s$", value) or value[:1] in ("'", '"', "#"):
        value = ("'%s'" if '"' in value else '"%s"') % value
    new = f"{key}={value}"
    for i, line in enumerate(lines):
        parsed = parse(line)
        if parsed and parsed[0] == key:
            lines[i] = new
            break
    else:
        lines.append(new)
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
PY
}

env_get() { envtool get "$1" "$2"; }

# An .env that exists but cannot be read must stop the run: carrying on would mean judging (and
# later running with) settings that are not the real ones. A missing .env is fine here.
require_readable_env() {
  local path=$1 reason
  [[ -e $path || -L $path ]] || return 0
  if ! reason=$(python3 -c '
import sys
try:
    open(sys.argv[1], encoding="utf-8").read()
except (OSError, UnicodeDecodeError) as e:
    print(getattr(e, "strerror", None) or type(e).__name__)
    sys.exit(1)' "$path"); then
    die "cannot read $path ($reason). Parker needs its settings and will not run with defaults instead. Fix the file (permissions, or it is a directory?) or, for a root-only .env, re-run with sudo."
  fi
}

# Parker has no built-in values for settings that belong to your server: a run that needs one
# stops and names it. Say so now, at install time, instead of at the first provisioning.
warn_about_settings() {
  local env=$1
  if placeholder "$(env_get "$env" WEBROOT)"; then
    warn "WEBROOT is not set in $env: Parker will refuse to provision until it is (e.g. WEBROOT=/var/www)"
  fi
  if placeholder "$(env_get "$env" CNAME_TARGET)"; then
    warn "CNAME_TARGET is not set in $env: Parker cannot create DNS records until it is (this server's public hostname, e.g. server.example.com; or use --no-dns)"
  fi
  if placeholder "$(env_get "$env" MAIL_HOSTNAME)" || placeholder "$(env_get "$env" DKIM_SELECTOR)"; then
    info "MAIL_HOSTNAME and/or DKIM_SELECTOR are not set in $env: only needed if you set up mail (SPF/DKIM/MX); MX_HOSTNAME defaults to MAIL_HOSTNAME"
  fi
}
env_set() { PARKER_ENV_VALUE="$3" envtool set "$1" "$2"; }

as_service_user() {
  if have runuser; then
    runuser -u "$SERVICE_USER" -- "$@"
  else
    sudo -n -u "$SERVICE_USER" -- "$@"
  fi
}

service_user_exists() { id -u "$SERVICE_USER" >/dev/null 2>&1; }

systemd_available() { [[ -d $SYSTEMD_RUNTIME_DIR ]] && have systemctl; }

venv_python() { printf '%s/venv/bin/python3' "$APP_DIR"; }

# Same rule as parker.py: empty, or an example value from .env.example, counts as "not set".
placeholder() {
  local v=${1,,}
  [[ -z $v || $v == your_* || $v == *yourdomain.com* ]]
}

# --------------------------------------------------------------------------- arguments

parse_args() {
  local args=() arg
  for arg in "$@"; do          # accept --opt=value as well as --opt value
    if [[ $arg == --*=* ]]; then args+=("${arg%%=*}" "${arg#*=}"); else args+=("$arg"); fi
  done
  set -- "${args[@]+"${args[@]}"}"

  while (( $# )); do
    case $1 in
      --check)             MODE=check ;;
      --uninstall)         MODE=uninstall ;;
      --purge)             PURGE=1 ;;
      --user)              [[ $# -ge 2 ]] || die "--user needs a value"; SERVICE_USER=$2; shift ;;
      --port)              [[ $# -ge 2 ]] || die "--port needs a value"; PORT=$2; shift ;;
      --install-packages)  INSTALL_PACKAGES=1 ;;
      --with-mail)         WITH_MAIL=1 ;;
      --env-file)          [[ $# -ge 2 ]] || die "--env-file needs a value"; ENV_FILE=$2; shift ;;
      --public-url)        [[ $# -ge 2 ]] || die "--public-url needs a value"; PUBLIC_URL=$2; shift ;;
      --cloudflared)       SETUP_CLOUDFLARED=1 ;;
      --cloudflared-token) [[ $# -ge 2 ]] || die "--cloudflared-token needs a value"
                           CLOUDFLARED_TOKEN=$2; SETUP_CLOUDFLARED=1; shift ;;
      --no-start)          NO_START=1 ;;
      -y|--yes)            ASSUME_YES=1 ;;
      -n|--dry-run)        DRY_RUN=1 ;;
      -h|--help)           usage; exit 0 ;;
      *)                   usage >&2; die "unknown option: $1" ;;
    esac
    shift
  done
}

validate_args() {
  [[ $SERVICE_USER =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "invalid user name: $SERVICE_USER"
  [[ $SERVICE_USER != root ]] || die "refusing to run the dashboard as root"
  if [[ ! $PORT =~ ^[0-9]+$ ]] || (( PORT < 1024 || PORT > 65535 )); then
    die "--port must be between 1024 and 65535"
  fi
  [[ -z $PUBLIC_URL || $PUBLIC_URL =~ ^https?://[A-Za-z0-9.-]+(:[0-9]+)?$ ]] \
    || die "--public-url must look like https://parker.example.com (no path)"
  [[ -z $ENV_FILE || -f $ENV_FILE ]] || die "--env-file not found: $ENV_FILE"
  # The path ends up in sudoers and a systemd unit; keep it free of characters that
  # need escaping in either (a wrongly escaped sudoers path is a security bug).
  [[ $APP_DIR =~ ^/[A-Za-z0-9_./-]+$ ]] \
    || die "install path '$APP_DIR' contains characters that are unsafe in sudoers/systemd; use e.g. /opt/parker"
}

# --------------------------------------------------------------------------- steps

preflight() {
  step "Preflight checks"

  if (( ! DRY_RUN )) && [[ $EUID -ne 0 ]]; then
    die "run as root: sudo ./install.sh"
  fi
  (( DRY_RUN )) && info "dry run: nothing will be changed"

  local required
  for required in parker.py parker-ui/main.py parker-ui/templates/index.html requirements.txt .env.example; do
    [[ -e $APP_DIR/$required ]] || die "$APP_DIR/$required is missing: run install.sh from a complete checkout"
  done
  ok "checkout: $APP_DIR"

  have python3 || die "python3 is not installed (try --install-packages)"
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Python 3.10+ is required (found $(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])'))"
  ok "python $(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"

  if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
    if (( INSTALL_PACKAGES )); then
      info "python3-venv will be installed with the packages"
    else
      die "python3-venv is missing: apt-get install python3-venv (or use --install-packages)"
    fi
  fi

  if systemd_available; then
    ok "systemd"
  elif (( DRY_RUN )); then
    warn "systemd not detected (dry run continues)"
  else
    die "systemd is not running on this host; the service cannot be installed"
  fi

  if [[ -r /etc/os-release ]] && ! grep -qiE 'debian|ubuntu' /etc/os-release; then
    warn "Parker targets Debian/Ubuntu; this host reports: $(os_release_field PRETTY_NAME)"
  fi

  # The port must be free unless it is our own running service.
  require_readable_env "$APP_DIR/.env"

  if have systemctl && systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    ok "$SERVICE_NAME is currently running: it will be restarted on port $PORT"
  else
    if have ss && [[ -n $(ss -ltnH "sport = :$PORT" 2>/dev/null) ]]; then
      die "port $PORT is already in use by another process (choose --port)"
    fi
    ok "port $PORT is available"
  fi
}

missing_packages() {
  local pkg
  for pkg in "$@"; do
    dpkg -s "$pkg" >/dev/null 2>&1 || printf '%s\n' "$pkg"
  done
}

install_packages() {
  (( INSTALL_PACKAGES || WITH_MAIL )) || return 0
  step "System packages"

  have apt-get || die "--install-packages needs apt-get (Debian/Ubuntu)"

  local wanted=()
  (( INSTALL_PACKAGES )) && wanted+=("${BASE_PACKAGES[@]}")
  (( WITH_MAIL )) && wanted+=("${MAIL_PACKAGES[@]}")

  local missing
  mapfile -t missing < <(missing_packages "${wanted[@]}")

  if (( ${#missing[@]} == 0 )); then
    ok "all packages already installed"
    return 0
  fi

  info "installing: ${missing[*]}"
  run env DEBIAN_FRONTEND=noninteractive apt-get update -qq
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
}

ensure_service_user() {
  step "Service user"

  if service_user_exists; then
    local uid shell
    uid=$(id -u "$SERVICE_USER")
    (( uid != 0 )) || die "'$SERVICE_USER' is uid 0; refusing"
    shell=$(getent passwd "$SERVICE_USER" | cut -d: -f7)
    ok "user '$SERVICE_USER' exists (uid $uid)"
    case $shell in
      */nologin|*/false) ;;
      *) warn "'$SERVICE_USER' has a login shell ($shell); a system user with nologin is safer" ;;
    esac
    if id -nG "$SERVICE_USER" | tr ' ' '\n' | grep -qxE 'sudo|wheel|admin'; then
      warn "'$SERVICE_USER' is in an admin group; it should have no sudo rights beyond Parker's two commands"
    fi
    return 0
  fi

  local nologin=/usr/sbin/nologin
  have nologin && nologin=$(command -v nologin)
  run useradd --system --user-group --no-create-home --home-dir /nonexistent --shell "$nologin" "$SERVICE_USER"
  ok "created system user '$SERVICE_USER'"
}

setup_venv() {
  step "Python environment"

  local py
  py=$(venv_python)

  if (( VENV_UNTRUSTED )) && [[ -e $APP_DIR/venv ]]; then
    # Never execute it: it was writable by the service user, and we are about to run python as root.
    warn "the existing venv was owned by '$SERVICE_USER' and cannot be trusted to run as root: rebuilding it from scratch"
    run rm -rf "$APP_DIR/venv"
    run python3 -m venv "$APP_DIR/venv"
    ok "created $APP_DIR/venv"
  elif [[ -x $py ]] && "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    ok "reusing $APP_DIR/venv"
  else
    [[ -e $APP_DIR/venv ]] && { warn "existing venv is broken or too old; recreating"; run rm -rf "$APP_DIR/venv"; }
    run python3 -m venv "$APP_DIR/venv"
    ok "created $APP_DIR/venv"
  fi

  info "installing requirements (as root: the service user never needs to write here)"
  run "$py" -m pip install --disable-pip-version-check --quiet -r "$APP_DIR/requirements.txt"

  if (( ! DRY_RUN )); then
    [[ -x $APP_DIR/venv/bin/uvicorn ]] || die "uvicorn was not installed into the venv"
    "$py" -c 'import fastapi, requests, jinja2, uvicorn' || die "venv is missing required modules"
  fi
  ok "dependencies installed"
}

# Set when the venv held files owned by the service user. Their contents cannot be trusted
# (re-owning a tampered file does not un-tamper it), so the venv is rebuilt instead of reused.
VENV_UNTRUSTED=0

# Hands anything the service user owns (left by the old `chown -R parker:parker` instructions)
# to root. Must run BEFORE anything from the install (the venv's python, ...) is executed as
# root, so it is called right after the user exists, and again by secure_install_dir.
reclaim_service_owned_files() {
  service_user_exists || return 0

  local group
  group=$(id -gn "$SERVICE_USER")

  # A shared primary group (e.g. "users") is not touched here; the writability checks catch
  # any access it grants. .git is included on purpose: root runs git there during upgrades
  # (`sudo git pull`), so a service-writable .git (hooks, config) would run attacker-chosen
  # code as root.
  local match=(-user "$SERVICE_USER")
  [[ $group == "$SERVICE_USER" ]] && match=(\( -user "$SERVICE_USER" -o -group "$group" \))

  # The venv's python is about to be executed as root, so ask the question that matters: could the
  # service user have changed anything in it? Ownership is one way; permission bits (a stray
  # chmod 777, a shared group) are another. Ask the system, as that user.
  if [[ -d $APP_DIR/venv ]]; then
    if [[ -n $(find "$APP_DIR/venv" -xdev "${match[@]}" -print -quit 2>/dev/null) ]]; then
      VENV_UNTRUSTED=1
    elif (( EUID == 0 )) && [[ -n $(as_service_user find "$APP_DIR/venv" -xdev -writable -print -quit 2>/dev/null) ]]; then
      warn "the venv is writable by '$SERVICE_USER' (permission bits, not ownership)"
      VENV_UNTRUSTED=1
    fi
  fi

  local owned
  owned=$(find "$APP_DIR" -xdev "${match[@]}" -print 2>/dev/null | head -n 1 || true)
  if [[ -n $owned ]]; then
    warn "files under $APP_DIR belong to '$SERVICE_USER' (e.g. $owned): handing them to root"
    run find "$APP_DIR" -xdev "${match[@]}" -exec chown root:root {} + -exec chmod go-w {} +
  fi
}

# Everything `sudo` runs as root, and the interpreter behind it, must not be modifiable
# by the service user. Fixes what it safely can, and refuses to continue otherwise.
secure_install_dir() {
  step "File permissions (code run as root must be root-owned)"

  if ! service_user_exists; then
    info "service user does not exist yet (dry run): skipping"
    return 0
  fi

  reclaim_service_owned_files

  (( DRY_RUN )) && { info "dry run: skipping write verification"; return 0; }

  # 2. Nothing in the install may be writable by the service user.
  local writable
  writable=$(as_service_user find "$APP_DIR" -xdev -writable -print 2>/dev/null | head -n 5 || true)
  if [[ -n $writable ]]; then
    printf '%s\n' "$writable" | sed 's/^/        /' >&2
    die "the service user can modify files under $APP_DIR (listed above). That would let it become root through sudo. Fix the ownership/mode and re-run."
  fi

  # 3. ...nor may it be able to swap the install directory out from under us.
  local dir=$APP_DIR
  while [[ $dir != / ]]; do
    dir=$(dirname "$dir")
    if [[ ! -k $dir ]] && as_service_user test -w "$dir"; then
      die "the service user can write to $dir, which contains the install. Move the install (e.g. /opt/parker) or fix its permissions."
    fi
  done
  ok "service user cannot modify the install"

  # 4. It must, however, be able to read and run the dashboard.
  as_service_user test -r "$APP_DIR/parker-ui/main.py" \
    || die "'$SERVICE_USER' cannot read $APP_DIR (a 0700 home directory?). Install under /opt/parker."
  as_service_user test -x "$APP_DIR/venv/bin/uvicorn" \
    || die "'$SERVICE_USER' cannot execute $APP_DIR/venv/bin/uvicorn"
  ok "service user can read and run the dashboard"
}

setup_env_file() {
  step "Configuration (.env)"
  local env=$APP_DIR/.env

  require_readable_env "$env"

  if [[ -f $env ]]; then
    ok ".env already exists: left untouched"
  elif [[ -n $ENV_FILE ]]; then
    run install -m 600 -o root -g root "$ENV_FILE" "$env"
    ok "installed $ENV_FILE as .env"
  else
    run install -m 600 -o root -g root "$APP_DIR/.env.example" "$env"
    ok "created .env from .env.example"

    if (( ! DRY_RUN )) && is_tty && (( ! ASSUME_YES )); then
      info "Enter the values for this server (Enter keeps the placeholder; edit $env later):"
      local value
      value=$(prompt_secret "Cloudflare API token"); [[ -n $value ]] && env_set "$env" CLOUDFLARE_API_TOKEN "$value"
      value=$(prompt_value "Cloudflare account ID" "");  [[ -n $value ]] && env_set "$env" CLOUDFLARE_ACCOUNT_ID "$value"
      value=$(prompt_value "Let's Encrypt email" "");    [[ -n $value ]] && env_set "$env" DEFAULT_SSL_EMAIL "$value"
      value=$(prompt_value "CNAME target: this server's public hostname (e.g. server.example.com)" "")
      [[ -n $value ]] && env_set "$env" CNAME_TARGET "$value"
      value=$(prompt_value "Mail hostname (e.g. mail.example.com; Enter to skip mail)" "")
      [[ -n $value ]] && env_set "$env" MAIL_HOSTNAME "$value"
      value=$(prompt_value "Web root for project directories" "$(env_get "$env" WEBROOT)")
      [[ -n $value ]] && env_set "$env" WEBROOT "$value"
    fi
  fi

  if (( ! DRY_RUN )); then
    chown root:root "$env"
    chmod 600 "$env"
    ok ".env is root-only (mode 600)"

    local token
    token=$(env_get "$env" CLOUDFLARE_API_TOKEN)
    if placeholder "$token"; then
      warn "CLOUDFLARE_API_TOKEN is not set in $env: DNS steps will be skipped until you add it"
    fi
    placeholder "$(env_get "$env" DEFAULT_SSL_EMAIL)" && warn "DEFAULT_SSL_EMAIL is not set in $env: certbot will register without an email"

    # The dashboard (a different user) can no longer read .env, so its own settings must live
    # in the service environment file instead.
    local key
    for key in PARKER_ALLOWED_ORIGINS PARKER_SCRIPT_PATH PARKER_VENV_PYTHON; do
      if [[ -n $(env_get "$env" "$key") ]]; then
        warn "$key is set in $env but the dashboard can no longer read that file: move it to $CONF_DIR/parker-ui.env (PARKER_SCRIPT_PATH/PARKER_VENV_PYTHON must also match the sudo rule)"
      fi
    done
  else
    info "dry run: .env mode/ownership would be set to 600 root:root"
  fi

  warn_about_settings "$env"
  return 0
}

setup_dashboard_conf() {
  step "Dashboard settings ($CONF_DIR)"

  run mkdir -p "$CONF_DIR"
  run chmod 755 "$CONF_DIR"

  local envfile=$CONF_DIR/parker-ui.env
  if [[ ! -f $envfile ]]; then
    write_file "$envfile" 644 root:root <<EOF
# Settings for the Parker dashboard service. Contains no secrets (those live in $APP_DIR/.env,
# which only root can read). Edit, then: systemctl restart $SERVICE_NAME
#
# Extra origins allowed to open the terminal WebSocket (comma separated). Only needed if your
# tunnel/proxy rewrites the Host header so the page origin no longer matches it.
# PARKER_ALLOWED_ORIGINS=https://parker.example.com
EOF
  else
    ok "unchanged: $envfile"
  fi

  if [[ -n $PUBLIC_URL ]]; then
    if (( DRY_RUN )); then
      info "[dry-run] set PARKER_ALLOWED_ORIGINS=$PUBLIC_URL in $envfile"
    else
      env_set "$envfile" PARKER_ALLOWED_ORIGINS "$PUBLIC_URL"
      ok "PARKER_ALLOWED_ORIGINS=$PUBLIC_URL"
    fi
  fi

  # What --check / --uninstall need to know without being told again.
  write_file "$CONF_DIR/install.conf" 644 root:root <<EOF
PARKER_USER=$SERVICE_USER
PARKER_APP_DIR=$APP_DIR
PARKER_PORT=$PORT
PARKER_SERVICE=$SERVICE_NAME
EOF
}

setup_php_snippet() {
  step "Nginx"

  if ! have nginx; then
    warn "nginx is not installed (use --install-packages)"
    return 0
  fi
  for dir in sites-available sites-enabled; do
    [[ -d $NGINX_DIR/$dir ]] || warn "$NGINX_DIR/$dir is missing: Parker expects the Debian layout"
  done
  grep -qs 'sites-enabled' "$NGINX_DIR/nginx.conf" || warn "$NGINX_DIR/nginx.conf does not include sites-enabled"

  local snippet
  snippet=$(env_get "$APP_DIR/.env" PHP_FPM_SNIPPET)
  snippet=${snippet:-snippets/php8.5.conf}
  [[ $snippet == /* ]] || snippet=$NGINX_DIR/$snippet

  if [[ -f $snippet ]]; then
    ok "PHP-FPM snippet present: $snippet"
    return 0
  fi

  local socket
  # shellcheck disable=SC2012
  socket=$(ls /run/php/php*-fpm.sock 2>/dev/null | sort -V | tail -n 1 || true)
  if [[ -z $socket ]]; then
    warn "no PHP-FPM socket found and $snippet is missing: PHP/WordPress projects will be refused until you create it"
    return 0
  fi

  [[ -f $NGINX_DIR/snippets/fastcgi-php.conf ]] || warn "$NGINX_DIR/snippets/fastcgi-php.conf is missing (nginx-common)"
  write_file "$snippet" 644 root:root <<EOF
# Created by Parker's install.sh for PHP / WordPress projects.
location ~ \.php\$ {
    include snippets/fastcgi-php.conf;
    fastcgi_pass unix:$socket;
}
EOF
  info "uses $socket; adjust if you run a different PHP version"
}

setup_mail_prerequisites() {
  (( WITH_MAIL )) || return 0
  step "Mail prerequisites (files and users only)"

  getent group vmail >/dev/null || run groupadd -g 5000 vmail
  if ! id -u vmail >/dev/null 2>&1; then
    run useradd -u 5000 -g vmail -s /usr/sbin/nologin -d /var/mail/vhosts -M vmail
  fi
  [[ $(id -u vmail 2>/dev/null || echo 5000) == 5000 ]] || warn "user 'vmail' exists with a uid other than 5000; Parker's mailbox code assumes 5000"
  run mkdir -p /var/mail/vhosts
  run chown vmail:vmail /var/mail/vhosts
  ok "vmail user and /var/mail/vhosts"

  if [[ -d /etc/dovecot ]]; then
    [[ -f /etc/dovecot/users ]] || run touch /etc/dovecot/users
    getent group dovecot >/dev/null && run chown root:dovecot /etc/dovecot/users
    run chmod 640 /etc/dovecot/users
    ok "/etc/dovecot/users"
  else
    warn "/etc/dovecot not found: Dovecot is not installed yet"
  fi

  if [[ -d /etc/postfix ]]; then
    local f
    for f in virtual_domains virtual_mailbox_maps; do
      [[ -f /etc/postfix/$f ]] || run touch "/etc/postfix/$f"
    done
    have postmap && run postmap /etc/postfix/virtual_mailbox_maps
    ok "/etc/postfix/virtual_domains and virtual_mailbox_maps"
  else
    warn "/etc/postfix not found: Postfix is not installed yet"
  fi

  run mkdir -p /etc/opendkim/keys
  local f
  for f in key.table signing.table; do
    [[ -f /etc/opendkim/$f ]] || run touch "/etc/opendkim/$f"
  done
  if id -u opendkim >/dev/null 2>&1; then
    run chown -R opendkim:opendkim /etc/opendkim
  fi
  ok "/etc/opendkim (keys, key.table, signing.table)"

  warn "Postfix main.cf/master.cf, Dovecot and OpenDKIM/OpenDMARC configuration are NOT touched: follow the README's mail sections"
}

render_sudoers() {
  local py
  py=$(venv_python)
  cat <<EOF
# Managed by Parker's install.sh (regenerated on every run). Do not edit.
#
# The dashboard user may run provisioning only through these two exact commands.
# sudo matches the whole argument string ("<script>" or "<script> --dry-run"), so any
# other flag (--remove, --yes, ...) is refused. Do not add a trailing "": with the
# script path already in the arguments it would stop the plain command from matching.
$SERVICE_USER ALL=(root) NOPASSWD: $py $APP_DIR/parker.py
$SERVICE_USER ALL=(root) NOPASSWD: $py $APP_DIR/parker.py --dry-run
EOF
}

setup_sudoers() {
  step "Sudo rule"
  have visudo || die "visudo is missing (apt-get install sudo)"

  local tmp
  tmp=$(mktemp)
  render_sudoers > "$tmp"

  # Never install a sudoers file that visudo rejects: a syntax error here can lock
  # everyone out of sudo.
  if visudo -cf "$tmp" >/dev/null 2>&1; then
    ok "sudoers syntax validated with visudo"
  elif (( DRY_RUN )) && [[ $EUID -ne 0 ]]; then
    info "visudo needs root to validate; skipped in this dry run"
  else
    rm -f "$tmp"
    die "generated sudoers rule failed validation (visudo -c); nothing was installed"
  fi

  write_file "$SUDOERS_DIR/parker" 440 root:root < "$tmp"
  rm -f "$tmp"
}

setup_systemd() {
  step "systemd service ($SERVICE_NAME)"

  # No sandboxing options on purpose: the unit's restrictions are inherited by the
  # `sudo parker.py` child. NoNewPrivileges would break sudo, ProtectSystem/ProtectHome
  # would make /etc and the web root read-only for the provisioning run.
  write_file "$SYSTEMD_DIR/$SERVICE_NAME.service" 644 root:root <<EOF
[Unit]
Description=Parker Dashboard - Domain Parking Web UI
After=network.target nginx.service

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$APP_DIR/parker-ui
EnvironmentFile=-$CONF_DIR/parker-ui.env
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/venv/bin/uvicorn main:app --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=5
# Stopping the service signals a provisioning run too; it rolls back on SIGTERM and
# needs a moment to finish doing so.
TimeoutStopSec=60
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

  if have systemd-analyze && (( ! DRY_RUN )) && [[ -f $SYSTEMD_DIR/$SERVICE_NAME.service ]]; then
    # Only the unit's own syntax is checked here (the referenced binaries are verified elsewhere).
    if systemd-analyze verify "$SYSTEMD_DIR/$SERVICE_NAME.service" 2>&1 | grep -iE 'unknown (section|key|lvalue)|failed to parse|invalid' >/dev/null; then
      bad "systemd-analyze reports problems in $SERVICE_NAME.service"
    fi
  fi

  if [[ -d $LOGROTATE_DIR ]]; then
    local log
    log=$(env_get "$APP_DIR/.env" PARKER_LOG_FILE)
    log=${log:-/var/log/parker.log}
    write_file "$LOGROTATE_DIR/parker" 644 root:root <<EOF
$log {
    weekly
    rotate 12
    compress
    delaycompress
    missingok
    notifempty
    create 0600 root root
}
EOF
  else
    info "logrotate not installed: audit log rotation skipped"
  fi
}

setup_cloudflared() {
  (( SETUP_CLOUDFLARED )) || return 0
  step "Cloudflare Tunnel connector"

  if systemd_available && systemctl cat cloudflared >/dev/null 2>&1; then
    ok "a cloudflared service is already installed: left untouched"
    info "point its public hostname at http://127.0.0.1:$PORT"
    return 0
  fi

  if [[ -z $CLOUDFLARED_TOKEN ]]; then
    info "Create a tunnel in Cloudflare Zero Trust (Networks > Tunnels), add a public hostname"
    info "with service http://127.0.0.1:$PORT, and copy the connector token."
    CLOUDFLARED_TOKEN=$(prompt_secret "Tunnel token")
  fi
  if [[ -z $CLOUDFLARED_TOKEN ]] && (( ! DRY_RUN )); then
    warn "no tunnel token given: cloudflared was not set up (re-run with CLOUDFLARED_TOKEN=... --cloudflared)"
    return 0
  fi

  if ! have cloudflared; then
    have curl || die "curl is required to add the Cloudflare package repository"
    have apt-get || die "cloudflared installation needs apt-get; install it manually"
    local keyring=/usr/share/keyrings/cloudflare-main.gpg list=/etc/apt/sources.list.d/cloudflared.list suite
    run mkdir -p --mode=0755 /usr/share/keyrings
    if (( DRY_RUN )); then
      info "[dry-run] fetch https://pkg.cloudflare.com/cloudflare-main.gpg, add apt source, install cloudflared"
    else
      curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o "$keyring" \
        || die "could not download the Cloudflare package key"
      # Try the current suite name first, then the per-release one; keep apt healthy
      # (remove our entry) if neither provides the package.
      for suite in any "$(os_release_field VERSION_CODENAME)"; do
        [[ -n $suite ]] || continue
        printf 'deb [signed-by=%s] https://pkg.cloudflare.com/cloudflared %s main\n' "$keyring" "$suite" > "$list"
        apt-get update -qq >/dev/null 2>&1 || true
        if apt-cache policy cloudflared 2>/dev/null | grep -q 'Candidate: [0-9]'; then
          break
        fi
        rm -f "$list"
      done
      if [[ ! -f $list ]]; then
        apt-get update -qq >/dev/null 2>&1 || true
        warn "the cloudflared package repository did not work; install cloudflared manually, then run: cloudflared service install <token>"
        return 0
      fi
      DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cloudflared
    fi
  fi

  if (( DRY_RUN )); then
    info "[dry-run] cloudflared service install <token>"
  else
    cloudflared service install "$CLOUDFLARED_TOKEN"
    ok "cloudflared connector installed and started"
  fi
  warn "REQUIRED: add a Zero Trust Access policy for the dashboard hostname. The dashboard has no login of its own."
}

start_service() {
  step "Starting $SERVICE_NAME"

  run systemctl daemon-reload
  run systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true

  if (( NO_START )); then
    info "--no-start: not starting the service"
    return 0
  fi

  # restart (not start): an upgrade must pick up new code and a new unit.
  run systemctl restart "$SERVICE_NAME"
  (( DRY_RUN )) && return 0

  for _ in $(seq 1 30); do
    if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/" 2>/dev/null; then
      ok "dashboard answers on http://127.0.0.1:$PORT/"
      return 0
    fi
    sleep 1
  done
  journalctl -u "$SERVICE_NAME" -n 15 --no-pager 2>/dev/null | sed 's/^/        /' >&2 || true
  die "the dashboard did not answer on port $PORT within 30s (see the log above, or: journalctl -u $SERVICE_NAME)"
}

# --------------------------------------------------------------------------- verification

verify_sudo_rule() {
  local py
  py=$(venv_python)
  if ! service_user_exists; then
    bad "service user '$SERVICE_USER' does not exist"
    return
  fi

  if as_service_user sudo -n -l "$py" "$APP_DIR/parker.py" >/dev/null 2>&1; then
    ok "'$SERVICE_USER' may run parker.py (no arguments) as root"
  else
    bad "'$SERVICE_USER' may NOT run parker.py through sudo"
  fi

  if as_service_user sudo -n -l "$py" "$APP_DIR/parker.py" --dry-run >/dev/null 2>&1; then
    ok "'$SERVICE_USER' may run parker.py --dry-run as root"
  else
    bad "'$SERVICE_USER' may NOT run parker.py --dry-run through sudo"
  fi

  local extra
  # Each of these must be REFUSED: sudo matches the full argument string.
  for extra in "--list" "--remove example.com --yes" "--yes --domain x.example.com --type node --port 3000 --no-dns"; do
    # shellcheck disable=SC2086
    if as_service_user sudo -n -l "$py" "$APP_DIR/parker.py" $extra >/dev/null 2>&1; then
      bad "'$SERVICE_USER' can run parker.py $extra as root: the sudoers rule is too broad"
    fi
  done
  ok "no other parker.py arguments are permitted for '$SERVICE_USER'"

  if as_service_user sudo -n -l /bin/true >/dev/null 2>&1; then
    bad "'$SERVICE_USER' can run other commands through sudo"
  else
    ok "'$SERVICE_USER' has no other sudo rights"
  fi
}

verify_not_writable() {
  service_user_exists || return 0
  local writable
  writable=$(as_service_user find "$APP_DIR" -xdev -writable -print 2>/dev/null | head -n 3 || true)
  if [[ -n $writable ]]; then
    bad "'$SERVICE_USER' can modify the install (e.g. ${writable%%$'\n'*}): sudo would let it become root"
  else
    ok "'$SERVICE_USER' cannot modify $APP_DIR"
  fi
}

check_mode() {
  step "Parker dashboard check"

  [[ $EUID -eq 0 ]] || die "run as root: sudo ./install.sh --check (it tests the service user's sudo rights)"

  local conf=$CONF_DIR/install.conf
  if [[ -f $conf ]]; then
    local value
    value=$(sed -n 's/^PARKER_USER=//p' "$conf");    [[ -n $value ]] && SERVICE_USER=$value
    value=$(sed -n 's/^PARKER_PORT=//p' "$conf");    [[ -n $value ]] && PORT=$value
    value=$(sed -n 's/^PARKER_SERVICE=//p' "$conf"); [[ -n $value ]] && SERVICE_NAME=$value
    ok "install record: user=$SERVICE_USER port=$PORT service=$SERVICE_NAME"
  else
    warn "no install record at $conf: checking with defaults (user=$SERVICE_USER port=$PORT)"
  fi

  if systemd_available; then
    verdict "service is enabled" "service is not enabled" bad systemctl is-enabled --quiet "$SERVICE_NAME"
    verdict "service is running" "service is not running" bad systemctl is-active --quiet "$SERVICE_NAME"
  else
    bad "systemd is not available"
  fi

  if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/" 2>/dev/null; then
    ok "dashboard answers on 127.0.0.1:$PORT"
  else
    bad "dashboard does not answer on 127.0.0.1:$PORT"
  fi
  if have ss && ss -ltnH "sport = :$PORT" 2>/dev/null | grep -qvE '127\.0\.0\.1:|\[::1\]:'; then
    bad "port $PORT is listening on a non-loopback address: the dashboard must never be exposed directly"
  fi

  verdict "sudoers rule installed" "sudoers rule missing: $SUDOERS_DIR/parker" bad test -f "$SUDOERS_DIR/parker"
  verify_sudo_rule
  verify_not_writable

  local env=$APP_DIR/.env
  require_readable_env "$env"
  if [[ -f $env ]]; then
    verdict ".env is root-only" ".env must be mode 600 and owned by root (found $(stat -c '%a %U' "$env"))" bad \
      test "$(stat -c '%a %U' "$env")" = "600 root"
    placeholder "$(env_get "$env" CLOUDFLARE_API_TOKEN)" && warn "CLOUDFLARE_API_TOKEN is not set"
    placeholder "$(env_get "$env" DEFAULT_SSL_EMAIL)" && warn "DEFAULT_SSL_EMAIL is not set"
    warn_about_settings "$env"
  else
    bad ".env is missing"
  fi

  step "Prerequisites for provisioning"
  local tool
  for tool in nginx certbot; do
    verdict "$tool" "$tool is not installed" warn have "$tool"
  done
  verdict "nginx sites-available/sites-enabled" "nginx sites-available/sites-enabled layout missing" warn \
    test -d "$NGINX_DIR/sites-available" -a -d "$NGINX_DIR/sites-enabled"
  local snippet
  snippet=$(env_get "$env" PHP_FPM_SNIPPET); snippet=${snippet:-snippets/php8.5.conf}
  [[ $snippet == /* ]] || snippet=$NGINX_DIR/$snippet
  verdict "PHP-FPM snippet $snippet" "PHP-FPM snippet missing ($snippet): PHP/WordPress projects will be refused" warn test -f "$snippet"
  for tool in opendkim-genkey doveadm postmap; do
    verdict "$tool" "$tool not installed (only needed for mail setup)" info have "$tool"
  done
}

# --------------------------------------------------------------------------- uninstall

uninstall_mode() {
  step "Uninstalling the Parker dashboard"

  if (( ! DRY_RUN )) && [[ $EUID -ne 0 ]]; then
    die "run as root: sudo ./install.sh --uninstall"
  fi

  local conf=$CONF_DIR/install.conf value
  if [[ -f $conf ]]; then
    value=$(sed -n 's/^PARKER_USER=//p' "$conf");    [[ -n $value ]] && SERVICE_USER=$value
    value=$(sed -n 's/^PARKER_SERVICE=//p' "$conf"); [[ -n $value ]] && SERVICE_NAME=$value
  fi

  info "will remove: service $SERVICE_NAME, $SUDOERS_DIR/parker, $LOGROTATE_DIR/parker, $CONF_DIR (except $CONF_DIR/backups)"
  (( PURGE )) && info "and (--purge): $APP_DIR/venv and the user '$SERVICE_USER'"
  info "will NOT touch: .env, project files, nginx sites, certificates, mail data, cloudflared"
  # --yes means "do it"; without it (or without a terminal) the answer is no.
  (( ASSUME_YES )) || confirm "Proceed?" n || die "aborted"

  if systemd_available; then
    run systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
  fi
  run rm -f "$SYSTEMD_DIR/$SERVICE_NAME.service" "$SUDOERS_DIR/parker" "$LOGROTATE_DIR/parker"
  # Keep backups of files this installer replaced; they are the only copy of your old settings.
  if [[ -d $CONF_DIR ]]; then
    run find "$CONF_DIR" -mindepth 1 -maxdepth 1 ! -name backups -exec rm -rf {} +
    [[ -d $CONF_DIR/backups ]] || run rmdir "$CONF_DIR" 2>/dev/null || true
  fi
  systemd_available && run systemctl daemon-reload
  ok "service, sudoers rule, log rotation and settings removed"

  if (( PURGE )); then
    run rm -rf "$APP_DIR/venv"
    if service_user_exists; then
      run userdel "$SERVICE_USER"
      getent group "$SERVICE_USER" >/dev/null && run groupdel "$SERVICE_USER" 2>/dev/null || true
    fi
    ok "venv and user removed"
  else
    info "kept the venv and user (use --purge to remove them)"
  fi

  if have cloudflared; then
    info "cloudflared was left installed; remove it with: cloudflared service uninstall"
  fi
}

# --------------------------------------------------------------------------- summary

print_summary() {
  printf '\n%s========================================%s\n' "$C_BOLD" "$C_OFF"
  if (( DRY_RUN )); then
    printf ' Dry run complete: nothing was changed\n'
  elif (( ${#WARNINGS[@]} )); then
    printf ' Parker dashboard installed (with warnings)\n'
  else
    printf ' Parker dashboard installed\n'
  fi
  printf '%s========================================%s\n\n' "$C_BOLD" "$C_OFF"

  cat <<EOF
Service     : $SERVICE_NAME   (systemctl status $SERVICE_NAME)
Dashboard   : http://127.0.0.1:$PORT   (loopback only)
Runs as     : $SERVICE_USER (sudo: parker.py and parker.py --dry-run only)
Install dir : $APP_DIR
Config      : $APP_DIR/.env (root only), $CONF_DIR/parker-ui.env
Logs        : journalctl -u $SERVICE_NAME      audit: $(env_get "$APP_DIR/.env" PARKER_LOG_FILE | sed 's/^$/\/var\/log\/parker.log/')

Next steps
  - The dashboard has NO login of its own. Reach it through a Cloudflare Tunnel with a
    Zero Trust Access policy (--cloudflared), or an SSH tunnel for a quick look:
        ssh -L $PORT:127.0.0.1:$PORT user@this-server   then open http://127.0.0.1:$PORT
  - Verify at any time:   sudo $APP_DIR/install.sh --check
  - Upgrade:              git pull && sudo $APP_DIR/install.sh
EOF

  if (( ${#WARNINGS[@]} )); then
    printf '\nWarnings\n'
    local w
    for w in "${WARNINGS[@]}"; do
      printf '  - %s\n' "$w"
    done
  fi
}

# --------------------------------------------------------------------------- main

main() {
  parse_args "$@"
  validate_args

  case $MODE in
    check)
      check_mode
      printf '\n'
      if (( FAILURES )); then
        printf '%s%d check(s) failed%s\n' "$C_RED" "$FAILURES" "$C_OFF"
        exit 1
      fi
      printf '%sAll checks passed%s' "$C_GREEN" "$C_OFF"
      (( ${#WARNINGS[@]} )) && printf ' (%d warning(s))' "${#WARNINGS[@]}"
      printf '\n'
      exit 0
      ;;
    uninstall)
      uninstall_mode
      exit 0
      ;;
  esac

  preflight
  install_packages
  ensure_service_user
  reclaim_service_owned_files
  setup_venv
  secure_install_dir
  setup_env_file
  setup_dashboard_conf
  setup_php_snippet
  setup_mail_prerequisites
  setup_sudoers
  setup_systemd
  setup_cloudflared
  start_service

  if (( ! DRY_RUN && ! NO_START )); then
    step "Verification"
    verify_sudo_rule
    verify_not_writable
    (( FAILURES == 0 )) || die "verification failed (see above). The service was started but is not safe to use until this is fixed."
  fi

  print_summary
}

# Run only when executed, not when sourced (the tests source this file to call functions).
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
