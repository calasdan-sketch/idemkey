def one(store, key):
    """The single row for a *user* key (keys are namespaced in the store)."""
    rows = store.find(key)
    assert len(rows) <= 1, f"ambiguous key {key!r}: {[r['key'] for r in rows]}"
    return rows[0] if rows else None
