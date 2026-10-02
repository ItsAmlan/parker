"""
Shared fixtures. Every filesystem location parker.py touches is redirected into a
temp directory, and every external command (nginx, certbot, systemctl, opendkim,
doveadm, ...) plus the Cloudflare API is faked, so the whole suite runs without
root and without touching the machine. A separate test runs the generated configs
through the real `nginx -t` when nginx is installed.
"""
import builtins
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "parker-ui"))

import parker  # noqa: E402


DEFAULT_ZONE = {"id": "zone1", "name_servers": ["ns1.fake", "ns2.fake"]}


class FakeCloudflare:
    """Stands in for CloudflareManager and records every mutating call."""

    zone = dict(DEFAULT_ZONE)
    existing_records = {}          # name -> [record dicts]
    calls = []
    created_zone = None

    def __init__(self):
        pass

    def get_zone(self, root):
        return type(self).zone

    def create_zone(self, root):
        type(self).calls.append(("create_zone", root))
        zone = {"id": "zone-new", "name_servers": ["ns1.new", "ns2.new"]}
        zone["_rollback_task"] = parker.rollback_stack.add(
            lambda: type(self).calls.append(("delete_zone", "zone-new")),
            label="Delete zone",
        )
        return zone

    def list_dns_records(self, zone_id, name):
        return list(type(self).existing_records.get(name, []))

    def create_site_cname_record(self, **kw):
        type(self).calls.append(("cname", kw["record_name"]))

    def create_dns_record(self, zone_id, record_type, record_name, content, priority=None, **kw):
        type(self).calls.append((record_type, record_name, content))
        rid = f"id-{len(type(self).calls)}"
        parker.rollback_stack.add(
            lambda r=rid: type(self).calls.append(("undo", r)),
            label=f"Delete DNS record {record_name}",
        )
        return rid

    def delete_dns_record(self, zone_id, record_id):
        type(self).calls.append(("delete_record", record_id))
        return True


class Env:
    """Handle returned by the `env` fixture."""

    def __init__(self, root, monkeypatch):
        self.root = root
        self.mp = monkeypatch
        self.web = root / "web"
        self.available = root / "sites-available"
        self.enabled = root / "sites-enabled"
        self.live = root / "letsencrypt-live"
        self.log = root / "parker.log"
        for d in (self.web, self.available, self.enabled, self.live):
            d.mkdir()

        self.commands = []          # every fake run() call
        self.prompts = []           # every prompt text shown
        self.answers = []
        self.secrets = []
        self.nginx_test_fails = False
        self.certbot_fails = False
        self.tools = {"nginx", "certbot", "opendkim-genkey", "doveadm", "postmap"}

        snippet = root / "php.conf"
        snippet.write_text('location ~ \\.php$ { return 200 "php"; }\n')
        self.snippet = snippet

        mp = monkeypatch
        for name, value in {
            "BASE_DIR": str(self.web),
            "NGINX_SITES_AVAILABLE": str(self.available),
            "NGINX_SITES_ENABLED": str(self.enabled),
            "LETSENCRYPT_LIVE": str(self.live),
            "DOVECOT_USERS_FILE": str(root / "dovecot-users"),
            "POSTFIX_VIRTUAL_DOMAINS": str(root / "virtual_domains"),
            "POSTFIX_VIRTUAL_MAILBOX_MAPS": str(root / "virtual_mailbox_maps"),
            "VMAIL_BASE": str(root / "vhosts"),
            "OPENDKIM_DIR": str(root / "opendkim"),
            "PHP_FPM_SNIPPET": str(snippet),
            "DEFAULT_SSL_EMAIL": "ops@example.test",
            "CLOUDFLARE_API_TOKEN": "",
            "PROJECT_OWNER": "",
            "PARKER_LOG_FILE": str(self.log),
            "NGINX_IPV6": "0",
            "NGINX_SECURITY_HEADERS": True,
            "DRY_RUN": False,
            "NON_INTERACTIVE": False,
        }.items():
            mp.setattr(parker, name, value)

        mp.setattr(parker, "ensure_root", lambda: None)
        mp.setattr(parker, "command_exists", lambda c: c in self.tools)
        mp.setattr(parker, "run", self._run)
        mp.setattr(parker, "wait_for_dns", lambda *a, **k: True)
        mp.setattr(parker, "verify_acme_challenge_path", lambda *a, **k: True)
        mp.setattr(parker, "port_is_listening", lambda port: False)
        mp.setattr(parker, "reload_nginx_quietly", lambda: self.commands.append(["(rollback) reload nginx"]))
        mp.setattr(parker, "hash_password", lambda pw: "{BLF-CRYPT}fakehash")
        mp.setattr(parker.time, "sleep", lambda s: None)
        mp.setattr(builtins, "input", self._input)
        mp.setattr(parker.getpass, "getpass", self._getpass)

        FakeCloudflare.zone = dict(DEFAULT_ZONE)
        FakeCloudflare.existing_records = {}
        FakeCloudflare.calls = []
        mp.setattr(parker, "CloudflareManager", FakeCloudflare)
        self.cf = FakeCloudflare

    # --- faked externals -------------------------------------------------
    def _run(self, cmd, cwd=None, check=True):
        self.commands.append(list(cmd))

        if parker.DRY_RUN:
            return subprocess.CompletedProcess(cmd, 0)

        if cmd[0] == "opendkim-genkey":
            key_dir = Path(cmd[cmd.index("-D") + 1])
            selector = cmd[cmd.index("-s") + 1]
            key_dir.mkdir(parents=True, exist_ok=True)
            (key_dir / f"{selector}.private").write_text("PRIVATE")
            (key_dir / f"{selector}.txt").write_text(f'{selector}._domainkey IN TXT ( "v=DKIM1; k=rsa; p=ABC123" )')
            return subprocess.CompletedProcess(cmd, 0)

        failed = (
            (cmd[:2] == ["nginx", "-t"] and self.nginx_test_fails)
            or (cmd[0] == "certbot" and cmd[1] == "run" and self.certbot_fails)
        )
        if failed:
            if check:
                raise subprocess.CalledProcessError(1, cmd)
            return subprocess.CompletedProcess(cmd, 1)

        return subprocess.CompletedProcess(cmd, 0)

    def _input(self, prompt=""):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"Unexpected prompt (no scripted answer left): {prompt!r}")
        return self.answers.pop(0)

    def _getpass(self, prompt=""):
        self.prompts.append(prompt)
        if not self.secrets:
            raise AssertionError(f"Unexpected secret prompt: {prompt!r}")
        return self.secrets.pop(0)

    # --- helpers for tests ------------------------------------------------
    def say(self, *answers):
        self.answers.extend(answers)

    def commands_named(self, name):
        return [c for c in self.commands if c and c[0] == name]

    def conf(self, domain):
        return (self.available / f"{domain}.conf").read_text()

    def enable_cloudflare(self, zone="default"):
        self.mp.setattr(parker, "CLOUDFLARE_API_TOKEN", "token")
        if zone is None:
            self.cf.zone = None
        elif zone != "default":
            self.cf.zone = zone

    def run_main(self, *argv):
        parker.main(list(argv))


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    yield e
    parker.DRY_RUN = False
    parker.NON_INTERACTIVE = False
    parker.rollback_stack.tasks.clear()
    parker.rollback_stack.backups.clear()
