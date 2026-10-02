"""
Tests for install.sh.

Most run unprivileged against `--dry-run` (nothing is changed; the script prints what it
would do) or by sourcing the script to call individual functions. The sudo-rule semantics
need real sudo and root, so those tests only run when the suite is executed as root.
"""
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

import parker

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "install.sh"
IS_ROOT = os.geteuid() == 0

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")


@pytest.fixture
def sandbox(tmp_path):
    """Every location install.sh writes to, redirected into a temp dir."""
    dirs = {name: tmp_path / name for name in ("systemd", "sudoers", "conf", "logrotate", "nginx", "runsystemd")}
    for d in dirs.values():
        d.mkdir()
    env = dict(
        os.environ,
        PARKER_SYSTEMD_DIR=str(dirs["systemd"]),
        PARKER_SUDOERS_DIR=str(dirs["sudoers"]),
        PARKER_CONF_DIR=str(dirs["conf"]),
        PARKER_LOGROTATE_DIR=str(dirs["logrotate"]),
        PARKER_NGINX_DIR=str(dirs["nginx"]),
        PARKER_SYSTEMD_RUNTIME_DIR=str(dirs["runsystemd"]),
    )
    env.pop("CLOUDFLARED_TOKEN", None)
    return type("Sandbox", (), {"dirs": dirs, "env": env, "tmp": tmp_path})


def run_install(sandbox, *args, cwd=None, script=INSTALL):
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True, text=True, env=sandbox.env, cwd=cwd, timeout=120,
    )


def call(sandbox, snippet):
    """Source install.sh and run a snippet that uses its functions."""
    return subprocess.run(
        ["bash", "-c", f"set -Eeuo pipefail; source {INSTALL}; {snippet}"],
        capture_output=True, text=True, env=sandbox.env, timeout=60,
    )


# ----------------------------------------------------------------------- hygiene

@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_shellcheck_is_clean():
    r = subprocess.run(["shellcheck", "-S", "style", str(INSTALL)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_script_parses_and_is_executable():
    assert subprocess.run(["bash", "-n", str(INSTALL)]).returncode == 0
    assert os.access(INSTALL, os.X_OK)


def test_help_lists_every_mode_and_option(sandbox):
    r = run_install(sandbox, "--help")
    assert r.returncode == 0
    for flag in ("--check", "--uninstall", "--purge", "--user", "--port", "--install-packages",
                 "--with-mail", "--env-file", "--public-url", "--cloudflared", "--no-start",
                 "--yes", "--dry-run"):
        assert flag in r.stdout


# ------------------------------------------------------------------ argument checks

@pytest.mark.parametrize("args,message", [
    (["--port", "80"], "--port must be between"),
    (["--port", "abc"], "--port must be between"),
    (["--port", "70000"], "--port must be between"),
    (["--user", "root"], "refusing to run the dashboard as root"),
    (["--user", "Bad User"], "invalid user name"),
    (["--user", "-x"], "invalid user name"),
    (["--public-url", "parker.example.com"], "--public-url must look like"),
    (["--public-url", "https://parker.example.com/path"], "--public-url must look like"),
    (["--env-file", "/nonexistent/.env"], "--env-file not found"),
    (["--bogus"], "unknown option"),
    (["--port"], "needs a value"),
])
def test_bad_arguments_are_rejected_before_anything_happens(sandbox, args, message):
    r = run_install(sandbox, "--dry-run", *args)
    assert r.returncode != 0
    assert message in r.stdout + r.stderr


def test_equals_syntax_is_accepted(sandbox):
    r = run_install(sandbox, "--dry-run", "--yes", "--port=9211", "--user=parkertest")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--port 9211" in r.stdout


def test_install_path_with_unsafe_characters_is_refused(sandbox, tmp_path):
    """The path is written into sudoers and a systemd unit; spaces would need escaping in both."""
    odd = tmp_path / "my parker"
    odd.mkdir()
    shutil.copy(INSTALL, odd / "install.sh")
    r = run_install(sandbox, "--dry-run", script=odd / "install.sh")
    assert r.returncode != 0
    assert "unsafe in sudoers/systemd" in r.stdout + r.stderr


def test_incomplete_checkout_is_refused(sandbox, tmp_path):
    shutil.copy(INSTALL, tmp_path / "install.sh")
    r = run_install(sandbox, "--dry-run", script=tmp_path / "install.sh")
    assert r.returncode != 0
    assert "is missing: run install.sh from a complete checkout" in r.stdout + r.stderr


# ------------------------------------------------------------------------ dry run

def snapshot(*roots):
    return sorted(str(p) for root in roots for p in Path(root).rglob("*"))


def test_dry_run_changes_nothing_and_shows_the_plan(sandbox):
    before = snapshot(REPO, *sandbox.dirs.values())
    r = run_install(sandbox, "--dry-run", "--yes", "--port", "9123",
                    "--public-url", "https://parker.example.com", "--with-mail")
    out = r.stdout
    assert r.returncode == 0, out + r.stderr

    assert snapshot(REPO, *sandbox.dirs.values()) == before        # no venv, no .env, no files anywhere
    assert "Dry run complete: nothing was changed" in out

    # The plan: user, venv, .env, sudoers, unit, mail prerequisites.
    assert "[dry-run] python3 -m venv" in out
    assert "[dry-run] install -m 600 -o root -g root" in out
    assert "[dry-run] write " in out and "parker-ui.service" in out
    assert "vmail" in out and "/var/mail/vhosts" in out
    assert "PARKER_ALLOWED_ORIGINS=https://parker.example.com" in out


def test_dry_run_unit_file_content(sandbox):
    out = run_install(sandbox, "--dry-run", "--yes", "--port", "9123", "--user", "parkertest").stdout

    assert f"WorkingDirectory={REPO}/parker-ui" in out
    assert f"ExecStart={REPO}/venv/bin/uvicorn main:app --host 127.0.0.1 --port 9123" in out
    assert "User=parkertest" in out and "Group=parkertest" in out
    assert "EnvironmentFile=-" in out and "/parker-ui.env" in out
    assert "TimeoutStopSec=60" in out
    assert "WantedBy=multi-user.target" in out
    # Sandboxing options are inherited by the `sudo parker.py` child and would break it.
    # (dry-run prints file content prefixed with "| ")
    unit_lines = [re.sub(r"^\s*\|\s?", "", l) for l in out.splitlines() if "|" in l]
    assert "[Service]" in unit_lines
    for forbidden in ("NoNewPrivileges", "ProtectSystem", "ProtectHome", "PrivateTmp", "ReadOnlyPaths"):
        assert not any(l.startswith(forbidden + "=") for l in unit_lines), forbidden
    # Never exposed beyond loopback.
    assert "0.0.0.0" not in out and "--host 127.0.0.1" in out


def test_dry_run_with_existing_php_fpm_socket_snippet_is_not_overwritten(sandbox):
    snippet = sandbox.dirs["nginx"] / "snippets" / "php8.5.conf"
    snippet.parent.mkdir()
    snippet.write_text("# mine\n")
    out = run_install(sandbox, "--dry-run", "--yes").stdout
    assert "PHP-FPM snippet present" in out or "nginx is not installed" in out
    assert snippet.read_text() == "# mine\n"


# ----------------------------------------------------------------------- sudoers

def test_sudoers_rule_is_exactly_the_two_commands(sandbox):
    r = call(sandbox, 'SERVICE_USER=parker; APP_DIR=/opt/parker; render_sudoers')
    rules = [l for l in r.stdout.splitlines() if l and not l.startswith("#")]
    assert rules == [
        "parker ALL=(root) NOPASSWD: /opt/parker/venv/bin/python3 /opt/parker/parker.py",
        "parker ALL=(root) NOPASSWD: /opt/parker/venv/bin/python3 /opt/parker/parker.py --dry-run",
    ]
    # Regression: a trailing "" made the plain command stop matching (the dashboard could not start runs).
    assert '""' not in "\n".join(rules)


@pytest.mark.skipif(shutil.which("visudo") is None, reason="visudo is not installed")
def test_rendered_sudoers_passes_visudo(sandbox, tmp_path):
    rule = call(sandbox, 'SERVICE_USER=parker; APP_DIR=/opt/parker; render_sudoers').stdout
    f = tmp_path / "parker-sudoers"
    f.write_text(rule)
    r = subprocess.run(["visudo", "-cf", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(not IS_ROOT or shutil.which("sudo") is None or shutil.which("useradd") is None,
                    reason="needs root, sudo and useradd (changes the machine, so root-only)")
def test_sudo_semantics_with_the_real_sudo(sandbox, tmp_path):
    """The plain command and --dry-run run; every other flag combination is refused."""
    user = "parkerst" + os.urandom(2).hex()
    app = Path("/opt") / f"parker-st-{user}"
    app.mkdir()
    (app / "venv" / "bin").mkdir(parents=True)
    py = app / "venv" / "bin" / "python3"
    py.symlink_to(shutil.which("python3"))
    (app / "parker.py").write_text("import sys; print('ran', sys.argv[1:])\n")
    sudoers_file = Path("/etc/sudoers.d") / f"zz-{user}"
    subprocess.run(["useradd", "--system", "--shell", "/usr/sbin/nologin", user], check=True)
    try:
        rule = call(sandbox, f'SERVICE_USER={user}; APP_DIR={app}; render_sudoers').stdout
        sudoers_file.write_text(rule)
        sudoers_file.chmod(0o440)

        def attempt(*args):
            return subprocess.run(
                ["runuser", "-u", user, "--", "sudo", "-n", str(py), str(app / "parker.py"), *args],
                capture_output=True, text=True,
            )

        assert attempt().stdout.strip() == "ran []"
        assert attempt("--dry-run").stdout.strip() == "ran ['--dry-run']"
        for forbidden in (["--list"], ["--remove", "example.com", "--yes"],
                          ["--yes", "--domain", "x.example.com"], ["--dry-run", "--yes"]):
            assert attempt(*forbidden).returncode != 0, forbidden
    finally:
        sudoers_file.unlink(missing_ok=True)
        subprocess.run(["userdel", user], check=False)
        shutil.rmtree(app, ignore_errors=True)


# --------------------------------------------------------------------- .env tools

def test_env_set_and_get_roundtrip(sandbox, tmp_path):
    env = tmp_path / ".env"
    env.write_text("# header\nA=1   # keep going\nWEBROOT=/var/www\n# B=commented\n")

    script = textwrap.dedent(f"""
        env_set {env} A 2
        env_set {env} NEW "has # hash"
        env_set {env} SPACED "  padded  "
        env_set {env} B 3
        env_get {env} A; env_get {env} NEW; env_get {env} SPACED; env_get {env} B; env_get {env} WEBROOT
    """)
    r = call(sandbox, script)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["2", "has # hash", "  padded  ", "3", "/var/www"]

    text = env.read_text()
    assert text.startswith("# header\n")                 # comments survive
    assert "# B=commented" in text and "B=3" in text     # a commented key is not mistaken for the real one
    assert text.count("A=") == 1                         # replaced, not duplicated


def test_installer_env_parsing_matches_parkers(sandbox, tmp_path):
    """install.sh and parker.py must read .env identically (they are separate implementations)."""
    lines = [
        "KEY=value", "KEY = value  ", "export KEY=value", "KEY=  # c", 'KEY="quoted # x"', "KEY='single'",
        "MX_HOSTNAME=mail.example.com    # MX record target", "PASS=abc#def", "KEY=a=b=c",
    ]
    for i, line in enumerate(lines):
        f = tmp_path / f"env{i}"
        f.write_text(line + "\n")
        key = parker.parse_env_line(line)[0]
        expected = parker.parse_env_line(line)[1]
        got = call(sandbox, f"env_get {f} {key}").stdout.rstrip("\n")
        assert got == expected, line


def test_placeholder_detection(sandbox):
    script = 'for v in "" your_token ssl@yourdomain.com real_value; do placeholder "$v" && echo "P:$v" || echo "R:$v"; done'
    assert call(sandbox, script).stdout.splitlines() == ["P:", "P:your_token", "P:ssl@yourdomain.com", "R:real_value"]


# ------------------------------------------------------------------- permissions

@pytest.mark.skipif(not IS_ROOT, reason="needs root to create users and chown")
def test_installer_detects_and_fixes_service_user_ownership(sandbox, tmp_path):
    """The old README did `chown -R parker:parker`: code run as root would be editable by the service user."""
    user = "parkerow" + os.urandom(2).hex()
    app = Path("/opt") / f"parker-ow-{user}"
    app.mkdir()
    (app / "parker.py").write_text("print(1)\n")
    (app / "sub").mkdir()
    (app / "sub" / "x.py").write_text("x\n")
    subprocess.run(["useradd", "--system", "--user-group", "--no-create-home", "--shell", "/usr/sbin/nologin", user], check=True)
    try:
        os.chmod(app, 0o755)
        subprocess.run(["chown", "-R", f"{user}:{user}", str(app)], check=True)

        r = call(sandbox, f"SERVICE_USER={user}; APP_DIR={app}; secure_install_dir")
        out = r.stdout + r.stderr
        assert "handing them to root" in out, out

        for p in (app, app / "parker.py", app / "sub", app / "sub" / "x.py"):
            assert p.stat().st_uid == 0, p
        assert subprocess.run(["runuser", "-u", user, "--", "test", "-w", str(app / "parker.py")]).returncode != 0
    finally:
        subprocess.run(["userdel", user], check=False)
        shutil.rmtree(app, ignore_errors=True)


# --------------------------------------------- a bad sudoers file must never be installed

def fake_visudo(tmp_path, exit_code):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "visudo"
    script.write_text(f"#!/bin/sh\nexit {exit_code}\n")
    script.chmod(0o755)
    return bin_dir


def test_sudoers_rejected_by_visudo_is_never_installed(sandbox, tmp_path):
    """A syntax error in /etc/sudoers.d can lock everyone out of sudo."""
    sandbox.env["PATH"] = f"{fake_visudo(tmp_path, 1)}:{os.environ['PATH']}"
    r = call(sandbox, "SERVICE_USER=parker; APP_DIR=/opt/parker; DRY_RUN=0; setup_sudoers")

    assert r.returncode != 0
    assert "failed validation" in r.stdout + r.stderr
    assert list(sandbox.dirs["sudoers"].iterdir()) == []


@pytest.mark.skipif(not IS_ROOT, reason="chown to root needs root")
def test_validated_sudoers_is_installed_read_only(sandbox, tmp_path):
    sandbox.env["PATH"] = f"{fake_visudo(tmp_path, 0)}:{os.environ['PATH']}"
    r = call(sandbox, "SERVICE_USER=parker; APP_DIR=/opt/parker; DRY_RUN=0; setup_sudoers")
    assert r.returncode == 0, r.stdout + r.stderr

    installed = sandbox.dirs["sudoers"] / "parker"
    assert oct(installed.stat().st_mode & 0o777) == "0o440"
    assert installed.stat().st_uid == 0
    assert "NOPASSWD: /opt/parker/venv/bin/python3 /opt/parker/parker.py" in installed.read_text()


@pytest.mark.skipif(not IS_ROOT, reason="chown to root needs root")
def test_env_file_is_created_root_only_from_the_example(sandbox, tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    shutil.copy(REPO / ".env.example", app / ".env.example")

    r = call(sandbox, f"APP_DIR={app}; ASSUME_YES=1; DRY_RUN=0; setup_env_file")
    assert r.returncode == 0, r.stdout + r.stderr

    env = app / ".env"
    assert oct(env.stat().st_mode & 0o777) == "0o600" and env.stat().st_uid == 0
    assert "CLOUDFLARE_API_TOKEN" in env.read_text()
    assert "CLOUDFLARE_API_TOKEN is not set" in r.stdout          # placeholder values are flagged


@pytest.mark.skipif(not IS_ROOT, reason="chown to root needs root")
def test_existing_env_file_is_never_overwritten_but_is_locked_down(sandbox, tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    shutil.copy(REPO / ".env.example", app / ".env.example")
    (app / ".env").write_text("CLOUDFLARE_API_TOKEN=real-secret\nDEFAULT_SSL_EMAIL=me@example.com\n")
    os.chmod(app / ".env", 0o644)                                  # e.g. left world-readable by hand

    r = call(sandbox, f"APP_DIR={app}; ASSUME_YES=1; DRY_RUN=0; setup_env_file")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (app / ".env").read_text().startswith("CLOUDFLARE_API_TOKEN=real-secret")
    assert oct((app / ".env").stat().st_mode & 0o777) == "0o600"
    assert "is not set" not in r.stdout                           # real values: no placeholder warning
