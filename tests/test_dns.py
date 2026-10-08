"""
Does public DNS resolve the site the way Let's Encrypt will resolve it? (Not this server's own
resolver, and A *and* AAAA.) And: certbot must not be run against DNS that is known to be broken.
"""
import http.server
import json
import threading

import pytest

import parker

# the failure that was reported: A lookups time out, the AAAA lookup is SERVFAIL
SERVFAIL = {"status": 2, "answers": [], "comment": "EDE(22): No Reachable Authority"}
NOERROR_EMPTY = {"status": 0, "answers": [], "comment": ""}
NXDOMAIN = {"status": 3, "answers": [], "comment": ""}


def ok_a(*ips):
    return {"status": 0, "answers": list(ips) or ["104.21.0.1"], "comment": ""}


def fake_resolver(monkeypatch, table):
    """table: {(name, type): result-or-None}. Anything not listed is "no resolver reachable"."""
    calls = []

    def doh_query(name, rtype, timeout=6):
        calls.append((name, rtype))
        return table.get((name, rtype))

    monkeypatch.setattr(parker, "doh_query", doh_query)
    return calls


# ------------------------------------------------------- the resolver client (real HTTP)

@pytest.fixture
def doh_servers(monkeypatch):
    """Spin up local servers standing in for DoH endpoints; returns a function to register them."""
    started = []

    def make(body, status=200, content_type="application/dns-json"):
        class H(http.server.BaseHTTPRequestHandler):
            seen = []

            def do_GET(self):
                H.seen.append((self.path, self.headers.get("accept")))
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}/dns-query", H

    yield make
    for httpd in started:
        httpd.shutdown()


def use_endpoints(monkeypatch, *urls):
    monkeypatch.setattr(parker, "DOH_ENDPOINTS", tuple(urls))


def test_doh_query_returns_only_records_of_the_asked_type(doh_servers, monkeypatch):
    url, handler = doh_servers({"Status": 0, "Answer": [
        {"name": "www.a.example.", "type": 5, "data": "a.example."},        # CNAME in the chain
        {"name": "a.example.", "type": 1, "data": "104.21.0.1"},
        {"name": "a.example.", "type": 1, "data": "104.21.0.2"},
    ]})
    use_endpoints(monkeypatch, url)

    result = parker.doh_query("www.a.example", "A")

    assert result == {"status": 0, "answers": ["104.21.0.1", "104.21.0.2"], "comment": ""}
    path, accept = handler.seen[0]
    assert "name=www.a.example" in path and "type=A" in path and accept == "application/dns-json"


def test_doh_query_reports_servfail_with_the_resolvers_explanation(doh_servers, monkeypatch):
    url, _ = doh_servers({"Status": 2, "Comment": ["EDE(22): No Reachable Authority", "at delegation"]})
    use_endpoints(monkeypatch, url)
    result = parker.doh_query("skylet.in", "AAAA")
    assert result["status"] == 2 and result["answers"] == []
    assert "No Reachable Authority" in result["comment"] and "at delegation" in result["comment"]


def test_doh_query_falls_back_to_the_next_resolver(doh_servers, monkeypatch):
    bad, _ = doh_servers(b"<html>not json</html>", content_type="text/html")
    no_status, _ = doh_servers({"unexpected": True})
    good, _ = doh_servers({"Status": 0, "Answer": [{"type": 1, "data": "1.2.3.4"}]})
    use_endpoints(monkeypatch, "http://127.0.0.1:1/dns-query", bad, no_status, good)      # first one refuses connections

    assert parker.doh_query("a.example", "A")["answers"] == ["1.2.3.4"]


def test_doh_query_returns_none_when_no_resolver_can_be_reached(monkeypatch):
    use_endpoints(monkeypatch, "http://127.0.0.1:1/dns-query", "http://127.0.0.1:2/dns-query")
    assert parker.doh_query("a.example", "A") is None


# --------------------------------------------------------- would Let's Encrypt resolve it?

def test_a_healthy_name_is_ok(monkeypatch):
    fake_resolver(monkeypatch, {("a.example", "A"): ok_a(), ("a.example", "AAAA"): NOERROR_EMPTY})
    assert parker.dns_health("a.example") == ("ok", None)


def test_an_ipv6_only_name_is_ok(monkeypatch):
    fake_resolver(monkeypatch, {("a.example", "A"): NOERROR_EMPTY,
                                ("a.example", "AAAA"): {"status": 0, "answers": ["2606:4700::1"], "comment": ""}})
    assert parker.dns_health("a.example")[0] == "ok"


def test_the_reported_case_a_ok_but_aaaa_servfail_is_bad(monkeypatch):
    """An AAAA SERVFAIL fails validation even though the A record resolves."""
    fake_resolver(monkeypatch, {("skylet.in", "A"): ok_a(), ("skylet.in", "AAAA"): SERVFAIL})
    state, advice = parker.dns_health("skylet.in")
    assert state == "bad"
    assert "SERVFAIL looking up AAAA" in advice and "No Reachable Authority" in advice
    assert "nameservers at your registrar" in advice and "DNSSEC" in advice and "DS" in advice


def test_servfail_on_both_lookups_names_both(monkeypatch):
    fake_resolver(monkeypatch, {("skylet.in", "A"): SERVFAIL, ("skylet.in", "AAAA"): SERVFAIL})
    state, advice = parker.dns_health("skylet.in")
    assert state == "bad" and "looking up A (" in advice and "looking up AAAA (" in advice


def test_a_missing_record_such_as_www_is_bad_and_says_so(monkeypatch):
    fake_resolver(monkeypatch, {("www.a.example", "A"): NXDOMAIN, ("www.a.example", "AAAA"): NXDOMAIN})
    state, advice = parker.dns_health("www.a.example")
    assert state == "bad" and "NXDOMAIN" in advice and "including www" in advice


def test_a_name_with_no_addresses_is_bad(monkeypatch):
    fake_resolver(monkeypatch, {("a.example", "A"): NOERROR_EMPTY, ("a.example", "AAAA"): NOERROR_EMPTY})
    state, advice = parker.dns_health("a.example")
    assert state == "bad" and "no A or AAAA record" in advice


def test_nothing_can_be_concluded_when_no_resolver_is_reachable(monkeypatch):
    fake_resolver(monkeypatch, {})
    assert parker.dns_health("a.example") == ("unknown", None)


def test_one_reachable_answer_is_enough_to_judge(monkeypatch):
    fake_resolver(monkeypatch, {("a.example", "A"): ok_a()})                 # the AAAA question got no answer
    assert parker.dns_health("a.example")[0] == "ok"
    fake_resolver(monkeypatch, {("a.example", "AAAA"): SERVFAIL})
    assert parker.dns_health("a.example")[0] == "bad"


# ----------------------------------------------------------- nameserver delegation

NS = ["amara.ns.cloudflare.com", "bart.ns.cloudflare.com"]


def ns_result(names, status=0):
    return {"status": status, "answers": [n + "." for n in names], "comment": ""}


def test_delegation_matching_cloudflare_is_fine(monkeypatch):
    fake_resolver(monkeypatch, {("skylet.in", "NS"): ns_result(["AMARA.ns.cloudflare.com", "bart.ns.cloudflare.com"])})
    assert parker.delegation_problem("skylet.in", NS) is None


def test_delegation_still_at_the_old_host_is_reported_with_both_lists(monkeypatch):
    fake_resolver(monkeypatch, {("skylet.in", "NS"): ns_result(["ns1.oldhost.example", "ns2.oldhost.example"])})
    msg = parker.delegation_problem("skylet.in", NS)
    assert "ns1.oldhost.example" in msg and "amara.ns.cloudflare.com" in msg and "registrar" in msg


def test_delegation_with_no_nameservers_at_all(monkeypatch):
    fake_resolver(monkeypatch, {("skylet.in", "NS"): ns_result([], status=2)})
    assert "no nameservers" in parker.delegation_problem("skylet.in", NS)


def test_delegation_cannot_be_checked_without_a_resolver(monkeypatch):
    fake_resolver(monkeypatch, {})
    assert parker.delegation_problem("skylet.in", NS) is None


# ------------------------------------------------------------------- waiting for DNS

def patch_clock(monkeypatch):
    now = {"t": 1000.0}
    sleeps = []
    monkeypatch.setattr(parker.time, "time", lambda: now["t"])
    monkeypatch.setattr(parker.time, "sleep", lambda s: (sleeps.append(s), now.__setitem__("t", now["t"] + s)))
    return sleeps


def test_wait_polls_until_the_name_is_healthy(monkeypatch):
    sleeps = patch_clock(monkeypatch)
    monkeypatch.setattr(parker, "DRY_RUN", False)
    answers = iter([("bad", "not yet"), ("bad", "not yet"), ("ok", None)])
    monkeypatch.setattr(parker, "dns_health", lambda name: next(answers))

    assert parker.wait_for_dns(["a.example"], timeout=60, interval=5) == []
    assert sleeps == [5, 5]


def test_wait_gives_up_after_the_timeout_and_reports_the_public_problem(monkeypatch, capsys):
    patch_clock(monkeypatch)
    monkeypatch.setattr(parker, "DRY_RUN", False)
    monkeypatch.setattr(parker, "dns_health", lambda name: ("bad", f"public DNS cannot resolve {name}: SERVFAIL"))

    result = parker.wait_for_dns(["a.example", "www.a.example"], timeout=20, interval=5)

    assert sorted(e["name"] for e in result) == ["a.example", "www.a.example"]
    assert all(e["public"] is True and "SERVFAIL" in e["advice"] for e in result)
    assert "public DNS cannot resolve a.example" in capsys.readouterr().out


def test_each_name_is_judged_on_its_own(monkeypatch):
    patch_clock(monkeypatch)
    monkeypatch.setattr(parker, "DRY_RUN", False)
    monkeypatch.setattr(parker, "dns_health", lambda n: ("ok", None) if n == "a.example" else ("bad", "no www"))
    result = parker.wait_for_dns(["a.example", "www.a.example"], timeout=0)
    assert [e["name"] for e in result] == ["www.a.example"]


def test_timeout_zero_checks_exactly_once(monkeypatch):
    sleeps = patch_clock(monkeypatch)
    monkeypatch.setattr(parker, "DRY_RUN", False)
    monkeypatch.setattr(parker, "dns_health", lambda n: ("bad", "x"))
    assert len(parker.wait_for_dns(["a.example"], timeout=0)) == 1
    assert sleeps == []


def test_when_no_public_resolver_can_be_asked_the_local_one_decides_but_is_not_conclusive(monkeypatch):
    patch_clock(monkeypatch)
    monkeypatch.setattr(parker, "DRY_RUN", False)
    monkeypatch.setattr(parker, "dns_health", lambda n: ("unknown", None))

    monkeypatch.setattr(parker, "local_resolves", lambda n: True)
    assert parker.wait_for_dns(["a.example"], timeout=0) == []

    monkeypatch.setattr(parker, "local_resolves", lambda n: False)
    result = parker.wait_for_dns(["a.example"], timeout=0)
    assert len(result) == 1 and result[0]["public"] is False          # a weak signal: must not block certbot


def test_a_dry_run_asks_nobody(monkeypatch, capsys):
    monkeypatch.setattr(parker, "DRY_RUN", True)
    monkeypatch.setattr(parker, "doh_query", lambda *a, **k: pytest.fail("a dry run must not query DNS"))
    assert parker.wait_for_dns(["a.example"]) == []
    assert "Would check" in capsys.readouterr().out


# ----------------------------- the run: do not spend Let's Encrypt attempts on broken DNS

ARGS = ["--yes", "--domain", "skylet.in", "--www", "--type", "node", "--port", "3001", "--no-dns"]
BROKEN = {"name": "skylet.in", "public": True,
          "advice": "public DNS cannot resolve skylet.in: SERVFAIL looking up AAAA (EDE(22): No Reachable Authority). "
                    "SERVFAIL means the domain's nameservers are not answering correctly"}


def certbot_calls(env):
    return [c for c in env.commands if c[:2] == ["certbot", "run"]]


def test_certbot_is_not_run_when_public_dns_is_broken(env, capsys):
    env.dns_unresolved = [BROKEN]
    env.run_main(*ARGS)
    out = capsys.readouterr().out

    assert certbot_calls(env) == []                                   # not one attempt spent
    assert (env.available / "skylet.in.conf").exists()                # the site itself is still set up (HTTP)
    assert "SSL was NOT attempted: public DNS cannot resolve this site yet" in out
    assert "SERVFAIL looking up AAAA" in out
    assert "dig NS skylet.in +short" in out and "dig @1.1.1.1 skylet.in A / AAAA" in out
    assert "certbot certonly --dry-run" in out and "sudo certbot run" in out
    assert "Setup Completed With Warnings" in out


def test_certbot_still_runs_when_only_the_local_resolver_complained(env):
    """No public resolver could be asked: that is not evidence, so do not block SSL."""
    env.dns_unresolved = [{"name": "skylet.in", "public": False, "advice": "does not resolve from this server"}]
    env.run_main(*ARGS)
    assert len(certbot_calls(env)) == 1


def test_certbot_runs_normally_when_dns_is_fine(env):
    env.run_main(*ARGS)
    assert len(certbot_calls(env)) == 1


def test_the_nameserver_mismatch_is_part_of_the_explanation(env, monkeypatch, capsys):
    env.enable_cloudflare()
    env.dns_unresolved = [BROKEN]
    monkeypatch.setattr(parker, "delegation_problem",
                        lambda root, ns: f"the registrar's nameservers for {root} are ns1.oldhost.example, but Cloudflare expects {', '.join(ns)}.")
    env.run_main("--yes", "--domain", "skylet.in", "--www", "--type", "node", "--port", "3001")
    out = capsys.readouterr().out
    assert "the registrar's nameservers for skylet.in are ns1.oldhost.example" in out
    assert "ns1.fake" in out                                          # what Cloudflare assigned (from the zone)
    assert certbot_calls(env) == []


def test_broken_dns_never_downgrades_a_site_that_already_has_a_certificate(env, capsys):
    (env.live / "skylet.in").mkdir()
    env.dns_unresolved = [BROKEN]
    with pytest.raises(SystemExit) as exc:
        env.run_main(*ARGS, "--force")
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "already had a certificate" in out and "SERVFAIL looking up AAAA" in out
    assert not (env.available / "skylet.in.conf").exists() or "ROLLING BACK" in out


def test_a_dns_check_is_made_even_when_parker_did_not_create_the_dns(env):
    """With --no-dns the user manages DNS elsewhere; broken DNS is just as fatal for validation."""
    asked = []
    import parker as p
    env.mp.setattr(p, "wait_for_dns", lambda domains, timeout=None, interval=None: asked.append(timeout) or [])
    env.run_main(*ARGS)
    assert asked == [0]                                               # one check, no waiting


def test_when_parker_created_the_dns_it_waits_for_propagation(env):
    asked = []
    env.enable_cloudflare()
    env.mp.setattr(parker, "wait_for_dns", lambda domains, timeout=None, interval=None: asked.append(timeout) or [])
    env.run_main("--yes", "--domain", "skylet.in", "--www", "--type", "node", "--port", "3001")
    assert asked == [None]                                            # default wait


# ------------------------------------------------ the Cloudflare zone's own state

def test_a_pending_zone_is_called_out_with_the_nameservers_to_set(env, capsys):
    env.enable_cloudflare()
    env.cf.zone = {"id": "z1", "name_servers": ["amara.ns.cloudflare.com", "bart.ns.cloudflare.com"], "status": "pending"}
    env.run_main("--yes", "--domain", "skylet.in", "--www", "--type", "node", "--port", "3001")
    out = capsys.readouterr().out
    assert "reports the zone skylet.in as 'pending'" in out
    assert "amara.ns.cloudflare.com" in out and "not served publicly until the zone is active" in out


def test_an_active_zone_has_no_such_warning(env, capsys):
    env.enable_cloudflare()
    env.cf.zone = {"id": "z1", "name_servers": ["a.ns.cloudflare.com"], "status": "active"}
    env.run_main("--yes", "--domain", "skylet.in", "--www", "--type", "node", "--port", "3001")
    assert "as 'pending'" not in capsys.readouterr().out


# -------------------------- new zone: verify the nameserver change the user says they made

NEW_ZONE_ANSWERS = ["skylet.in", "y", "1", "", "n", "y", ""]       # domain, create zone, type, dir, mail=no, go, "Press Enter"


def test_after_press_enter_the_registrar_delegation_is_verified(env, monkeypatch, capsys):
    env.enable_cloudflare(zone=None)
    seen = []
    monkeypatch.setattr(parker, "delegation_problem",
                        lambda root, ns: seen.append((root, list(ns))) or "the registrar's nameservers for skylet.in are ns1.oldhost.example, but Cloudflare expects ns1.new, ns2.new.")
    env.say(*NEW_ZONE_ANSWERS)
    env.run_main("--no-www")
    out = capsys.readouterr().out

    assert seen == [("skylet.in", ["ns1.new", "ns2.new"])]
    assert "ns1.oldhost.example" in out
    assert "Nameservers: the registrar's nameservers" in out and "cannot resolve the site" in out


def test_a_correct_delegation_adds_no_warning(env, monkeypatch, capsys):
    env.enable_cloudflare(zone=None)
    monkeypatch.setattr(parker, "delegation_problem", lambda root, ns: None)
    env.say(*NEW_ZONE_ANSWERS)
    env.run_main("--no-www")
    assert "Nameservers:" not in capsys.readouterr().out
