# Contributing to cogitobase

Thanks for your interest in improving cogitobase! This is a self-hosted MCP server; contributions of all sizes are welcome.

## Getting started

```bash
git clone <your-fork-url>
cd cogitobase
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Run the test suite (no external services or API keys needed — clients degrade to `None` and tests mock or stub them):

```bash
pytest -q                       # unit tests
python tests/e2e_server.py      # server boot + auth middleware
python tests/e2e_search.py      # search pipeline (offline stub embedder)
python tests/e2e_streamable.py  # full Streamable HTTP handshake
```

CI runs the same suite on Python 3.12 for every push and PR.

## Ground rules

- **Match the surrounding style.** The codebase favors small focused modules, a decorator-based tool registry (`@register`), and reading `config.X` at call-time so tests can monkeypatch.
- **Add a tool with a decorator, not a branch.** New MCP tools register via `@register(name, description, schema)` in the appropriate module.
- **Security-relevant changes need a test.** Anything touching `security.py`, auth, the ingress middleware, or path handling must come with a test proving the guard.
- **Keep the server "dumb".** No autonomous routing or hidden LLM calls in the server; intelligence lives in the client.
- **Don't break reproducibility.** Dependencies are pinned in `requirements.txt`; bump deliberately, in its own commit.
- **Update the docs.** If you change behavior, config, or tools, update `README.md` / `INSTALL.md` / `ARCHITECTURE.md` accordingly. Architectural decisions go in `ARCHITECTURE.md` as an ADR.

## Pull requests

1. Branch from `main`.
2. Keep PRs focused; explain the "why" in the description.
3. Ensure `pytest -q` and the three e2e scripts pass.
4. For security issues, follow `SECURITY.md` instead of opening a public PR/issue.

## License

By contributing, you agree that your contributions are licensed under the project's **AGPL-3.0** license (see `LICENSE`).
