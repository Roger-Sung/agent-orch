"""Pure, offline HTML from one snapshot; all source values are text."""
import json
import base64
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo
from html import escape

PANEL_SCRIPT = r"""(() => {
  const panel = document.getElementById('card-panel');
  const content = document.getElementById('panel-content');
  const closeButton = document.getElementById('close-panel');
  const backdrop = document.getElementById('panel-backdrop');
  const cards = Array.from(document.querySelectorAll('.select-card'));
  const narrow = window.matchMedia('(max-width: 760px)');
  let selected = null;
  let requestGeneration = 0;
  let controller = null;
  function syncMode() {
    const modal = narrow.matches && !panel.hidden;
    panel.setAttribute('aria-modal', modal ? 'true' : 'false');
    backdrop.hidden = !modal;
    document.getElementById('board-page').inert = modal;
    if (modal && !panel.contains(document.activeElement)) closeButton.focus();
  }
  function closePanel() {
    requestGeneration += 1;
    if (controller) controller.abort();
    panel.hidden = true;
    content.replaceChildren();
    document.body.classList.remove('panel-open');
    cards.forEach(card => card.setAttribute('aria-expanded', 'false'));
    syncMode();
    if (selected) selected.focus();
  }
  cards.forEach(card => card.addEventListener('click', async () => {
    const target = card.getAttribute('data-detail');
    const generation = ++requestGeneration;
    if (controller) controller.abort();
    selected = card;
    content.replaceChildren();
    cards.forEach(other => other.setAttribute('aria-expanded', other === card ? 'true' : 'false'));
    panel.hidden = false;
    document.body.classList.add('panel-open');
    syncMode();
    panel.scrollTop = 0;
    closeButton.focus();
    if (/^\/detail\/(board|backlog|archived)\/(0|-?[1-9][0-9]{0,18})$/.test(target)) {
      content.textContent = '載入明細…';
      controller = new AbortController();
      try {
        const response = await fetch(target, {method: 'GET', mode: 'same-origin', credentials: 'omit', cache: 'no-store', redirect: 'error', signal: controller.signal});
        if (!response.ok) throw new Error('unavailable');
        const parsed = new DOMParser().parseFromString(await response.text(), 'text/html');
        const template = parsed.querySelector('template');
        if (!template) throw new Error('unavailable');
        if (generation !== requestGeneration || panel.hidden || selected !== card) return;
        content.replaceChildren(template.content.cloneNode(true));
      } catch (error) {
        if (generation === requestGeneration && !panel.hidden && selected === card) content.textContent = '明細暫不可用，請重新整理看板。';
      }
    } else {
      const template = document.getElementById(target);
      if (template) content.replaceChildren(template.content.cloneNode(true));
    }
  }));
  closeButton.addEventListener('click', closePanel);
  document.addEventListener('keydown', event => {
    if (panel.hidden) return;
    if (event.key === 'Escape') { event.preventDefault(); closePanel(); }
    if (event.key === 'Tab' && narrow.matches) {
      const focusable = Array.from(panel.querySelectorAll('button, summary'));
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  narrow.addEventListener('change', syncMode);
  syncMode();
})();
"""

GROUPS = ("待處理", "實作中", "完成", "已驗")
EXCEPTION_GROUPS = ("待使用者", "工程受阻", "暫停待確認", "已暫停", "狀態待核對")

def _progress_event(card, event):
    """Common accepted-event validation, independent of current scope selection."""
    from .commands import PROGRESS_STATUSES, reserved_marker_in, validate_worker_observation
    def integer(value, minimum=0):
        return type(value) is int and minimum <= value <= (1 << 63) - 1
    revision = card.get('revision')
    if (not integer(revision) or not integer(card.get('approval_generation')) or
        event.get('kind') != 'report-progress' or event.get('result') != 'accepted' or
        not integer(event.get('result_revision'), 1) or event['result_revision'] > revision):
        raise ValueError('invalid progress revision')
    payload = json.loads(event.get('payload') or 'null')
    allowed = {'card_id', 'expected_revision', 'actor', 'report_status', 'summary',
               'blocker', 'decision', 'next_step', 'source_refs', 'progress_binding',
               'worker_observation', 'worker_binding'}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError('invalid progress payload')
    actor = payload.get('actor')
    if (payload.get('card_id') != card.get('card_id') or
        not integer(payload.get('expected_revision')) or payload['expected_revision'] != event['result_revision'] - 1 or
        not isinstance(actor, str) or not actor.strip() or len(actor) > 500 or actor != event.get('actor') or
        not integer(event.get('at')) or not isinstance(payload.get('report_status'), str) or payload['report_status'] not in PROGRESS_STATUSES or
        not isinstance(payload.get('summary'), str) or not payload['summary'].strip()):
        raise ValueError('invalid progress fields')
    for key in ('summary', 'blocker', 'decision', 'next_step'):
        text = payload.get(key)
        if text is not None and (not isinstance(text, str) or len(text) > 4000):
            raise ValueError('invalid progress text')
    refs = payload.get('source_refs')
    if not isinstance(refs, list) or len(refs) > 20 or any(not isinstance(r, str) or not r.strip() or len(r) > 1000 for r in refs):
        raise ValueError('invalid progress sources')
    texts = [payload.get(k) for k in ('summary', 'blocker', 'decision', 'next_step', 'actor')] + refs
    if any(reserved_marker_in(t) for t in texts if isinstance(t, str)):
        raise ValueError('unsupported progress framing')
    binding = payload.get('progress_binding')
    if not isinstance(binding, dict) or type(binding.get('schema_version')) is not int or binding['schema_version'] != 1 or not integer(binding.get('approval_generation')):
        raise ValueError('invalid progress binding')
    if 'worker_observation' in payload:
        import uuid
        if not isinstance(event.get('operation_id'), str) or str(uuid.UUID(event['operation_id'])) != event['operation_id']:
            raise ValueError('invalid worker operation')
        validate_worker_observation(payload['worker_observation'], refs)
        b = payload.get('worker_binding')
        if (not isinstance(b, dict) or set(b) != {'schema_version', 'approval_generation', 'scope_digest', 'started_revision'} or
            type(b['schema_version']) is not int or b['schema_version'] != 1 or not integer(b['approval_generation']) or
            not integer(b['started_revision'], 1) or b['started_revision'] > event['result_revision'] or
            not isinstance(b['scope_digest'], str) or len(b['scope_digest']) != 64 or any(c not in '0123456789abcdef' for c in b['scope_digest'])):
            raise ValueError('invalid worker binding')
    elif 'worker_binding' in payload:
        raise ValueError('orphan worker binding')
    return payload


def worker_scope_current(card, binding, events, *, started_revision=None):
    from .commands import progress_scope_digest
    start = binding.get('started_revision') if started_revision is None else started_revision
    if binding['approval_generation'] != card['approval_generation'] or binding.get('scope_digest') != progress_scope_digest(card):
        return False
    for event in events:
        if event.get('kind') != 'edit' or event.get('result') != 'accepted':
            continue
        revision = event.get('result_revision')
        if type(revision) is not int or not 1 <= revision <= card['revision']:
            raise ValueError('invalid scope barrier')
        if revision > start:
            delta = json.loads(event.get('metadata_delta') or 'null')
            if not isinstance(delta, dict):
                raise ValueError('unknown scope edit')
            if delta.get('scope_fields_changed'):
                return False
    return True


def _observation_event(event):
    if event.get('kind') != 'report-progress' or event.get('result') != 'accepted':
        return False
    try:
        payload = json.loads(event.get('payload') or 'null')
    except (TypeError, ValueError):
        return True
    return not isinstance(payload, dict) or 'worker_observation' in payload or 'worker_binding' in payload


def latest_worker_event(card, events):
    candidates = [e for e in events if _observation_event(e)]
    if not candidates:
        return None
    if any(type(e.get('result_revision')) is not int or not 1 <= e['result_revision'] <= card['revision'] for e in candidates) or len({e['result_revision'] for e in candidates}) != len(candidates):
        raise ValueError('worker_history_unknown')
    event = max(candidates, key=lambda e: (e['result_revision'], str(e.get('operation_id', ''))))
    payload = _progress_event(card, event)
    if 'worker_observation' not in payload:
        raise ValueError('worker_history_unknown')
    return event, payload


def worker_report(card, events):
    try:
        latest = latest_worker_event(card, events)
        if latest is None:
            return None, '未記錄 worker 觀測'
        event, payload = latest
        if not worker_scope_current(card, payload['worker_binding'], events):
            observation = payload['worker_observation']
            if observation['state'] in {'completed', 'failed', 'stopped'} and observation['handoff']['state'] == 'acknowledged':
                return None, '舊 scope 已收尾；僅保留歷史，不作目前執行證據（scope／generation 已失效）'
            return None, 'worker scope／generation 已失效；僅保留歷史，需 reconciliation'
        return {'operation_id': event['operation_id'], 'recorded_at': event['at'],
                'result_revision': event['result_revision'], **payload['worker_observation']}, '最後一次有來源的觀測；非即時監控，執行未驗證'
    except (ValueError, TypeError, KeyError, AttributeError):
        return None, 'worker 觀測未知／格式不符；不回退舊觀測'


def progress_report(card: dict, events: list[dict]) -> tuple[dict | None, str]:
    """Read existing event JSON only; malformed history never falls back."""
    unknown = '回報資料未知／格式不符，保留原始歷史'
    reports = [e for e in events if e.get('kind') == 'report-progress' and e.get('result') == 'accepted']
    if not reports:
        return None, '未記錄進度回報'
    try:
        if type(card.get('revision')) is not int or any(type(e.get('result_revision')) is not int or not 1 <= e['result_revision'] <= card['revision'] for e in reports):
            return None, unknown
        latest = max(reports, key=lambda e: (e['result_revision'], str(e.get('operation_id', ''))))
        payload = _progress_event(card, latest)
        binding = payload['progress_binding']
        if not worker_scope_current(card, binding, events, started_revision=latest['result_revision']):
            return None, '舊 scope／generation 回報，scope 已修改；只保留歷史'
        return {'operation_id': latest.get('operation_id'), 'actor': latest.get('actor'),
                'recorded_at': latest.get('at'), 'result_revision': latest['result_revision'],
                **{key: payload.get(key) for key in ('report_status', 'summary', 'blocker', 'decision', 'next_step', 'source_refs')}}, '目前 scope 的助手回報；未驗證'
    except (ValueError, TypeError, KeyError, AttributeError):
        return None, unknown


def _user_attributed(event):
    if event.get('actor') == 'USER':
        return True
    try:
        payload = json.loads(event.get('payload') or 'null')
        return isinstance(payload, dict) and payload.get('actor') == 'USER'
    except (ValueError, TypeError):
        return False


def report_presentation(card, events, flags, *, user):
    """Separate attributed records for display; never feeds writer placement."""
    result = {'state': 'missing', 'record': None, 'scope': 'unknown', 'raw_payload': None,
              'acceptance_recorded': False, 'is_acceptance': False,
              'label': '未記錄使用者驗收' if user else '未記錄助手交付／進度回報',
              'candidate_binding': '未綁目前候選' if user else None,
              'note': 'USER 僅為來源歸屬標記，不是身份驗證。' if user else '來源回報，非正式驗證證據。'}
    candidates = [e for e in events if e.get('kind') == 'report-progress' and e.get('result') == 'accepted' and
                  (_user_attributed(e) if user else e.get('actor') != 'USER')]
    if not candidates:
        return result
    try:
        if any(type(e.get('result_revision')) is not int or not 1 <= e['result_revision'] <= card['revision'] for e in candidates):
            raise ValueError('unknown revision')
        latest = max(candidates, key=lambda e: (e['result_revision'], str(e.get('operation_id', ''))))
        result['raw_payload'] = latest.get('payload')
        payload = _progress_event(card, latest)
        result['record'] = {'operation_id': latest.get('operation_id'), 'actor': latest.get('actor'),
                            'recorded_at': latest['at'], 'result_revision': latest['result_revision'],
                            'progress_binding': payload['progress_binding'],
                            **{k: payload.get(k) for k in ('report_status', 'summary', 'blocker', 'decision', 'next_step', 'source_refs')}}
        if any(flags.get(k) for k in ('bad_card', 'bad_revision', 'bad_edit')):
            raise ValueError('unknown evidence')
        current = worker_scope_current(card, payload['progress_binding'], events, started_revision=latest['result_revision'])
        result['scope'] = 'current' if current else 'historical'
        prefix = '使用者驗收通過：'
        acceptance = (user and payload['report_status'] == 'reported_done' and
                      payload['summary'].startswith(prefix) and bool(payload['summary'][len(prefix):].strip()) and
                      bool(payload.get('decision') and payload['decision'].strip()) and bool(payload['source_refs']))
        result['is_acceptance'] = bool(acceptance)
        if not current:
            result.update(state='historical', label=('歷史驗收；不適用目前 scope' if acceptance else '歷史 USER 回報；不適用目前 scope') if user else '歷史助手回報；不適用目前 scope')
        elif user:
            result.update(state='recorded' if acceptance else 'incomplete', acceptance_recorded=bool(acceptance),
                          label='已記錄使用者驗收' if acceptance else 'USER 回報／驗收資料不足')
            if not acceptance:
                result['note'] += ' 最新 USER 回報不解讀為批准或撤回。'
        else:
            delivered = payload['report_status'] == 'reported_done'
            result.update(state='delivered' if delivered else 'progress', label='助手回報交付' if delivered else '助手最新進度回報')
            if not delivered:
                result['note'] += ' 以此筆為最新進度；先前交付如有，僅屬歷史。'
        return result
    except (ValueError, TypeError, KeyError, AttributeError):
        result.update(state='unknown', scope='unknown', label='USER 驗收資料未知／格式不符' if user else '助手交付／進度資料未知／格式不符')
        result['note'] += ' 不回退較舊成功紀錄。'
        return result


def event_time_known(event: dict) -> bool:
    value = event.get("at")
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def project(data: dict) -> list[dict]:
    tasks = {t["id"]: t for t in data["tasks"]}
    result = []
    for card in data["cards"]:
        task = tasks.get(card.get("task_id"))
        status = task.get("status") if task else None
        manual = card["manual_state"]
        events = [e for e in data.get('summary_events', data["events"]) if e.get("card_id") == card["card_id"]]
        nights = [n for n in data["nights"] if n.get("card_id") == card["card_id"]]
        pending = card.get("last_reason") == "manual_pause_pending" or bool(task and task.get("stop_reason") == "manual_pause_pending")
        truncation = data.get('history_truncated', {}).get(card['card_id'], {})
        report, report_note = progress_report(card, events) if data.get('summary_flags') is not None or not truncation.get('events') else (None, '事件歷史限量；助手回報與既有決策完整性未知')
        flags = data.get("summary_flags", {}).get(card["card_id"], {})
        acceptance = report_presentation(card, events, flags, user=True)
        delivery = report_presentation(card, events, flags, user=False)
        display_report = delivery['record'] if delivery['scope'] == 'current' and delivery['state'] in {'delivered', 'progress'} else None
        worker, worker_note = worker_report(card, events) if data.get('summary_flags') is not None or not truncation.get('events') else (None, 'worker 歷史限量；觀測未知')
        if flags.get('bad_worker') or flags.get('bad_revision'):
            worker, worker_note = None, 'worker 觀測未知／歷史異常；不回退舊觀測'
        anomalies = []
        if any(flags.get(key) for key in ("bad_card", "bad_time", "bad_revision", "bad_edit", "bad_night")):
            anomalies.append("摘要證據含未知／格式不符資料")
        location = data.get("queue_locations", {}).get(card["card_id"], "board")
        if location not in {"board", "backlog"}:
            anomalies.append("位置日誌未知／格式不符")
        if manual not in {"inbox", "ready", "needs_clarification", "returned", "done", "archived"}:
            anomalies.append("卡片狀態未知")
        if card.get("task_id") and task is None:
            anomalies.append("關聯任務缺失")
        if task is not None and status not in {"queued", "running", "paused", "blocked", "waiting_user", "failed", "done", "UserReview", "user_review"}:
            anomalies.append("任務狀態未知")
        if any(n.get("phase") not in {"reserved", "submitted", "stopped"} for n in nights):
            anomalies.append("夜間狀態未知")
        if any(truncation.values()) and data.get("summary_flags") is None:
            anomalies.append("歷史限量，完整狀態未知")
        if "未知" in report_note or any(not event_time_known(e) for e in events):
            anomalies.append("回報或事件歷史無法確認")
        # The writer uses group/workflow_reason for existing placement checks.
        # Keep that contract stable; display classification carries no authority.
        workflow_reason = None
        group = "待處理"
        if manual == "archived":
            group = "封存"
        elif anomalies:
            group = "待決策"
            workflow_reason = "狀態待確認：" + "；".join(anomalies)
        elif pending or status in {"paused", "blocked", "waiting_user", "failed"} or manual in {"needs_clarification", "returned"} or report and report["report_status"] in {"blocked", "needs_decision"}:
            group = "待決策"
        elif manual == "done" or status in {"done", "UserReview", "user_review"} or report and report["report_status"] == "reported_done":
            group = "完成"
        elif status == "running" or report and report["report_status"] == "in_progress":
            group = "實作中"
        display_reason = "狀態待確認：" + "；".join(anomalies) if anomalies else None
        report_status = display_report["report_status"] if display_report else None
        decision = display_report.get("decision") if display_report else None
        raw_reason = task.get("stop_reason") if task and task.get("stop_reason") is not None else card.get("last_reason")
        display_group = "待處理"
        if manual == "archived":
            display_group = "封存"
        elif anomalies:
            display_group = "狀態待核對"
        elif delivery['state'] == 'unknown':
            display_group = "狀態待核對"
            display_reason = delivery['label']
        elif pending:
            display_group = "暫停待確認"
        elif report_status == "needs_decision" and decision and decision.strip():
            display_group = "待使用者"
        elif status in {"blocked", "failed"} or report_status == "blocked" or raw_reason == "dirty_tree":
            display_group = "工程受阻"
        elif status == "waiting_user" or report_status == "needs_decision" or manual == "needs_clarification":
            display_group = "狀態待核對"
            display_reason = "待答問題未記錄；由 coordinator 核對下一步"
        elif status == "paused":
            display_group = "已暫停"
        elif manual == "returned":
            display_group = "工程受阻"
        elif manual == "done" or status in {"done", "UserReview", "user_review"} or report_status == "reported_done":
            display_group = "完成"
        elif status == "running" or report_status == "in_progress":
            display_group = "實作中"
        result.append({"progress_report": report, "progress_report_note": report_note, "worker_observation": worker, "worker_observation_note": worker_note, "user_acceptance": acceptance, "delivery_report": delivery, "formal_verification_note": "未有可核對紀錄；現有回報不足以證明正式驗證，不表示從未執行。", "card": card, "task": task, "events": [e for e in data["events"] if e.get("card_id") == card["card_id"]], "nights": nights, "queue_location": location, "pending": "pending（尚未生效）" if pending else "未從 task reason 觀測到 pending；不保證沒有待處理請求", "group": group, "workflow_reason": workflow_reason, "display_group": display_group, "display_reason": display_reason, "completion_evidence": "使用者驗收已記錄；技術／正式驗證另列" if acceptance["acceptance_recorded"] else "未驗證", "liveness": "未知（running 不保證存活）", "updated_sources": {"card": card.get("updated_at"), "task": task.get("updated_at") if task else None, "events": flags.get("latest_event_at") if flags and not flags.get("bad_time") else max((e["at"] for e in events), default=None) if all(event_time_known(e) for e in events) else None}})
    return result

def render(data: dict, *, demo: bool = False, archived: bool = False, backlog: bool = False, lazy: bool = False) -> str:
    """Offline board with compact selectors and local-only detail panel."""
    def value(raw, *, optional=False):
        return escape(("未記錄" if optional else "未知／缺資料") if raw is None else str(raw), quote=True)

    def when(raw):
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
            return "未知／缺資料"
        try:
            return datetime.fromtimestamp(raw / 1000, ZoneInfo("Asia/Taipei")).strftime("%Y/%m/%d %H:%M:%S")
        except (ValueError, OverflowError, OSError):
            return "未知／無效時間"

    manual_names = {"inbox": "收件匣", "needs_clarification": "需要釐清", "ready": "已準備", "returned": "已退回", "done": "回報完成", "archived": "已封存"}
    task_names = {"queued": "排隊", "running": "進行中", "paused": "已暫停", "blocked": "受阻", "waiting_user": "等待使用者", "failed": "回報失敗", "done": "回報完成", "UserReview": "等待人工審查", "user_review": "等待人工審查"}
    report_names = {"not_started": "助手回報尚未開始", "in_progress": "助手回報進行中", "blocked": "助手回報受阻", "needs_decision": "助手回報待決策", "reported_done": "助手回報完成（未驗證）"}
    reason_names = {"manual_pause_pending": "暫停請求 pending（尚未生效）", "needs_user_decision": "需要使用者決策"}

    def state(raw, names):
        label = names.get(raw)
        return value(label) + ' <code>' + value(raw) + '</code>' if label else '未知狀態 <code>' + value(raw) + '</code>'

    def reason(raw):
        if raw is None:
            return "未記錄原因；是否存在卡點未知"
        return value(reason_names.get(raw, raw))

    def field(label, content):
        return '<div class="field"><dt>' + label + '</dt><dd>' + content + '</dd></div>'

    def appendix(label, raw):
        return '<details class="raw"><summary>' + label + '</summary><pre>' + escape(json.dumps(raw, ensure_ascii=False, indent=2), quote=True) + '</pre></details>'

    cards = project(data)
    if data.get('queue_locations') is not None:
        if archived:
            cards = [item for item in cards if item['display_group'] == '封存']
        else:
            cards = [item for item in cards if item['display_group'] != '封存' and (item['queue_location'] == 'backlog') == backlog]
    templates = []
    card_index = 0
    script_hash = base64.b64encode(hashlib.sha256(PANEL_SCRIPT.encode()).digest()).decode()
    styles = """
    *{box-sizing:border-box}body{margin:0;background:#f2f4f8;color:#17243a;font:15px/1.55 system-ui,sans-serif}
    header,main,footer{margin:0;padding:24px 28px}header{padding-bottom:12px}.header-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,340px);gap:20px;align-items:start}.header-intro,.header-quota{min-width:0}.header-quota .quota-section{margin:0}.header-quota .quota-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.header-quota .quota-card{margin:0;padding:12px}.header-quota .quota-card h3{font-size:14px}.header-quota .quota-card .quota-value{font-size:22px}.header-quota .quota-card .field{margin-top:6px}.header-quota .quota-card dd{font-size:12px}.header-quota .account-quota{padding:14px;margin-bottom:12px}
    h1{font-size:30px;letter-spacing:-.5px;margin:8px 0}h2{font-size:18px;margin:0}h3{font-size:18px;line-height:1.4;margin:10px 0;overflow-wrap:anywhere}
    p{margin:8px 0}.eyebrow,.muted{color:#607087;font-size:13px}.demo,.badge,.count{display:inline-block;border-radius:6px;padding:3px 8px;font-size:12px;font-weight:650;background:#e5edf9;color:#244876}
    .priority-section{border:1px solid #eddaac;background:#fff8e8;border-radius:12px;padding:16px;margin-bottom:20px}.priority-heading{display:flex;align-items:center;justify-content:space-between;gap:12px}.priority-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr));gap:16px}.priority-cards .card{margin-bottom:0}
    .archive-list{max-width:960px}.archive-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr));gap:16px}.archive-cards>h2,.archive-cards>.empty{grid-column:1/-1}.lane-exception{border-color:#eddaac;background:#fff8e8}
    .board-explanation{font-size:12px;color:#63718a;margin-top:10px}.board-explanation>summary{cursor:pointer}
    .demo{background:#fff1cb;color:#77510a}.notice{background:#fff8e8;border:1px solid #eddaac;border-radius:10px;padding:12px 16px}
    .board{display:grid;grid-template-columns:repeat(4,minmax(250px,1fr));gap:16px;align-items:start;overflow-x:auto;padding-bottom:16px}.lane{min-width:0;border:1px solid #dce2ec;background:#e9edf4;border-radius:12px;padding:14px}
    .lane-heading{display:flex;justify-content:space-between;gap:8px;align-items:center;margin-bottom:12px}.count{background:white;color:#607087}
    .card,.quota-card{min-width:0;background:#fff;border:1px solid #d8e0ec;border-radius:10px;padding:16px;margin:12px 0;box-shadow:0 2px 3px #17243a06}
    .empty{color:#718096;font-size:14px;padding:12px 0}.field{margin-top:10px}dt{font-size:12px;color:#63718a}dd{margin:2px 0 0;overflow-wrap:anywhere}dl{margin:0}
    .callout{padding:10px 12px;border-left:3px solid #c58a24;background:#fff8e8;margin:12px 0;font-size:13px}.note{white-space:pre-wrap;overflow-wrap:anywhere;color:#41516b;font-size:14px}
    .event-list{padding-left:20px;margin:8px 0}.event-list li{margin:8px 0;font-size:13px;overflow-wrap:anywhere}.event-list small{display:block;color:#63718a}
    code{font:12px ui-monospace,monospace;background:#f0f3f8;border-radius:3px;padding:1px 4px;overflow-wrap:anywhere}
    .raw{border-top:1px solid #e5e9f0;margin-top:12px;padding-top:10px;font-size:12px;color:#63718a}.raw summary{cursor:pointer}.raw pre{color:#41516b;white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}
    .quota-section{margin:0 0 24px}.header-quota .quota-section{background:#eaf2ff;border:1px solid #c5d7f4;border-radius:9px;padding:10px 12px}.quota-summary-head{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin:0}.quota-summary-head strong{font-size:24px;line-height:1.2}.quota-summary-head span{font-size:13px;font-weight:600}.quota-reset{font-size:12px;margin:4px 0}.quota-small{font-size:11px;color:#607087;margin:3px 0;overflow-wrap:anywhere}.quota-explanation{margin-top:6px;border-top:1px solid #cddbee;padding-top:5px}.quota-explanation>summary{font-size:11px;color:#607087;cursor:pointer}.quota-explanation .quota-grid{grid-template-columns:1fr}.archive-lane>summary{cursor:pointer;list-style-position:inside;font-size:18px;font-weight:650;overflow-wrap:anywhere}.archive-lane .count{margin-left:8px}.archive-note{font-size:12px;color:#63718a}.account-quota{background:#eaf2ff;border:1px solid #c5d7f4;border-radius:12px;padding:16px;margin-bottom:16px}.account-quota h3{margin:0}.account-quota .quota-value{display:inline-block;margin:8px 12px 8px 0}.quota-source{font-size:12px;color:#63718a;overflow-wrap:anywhere}.quota-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}.quota-value{font-size:28px;font-weight:700;margin:8px 0}.source{overflow-wrap:anywhere}footer{color:#63718a;font-size:12px;padding-top:0}
    .select-card{display:block;width:100%;border:0;background:transparent;color:inherit;text-align:left;cursor:pointer;padding:0;font:inherit}.select-card:focus-visible,.close-panel:focus-visible,summary:focus-visible{outline:3px solid #427ed0;outline-offset:4px}.card:has(.select-card[aria-expanded="true"]){border-color:#427ed0;box-shadow:0 0 0 2px #427ed033}
    .card-title{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;font-size:17px;font-weight:700;margin:10px 0;overflow-wrap:anywhere}.short-progress{display:block;font-size:13px;color:#41516b;margin:8px 0}.card-alert{display:block;font-size:12px;padding:6px 8px;background:#fff3da;border-radius:5px;overflow-wrap:anywhere}.card-id{display:block;color:#718096;font-size:11px;margin-top:10px;overflow-wrap:anywhere}
    .detail-panel{position:fixed;right:0;top:0;height:100dvh;width:440px;overflow-y:auto;background:#fff;border-left:1px solid #ccd5e3;box-shadow:-8px 0 24px #17243a18;padding:22px;z-index:20}.detail-panel[hidden],.panel-backdrop[hidden]{display:none}.panel-top{display:flex;align-items:center;justify-content:space-between;gap:12px}.close-panel{border:1px solid #ccd5e3;background:#f2f4f8;border-radius:6px;padding:7px 12px;color:#17243a;cursor:pointer}.panel-open header,.panel-open main,.panel-open footer{margin-right:440px}.panel-backdrop{display:none}.detail-title{font-size:23px}.detail-content{margin-top:18px}.panel-empty{color:#718096}
    @media(max-width:1000px){.header-grid{grid-template-columns:1fr;gap:18px}}@media(max-width:500px){.header-quota .quota-grid{grid-template-columns:1fr}}@media(max-width:1050px){.quota-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:760px){header,main,footer{padding:18px}.panel-open header,.panel-open main,.panel-open footer{margin-right:0}.detail-panel{width:min(100%,440px)}.panel-backdrop{display:block;position:fixed;inset:0;background:#17243a55;z-index:19}.board{grid-template-columns:repeat(4,270px)}h1{font-size:26px}}@media(max-width:500px){.quota-grid{grid-template-columns:1fr}}

    """
    def quota_panel():
        parts = []
        parts.append('<section class="quota-section" aria-label="額度觀測摘要">')
        observation = data.get('usage_observation')
        if observation is not None:
            weekly = observation.get('weekly') if isinstance(observation.get('weekly'), dict) else {}
            remaining = weekly.get('remaining_percent')
            reset = weekly.get('reset_at_ms')
            observed = observation.get('observed_at_ms')
            valid = isinstance(remaining, (int, float)) and not isinstance(remaining, bool) and 0 <= remaining <= 100 and weekly.get('window_minutes') == 10080
            freshness = '本次唯讀觀測；離線快照不保證目前餘額'
            if not isinstance(observed, int) or isinstance(observed, bool) or observed > data.get('generated_at_ms', 0):
                valid = False
                freshness = '未知／無效觀測時間'
            elif isinstance(reset, int) and data.get('generated_at_ms', 0) >= reset:
                freshness = 'reset 已過；觀測過期'
            percent = f'{remaining:g}%' if valid else '未知／缺資料'
            primary_flag = '單次快照' if freshness == '本次唯讀觀測；離線快照不保證目前餘額' else '單次快照 · ' + freshness
            parts.append('<div class="quota-summary"><p class="quota-summary-head"><span>Codex · 7 天剩餘</span><strong>' + percent + '</strong></p><p class="quota-reset">Reset：' + when(reset) + ' Asia/Taipei</p><p class="quota-small">' + primary_flag + ' · 5 小時視窗：未知／此回應未提供</p><p class="quota-small">來源：' + value(observation.get('source')) + '<br>觀測：' + when(observed) + ' Asia/Taipei</p></div>')
        else:
            parts.append('<p class="muted">未附工具額度讀值；自動額度未知。本 renderer 不呼叫帳戶或 provider。</p>')
        parts.append('<details class="quota-explanation"><summary>觀測說明／人工紀錄</summary><p class="muted">工具讀值與人工 snapshot 分開呈現；離線頁不自動更新。人工記錄是歷史觀測，並非即時額度或可執行保證。</p>')
        if observation is not None:
            parts.append('<p class="quota-small">' + freshness + '<br>' + value(observation.get('capture_mode')) + '<br>' + value(observation.get('pool_binding')) + '</p>')
        parts.append('<p class="muted">人工 quota snapshot（source=manual' + ('；以下為合成演示值' if demo else '') + '）</p><div class="quota-grid">')
        if not data['quota']:
            parts.append('<article class="quota-card"><h3>缺人工觀測；額度未知</h3><p>沒有觀測資料，不能當成 0 或空額度。</p></article>')
        for q in data['quota']:
            now = data.get('generated_at_ms')
            freshness = '未知／缺資料'
            if all(isinstance(q.get(k), int) and not isinstance(q.get(k), bool) for k in ('observed_at', 'recorded_at', 'reset_at')) and isinstance(now, int):
                freshness = '無效／未來觀測' if q['observed_at'] > now or q['recorded_at'] > now else 'reset 已過' if now >= q['reset_at'] else '過期' if q.get('stale') or now - q['observed_at'] >= 604800000 else '期限內人工觀測（非即時額度）'
            remaining = q.get('weekly_remaining_bp')
            percentage = f'{remaining / 100:.2f}%' if isinstance(remaining, int) and not isinstance(remaining, bool) and 0 <= remaining <= 10000 else '未知／缺資料'
            parts.append('<article class="quota-card"><p class="muted">人工觀測 · ' + value(q.get('snapshot_id')) + '</p><h3>' + value(q.get('pool_key')) + '</h3><p class="quota-value">' + percentage + '</p><span class="badge">' + freshness + '</span><dl>' + field('觀測時間 · Asia/Taipei', when(q.get('observed_at'))) + field('記錄時間 · Asia/Taipei', when(q.get('recorded_at'))) + field('Reset 時間 · Asia/Taipei', when(q.get('reset_at'))) + field('來源／記錄者', value(q.get('source')) + '／' + value(q.get('operator'))) + '</dl></article>')
        parts.append('</div></details></section>')
        return ''.join(parts)

    title = "進度看板" + (" — 演示資料" if demo else "")
    parts = ['<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; connect-src &#39;self&#39;; style-src &#39;unsafe-inline&#39;; script-src &#39;sha256-' + script_hash + '&#39;"><title>' + title + '</title><style>' + styles + '</style></head><body><div id="board-page">', '<header class="header-grid"><div class="header-intro"><h1>' + title + '</h1>']
    if demo:
        parts.append('<p><span class="demo">演示資料 · 合成 snapshot · 尚未接正式任務</span></p>')
    quota_content = quota_panel() if data.get('quota') or data.get('usage_observation') else '<section class="quota-section" aria-label="額度摘要"><p class="quota-summary-head"><span>額度</span><strong>暫無資料</strong></p></section>'
    quota = '<div class="header-quota">' + quota_content + '</div>'
    parts.append('<p class="muted source">更新於 ' + when(data.get('generated_at_ms')) + ' Asia/Taipei</p><details class="board-explanation"><summary>說明</summary><p class="source">資料來源：' + value(data.get('source')) + '</p><p>點選卡片查看明細；Enter／空白鍵開啟，Esc 關閉。</p><p>此頁顯示產生當下的資料，重新整理取得新快照。來源引用只作文字顯示。</p></details><noscript><p class="notice">JavaScript 未啟用，卡片明細無法開啟。</p></noscript></div>' + quota + '</header><main>')
    if archived:
        parts.append('<div class="archive-list" role="region" aria-label="封存卡片">')
    display_groups = ('Backlog',) if backlog else ('封存',) if archived else (tuple(group for group in EXCEPTION_GROUPS if any(item['display_group'] == group for item in cards)) + GROUPS)
    for group in display_groups:
        if not archived and group == GROUPS[0]:
            parts.append('<div class="board" tabindex="0" role="region" aria-label="狀態欄，可橫向捲動">')
        entries = cards if backlog else [item for item in cards if item['display_group'] == group]
        if group in EXCEPTION_GROUPS:
            parts.append('<section class="priority-section" aria-label="' + group + '"><div class="priority-heading"><h2>' + group + '</h2><span class="count">' + str(len(entries)) + ' 張</span></div><div class="priority-cards">')
        elif group in {'封存', 'Backlog'}:
            parts.append('<section class="archive-cards"><h2>' + group + '</h2>')
        else:
            parts.append('<section class="lane"><div class="lane-heading"><h2>' + group + '</h2><span class="count">' + str(len(entries)) + ' 張</span></div>')
        if not entries:
            parts.append('<p class="empty">此 snapshot 無卡片</p>')
        for item in entries:
            card = item['card']
            task = item['task']
            detail_id = ('/detail/' + ('backlog' if backlog else 'archived' if archived else 'board') + '/' + str(card['_detail_token'])) if lazy else 'card-detail-' + str(card_index)
            card_index += 1
            progress = state(task.get('status'), task_names) if task else ('任務關聯缺失；進度未知' if card.get('task_id') else '尚無回報')
            delivery = item['delivery_report']
            acceptance = item['user_acceptance']
            report = delivery['record'] if delivery['scope'] == 'current' and delivery['state'] in {'delivered', 'progress'} else None
            accepted_note = '使用者驗收已記錄；技術／正式驗證另列'
            acceptance_note = accepted_note if acceptance['acceptance_recorded'] else (acceptance['label'] + '；不由此推定目前是否驗收，技術／正式驗證另列' if acceptance['state'] != 'missing' else None)
            acceptance_summary = value(acceptance['label']) + (' · ' + value(acceptance['candidate_binding']) if acceptance['state'] != 'missing' else '')
            layers_summary = '<span class="short-progress">交付／進度：' + value(delivery['label']) + '</span><span class="short-progress">正式驗證：未有可核對紀錄</span><span class="short-progress">使用者驗收：' + acceptance_summary + '</span>'
            report_progress = value(report_names[report['report_status']]) if report else None
            if report_progress:
                progress = report_progress + (' · 引擎：' + state(task.get('status'), task_names) if task else ' · 無引擎執行證據')
            alert = ''
            secondary_alert = ''
            if report:
                first, second = ('decision', 'blocker') if report['report_status'] == 'needs_decision' else ('blocker', 'decision')
                raw_alert = report.get(first) or report.get(second)
                if raw_alert:
                    alert = value(raw_alert[:90] + ('…' if len(raw_alert) > 90 else ''))
                if report.get(first) and report.get(second) and report[first] != report[second]:
                    secondary_alert = value(report[second][:90] + ('…' if len(report[second]) > 90 else ''))
            worker = item['worker_observation']
            worker_summary = ''
            if worker:
                worker_summary = '於 ' + when(worker['observed_at_ms']) + ' 觀測到 ' + value(worker['state']) + '；非即時監控'
                if worker['handoff']:
                    worker_summary += ' · 接手：' + value(worker['handoff']['state']) + ' · ' + value(worker['handoff']['owner'], optional=True)
            warnings = []
            raw_reason = task.get('stop_reason') if task and task.get('stop_reason') is not None else card.get('last_reason')
            if item.get('display_reason'):
                warnings.append(item['display_reason'])
            if item['pending'].startswith('pending（'):
                warnings.append('暫停待確認 · pending（尚未生效）；worker 停止尚未確認')
            if item['display_group'] == '封存':
                warnings.append('封存備查；' + (acceptance_note or '完成證據未驗證'))
            if item['display_group'] == '完成':
                warnings.append(acceptance_note or '未驗收 · 完成證據未驗證')
            if raw_reason is not None:
                short = reason_names.get(raw_reason, str(raw_reason))
                warnings.append(short[:90] + ('…' if len(short) > 90 else ''))
            if task and task.get('status') == 'running' and not worker:
                warnings.append('running 不保證存活；worker 狀態未觀測')
            elif report and report['report_status'] == 'in_progress' and not worker:
                warnings.append('worker 狀態未觀測；是否仍存活未知')
            if not worker and item['worker_observation_note'] != '未記錄 worker 觀測':
                warnings.append(item['worker_observation_note'])
            if item['display_group'] == '已暫停':
                warnings.append('引擎回報已暫停；worker 停止尚未確認')
            warning = value('；'.join(warnings))
            parts.append('<article class="card"><button type="button" class="select-card" aria-controls="card-panel" aria-expanded="false" data-detail="' + detail_id + '"><span class="badge">' + value(manual_names.get(card.get('manual_state'), '未知狀態')) + '</span><span class="card-title">' + value(card.get('title')) + '</span><span class="short-progress">' + progress + '</span>' + ('<span class="short-progress">' + value(str(report.get('summary') or '未知／缺資料')[:140]) + '</span>' if report else '') + ('<span class="card-alert">' + alert + '</span>' if alert else '') + ('<span class="card-alert">' + secondary_alert + '</span>' if secondary_alert else '') + ('<span class="short-progress">' + worker_summary + '</span>' if worker_summary else '') + layers_summary + ('<span class="card-alert">' + warning + '</span>' if warning else '') + '<span class="card-id">ID：' + value(card.get('card_id')) + '</span></button></article>')
            if lazy:
                continue
            # Detail content is pre-rendered, escaped inert HTML. The fixed script
            # clones its DOM; data never enters executable JavaScript or innerHTML.
            templates.append('<template id="' + detail_id + '"><div class="detail-content"><h3 class="detail-title">' + value(card.get('title')) + '</h3><p class="muted">卡片 ID：<code>' + value(card.get('card_id')) + '</code></p><dl>')
            detail_parts = templates
            if item.get('display_reason'):
                detail_parts.append(field('狀態待確認', value(item['display_reason'])))
            detail_parts.append(field('交付／進度回報（非 USER）', value(delivery['label']) + '<br>' + value(delivery['note'])))
            if delivery['record']:
                detail_parts.append(field('交付回報適用範圍', value(delivery['scope']) + ' · revision ' + value(delivery['record']['result_revision'])))
            if delivery['state'] in {'historical', 'unknown'}:
                detail_parts.append(appendix('非 USER 原始回報（歷史／未知）', delivery['raw_payload']))
            detail_parts.append(field('正式驗證紀錄', value(item['formal_verification_note'])))
            detail_parts.append(field('使用者驗收紀錄', acceptance_summary + '<br>' + value(acceptance['note'])))
            if acceptance['record']:
                user_record = acceptance['record']
                detail_parts.append(field('USER 回報原文', value(user_record['summary'])))
                detail_parts.append(field('接受範圍（原文）' if acceptance['is_acceptance'] else 'USER 決策欄（原文）', value(user_record['decision'], optional=True)))
                detail_parts.append(field('USER 來源／記錄時間／scope', value(user_record['actor']) + ' · ' + when(user_record['recorded_at']) + ' Asia/Taipei · ' + value(acceptance['scope'])))
                detail_parts.append(field('USER 來源引用（純文字，未讀取／未驗證）', '<br>'.join(value(ref) for ref in user_record['source_refs']) if user_record['source_refs'] else '未提供；來源未知'))
            if acceptance['state'] == 'unknown':
                detail_parts.append(appendix('USER 原始回報（格式／證據待核對）', acceptance['raw_payload']))
            detail_parts.append(field('worker 觀測與證據', value(item['worker_observation_note'])))
            if worker:
                detail_parts.append(field('worker 最後觀測', worker_summary))
                detail_parts.append(field('worker 身份（來源回報，未認證）', value(worker['provider']) + ' · ' + value(worker['thread_id']) + ' · turn：' + value(worker['turn_id'], optional=True)))
                detail_parts.append(field('attempt／coordinator', value(worker['attempt_id']) + ' · ' + value(worker['coordinator'])))
                detail_parts.append(field('worker 來源（純文字，未讀取／未驗證）', value(worker['source_ref'])))
                if worker['handoff']:
                    h = worker['handoff']
                    detail_parts.append(field('終態接手／下一步', value(h['state']) + ' · ' + value(h['owner'], optional=True) + '<br>' + value(h['next_step'], optional=True)))
                    detail_parts.append(field('結果讀回來源（純文字）', value(h['result_source_ref'], optional=True)))
            if any(data.get('history_truncated', {}).get(card['card_id'], {}).values()):
                detail_parts.append('<div class="callout">本頁每卡事件／night 最多各 50 筆；此卡歷史已限量，未顯示部分的決策與狀態未知，不能當成不存在。</div>')
            source_report = delivery['record']
            if source_report:
                detail_parts.append(field('助手回報狀態', value(report_names[source_report['report_status']])))
                for key, label in (("summary", "回報摘要"), ("blocker", "回報卡點"), ("decision", "所需決策"), ("next_step", "下一步")):
                    detail_parts.append(field(label, value(source_report.get(key), optional=True)))
                detail_parts.append(field('回報來源／記錄時間', value(source_report.get('actor')) + ' · ' + when(source_report.get('recorded_at')) + ' Asia/Taipei'))
                refs = source_report.get('source_refs')
                detail_parts.append(field('來源引用（純文字，未讀取／未驗證）', '<br>'.join(value(ref) for ref in refs) if isinstance(refs, list) and refs else '未提供；來源未知'))
            engine_progress = state(task.get('status'), task_names) if task else ('關聯任務缺失；引擎執行證據未知' if card.get('task_id') else '無引擎關聯任務；執行證據未記錄')
            detail_parts.append(field('引擎原始進度（獨立於助手回報）', engine_progress))
            detail_parts.append(field('卡片原始狀態', state(card.get('manual_state'), manual_names)))
            if task and task.get('current_stage') is not None:
                detail_parts.append(field('目前階段（原始值）', value(task['current_stage'])))
            detail_parts.append(field('卡點／原因', reason(task.get('stop_reason') if task and task.get('stop_reason') is not None else card.get('last_reason'))))
            if item['pending'].startswith('pending（'):
                detail_parts.append(field('待處理請求', '暫停待確認 · pending（尚未生效）；worker 停止尚未確認'))
            if item['display_group'] == '待使用者':
                detail_parts.append(field('待使用者', value(report['decision'])))
            if item['display_group'] == '封存':
                detail_parts.append('<div class="callout">封存只代表備查分類；' + value(acceptance_note or '完成證據未驗證，不表示功能已完成') + '。</div>')
            if item['display_group'] == '完成':
                detail_parts.append('<div class="callout">' + value(acceptance_note or '未驗收：完成證據未驗證，不列入已驗') + '。</div>')
            if task and task.get('status') == 'running':
                detail_parts.append('<div class="callout">running 是原始回報；程序是否仍存活未知。</div>')
            if report and report['report_status'] == 'in_progress' and not worker:
                detail_parts.append(field('worker 觀測', 'worker 狀態未觀測；是否仍存活未知'))
            if item['display_group'] == '已暫停':
                detail_parts.append(field('暫停回報', '引擎回報已暫停；worker 停止尚未確認'))
            detail_parts.append(field('最後更新（Asia/Taipei；依來源）', '卡片：' + when(item['updated_sources']['card']) + '<br>任務：' + when(item['updated_sources']['task']) + '<br>事件：' + when(item['updated_sources']['events'])))
            detail_parts.append('</dl>')
            if card.get('note') is not None:
                detail_parts.append('<div class="field"><p class="muted">卡片備註（原文）</p><p class="note">' + value(card['note']) + '</p></div>')
            events = sorted(item['events'], key=lambda e: (0 if event_time_known(e) else 1, e['at'] if event_time_known(e) else 0, str(e.get('operation_id', ''))))
            detail_parts.append('<div class="field"><p class="muted">已記錄事件／決策</p>')
            if not events:
                detail_parts.append('<p class="muted">無關聯事件記錄；是否已有決策未知。</p>')
            else:
                if any(not event_time_known(e) for e in events):
                    detail_parts.append('<p class="muted">事件時間含未知值；已知時間順序列出，未知時間依 operation ID 列於末尾，無法確認完整時序。</p>')
                detail_parts.append('<ol class="event-list">')
                for event in events:
                    outcome = {'accepted': '已接受記錄', 'rejected': '已拒絕記錄'}.get(event.get('result'), '結果未知')
                    detail_parts.append('<li><strong>' + value(event.get('kind')) + '</strong> · ' + outcome + '<br>' + reason(event.get('reason')) + '<small>' + when(event.get('at')) + ' · actor：' + value(event.get('actor')) + ' · 原始結果：' + value(event.get('result')) + '</small></li>')
                detail_parts.append('</ol>')
            detail_parts.append('</div>' + appendix('原始卡片與關聯資料（JSON，選讀）', item) + '</div></template>')
        parts.append('</div></section>' if group in EXCEPTION_GROUPS else '</section>')
    parts.append('</div></main></div><div id="panel-backdrop" class="panel-backdrop" hidden></div><aside id="card-panel" class="detail-panel" role="dialog" aria-modal="false" aria-labelledby="panel-heading" hidden><div class="panel-top"><h2 id="panel-heading">卡片明細</h2><button type="button" id="close-panel" class="close-panel">關閉明細（Esc）</button></div><div id="panel-content"></div></aside>')
    parts.extend(templates)
    parts.append('<script>' + PANEL_SCRIPT + '</script></body></html>')
    return ''.join(parts)


def detail_fragment(data: dict, *, archived=False, backlog=False) -> str:
    """One server-escaped inert template; no executable script or raw routes."""
    import re
    html = render(data, archived=archived, backlog=backlog)
    match = re.search(r'<template id="card-detail-0">.*?</template>', html, re.S)
    if match is None:
        raise ValueError('detail unavailable')
    return match.group(0)
