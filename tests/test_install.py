"""
Tests for install.sh.

Most run unprivileged against `--dry-run` (nothing is changed; the script prints what it
would do) or by sourcing the script to call individual functions. The sudo-rule semantics
need real sudo and root, so those tests only run when the suite is executed as root.
"""
import os
import pwd
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


def free_port():
    """A port nothing is listening on, so tests do not depend on the machine's own services."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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
    port = free_port()
    r = run_install(sandbox, "--dry-run", "--yes", f"--port={port}", "--user=parkertest")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"--port {port}" in r.stdout


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
    out = run_install(sandbox, "--dry-run", "--yes", "--port", str(free_port())).stdout
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
    (app / ".git" / "hooks").mkdir(parents=True)          # root runs git here during upgrades
    subprocess.run(["useradd", "--system", "--user-group", "--no-create-home", "--shell", "/usr/sbin/nologin", user], check=True)
    try:
        os.chmod(app, 0o755)
        subprocess.run(["chown", "-R", f"{user}:{user}", str(app)], check=True)

        r = call(sandbox, f"SERVICE_USER={user}; APP_DIR={app}; secure_install_dir")
        out = r.stdout + r.stderr
        assert "handing them to root" in out, out

        for p in (app, app / "parker.py", app / "sub", app / "sub" / "x.py", app / ".git", app / ".git" / "hooks"):
            assert p.stat().st_uid == 0, p
        assert subprocess.run(["runuser", "-u", user, "--", "test", "-w", str(app / "parker.py")]).returncode != 0
        # A service-writable .git would run its hooks as root on the next `sudo git pull`.
        assert subprocess.run(["runuser", "-u", user, "--", "test", "-w", str(app / ".git" / "hooks")]).returncode != 0
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
    (app / ".env").write_text(
        "CLOUDFLARE_API_TOKEN=real-secret\nDEFAULT_SSL_EMAIL=me@example.com\nWEBROOT=/srv/sites\n"
        "CNAME_TARGET=server.example.org\nMAIL_HOSTNAME=mail.example.org\nDKIM_SELECTOR=default\n"
    )
    os.chmod(app / ".env", 0o644)                                  # e.g. left world-readable by hand

    r = call(sandbox, f"APP_DIR={app}; ASSUME_YES=1; DRY_RUN=0; setup_env_file")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (app / ".env").read_text().startswith("CLOUDFLARE_API_TOKEN=real-secret")
    assert oct((app / ".env").stat().st_mode & 0o777) == "0o600"
    assert "is not set" not in r.stdout                           # real values: no placeholder warning


# ----------------------------------- upgrades over an existing (old) deployment

def sh_write(sandbox, tmp_path, target, content, *, dry_run=0):
    """Call install.sh's write_file for `target` as the current user."""
    me = f"{os.getuid()}:{os.getgid()}"
    return call(
        sandbox,
        f"BACKUP_STAMP=20260101-000000; DRY_RUN={dry_run}; "
        f"printf '%s' {content!r} | write_file {target} 644 {me}",
    )


def test_replaced_files_are_backed_up_outside_the_scanned_config_dirs(sandbox, tmp_path):
    """A customised unit/sudoers must never be silently destroyed; a stray .bak in
    /etc/logrotate.d would be parsed as a second config, so backups go to one directory."""
    target = sandbox.dirs["systemd"] / "parker-ui.service"
    target.write_text("[Service]\nEnvironment=MY_CUSTOM_SETTING=keep-me\n")
    conf = sandbox.dirs["conf"]

    r = sh_write(sandbox, tmp_path, target, "[Service]\nExecStart=new\n")
    assert r.returncode == 0, r.stdout + r.stderr

    backup = conf / "backups" / "parker-ui.service.20260101-000000"
    assert "MY_CUSTOM_SETTING=keep-me" in backup.read_text()
    assert "ExecStart=new" in target.read_text()
    assert "previous version is saved as" in r.stdout
    assert [p.name for p in sandbox.dirs["systemd"].iterdir()] == ["parker-ui.service"]   # no .bak next to it
    assert oct(backup.parent.stat().st_mode & 0o777) == "0o700"


def test_identical_content_is_not_rewritten_or_backed_up(sandbox, tmp_path):
    target = sandbox.dirs["systemd"] / "x.service"
    target.write_text("same")
    r = sh_write(sandbox, tmp_path, target, "same")
    assert "unchanged" in r.stdout
    assert not (sandbox.dirs["conf"] / "backups").exists()


def test_installers_own_config_files_are_not_backed_up(sandbox, tmp_path):
    target = sandbox.dirs["conf"] / "install.conf"
    target.write_text("old")
    sh_write(sandbox, tmp_path, target, "new")
    assert target.read_text().strip() == "new"
    assert not (sandbox.dirs["conf"] / "backups").exists()


def test_dry_run_reports_the_backup_but_makes_none(sandbox, tmp_path):
    target = sandbox.dirs["systemd"] / "parker-ui.service"
    target.write_text("old")
    r = sh_write(sandbox, tmp_path, target, "new", dry_run=1)
    assert "back up existing" in r.stdout
    assert target.read_text() == "old"
    assert not (sandbox.dirs["conf"] / "backups").exists()


def fake_systemctl(tmp_path):
    """So a test can never disable a real service on the machine running it."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "systemctl"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    return bin_dir


@pytest.mark.skipif(not IS_ROOT, reason="uninstall requires root")
def test_uninstall_keeps_the_backups(sandbox, tmp_path):
    conf = sandbox.dirs["conf"]
    (conf / "backups").mkdir()
    (conf / "backups" / "parker.20260101-000000").write_text("old sudoers")
    (conf / "install.conf").write_text("PARKER_USER=parker\n")
    (conf / "parker-ui.env").write_text("# settings\n")
    sandbox.env["PATH"] = f"{fake_systemctl(tmp_path)}:{os.environ['PATH']}"

    r = run_install(sandbox, "--uninstall", "--yes")
    assert r.returncode == 0, r.stdout + r.stderr

    assert not (conf / "install.conf").exists() and not (conf / "parker-ui.env").exists()
    assert (conf / "backups" / "parker.20260101-000000").read_text() == "old sudoers"


@pytest.mark.skipif(not IS_ROOT, reason="chown to root needs root")
@pytest.mark.parametrize("key", ["PARKER_VENV_PYTHON", "PARKER_SCRIPT_PATH", "PARKER_ALLOWED_ORIGINS"])
def test_dashboard_settings_stranded_in_a_root_only_env_are_flagged(sandbox, tmp_path, key):
    """The dashboard (another user) cannot read .env any more, so these would silently stop working."""
    app = tmp_path / "app"
    app.mkdir()
    shutil.copy(REPO / ".env.example", app / ".env.example")
    (app / ".env").write_text(f"CLOUDFLARE_API_TOKEN=real\nDEFAULT_SSL_EMAIL=me@example.com\n{key}=/somewhere\n")

    r = call(sandbox, f"APP_DIR={app}; ASSUME_YES=1; DRY_RUN=0; setup_env_file")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"{key} is set in" in r.stdout and "can no longer read that file" in r.stdout


@pytest.mark.skipif(not IS_ROOT, reason="needs root to create users and chown")
def test_a_service_owned_git_directory_alone_is_repaired(sandbox, tmp_path):
    """
    Typical state after an earlier install: the code is root-owned but .git still belongs to the
    service user. Root runs git there on every upgrade, so a planted hook would run as root.
    """
    user = "parkergit" + os.urandom(2).hex()
    app = Path("/opt") / f"parker-git-{user}"
    (app / ".git" / "hooks").mkdir(parents=True)
    (app / "parker.py").write_text("print(1)\n")
    subprocess.run(["useradd", "--system", "--user-group", "--no-create-home", "--shell", "/usr/sbin/nologin", user], check=True)
    try:
        os.chmod(app, 0o755)
        subprocess.run(["chown", "-R", f"{user}:{user}", str(app / ".git")], check=True)   # ONLY .git

        assert subprocess.run(["runuser", "-u", user, "--", "test", "-w", str(app / ".git" / "hooks")]).returncode == 0

        r = call(sandbox, f"SERVICE_USER={user}; APP_DIR={app}; secure_install_dir")
        assert "handing them to root" in r.stdout + r.stderr, r.stdout + r.stderr
        assert (app / ".git" / "hooks").stat().st_uid == 0
        assert subprocess.run(["runuser", "-u", user, "--", "test", "-w", str(app / ".git" / "hooks")]).returncode != 0
    finally:
        subprocess.run(["userdel", user], check=False)
        shutil.rmtree(app, ignore_errors=True)


# ------------------------------------- a service-owned venv must never be run as root

def service_user_for_tests(tmp_path):
    """
    A user that owns the files we create. Unprivileged: the current user (so `find -user`
    matches). Root: 'nobody', with the files chowned to it.
    """
    me = pwd.getpwuid(os.getuid()).pw_name
    return ("nobody", True) if me == "root" else (me, False)


def make_planted_venv(app, marker, owner_is_nobody):
    """A venv whose python3 records that it was executed, to prove it never is."""
    bin_dir = app / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    py = bin_dir / "python3"
    py.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
    py.chmod(0o755)
    if owner_is_nobody:
        subprocess.run(["chown", "-R", "nobody", str(app / "venv")], check=True)


def test_reclaim_flags_a_service_owned_venv_as_untrusted(sandbox, tmp_path):
    user, as_root = service_user_for_tests(tmp_path)
    app = tmp_path / "app"
    make_planted_venv(app, tmp_path / "executed", as_root)

    r = call(sandbox, f"SERVICE_USER={user}; APP_DIR={app}; DRY_RUN=1; reclaim_service_owned_files; echo UNTRUSTED=$VENV_UNTRUSTED")
    assert "UNTRUSTED=1" in r.stdout, r.stdout + r.stderr
    assert "handing them to root" in r.stdout


def a_user_other_than_the_current_one():
    """A real account that does not own the files the test creates (whoever runs the tests)."""
    me = pwd.getpwuid(os.getuid()).pw_name
    for name in ("nobody", "daemon", "bin", "sys"):
        try:
            pwd.getpwnam(name)
        except KeyError:
            continue
        if name != me:
            return name
    pytest.skip("no other system account available")


def test_a_venv_not_owned_by_the_service_user_stays_trusted(sandbox, tmp_path):
    app = tmp_path / "app"
    make_planted_venv(app, tmp_path / "executed", False)           # owned by whoever runs the tests
    other = a_user_other_than_the_current_one()
    r = call(sandbox, f"SERVICE_USER={other}; APP_DIR={app}; DRY_RUN=1; reclaim_service_owned_files; echo UNTRUSTED=$VENV_UNTRUSTED")
    assert "UNTRUSTED=0" in r.stdout, r.stdout + r.stderr
    assert "handing them to root" not in r.stdout


def test_an_untrusted_venv_is_rebuilt_and_never_executed(sandbox, tmp_path):
    app = tmp_path / "app"
    marker = tmp_path / "executed"
    make_planted_venv(app, marker, False)
    (app / "requirements.txt").write_text("")

    r = call(sandbox, f"APP_DIR={app}; DRY_RUN=1; VENV_UNTRUSTED=1; setup_venv")
    assert "cannot be trusted to run as root" in r.stdout, r.stdout + r.stderr
    assert f"[dry-run] rm -rf {app}/venv" in r.stdout
    assert not marker.exists()                                      # the planted python never ran


def test_a_trusted_venv_is_reused_and_executed(sandbox, tmp_path):
    """Control for the test above: proves the marker really does detect execution."""
    app = tmp_path / "app"
    marker = tmp_path / "executed"
    make_planted_venv(app, marker, False)
    (app / "requirements.txt").write_text("")

    call(sandbox, f"APP_DIR={app}; DRY_RUN=1; VENV_UNTRUSTED=0; setup_venv")
    assert marker.exists()


def test_full_dry_run_never_executes_a_service_owned_venv(sandbox, tmp_path):
    """End to end: with a service-owned venv in place, nothing from it may run before it is replaced."""
    user, as_root = service_user_for_tests(tmp_path)
    app = tmp_path / "parker"
    app.mkdir()
    for name in ("install.sh", "parker.py", "requirements.txt", ".env.example"):
        shutil.copy(REPO / name, app / name)
    shutil.copytree(REPO / "parker-ui", app / "parker-ui", ignore=shutil.ignore_patterns("__pycache__"))
    marker = tmp_path / "executed"
    make_planted_venv(app, marker, as_root)

    r = run_install(sandbox, "--dry-run", "--yes", "--user", user, "--port", str(free_port()), script=app / "install.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "cannot be trusted to run as root" in r.stdout
    assert not marker.exists(), "a service-owned venv was executed"
    # ...and the ownership repair is reported before the Python environment step.
    assert r.stdout.index("handing them to root") < r.stdout.index("Python environment")


@pytest.mark.skipif(not IS_ROOT, reason="needs root to run find as another user")
@pytest.mark.parametrize("mode,expected", [(0o777, "1"), (0o755, "0")])
def test_a_venv_writable_by_permission_bits_alone_is_untrusted(sandbox, mode, expected):
    """Owned by root, but world-writable: not "owned by the service user", yet it could tamper with it."""
    app = Path("/opt") / f"parker-wr-{os.urandom(3).hex()}"
    (app / "venv" / "bin").mkdir(parents=True)
    (app / "venv" / "bin" / "python3").write_text("#!/bin/sh\nexit 0\n")
    try:
        os.chmod(app, 0o755)
        os.chmod(app / "venv", mode)
        r = call(sandbox, f"SERVICE_USER=nobody; APP_DIR={app}; DRY_RUN=1; reclaim_service_owned_files; echo UNTRUSTED=$VENV_UNTRUSTED")
        assert f"UNTRUSTED={expected}" in r.stdout, r.stdout + r.stderr
        if expected == "1":
            assert "permission bits, not ownership" in r.stdout
    finally:
        shutil.rmtree(app, ignore_errors=True)


# ------------------------------------------ settings warnings and the unreadable-.env stop

def warnings_for(sandbox, tmp_path, env_text):
    env = tmp_path / ".env"
    env.write_text(env_text)
    return call(sandbox, f"warn_about_settings {env}")


def test_no_warnings_when_every_setting_is_configured(sandbox, tmp_path):
    r = warnings_for(sandbox, tmp_path,
                     "WEBROOT=/srv/sites\nCNAME_TARGET=server.example.org\nMAIL_HOSTNAME=mail.example.org\nDKIM_SELECTOR=default\n")
    assert r.returncode == 0, r.stderr
    assert "is not set" not in r.stdout and "are not set" not in r.stdout


def test_missing_settings_are_named(sandbox, tmp_path):
    r = warnings_for(sandbox, tmp_path, "# empty\n")
    assert "WEBROOT is not set" in r.stdout
    assert "CNAME_TARGET is not set" in r.stdout and "--no-dns" in r.stdout
    assert "MAIL_HOSTNAME and/or DKIM_SELECTOR are not set" in r.stdout and "defaults to MAIL_HOSTNAME" in r.stdout


def test_example_placeholders_count_as_missing(sandbox, tmp_path):
    r = warnings_for(sandbox, tmp_path, (REPO / ".env.example").read_text())
    assert "CNAME_TARGET is not set" in r.stdout          # server.yourdomain.com
    assert "WEBROOT is not set" not in r.stdout           # /var/www is a real value
    assert "MAIL_HOSTNAME and/or DKIM_SELECTOR" in r.stdout


def test_mx_hostname_is_never_demanded(sandbox, tmp_path):
    r = warnings_for(sandbox, tmp_path,
                     "WEBROOT=/srv\nCNAME_TARGET=s.example.org\nMAIL_HOSTNAME=mail.example.org\nDKIM_SELECTOR=d\n")
    assert "MX_HOSTNAME is not set" not in r.stdout


@pytest.mark.parametrize("value,is_placeholder", [
    ("", True), ("your_cloudflare_api_token", True), ("ssl@yourdomain.com", True),
    ("Mail.YourDomain.com", True), ("server.yourdomain.com", True),
    ("me@yourcompany.com", False), ("mail.example.org", False), ("/var/www", False),
])
def test_placeholder_rule_matches_parkers(sandbox, value, is_placeholder):
    r = call(sandbox, f'placeholder {value!r} && echo P || echo R')
    assert r.stdout.strip() == ("P" if is_placeholder else "R")
    assert parker.is_placeholder(value) is (is_placeholder and value != "")     # parker.py treats "" as unset separately


def test_unreadable_env_stops_the_installer(sandbox, tmp_path):
    """.env that exists but cannot be read (here: it is a directory) must stop, not be treated as empty."""
    (tmp_path / ".env").mkdir()
    r = call(sandbox, f"require_readable_env {tmp_path}/.env")
    assert r.returncode != 0
    assert "cannot read" in r.stdout + r.stderr and "will not run with defaults" in r.stdout + r.stderr


def test_missing_or_readable_env_is_fine(sandbox, tmp_path):
    assert call(sandbox, f"require_readable_env {tmp_path}/nope.env").returncode == 0
    (tmp_path / ".env").write_text("WEBROOT=/srv\n")
    assert call(sandbox, f"require_readable_env {tmp_path}/.env").returncode == 0


@pytest.mark.skipif(IS_ROOT, reason="root can read any file; this is the normal-user case")
def test_permission_denied_env_stops_with_a_sudo_hint(sandbox, tmp_path):
    env = tmp_path / ".env"
    env.write_text("WEBROOT=/srv\n")
    env.chmod(0o000)
    try:
        r = call(sandbox, f"require_readable_env {env}")
        assert r.returncode != 0
        assert "Permission denied" in r.stdout + r.stderr and "sudo" in r.stdout + r.stderr
    finally:
        env.chmod(0o600)


def test_full_dry_run_stops_before_changing_anything_when_env_is_unreadable(sandbox, tmp_path):
    app = tmp_path / "parker"
    app.mkdir()
    for name in ("install.sh", "parker.py", "requirements.txt", ".env.example"):
        shutil.copy(REPO / name, app / name)
    shutil.copytree(REPO / "parker-ui", app / "parker-ui", ignore=shutil.ignore_patterns("__pycache__"))
    (app / ".env").mkdir()

    r = run_install(sandbox, "--dry-run", "--yes", "--port", str(free_port()), script=app / "install.sh")
    out = r.stdout + r.stderr
    assert r.returncode != 0
    assert "cannot read" in out
    assert "Python environment" not in out              # stopped in preflight, before any step
    assert "[dry-run]" not in out


def test_install_step_reports_missing_settings_even_in_a_dry_run(sandbox, tmp_path):
    """The warnings are part of the installer's .env step (not just a helper that exists)."""
    app = tmp_path / "app"
    app.mkdir()
    shutil.copy(REPO / ".env.example", app / ".env.example")
    (app / ".env").write_text("CLOUDFLARE_API_TOKEN=real\nDEFAULT_SSL_EMAIL=me@example.com\n")

    r = call(sandbox, f"APP_DIR={app}; ASSUME_YES=1; DRY_RUN=1; setup_env_file")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WEBROOT is not set" in r.stdout and "CNAME_TARGET is not set" in r.stdout
