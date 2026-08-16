"""Startup guards: the server refuses a configuration that would not apply.

AUTH_TOKEN, GIT_REPO_URL and the enforcement profile are all resolved at import
time, so a bad value has to fail loudly there rather than degrade silently later.
"""
import secrets
import sys
import pytest
import config


# ----------------- Startup: AUTH_TOKEN must be a real secret -----------------
# ensure_auth_token reads the module-level config.AUTH_TOKEN, not os.environ, so
# these patch the attribute. Dropping `pytest` from sys.modules disables the
# test-only dummy-token fallback and exercises the path a real server takes;
# monkeypatch restores both afterwards.
def _token_verdict(monkeypatch, token):
    monkeypatch.setattr(config, "AUTH_TOKEN", token)
    # raising=False: a test may call this twice, and the entry is only restored
    # at teardown, so the second removal would otherwise be a KeyError.
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    return config.ensure_auth_token()


# Spelled out rather than read from config._REJECTED_TOKENS: parametrizing over
# the set under test would turn an emptied list into a silent skip instead of a
# failure. These are the values this repo actually ships.
_SHIPPED_PLACEHOLDERS = [
    "your-secure-bearer-token",     # .env.example
    "a_very_long_secure_password",  # INSTALL.md
    "YOUR_AUTH_TOKEN",              # README.md / INSTALL.md client examples
    "default-token",
    "changeme",
]


@pytest.mark.parametrize("token", _SHIPPED_PLACEHOLDERS)
def test_ensure_auth_token_rejects_shipped_placeholders(monkeypatch, token):
    """A placeholder from .env.example or the docs is public in every clone, so
    booting a network-facing server on one must be impossible."""
    with pytest.raises(SystemExit):
        _token_verdict(monkeypatch, token)
    with pytest.raises(SystemExit):  # stray whitespace/case from a pasted .env
        _token_verdict(monkeypatch, f"  {token.upper()} ")


def test_rejection_list_covers_every_placeholder_in_the_docs():
    """Guards the list itself: each shipped placeholder must be on it, so the
    parametrized test above cannot pass for the wrong reason (e.g. a value that
    only fails the length check)."""
    for token in _SHIPPED_PLACEHOLDERS:
        assert token.lower() in config._REJECTED_TOKENS


def test_ensure_auth_token_rejects_non_ascii(monkeypatch):
    """A non-ASCII token would start the server but reject EVERY request: the
    bearer check compares via secrets.compare_digest, which raises TypeError on
    non-ASCII and is caught there as a plain mismatch. Refusing to start is the
    louder failure, so assert the broken comparison too — that is the reason."""
    token = "süper-secret-tökchen-abc"
    with pytest.raises(SystemExit):
        _token_verdict(monkeypatch, token)
    import server
    monkeypatch.setattr(config, "AUTH_TOKEN", token)
    assert server._token_matches(f"Bearer {token}") is False


def test_ensure_auth_token_enforces_minimum_length(monkeypatch):
    with pytest.raises(SystemExit):
        _token_verdict(monkeypatch, "a" * (config.MIN_AUTH_TOKEN_LEN - 1))
    assert _token_verdict(monkeypatch, "a" * config.MIN_AUTH_TOKEN_LEN)


def test_ensure_auth_token_accepts_a_generated_secret(monkeypatch):
    """The positive case, so the rejection list cannot quietly grow too broad."""
    token = secrets.token_hex(32)
    assert _token_verdict(monkeypatch, token) == token


def test_ensure_auth_token_rejects_empty(monkeypatch):
    with pytest.raises(SystemExit):
        _token_verdict(monkeypatch, "")


# ----------------- Startup: GIT_REPO_URL must not carry a credential ---------
def _repo_url_verdict(monkeypatch, url):
    monkeypatch.setattr(config, "GIT_REPO_URL", url)
    return config.ensure_git_repo_url()


# A credential in the URL reaches the logs verbatim: GitPython masks it in the
# command line it attaches to the error, but appends git's own stderr unmasked.
@pytest.mark.parametrize("url", [
    "https://TOKEN@github.com/u/vault.git",              # single-field PAT
    "https://user:TOKEN@github.com/u/vault.git",         # user:password
    "http://x-access-token:TOKEN@github.com/u/vault.git",  # GitHub App form
    "HTTPS://USER:TOKEN@github.com/u/vault.git",         # scheme case is irrelevant
    "ssh://git:PASSWORD@github.com/u/vault.git",         # ssh password (not identity)
])
def test_ensure_git_repo_url_rejects_embedded_credentials(monkeypatch, url):
    with pytest.raises(SystemExit):
        _repo_url_verdict(monkeypatch, url)


# The positive cases, so the rejection cannot quietly grow to cover the setups
# .env.example and INSTALL.md actually document.
@pytest.mark.parametrize("url", [
    "git@github.com:yourusername/personal-context.git",  # scp form, ADR-5 default
    "ssh://git@github.com/u/vault.git",                  # ssh URL: git@ is an identity
    "https://github.com/u/vault.git",                    # https via credential helper
    "/srv/mirror/vault.git",                             # local path remote
    None,                                                # unset → local-only mode
    "",
])
def test_ensure_git_repo_url_accepts_credential_free_remotes(monkeypatch, url):
    assert _repo_url_verdict(monkeypatch, url) == url


def test_ensure_git_repo_url_refuses_an_unparseable_url(monkeypatch):
    """A URL urlsplit cannot parse is refused rather than passed through unchecked."""
    with pytest.raises(SystemExit):
        _repo_url_verdict(monkeypatch, "https://user:TOKEN@[::1/vault.git")


# ----------------- Startup: the enforcement config must actually apply --------
def _enforcement_verdict(monkeypatch, profile="balanced", **rules):
    monkeypatch.setattr(config, "VAULT_ENFORCEMENT", profile)
    for var, value in rules.items():
        monkeypatch.setenv(var, value)
    return config.ensure_enforcement_config()


# `schema` is the only rule that rejects by default, and it is what keeps type_meta
# inside NOTE_TYPES — with it off, an arbitrary type is accepted AND the note is
# filed flat, because the folder is derived from the type. A configuration that
# disables it must not boot.
@pytest.mark.parametrize("rules", [
    {"VAULT_RULE_SCHEMA": "eror"},          # typo in the strength → emit() ignores it
    {"VAULT_RULE_PREAMBLE": "reject"},      # plausible-but-wrong strength
    {"VAULT_RULE_SCHEMA": ""},              # empty: falsy, so it is not even an override
    {"VAULT_RULE_SCHMEA": "off"},           # typo in the RULE NAME → silently no-op
    {"VAULT_RULE_SOURCES": "warn", "VAULT_RULE_CONFIDENCE": "wran"},  # one bad among good
])
def test_ensure_enforcement_config_refuses_a_rule_that_would_not_apply(monkeypatch, rules):
    with pytest.raises(SystemExit):
        _enforcement_verdict(monkeypatch, **rules)


@pytest.mark.parametrize("profile", ["strikt", "STRICT", "", "off"])
def test_ensure_enforcement_config_refuses_an_unknown_profile(monkeypatch, profile):
    with pytest.raises(SystemExit):
        _enforcement_verdict(monkeypatch, profile=profile)


# The positive cases, so the refusal cannot grow to cover documented setups.
@pytest.mark.parametrize("profile", ["strict", "balanced", "lenient"])
def test_ensure_enforcement_config_accepts_every_shipped_profile(monkeypatch, profile):
    _enforcement_verdict(monkeypatch, profile=profile)


@pytest.mark.parametrize("strength", ["error", "warn", "off", "ERROR"])
def test_ensure_enforcement_config_accepts_valid_rule_overrides(monkeypatch, strength):
    _enforcement_verdict(monkeypatch, VAULT_RULE_PREAMBLE=strength)


def test_rule_strength_honours_a_validated_override(monkeypatch):
    """The override wins over the profile — the behaviour the startup check protects."""
    monkeypatch.setattr(config, "VAULT_ENFORCEMENT", "lenient")
    monkeypatch.setenv("VAULT_RULE_PREAMBLE", "error")
    assert config.rule_strength("preamble") == "error"
    assert config.rule_strength("sources") == "off"   # still the lenient default
