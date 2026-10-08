"""Card-scoped readonly snapshot. Explicit metadata columns; no task content."""
from pathlib import Path
import sqlite3
import time
from ..db import connect

class Unavailable(RuntimeError):
    pass

COLUMNS = {
    "cards": "card_id revision title note priority manual_state repo_path worktree_path git_common_dir change_name spec_path spec_hash acceptance_path acceptance_hash spec_review_pointer spec_review_hash base_head candidate_fingerprint risk estimate_by_pool allowed_commands profile_name profile_hash provider model effort routing_digest config_digest approval_generation approval_actor approval_at approval_hash approval_event_id task_id request_id last_reason last_evidence_event_id created_at updated_at".split(),
    "events": "operation_id payload_hash kind card_id task_id expected_revision result_revision actor at result reason payload metadata_delta".split(),
    "nights": "claim_seq night_id card_id approval_generation task_id request_id reserved_at phase stop_reason stop_evidence_event_id".split(),
    "quota": "snapshot_id pool_key weekly_remaining_bp observed_at recorded_at reset_at source operator covered_claim_seq stale stale_event_id".split(),
    "tasks": "id status stop_reason current_stage updated_at".split(),
}
TABLES = {"cards":"kanban_cards", "events":"kanban_events", "nights":"kanban_nights", "quota":"kanban_quota_snapshots", "tasks":"tasks"}

PAGE_SIZE = 20
HISTORY_LIMIT = 50

def page_snapshot(home: Path, *, archived: bool = False, after: int | None = None,
                  before: int | None = None, at_ms: int | None = None) -> dict:
    """Bounded HTTP read; independent archive view and stable card-ID keyset pages.

    CLI snapshot remains unchanged. Related histories are limited per page card;
    truncation is explicit and must never be interpreted as complete evidence.
    """
    if type(archived) is not bool or after is not None and before is not None:
        raise ValueError("invalid page")
    for cursor in (after, before):
        if cursor is not None and (type(cursor) is not int or not -(1 << 63) <= cursor <= (1 << 63) - 1):
            raise ValueError("invalid cursor")
    conn = None
    try:
        conn = connect(home / "orchestrator.db", read_only=True)
        conn.execute("BEGIN")
        for key in ('cards', 'events', 'nights', 'tasks'):
            actual = {row[1] for row in conn.execute("PRAGMA table_info(" + TABLES[key] + ")")}
            if not set(COLUMNS[key]) <= actual:
                raise Unavailable("schema unavailable: " + TABLES[key])
        state = "manual_state = 'archived'" if archived else "manual_state <> 'archived'"
        cursor = before if before is not None else after
        clause = " WHERE " + state
        args = ()
        if cursor is not None:
            identity = conn.execute('SELECT card_id FROM kanban_cards WHERE rowid=? AND ' + state + ' LIMIT 1', (cursor,)).fetchone()
            if identity is None:
                raise Unavailable('page cursor unavailable')
            clause += " AND card_id " + ('<' if before is not None else '>') + " ? COLLATE BINARY"
            args = (identity['card_id'],)
        descending = before is not None
        query = "SELECT rowid AS _page_rowid," + ','.join(COLUMNS['cards']) + " FROM kanban_cards" + clause + " ORDER BY card_id COLLATE BINARY " + ('DESC' if descending else 'ASC') + " LIMIT ?"
        rows = [dict(row) for row in conn.execute(query, (*args, PAGE_SIZE + 1))]
        identities = {row['card_id']: row.pop('_page_rowid') for row in rows}
        more = len(rows) > PAGE_SIZE
        cards = rows[:PAGE_SIZE]
        if descending:
            cards.reverse()
        def exists(op, ident):
            return conn.execute("SELECT 1 FROM kanban_cards WHERE " + state + " AND card_id " + op + " ? COLLATE BINARY LIMIT 1", (ident,)).fetchone() is not None
        has_previous = bool(cards) and (more if descending else exists('<', cards[0]['card_id']) if after is not None else False)
        has_next = bool(cards) and (exists('>', cards[-1]['card_id']) if descending else more)
        data = {'cards': cards, 'events': [], 'nights': [], 'tasks': [], 'quota': []}
        truncated = {}
        # Bound both materialized card history and task metadata to this page.
        for card in cards:
            ident = card['card_id']; flags = {}
            for key, order in (('events', 'result_revision DESC,at DESC,operation_id DESC'), ('nights', 'claim_seq DESC,night_id DESC')):
                sql = "SELECT " + ','.join(COLUMNS[key]) + " FROM " + TABLES[key] + " WHERE card_id=? ORDER BY " + order + " LIMIT ?"
                history = [dict(row) for row in conn.execute(sql, (ident, HISTORY_LIMIT + 1))]
                flags[key] = len(history) > HISTORY_LIMIT
                data[key].extend(history[:HISTORY_LIMIT])
            if any(flags.values()):
                truncated[ident] = flags
        task_ids = sorted({card['task_id'] for card in cards if card.get('task_id') is not None})
        if task_ids:
            sql = "SELECT " + ','.join(COLUMNS['tasks']) + " FROM tasks WHERE id IN (" + ','.join('?' for _ in task_ids) + ") ORDER BY id"
            data['tasks'] = [dict(row) for row in conn.execute(sql, task_ids)]
        conn.execute("COMMIT")
        return {'available': True, 'source': str(home / 'orchestrator.db'),
                'generated_at_ms': int(time.time()*1000) if at_ms is None else at_ms,
                **data, 'history_truncated': truncated,
                'page': {'archived': archived, 'size': PAGE_SIZE,
                         'previous': identities[cards[0]['card_id']] if has_previous else None,
                         'next': identities[cards[-1]['card_id']] if has_next else None,
                         'history_limit': HISTORY_LIMIT}}
    except (OSError, sqlite3.Error) as exc:
        raise Unavailable(str(exc)) from exc
    finally:
        if conn is not None:
            conn.close()

def snapshot(home: Path, *, at_ms: int | None = None, card_id: str | None = None) -> dict:
    conn = None
    try:
        conn = connect(home / "orchestrator.db", read_only=True)
        conn.execute("BEGIN")
        for key, table in TABLES.items():
            actual = {row[1] for row in conn.execute("PRAGMA table_info(" + table + ")")}
            if not set(COLUMNS[key]) <= actual:
                raise Unavailable("schema unavailable: " + table)
        def select(key, clause="", args=()):
            return [dict(row) for row in conn.execute("SELECT " + ",".join(COLUMNS[key]) + " FROM " + TABLES[key] + clause, args)]
        cards = select("cards", " WHERE card_id=?" if card_id is not None else "", (card_id,) if card_id is not None else ())
        data = {"cards":cards, "quota":select("quota")}
        # SQL is scoped to card membership, never historical task content.
        membership = "SELECT card_id FROM kanban_cards" + (" WHERE card_id=?" if card_id is not None else "")
        args = (card_id,) if card_id is not None else ()
        for key in ("events", "nights"):
            data[key] = select(key, " WHERE card_id IN (" + membership + ")", args) if cards else []
        if any(card.get("task_id") is not None for card in cards):
            binding = "SELECT task_id FROM kanban_cards WHERE task_id IS NOT NULL" + (" AND card_id=?" if card_id is not None else "")
            data["tasks"] = select("tasks", " WHERE id IN (" + binding + ")", args)
        else:
            data["tasks"] = []
        conn.execute("COMMIT")
        for key, rows in data.items():
            rows.sort(key=lambda row: str(row.get({"cards":"card_id", "events":"operation_id", "nights":"night_id", "quota":"snapshot_id", "tasks":"id"}[key])))
        return {"available":True, "source":str(home / "orchestrator.db"), "generated_at_ms":int(time.time()*1000) if at_ms is None else at_ms, **data}
    except (OSError, sqlite3.Error) as exc:
        raise Unavailable(str(exc)) from exc
    finally:
        if conn is not None:
            conn.close()
