"""
Settings come from .env only (no built-in values for someone's server), an unreadable .env
stops everything, and the Cloudflare zone is found correctly for multi-part TLDs.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import parker

REPO = Path(__file__).resolve().parent.parent
SETTING_VARS = (
    "WEBROOT", "CNAME_TARGET", "MAIL_HOSTNAME", "MX_HOSTNAME", "DKIM_SELECTOR",
    "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "DEFAULT_SSL_EMAIL",
)

DUMP = (
    "import sys, json; sys.path.insert(0, '.'); import parker; "
    "print(json.dumps({k: getattr(parker, k) for k in "
    "['BASE_DIR','CNAME_TARGET','MAIL_HOSTNAME','MX_HOSTNAME','DKIM_SELECTOR',"
    "'CLOUDFLARE_API_TOKEN','CLOUDFLARE_ACCOUNT_ID','DEFAULT_SSL_EMAIL','ENV_LOAD_ERROR']}))"
)


def isolated(tmp_path, env_text=None, extra_env=None, code=DUMP, args=None):
    """Run a copy of parker.py in an empty directory with none of the settings in the environment."""
    shutil.copy(REPO / "parker.py", tmp_path / "parker.py")
    if env_text is not None:
        (tmp_path / ".env").write_text(env_text)
    env = {k: v for k, v in os.environ.items() if k not in SETTING_VARS}
    env.update(extra_env or {})
    cmd = [sys.executable, "-c", code] if args is None else [sys.executable, "parker.py", *args]
    return subprocess.run(cmd, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)


def settings_of(tmp_path, env_text=None, extra_env=None):
    r = isolated(tmp_path, env_text, extra_env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ------------------------------------------------------------ no built-in values

def test_the_source_contains_no_site_specific_hostnames_or_paths():
    source = (REPO / "parker.py").read_text()
    assert "bws" not in source          # was: server.bws.link, mail.bws.link, /bws/phoenix


def test_nothing_is_configured_when_nothing_is_provided(tmp_path):
    s = settings_of(tmp_path)
    assert s["BASE_DIR"] == "" and s["CNAME_TARGET"] == ""
    assert s["MAIL_HOSTNAME"] == "" and s["MX_HOSTNAME"] == "" and s["DKIM_SELECTOR"] == ""
    assert s["ENV_LOAD_ERROR"] is None


def test_settings_are_read_from_dotenv(tmp_path):
    s = settings_of(tmp_path, (
        "WEBROOT=/srv/sites\nCNAME_TARGET=server.example.org\nMAIL_HOSTNAME=mail.example.org\n"
        "MX_HOSTNAME=mx.example.org\nDKIM_SELECTOR=sel1\n"
    ))
    assert s["BASE_DIR"] == "/srv/sites"
    assert s["CNAME_TARGET"] == "server.example.org"
    assert s["MAIL_HOSTNAME"] == "mail.example.org"
    assert s["MX_HOSTNAME"] == "mx.example.org"          # explicit MX wins over MAIL_HOSTNAME
    assert s["DKIM_SELECTOR"] == "sel1"


def test_settings_can_also_come_from_the_real_environment(tmp_path):
    s = settings_of(tmp_path, extra_env={"WEBROOT": "/srv/env", "CNAME_TARGET": "t.example.org"})
    assert s["BASE_DIR"] == "/srv/env" and s["CNAME_TARGET"] == "t.example.org"


@pytest.mark.parametrize("mx_line", ["", "MX_HOSTNAME=\n", "MX_HOSTNAME=   # use the mail host\n", "MX_HOSTNAME=mail.yourdomain.com\n"])
def test_mx_hostname_falls_back_to_mail_hostname(tmp_path, mx_line):
    """Missing, empty, comment-only and placeholder MX_HOSTNAME all mean "use MAIL_HOSTNAME"."""
    s = settings_of(tmp_path, f"MAIL_HOSTNAME=mail.example.org\n{mx_line}")
    assert s["MX_HOSTNAME"] == "mail.example.org"


def test_mx_hostname_does_not_invent_a_value(tmp_path):
    assert settings_of(tmp_path, "DKIM_SELECTOR=default\n")["MX_HOSTNAME"] == ""


def test_the_example_file_placeholders_are_not_taken_as_real_settings(tmp_path):
    """Copying .env.example verbatim must not put 'mail.yourdomain.com' into SPF/MX records."""
    s = settings_of(tmp_path, (REPO / ".env.example").read_text())
    assert s["BASE_DIR"] == "/var/www" and s["DKIM_SELECTOR"] == "default"      # real example values
    assert s["CNAME_TARGET"] == ""
    assert s["MAIL_HOSTNAME"] == "" and s["MX_HOSTNAME"] == ""
    assert s["CLOUDFLARE_API_TOKEN"] == "" and s["CLOUDFLARE_ACCOUNT_ID"] == "" and s["DEFAULT_SSL_EMAIL"] == ""


@pytest.mark.parametrize("value,expected", [
    ("your_cloudflare_api_token", True), ("ssl@yourdomain.com", True), ("mail.yourdomain.com", True),
    ("server.yourdomain.com", True), ("mail.example.org", False), ("me@yourcompany.com", False), ("", False),
])
def test_placeholder_detection(value, expected):
    assert parker.is_placeholder(value) is expected


def test_empty_webroot_is_not_treated_as_the_current_directory(monkeypatch, tmp_path):
    """validate_path used abspath('') == cwd: an unset WEBROOT would have allowed the working directory."""
    monkeypatch.setattr(parker, "BASE_DIR", "")
    monkeypatch.setattr(parker.tempfile, "gettempdir", lambda: "/nonexistent-tmp")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(PermissionError):
        parker.validate_path(str(tmp_path / "anything"))


# --------------------------------------------- a run stops when a needed setting is missing

def provision_args(*extra):
    return ["--yes", "--domain", "a.example.test", "--no-www", "--type", "node", "--port", "3100", *extra]


def test_missing_webroot_stops_before_the_first_question(env, monkeypatch, capsys):
    monkeypatch.setattr(parker, "BASE_DIR", "")
    with pytest.raises(SystemExit) as exc:
        env.run_main(*provision_args("--no-dns"))
    out = capsys.readouterr().out
    assert exc.value.code == 1
    assert "WEBROOT is not set" in out and "assigning project directories" in out
    assert env.prompts == [] and not env.commands
    assert list(env.available.iterdir()) == []


def test_missing_webroot_message_names_the_env_file_and_the_example_placeholders(env, monkeypatch, capsys):
    monkeypatch.setattr(parker, "BASE_DIR", "")
    with pytest.raises(SystemExit):
        env.run_main(*provision_args("--no-dns"))
    out = capsys.readouterr().out
    assert str(parker.ENV_FILE) in out and ".env.example" in out and "WEBROOT=..." in out


def test_missing_cname_target_stops_a_dns_run_before_any_change(env, monkeypatch, capsys):
    env.enable_cloudflare()
    monkeypatch.setattr(parker, "CNAME_TARGET", "")
    with pytest.raises(SystemExit) as exc:
        env.run_main(*provision_args())
    out = capsys.readouterr().out
    assert exc.value.code == 1
    assert "CNAME_TARGET is not set" in out and "--no-dns" in out
    assert env.cf.calls == [] and list(env.available.iterdir()) == [] and list(env.web.iterdir()) == []


def test_missing_cname_target_is_fine_when_dns_is_skipped(env, monkeypatch):
    monkeypatch.setattr(parker, "CNAME_TARGET", "")
    env.run_main(*provision_args("--no-dns"))
    assert (env.available / "a.example.test.conf").exists()


def test_cname_records_point_at_the_configured_target(env, monkeypatch):
    env.enable_cloudflare()
    seen = []
    monkeypatch.setattr(env.cf, "create_site_cname_record", lambda self, **kw: seen.append(kw["content"]), raising=False)
    monkeypatch.setattr(parker, "CNAME_TARGET", "server.example.org")
    env.run_main(*provision_args())
    assert seen == ["server.example.org"]


@pytest.mark.parametrize("missing,names", [
    ("MAIL_HOSTNAME", ["MAIL_HOSTNAME"]),
    ("DKIM_SELECTOR", ["DKIM_SELECTOR"]),
])
def test_mail_setup_needs_its_settings_and_says_which(env, monkeypatch, capsys, missing, names):
    env.enable_cloudflare()
    monkeypatch.setattr(parker, missing, "")
    monkeypatch.setattr(parker, "MX_HOSTNAME", "")
    with pytest.raises(SystemExit) as exc:
        env.run_main("--yes", "--domain", "example.test", "--www", "--type", "php", "--mail-dns")
    out = capsys.readouterr().out
    assert exc.value.code == 1
    assert f"{names[0]} is not set" in out and "mail authentication" in out
    assert [c for c in env.cf.calls if c[0] in ("TXT", "MX")] == []
    assert list(env.available.iterdir()) == []


def test_mail_setup_reports_every_missing_setting_at_once(env, monkeypatch, capsys):
    env.enable_cloudflare()
    monkeypatch.setattr(parker, "MAIL_HOSTNAME", "")
    monkeypatch.setattr(parker, "DKIM_SELECTOR", "")
    with pytest.raises(SystemExit):
        env.run_main("--yes", "--domain", "example.test", "--www", "--type", "php", "--mail-dns")
    out = capsys.readouterr().out
    assert "MAIL_HOSTNAME, DKIM_SELECTOR are not set" in out


def test_mail_setup_works_without_mx_hostname_because_it_falls_back(env, capsys):
    """MX_HOSTNAME is optional: with MAIL_HOSTNAME set, mail setup does not ask for it."""
    env.enable_cloudflare()
    env.run_main("--yes", "--domain", "example.test", "--www", "--type", "php", "--mail-dns")
    assert ("MX", "example.test", parker.MX_HOSTNAME) in env.cf.calls
    assert "MX_HOSTNAME" not in capsys.readouterr().out


def test_removing_dns_needs_cname_target_and_changes_nothing_without_it(env, monkeypatch, capsys):
    env.run_main(*provision_args("--no-dns"))
    env.enable_cloudflare()
    monkeypatch.setattr(parker, "CNAME_TARGET", "")

    with pytest.raises(SystemExit) as exc:
        env.run_main("--remove", "a.example.test", "--yes", "--remove-dns")

    assert exc.value.code == 1
    assert "CNAME_TARGET is not set" in capsys.readouterr().out
    assert (env.available / "a.example.test.conf").exists()          # nginx site was NOT removed first


# ------------------------------------------------------------- an unreadable .env stops everything

def test_load_env_raises_for_a_directory_and_for_bad_encoding(tmp_path):
    directory = tmp_path / "dir.env"
    directory.mkdir()
    with pytest.raises(parker.EnvFileError, match="Cannot read"):
        parker.load_env(directory)

    binary = tmp_path / "binary.env"
    binary.write_bytes(b"KEY=\xff\xfe\x00bad\n")
    with pytest.raises(parker.EnvFileError, match="Cannot read"):
        parker.load_env(binary)


def test_load_env_error_tells_the_user_what_to_do(tmp_path):
    directory = tmp_path / "dir.env"
    directory.mkdir()
    with pytest.raises(parker.EnvFileError) as exc:
        parker.load_env(directory)
    text = str(exc.value)
    assert str(directory) in text and "sudo" in text and "default settings" in text


def test_a_missing_env_file_is_not_an_error(tmp_path):
    parker.load_env(tmp_path / "does-not-exist.env")          # must not raise


@pytest.mark.parametrize("argv", [["--list"], provision_args("--no-dns"), ["--remove", "a.example.test", "--yes"]])
def test_main_refuses_every_operation_when_env_is_unreadable(env, monkeypatch, capsys, argv):
    monkeypatch.setattr(parker, "ENV_LOAD_ERROR", "Cannot read /x/.env: Permission denied. Run as root.")
    with pytest.raises(SystemExit) as exc:
        env.run_main(*argv)
    assert exc.value.code == 1
    assert "Cannot read /x/.env: Permission denied" in capsys.readouterr().out
    assert env.prompts == [] and not env.commands
    assert list(env.available.iterdir()) == [] and list(env.web.iterdir()) == []


def test_unreadable_env_is_reported_before_the_root_check(env, monkeypatch, capsys):
    """A normal user who cannot read a root-only .env should be told about the file, not just 'run as root'."""
    called = []
    monkeypatch.setattr(parker, "ensure_root", lambda: called.append(1))
    monkeypatch.setattr(parker, "ENV_LOAD_ERROR", "Cannot read /x/.env: Permission denied.")
    with pytest.raises(SystemExit):
        env.run_main(*provision_args("--no-dns"))
    assert called == []


def test_end_to_end_an_unreadable_env_stops_with_a_clean_error(tmp_path):
    """Real process: .env is a directory (unreadable as a file for any user, root included)."""
    (tmp_path / ".env").mkdir()
    for argv in (["--list"], ["--yes", "--domain", "a.example.test", "--type", "node", "--no-dns"]):
        r = isolated(tmp_path, args=argv)
        assert r.returncode == 1, r.stdout + r.stderr
        assert "Cannot read" in r.stdout and "Traceback" not in r.stdout + r.stderr


def test_help_still_works_when_env_is_unreadable(tmp_path):
    (tmp_path / ".env").mkdir()
    r = isolated(tmp_path, args=["--help"])
    assert r.returncode == 0 and "usage:" in r.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file; the permission case needs a normal user")
def test_end_to_end_permission_denied(tmp_path):
    shutil.copy(REPO / "parker.py", tmp_path / "parker.py")
    env_file = tmp_path / ".env"
    env_file.write_text("WEBROOT=/srv/x\n")
    env_file.chmod(0o000)
    try:
        r = isolated(tmp_path, args=["--list"])
        assert r.returncode == 1
        assert "Permission denied" in r.stdout and "Traceback" not in r.stdout + r.stderr
    finally:
        env_file.chmod(0o600)


def test_a_readable_env_reports_no_error(tmp_path):
    assert settings_of(tmp_path, "WEBROOT=/srv/x\n")["ENV_LOAD_ERROR"] is None
    assert settings_of(tmp_path, "")["ENV_LOAD_ERROR"] is None        # empty file is fine too


# ------------------------------------------------------ multi-part TLDs / the right zone

@pytest.mark.parametrize("domain,root", [
    ("example.com", "example.com"),
    ("www.example.com", "example.com"),
    ("a.b.example.com", "example.com"),
    ("example.co.in", "example.co.in"),
    ("shop.example.co.uk", "example.co.uk"),
    ("example.com.au", "example.com.au"),
    ("example.co.nz", "example.co.nz"),                # was "co.nz"
    ("www.shop.example.co.nz", "example.co.nz"),
    ("shop.example.com.br", "example.com.br"),
    ("example.co.za", "example.co.za"),
    ("blog.example.co.jp", "example.co.jp"),
    ("example.govt.nz", "example.govt.nz"),            # in the list only: the ccTLD rule cannot guess "govt"
    ("shop.example.id.au", "example.id.au"),
    ("example.me.uk", "example.me.uk"),
    ("example.firm.in", "example.firm.in"),
    ("example.co.xx", "example.co.xx"),                # country not in the list: the ccTLD rule
    ("example.com.zz", "example.com.zz"),
    ("example.org.zz", "example.org.zz"),
    ("example.io", "example.io"),
    ("sub.example.io", "example.io"),
    ("example.co", "example.co"),                      # .co is itself a TLD, not a suffix
    ("sub.example.com", "example.com"),                # "com" as a second level only counts under a 2-letter TLD
])
def test_extract_root_domain(domain, root):
    assert parker.extract_root_domain(domain) == root


@pytest.mark.parametrize("domain,root,sub", [
    ("example.co.nz", "example.co.nz", None),
    ("shop.example.co.nz", "example.co.nz", "shop"),
    ("a.b.example.com", "example.com", "a.b"),
])
def test_subdomain_part(domain, root, sub):
    assert parker.get_subdomain_part(domain) == sub
    assert parker.get_subdomain_part(domain, root) == sub


def test_zone_candidates_order_and_exclusions():
    c = parker.zone_candidates("a.b.example.co.nz")
    assert c[0] == "example.co.nz"                                     # the usual guess first
    assert c[1:] == ["a.b.example.co.nz", "b.example.co.nz"]           # then longer names, longest first
    assert "co.nz" not in c and "nz" not in c                          # registry suffixes / bare TLD never queried
    assert parker.zone_candidates("example.com") == ["example.com"]
    assert parker.zone_candidates("www.example.com") == ["example.com", "www.example.com"]


def test_find_zone_takes_the_usual_guess_in_one_call(env):
    env.cf.zone_names = {"example.com"}
    zone, name = parker.find_zone(env.cf(), "shop.example.com")
    assert name == "example.com" and zone["id"] == "zone1"
    assert env.cf.zone_lookups == ["example.com"]


def test_find_zone_asks_cloudflare_when_the_suffix_is_unknown(env):
    """gv.at is not in the list, so the guess for shop.example.gv.at is the wrong 'gv.at'."""
    assert parker.extract_root_domain("shop.example.gv.at") == "gv.at"
    env.cf.zone_names = {"example.gv.at"}
    zone, name = parker.find_zone(env.cf(), "shop.example.gv.at")
    assert name == "example.gv.at" and zone is not None
    assert env.cf.zone_lookups == ["gv.at", "shop.example.gv.at", "example.gv.at"]


def test_find_zone_prefers_a_delegated_subdomain_zone_over_nothing(env):
    env.cf.zone_names = {"shop.example.com"}
    zone, name = parker.find_zone(env.cf(), "shop.example.com")
    assert name == "shop.example.com"


def test_find_zone_returns_the_guess_when_there_is_no_zone(env):
    env.cf.zone_names = set()
    zone, name = parker.find_zone(env.cf(), "shop.example.co.nz")
    assert zone is None and name == "example.co.nz"                   # what "create new zone" should offer
    assert "co.nz" not in env.cf.zone_lookups and "nz" not in env.cf.zone_lookups


def test_find_zone_propagates_api_failures(env):
    class Broken(env.cf):
        def get_zone(self, root):
            raise parker.CloudflareError("token rejected")

    with pytest.raises(parker.CloudflareError, match="token rejected"):
        parker.find_zone(Broken(), "example.co.nz")


def test_a_co_nz_site_gets_its_dns_in_the_right_zone(env):
    """Before: root was guessed as 'co.nz', so the zone lookup failed for a domain the account owns."""
    env.enable_cloudflare()
    env.cf.zone_names = {"example.co.nz"}
    env.run_main("--yes", "--domain", "shop.example.co.nz", "--no-www", "--type", "node", "--port", "3100")
    assert ("cname", "shop.example.co.nz") in env.cf.calls
    assert env.cf.zone_lookups[0] == "example.co.nz"


def test_mail_setup_works_for_the_root_of_a_co_nz_domain(env):
    """Before: example.co.nz looked like a subdomain of 'co.nz', so mail setup was silently skipped."""
    env.enable_cloudflare()
    env.cf.zone_names = {"example.co.nz"}
    env.run_main("--yes", "--domain", "example.co.nz", "--www", "--type", "php", "--mail-dns")
    assert any(c[0] == "MX" and c[1] == "example.co.nz" for c in env.cf.calls)


def test_zone_found_by_lookup_defines_the_root_even_when_the_guess_differs(env, capsys):
    env.enable_cloudflare()
    env.cf.zone_names = {"example.gv.at"}
    env.run_main("--yes", "--domain", "example.gv.at", "--www", "--type", "php", "--mail-dns")
    assert any(c[0] == "MX" and c[1] == "example.gv.at" for c in env.cf.calls)       # treated as a root, not a subdomain
    assert "Cloudflare Zone Found: example.gv.at" in capsys.readouterr().out


def test_remove_dns_uses_the_same_zone_lookup(env):
    env.run_main("--yes", "--domain", "x.example.co.nz", "--no-www", "--type", "node", "--port", "3100", "--no-dns")
    env.enable_cloudflare()
    env.cf.zone_names = {"example.co.nz"}
    env.cf.existing_records = {"x.example.co.nz": [{"id": "r1", "type": "CNAME", "content": parker.CNAME_TARGET}]}
    env.run_main("--remove", "x.example.co.nz", "--yes", "--remove-dns")
    assert ("delete_record", "r1") in env.cf.calls
