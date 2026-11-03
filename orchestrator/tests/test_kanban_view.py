from orchestrator.kanban.view import project, render, PANEL_SCRIPT

def data():
    return {'source':'synthetic','generated_at_ms':1000,'cards':[],'tasks':[],'events':[],'quota':[],'nights':[]}

def test_empty_and_escape():
    d=data(); assert '暫無資料' in render(d)
    d['cards']=[{'card_id':'<script>x</script>','title':'中文長文'*100+'<img src=https://evil>', 'manual_state':'done','task_id':None,'updated_at':None,'spec_path':'javascript:alert(1)'}]
    html=render(d,demo=True)
    assert html.count('<script>') == 1 and '<img' not in html and 'href=' not in html
    assert html.split('<script>')[1].split('</script>')[0] == PANEL_SCRIPT
    assert '演示資料' in html and 'Asia/Taipei' in html
    assert project(d)[0]['display_group']=='完成'
    assert html==render(d,demo=True)

def test_unknown_pending_and_decisions():
    d=data(); d['cards']=[{'card_id':'a','title':'a','manual_state':'ready','task_id':'missing','updated_at':1}]
    assert project(d)[0]['display_group']=='狀態待核對'
    d['tasks']=[{'id':'missing','status':'paused','updated_at':2}]
    d['events']=[{'card_id':'a','at':3,'kind':'pause','result':'accepted','payload':'pending'}, {'card_id':'a','at':4,'kind':'decision','result':'rejected','reason':'needs_user_decision'}]
    assert project(d)[0]['display_group']=='已暫停'
    assert 'pending' in render(d) and 'needs_user_decision' in render(d)
    d['tasks'][0]['status']='mystery'
    assert project(d)[0]['display_group']=='狀態待核對'

def test_quota_stale_and_reported_done():
    d=data(); d['quota']=[{'snapshot_id':'q','pool_key':'test','source':'manual','observed_at':0,'recorded_at':0,'reset_at':999,'stale':0}]
    assert 'reset 已過' in render(d)
    d['quota'][0]['reset_at']=10000; d['quota'][0]['stale']=1
    assert '過期' in render(d)

import unittest
class ViewTests(unittest.TestCase):
    def test_empty(self): test_empty_and_escape()
    def test_pending(self): test_unknown_pending_and_decisions()
    def test_quota(self): test_quota_stale_and_reported_done()

class ProjectionBoundaryTests(unittest.TestCase):
    def test_running_pending_and_user_review(self):
        d=data(); d['cards']=[{'card_id':'c','title':'卡片','manual_state':'ready','task_id':'t','updated_at':1}]
        d['tasks']=[{'id':'t','status':'running','updated_at':2,'stop_reason':'manual_pause_pending'}]
        self.assertEqual('暫停待確認',project(d)[0]['display_group'])
        self.assertIn('pending（尚未生效）',render(d))
        d['tasks'][0]['stop_reason']='no_pending'
        self.assertFalse(project(d)[0]['pending'].startswith('pending（'))
        self.assertEqual('實作中',project(d)[0]['display_group'])
        d['tasks'][0]['stop_reason']=None
        d['cards'][0]['last_reason']='manual_pause_pending'
        self.assertEqual('暫停待確認',project(d)[0]['display_group'])
        d['tasks'][0]['status']='UserReview'
        self.assertEqual('暫停待確認',project(d)[0]['display_group'])
        d['cards'][0]['last_reason']=None
        self.assertEqual('完成',project(d)[0]['display_group'])
        self.assertNotEqual('已驗',project(d)[0]['display_group'])

from html.parser import HTMLParser

class BoardParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.in_details=0; self.in_template=0; self.in_code=0; self.json_outside_details=False; self.lanes=0; self.cards=0; self.open_details=False; self.visible=[]; self.detail_text=[]; self.bad_tags=[]; self.templates=[]; self.selectors=[]; self.panel_hidden=False; self.scripts=[]; self.csp=''; self.raw_outside_template=False
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag=='details':
            self.in_details+=1; self.open_details |= 'open' in attrs
            if attrs.get('class')=='raw' and not self.in_template: self.raw_outside_template=True
        if tag=='template': self.in_template+=1; self.templates.append(attrs.get('id'))
        if tag in {'script','style'}: self.in_code+=1
        if tag=='script': self.scripts.append(attrs)
        if tag=='meta' and attrs.get('http-equiv')=='Content-Security-Policy': self.csp=attrs.get('content','')
        if tag in {'section','details'} and 'lane' in attrs.get('class','').split(): self.lanes+=1
        if tag=='article' and attrs.get('class')=='card': self.cards+=1
        if tag=='button' and attrs.get('class')=='select-card': self.selectors.append(attrs)
        if tag=='aside' and attrs.get('id')=='card-panel': self.panel_hidden='hidden' in attrs
        if tag=='pre' and not self.in_details: self.json_outside_details=True
        if tag in {'iframe','img','form','a','link'} or any(k.startswith('on') or k in {'src','href'} for k in attrs): self.bad_tags.append(tag)
    def handle_endtag(self,tag):
        if tag=='details': self.in_details-=1
        if tag=='template': self.in_template-=1
        if tag in {'script','style'}: self.in_code-=1
    def handle_data(self,text):
        if self.in_template and not self.in_details: self.detail_text.append(text)
        elif not self.in_details and not self.in_code: self.visible.append(text)

class ReadableBoardTests(unittest.TestCase):
    def test_primary_card_is_human_readable(self):
        d=data(); d['cards']=[{'card_id':'case','title':'等待確認規格','manual_state':'needs_clarification','task_id':'task','updated_at':1000,'last_reason':'manual_pause_pending','note':'中文備註'}]
        d['tasks']=[{'id':'task','status':'running','current_stage':'audit','updated_at':2000,'stop_reason':'manual_pause_pending'}]
        d['events']=[{'card_id':'case','kind':'decision','at':3000,'result':'rejected','reason':'needs_user_decision','actor':'synthetic'}]
        parser=BoardParser(); parser.feed(render(d,demo=True)); visible=' '.join(parser.visible)
        self.assertEqual(4,parser.lanes); self.assertEqual(1,parser.cards)
        self.assertFalse(parser.json_outside_details); self.assertFalse(parser.open_details); self.assertEqual([],parser.bad_tags)
        for text in ['演示資料','尚未接正式任務','等待確認規格','case','實作中','進行中','pending（尚未生效）','暫停待確認','Asia/Taipei']:
            self.assertIn(text,visible)
        self.assertNotIn('"updated_sources"',visible)
        self.assertNotIn('中文備註',visible)
        self.assertNotIn('最後更新',visible)
        self.assertTrue(parser.panel_hidden)
        self.assertFalse(parser.raw_outside_template)
        detail=' '.join(parser.detail_text)
        for text in ['卡片 ID','卡點／原因','已記錄事件／決策','需要使用者決策','最後更新','1970/01/01','中文備註']: self.assertIn(text,detail)
        self.assertEqual(parser.templates,[a['data-detail'] for a in parser.selectors])
        self.assertTrue(all(a['aria-expanded']=='false' and a['type']=='button' for a in parser.selectors))
    def test_quota_visible_without_json(self):
        d=data(); d['quota']=[{'pool_key':'demo','weekly_remaining_bp':3500,'source':'manual','operator':'person','observed_at':0,'recorded_at':0,'reset_at':999,'stale':0}]
        parser=BoardParser(); parser.feed(render(d)); visible=' '.join(parser.visible)
        self.assertIn('35.00%',render(d)); self.assertIn('reset 已過',render(d)); self.assertIn('manual／person',render(d))
        d['quota'][0].pop('weekly_remaining_bp'); parser=BoardParser(); parser.feed(render(d)); self.assertIn('未知／缺資料',render(d))
    def test_raw_status_and_completion_visible(self):
        d=data(); d['cards']=[{'card_id':'x','title':'未知卡','manual_state':'novel','task_id':None,'updated_at':None}, {'card_id':'y','title':'完成卡','manual_state':'done','task_id':None,'updated_at':None}]
        parser=BoardParser(); parser.feed(render(d)); visible=' '.join(parser.visible)
        self.assertIn('完成證據未驗證',visible); self.assertIn('novel',' '.join(parser.detail_text)); self.assertIn('未知／缺資料',' '.join(parser.detail_text))

import base64
import hashlib
import json
import shutil
import subprocess

class SidebarInteractionTests(unittest.TestCase):
    def test_fixed_script_csp_and_escape(self):
        d=data(); d['cards']=[{'card_id':'" onclick="evil()','title':'</template><script>evil()</script>','manual_state':'inbox','task_id':None,'updated_at':None,'note':'<button onclick="evil()">'}]
        html=render(d); parser=BoardParser(); parser.feed(html)
        self.assertEqual(1,len(parser.scripts)); self.assertEqual({},parser.scripts[0]); self.assertEqual([],parser.bad_tags)
        digest=base64.b64encode(hashlib.sha256(PANEL_SCRIPT.encode()).digest()).decode()
        self.assertIn("script-src 'sha256-"+digest+"'",parser.csp)
        self.assertIn("default-src 'none'",parser.csp)
        self.assertNotIn('script-src \'unsafe-inline\'',parser.csp)
        self.assertEqual(PANEL_SCRIPT,html.split('<script>')[1].split('</script>')[0])
        for name in ['XMLHttpRequest', 'WebSocket', 'localStorage', 'sessionStorage', 'innerHTML', 'eval(']: self.assertNotIn(name,PANEL_SCRIPT)
        self.assertIn('repeat(4,minmax(250px,1fr))',html)
        self.assertIn('repeat(4,270px)',html)
    def test_isolated_dom_selection_close_and_keyboard(self):
        node=shutil.which('node')
        self.assertIsNotNone(node,'Node is required for isolated DOM interaction verification')
        # This is an in-memory DOM contract harness, never a browser/file URL.
        harness=r"""
const assert=require('assert'); const vm=require('vm');
let doc;
class Element {
 constructor(id){this.id=id;this.attrs={};this.hidden=false;this.handlers={};this.children=[];this.scrollTop=9;this.inert=false;this.classList={values:new Set(),add(x){this.values.add(x)},remove(x){this.values.delete(x)}};}
 setAttribute(k,v){this.attrs[k]=v} getAttribute(k){return this.attrs[k]}
 addEventListener(k,fn){this.handlers[k]=fn} focus(){doc.activeElement=this}
 replaceChildren(...nodes){this.children=nodes}
 contains(element){return [close,summary].includes(element)}
 querySelectorAll(){return [close,summary]}
}
const panel=new Element('card-panel');panel.hidden=true;
const content=new Element('panel-content'),close=new Element('close-panel'),backdrop=new Element('panel-backdrop'),page=new Element('board-page'),body=new Element('body'),summary=new Element('summary');
const first=new Element('first'),second=new Element('second');const cards=[first,second];
first.attrs['data-detail']='card-detail-0';second.attrs['data-detail']='card-detail-1';
const t0={content:{cloneNode(deep){assert.strictEqual(deep,true);return {text:'first detail',rawOpen:false}}}},t1={content:{cloneNode(){return {text:'second detail',rawOpen:false}}}};
const elements={'card-panel':panel,'panel-content':content,'close-panel':close,'panel-backdrop':backdrop,'board-page':page,'card-detail-0':t0,'card-detail-1':t1};
const media={matches:false,addEventListener(k,fn){this.change=fn}};
doc={body,activeElement:first,handlers:{},getElementById(id){return elements[id]},querySelectorAll(){return cards},addEventListener(k,fn){this.handlers[k]=fn}};
vm.runInNewContext(SOURCE,{document:doc,window:{matchMedia(){return media}}});
assert.strictEqual(panel.hidden,true);assert.strictEqual(page.inert,false);
first.handlers.click();assert.strictEqual(panel.hidden,false);assert.strictEqual(content.children[0].text,'first detail');assert.strictEqual(first.attrs['aria-expanded'],'true');assert.strictEqual(doc.activeElement,close);assert.strictEqual(panel.scrollTop,0);
second.handlers.click();assert.strictEqual(content.children.length,1);assert.strictEqual(content.children[0].text,'second detail');assert.strictEqual(first.attrs['aria-expanded'],'false');assert.strictEqual(second.attrs['aria-expanded'],'true');
close.handlers.click();assert.strictEqual(panel.hidden,true);assert.strictEqual(content.children.length,0);assert.strictEqual(doc.activeElement,second);
media.matches=true;first.handlers.click();assert.strictEqual(panel.attrs['aria-modal'],'true');assert.strictEqual(page.inert,true);assert.strictEqual(backdrop.hidden,false);
let prevented=false;doc.activeElement=summary;doc.handlers.keydown({key:'Tab',shiftKey:false,preventDefault(){prevented=true}});assert(prevented);assert.strictEqual(doc.activeElement,close);
prevented=false;doc.handlers.keydown({key:'Tab',shiftKey:true,preventDefault(){prevented=true}});assert(prevented);assert.strictEqual(doc.activeElement,summary);
doc.handlers.keydown({key:'Escape',preventDefault(){}});assert.strictEqual(panel.hidden,true);assert.strictEqual(page.inert,false);assert.strictEqual(backdrop.hidden,true);assert.strictEqual(doc.activeElement,first);
media.matches=false;second.handlers.click();doc.activeElement=first;media.matches=true;media.change();assert.strictEqual(doc.activeElement,close);assert.strictEqual(page.inert,true);
close.handlers.click();assert.strictEqual(second.attrs['aria-expanded'],'false');
console.log('isolated DOM: select/switch/close/Escape/Tab trap/responsive focus PASS');
"""
        result=subprocess.run([node,'-e','const SOURCE='+json.dumps(PANEL_SCRIPT)+';'+harness],capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertIn('PASS',result.stdout)

class ArchiveAndQuotaTests(unittest.TestCase):
    def test_archive_exact_state_and_no_completion_promotion(self):
        d=data(); d['cards']=[{'card_id':'a','title':'封存卡','manual_state':'archived','task_id':'t','updated_at':1}, {'card_id':'b','title':'回報完成','manual_state':'done','task_id':None,'updated_at':1}]
        d['tasks']=[{'id':'t','status':'done','updated_at':1}]
        self.assertEqual(['封存','完成'],[c['display_group'] for c in project(d)])
        html=render(d); parser=BoardParser(); parser.feed(html)
        self.assertEqual(4,parser.lanes);self.assertFalse(parser.open_details);self.assertTrue(parser.panel_hidden)
        self.assertNotIn('封存卡',' '.join(parser.visible))
        self.assertEqual(1,len(parser.selectors))
        archive=render(d,archived=True);ap=BoardParser();ap.feed(archive)
        self.assertEqual(0,ap.lanes);self.assertEqual(1,ap.cards)
        self.assertIn('封存卡',' '.join(ap.visible))
        self.assertNotIn('<span class="card-title">回報完成</span>',archive)
        self.assertIn('完成證據未驗證',' '.join(ap.detail_text))
        self.assertEqual(ap.templates,[a['data-detail'] for a in ap.selectors])
    def test_multiple_cards_keep_details_inert(self):
        d=data();d['cards']=[{'card_id':str(i),'title':'長標題'*i or '短','manual_state':'needs_clarification','task_id':None,'updated_at':i,'note':'完整備註'+str(i)} for i in range(5)]
        before=json.dumps(d,ensure_ascii=False)
        html=render(d);parser=BoardParser();parser.feed(html)
        self.assertEqual(5,parser.cards);self.assertEqual(5,len(parser.templates))
        self.assertEqual(1,len({c['display_group'] for c in project(d)}))
        self.assertNotIn('完整備註',' '.join(parser.visible));self.assertIn('完整備註',' '.join(parser.detail_text))
        self.assertEqual(before,json.dumps(d,ensure_ascii=False))
    def test_quota_top_distinct_sources_and_no_fake_defaults(self):
        d=data();d['usage_observation']={'source':'test-read-only','capture_mode':'工具讀值','observed_at_ms':900,'weekly':{'remaining_percent':90,'window_minutes':10080,'reset_at_ms':2000},'pool_binding':'未綁定'}
        html=render(d,demo=True)
        self.assertLess(html.index('<div class="header-intro">'),html.index('<div class="header-quota">'))
        self.assertLess(html.index('<div class="header-quota">'),html.index('<section class="quota-section"'))
        self.assertLess(html.index('<section class="quota-section"'),html.index('</header>'))
        self.assertLess(html.index('</header>'),html.index('<main>'))
        self.assertEqual(1,html.count('<section class="quota-section"'))
        self.assertIn('.header-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,340px)',html)
        self.assertIn('@media(max-width:1000px){.header-grid{grid-template-columns:1fr',html)
        self.assertIn('90%',html);self.assertIn('工具讀值',html);self.assertIn('5 小時視窗：未知',html);self.assertIn('以下為合成演示值',html)
        d['usage_observation']['weekly']['reset_at_ms']=999
        self.assertIn('reset 已過；觀測過期',render(d))
        d['usage_observation']['observed_at_ms']=1001
        self.assertIn('未知／無效觀測時間',render(d))
        d.pop('usage_observation');html=render(d)
        self.assertIn('<strong>暫無資料</strong>',html)
        self.assertNotIn('90%',html)
        self.assertNotIn('以下為合成演示值',html)

class CompactQuotaTests(unittest.TestCase):
    def test_compact_primary_and_closed_provenance(self):
        d=data();d['usage_observation']={'source':'supported-reader','capture_mode':'工具讀值','observed_at_ms':900,'weekly':{'remaining_percent':90,'window_minutes':10080,'reset_at_ms':2000},'pool_binding':'未綁定'}
        d['quota']=[{'snapshot_id':'manual-demo','pool_key':'demo','weekly_remaining_bp':3500,'source':'manual','operator':'person','observed_at':0,'recorded_at':0,'reset_at':999,'stale':0}]
        html=render(d);parser=BoardParser();parser.feed(html);visible=' '.join(parser.visible)
        for text in ['90%','Reset：','supported-reader','觀測：','單次快照','5 小時視窗：未知']:self.assertIn(text,visible)
        for text in ['35.00%','manual-demo','未綁定','Codex 帳戶額度 · 工具讀值']:self.assertNotIn(text,visible)
        self.assertIn('<details class="quota-explanation"><summary>',html)
        self.assertFalse(parser.open_details)
        self.assertIn('人工觀測',html)
        self.assertIn('離線頁不自動更新',html)
        d['usage_observation']['weekly'].pop('reset_at_ms')
        self.assertIn('Reset：未知／缺資料',render(d))
        d['usage_observation']['weekly']['reset_at_ms']=999
        parser=BoardParser();parser.feed(render(d));self.assertIn('reset 已過；觀測過期',' '.join(parser.visible))

from orchestrator.kanban.commands import progress_scope_digest
class ProgressViewTests(unittest.TestCase):
    def sample(self,status='reported_done'):
        d=data();card={'card_id':'c','title':'卡片','manual_state':'inbox','task_id':None,'updated_at':1,'approval_generation':0,'revision':1};d['cards']=[card]
        payload={'card_id':'c','expected_revision':0,'actor':'assistant:fixture','report_status':status,'summary':'<script>摘要</script>','blocker':'缺資料','decision':'確認範圍','next_step':'隔離驗證','source_refs':['javascript:alert(1)'],'progress_binding':{'schema_version':1,'approval_generation':0,'scope_digest':progress_scope_digest(card)}}
        d['events']=[{'card_id':'c','kind':'report-progress','result':'accepted','result_revision':1,'operation_id':'report','at':100,'actor':'assistant:fixture','payload':json.dumps(payload)}];return d
    def test_report_completion_remains_unverified_and_text(self):
        d=self.sample();item=project(d)[0];self.assertEqual('完成',item['display_group']);self.assertEqual('未驗證',item['completion_evidence'])
        html=render(d);parser=BoardParser();parser.feed(html)
        self.assertEqual([],parser.bad_tags);self.assertEqual(1,len(parser.scripts));self.assertIn('助手回報完成（未驗證）',' '.join(parser.visible));self.assertIn('摘要',' '.join(parser.visible))
        for text in ['assistant:fixture','來源引用（純文字，未讀取／未驗證）','確認範圍','隔離驗證','引擎原始進度','javascript:alert(1)']:self.assertIn(text,' '.join(parser.detail_text))
    def test_stale_rejected_or_malformed_reports_do_not_apply(self):
        d=self.sample();d['cards'][0]['approval_generation']=1;self.assertIsNone(project(d)[0]['progress_report'])
        d=self.sample();d['events'][0]['result']='rejected';self.assertIsNone(project(d)[0]['progress_report'])
        for raw in ['not-json','null','{}']:
            d=self.sample();d['events'][0]['payload']=raw;self.assertIsNone(project(d)[0]['progress_report']);render(d)
    def test_engine_holds_and_archive_remain_distinct(self):
        d=self.sample();d['cards'][0]['manual_state']='archived';self.assertEqual('封存',project(d)[0]['display_group'])
        d=self.sample();d['cards'][0]['task_id']='t';d['tasks']=[{'id':'t','status':'blocked','updated_at':1}]
        payload=json.loads(d['events'][0]['payload']);payload['progress_binding']['scope_digest']=progress_scope_digest(d['cards'][0]);d['events'][0]['payload']=json.dumps(payload)
        self.assertEqual('工程受阻',project(d)[0]['display_group']);self.assertEqual('blocked',project(d)[0]['task']['status']);self.assertIn('引擎：',render(d))

    def test_malformed_revisions_fail_closed_without_fallback(self):
        for revision in ['2', True, None, -1, 0, 2]:
            with self.subTest(revision=revision):
                d=self.sample();bad=dict(d['events'][0],operation_id='later',result_revision=revision)
                d['events'].append(bad)
                item=project(d)[0];self.assertIsNone(item['progress_report']);self.assertIn('未知',item['progress_report_note']);render(d)
        d=self.sample();d['events'].append({'card_id':'c','kind':'edit','result':'accepted','result_revision':'2','at':101})
        self.assertIsNone(project(d)[0]['progress_report']);render(d)
    def test_malformed_payload_fields_fail_closed(self):
        cases=[('summary',{}),('summary',''),('summary','x'*4001),('blocker',[]),('decision',False),('next_step',42),('source_refs',42),('source_refs',[{}]),('source_refs',['']),('actor',None),('card_id','other'),('expected_revision',True)]
        for key,value in cases:
            with self.subTest(key=key,value=value):
                d=self.sample();p=json.loads(d['events'][0]['payload']);p[key]=value;d['events'][0]['payload']=json.dumps(p)
                item=project(d)[0];self.assertIsNone(item['progress_report']);self.assertIn('未知',item['progress_report_note']);render(d)
        for key,value in [('schema_version',True),('approval_generation',False)]:
            d=self.sample();p=json.loads(d['events'][0]['payload']);p['progress_binding'][key]=value;d['events'][0]['payload']=json.dumps(p)
            self.assertIsNone(project(d)[0]['progress_report']);render(d)

    def test_mixed_unknown_event_times_preserve_history_without_crash(self):
        for at in ['unknown', None, True, -1]:
            with self.subTest(at=at):
                d=self.sample();d['events'].append(dict(d['events'][0],operation_id='zz-latest',at=at))
                item=project(d)[0];self.assertIsNone(item['progress_report']);self.assertIsNone(item['updated_sources']['events'])
                html=render(d);self.assertIn('無法確認完整時序',html);self.assertIn('未知／缺資料',html)
                self.assertEqual(html,render(d));self.assertEqual(at,d['events'][-1]['at'])


class ConciseHeaderTests(unittest.TestCase):
    def test_header_details_closed_and_missing_quota_not_displayed(self):
        d=data();d['source']='<private-source>&'
        html=render(d);header=html.split('<header',1)[1].split('</header>',1)[0]
        primary=header.split('<details',1)[0]
        self.assertIn('<h1>進度看板</h1>',primary)
        self.assertIn('更新於 1970/01/01 08:00:01 Asia/Taipei',primary)
        for text in ('資料來源','ASSISTANT','USER','pending','running','額度','橫向平鋪','Enter'):
            self.assertNotIn(text,primary)
        self.assertIn('<details class="board-explanation"><summary>說明</summary>',header)
        self.assertNotIn('board-explanation" open',header)
        self.assertIn('&lt;private-source&gt;&amp;',header)
        self.assertIn('<div class="header-quota">',header)
        self.assertIn('<strong>暫無資料</strong>',header)
        self.assertNotIn('<footer>',html)


class WorkflowLaneTests(unittest.TestCase):
    def sample(self,status=None):
        d=data();d['cards']=[{'card_id':'c','title':'工作','manual_state':'inbox','task_id':None,'updated_at':1}]
        if status is not None:
            d['cards'][0]['task_id']='t';d['tasks']=[{'id':'t','status':status,'updated_at':1}]
        return d
    def test_four_lanes_order_and_archive_has_no_workflow_lanes(self):
        html=render(self.sample());names=['待處理','實作中','完成','已驗']
        positions=[html.index('<h2>'+name) for name in names]
        self.assertEqual(sorted(positions),positions)
        for name in ['未知／孤立','進度回報','封存']:
            self.assertNotIn('<h2>'+name,html)
        self.assertNotIn('<section class="priority-section"',html)
        archive=self.sample();archive['cards'][0]['manual_state']='archived'
        h=render(archive,archived=True)
        for name in names:self.assertNotIn('<h2>'+name,h)
        self.assertIn('<section class="archive-cards">',h)
    def test_normal_and_explicit_engine_holds(self):
        for status,group in [(None,'待處理'),('queued','待處理'),('running','實作中'),('blocked','工程受阻'),('waiting_user','狀態待核對'),('failed','工程受阻'),('paused','已暫停'),('done','完成'),('UserReview','完成'),('mystery','狀態待核對')]:
            with self.subTest(status=status):self.assertEqual(group,project(self.sample(status))[0]['display_group'])
        self.assertIn('尚無回報',render(self.sample()))
        self.assertIn('排隊',render(self.sample('queued')))
        self.assertIn('進行中',render(self.sample('running')))
    def test_valid_reports_and_hold_priority(self):
        for status,group in [('not_started','待處理'),('in_progress','實作中'),('blocked','工程受阻'),('needs_decision','待使用者'),('reported_done','完成')]:
            d=ProgressViewTests().sample(status);self.assertEqual(group,project(d)[0]['display_group'])
        self.assertIn('尚未開始',render(ProgressViewTests().sample('not_started')))
        # Failure words alone never change a current valid in-progress report.
        d=ProgressViewTests().sample('in_progress');p=json.loads(d['events'][0]['payload']);p['summary']='error fail 已自行修正，繼續實作';d['events'][0]['payload']=json.dumps(p)
        self.assertEqual('實作中',project(d)[0]['display_group'])
        for status,group in [('blocked','工程受阻'),('waiting_user','狀態待核對'),('failed','工程受阻'),('paused','已暫停')]:
            d=ProgressViewTests().sample();d['cards'][0]['task_id']='t';d['tasks']=[{'id':'t','status':status,'updated_at':1}]
            p=json.loads(d['events'][0]['payload']);p['progress_binding']['scope_digest']=progress_scope_digest(d['cards'][0]);d['events'][0]['payload']=json.dumps(p)
            self.assertEqual(group,project(d)[0]['display_group'])
        for manual,group in [('needs_clarification','狀態待核對'),('returned','工程受阻')]:
            d=ProgressViewTests().sample('not_started');d['cards'][0]['manual_state']=manual
            self.assertEqual(group,project(d)[0]['display_group'])
        d=self.sample('done');d['cards'][0]['last_reason']='manual_pause_pending'
        self.assertEqual('暫停待確認',project(d)[0]['display_group']);self.assertIn('pending（尚未生效）',render(d))
    def test_anomalies_precede_completion_and_never_fake_verified(self):
        cases=[]
        d=self.sample('done');d['tasks']=[];cases.append(d)
        d=self.sample('done');d['nights']=[{'card_id':'c','phase':'unknown'}];cases.append(d)
        d=self.sample('done');d['nights']=[{'card_id':'c','phase':'future'}];cases.append(d)
        d=self.sample('done');d['cards'][0]['manual_state']='future';cases.append(d)
        for field in ['events','nights']:
            d=self.sample('done');d['history_truncated']={'c':{field:True}};cases.append(d)
        d=ProgressViewTests().sample();d['events'][0]['payload']='not-json';d['cards'][0]['manual_state']='done';cases.append(d)
        d=ProgressViewTests().sample();d['cards'][0]['revision']=2;d['events'].append({'card_id':'c','kind':'edit','result':'accepted','result_revision':2,'at':101,'metadata_delta':'null'});cases.append(d)
        for d in cases:
            with self.subTest(data=d):
                item=project(d)[0];self.assertEqual('狀態待核對',item['display_group']);self.assertIn('狀態待確認',item['workflow_reason']);self.assertIn('狀態待確認',render(d));self.assertEqual('未驗證',item['completion_evidence'])
        d=self.sample('done');d['cards'][0]['note']='PASS verified 已验收';self.assertEqual('完成',project(d)[0]['display_group'])
        self.assertIn('未驗收',render(d));self.assertNotEqual('已驗',project(d)[0]['display_group'])


class PrioritySectionTests(unittest.TestCase):
    def test_priority_only_on_page_when_needed_and_never_duplicate(self):
        d=WorkflowLaneTests().sample('blocked')
        d['cards'].append({'card_id':'todo','title':'一般卡','manual_state':'inbox','task_id':None,'updated_at':1})
        html=render(d);parser=BoardParser();parser.feed(html)
        self.assertEqual(4,parser.lanes);self.assertEqual(2,parser.cards)
        self.assertEqual(1,html.count('<section class="priority-section"'))
        self.assertIn('<h2>工程受阻</h2>',html)
        self.assertLess(html.index('class="priority-section"'),html.index('class="board"'))
        self.assertEqual(2,len(parser.selectors));self.assertEqual(2,len(set(parser.templates)))
        self.assertEqual(1,html.count('<h2>工程受阻</h2>'))
        self.assertNotIn('全部待決策',html)
        clean=render(WorkflowLaneTests().sample('running'))
        self.assertNotIn('<section class="priority-section"',clean)
        archive=WorkflowLaneTests().sample('blocked');archive['cards'][0]['manual_state']='archived'
        self.assertNotIn('class="priority-section"',render(archive,archived=True))


class RestoredQuotaTests(unittest.TestCase):
    def test_missing_quota_keeps_compact_right_slot_without_fake_number(self):
        d=data();html=render(d);header=html.split('<header',1)[1].split('</header>',1)[0]
        quota=header.split('<div class="header-quota">',1)[1]
        self.assertIn('<span>額度</span><strong>暫無資料</strong>',quota)
        for text in ('0%','90%','未公開人工額度','未附工具額度','來源：','Reset：'):
            self.assertNotIn(text,quota)
        self.assertLess(header.index('header-intro'),header.index('header-quota'))
        self.assertIn('.header-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,340px)',html)
        self.assertIn('@media(max-width:1000px){.header-grid{grid-template-columns:1fr',html)


class HonestStatusTests(unittest.TestCase):
    def sample(self, status='blocked', *, task_status=None, reason=None, **fields):
        d=ProgressViewTests().sample(status)
        if task_status is not None:
            d['cards'][0]['task_id']='t'
            d['tasks']=[{'id':'t','status':task_status,'stop_reason':reason,'updated_at':1}]
        else:
            d['cards'][0]['last_reason']=reason
        payload=json.loads(d['events'][0]['payload'])
        payload.update(fields)
        payload['progress_binding']['scope_digest']=progress_scope_digest(d['cards'][0])
        d['events'][0]['payload']=json.dumps(payload)
        return d

    def parsed(self, d, **options):
        parser=BoardParser();parser.feed(render(d,**options));return parser
    def test_needs_decision_question_first_and_blocker_retained_full_lazy(self):
        question='選哪個範圍？ <img src=x onerror="evil()">'
        blocker='缺少授權 <script>evil()</script>'
        for status,first,second in (('needs_decision',question,blocker),('blocked',blocker,question)):
            d=self.sample(status,decision=question,blocker=blocker);d['cards'][0]['_detail_token']=1
            for lazy in (False,True):
                with self.subTest(status=status,lazy=lazy):
                    parser=self.parsed(d,lazy=lazy);visible=' '.join(parser.visible)
                    self.assertIn(first,visible);self.assertIn(second,visible);self.assertLess(visible.index(first),visible.index(second))
                    self.assertEqual([],parser.bad_tags);self.assertEqual(1,len(parser.scripts))

    def test_display_classification_preserves_writer_placement_contract(self):
        for status,expected in (('blocked','工程受阻'),('failed','工程受阻'),('waiting_user','狀態待核對'),('paused','已暫停')):
            with self.subTest(status=status):
                d=self.sample('not_started',task_status=status,decision=None)
                item=project(d)[0]
                self.assertEqual('待決策',item['group'])
                self.assertIsNone(item['workflow_reason'])
                self.assertEqual(expected,item['display_group'])
        d=self.sample('needs_decision',decision=None)
        self.assertEqual('待決策',project(d)[0]['group'])
        self.assertIsNone(project(d)[0]['workflow_reason'])
        self.assertIn('待答問題未記錄',project(d)[0]['display_reason'])

    def test_engineering_holds_do_not_invent_a_human_question(self):
        for status in ('blocked','failed','waiting_user'):
            with self.subTest(status=status):
                d=self.sample('not_started',task_status=status,reason='dirty_tree',decision=None)
                self.assertEqual('工程受阻',project(d)[0]['display_group'])
                visible=' '.join(self.parsed(d).visible)
                self.assertIn('工程受阻',visible)
                self.assertNotIn('<h2>待使用者</h2>',render(d))
                self.assertIn('dirty_tree',visible)
        d=self.sample('in_progress',summary='dirty_tree mentioned in prose; already fixed',decision=None)
        self.assertEqual('實作中',project(d)[0]['display_group'])
        # A reason with a similar spelling is not the known machine code.
        d=self.sample('not_started',task_status='waiting_user',reason='dirty_tree-ish',decision=None)
        self.assertEqual('狀態待核對',project(d)[0]['display_group'])

    def test_question_requires_explicit_report_and_recorded_text(self):
        d=self.sample('needs_decision',task_status='blocked',decision='要保留哪一個範圍？',blocker=None)
        self.assertEqual('待使用者',project(d)[0]['display_group'])
        for text in ('待使用者','要保留哪一個範圍？'):
            self.assertIn(text,' '.join(self.parsed(d).visible))
        for decision in (None,'','   '):
            with self.subTest(decision=decision):
                d=self.sample('needs_decision',decision=decision)
                self.assertEqual('狀態待核對',project(d)[0]['display_group'])
        # Text in an ordinary report is not itself a decision request.
        d=self.sample('blocked',decision='無使用者待答；由工程 owner 處理')
        self.assertEqual('工程受阻',project(d)[0]['display_group'])

    def test_optional_null_is_unrecorded_and_not_an_explicit_no(self):
        d=self.sample('not_started',blocker=None,decision=None,next_step=None)
        parser=self.parsed(d);detail=' '.join(parser.detail_text)
        self.assertIn('所需決策 未記錄',detail)
        self.assertIn('回報卡點 未記錄',detail)
        self.assertIn('下一步 未記錄',detail)
        self.assertNotIn('關聯任務缺失',detail)
        self.assertIsNone(project(d)[0]['workflow_reason'])
        self.assertEqual('待處理',project(d)[0]['display_group'])
        d=self.sample('not_started',decision='明確不需決策',blocker=None,next_step=None)
        self.assertIn('所需決策 明確不需決策',' '.join(self.parsed(d).detail_text))
        d['cards'][0]['task_id']='missing'
        self.assertEqual('狀態待核對',project(d)[0]['display_group'])
        self.assertIn('關聯任務缺失',render(d))

    def test_specific_blocker_precedes_warnings_in_lazy_and_full_cards(self):
        blocker='engine owner：確認受阻原因 <script>evil()</script>'
        d=self.sample('in_progress',task_status='running',reason='manual_pause_pending',blocker=blocker,decision='需要選範圍')
        d['cards'][0]['_detail_token']=7
        self.assertEqual('暫停待確認',project(d)[0]['display_group'])
        for lazy in (False,True):
            with self.subTest(lazy=lazy):
                parser=self.parsed(d,lazy=lazy);visible=' '.join(parser.visible)
                self.assertLess(visible.index(blocker),visible.index('pending（尚未生效）'))
                self.assertIn('worker 停止尚未確認',visible)
                self.assertIn('running 不保證存活',visible)
                self.assertNotIn('已暫停',visible)
                self.assertEqual([],parser.bad_tags)
        d['summary_flags']={'c':{'bad_time':True}}
        self.assertEqual('狀態待核對',project(d)[0]['display_group'])
        visible=' '.join(self.parsed(d).visible)
        self.assertLess(visible.index(blocker),visible.index('狀態待確認'))
        self.assertIn('pending（尚未生效）',visible)

    def test_archive_and_unverified_delivery_keep_blockers_and_warnings(self):
        for manual,expected in (('archived','封存'),('inbox','完成')):
            d=self.sample('reported_done',blocker='正式部署尚未執行')
            d['cards'][0]['manual_state']=manual
            parser=self.parsed(d,archived=manual=='archived');visible=' '.join(parser.visible)
            self.assertEqual(expected,project(d)[0]['display_group'])
            self.assertIn('正式部署尚未執行',visible)
            self.assertIn('完成證據未驗證',visible)
            self.assertLess(visible.index('正式部署尚未執行'),visible.index('完成證據未驗證'))
        d['cards'][0]['manual_state']='archived';d['summary_flags']={'c':{'bad_time':True}}
        self.assertEqual('封存',project(d)[0]['display_group'])
        self.assertIn('狀態待確認',render(d,archived=True))

    def test_in_progress_is_a_report_without_worker_liveness_evidence(self):
        d=self.sample('in_progress',blocker=None,decision=None)
        parser=self.parsed(d)
        self.assertEqual('實作中',project(d)[0]['display_group'])
        for text in ('助手回報進行中','無引擎執行證據','worker 狀態未觀測','是否仍存活未知'):
            self.assertIn(text,' '.join(parser.visible))
        self.assertIn('worker 狀態未觀測',' '.join(parser.detail_text))
        d=self.sample('not_started',task_status='paused')
        self.assertEqual('已暫停',project(d)[0]['display_group'])
        self.assertIn('worker 停止尚未確認',' '.join(self.parsed(d).visible))

    def test_new_alert_surfaces_escape_decision_and_keep_fixed_csp(self):
        text='</template><img src=x onerror="evil()"> & "question"'
        d=self.sample('needs_decision',blocker=None,decision=text)
        html=render(d);parser=self.parsed(d)
        self.assertIn(text,' '.join(parser.visible))
        self.assertEqual([],parser.bad_tags)
        self.assertEqual(1,len(parser.scripts))
        digest=base64.b64encode(hashlib.sha256(PANEL_SCRIPT.encode()).digest()).decode()
        self.assertIn("script-src 'sha256-"+digest+"'",parser.csp)
        self.assertIn("default-src 'none'",parser.csp)
        self.assertEqual(PANEL_SCRIPT,html.split('<script>')[1].split('</script>')[0])
        self.assertLess(html.index('header-quota'),html.index('</header>'))
        self.assertLess(html.index('</header>'),html.index('<main>'))
        self.assertEqual(html,render(d))
    def test_provided_observation_retains_time_and_expiry(self):
        d=data();d['usage_observation']={'source':'synthetic-reader','capture_mode':'工具讀值','observed_at_ms':900,'weekly':{'remaining_percent':90,'window_minutes':10080,'reset_at_ms':2000},'pool_binding':'未綁定'}
        fresh=render(d);self.assertIn('90%',fresh);self.assertIn('觀測：',fresh);self.assertNotIn('<strong>暫無資料</strong>',fresh)
        d['usage_observation']['weekly']['reset_at_ms']=999
        stale=render(d);self.assertIn('reset 已過；觀測過期',stale);self.assertIn('觀測：',stale)


from orchestrator.tests import test_kanban_commands as worker_fixtures
from orchestrator.tests.test_kanban_commands import worker_sample,worker_handoff

class WorkerViewTests(unittest.TestCase):
    setUp=worker_fixtures.WorkerObservationTests.setUp
    payload=worker_fixtures.WorkerObservationTests.payload
    send=worker_fixtures.WorkerObservationTests.send
    card=worker_fixtures.WorkerObservationTests.card
    emit=worker_fixtures.WorkerObservationTests.emit
    advance=worker_fixtures.WorkerObservationTests.advance
    def snapshot(self):
        from orchestrator.kanban.read import page_snapshot
        return page_snapshot(self.home)
    def test_observed_running_time_source_and_handoff_are_separate_and_escaped(self):
        attack='</template><img src=x onerror="evil()">'
        o=worker_sample(thread_id=attack,coordinator='owner '+attack,source_ref='dispatch:'+attack);r=self.emit(o)
        o=self.advance(r,o,state='running',observed_at_ms=200);r=self.emit(o)
        d=self.snapshot();parser=BoardParser();parser.feed(render(d));visible=' '.join(parser.visible)
        self.assertIn('觀測到 running',visible);self.assertIn('1970/01/01',visible);self.assertIn('非即時監控',visible)
        self.assertNotIn('worker 狀態未觀測',visible);self.assertIn('dispatch:'+attack,' '.join(parser.detail_text));self.assertEqual([],parser.bad_tags)
        h={**worker_handoff(True),'owner':'next '+attack,'next_step':'review '+attack,'result_source_ref':'read:'+attack}
        o=self.advance(r,o,state='failed',source_ref='terminal:'+attack,handoff=h);self.assertEqual('accepted',self.emit(o)['result'])
        html=render(self.snapshot());parser=BoardParser();parser.feed(html);detail=' '.join(parser.detail_text)
        for text in ('觀測到 failed','acknowledged','next '+attack,'review '+attack,'read:'+attack):self.assertIn(text,detail)
        self.assertEqual([],parser.bad_tags);self.assertEqual(1,len(parser.scripts))
        digest=base64.b64encode(hashlib.sha256(PANEL_SCRIPT.encode()).digest()).decode();self.assertIn("script-src 'sha256-"+digest+"'",parser.csp)
        self.assertEqual(PANEL_SCRIPT,html.split('<script>')[1].split('</script>')[0])
    def test_pause_pending_is_distinct_from_observed_stopped(self):
        o=worker_sample();r=self.emit(o)
        self.assertEqual('accepted',self.send({'card_id':'c','expected_revision':1,'actor':'owner'},command='pause')['result'])
        page=self.snapshot();item=project(page)[0];self.assertEqual('暫停待確認',item['display_group']);self.assertEqual('submitted',item['worker_observation']['state'])
        self.assertNotIn('觀測到 stopped',render(page))
        o=self.advance(r,o,state='stopped',handoff=worker_handoff(),source_ref='synthetic:terminal-read');self.assertEqual('accepted',self.emit(o)['result'])
        page=self.snapshot();item=project(page)[0];self.assertEqual('暫停待確認',item['display_group']);self.assertEqual('stopped',item['worker_observation']['state'])
        html=render(page);self.assertIn('觀測到 stopped',html);self.assertIn('pending（尚未生效）',html)


class AcceptanceViewTests(unittest.TestCase):
    setUp=worker_fixtures.WorkerObservationTests.setUp
    payload=worker_fixtures.WorkerObservationTests.payload
    send=worker_fixtures.WorkerObservationTests.send
    card=worker_fixtures.WorkerObservationTests.card
    emit=worker_fixtures.WorkerObservationTests.emit
    advance=worker_fixtures.WorkerObservationTests.advance
    def accept(self,**changes):
        fields={'expected_revision':self.card()['revision'],'actor':'USER','report_status':'reported_done','summary':'使用者驗收通過：synthetic acceptance','decision':'接受目前範圍；正式驗證另列','source_refs':['synthetic:user'],**changes}
        return self.send(self.payload(**fields))
    def snapshot(self):
        from orchestrator.kanban.read import page_snapshot
        return page_snapshot(self.home)
    def parser(self,d,**options):
        parser=BoardParser();parser.feed(render(d,**options));return parser
    def test_legacy_acceptance_requires_full_affirmative_grammar(self):
        from orchestrator.tests.test_kanban_read_page import legacy_acceptance
        fields=legacy_acceptance();summary=fields['summary'];decision=fields['decision']
        self.assertEqual('accepted',self.accept(**fields)['result'])
        self.assertTrue(project(self.snapshot())[0]['user_acceptance']['acceptance_recorded'])
        cases=[
            {'summary':summary.replace('本次條件已核對通過','本次條件尚未通過')},
            {'summary':summary.replace('本次條件已核對通過','若本次條件核對通過')},
            {'summary':summary.replace('本次條件已核對通過','本次條件待核對通過')},
            {'decision':decision.replace('條件接受','條件不接受')},
            {'decision':decision.replace('USER 依','USER 若依')},
            {'decision':decision.replace('條件接受','條件待接受')},
            {'decision':'「'+decision+'」'},
            {'decision':decision+'等待驗收。'},
            {'decision':decision.replace('T1.3','T1.4')},
            {'decision':decision.replace('保留完全不可達分支未驗、有限容量與未發布限制','保留 ')},
            {'summary':summary.replace('確認都沒問題就可以關單','不同意接受')},
            {'summary':summary+'但尚未接受。'},
            {'summary':summary.split('受控驗收。')[0]+'受控驗收。   '},
            {'summary':'USER reported_done 接受'},
            {'source_refs':[]}, {'blocker':'待接受'}, {'next_step':'等待驗收'},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertEqual('accepted',self.accept(**legacy_acceptance(**changes))['result'])
                record=project(self.snapshot())[0]['user_acceptance']
                self.assertFalse(record['acceptance_recorded']);self.assertEqual('incomplete',record['state'])
        # The established format is unaffected, and does not fall back to legacy.
        self.accept();self.assertTrue(project(self.snapshot())[0]['user_acceptance']['acceptance_recorded'])
    def test_legacy_USER_escaping_CSP_lazy_and_detail_keep_unverified_axes(self):
        from orchestrator.tests.test_kanban_read_page import legacy_acceptance
        from orchestrator.kanban.read import page_snapshot
        from orchestrator.kanban.view import detail_fragment
        attack='</template><img src=x onerror="evil()">'
        fields=legacy_acceptance();fields['summary']+=attack;fields['source_refs'].append('javascript:'+attack)
        self.assertEqual('accepted',self.accept(**fields)['result']);d=self.snapshot()
        for lazy in (False,True):
            parser=self.parser(d,lazy=lazy);html=render(d,lazy=lazy)
            self.assertEqual([],parser.bad_tags);self.assertEqual(1,len(parser.scripts));self.assertIn('已記錄使用者驗收',' '.join(parser.visible))
            self.assertEqual(PANEL_SCRIPT,html.split('<script>')[1].split('</script>')[0])
            digest=base64.b64encode(hashlib.sha256(PANEL_SCRIPT.encode()).digest()).decode();self.assertIn("script-src 'sha256-"+digest+"'",parser.csp)
        detail=page_snapshot(self.home,detail=d['cards'][0]['_detail_token']);parser=BoardParser();parser.feed(detail_fragment(detail))
        text=' '.join(parser.detail_text)
        for value in (attack,'javascript:'+attack,'未綁目前候選','未有可核對紀錄','不是身份驗證','未發布限制'):self.assertIn(value,text)
        self.assertEqual([],parser.bad_tags);self.assertEqual([],parser.scripts)
    def test_legacy_display_does_not_change_writer_group_or_placement(self):
        from orchestrator.tests.test_kanban_read_page import legacy_acceptance
        self.send(self.payload());self.accept(**legacy_acceptance());item=project(self.snapshot())[0]
        self.assertTrue(item['user_acceptance']['acceptance_recorded']);self.assertEqual('完成',item['group'])
        self.assertIsNone(item['workflow_reason']);self.assertEqual('實作中',item['display_group'])
        placement=self.send({'card_id':'c','expected_revision':2,'actor':'owner','destination':'backlog','user_request':'synthetic selection'},command='place')
        self.assertEqual('backlog_requires_effective_pending',placement['reason'])
    def test_acceptance_delivery_and_formal_evidence_are_distinct(self):
        self.assertEqual('accepted',self.accept()['result'])
        self.assertEqual('accepted',self.send(self.payload(expected_revision=1,report_status='reported_done',summary='Candidate delivery only'))['result'])
        d=self.snapshot();p=project(d)[0];parser=self.parser(d);visible=' '.join(parser.visible);detail=' '.join(parser.detail_text)
        self.assertEqual('delivered',p['delivery_report']['state']);self.assertTrue(p['user_acceptance']['acceptance_recorded']);self.assertEqual('完成',p['display_group'])
        for text in ('助手回報交付','已記錄使用者驗收','未綁目前候選','正式驗證：未有可核對紀錄','使用者驗收已記錄；技術／正式驗證另列'):self.assertIn(text,visible)
        self.assertNotIn('未驗收',visible);self.assertNotIn('未驗收',detail);self.assertIn('USER 僅為來源歸屬標記，不是身份驗證',detail)
        self.assertIn('不表示從未執行',detail);self.assertNotEqual('已驗',p['display_group'])
    def test_latest_USER_does_not_masquerade_as_assistant_delivery_or_change_writer(self):
        self.assertEqual('accepted',self.send(self.payload())['result']);self.assertEqual('accepted',self.accept()['result'])
        d=self.snapshot();p=project(d)[0]
        self.assertEqual('完成',p['group']);self.assertIsNone(p['workflow_reason']) # unchanged writer classification
        self.assertEqual('實作中',p['display_group']);self.assertEqual('in_progress',p['delivery_report']['record']['report_status'])
        visible=' '.join(self.parser(d).visible);self.assertIn('助手回報進行中',visible);self.assertIn('已記錄使用者驗收',visible)
        placement=self.send({'card_id':'c','expected_revision':2,'actor':'owner','destination':'backlog','user_request':'synthetic selection'},command='place')
        self.assertEqual('backlog_requires_effective_pending',placement['reason'])
    def test_later_nonacceptance_USER_report_does_not_imply_withdrawal(self):
        self.accept();self.accept(report_status='in_progress',summary='最新 USER 進度說明')
        self.assertEqual('accepted',self.send(self.payload(expected_revision=2,report_status='reported_done'))['result'])
        p=project(self.snapshot())[0];self.assertFalse(p['user_acceptance']['acceptance_recorded']);self.assertEqual('incomplete',p['user_acceptance']['state'])
        parser=self.parser(self.snapshot());visible=' '.join(parser.visible);detail=' '.join(parser.detail_text)
        self.assertIn('USER 回報／驗收資料不足',visible);self.assertIn('不由此推定目前是否驗收',visible)
        self.assertNotIn('未驗收',visible);self.assertNotIn('未驗收',detail);self.assertIn('不解讀為批准或撤回',detail)
    def test_USER_text_escaping_CSP_lazy_and_detail(self):
        attack='</template><img src=x onerror="evil()">'
        # Build a canonical fixture event, retaining the existing USER protocol.
        p=self.payload(actor='USER',report_status='reported_done',summary='使用者驗收通過：'+attack,decision=attack,source_refs=['javascript:'+attack])
        self.assertEqual('accepted',self.send(p)['result']);d=self.snapshot()
        for lazy in (False,True):
            parser=self.parser(d,lazy=lazy);html=render(d,lazy=lazy)
            self.assertEqual([],parser.bad_tags);self.assertEqual(1,len(parser.scripts));self.assertIn('已記錄使用者驗收',' '.join(parser.visible))
            digest=base64.b64encode(hashlib.sha256(PANEL_SCRIPT.encode()).digest()).decode();self.assertIn("script-src 'sha256-"+digest+"'",parser.csp)
            self.assertEqual(PANEL_SCRIPT,html.split('<script>')[1].split('</script>')[0])
        from orchestrator.kanban.read import page_snapshot
        from orchestrator.kanban.view import detail_fragment
        detail=page_snapshot(self.home,detail=d['cards'][0]['_detail_token']);fragment=detail_fragment(detail);parser=BoardParser();parser.feed(fragment)
        self.assertEqual([],parser.bad_tags);self.assertEqual([],parser.scripts);self.assertIn(attack,' '.join(parser.detail_text));self.assertIn('javascript:'+attack,' '.join(parser.detail_text))
    def test_scope_closed_hint_is_not_a_new_reconciliation_todo(self):
        o=worker_sample();first=self.emit(o);o=self.advance(first,o,state='completed',source_ref='synthetic:terminal-read',handoff=worker_handoff(True));terminal=self.emit(o)
        self.assertEqual('accepted',self.send({'card_id':'c','expected_revision':2,'actor':'owner','fields':{'repo_path':'B'}},command='edit')['result'])
        d=self.snapshot();p=project(d)[0];self.assertIsNone(p['worker_observation']);self.assertIn('舊 scope 已收尾',p['worker_observation_note']);self.assertNotIn('reconciliation',p['worker_observation_note'])
        self.assertIn('舊 scope 已收尾',' '.join(self.parser(d).visible));self.assertNotIn('需 reconciliation',' '.join(self.parser(d).detail_text))
        new=worker_sample(supersedes_attempt_id=o['attempt_id'],previous_observation_operation_id=terminal['operation_id']);self.assertEqual('accepted',self.emit(new)['result'])
        self.assertEqual('accepted',self.send({'card_id':'c','expected_revision':4,'actor':'owner','fields':{'repo_path':'C'}},command='edit')['result'])
        p=project(self.snapshot())[0];self.assertIsNone(p['worker_observation']);self.assertIn('需 reconciliation',p['worker_observation_note'])
