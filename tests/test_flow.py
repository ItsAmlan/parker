"""End-to-end runs of parker.main() against a fully faked host (see conftest.py)."""
import json
import os
import pwd
import stat

import pytest

import parker


def domain_dir(env, domain):
    return env.web / domain


def provision_node(env, domain="app.example.test", port="3100"):
    """Interactive run: node project on `port`, no Cloudflare."""
    env.say(domain, "n", "y", "4", "", port, "y")
    env.run_main()


# ---------------------------------------------------------------- happy paths

def test_node_project_end_to_end(env):
    provision_node(env)

    project = domain_dir(env, "app.example.test")
    conf = env.conf("app.example.test")

    assert "proxy_pass http://127.0.0.1:3100;" in conf
    assert (env.enabled / "app.example.test.conf").is_symlink()
    assert os.readlink(env.enabled / "app.example.test.conf") == str(env.available / "app.example.test.conf")

    # Project directory is only assigned: challenge dir + manifest, no public_html, no build output.
    assert sorted(p.name for p in project.iterdir()) == [".parker.json", ".well-known"]
    assert (project / ".well-known" / "acme-challenge").is_dir()
    assert stat.S_IMODE((project / ".well-known" / "acme-challenge").stat().st_mode) == 0o755

    manifest = json.loads((project / ".parker.json").read_text())
    assert manifest["project_type"] == 4 and manifest["port"] == 3100
    assert manifest["domains"] == ["app.example.test"]

    assert ["nginx", "-t"] in env.commands
    assert ["systemctl", "reload", "nginx"] in env.commands

    certbot = env.commands_named("certbot")[0]
    assert certbot[:2] == ["certbot", "run"]
    assert certbot[certbot.index("--webroot-path") + 1] == str(project)
    assert certbot[certbot.index("--authenticator") + 1] == "webroot"
    assert "-d" in certbot and "app.example.test" in certbot

    # Nothing may ever run npm/node: Parker no longer builds projects.
    assert not any(c[0] in ("npm", "node", "npx") for c in env.commands)


def test_php_project_gets_public_html_and_acme_dir(env):
    env.say("shop.example.test", "y", "y", "1", "", "y")
    env.run_main()

    project = domain_dir(env, "shop.example.test")
    assert (project / "public_html").is_dir()
    assert (project / ".well-known" / "acme-challenge").is_dir()
    assert "server_name shop.example.test www.shop.example.test;" in env.conf("shop.example.test")
    assert f"root {project}/public_html;" in env.conf("shop.example.test")


def test_static_project_with_api_backend(env):
    env.say("spa.example.test", "n", "y", "3", "", "y", "5000", "y")
    env.run_main()

    conf = env.conf("spa.example.test")
    assert "proxy_pass http://127.0.0.1:5000/;" in conf
    assert "try_files $uri /index.html;" in conf


def test_invalid_answers_are_reprompted_not_fatal(env):
    env.say(
        "bad_domain", "example", "ok.example.test",     # domain
        "n", "y",                                        # www, no-dns
        "9", "x", "4",                                   # project type
        "/etc", str(env.web), "",                        # directory: outside, base itself, default
        "9000", "abc", "80", "3100",                     # port: reserved, junk, privileged, ok
        "y",
    )
    env.run_main()

    assert "proxy_pass http://127.0.0.1:3100;" in env.conf("ok.example.test")
    assert not env.answers          # every scripted answer was consumed


def test_dry_run_changes_nothing_and_prints_the_config(env, capsys):
    before = {p for p in env.root.rglob("*")}
    env.say("app.example.test", "n", "y", "4", "", "3100", "y")
    env.run_main("--dry-run")

    assert {p for p in env.root.rglob("*")} == before        # not a single file or directory added
    out = capsys.readouterr().out
    assert "[DRY RUN] Generated nginx config" in out
    assert "proxy_pass http://127.0.0.1:3100;" in out


def test_no_ssl_flag_skips_certbot(env, capsys):
    env.run_main("--yes", "--domain", "a.example.test", "--no-www", "--type", "node",
                 "--port", "3400", "--no-dns", "--no-ssl")
    assert not env.commands_named("certbot")
    assert "SSL          : skipped (--no-ssl)" in capsys.readouterr().out


# -------------------------------------------------------------- flags / --yes

def test_fully_non_interactive_run_never_prompts(env):
    env.run_main("--yes", "--domain", "API.Example.test", "--no-www", "--type", "next",
                 "--port", "3300", "--no-dns")

    assert env.prompts == []
    assert "proxy_pass http://127.0.0.1:3300;" in env.conf("api.example.test")


def test_yes_mode_defaults_the_port_when_none_is_given(env):
    env.run_main("--yes", "--domain", "a.example.test", "--no-www", "--type", "4", "--no-dns")
    ports = parker.find_proxy_ports(env.conf("a.example.test"))
    assert len(ports) == 1 and ports[0] >= 3000


@pytest.mark.parametrize("argv,needle", [
    (["--domain", "a.example.test", "--no-dns"], "Choose project type"),          # --type missing
    (["--domain", "bad_domain", "--type", "4", "--no-dns"], "Invalid domain"),
    (["--domain", "a.example.test", "--type", "9", "--no-dns"], "Enter a number"),
    (["--domain", "a.example.test", "--type", "4", "--port", "80", "--no-dns"], "1024"),
    (["--domain", "a.example.test", "--type", "4", "--port", "9000", "--no-dns"], "Parker dashboard"),
    (["--domain", "a.example.test", "--type", "4", "--dir", "/etc/x", "--no-dns"], "must be inside"),
    (["--domain", "a.example.test", "--type", "4"], "--no-dns"),                   # no token, no --no-dns
])
def test_yes_mode_fails_cleanly_on_missing_or_bad_input(env, capsys, argv, needle):
    with pytest.raises(SystemExit) as exc:
        env.run_main("--yes", "--no-www", *argv)

    assert exc.value.code == 1
    assert needle in capsys.readouterr().out
    assert not list(env.available.iterdir())          # nothing was written
    assert not list(env.web.iterdir())


def test_reprovisioning_needs_force_in_yes_mode(env, capsys):
    args = ["--yes", "--domain", "a.example.test", "--no-www", "--type", "node", "--port", "3100", "--no-dns"]
    env.run_main(*args)

    with pytest.raises(SystemExit) as exc:
        env.run_main(*args)
    assert exc.value.code == 1
    assert "--force" in capsys.readouterr().out

    env.run_main(*args, "--force")          # explicit opt-in works


def test_shared_port_needs_force(env):
    env.run_main("--yes", "--domain", "a.example.test", "--no-www", "--type", "node", "--port", "3100", "--no-dns")

    with pytest.raises(SystemExit):
        env.run_main("--yes", "--domain", "b.example.test", "--no-www", "--type", "node", "--port", "3100", "--no-dns")
    assert not (env.available / "b.example.test.conf").exists()

    env.run_main("--yes", "--domain", "b.example.test", "--no-www", "--type", "node", "--port", "3100",
                 "--no-dns", "--force")
    assert (env.available / "b.example.test.conf").exists()


def test_php_ignores_port_with_a_warning(env, capsys):
    env.run_main("--yes", "--domain", "a.example.test", "--no-www", "--type", "php", "--port", "3000", "--no-dns")
    assert "--port was ignored" in capsys.readouterr().out


# -------------------------------------------------------------------- re-runs

def test_second_run_defaults_to_the_recorded_type_and_port(env):
    provision_node(env, port="3200")
    env.prompts.clear()

    env.say("app.example.test", "n", "y", "y", "", "", "", "y")   # domain, www, reprov, no-dns, type, dir, port, go
    env.run_main()

    assert any("Choose project type [4]" in p for p in env.prompts)
    assert any("Port your app listens on [3200]" in p for p in env.prompts)


def test_suggested_port_skips_ports_other_sites_use(env):
    (env.enabled / "other.conf").write_text("server { location / { proxy_pass http://127.0.0.1:3000; } }")
    assert parker.suggest_free_port("new.example.test") != 3000
    assert parker.suggest_free_port("new.example.test", start=3000) > 3000


def test_shared_port_prompt_can_be_declined_interactively(env):
    provision_node(env, "a.example.test", "3100")

    env.say("b.example.test", "n", "y", "4", "", "3100", "n", "3101", "y")
    env.run_main()

    assert "proxy_pass http://127.0.0.1:3101;" in env.conf("b.example.test")


# ------------------------------------------------------------------- failures

def test_nginx_failure_rolls_everything_back_and_reloads(env):
    env.nginx_test_fails = True

    with pytest.raises(SystemExit) as exc:
        provision_node(env)

    assert exc.value.code == 1
    assert list(env.available.iterdir()) == []
    assert list(env.enabled.iterdir()) == []
    assert list(env.web.iterdir()) == []
    assert env.commands[-1] == ["(rollback) reload nginx"]


def test_preexisting_config_survives_a_failed_run(env):
    old = "# hand written\nserver { listen 80; server_name a.example.test; }\n"
    (env.available / "a.example.test.conf").write_text(old)
    os.symlink(env.available / "a.example.test.conf", env.enabled / "a.example.test.conf")
    env.nginx_test_fails = True

    with pytest.raises(SystemExit):
        env.run_main("--yes", "--domain", "a.example.test", "--no-www", "--type", "node",
                     "--port", "3100", "--no-dns", "--force")

    assert (env.available / "a.example.test.conf").read_text() == old
    assert not list(env.available.glob("*.phoenix_bak")) and not list(env.available.glob("*.parker_tmp"))


def test_certbot_failure_on_a_new_domain_keeps_the_site_up_over_http(env, capsys):
    env.certbot_fails = True
    provision_node(env)                       # no SystemExit: the run completes with warnings

    assert (env.enabled / "app.example.test.conf").is_symlink()
    assert len(env.commands_named("certbot")) == parker.SSL_ATTEMPTS
    out = capsys.readouterr().out
    assert "Setup Completed With Warnings" in out
    assert "sudo certbot run --authenticator webroot" in out


def test_certbot_failure_on_a_domain_with_a_certificate_rolls_back_to_the_old_config(env):
    (env.live / "a.example.test").mkdir()
    old = "# https config written by certbot\n"
    (env.available / "a.example.test.conf").write_text(old)
    os.symlink(env.available / "a.example.test.conf", env.enabled / "a.example.test.conf")
    env.certbot_fails = True

    with pytest.raises(SystemExit) as exc:
        env.run_main("--yes", "--domain", "a.example.test", "--no-www", "--type", "node",
                     "--port", "3100", "--no-dns", "--force")

    assert exc.value.code == 1
    assert (env.available / "a.example.test.conf").read_text() == old
    assert not (env.web / "a.example.test").exists()


def test_preflight_blocks_a_missing_php_snippet_before_anything_changes(env, capsys):
    env.snippet.unlink()
    env.say("shop.example.test", "n", "y", "1", "", "y")

    with pytest.raises(SystemExit):
        env.run_main()

    assert "PHP-FPM snippet" in capsys.readouterr().out
    assert list(env.web.iterdir()) == []


def test_ctrl_c_at_the_confirmation_changes_nothing(env, capsys):
    env.say("app.example.test", "n", "y", "4", "", "3100")

    def interrupt_at_review(prompt=""):
        if "Proceed with these settings" in prompt:
            raise KeyboardInterrupt
        return env.answers.pop(0)

    env.mp.setattr("builtins.input", interrupt_at_review)

    with pytest.raises(SystemExit) as exc:
        env.run_main()

    assert exc.value.code == 1
    assert "interrupted by user" in capsys.readouterr().out
    assert list(env.web.iterdir()) == [] and list(env.available.iterdir()) == []


def test_declining_the_review_screen_changes_nothing(env):
    env.say("app.example.test", "n", "y", "4", "", "3100", "n")

    with pytest.raises(SystemExit) as exc:
        env.run_main()

    assert exc.value.code == 0
    assert list(env.web.iterdir()) == [] and list(env.available.iterdir()) == []


# ----------------------------------------------------------------- ownership

def test_ensure_directory_applies_owner_only_to_directories_it_creates(tmp_path, monkeypatch):
    monkeypatch.setattr(parker, "BASE_DIR", str(tmp_path))
    me = (os.getuid(), os.getgid())
    existing = tmp_path / "existing"
    existing.mkdir()
    os.chmod(existing, 0o700)

    parker.ensure_directory(str(existing / "new" / "deeper"), mode=0o755, owner=me)

    assert stat.S_IMODE((existing / "new").stat().st_mode) == 0o755
    assert stat.S_IMODE(existing.stat().st_mode) == 0o700          # pre-existing dir untouched


def test_resolve_owner():
    me = pwd.getpwuid(os.getuid())
    assert parker.resolve_owner("") is None
    assert parker.resolve_owner(me.pw_name)[0] == me.pw_uid
    with pytest.raises(parker.ParkerError, match="does not exist"):
        parker.resolve_owner("no-such-user-xyz")
    with pytest.raises(parker.ParkerError, match="group"):
        parker.resolve_owner(f"{me.pw_name}:no-such-group-xyz")


def test_unknown_owner_is_caught_in_preflight(env, capsys):
    with pytest.raises(SystemExit):
        env.run_main("--yes", "--domain", "a.example.test", "--no-www", "--type", "node",
                     "--port", "3100", "--no-dns", "--owner", "no-such-user-xyz")
    assert "does not exist" in capsys.readouterr().out
    assert list(env.web.iterdir()) == []


@pytest.mark.skipif(os.geteuid() != 0, reason="chown to another user needs root")
def test_owner_flag_chowns_new_directories(env):
    env.run_main("--yes", "--domain", "a.example.test", "--no-www", "--type", "php",
                 "--no-dns", "--owner", "nobody")
    uid = pwd.getpwnam("nobody").pw_uid
    project = env.web / "a.example.test"
    for path in (project, project / "public_html", project / ".well-known", project / ".well-known" / "acme-challenge"):
        assert path.stat().st_uid == uid


# ------------------------------------------------------------ DNS / mail / CF

def test_dns_records_are_created_for_every_hostname(env):
    env.enable_cloudflare()
    env.say("shop.example.test", "y", "1", "", "y")     # domain, www, type, dir, go (subdomain: no mail prompts)
    env.run_main()

    assert ("cname", "shop.example.test") in env.cf.calls
    assert ("cname", "www.shop.example.test") in env.cf.calls


def test_mail_setup_end_to_end_with_hidden_password(env, capsys):
    env.enable_cloudflare()
    env.say("example.test", "y", "1", "", "y", "y", "support", "", "y")
    env.secrets.append("S3cretPassw0rd!")
    env.run_main()

    types = [c[0] for c in env.cf.calls]
    assert types.count("TXT") == 3 and types.count("MX") == 1     # SPF, DMARC, DKIM, MX

    assert (env.root / "virtual_domains").read_text() == "example.test\n"
    assert "support@example.test:{BLF-CRYPT}fakehash" in (env.root / "dovecot-users").read_text()
    assert "support@example.test    example.test/support/" in (env.root / "virtual_mailbox_maps").read_text()
    assert (env.root / "vhosts" / "example.test" / "support" / "cur").is_dir()

    # The password was requested through the hidden prompt and appears nowhere else.
    assert any("Enter password for support@example.test" in p for p in env.prompts)
    assert not any("S3cretPassw0rd!" in " ".join(c) for c in env.commands)
    assert "S3cretPassw0rd!" not in capsys.readouterr().out
    assert "S3cretPassw0rd!" not in env.log.read_text()


def test_existing_spf_dmarc_dkim_and_mx_records_are_not_duplicated(env, capsys):
    env.enable_cloudflare()
    env.cf.existing_records = {
        "example.test": [
            {"type": "TXT", "content": '"v=spf1 include:other.example ~all"'},
            {"type": "MX", "content": parker.MX_HOSTNAME},
        ],
        "_dmarc.example.test": [{"type": "TXT", "content": '"v=DMARC1; p=none"'}],
        f"{parker.DKIM_SELECTOR}._domainkey.example.test": [{"type": "TXT", "content": '"v=DKIM1; k=rsa; p=OLD"'}],
    }
    env.say("example.test", "y", "1", "", "y", "n", "y")
    env.run_main()

    assert [c for c in env.cf.calls if c[0] in ("TXT", "MX")] == []
    out = capsys.readouterr().out
    assert "Skipping TXT example.test; an equivalent record already exists." in out
    assert "differs from what Parker would create" in out          # SPF/DKIM values were not the ones Parker would write


def test_mail_is_opt_in_with_yes_and_mailboxes_need_a_terminal(env):
    env.enable_cloudflare()
    env.run_main("--yes", "--domain", "example.test", "--www", "--type", "php")
    assert [c for c in env.cf.calls if c[0] in ("TXT", "MX")] == []

    env.run_main("--yes", "--domain", "other.test", "--www", "--type", "php", "--mail-dns")
    assert [c[0] for c in env.cf.calls].count("MX") == 1
    assert not (env.root / "dovecot-users").exists()


def test_new_zone_is_rolled_back_only_until_nameservers_are_confirmed(env):
    env.enable_cloudflare(zone=None)
    env.say("example.test", "y", "1", "", "n", "y", "")      # domain, create zone, type, dir, mail=n, go, "Press Enter"
    env.run_main("--no-www")
    assert ("delete_zone", "zone-new") not in env.cf.calls       # confirmed -> zone must survive

    # Ctrl-C while waiting for the registrar change: the zone is removed again.
    env.cf.calls.clear()
    (env.available / "example.test.conf").unlink()
    (env.enabled / "example.test.conf").unlink()
    env.say("example.test", "y", "1", "", "n", "y")
    env.mp.setattr("builtins.input", _raise_on_press_enter(env))
    with pytest.raises(SystemExit):
        env.run_main("--no-www", "--force")
    assert ("delete_zone", "zone-new") in env.cf.calls


def _raise_on_press_enter(env):
    def fake_input(prompt=""):
        env.prompts.append(prompt)
        if "Press Enter" in prompt:
            raise KeyboardInterrupt
        return env.answers.pop(0)
    return fake_input


def test_failure_after_nameserver_confirmation_undoes_records_but_keeps_the_zone(env):
    env.enable_cloudflare(zone=None)
    (env.live / "example.test").mkdir()                # so a certbot failure aborts the run
    env.certbot_fails = True
    env.say("example.test", "y", "1", "", "n", "y", "")
    with pytest.raises(SystemExit):
        env.run_main("--no-www", "--force")

    assert ("delete_zone", "zone-new") not in env.cf.calls
    assert not (env.available / "example.test.conf").exists()


# ----------------------------------------------------------- list and remove

def test_list_shows_managed_sites_only(env, capsys):
    provision_node(env)
    (env.available / "handmade.conf").write_text("server { listen 80; }\n")
    capsys.readouterr()

    env.run_main("--list")
    out = capsys.readouterr().out

    assert "app.example.test" in out and "Node.js app on a port" in out and "3100" in out
    assert "handmade" not in out


def test_list_when_empty(env, capsys):
    env.run_main("--list")
    assert "No Parker-managed sites found." in capsys.readouterr().out


def test_remove_deletes_nginx_config_and_certificate_but_not_project_files(env, capsys):
    provision_node(env)
    (env.live / "app.example.test").mkdir()
    (env.web / "app.example.test" / "index.js").write_text("console.log(1)")

    env.run_main("--remove", "app.example.test", "--yes")

    assert not (env.available / "app.example.test.conf").exists()
    assert not os.path.lexists(env.enabled / "app.example.test.conf")
    assert ["certbot", "delete", "--cert-name", "app.example.test", "--non-interactive"] in env.commands
    assert (env.web / "app.example.test" / "index.js").exists()
    assert not list(env.available.glob("*.phoenix_bak"))
    assert "project files, mail configuration" in capsys.readouterr().out


def test_remove_can_keep_the_certificate(env):
    provision_node(env)
    (env.live / "app.example.test").mkdir()
    env.run_main("--remove", "app.example.test", "--yes", "--keep-cert")
    assert ["certbot", "delete", "--cert-name", "app.example.test", "--non-interactive"] not in env.commands
    assert not (env.available / "app.example.test.conf").exists()


def test_remove_interactive_needs_confirmation(env):
    provision_node(env)
    env.say("n")                                         # "Proceed with removal?" (no cert, no CF token)
    with pytest.raises(SystemExit) as exc:
        env.run_main("--remove", "app.example.test")
    assert exc.value.code == 0
    assert (env.available / "app.example.test.conf").exists()


def test_remove_restores_the_site_if_nginx_rejects_the_result(env):
    provision_node(env)
    original = env.conf("app.example.test")
    env.nginx_test_fails = True

    with pytest.raises(SystemExit):
        env.run_main("--remove", "app.example.test", "--yes")

    assert env.conf("app.example.test") == original
    assert (env.enabled / "app.example.test.conf").is_symlink()


def test_remove_refuses_configs_parker_did_not_write(env, capsys):
    (env.available / "manual.example.test.conf").write_text("server { listen 80; }\n")
    with pytest.raises(SystemExit):
        env.run_main("--remove", "manual.example.test", "--yes")
    assert "not created by Parker" in capsys.readouterr().out
    assert (env.available / "manual.example.test.conf").exists()


def test_remove_unknown_domain(env, capsys):
    with pytest.raises(SystemExit):
        env.run_main("--remove", "nothing.example.test", "--yes")
    assert "No nginx config found" in capsys.readouterr().out


def test_remove_dns_only_deletes_parker_cnames(env):
    provision_node(env)
    env.enable_cloudflare()
    env.cf.existing_records = {
        "app.example.test": [
            {"id": "r1", "type": "CNAME", "content": parker.DEFAULT_CNAME_TARGET},
            {"id": "r2", "type": "CNAME", "content": "somewhere-else.example"},
            {"id": "r3", "type": "TXT", "content": "keep me"},
        ]
    }
    env.run_main("--remove", "app.example.test", "--yes", "--remove-dns")
    assert env.cf.calls == [("delete_record", "r1")]


# -------------------------------------------------------------------- audit log

def test_every_run_is_appended_to_the_audit_log(env):
    provision_node(env)
    text = env.log.read_text()
    assert "===== parker run by" in text
    assert "Setup Completed" in text
    assert stat.S_IMODE(env.log.stat().st_mode) == 0o600

    provision_node(env, "b.example.test", "3101")
    assert env.log.read_text().count("===== parker run by") == 2


def test_an_unwritable_audit_log_does_not_stop_the_run(env, monkeypatch):
    monkeypatch.setattr(parker, "PARKER_LOG_FILE", "/nonexistent-dir/parker.log")
    provision_node(env)
    assert (env.available / "app.example.test.conf").exists()


# ------------------------------------------------------------------ small units

def test_append_unique_line_matches_whole_lines_and_repairs_missing_newline(tmp_path, monkeypatch):
    monkeypatch.setattr(parker, "BASE_DIR", str(tmp_path))
    path = tmp_path / "virtual_domains"
    path.write_text("notexample.test")                       # substring trap + no trailing newline

    parker.append_unique_line(str(path), "example.test")
    parker.append_unique_line(str(path), "example.test")     # idempotent

    assert path.read_text() == "notexample.test\nexample.test\n"


def test_validate_path_respects_directory_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr(parker, "BASE_DIR", str(tmp_path / "web"))
    monkeypatch.setattr(parker.tempfile, "gettempdir", lambda: "/nonexistent-tmp")
    parker.validate_path(str(tmp_path / "web" / "site"))
    with pytest.raises(PermissionError):
        parker.validate_path(str(tmp_path / "web-evil" / "site"))
    with pytest.raises(PermissionError):
        parker.validate_path("/etc/passwd")


def test_write_file_is_atomic_and_leaves_no_temp_files(tmp_path, monkeypatch):
    monkeypatch.setattr(parker, "BASE_DIR", str(tmp_path))
    path = tmp_path / "site.conf"
    parker.write_file(str(path), "one")
    parker.write_file(str(path), "two")
    assert path.read_text() == "two"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["site.conf"]


def test_hash_password_never_puts_the_password_on_the_command_line(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        class R: stdout = "{BLF-CRYPT}$2y$05$abc\n"
        return R()

    monkeypatch.setattr(parker, "DRY_RUN", False)
    monkeypatch.setattr(parker.subprocess, "run", fake_run)

    assert parker.hash_password("hunter22") == "{BLF-CRYPT}$2y$05$abc"
    assert "hunter22" not in " ".join(seen["cmd"])
    assert seen["input"] == "hunter22\nhunter22\n"


@pytest.mark.parametrize("files,expected", [
    ({"public_html/wp-config.php": ""}, 2),
    ({"package.json": '{"dependencies": {"next": "15"}}'}, 4),
    ({"app/package.json": '{"dependencies": {"express": "4"}}'}, 4),
    ({"package.json": '{"devDependencies": {"vite": "5"}}'}, 3),
    ({"public_html/index.php": ""}, 1),
    ({"notes.txt": ""}, None),
    ({"package.json": "not json"}, None),
])
def test_detect_project_type(tmp_path, files, expected):
    for name, content in files.items():
        f = tmp_path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    assert parker.detect_project_type(str(tmp_path)) == expected


@pytest.mark.parametrize("value,ok", [
    ("example.com", True), ("a.b.example.co.in", True), ("xn--p1ai.example", True),
    ("localhost", False), ("bad_domain.com", False), ("-a.com", False), ("a..com", False),
    ("example.123", False), ("a" * 64 + ".com", False),
])
def test_domain_validation(value, ok):
    assert parker.is_valid_domain(value) is ok


def test_wait_for_dns_polls_until_the_name_resolves(monkeypatch):
    attempts = {"n": 0}

    def flaky(name, *a, **k):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise parker.socket.gaierror("not yet")
        return []

    monkeypatch.setattr(parker, "DRY_RUN", False)
    monkeypatch.setattr(parker.socket, "getaddrinfo", flaky)
    monkeypatch.setattr(parker.time, "sleep", lambda s: None)

    assert parker.wait_for_dns(["a.example.test"], timeout=60, interval=1) is True
    assert attempts["n"] == 3


def test_wait_for_dns_gives_up_after_the_timeout(monkeypatch):
    clock = iter(range(0, 1000, 20))
    monkeypatch.setattr(parker, "DRY_RUN", False)
    monkeypatch.setattr(parker.socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(parker.socket.gaierror()))
    monkeypatch.setattr(parker.time, "sleep", lambda s: None)
    monkeypatch.setattr(parker.time, "time", lambda: next(clock))

    assert parker.wait_for_dns(["a.example.test"], timeout=60, interval=1) is False


# ---------------------------------------------------------------- stop signals

def _spawn_blocked_run(tmp_path, slow_rollback_seconds=0):
    """
    Starts a real parker.py run that has already changed something (project dir + manifest)
    and is stalled in the nginx step. Returns (process, web_dir, started_marker, done_marker).
    With slow_rollback_seconds a deliberately slow rollback step is registered, to give the
    test a window in which to signal the process *during* its rollback.
    """
    import subprocess
    import sys
    import textwrap
    import time

    web = tmp_path / "web"
    web.mkdir()
    ready, started, done = tmp_path / "ready", tmp_path / "rollback-started", tmp_path / "rollback-done"

    script = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(parker.__file__).rsplit('/', 1)[0]!r})
        import parker
        parker.BASE_DIR = {str(web)!r}
        parker.ensure_root = lambda: None
        parker.PARKER_LOG_FILE = ""

        def slow_rollback_step():
            open({str(started)!r}, "w").close()
            time.sleep({slow_rollback_seconds})
            open({str(done)!r}, "w").close()

        def slow_nginx(domain, config):
            # A change has been made (project dir + manifest); now stall like a slow certbot.
            if {slow_rollback_seconds}:
                parker.rollback_stack.add(slow_rollback_step, label="slow step")
            open({str(ready)!r}, "w").close()
            time.sleep(30)

        parker.setup_nginx = slow_nginx
        parker.command_exists = lambda c: True
        parker.php_snippet_path = lambda: "/dev/null"
        parker.NGINX_SITES_AVAILABLE = parker.NGINX_SITES_ENABLED = {str(tmp_path)!r}
        parker.main(["--yes", "--domain", "a.example.test", "--no-www", "--type", "node",
                     "--port", "3100", "--no-dns"])
    """)

    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 20
    while not ready.exists():
        assert time.time() < deadline, "child never reached the nginx step"
        assert proc.poll() is None, proc.stdout.read()
        time.sleep(0.05)

    assert (web / "a.example.test").exists()          # the run had really changed something
    return proc, web, started, done


@pytest.mark.parametrize("sig_name", ["SIGTERM", "SIGHUP"])
def test_stop_signals_trigger_the_rollback(tmp_path, sig_name):
    """
    systemd stop / a closed dashboard tab deliver SIGTERM or SIGHUP. Without a handler
    those kill parker.py halfway through a run with nothing undone.
    """
    import signal

    proc, web, _, _ = _spawn_blocked_run(tmp_path)

    proc.send_signal(getattr(signal, sig_name))
    out, _ = proc.communicate(timeout=20)

    assert proc.returncode == 1, out
    assert "interrupted by user" in out
    assert "ROLLING BACK CHANGES" in out
    assert not (web / "a.example.test").exists()      # ...and the rollback removed it again


def test_rollback_cannot_be_interrupted_by_further_signals(tmp_path):
    """A second Ctrl-C / SIGTERM arriving mid-rollback must not abandon it halfway."""
    import signal
    import time

    proc, web, started, done = _spawn_blocked_run(tmp_path, slow_rollback_seconds=2)

    proc.send_signal(signal.SIGTERM)                  # starts the rollback
    deadline = time.time() + 20
    while not started.exists():
        assert time.time() < deadline, "rollback never started"
        time.sleep(0.02)

    proc.send_signal(signal.SIGTERM)                  # impatient: signals during the rollback
    proc.send_signal(signal.SIGINT)
    proc.send_signal(signal.SIGHUP)
    out, _ = proc.communicate(timeout=30)

    assert done.exists(), out                         # the slow step ran to completion
    assert not (web / "a.example.test").exists(), out # and the steps after it still ran
    assert proc.returncode == 1
