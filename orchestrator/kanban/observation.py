"""Strict, fixed-source display projection; no account or admission binding."""
import json
import sqlite3
import time

ERRORS = frozenset(('timeout', 'authentication', 'rpc', 'protocol', 'invalid_quota', 'transport', 'storage', 'environment', 'interrupted'))
MAX_TIMESTAMP_MS = 253402300799999
WEEK_MS = 604800000
BASE = {'actor', 'source', 'attempted_at_ms', 'outcome', 'error'}
SUCCESS = {'observed_at_ms', 'remaining_percent', 'reset_at_ms'}


def timestamp(value):
    return type(value) is int and 0 < value <= MAX_TIMESTAMP_MS


def validate_attempt(payload):
    if not isinstance(payload, dict) or payload.get('actor') != 'quota-updater' or payload.get('source') != 'codex-cli':
        return False
    if not timestamp(payload.get('attempted_at_ms')):
        return False
    if payload.get('outcome') == 'failed':
        return set(payload) == BASE and type(payload.get('error')) is str and payload['error'] in ERRORS
    if payload.get('outcome') != 'ok' or set(payload) != BASE | SUCCESS or payload['error'] is not None:
        return False
    observed, reset, remaining = (payload[k] for k in ('observed_at_ms', 'reset_at_ms', 'remaining_percent'))
    return (timestamp(observed) and timestamp(reset) and observed <= payload['attempted_at_ms']
            and observed < reset <= observed + WEEK_MS and type(remaining) is int and 0 <= remaining <= 100)


def attempt_command(conn, operation_id, digest, payload):
    # The canonical handler has already validated and begun its writer txn.
    if payload['attempted_at_ms'] > int(time.time()*1000):
        from .commands import KanbanError
        raise KanbanError('future CLI quota attempt')
    row = conn.execute('SELECT attempted_at_ms,observed_at_ms FROM cli_quota_observation WHERE source=?', ('codex-cli',)).fetchone()
    stale = row is not None and (payload['attempted_at_ms'] <= row['attempted_at_ms'] or
            payload['outcome'] == 'ok' and row['observed_at_ms'] is not None and payload['observed_at_ms'] <= row['observed_at_ms'])
    result, reason = ('rejected', 'stale_attempt') if stale else ('accepted', None)
    if not stale:
        success = payload['outcome'] == 'ok'
        conn.execute('''INSERT INTO cli_quota_observation(source,attempted_at_ms,outcome,error,observed_at_ms,remaining_percent,reset_at_ms)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET
            attempted_at_ms=excluded.attempted_at_ms,outcome=excluded.outcome,error=excluded.error,
            observed_at_ms=COALESCE(excluded.observed_at_ms,cli_quota_observation.observed_at_ms),
            remaining_percent=COALESCE(excluded.remaining_percent,cli_quota_observation.remaining_percent),
            reset_at_ms=COALESCE(excluded.reset_at_ms,cli_quota_observation.reset_at_ms)''',
            ('codex-cli',payload['attempted_at_ms'],payload['outcome'],payload['error'],
             payload['observed_at_ms'] if success else None,payload['remaining_percent'] if success else None,payload['reset_at_ms'] if success else None))
    conn.execute('''INSERT INTO kanban_events(operation_id,payload_hash,kind,actor,at,result,reason,payload)
        VALUES(?,?,'quota-cli-attempt','quota-updater',?,?,?,?)''',
        (operation_id,digest,int(time.time()*1000),result,reason,json.dumps(payload,sort_keys=True)))
    return {'command':'quota-cli-attempt','operation_id':operation_id,'card_id':None,'result':result,
            'reason':reason,'revision':None,'recorded':True,'replayed':False}


def read_projection(conn):
    """One point lookup inside reader transaction; missing schema stays unavailable."""
    columns = 'source,attempted_at_ms,outcome,error,observed_at_ms,remaining_percent,reset_at_ms'
    try:
        from .store import SCHEMA
        validate_schema(conn, SCHEMA)
        row = conn.execute('SELECT '+columns+' FROM cli_quota_observation WHERE source=? LIMIT 1', ('codex-cli',)).fetchone()
    except (sqlite3.Error, ValueError):
        return {'available':False,'observation':None,'attempt':None}
    if row is None:
        return {'available':True,'observation':None,'attempt':None}
    data = dict(row)
    attempt = {'actor':'quota-updater','source':data['source'],'attempted_at_ms':data['attempted_at_ms'],'outcome':data['outcome'],'error':data['error']}
    if data['outcome'] == 'ok':
        attempt.update({k:data[k] for k in SUCCESS})
    if not validate_attempt(attempt):
        return {'available':False,'observation':None,'attempt':None}
    observation = None
    if data['observed_at_ms'] is not None:
        check = {**attempt,'outcome':'ok','error':None,**{k:data[k] for k in SUCCESS}}
        if not validate_attempt(check):
            return {'available':False,'observation':None,'attempt':None}
        observation = {'observed_at_ms':data['observed_at_ms'],'weekly':{'remaining_percent':data['remaining_percent'],'window_minutes':10080,'reset_at_ms':data['reset_at_ms']}}
    status = {'schema_version':1,'checked_at_ms':data['attempted_at_ms'],'outcome':data['outcome'],'error':data['error']}
    return {'available':True,'observation':observation,'attempt':status}


def validate_schema(conn, schema):
    """Exact SQL/column validation; never accept a foreign projection writer."""
    actual = conn.execute("SELECT type,name,sql FROM sqlite_master WHERE name='cli_quota_observation' OR tbl_name='cli_quota_observation'").fetchall()
    if not actual:
        return
    reference = sqlite3.connect(':memory:')
    try:
        reference.executescript(schema)
        expected = reference.execute("SELECT type,name,sql FROM sqlite_master WHERE name='cli_quota_observation' OR tbl_name='cli_quota_observation'").fetchall()
        # sqlite_master already omits CREATE's IF NOT EXISTS in both the
        # reference and target. Compare its bytes conservatively: changing
        # case or whitespace inside quoted literals changes CHECK semantics.
        objects = lambda rows: sorted(tuple(r) for r in rows)
        columns = lambda database: [tuple(r) for r in database.execute('PRAGMA table_info(cli_quota_observation)')]
        if objects(actual) != objects(expected) or columns(conn) != columns(reference):
            raise ValueError('incompatible CLI quota schema')
    finally:
        reference.close()


def validate_request(request):
    import uuid
    if not isinstance(request, dict) or set(request) != {'request_id','action','command','operation_id','payload'}:
        return False
    if request['action'] != 'kanban' or request['command'] != 'quota-cli-attempt' or not validate_attempt(request['payload']):
        return False
    for key in ('request_id','operation_id'):
        try:
            if type(request[key]) is not str or str(uuid.UUID(request[key])) != request[key]:
                return False
        except ValueError:
            return False
    return True
