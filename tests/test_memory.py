"""The Mem0 layer: enforcement on the way in, delegation on the way out.

The mem0 client is mocked throughout — what is under test is the boundary this
server puts in front of it (length, behavioural prefix) and its offline handling.
One test is the exception and imports the real package: a mock cannot tell whether
the keywords this server sends are the ones mem0 actually reads.
"""
import os
import sys
import types
import pytest
import config
from conftest import call


# ----------------- Mem0 telemetry is off by default -----------------
def _fake_mem0_module(monkeypatch, seen):
    """Stand in for the mem0 package.

    Records MEM0_TELEMETRY at ATTRIBUTE ACCESS, i.e. exactly when the
    `from mem0 import Memory` statement runs. The real mem0.memory.telemetry reads
    the variable at module scope, so only the value visible at that instant decides
    whether a PostHog client is built — recording it any later would still pass if
    the opt-out slipped below the import.
    """
    import os

    class FakeMemory:
        @staticmethod
        def from_config(cfg):
            seen["config"] = cfg
            return object()

    mod = types.ModuleType("mem0")

    def module_getattr(name):
        if name != "Memory":
            raise AttributeError(name)
        seen["telemetry_at_import"] = os.environ.get("MEM0_TELEMETRY")
        return FakeMemory

    mod.__getattr__ = module_getattr    # PEP 562
    monkeypatch.setitem(sys.modules, "mem0", mod)


def _unset_telemetry_env(monkeypatch):
    """Make MEM0_TELEMETRY absent AND restorable. A bare delenv(raising=False) on an
    already-absent key records no undo, so the setdefault under test would leak the
    variable into every later test."""
    monkeypatch.setenv("MEM0_TELEMETRY", "")
    monkeypatch.delenv("MEM0_TELEMETRY")


def test_connect_mem0_disables_bundled_telemetry(monkeypatch):
    """mem0ai ships opt-out PostHog telemetry that fires on Memory() init. It must
    be off before the import, or the server phones us.i.posthog.com on every start
    with an egress destination PRIVACY.md does not list."""
    import clients
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    _unset_telemetry_env(monkeypatch)
    seen = {}
    _fake_mem0_module(monkeypatch, seen)

    assert clients.connect_mem0() is not None
    assert seen["telemetry_at_import"] == "False"
    # Still pinned to Gemini — the opt-out must not have displaced that.
    assert seen["config"]["llm"]["provider"] == "gemini"
    assert seen["config"]["embedder"]["provider"] == "gemini"


def test_connect_mem0_keeps_an_explicit_telemetry_optin(monkeypatch):
    """setdefault, not overwrite: an operator who exports MEM0_TELEMETRY=True has
    made an informed choice (PRIVACY.md documents what it sends)."""
    import clients
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("MEM0_TELEMETRY", "True")
    seen = {}
    _fake_mem0_module(monkeypatch, seen)

    clients.connect_mem0()
    assert seen["telemetry_at_import"] == "True"


# ----------------- Mem0 enforcement (mem0 client mocked) -----------------
@pytest.mark.asyncio
async def test_add_memory_rejects_long(monkeypatch):
    import clients
    monkeypatch.setattr(clients, "mem0_client", object())
    res = await call("add_memory", {"fact": "[Preference] " + "x" * 500})
    assert "MEM0_MAX_FACT_CHARS" in res[0].text and str(config.MEM0_MAX_FACT_CHARS) in res[0].text


@pytest.mark.asyncio
async def test_add_memory_rejects_missing_prefix(monkeypatch):
    import clients
    monkeypatch.setattr(clients, "mem0_client", object())
    res = await call("add_memory", {"fact": "user likes python"})
    assert "prefix" in res[0].text and "[Preference]" in res[0].text


@pytest.mark.asyncio
async def test_add_memory_rejects_content_prefixes(monkeypatch):
    # A tech/project FACT belongs in a Vault note (tech/ or projects/ folder), not
    # Mem0 — rejected like any missing prefix.
    import clients
    monkeypatch.setattr(clients, "mem0_client", object())
    for fact in ("[Tech: PostgreSQL] Acme runs on Postgres",
                 "[Project: Phoenix] uses React"):
        res = await call("add_memory", {"fact": fact})
        assert "prefix" in res[0].text and "write_note" in res[0].text


@pytest.mark.asyncio
async def test_add_memory_reports_similar(monkeypatch):
    import clients

    class FakeMem:
        def search(self, *a, **k): return [{"id": "abc123", "memory": "old"}]
        def add(self, *a, **k): return None
    monkeypatch.setattr(clients, "mem0_client", FakeMem())
    res = await call("add_memory", {"fact": "[Preference] dark mode"})
    assert "Fact remembered" in res[0].text
    assert "abc123" in res[0].text


def _recording_mem0(monkeypatch, added=None, similar=()):
    """A mem0 stand-in that records what add() was called with."""
    import clients
    seen = {}

    class FakeMem:
        def search(self, *a, **k): return list(similar)

        def add(self, fact, **kwargs):
            seen["fact"] = fact
            seen["kwargs"] = kwargs
            return added
    monkeypatch.setattr(clients, "mem0_client", FakeMem())
    return seen


@pytest.mark.asyncio
async def test_add_memory_stores_the_fact_verbatim(monkeypatch):
    """The prefix this handler enforces has to survive into the store.

    Mem0's default infer=True runs the text through an LLM extraction that rewrites
    it and drops the '[Preference]' marker, leaving the store holding facts the
    handler would have rejected on the way in.
    """
    fact = "[Preference] pineapple belongs on pizza"
    seen = _recording_mem0(monkeypatch)
    await call("add_memory", {"fact": fact})
    assert seen["kwargs"].get("infer") is False
    assert seen["fact"] == fact


@pytest.mark.asyncio
async def test_add_memory_returns_the_new_id(monkeypatch):
    """Correcting a fact needs its id, and add() hands one back."""
    _recording_mem0(monkeypatch, added={"results": [{"id": "new-id-1", "event": "ADD"}]})
    res = await call("add_memory", {"fact": "[Constraint] never Mongo"})
    assert "new-id-1" in res[0].text


@pytest.mark.asyncio
async def test_add_memory_refuses_a_fact_it_already_holds(monkeypatch):
    """A verbatim write has no dedup behind it: Mem0's own md5 check lives in the
    extraction path add_memory skips, so a resend would store the same text twice
    under two ids — and neither would be the one to update."""
    fact = "[Preference] dark mode everywhere"
    seen = _recording_mem0(monkeypatch, similar=[{"id": "existing-7", "memory": fact}])
    res = await call("add_memory", {"fact": fact})
    assert "already stored" in res[0].text
    assert "existing-7" in res[0].text
    assert "fact" not in seen, "the duplicate must not reach add()"


@pytest.mark.asyncio
async def test_add_memory_still_stores_a_merely_similar_fact(monkeypatch):
    """Only an exact repeat is refused. A near-miss stays a hint, as before —
    'similar' is the embedder's judgement, not grounds to drop the caller's fact."""
    seen = _recording_mem0(
        monkeypatch,
        added={"results": [{"id": "fresh"}]},
        similar=[{"id": "close-3", "memory": "[Preference] dark mode in the editor"}])
    res = await call("add_memory", {"fact": "[Preference] dark mode everywhere"})
    assert "Fact remembered" in res[0].text
    assert "close-3" in res[0].text
    assert seen["fact"] == "[Preference] dark mode everywhere"


@pytest.mark.asyncio
async def test_add_memory_writes_when_the_duplicate_check_cannot_run(monkeypatch):
    """A search that is down must not block a write: a lost check costs a duplicate
    the caller can delete, a blocked write costs the fact."""
    import clients
    seen = {}

    class FakeMem:
        def search(self, *a, **k): raise RuntimeError("Qdrant unreachable")

        def add(self, fact, **kwargs):
            seen["fact"] = fact
            return {"results": [{"id": "stored-anyway"}]}
    monkeypatch.setattr(clients, "mem0_client", FakeMem())
    res = await call("add_memory", {"fact": "[Constraint] no force pushes"})
    assert "Fact remembered" in res[0].text
    assert seen["fact"] == "[Constraint] no force pushes"


@pytest.mark.asyncio
async def test_add_memory_survives_a_response_without_an_id(monkeypatch):
    """A stored fact is not a failure because its id could not be named."""
    for added in (None, [], {}, {"results": []}, [{"event": "ADD"}], "unexpected"):
        _recording_mem0(monkeypatch, added=added)
        res = await call("add_memory", {"fact": "[Explicit] speak German"})
        assert "Fact remembered" in res[0].text
        assert "id:" not in res[0].text


# ----------------- Mem0 query tools (search/get_all) -----------------
@pytest.mark.asyncio
async def test_search_memories_formats_results(monkeypatch):
    import clients

    class FakeMem:
        def search(self, query, filters=None):
            return [{"id": "id1", "memory": "likes dark mode"},
                    {"id": "id2", "memory": "uses Python"}]
    monkeypatch.setattr(clients, "mem0_client", FakeMem())
    res = await call("search_memories", {"query": "preferences"})
    assert "id1" in res[0].text and "likes dark mode" in res[0].text
    assert "id2" in res[0].text


@pytest.mark.asyncio
async def test_search_memories_handles_results_dict(monkeypatch):
    # Newer mem0 wraps results in {"results": [...]}; the tool must unwrap it.
    import clients

    class FakeMem:
        def search(self, query, filters=None):
            return {"results": [{"id": "x", "memory": "fact"}]}
    monkeypatch.setattr(clients, "mem0_client", FakeMem())
    res = await call("search_memories", {"query": "q"})
    assert "fact" in res[0].text


def test_every_mem0_keyword_is_one_mem0_reads():
    """Mem0's methods take **kwargs, so a keyword it does not know is dropped, not raised.

    That makes a wrong name invisible from this side: the call returns normally, the store
    applies its own default, and every mock written to match our call agrees with us. A
    clamp in front of such a call decides nothing while the suite stays green — `top_k` is
    what mem0 reads for the size of a `get_all`, and `limit=` would be discarded.

    So the real package is imported and the call shapes are read out of memory.py rather
    than listed here: a list would be a second claim about the same calls, free to drift
    from them exactly like the mocks did. A keyword that lands in **kwargs fails the test.
    """
    import ast
    import inspect
    from pathlib import Path

    # Before the import, not after: mem0.memory.telemetry reads this at module scope and
    # builds a PostHog client when it is unset — an egress PRIVACY.md does not list, which
    # a test must not trigger. clients.connect_mem0 does the same, for the same reason.
    os.environ.setdefault("MEM0_TELEMETRY", "False")
    from mem0.memory.main import Memory

    def _mem0_method(node):
        """The method name if `node` is an attribute access on the mem0 client, else None."""
        return (node.attr if isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "mem0_client" else None)

    source = (Path(__file__).resolve().parent.parent / "memory.py").read_text(encoding="utf-8")
    calls = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        keywords = [kw.arg for kw in node.keywords if kw.arg]
        if (method := _mem0_method(node.func)):
            # A direct `clients.mem0_client.<method>(...)`.
            calls.append((method, len(node.args), keywords, node.lineno))
        elif any((method := _mem0_method(arg)) for arg in node.args):
            # `asyncio.to_thread(clients.mem0_client.<method>, ...)` — the shape every call
            # in this module actually uses, since they all run off the event loop. to_thread
            # forwards its remaining arguments verbatim, so they are the method's.
            positional = len(node.args) - 1  # minus the method reference itself
            calls.append((method, positional, keywords, node.lineno))

    assert len(calls) >= 6, (f"expected every mem0 call to be found, got {len(calls)} — the "
                             "extraction has stopped matching and would pass on anything")
    stray = []
    for method, positional, keywords, lineno in calls:
        signature = inspect.signature(getattr(Memory, method))
        # bind() reports where each argument lands. `self` plus the call's positional
        # arguments are stood in for by None; only the keyword NAMES are under test, and a
        # bind that raises is a genuine mismatch this test should report too.
        try:
            bound = signature.bind(*([None] * (positional + 1)), **{k: None for k in keywords})
        except TypeError as e:
            stray.append(f"memory.py:{lineno} {method}: {e}")
            continue
        if (swallowed := set(bound.arguments.get("kwargs", {}))):
            stray.append(f"memory.py:{lineno} {method}({', '.join(sorted(swallowed))})")
    assert not stray, ("these keywords are not parameters of the mem0 method they are sent "
                       f"to, so mem0 ignores them silently: {sorted(stray)}")


@pytest.mark.asyncio
async def test_get_memories_passes_its_limit_to_the_store(monkeypatch):
    """Bounded at the store, not by slicing what came back — a full fetch is the cost the
    limit exists to avoid."""
    import clients
    seen = {}

    class FakeMem:
        def get_all(self, *, filters=None, top_k=20, **kwargs):
            seen["top_k"] = top_k
            return [{"id": "a", "memory": "one"}]
    monkeypatch.setattr(clients, "mem0_client", FakeMem())
    res = await call("get_memories", {"limit": 7})
    assert seen["top_k"] == 7
    assert "one" in res[0].text


@pytest.mark.asyncio
async def test_get_memories_clamps_its_limit(monkeypatch):
    """The schema `maximum` is advisory — a client may send any integer, and the reply
    is spent from the caller's context, so an unbounded limit hands the whole store to
    one call. Clamped in the handler, like search_vault's."""
    import clients
    import memory
    seen = {}

    # Keyword-only and named as mem0 names it, so a call this fake accepts is one the real
    # client accepts too. A fake looser than the library confirms what the handler sends,
    # not what mem0 takes, and a wrong keyword would pass it.
    class FakeMem:
        def get_all(self, *, filters=None, top_k=20, **kwargs):
            seen["top_k"] = top_k
            return [{"id": "a", "memory": "one"}]
    monkeypatch.setattr(clients, "mem0_client", FakeMem())
    await call("get_memories", {"limit": 5000})
    assert seen["top_k"] == memory._MAX_MEMORY_LIMIT
    await call("get_memories", {"limit": 0})
    assert seen["top_k"] == 1, "a floor too — 0 must not mean 'no results'"


# ----------------- Mem0 CRUD (update/delete/get) delegate to the client -----------------
@pytest.mark.asyncio
async def test_update_memory_calls_client(monkeypatch):
    import clients
    calls = {}

    class FakeMem:
        def update(self, memory_id, fact): calls["update"] = (memory_id, fact)
    monkeypatch.setattr(clients, "mem0_client", FakeMem())
    res = await call("update_memory", {"memory_id": "m1", "fact": "[Preference] new"})
    assert "Updated." in res[0].text
    assert calls["update"] == ("m1", "[Preference] new")


@pytest.mark.asyncio
async def test_delete_memory_calls_client(monkeypatch):
    import clients
    calls = {}

    class FakeMem:
        def delete(self, memory_id): calls["delete"] = memory_id
    monkeypatch.setattr(clients, "mem0_client", FakeMem())
    res = await call("delete_memory", {"memory_id": "m2"})
    assert "Deleted." in res[0].text
    assert calls["delete"] == "m2"


@pytest.mark.asyncio
async def test_update_memory_unknown_id_returns_not_found(monkeypatch):
    import clients

    class FakeMem:
        def update(self, memory_id, fact):
            raise ValueError(f"Memory with id {memory_id} not found in collection.")
    monkeypatch.setattr(clients, "mem0_client", FakeMem())
    res = await call("update_memory", {"memory_id": "bogus", "fact": "[Preference] new"})
    assert "bogus" in res[0].text and "get_memories" in res[0].text


@pytest.mark.asyncio
async def test_delete_memory_unknown_id_returns_not_found(monkeypatch):
    import clients

    class FakeMem:
        def delete(self, memory_id):
            raise ValueError(f"Memory with id {memory_id} not found in collection.")
    monkeypatch.setattr(clients, "mem0_client", FakeMem())
    res = await call("delete_memory", {"memory_id": "bogus"})
    assert "bogus" in res[0].text and "get_memories" in res[0].text


@pytest.mark.asyncio
async def test_get_memory_returns_fetched(monkeypatch):
    import clients

    class FakeMem:
        def get(self, memory_id): return {"id": memory_id, "memory": "the fact"}
    monkeypatch.setattr(clients, "mem0_client", FakeMem())
    res = await call("get_memory", {"memory_id": "m3"})
    assert "the fact" in res[0].text


@pytest.mark.asyncio
async def test_get_memory_renders_like_the_other_read_tools(monkeypatch):
    """One memory per `[id] text` line, as get_memories/search_memories do — not a
    Python dict, which makes the same memory read differently per tool and wraps the
    text in quoting and escapes."""
    import clients

    class FakeMem:
        def get(self, memory_id):
            return {"id": memory_id, "memory": "[Preference] dark mode",
                    "created_at": "2026-08-01T00:00:00Z", "hash": "abc"}
    monkeypatch.setattr(clients, "mem0_client", FakeMem())
    res = await call("get_memory", {"memory_id": "m4"})
    assert res[0].text == "[m4] [Preference] dark mode"


@pytest.mark.asyncio
async def test_get_memory_shows_an_unexpected_shape_rather_than_hiding_it(monkeypatch):
    """The id WAS found, so 'Not found.' would be a lie."""
    import clients

    class FakeMem:
        def get(self, memory_id): return {"id": memory_id, "unexpected": "payload"}
    monkeypatch.setattr(clients, "mem0_client", FakeMem())
    res = await call("get_memory", {"memory_id": "m5"})
    assert "not found" not in res[0].text.lower()
    assert "payload" in res[0].text


@pytest.mark.asyncio
async def test_memory_tools_report_offline_without_client(monkeypatch):
    import clients
    monkeypatch.setattr(clients, "mem0_client", None)
    for tool, args in (("update_memory", {"memory_id": "x", "fact": "[Preference] y"}),
                       ("delete_memory", {"memory_id": "x"}),
                       ("get_memory", {"memory_id": "x"})):
        res = await call(tool, args)
        assert "Mem0 is offline" in res[0].text
