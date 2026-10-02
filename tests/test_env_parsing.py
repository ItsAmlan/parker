import importlib.util
from pathlib import Path

import pytest

import parker

MAIN_PATH = Path(__file__).resolve().parent.parent / "parker-ui" / "main.py"

CASES = [
    ("KEY=value", ("KEY", "value")),
    ("  KEY = value  ", ("KEY", "value")),
    ("export KEY=value", ("KEY", "value")),
    ("KEY=", ("KEY", "")),
    ("KEY=  # only a comment", ("KEY", "")),
    ("KEY=#notacomment_start", ("KEY", "")),
    # The exact lines shipped in .env.example / the README
    ("MX_HOSTNAME=mail.example.com    # MX record target", ("MX_HOSTNAME", "mail.example.com")),
    ("WEBROOT=/bws/phoenix                  # Optional: base path", ("WEBROOT", "/bws/phoenix")),
    # '#' inside a value (no preceding whitespace) is part of the value
    ("PASS=abc#def", ("PASS", "abc#def")),
    ('KEY="quoted value"', ("KEY", "quoted value")),
    ("KEY='single # quoted'", ("KEY", "single # quoted")),
    ('KEY="quoted"   # trailing comment', ("KEY", "quoted")),
    ("KEY=a=b=c", ("KEY", "a=b=c")),
    ("# comment", None),
    ("", None),
    ("no equals sign", None),
    ("=novalue", None),
]


@pytest.fixture(scope="module")
def ui_main():
    pytest.importorskip("fastapi")
    spec = importlib.util.spec_from_file_location("parker_ui_main", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("line,expected", CASES)
def test_parker_parses_env_lines(line, expected):
    assert parker.parse_env_line(line) == expected


@pytest.mark.parametrize("line,expected", CASES)
def test_dashboard_parser_matches_parker(ui_main, line, expected):
    assert ui_main.parse_env_line(line) == expected


def test_load_env_reads_file_with_inline_comments(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# header\n"
        "PARKER_T_A=one    # comment\n"
        'PARKER_T_B="two words"\n'
        "export PARKER_T_C=three\n"
    )
    for name in ("PARKER_T_A", "PARKER_T_B", "PARKER_T_C"):
        monkeypatch.delenv(name, raising=False)

    parker.load_env(env_file)

    import os
    assert os.environ["PARKER_T_A"] == "one"
    assert os.environ["PARKER_T_B"] == "two words"
    assert os.environ["PARKER_T_C"] == "three"
    for name in ("PARKER_T_A", "PARKER_T_B", "PARKER_T_C"):
        monkeypatch.delenv(name)


def test_env_example_has_no_comment_leakage(monkeypatch):
    """Copying .env.example verbatim must not leave '# ...' text inside values."""
    example = MAIN_PATH.parent.parent / ".env.example"
    values = {}
    for line in example.read_text().splitlines():
        parsed = parker.parse_env_line(line)
        if parsed:
            values[parsed[0]] = parsed[1]

    assert values["MX_HOSTNAME"] == "mail.yourdomain.com"
    assert values["WEBROOT"] == "/var/www"
    assert all("#" not in v for v in values.values())
