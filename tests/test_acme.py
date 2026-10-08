"""
HTTP-01 / Let's Encrypt diagnosis: the pre-check that looks at the challenge the way Let's Encrypt
will (by name, through DNS and Cloudflare), the parsing of certbot's real error, and the retry
policy that must not burn Let's Encrypt's failed-validation limit.
"""
import http.server
import threading

import pytest

import parker

CERTBOT_404_AND_NXDOMAIN = """\
Saving debug log to /var/log/letsencrypt/letsencrypt.log
Requesting a certificate for skylet.in and www.skylet.in

Certbot failed to authenticate some domains (authenticator: webroot). The Certificate Authority reported these problems:
  Domain: skylet.in
  Type:   unauthorized
  Detail: 203.0.113.7: Invalid response from http://skylet.in/.well-known/acme-challenge/AbCdEf: 404

  Domain: www.skylet.in
  Type:   dns
  Detail: DNS problem: NXDOMAIN looking up A for www.skylet.in - check that a DNS
record exists for this domain

Hint: The Certificate Authority failed to download the temporary challenge files created by Certbot.
"""

CERTBOT_REDIRECT = """\
Certbot failed to authenticate some domains (authenticator: webroot). The Certificate Authority reported these problems:
  Domain: skylet.in
  Type:   unauthorized
  Detail: 104.21.0.1: Invalid response from https://skylet.in/.well-known/acme-challenge/xyz: 526
"""

CERTBOT_RATE_LIMIT = """\
An unexpected error occurred:
There were too many requests of a given type :: Error creating new order :: too many failed authorizations (5) for "skylet.in" in the last 1h0m0s, retry after 2026-10-08 12:00:00 UTC
"""


# ------------------------------------------------- reading a plain-HTTP answer

@pytest.mark.parametrize("status,headers,expected", [
    (301, {"Location": "https://skylet.in/.well-known/acme-challenge/t", "Server": "cloudflare"}, ["Always Use HTTPS", "Exempt", "DNS-only"]),
    (301, {"Location": "https://skylet.in/x"}, ["redirect rule"]),
    (302, {"Location": "http://elsewhere.example/x"}, ["redirects to http://elsewhere.example/x"]),
    (521, {"Server": "cloudflare"}, ["Cloudflare answered 521", "refused the connection", "THIS server"]),
    (526, {"cf-ray": "abc"}, ["invalid TLS certificate"]),
    (403, {"Server": "cloudflare"}, ["WAF", "namei -l"]),
    (403, {"Server": "nginx"}, ["nginx answered 403", "namei -l"]),
    (404, {"Server": "cloudflare"}, ["DIFFERENT server", "CNAME_TARGET"]),
    (404, {}, ["DIFFERENT server"]),
    (500, {}, ["server error"]),
    (521, {}, ["server error"]),                       # a 52x without Cloudflare is just a server error
    (418, {}, ["unexpected answer HTTP 418"]),
])
def test_explain_public_response(status, headers, expected):
    text = parker.explain_public_response(status, headers)
    for needle in expected:
        assert needle in text, text


def test_header_names_are_case_insensitive():
    assert "Always Use HTTPS" in parker.explain_public_response(301, {"LOCATION": "https://x/", "SERVER": "Cloudflare"})


# --------------------------------- the same, against a real HTTP server

class _Handler(http.server.BaseHTTPRequestHandler):
    script = {}                                     # path suffix -> (status, headers, body)

    def do_GET(self):
        status, headers, body = self.script.get("answer", (404, {}, b""))
        if callable(body):
            body = body(self.path)
        if status == "hang":
            import time
            time.sleep(2)
            status, headers, body = 200, {}, b""
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    handler = type("H", (_Handler,), {"script": {}})
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield type("S", (), {"host": f"127.0.0.1:{httpd.server_address[1]}", "script": handler.script})
    httpd.shutdown()


def test_public_probe_ok(server):
    server.script["answer"] = (200, {}, lambda path: path.rsplit("/", 1)[1].encode())
    assert parker.check_public_acme_path(server.host, "tok123") == (True, None)


def test_public_probe_sees_the_cloudflare_https_redirect(server):
    server.script["answer"] = (301, {"Location": "https://skylet.in/.well-known/acme-challenge/tok", "Server": "cloudflare"}, b"")
    ok, advice = parker.check_public_acme_path(server.host, "tok")
    assert ok is False
    assert "Always Use HTTPS" in advice and "Exempt /.well-known/acme-challenge/*" in advice


def test_public_probe_does_not_follow_redirects(server):
    """Following it would end on an HTTPS page that cannot answer yet and hide the real cause."""
    server.script["answer"] = (302, {"Location": "https://example.invalid/"}, b"")
    ok, advice = parker.check_public_acme_path(server.host, "tok")
    assert ok is False and "redirect" in advice.lower()


def test_public_probe_404_means_wrong_server(server):
    server.script["answer"] = (404, {}, b"not here")
    ok, advice = parker.check_public_acme_path(server.host, "tok")
    assert not ok and "DIFFERENT server" in advice


def test_public_probe_200_with_the_wrong_body_is_not_a_pass(server):
    """A catch-all page answering 200 for everything must not count as 'the challenge is served'."""
    server.script["answer"] = (200, {}, b"<html>parked page</html>")
    ok, advice = parker.check_public_acme_path(server.host, "tok")
    assert ok is False and advice


def test_public_probe_connection_refused(monkeypatch):
    ok, advice = parker.check_public_acme_path("127.0.0.1:1", "tok")
    assert ok is False and "could not connect" in advice and "port 80" in advice


def test_public_probe_timeout(server, monkeypatch):
    monkeypatch.setattr(parker, "ACME_PUBLIC_TIMEOUT", 0.3)
    server.script["answer"] = ("hang", {}, b"")
    ok, advice = parker.check_public_acme_path(server.host, "tok")
    assert ok is False and "timed out" in advice and "port 80" in advice


def test_public_probe_name_that_does_not_resolve():
    ok, advice = parker.check_public_acme_path("no-such-host.invalid", "tok")
    assert ok is False
    assert "does not resolve" in advice or "could not connect" in advice      # resolver-dependent wording


# -------------------------------------------- the whole pre-check, per hostname

class _Resp:
    def __init__(self, status, text="", headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}


def fake_local_nginx(monkeypatch, answers):
    """answers: {Host header -> status}. 200 returns the requested token like a working nginx."""
    class Session:
        trust_env = False

        def get(self, url, headers=None, **kw):
            status = answers.get((headers or {}).get("Host"), 404)
            return _Resp(status, url.rsplit("/", 1)[1] if status == 200 else "")

    monkeypatch.setattr(parker.requests, "Session", Session)
    monkeypatch.setattr(parker.time, "sleep", lambda s: None)


def test_precheck_reports_nothing_when_local_and_public_are_fine(tmp_path, monkeypatch):
    (tmp_path / ".well-known" / "acme-challenge").mkdir(parents=True)
    fake_local_nginx(monkeypatch, {"a.example": 200, "www.a.example": 200})
    monkeypatch.setattr(parker, "check_public_acme_path", lambda d, t: (True, None))

    assert parker.verify_acme_challenge_path(["a.example", "www.a.example"], str(tmp_path)) == []
    assert list((tmp_path / ".well-known" / "acme-challenge").iterdir()) == []      # probe file removed


def test_precheck_blames_this_servers_nginx_when_the_local_probe_fails(tmp_path, monkeypatch):
    (tmp_path / ".well-known" / "acme-challenge").mkdir(parents=True)
    fake_local_nginx(monkeypatch, {"a.example": 404})
    called = []
    monkeypatch.setattr(parker, "check_public_acme_path", lambda d, t: called.append(d) or (True, None))

    problems = parker.verify_acme_challenge_path(["a.example"], str(tmp_path))

    assert len(problems) == 1 and problems[0][0] == "a.example"
    assert "THIS server" in problems[0][1] and "HTTP 404" in problems[0][1] and "namei" in problems[0][1]
    assert called == []                                  # no point asking the internet when local is broken


def test_precheck_reports_each_hostname_separately(tmp_path, monkeypatch):
    (tmp_path / ".well-known" / "acme-challenge").mkdir(parents=True)
    fake_local_nginx(monkeypatch, {"a.example": 200, "www.a.example": 200})
    monkeypatch.setattr(
        parker, "check_public_acme_path",
        lambda d, t: (True, None) if d == "a.example" else (False, "www.a.example does not resolve (yet)"),
    )
    problems = parker.verify_acme_challenge_path(["a.example", "www.a.example"], str(tmp_path))
    assert problems == [("www.a.example", "www.a.example does not resolve (yet)")]


def test_precheck_in_a_dry_run_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(parker, "DRY_RUN", True)
    assert parker.verify_acme_challenge_path(["a.example"], str(tmp_path)) == []
    assert not (tmp_path / ".well-known").exists()


# ------------------------------------------------------- what Let's Encrypt said

def test_parse_certbot_failures_reads_every_domain_and_joins_wrapped_details():
    failures = parker.parse_certbot_failures(CERTBOT_404_AND_NXDOMAIN)
    assert [f["domain"] for f in failures] == ["skylet.in", "www.skylet.in"]
    assert failures[0]["type"] == "unauthorized" and "404" in failures[0]["detail"]
    assert failures[1]["type"] == "dns"
    assert failures[1]["detail"].endswith("record exists for this domain")        # wrapped line joined


def test_parse_certbot_failures_recognises_a_rate_limit():
    failures = parker.parse_certbot_failures(CERTBOT_RATE_LIMIT)
    assert failures and failures[0]["type"] == "ratelimited" and "too many failed authorizations" in failures[0]["detail"]


@pytest.mark.parametrize("text", ["", None, "Congratulations! Your certificate...", "some unrelated error"])
def test_parse_certbot_failures_finds_nothing_in_other_output(text):
    assert parker.parse_certbot_failures(text) == []


@pytest.mark.parametrize("failure,needle", [
    ({"domain": "x", "type": "dns", "detail": "DNS problem: NXDOMAIN looking up A for x"}, "no DNS record"),
    ({"domain": "x", "type": "connection", "detail": "1.2.3.4: Timeout during connect (likely firewall problem)"}, "port 80"),
    ({"domain": "x", "type": "unauthorized", "detail": "1.2.3.4: Invalid response from http://x/.well-known/acme-challenge/t: 404"}, "DIFFERENT server"),
    ({"domain": "x", "type": "unauthorized", "detail": "1.2.3.4: Invalid response from https://x/.well-known/acme-challenge/t: 301"}, "Always Use HTTPS"),
    ({"domain": "x", "type": "unauthorized", "detail": "1.2.3.4: Invalid response from http://x/.well-known/acme-challenge/t: 403"}, "forbidden"),
    ({"domain": "x", "type": "caa", "detail": "CAA record for x prevents issuance"}, "CAA"),
    ({"domain": "(account)", "type": "ratelimited", "detail": "too many failed authorizations (5)"}, "rate limiting"),
])
def test_explain_acme_detail(failure, needle):
    assert needle in parker.explain_acme_detail(failure)


def test_explain_acme_detail_says_nothing_rather_than_guessing():
    assert parker.explain_acme_detail({"domain": "x", "type": "serverInternal", "detail": "boom"}) is None


# ------------------------------------------------- retry policy (the rate limit)

@pytest.mark.parametrize("failures,expected", [
    ([], True),                                                                    # nothing parsed: unknown, retry
    ([{"domain": "a", "type": "dns", "detail": "NXDOMAIN"}], True),                # DNS still propagating
    ([{"domain": "a", "type": "dns", "detail": "x"}, {"domain": "b", "type": "dns", "detail": "SERVFAIL"}], True),
    ([{"domain": "a", "type": "unauthorized", "detail": "Invalid response ...: 404"}], False),
    ([{"domain": "a", "type": "unauthorized", "detail": "Invalid response ...: 301"}], False),
    ([{"domain": "a", "type": "connection", "detail": "Timeout during connect"}], False),
    ([{"domain": "a", "type": "dns", "detail": "NXDOMAIN"}, {"domain": "b", "type": "unauthorized", "detail": "404"}], False),
    ([{"domain": "(account)", "type": "ratelimited", "detail": "too many failed authorizations"}], False),
])
def test_is_retryable(failures, expected):
    assert parker.is_retryable(failures) is expected


def certbot_calls(env):
    return [c for c in env.commands if c[:2] == ["certbot", "run"]]


def test_a_deterministic_failure_is_tried_once_not_three_times(env, capsys):
    """Each failed validation counts against Let's Encrypt's ~5/hour limit: retrying locks the name out."""
    env.certbot_fails, env.certbot_output = True, CERTBOT_REDIRECT
    ok, failures = parker.setup_ssl(["skylet.in"], "/srv/p")
    assert ok is False and len(certbot_calls(env)) == 1
    assert failures[0]["domain"] == "skylet.in"
    assert "not retrying" in capsys.readouterr().out


def test_dns_propagation_failures_are_retried(env):
    env.certbot_fails = True
    env.certbot_output = "  Domain: www.a.example\n  Type:   dns\n  Detail: DNS problem: NXDOMAIN looking up A for www.a.example\n"
    ok, _ = parker.setup_ssl(["a.example", "www.a.example"], "/srv/p")
    assert not ok and len(certbot_calls(env)) == parker.SSL_ATTEMPTS


def test_unknown_failures_are_still_retried_like_before(env):
    env.certbot_fails, env.certbot_output = True, "certbot crashed: no traceback you can parse"
    parker.setup_ssl(["a.example"], "/srv/p")
    assert len(certbot_calls(env)) == parker.SSL_ATTEMPTS


def test_a_rate_limit_stops_immediately(env):
    env.certbot_fails, env.certbot_output = True, CERTBOT_RATE_LIMIT
    ok, failures = parker.setup_ssl(["a.example"], "/srv/p")
    assert not ok and len(certbot_calls(env)) == 1 and failures[0]["type"] == "ratelimited"


def test_success_returns_no_failures_after_one_call(env):
    assert parker.setup_ssl(["a.example"], "/srv/p") == (True, [])
    assert len(certbot_calls(env)) == 1


# ------------------------------------------------------ what the user is shown

ARGS = ["--yes", "--domain", "skylet.in", "--www", "--type", "node", "--port", "3001", "--no-dns"]


def test_the_summary_shows_what_lets_encrypt_said_with_a_hint_per_domain(env, capsys):
    env.certbot_fails, env.certbot_output = True, CERTBOT_404_AND_NXDOMAIN
    env.run_main(*ARGS)
    out = capsys.readouterr().out

    assert "Setup Completed With Warnings" in out
    assert "Let's Encrypt said:" in out
    assert "skylet.in: 203.0.113.7: Invalid response" in out and "DIFFERENT server" in out
    assert "www.skylet.in: DNS problem: NXDOMAIN" in out and "no DNS record is visible" in out
    assert "certbot certonly --dry-run" in out and "failed-validation limit" in out
    assert len(certbot_calls(env)) == 1                       # 404 is deterministic: one attempt


def test_the_summary_includes_the_precheck_findings(env, capsys):
    env.certbot_fails = True
    env.acme_problems = [("skylet.in", "HTTP is redirected to HTTPS (Cloudflare ('Always Use HTTPS' ...")]
    env.run_main(*ARGS)
    out = capsys.readouterr().out
    assert "Parker's pre-check found:" in out and "skylet.in: HTTP is redirected to HTTPS" in out


def test_certbot_failure_on_a_site_with_a_certificate_still_rolls_back_and_says_why(env, capsys):
    (env.live / "skylet.in").mkdir()
    env.certbot_fails, env.certbot_output = True, CERTBOT_REDIRECT
    with pytest.raises(SystemExit) as exc:
        env.run_main(*ARGS, "--force")
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "already had a certificate" in out and "Let's Encrypt said" in out and "526" in out


def test_the_dry_run_certbot_command_tests_authentication_only(env):
    cmd = parker.certbot_test_command(["skylet.in", "www.skylet.in"], "/web/skylet.in")
    assert cmd[:3] == ["certbot", "certonly", "--dry-run"]
    assert cmd[cmd.index("--webroot-path") + 1] == "/web/skylet.in"
    assert cmd.count("-d") == 2 and "-m" in cmd


# ----------------------------------------------------------------- run_capture

def test_run_capture_returns_output_and_the_exit_code_and_still_shows_it(capsys):
    # The marker is built at run time, so only the program's OUTPUT (not the echoed command line) contains it.
    code, output = parker.run_capture(
        ["python3", "-c", "import sys; print('MARK' + str(40 + 2)); sys.stderr.write('ERR' + str(7) + '\\n'); sys.exit(3)"])
    assert code == 3 and "MARK42" in output and "ERR7" in output          # stderr is captured too
    assert "MARK42" in capsys.readouterr().out                  # ...and streamed live, not swallowed


def test_run_capture_in_a_dry_run_runs_nothing(monkeypatch, capsys):
    monkeypatch.setattr(parker, "DRY_RUN", True)
    assert parker.run_capture(["definitely-not-a-command"]) == (0, "")
    assert "Would run" in capsys.readouterr().out
