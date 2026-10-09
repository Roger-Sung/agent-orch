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

def _cursor(value):
    if value is not None and (type(value) is not int or not -(1 << 63) <= value < (1 << 63)):
        raise ValueError("invalid cursor")


def _location(event, revision):
    """Validate journal evidence; legacy creates without placement remain board."""
    import json
    from .commands import payload_hash, reserved_marker_in
    if event is None:
        return "board"
    if type(revision) is not int or not 0 <= revision < (1 << 63):
        return "unknown"
    try:
        delta = json.loads(event.get("metadata_delta") or "null")
        if event["kind"] == "create":
            if not isinstance(delta, dict):
                return "unknown"
            destination = delta.get("queue_location", "board")
            if "queue_location" not in delta:
                return "board"
            payload = json.loads(event.get("payload") or "null")
            if (destination not in {"board", "backlog"} or type(event.get("result_revision")) is not int or event["result_revision"] != 0 or
                not isinstance(payload, dict) or payload.get("card_id") != event.get("card_id") or
                payload.get("actor") != event.get("actor") or not isinstance(payload.get("fields"), dict) or
                event.get("payload_hash") != payload_hash("create", payload)):
                return "unknown"
            return destination
        payload = json.loads(event.get("payload") or "null")
        result_revision = event.get("result_revision")
        expected = event.get("expected_revision")
        if (type(result_revision) is not int or not 1 <= result_revision <= revision or
            type(expected) is not int or expected != result_revision - 1 or
            not isinstance(payload, dict) or set(payload) != {"card_id", "expected_revision", "actor", "destination", "user_request"} or
            payload.get("card_id") != event.get("card_id") or type(payload.get("expected_revision")) is not int or payload.get("expected_revision") != expected or
            payload.get("actor") != event.get("actor") or type(event.get("at")) is not int or event["at"] < 0):
            return "unknown"
        for key, limit in (("actor", 500), ("user_request", 4000)):
            text = payload.get(key)
            if not isinstance(text, str) or not text.strip() or len(text) > limit or reserved_marker_in(text):
                return "unknown"
        destination = payload.get("destination")
        if destination not in {"board", "backlog"} or delta != {"queue_location": destination} or event.get("payload_hash") != payload_hash("place", payload):
            return "unknown"
        return destination
    except (ValueError, TypeError, KeyError):
        return "unknown"


def _summaries(conn, cards):
    """Batch current evidence and aggregates; never materialize card histories."""
    import json
    data = {"events": [], "nights": [], "tasks": [], "quota": [], "summary_flags": {}, "queue_locations": {}}
    if not cards:
        return data
    for card in cards:
        data['summary_flags'][card['card_id']] = {'bad_card': any(type(card.get(key)) is not int or not 0 <= card[key] < (1 << 63) for key in ('revision','approval_generation'))}
    ids = [c["card_id"] for c in cards]
    marks = ','.join('?' for _ in ids)
    columns = ','.join(COLUMNS['events'])
    # Current report only; retain the latest actual scope barrier, even if later
    # descriptive edits exist (A -> B -> A must invalidate the old A report).
    for predicate in ("kind='report-progress'", "kind='edit' AND CASE WHEN json_valid(metadata_delta) THEN CASE WHEN json_type(metadata_delta,'$.scope_fields_changed')='array' THEN json_array_length(metadata_delta,'$.scope_fields_changed')>0 ELSE json_extract(metadata_delta,'$.scope_fields_changed') IS NOT NULL AND json_extract(metadata_delta,'$.scope_fields_changed') NOT IN (0,'') END ELSE 0 END", "kind IN ('place','create')"):
        query = "SELECT " + columns + " FROM (SELECT *,ROW_NUMBER() OVER (PARTITION BY card_id ORDER BY result_revision DESC,operation_id DESC) AS rn FROM kanban_events WHERE card_id IN (" + marks + ") AND result='accepted' AND " + predicate + ") WHERE rn=1"
        rows = [dict(row) for row in conn.execute(query, ids)]
        if predicate.startswith("kind IN"):
            by_id = {row['card_id']: row for row in rows}
            for card in cards:
                data['queue_locations'][card['card_id']] = _location(by_id.get(card['card_id']), card['revision'])
        else:
            data['events'].extend(rows)
    def location_bad(raw, revision):
        return int(_location(json.loads(raw), revision) == 'unknown')
    conn.create_function('location_bad', 2, location_bad)
    event_json = 'json_object(' + ','.join("'" + col + "',e." + col for col in COLUMNS['events']) + ')'
    query = """SELECT e.card_id,
        MAX(typeof(e.at)<>'integer' OR e.at<0) AS bad_time,
        MAX(e.kind IN ('edit','report-progress') AND e.result='accepted' AND
            (typeof(e.result_revision)<>'integer' OR e.result_revision<1 OR e.result_revision>c.revision)) AS bad_revision,
        MAX(CASE WHEN e.kind='edit' AND e.result='accepted' AND e.result_revision >
            COALESCE((SELECT MAX(r.result_revision) FROM kanban_events r WHERE r.card_id=e.card_id AND r.kind='report-progress' AND r.result='accepted'),-1)
            THEN CASE WHEN json_valid(e.metadata_delta) THEN json_type(e.metadata_delta)<>'object' ELSE 1 END ELSE 0 END) AS bad_edit,
        MAX(CASE WHEN e.kind IN ('place','create') AND e.result='accepted' THEN location_bad(""" + event_json + """,c.revision) ELSE 0 END) OR
        COUNT(CASE WHEN e.kind IN ('place','create') AND e.result='accepted' THEN 1 END) > COUNT(DISTINCT CASE WHEN e.kind IN ('place','create') AND e.result='accepted' THEN e.result_revision END) AS bad_location,
        MAX(e.at) AS latest_event_at
        FROM kanban_events e JOIN kanban_cards c ON c.card_id=e.card_id WHERE e.card_id IN (""" + marks + ") GROUP BY e.card_id"
    for row in conn.execute(query, ids):
        flags = dict(row); ident = flags.pop('card_id'); data['summary_flags'].setdefault(ident, {}).update(flags)
        if flags['bad_location']:
            data['queue_locations'][ident] = 'unknown'
    for row in conn.execute("SELECT card_id,MAX(phase NOT IN ('reserved','submitted','stopped') OR phase IS NULL) AS bad_night FROM kanban_nights WHERE card_id IN (" + marks + ") GROUP BY card_id", ids):
        data['summary_flags'].setdefault(row['card_id'], {})['bad_night'] = row['bad_night']
    task_ids = sorted({c['task_id'] for c in cards if c.get('task_id') is not None})
    if task_ids:
        data['tasks'] = [dict(row) for row in conn.execute("SELECT " + ','.join(COLUMNS['tasks']) + " FROM tasks WHERE id IN (" + ','.join('?' for _ in task_ids) + ")", task_ids)]
    return data


def _placement_expression(conn):
    """SQL membership is canonical journal metadata, including unknown evidence."""
    import json
    conn.create_function('location_value', 2, lambda raw, revision: _location(json.loads(raw), revision))
    event_json = 'json_object(' + ','.join("'" + col + "',e." + col for col in COLUMNS['events']) + ')'
    common = " FROM kanban_events e WHERE e.card_id=c.card_id AND e.kind IN ('place','create') AND e.result='accepted'"
    value = 'location_value(' + event_json + ',c.revision)'
    return "CASE WHEN EXISTS(SELECT 1" + common + " AND " + value + "='unknown') OR EXISTS(SELECT 1" + common + " GROUP BY e.result_revision HAVING COUNT(*)>1) THEN 'unknown' ELSE COALESCE((SELECT " + value + common + " ORDER BY e.result_revision DESC,e.operation_id DESC LIMIT 1),'board') END"


def page_snapshot(home: Path, *, archived=False, backlog=False, after=None, before=None,
                  at_ms=None, detail=None) -> dict:
    """All active summaries; independent 20-card backlog/archive keyset views.

    Only a selected canonical rowid loads events/nights (50 each). Membership
    and read evidence share one readonly transaction; HTTP never writes state.
    """
    if type(archived) is not bool or type(backlog) is not bool or archived and backlog or after is not None and before is not None:
        raise ValueError('invalid view')
    for token in (after, before, detail):
        _cursor(token)
    if not archived and not backlog and (after is not None or before is not None):
        raise ValueError('main board is not paginated')
    if detail is not None and (after is not None or before is not None):
        raise ValueError('invalid detail')
    conn = None
    try:
        conn = connect(home / 'orchestrator.db', read_only=True)
        conn.execute('BEGIN')
        for key in ('cards','events','nights','tasks'):
            actual = {row[1] for row in conn.execute('PRAGMA table_info(' + TABLES[key] + ')')}
            if not set(COLUMNS[key]) <= actual:
                raise Unavailable('schema unavailable: ' + TABLES[key])
        where = "c.manual_state = 'archived'" if archived else "(c.manual_state <> 'archived' OR c.manual_state IS NULL)"
        if not archived:
            where += ' AND (' + _placement_expression(conn) + ") " + ("= 'backlog'" if backlog else "<> 'backlog'")
        cursor = before if before is not None else after
        clause = where; args = ()
        if detail is not None:
            clause += ' AND c.rowid=?'; args = (detail,)
        elif cursor is not None:
            identity = conn.execute('SELECT c.card_id FROM kanban_cards c WHERE ' + where + ' AND c.rowid=? LIMIT 1', (cursor,)).fetchone()
            if identity is None:
                raise Unavailable('page cursor unavailable')
            clause += ' AND c.card_id ' + ('<' if before is not None else '>') + ' ? COLLATE BINARY'; args = (identity['card_id'],)
        paginated = archived or backlog
        query = 'SELECT c.rowid AS _detail_token,' + ','.join('c.' + col for col in COLUMNS['cards']) + ' FROM kanban_cards c WHERE ' + clause + ' ORDER BY c.card_id COLLATE BINARY ' + ('DESC' if before is not None else 'ASC')
        rows = [dict(row) for row in conn.execute(query + (' LIMIT ?' if paginated and detail is None else ''), (*args, PAGE_SIZE + 1) if paginated and detail is None else args)]
        cards = rows[:PAGE_SIZE] if paginated and detail is None else rows
        if before is not None:
            cards.reverse()
        if detail is not None and not cards:
            raise Unavailable('card detail unavailable')
        summaries = _summaries(conn, cards)
        def exists(operator, identity):
            return conn.execute('SELECT 1 FROM kanban_cards c WHERE ' + where + ' AND c.card_id ' + operator + ' ? COLLATE BINARY LIMIT 1', (identity,)).fetchone() is not None
        has_previous = paginated and bool(cards) and exists('<', cards[0]['card_id'])
        has_next = paginated and bool(cards) and exists('>', cards[-1]['card_id'])
        selected = {c['card_id'] for c in cards}
        data = {**summaries, 'cards': cards, 'history_truncated': {}, 'summary_only': detail is None}
        data['events'] = [e for e in summaries['events'] if e['card_id'] in selected]
        data['tasks'] = [t for t in summaries['tasks'] if t['id'] in {c['task_id'] for c in cards}]
        for key in ('summary_flags','queue_locations'):
            data[key] = {ident: val for ident, val in summaries[key].items() if ident in selected}
        if detail is not None:
            card = cards[0]; flags = {}
            for key, order in (('events','result_revision DESC,at DESC,operation_id DESC'),('nights','claim_seq DESC,night_id DESC')):
                history = [dict(row) for row in conn.execute('SELECT ' + ','.join(COLUMNS[key]) + ' FROM ' + TABLES[key] + ' WHERE card_id=? ORDER BY ' + order + ' LIMIT ?', (card['card_id'], HISTORY_LIMIT + 1))]
                flags[key] = len(history) > HISTORY_LIMIT
                if key == 'events':
                    data['summary_events'] = data['events']
                data[key] = history[:HISTORY_LIMIT]
            data['history_truncated'][card['card_id']] = flags
        first = cards[0]['_detail_token'] if cards else None; last = cards[-1]['_detail_token'] if cards else None
        conn.execute('COMMIT')
        return {'available': True, 'source': str(home / 'orchestrator.db'), 'generated_at_ms': int(time.time()*1000) if at_ms is None else at_ms,
                **data, 'page': {'archived': archived, 'backlog': backlog, 'size': PAGE_SIZE if paginated else None,
                'previous': first if has_previous else None,
                'next': last if has_next else None,
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
        summaries = _summaries(conn, cards)
        conn.execute("COMMIT")
        for key, rows in data.items():
            rows.sort(key=lambda row: str(row.get({"cards":"card_id", "events":"operation_id", "nights":"night_id", "quota":"snapshot_id", "tasks":"id"}[key])))
        return {"available":True, "source":str(home / "orchestrator.db"), "generated_at_ms":int(time.time()*1000) if at_ms is None else at_ms, **data,
                "queue_locations": summaries["queue_locations"], "summary_flags": summaries["summary_flags"]}
    except (OSError, sqlite3.Error) as exc:
        raise Unavailable(str(exc)) from exc
    finally:
        if conn is not None:
            conn.close()
