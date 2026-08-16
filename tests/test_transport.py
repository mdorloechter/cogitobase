"""The HTTP surface: auth gate, rate limiter, probes, metrics and the MCP route.

Exercised through the real Starlette app, so the middleware order that decides
whether an unauthenticated request ever reaches the limiter is under test too.
"""
import json as _json
import logging as _logging

import pytest

import config
import git_sync
import observability
import registry
from ratelimit import SlidingWindowRateLimiter
from conftest import PREAMBLE, call, _counter_value


# ----------------- registry sanity -----------------
def test_registry_has_expected_tools():
    names = {t.name for t in registry.all_tools()}
    for expected in ("search_vault", "write_note", "add_memory",
                     "list_skills", "get_skill", "write_skill", "get_core_context",
                     "capture_inbox"):
        assert expected in names


def test_every_required_parameter_is_described():
    """A required parameter with no description is a question the caller answers by guessing.

    The tool list is all a client has before its first call: a name and a type say what
    shape to send, not what the server will accept in it. Every required one, with no
    allowlist for the "obvious" cases — the alternative is a hand-kept exemption list,
    and the next undescribed parameter joins it by being forgotten rather than by being
    obvious. Optional parameters are left to judgement: omitting one is always valid, so
    the caller is never forced to guess.
    """
    missing = []
    for tool in registry.all_tools():
        schema = tool.inputSchema or {}
        props = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if not str((props.get(name) or {}).get("description", "")).strip():
                missing.append(f"{tool.name}.{name}")
    assert not missing, ("these required parameters carry no description, so a client has "
                        f"only their name and type to go on: {sorted(missing)}")


def test_every_tool_is_mentioned_in_a_skill_or_the_meta_prompt():
    """A tool nothing points at is one the model has to think of unprompted.

    The catalog and the meta prompt are the only text pushed into every session, and the
    seed skills are what a matching task pulls; a tool named in none of them is reachable
    only if the model goes looking through the raw tool list for it. Its own description
    does not count — that is what this test is checking is not the only mention.
    """
    import re
    import skills

    corpus = {p.name: p.read_text(encoding="utf-8")
              for p in sorted(config.SEED_SKILLS_DIR.glob("*.md"))}
    corpus["the meta prompt"] = skills.AGENTIC_META_PROMPT + skills.CORE_CONTEXT_TRIGGER
    unmentioned = [t.name for t in registry.all_tools()
                   if not any(re.search(rf"\b{t.name}\b", v) for v in corpus.values())]
    assert not unmentioned, ("these tools are named in no seed skill and not in the meta "
                             f"prompt, so nothing tells a session they exist: {sorted(unmentioned)}")


# ----------------- ingress rate limiter -----------------
def test_rate_limiter_allows_then_blocks():
    clock = [1000.0]
    rl = SlidingWindowRateLimiter(3, 60, clock=lambda: clock[0])
    assert [rl.check("a")[0] for _ in range(3)] == [True, True, True]
    allowed, retry = rl.check("a")
    assert allowed is False and retry > 0


def test_rate_limiter_window_resets():
    clock = [0.0]
    rl = SlidingWindowRateLimiter(1, 60, clock=lambda: clock[0])
    assert rl.check("a")[0] is True
    assert rl.check("a")[0] is False
    clock[0] += 61
    assert rl.check("a")[0] is True


def test_rate_limiter_keys_independent():
    clock = [0.0]
    rl = SlidingWindowRateLimiter(1, 60, clock=lambda: clock[0])
    assert rl.check("a")[0] is True
    assert rl.check("b")[0] is True   # different client unaffected


def test_rate_limiter_prune_drops_idle_keys():
    clock = [0.0]
    rl = SlidingWindowRateLimiter(5, 60, clock=lambda: clock[0])
    rl.check("a")
    clock[0] += 61
    rl.prune()
    assert "a" not in rl._hits


def test_unauthenticated_requests_never_reach_the_limiter(monkeypatch):
    """The 401 is returned before the limiter is consulted, so token guessing is not
    throttled here — the documented scope of this limiter (ratelimit.py, ADR 11,
    README §5) depends on that ordering. Asserted on the limiter's own state rather
    than on the status code, because a 401 alone cannot tell the two apart."""
    import server
    from starlette.testclient import TestClient
    monkeypatch.setattr(git_sync, "init_git_repo", lambda: None)
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    limiter = SlidingWindowRateLimiter(2, 60)
    monkeypatch.setattr(server, "_rate_limiter", limiter)
    with TestClient(server.starlette_app) as client:
        codes = {client.post("/mcp/", headers={"Authorization": "Bearer wrong"}).status_code
                 for _ in range(5)}
    assert codes == {401}, f"expected only 401s, got {codes}"
    assert not limiter._hits, \
        f"unauthenticated traffic was keyed into the limiter: {list(limiter._hits)}"


def test_authenticated_requests_are_throttled_by_token(monkeypatch):
    """The other half: what the limiter does cover is a client that holds a valid token
    and hammers it. The key is the hashed token, so it must never carry the raw one."""
    import server
    from starlette.testclient import TestClient
    monkeypatch.setattr(git_sync, "init_git_repo", lambda: None)
    monkeypatch.setattr(config, "RATE_LIMIT_ENABLED", True)
    limiter = SlidingWindowRateLimiter(2, 60)
    monkeypatch.setattr(server, "_rate_limiter", limiter)
    auth = {"Authorization": f"Bearer {config.AUTH_TOKEN}"}
    with TestClient(server.starlette_app) as client:
        codes = [client.post("/mcp/", headers=auth, json={}).status_code for _ in range(4)]
    assert 429 in codes, f"a hammered token was never throttled: {codes}"
    assert all(k.startswith("t:") for k in limiter._hits), list(limiter._hits)
    assert not any(config.AUTH_TOKEN in k for k in limiter._hits), "raw token used as key"


# ----------------- structured logging + request id -----------------
def test_json_formatter_emits_valid_json_with_request_id():
    observability.set_request_id("req-xyz")
    rec = _logging.makeLogRecord({"name": "t", "levelno": _logging.INFO,
                                  "levelname": "INFO", "msg": "hello"})
    rec.event = "demo"
    rec.tool = "write_note"
    line = observability.JSONFormatter().format(rec)
    obj = _json.loads(line)
    assert obj["msg"] == "hello"
    assert obj["request_id"] == "req-xyz"
    assert obj["event"] == "demo" and obj["tool"] == "write_note"


def test_request_id_default_is_dash():
    # Fresh context defaults to "-"
    observability.request_id_var.set("-")
    assert observability.get_request_id() == "-"


# ----------------- metrics registry -----------------
def test_counter_increments_with_labels():
    reg = observability.MetricsRegistry()
    c = reg.counter("things_total", "h", ("kind",))
    c.inc(("a",)); c.inc(("a",)); c.inc(("b",), 3)
    out = reg.render()
    assert 'things_total{kind="a"} 2' in out
    assert 'things_total{kind="b"} 3' in out


def test_gauge_sets_value():
    reg = observability.MetricsRegistry()
    g = reg.gauge("depth", "h")
    g.set(7)
    assert "depth 7" in reg.render()


def test_histogram_buckets_and_count():
    reg = observability.MetricsRegistry()
    h = reg.histogram("lat_seconds", "h", ("tool",))
    for v in (0.01, 0.2, 3.0):
        h.observe(v, ("x",))
    out = reg.render()
    assert 'lat_seconds_count{tool="x"} 3' in out
    assert 'lat_seconds_bucket{tool="x",le="+Inf"} 3' in out
    # 0.01 and 0.2 fall into the <=0.5 bucket, 3.0 does not
    assert 'lat_seconds_bucket{tool="x",le="0.5"} 2' in out


def test_render_is_prometheus_shaped():
    reg = observability.MetricsRegistry()
    reg.counter("c_total", "help text", ()).inc()
    out = reg.render()
    assert "# HELP c_total help text" in out
    assert "# TYPE c_total counter" in out


def test_label_escaping():
    reg = observability.MetricsRegistry()
    reg.counter("c_total", "h", ("k",)).inc(('a"b',))
    assert '\\"' in reg.render()


# ----------------- /metrics endpoint via the real app -----------------
def test_metrics_endpoint_requires_auth(monkeypatch):
    import server
    from starlette.testclient import TestClient
    monkeypatch.setattr(git_sync, "init_git_repo", lambda: None)
    with TestClient(server.starlette_app) as client:
        assert client.get("/metrics").status_code == 401
        ok = client.get("/metrics", headers={"Authorization": f"Bearer {config.AUTH_TOKEN}"})
        assert ok.status_code == 200
        assert "mcp_tool_calls_total" in ok.text or "# HELP" in ok.text
        # request id is echoed for correlation
        assert "x-request-id" in {k.lower() for k in ok.headers}


@pytest.mark.asyncio
async def test_tool_dispatch_records_metrics():
    # Assert a real +1 increment against the global (never-reset) registry, so the
    # test can't pass on residue from an earlier call.
    label = 'mcp_tool_calls_total{tool="list_notes",outcome="ok"}'
    before = _counter_value(observability.metrics.render(), label)
    await call("list_notes", {"vault": "brain"})
    after = _counter_value(observability.metrics.render(), label)
    assert after == before + 1
    assert 'mcp_tool_duration_seconds_count{tool="list_notes"}' in observability.metrics.render()


@pytest.mark.asyncio
async def test_list_notes_unfiltered():
    await call("write_note", {"vault": "brain", "filename": "person_a", "title": "A", "type_meta": "person", "tags": [], "content": PREAMBLE})
    await call("write_note", {"vault": "brain", "filename": "project_b", "title": "B", "type_meta": "project", "tags": [], "content": PREAMBLE})
    res = await call("list_notes", {"vault": "brain"})
    assert "person_a.md" in res[0].text
    assert "project_b.md" in res[0].text


@pytest.mark.asyncio
async def test_list_notes_filtered_by_type_meta():
    await call("write_note", {"vault": "brain", "filename": "person_c", "title": "C", "type_meta": "person", "tags": [], "content": PREAMBLE})
    await call("write_note", {"vault": "brain", "filename": "project_d", "title": "D", "type_meta": "project", "tags": [], "content": PREAMBLE})
    res = await call("list_notes", {"vault": "brain", "type_meta": "person"})
    assert "person_c.md" in res[0].text
    assert "project_d.md" not in res[0].text


@pytest.mark.asyncio
async def test_list_notes_filtered_by_context_type():
    """identity/standard map to None in TYPE_FOLDER (flat context-vault storage) —
    filtering list_notes by either must not raise, and must return only that
    vault's flat contents."""
    await call("write_note", {"vault": "context", "filename": "me", "title": "Me",
                               "type_meta": "identity", "tags": [], "content": PREAMBLE})
    res = await call("list_notes", {"vault": "context", "type_meta": "identity"})
    assert "me.md" in res[0].text


@pytest.mark.asyncio
async def test_list_notes_names_notes_so_read_note_resolves_them():
    """Every listed line must be a name read_note takes back — including when two
    notes share a basename, which bare basenames could neither distinguish nor
    resolve."""
    await call("write_note", {"vault": "brain", "filename": "roadmap", "title": "R",
                              "type_meta": "concept", "tags": [], "content": PREAMBLE})
    await call("write_note", {"vault": "brain", "filename": "roadmap", "title": "R",
                              "type_meta": "project", "tags": [], "content": PREAMBLE})
    listed = (await call("list_notes", {"vault": "brain"}))[0].text.splitlines()
    assert "brain-vault/concepts/roadmap.md" in listed
    assert "brain-vault/projects/roadmap.md" in listed
    for name in listed:
        assert "is ambiguous" not in (await call("read_note", {"filename": name}))[0].text


@pytest.mark.asyncio
async def test_list_notes_keeps_basenames_for_media(tmp_path):
    """Media files address no note, so they are listed by basename — there is no
    vault-qualified form for read_note to take back."""
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (config.MEDIA_DIR / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    res = await call("list_notes", {"vault": "media"})
    assert res[0].text.splitlines() == ["diagram.png"]


@pytest.mark.asyncio
async def test_list_notes_skips_a_symlinked_note(tmp_path):
    """A symlink planted in the vault (git materialises one from a 120000 blob,
    ADR 4) must not be listed — read_note refuses it, so listing it advertises a
    note that cannot be opened."""
    outside = tmp_path / "outside.md"
    outside.write_text("SECRET")
    config.BRAIN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        (config.BRAIN_DIR / "linked.md").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation not permitted in this environment")
    res = await call("list_notes", {"vault": "brain"})
    assert "linked.md" not in res[0].text


@pytest.mark.asyncio
async def test_rejected_tool_classified_as_rejected():
    # A schema-invalid write is a soft refusal → outcome="rejected", not "error".
    label = 'mcp_tool_calls_total{tool="write_note",outcome="rejected"}'
    before = _counter_value(observability.metrics.render(), label)
    await call("write_note", {"vault": "brain", "filename": "x", "title": "T",
                              "type_meta": "bogus", "tags": [], "content": PREAMBLE})
    assert _counter_value(observability.metrics.render(), label) == before + 1


@pytest.mark.asyncio
async def test_an_offline_dependency_is_not_counted_as_a_successful_call(monkeypatch):
    """The failure this label exists for. A search with no Qdrant behind it returns a
    text reply like any other, so counting it as `ok` made a broken deployment look
    perfectly healthy in /metrics — every call succeeding, nothing to alert on. It is
    also not `rejected`: the caller did nothing wrong and cannot fix it."""
    import clients
    monkeypatch.setattr(clients, "qdrant_db", None)
    unavailable = 'mcp_tool_calls_total{tool="search_vault",outcome="unavailable"}'
    ok = 'mcp_tool_calls_total{tool="search_vault",outcome="ok"}'
    before_u = _counter_value(observability.metrics.render(), unavailable)
    before_ok = _counter_value(observability.metrics.render(), ok)

    res = await call("search_vault", {"query": "anything"})
    assert "offline" in res[0].text

    after = observability.metrics.render()
    assert _counter_value(after, unavailable) == before_u + 1
    assert _counter_value(after, ok) == before_ok, "an outage must not count as a success"


@pytest.mark.asyncio
async def test_the_outcome_survives_a_reworded_refusal(monkeypatch):
    """The classification must not depend on the reply's PROSE.

    Deriving it that way — matching the text against a list of expected openings — makes
    every refusal message a metrics contract: rephrasing one, or writing a new refusal
    that opens differently, silently moves the call into `ok`. The wording is user-facing
    text, and it is edited for the reader, so the outcome is declared instead."""
    import capture
    monkeypatch.setattr(capture, "text",
                        lambda msg, outcome="ok": registry.text("Nope, empty.", outcome))
    label = 'mcp_tool_calls_total{tool="capture_inbox",outcome="rejected"}'
    before = _counter_value(observability.metrics.render(), label)

    res = await call("capture_inbox", {"note": "   "})
    assert res[0].text == "Nope, empty.", "the reworded text must be what reaches the client"
    assert _counter_value(observability.metrics.render(), label) == before + 1


@pytest.mark.asyncio
async def test_a_successful_call_is_still_counted_as_ok():
    """The labels are opt-in, so the change must not reclassify what already worked."""
    label = 'mcp_tool_calls_total{tool="list_skills",outcome="ok"}'
    before = _counter_value(observability.metrics.render(), label)
    await call("list_skills", {})
    assert _counter_value(observability.metrics.render(), label) == before + 1


# Handlers whose plain text() call is genuinely a SUCCESS: the reply is the answer, or
# the tool reports a state it was asked about. Listed by (module, line-content substring)
# so the completeness test below can tell them from a refusal that forgot its label.
_SUCCESS_REPLIES = {
    # vault.py — results, listings, note content, and write confirmations
    "text(msg)", "text(out)", "text(filepath.read_text(encoding=\"utf-8\"))",
    'text("\\n---\\n".join(results) if results else "No matches found.")',
    'text("Vault empty.")', 'text("\\n".join(files) if files else "No files.")',
    'text(f"Deleted {filepath.name}")',
    # upload_media: the file IS written and committed; a failed index is counted by
    # mcp_index_failures_total and repaired by reindex_vault, as in write_note.
    'text(f"Uploaded, but indexing failed: {filepath.name}. The file is saved "'
    ' f"but not searchable yet; check server logs or retry via reindex_vault.")',
    'text(f"Uploaded and indexed PDF successfully: {filepath.name}. You can link it using: [PDF](../media/{filepath.name})")',
    'text(f"Uploaded and indexed successfully. You can link it in markdown using: ![alt text](../media/{filepath.name})")',
    # memory.py — query answers and write confirmations
    'text(f"Fact remembered{_added_id(added)}.{note}")', 'text(f"Results:\\n{mem_text}")',
    'text(f"Memories:\\n{mem_text}")', 'text(str(res))', 'text("Updated.")', 'text("Deleted.")',
    "text(f\"[{res.get('id', memory_id)}] {res['memory']}\")",
    # skills.py — the catalog, the bootstrap, a body, a write confirmation
    'text("No skills defined yet." + note)', 'text("Available skills:\\n" + "\\n".join(lines) + note)',
    "text(await asyncio.to_thread(build_prompt))", 'text(header + skill["body"])',
    "text(f\"Saved skill '{arguments['name']}' (v{version}).\")",
    "text(f\"Deleted skill '{name}'.\")",
    # augment.py — the fetched content, and an empty result set (a valid answer)
    'text(f"{label}:\\n{cut}")', "text(report)", 'text("No results found.")',
    # git_sync.py — sync_status is ASKED for a state, so reporting one is the answer,
    # whichever state it is; the paused case is counted by mcp_git_sync_total. And a
    # resume with nothing to resume already holds the state the caller wanted: nothing
    # failed, and there is no other way to send it.
    'text("Git sync is not configured (no repo). Vault changes are local only.")',
    'text( "Git auto-sync is PAUSED after a merge conflict. Local work was parked on a "'
    ' "conflict-* branch. Resolve the divergence in the repo, then call resume_sync.")',
    'text("Git auto-sync is active.")',
    'text("Git auto-sync is not paused; nothing to resume.")',
    'text("Git auto-sync resumed. The next write (or the 15-minute cron) will sync.")',
}


def _tool_modules():
    """Every module whose `text()` is the registry's — the audit's subject set.

    Discovered rather than listed, because a hand-kept list silently omits the next tool
    module, and omission here does not fail: an unaudited module's refusals are simply
    never checked. The `from registry import … text …` is the criterion, not @register,
    since only the import proves a `text(...)` call in that file is THIS text() and not
    a local function that happens to share the name.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    modules = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.ImportFrom) and node.module == "registry"
                    and any(a.name == "text" for a in node.names)):
                modules.append(path.name)
                break
    return modules


@pytest.mark.asyncio
async def test_a_size_refusal_names_the_actual_size_and_the_limit(monkeypatch):
    """A bare "too large" leaves the caller unable to act: it cannot tell whether to
    shrink what it sent or ask the operator to raise a limit, and it cannot tell by how
    much it missed. Both numbers, in every tool that enforces MAX_FILE_SIZE_BYTES."""
    import config as cfg
    await call("write_note", {"vault": "brain", "filename": "big", "title": "Big",
                              "type_meta": "project", "tags": [],
                              "content": "## AI Summary\nfits\n\n# Big\n" + "x" * 200})
    monkeypatch.setattr(cfg, "MAX_FILE_SIZE_BYTES", 40)
    cases = [
        ("write_note", {"vault": "brain", "filename": "huge", "title": "H",
                        "type_meta": "project", "tags": [], "content": "y" * 300}, 300),
        ("append_to_note", {"filename": "big", "content": "z" * 500}, 500),
        ("read_note", {"filename": "big"}, None),
        ("write_skill", {"name": "long-one", "description": "d", "when_to_use": "w",
                         "body": "b" * 700}, 700),
    ]
    for tool, args, sent in cases:
        reply = (await call(tool, args))[0].text
        assert "40" in reply, f"{tool} does not name the limit it enforced: {reply}"
        assert "MAX_FILE_SIZE_BYTES" in reply, f"{tool} does not name the knob: {reply}"
        if sent is not None:
            assert str(sent) in reply, f"{tool} does not name the size it got: {reply}"


@pytest.mark.asyncio
async def test_a_missing_target_names_it_and_a_way_to_find_one(monkeypatch):
    """"Not found." is a dead end for a caller working from memory: it cannot tell a typo
    from something never written, and has nothing to try next. Every miss echoes the name
    it was given and names a tool that produces a real one."""
    import clients

    class FakeMem:
        def get(self, memory_id): return None
        def update(self, memory_id, fact): raise ValueError("no such id")
        def delete(self, memory_id): raise ValueError("no such id")
    monkeypatch.setattr(clients, "mem0_client", FakeMem())
    cases = [
        ("read_note", {"filename": "ghost.md"}, "ghost.md", "search_vault"),
        ("append_to_note", {"filename": "ghost.md", "content": "x"}, "ghost.md", "search_vault"),
        ("rename_note", {"old_filename": "ghost.md", "new_filename": "other.md"},
         "ghost.md", "search_vault"),
        ("delete_note", {"filename": "ghost.md"}, "ghost.md", "search_vault"),
        ("get_skill", {"name": "ghost-skill"}, "ghost-skill", "list_skills"),
        ("delete_skill", {"name": "ghost-skill"}, "ghost-skill", "list_skills"),
        ("get_memory", {"memory_id": "ghost-id"}, "ghost-id", "get_memories"),
        ("update_memory", {"memory_id": "ghost-id", "fact": "[Preference] x"},
         "ghost-id", "get_memories"),
        ("delete_memory", {"memory_id": "ghost-id"}, "ghost-id", "get_memories"),
    ]
    for tool, args, name, finder in cases:
        reply = (await call(tool, args))[0].text
        assert "not found" in reply.lower(), f"{tool} does not report a miss: {reply}"
        assert name in reply, f"{tool} does not echo the name it was given: {reply}"
        assert finder in reply, f"{tool} names no way to find a real one: {reply}"


@pytest.mark.asyncio
async def test_an_offline_dependency_says_what_it_could_not_do(monkeypatch):
    """A product name is not an answer. "Mem0 offline." does not say whether the fact was
    stored, and the caller never chose the store — so the reply names the blocked action,
    which is what decides whether to retry, work around it, or tell the user."""
    import clients
    monkeypatch.setattr(clients, "mem0_client", None)
    monkeypatch.setattr(clients, "qdrant_db", None)
    monkeypatch.setattr(clients, "embedder", None)
    cases = [
        ("add_memory", {"fact": "[Preference] x"}),
        ("search_memories", {"query": "q"}),
        ("get_memories", {}),
        ("get_memory", {"memory_id": "m"}),
        ("update_memory", {"memory_id": "m", "fact": "[Preference] x"}),
        ("delete_memory", {"memory_id": "m"}),
        ("search_vault", {"query": "q"}),
        ("reindex_vault", {}),
    ]
    for tool, args in cases:
        reply = (await call(tool, args))[0].text
        assert reply.lower().startswith("cannot "), (
            f"{tool} reports a dependency, not the action it blocked: {reply}")
        assert "offline" in reply.lower(), f"{tool} does not say the cause: {reply}"


def test_the_outcome_audit_covers_every_tool_module():
    """A search that finds nothing passes forever, so the discovery needs its own guard.

    Every module that registers tools has to be in the audited set. git_sync.py is the
    case that motivates this: it registers two tools and was missing from the audit,
    which cost nothing only because all of its replies happen to be successes today.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    audited = set(_tool_modules())
    # registry.py DEFINES text() and registers nothing; server.py only imports the
    # modules to trigger their decorators.
    registering = {p.name for p in sorted(root.glob("*.py"))
                   if p.name not in ("registry.py", "server.py")
                   and any(isinstance(n, ast.Call) and getattr(n.func, "id", None) == "register"
                           for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))))}
    assert registering <= audited, (
        "these modules register tools but their replies are never audited for an "
        f"outcome: {sorted(registering - audited)}")


def test_every_refusal_declares_its_outcome():
    """A refusal with no label is counted as a success, and nothing about the code says
    so — which is exactly how a whole class of them (offline dependencies, oversized
    uploads, unsupported media) came to be reported as healthy calls. Every text() in a
    tool module therefore has to either carry an outcome or be listed above as a genuine
    success, so adding a refusal without a label fails here rather than in a dashboard."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    missing = []
    for module in _tool_modules():
        source = (root / module).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "text"):
                continue
            # A second positional arg or an `outcome=` keyword is the label. `text(*pair)`
            # counts too — the outcome is chosen inside the helper being unpacked.
            if (len(node.args) > 1 or any(k.arg == "outcome" for k in node.keywords)
                    or any(isinstance(a, ast.Starred) for a in node.args)):
                continue
            snippet = ast.get_source_segment(source, node)
            if snippet and " ".join(snippet.split()) in {" ".join(s.split()) for s in _SUCCESS_REPLIES}:
                continue
            missing.append(f"{module}:{node.lineno}  {snippet}")
    assert not missing, (
        "these text() replies carry no outcome and are not listed as successes — label "
        "the refusals, or add a genuine success to _SUCCESS_REPLIES:\n" + "\n".join(missing))


# ----------------- health/readiness endpoints (unauthenticated probes) -----------------
def test_healthz_is_unauthenticated(monkeypatch):
    import server
    from starlette.testclient import TestClient
    monkeypatch.setattr(git_sync, "init_git_repo", lambda: None)
    with TestClient(server.starlette_app) as client:
        r = client.get("/healthz")          # no Authorization header
        assert r.status_code == 200
        assert r.text == "ok"


def test_body_cap_rejects_chunked_without_content_length(monkeypatch):
    """A chunked request (no Content-Length) that streams past the cap must still
    be rejected with 413 — the header check alone can't see chunked size."""
    import server
    from starlette.testclient import TestClient
    monkeypatch.setattr(git_sync, "init_git_repo", lambda: None)
    monkeypatch.setattr(config, "MAX_REQUEST_BODY_BYTES", 1000)
    auth = {"Authorization": f"Bearer {config.AUTH_TOKEN}"}
    with TestClient(server.starlette_app) as client:
        def oversized():
            yield b"x" * 5000  # generator body => requests sends no Content-Length
        r = client.post("/mcp", headers=auth, content=oversized())
        assert r.status_code == 413

        def small():
            yield b"y" * 50
        r2 = client.post("/mcp", headers=auth, content=small())
        assert r2.status_code != 413  # under the cap: passes the body gate


def test_readyz_reports_dependency_state(monkeypatch):
    import server
    import clients
    from starlette.testclient import TestClient
    monkeypatch.setattr(git_sync, "init_git_repo", lambda: None)

    class LiveQdrant:  # a reachable store answers the liveness ping
        def get_collections(self): return object()

    class DeadQdrant:  # a dead store raises on the ping
        def get_collections(self): raise ConnectionError("qdrant down")

    with TestClient(server.starlette_app) as client:
        monkeypatch.setattr(clients, "embedder", object())
        monkeypatch.setattr(clients, "qdrant_db", LiveQdrant())
        assert client.get("/readyz").status_code == 200
        # A store that died AFTER startup must flip to 503 (live probe, not stale handle).
        monkeypatch.setattr(clients, "qdrant_db", DeadQdrant())
        # Prevent lazy-reconnect from resurrecting it during the test.
        monkeypatch.setattr(clients, "connect_qdrant", lambda: None)
        assert client.get("/readyz").status_code == 503


def test_probe_ready_paths(monkeypatch):
    """Unit-level: probe_ready is false without an embedder, true on a live ping,
    and false (dropping the handle) when the ping fails."""
    import clients

    class LiveQdrant:
        def get_collections(self): return object()

    class DeadQdrant:
        def get_collections(self): raise ConnectionError("down")

    # No embedder → not ready regardless of Qdrant.
    monkeypatch.setattr(clients, "embedder", None)
    monkeypatch.setattr(clients, "qdrant_db", LiveQdrant())
    assert clients.probe_ready() is False

    # Embedder + live Qdrant → ready.
    monkeypatch.setattr(clients, "embedder", object())
    assert clients.probe_ready() is True

    # Live ping fails → not ready, and the handle is dropped for a later reconnect.
    monkeypatch.setattr(clients, "qdrant_db", DeadQdrant())
    monkeypatch.setattr(clients, "connect_qdrant", lambda: None)
    assert clients.probe_ready() is False
    assert clients.qdrant_db is None


# ----------------- Streamable HTTP transport -----------------
def test_mcp_route_is_auth_gated_and_unknown_transport_paths_404(monkeypatch):
    """The single /mcp route requires auth; /sse and /messages/ do not exist (404)."""
    import server
    from starlette.testclient import TestClient
    monkeypatch.setattr(git_sync, "init_git_repo", lambda: None)
    with TestClient(server.starlette_app) as client:
        # /mcp is gated: no token → 401 (not 404 — the route exists).
        assert client.post("/mcp").status_code == 401
        assert client.get("/sse").status_code == 404
        assert client.post("/messages/").status_code == 404


# A bearer header carrying a byte outside ASCII. Sent raw because httpx refuses
# to encode a non-ASCII str header — a real HTTP client puts bytes on the wire.
_NON_ASCII_AUTH = {b"Authorization": "Bearer tökén".encode("utf-8")}


def test_non_ascii_auth_header_is_rejected_with_401(monkeypatch):
    """A non-ASCII Authorization header is a mismatch, not a server error.

    compare_digest raises TypeError on non-ASCII strings; unguarded that surfaces
    as a 500 and skips the response bookkeeping, so assert the request id is set
    too — it proves the normal rejection path ran rather than an exception escaping.
    """
    import server
    from starlette.testclient import TestClient
    monkeypatch.setattr(git_sync, "init_git_repo", lambda: None)
    # Mirror uvicorn: an unhandled exception becomes a 500 instead of propagating.
    with TestClient(server.starlette_app, raise_server_exceptions=False) as client:
        r = client.post("/mcp", headers=_NON_ASCII_AUTH)
    assert r.status_code == 401
    assert "X-Request-ID" in r.headers


def test_non_ascii_auth_rejection_is_counted(monkeypatch):
    """The rejection lands in the HTTP metric — a 500 would bypass the counter."""
    import server
    from starlette.testclient import TestClient
    monkeypatch.setattr(git_sync, "init_git_repo", lambda: None)
    label = 'mcp_http_requests_total{path="/mcp",status="401"}'
    before = _counter_value(observability.metrics.render(), label)
    with TestClient(server.starlette_app, raise_server_exceptions=False) as client:
        client.post("/mcp", headers=_NON_ASCII_AUTH)
    after = _counter_value(observability.metrics.render(), label)
    assert after == before + 1


def test_session_manager_reflects_config(monkeypatch):
    """The transport knobs in config actually flow into the session manager."""
    import server
    monkeypatch.setattr(config, "MCP_JSON_RESPONSE", True)
    monkeypatch.setattr(config, "MCP_STATELESS", True)
    sm = server._new_session_manager()
    assert sm.json_response is True
    assert sm.stateless is True


def test_dns_rebinding_protection_off_by_default(monkeypatch):
    """With no host/origin allowlist, the transport's DNS-rebinding guard is off
    (the reverse proxy is the first line of defence — ADR 8/13)."""
    import server
    from mcp.server.streamable_http import TransportSecuritySettings
    # Default: empty allowlists → protection disabled.
    monkeypatch.setattr(config, "MCP_ALLOWED_HOSTS", [])
    monkeypatch.setattr(config, "MCP_ALLOWED_ORIGINS", [])
    s = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(config.MCP_ALLOWED_HOSTS or config.MCP_ALLOWED_ORIGINS),
        allowed_hosts=config.MCP_ALLOWED_HOSTS, allowed_origins=config.MCP_ALLOWED_ORIGINS)
    assert s.enable_dns_rebinding_protection is False
    # Configuring an allowlist flips it on.
    monkeypatch.setattr(config, "MCP_ALLOWED_HOSTS", ["brain.example.com"])
    s2 = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(config.MCP_ALLOWED_HOSTS or config.MCP_ALLOWED_ORIGINS),
        allowed_hosts=config.MCP_ALLOWED_HOSTS, allowed_origins=config.MCP_ALLOWED_ORIGINS)
    assert s2.enable_dns_rebinding_protection is True
