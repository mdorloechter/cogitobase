"""Mem0 short-term memory tools (atomic, category-prefixed preferences).

ADR 6 (and the pillar split in ADR 2): Mem0 holds only implicit, atomic, categorized
facts. The boundary is enforced server-side (hard reject; soft "similar id" hint).
"""
import asyncio

import config
import clients
from config import log
from registry import register, text, OUTCOME_REJECTED, OUTCOME_UNAVAILABLE


def _no_such_id(memory_id: str) -> str:
    """The reply for an id the store does not hold: the id, and how to obtain a real one.

    One wording for all three tools that take an id — the same miss should not read
    differently depending on which one hit it.
    """
    return (f"Memory '{memory_id}' not found. Use get_memories to list ids, or "
            f"search_memories to find one by content.")


def _offline(action: str) -> str:
    """The reply for a call Mem0 cannot serve, naming the action it blocked.

    The blocked verb, not just the product: a caller reading "Mem0 offline." cannot tell
    whether its fact was stored, and the store is an implementation detail it never chose.
    """
    return f"Cannot {action}: Mem0 is offline. Vault notes (write_note/search_vault) still work."


# get_memories has no offset, so `limit` is the whole window: an unbounded one lets a
# single call spend the caller's context on the entire store. Clamped like search_vault's,
# and higher than it, because a memory is one line where a search hit is a snippet.
_DEFAULT_MEMORY_LIMIT = 10
_MAX_MEMORY_LIMIT = 50


def _results_of(obj):
    """Mem0 returns either a list or a {'results': [...]} dict depending on version."""
    return obj.get("results", obj) if isinstance(obj, dict) else obj


def _added_id(added) -> str:
    """The id of the memory an add() just created, as ' (id: …)' — or '' if unknown.

    Correcting a fact needs its id, and add() hands one back, so withholding it forces
    the caller to search for what it just wrote. A response carrying no id yields "":
    a stored fact is not a failure because its id could not be named.
    """
    for entry in _results_of(added) or []:
        if isinstance(entry, dict) and entry.get("id"):
            return f" (id: {entry['id']})"
    return ""


@register(
    "add_memory", "Store an atomic behavioural fact that should tune future sessions (e.g. '[Preference] ...'). For facts about a tech/project/person, use write_note instead.",
    {"type": "object", "properties": {"fact": {"type": "string", "description":
        "One atomic behavioural rule, starting with [Preference], [Constraint], [Explicit] or "
        f"[Inferred], at most {config.MEM0_MAX_FACT_CHARS} characters. Stored verbatim."}},
     "required": ["fact"]},
)
async def add_memory(arguments: dict) -> list:
    if not clients.mem0_client:
        return text(_offline("store the fact"), OUTCOME_UNAVAILABLE)
    fact = arguments["fact"].strip()
    # Enforce the Vault-vs-Mem0 boundary in code, not just the prompt.
    if len(fact) > config.MEM0_MAX_FACT_CHARS:
        return text(
            f"Fact is {len(fact)} characters, over the {config.MEM0_MAX_FACT_CHARS} limit "
            f"(MEM0_MAX_FACT_CHARS). Mem0 is for atomic preferences — split it into one "
            f"rule per memory, or use write_note for structured knowledge.", OUTCOME_REJECTED)
    if not fact.startswith(config.MEM0_ALLOWED_PREFIXES):
        return text(
            "Fact must start with a behavioural category prefix — "
            "[Preference], [Constraint], [Explicit], or [Inferred]. Facts about a "
            "tech/project/person are Vault notes (write_note), not memories.", OUTCOME_REJECTED)
    # Refuse an exact repeat (hard), report a merely similar one (soft: "update over
    # append"). A verbatim write has no deduplication behind it — Mem0's own md5 check
    # sits in the extraction path this handler deliberately skips — so a resend would
    # add a second memory holding the identical text under a second id, and neither of
    # them is the one to update. Compared on the stripped text, which is what md5 saw.
    note = ""
    try:
        similar = await asyncio.to_thread(clients.mem0_client.search, fact, filters={"user_id": config.MEM0_USER_ID})
        sim_list = _results_of(similar) or []
        duplicate = next((r for r in sim_list
                          if isinstance(r, dict) and str(r.get("memory", "")).strip() == fact), None)
        if duplicate:
            return text(
                f"This exact fact is already stored as {duplicate.get('id', '?')}. "
                f"Use update_memory to change it, or delete_memory to drop it.", OUTCOME_REJECTED)
        if sim_list:
            ids = ", ".join(str(r.get("id", "")) for r in sim_list[:3])
            note = f" Similar existing memories: {ids} — consider update_memory instead of duplicating."
    except Exception:
        # Non-fatal on purpose, and that includes the duplicate check: a search that is
        # down must not block a write. The cost of a lost check is a duplicate the
        # caller can delete; the cost of blocking is a fact that cannot be stored.
        log.exception("Mem0 similarity search failed (non-fatal).")
    # infer=False stores the text VERBATIM. The default runs it through an LLM
    # extraction step that rewrites it — and the first thing it drops is the
    # behavioural prefix this handler just insisted on, so the store ends up holding
    # facts that MEM0_ALLOWED_PREFIXES would have rejected. It also makes one add
    # exactly one memory with one id, which is what the caller is told below.
    added = await asyncio.to_thread(
        clients.mem0_client.add, fact, user_id=config.MEM0_USER_ID, infer=False)
    return text(f"Fact remembered{_added_id(added)}.{note}")


@register(
    "search_memories", "Search stored behavioural facts by content. Call this before add_memory, "
    "to update an existing rule instead of duplicating it.",
    {"type": "object", "properties": {"query": {"type": "string", "description":
        "Keywords describing the rule you are looking for. Each hit is returned with its id."}},
     "required": ["query"]},
)
async def search_memories(arguments: dict) -> list:
    if not clients.mem0_client:
        return text(_offline("search memories"), OUTCOME_UNAVAILABLE)
    results = await asyncio.to_thread(clients.mem0_client.search, arguments["query"], filters={"user_id": config.MEM0_USER_ID})
    results = _results_of(results)
    mem_text = "\n".join([f"[{r.get('id', '')}] {r.get('memory', '')}" for r in results]) if results else "None."
    return text(f"Results:\n{mem_text}")


@register(
    "get_memories",
    f"List stored memories, up to {_MAX_MEMORY_LIMIT}, in no particular order. There is no "
    f"paging and no sorting, so in a store larger than that this is a sample, not the whole "
    f"of it — use search_memories to find a specific fact.",
    {"type": "object", "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_MEMORY_LIMIT,
                  "description": f"Memories to return (default {_DEFAULT_MEMORY_LIMIT})."}}},
)
async def get_memories(arguments: dict) -> list:
    if not clients.mem0_client:
        return text(_offline("list memories"), OUTCOME_UNAVAILABLE)
    # Clamped, not trusted: a schema `maximum` is advisory — a client is free to send
    # any integer, and an unbounded one returns the whole store in one reply. Mirrors
    # search_vault, including tolerating a non-integer rather than raising on one.
    raw_limit = arguments.get("limit")
    try:
        requested = _DEFAULT_MEMORY_LIMIT if raw_limit is None else int(raw_limit)
    except (TypeError, ValueError):
        requested = _DEFAULT_MEMORY_LIMIT
    limit = max(1, min(requested, _MAX_MEMORY_LIMIT))
    # `top_k` is Mem0's name for it, and get_all takes **kwargs: a wrong keyword is
    # swallowed rather than raised, so the store silently applies its own default and
    # the clamp above decides nothing. Bound at the DB layer, not by slicing a full
    # fetch, so the store never assembles more than is asked for.
    results = _results_of(await asyncio.to_thread(
        clients.mem0_client.get_all, filters={"user_id": config.MEM0_USER_ID}, top_k=limit))
    mem_text = "\n".join([f"[{r.get('id', '')}] {r.get('memory', '')}" for r in results]) if results else "None."
    return text(f"Memories:\n{mem_text}")


@register(
    "get_memory", "Get one memory by id.",
    {"type": "object", "properties": {"memory_id": {"type": "string", "description":
        "Id as returned by get_memories, search_memories or add_memory."}},
     "required": ["memory_id"]},
)
async def get_memory(arguments: dict) -> list:
    if not clients.mem0_client:
        return text(_offline("fetch the memory"), OUTCOME_UNAVAILABLE)
    memory_id = arguments["memory_id"]
    res = await asyncio.to_thread(clients.mem0_client.get, memory_id)
    if not res:
        return text(_no_such_id(memory_id), OUTCOME_REJECTED)
    # Render as the other two read tools do: one memory per `[id] text` line. str(res)
    # would print a Python dict, so the same memory reads differently depending on
    # which tool fetched it, and the text arrives wrapped in quoting and escapes.
    if isinstance(res, dict) and res.get("memory"):
        return text(f"[{res.get('id', memory_id)}] {res['memory']}")
    # An unexpected shape shows its content rather than swallowing it — the id WAS
    # found, so reporting it as missing would be a lie.
    return text(str(res))


@register(
    "update_memory", "Replace a stored memory's text, keeping its id. The preferred way to correct "
    "a rule — the store must never hold two memories contradicting each other.",
    {"type": "object", "properties": {
        "memory_id": {"type": "string", "description":
                      "Id of the memory to replace, from get_memories or search_memories."},
        "fact": {"type": "string", "description":
                 "The new text, under the same prefix rules as add_memory."}},
     "required": ["memory_id", "fact"]},
)
async def update_memory(arguments: dict) -> list:
    if not clients.mem0_client:
        return text(_offline("update the memory"), OUTCOME_UNAVAILABLE)
    memory_id = arguments["memory_id"]
    # Mem0 raises on an unknown id; report it like get_memory instead of leaking the driver error.
    try:
        await asyncio.to_thread(clients.mem0_client.update, memory_id, arguments["fact"])
    except Exception:
        log.exception("update_memory failed")
        return text(_no_such_id(memory_id), OUTCOME_REJECTED)
    return text("Updated.")


@register(
    "delete_memory", "Drop a memory for good, so it stops tuning future sessions. Not undoable, "
    "and unlike a vault note there is no git history to recover it from — prefer update_memory "
    "where the rule merely changed.",
    {"type": "object", "properties": {"memory_id": {"type": "string", "description":
        "Id of the memory to drop, from get_memories or search_memories."}},
     "required": ["memory_id"]},
)
async def delete_memory(arguments: dict) -> list:
    if not clients.mem0_client:
        return text(_offline("delete the memory"), OUTCOME_UNAVAILABLE)
    memory_id = arguments["memory_id"]
    # Mem0 raises on an unknown id; report it like get_memory instead of leaking the driver error.
    try:
        await asyncio.to_thread(clients.mem0_client.delete, memory_id)
    except Exception:
        log.exception("delete_memory failed")
        return text(_no_such_id(memory_id), OUTCOME_REJECTED)
    return text("Deleted.")
