import shutil
import socket
import subprocess

import pytest

import parker

def _host_has_ipv6():
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM):
            return True
    except OSError:
        return False


HOST_HAS_IPV6 = _host_has_ipv6()
DOMAINS = ["example.test", "www.example.test"]
ROOT = "/srv/web/example.test"
PUBLIC = f"{ROOT}/public_html"


def render(project_type, **kw):
    kw.setdefault("ipv6", False)
    kw.setdefault("security_headers", True)
    return parker.generate_nginx_config(
        domains=DOMAINS,
        project_type=project_type,
        project_root=ROOT,
        public_html=None if project_type == 4 else PUBLIC,
        **kw,
    )


@pytest.mark.parametrize("project_type,port", [(1, None), (2, None), (3, None), (3, 5000), (4, 3000)])
def test_every_type_serves_acme_challenge_from_the_project_directory(project_type, port):
    conf = render(project_type, port=port)

    assert "location ^~ /.well-known/acme-challenge/ {" in conf
    assert f"root {ROOT};" in conf.split("location ^~ /.well-known/acme-challenge/")[1].split("}")[0]
    assert "/var/www" not in conf
    assert "server_name example.test www.example.test;" in conf
    assert conf.startswith("# Managed by Parker\n# Project: " + ROOT)


def test_php_type():
    conf = render(1)
    assert f"root {PUBLIC};" in conf
    assert "try_files $uri $uri/ /index.php?$query_string;" in conf
    assert f"include {parker.PHP_FPM_SNIPPET};" in conf
    assert "proxy_pass" not in conf


def test_static_type_without_api():
    conf = render(3)
    assert "try_files $uri /index.html;" in conf
    assert "proxy_pass" not in conf
    assert "location /assets/ {" in conf and "expires 1y;" in conf


def test_static_type_with_api_strips_prefix():
    conf = render(3, port=5000)
    assert "location /api/ {" in conf
    assert "proxy_pass http://127.0.0.1:5000/;" in conf


def test_node_type_proxies_everything_and_leaves_favicon_to_the_app():
    conf = render(4, port=3000)
    assert "location / {\n        proxy_pass http://127.0.0.1:3000;" in conf
    assert 'proxy_set_header Upgrade $http_upgrade;' in conf
    assert "proxy_read_timeout 300s;" in conf
    assert "public_html" not in conf and "index index.php" not in conf   # no document root
    assert "favicon.ico" not in conf and "robots.txt" not in conf
    assert "try_files $uri $uri/" not in conf


def test_gzip_is_enabled_for_all_types():
    for t, port in ((1, None), (3, None), (4, 3000)):
        assert "gzip on;" in render(t, port=port)


def test_security_headers_only_on_nginx_served_sites_and_toggleable():
    assert "X-Content-Type-Options" in render(1)
    assert "X-Content-Type-Options" in render(3)
    assert "X-Content-Type-Options" not in render(4, port=3000)   # the app owns its headers
    assert "X-Content-Type-Options" not in render(1, security_headers=False)


def test_ipv6_listener_is_optional():
    assert "listen [::]:80;" not in render(1, ipv6=False)
    assert "listen [::]:80;" in render(1, ipv6=True)


@pytest.mark.parametrize("setting,expected", [("1", True), ("0", False)])
def test_ipv6_setting_overrides_detection(monkeypatch, setting, expected):
    monkeypatch.setattr(parker, "NGINX_IPV6", setting)
    assert parker.nginx_ipv6_enabled() is expected


def test_find_proxy_ports():
    assert parker.find_proxy_ports("proxy_pass http://127.0.0.1:3000;\nproxy_pass http://127.0.0.1:4000/;") == [3000, 4000]


# --- the real thing ------------------------------------------------------

@pytest.mark.skipif(shutil.which("nginx") is None, reason="nginx is not installed")
@pytest.mark.parametrize("project_type,port", [(1, None), (2, None), (3, None), (3, 5000), (4, 3000)])
@pytest.mark.parametrize("ipv6", [False, True])
def test_generated_config_passes_real_nginx(tmp_path, project_type, port, ipv6):
    if ipv6 and not HOST_HAS_IPV6:
        pytest.skip("this host cannot open IPv6 sockets, so nginx rejects [::] listeners")
    snippet = tmp_path / "php.conf"
    snippet.write_text('location ~ \\.php$ { return 200 "php"; }\n')

    conf = parker.generate_nginx_config(
        domains=DOMAINS,
        project_type=project_type,
        project_root=str(tmp_path / "proj"),
        public_html=str(tmp_path / "proj" / "public_html"),
        port=port,
        ipv6=ipv6,
        security_headers=True,
    ).replace("/var/log/nginx/", f"{tmp_path}/")
    # `nginx -t` really binds its listen sockets; an unprivileged user (CI) cannot take port 80.
    conf = conf.replace("listen 80;", "listen 28080;").replace("listen [::]:80;", "listen [::]:28080;")
    conf = conf.replace(parker.PHP_FPM_SNIPPET, str(snippet))

    (tmp_path / "site.conf").write_text(conf)
    main_conf = tmp_path / "nginx.conf"
    main_conf.write_text(
        f"pid {tmp_path}/nginx.pid;\n"
        "events {}\n"
        f"http {{ access_log off; include {tmp_path}/site.conf; }}\n"
    )

    result = subprocess.run(
        ["nginx", "-t", "-p", str(tmp_path), "-c", str(main_conf), "-e", str(tmp_path / "error.log")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_ipv6_auto_detection_matches_the_host(monkeypatch):
    monkeypatch.setattr(parker, "NGINX_IPV6", "auto")
    assert parker.nginx_ipv6_enabled() is HOST_HAS_IPV6
