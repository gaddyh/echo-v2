"""Echo Operations Center admin dashboard page (vanilla JS, no framework).

Renders the /api/debug/guard/admin snapshot: one page that answers the
operational health of the entire Echo platform in five seconds, then
drills into any problem. Strictly admin-only (wls session cookie +
GUARD_OPERATIONS_ADMIN_USER_IDS); parent-facing Guard Activity stays a
separate page. Read-only — the dashboard never enqueues WAHA management
commands or makes provider HTTP calls.
"""

ADMIN_DASHBOARD_PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Echo Operations Center</title>
<style>
:root{font-family:Inter,system-ui,sans-serif;color:#17202a;background:#f3f6f8;line-height:1.4}*{box-sizing:border-box}body{margin:0}
.wrap{max-width:1440px;margin:auto;padding:24px 18px 60px}h1,h2,h3,p{margin:0}
.muted{color:#6d7b86;font-size:.84rem}.tiny{font-size:.74rem;color:#8b98a3}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:14px;flex-wrap:wrap}
.controls{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.controls select,.controls input,.controls button{border:1px solid #cbd5dc;border-radius:8px;background:#fff;padding:7px 10px;font:inherit;font-size:.84rem}
.controls button{cursor:pointer}.controls button.active{background:#2a78d0;color:#fff;border-color:#2a78d0}
.badge{display:inline-flex;align-items:center;gap:6px;padding:6px 12px;border-radius:999px;font-size:.8rem;font-weight:700}
.badge.healthy{background:#e4f6ec;color:#176b43}.badge.warning{background:#fdf3e0;color:#9a6300}.badge.critical{background:#fdeaea;color:#a32020}.badge.unknown,.badge.unavailable{background:#eef1f3;color:#5d6b76}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(158px,1fr));gap:10px;margin:14px 0}
.card,.panel{background:#fff;border:1px solid #dbe3e8;border-radius:14px;box-shadow:0 2px 8px #1b34400b}
.card{padding:13px 14px}.card strong{display:block;font-size:1.5rem;margin-top:2px}.card span{font-size:.76rem;color:#6d7b86}.card.warn strong{color:#b45b00}.card.bad strong{color:#c23b3b}.card.good strong{color:#16834d}
.panel{padding:16px 18px;margin-top:16px}.panel h2{font-size:1rem;margin-bottom:4px}.panel h2 .muted{font-weight:400;margin-left:8px}
.row{display:flex;gap:18px;flex-wrap:wrap}.row>div{flex:1;min-width:280px}
.table{width:100%;border-collapse:collapse;font-size:.82rem;margin-top:8px}
.table th,.table td{text-align:left;padding:7px 8px;border-bottom:1px solid #edf0f2;vertical-align:top;white-space:nowrap}
.table th{cursor:pointer;user-select:none;color:#48555f;font-size:.76rem;text-transform:uppercase;letter-spacing:.02em}
.table th:hover{color:#17202a}.table tr:hover td{background:#f8fafb}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:.72rem;font-weight:600}
.pill.healthy,.pill.connected_processing,.pill.connected,.pill.ok,.pill.completed,.pill.success,.pill.processed,.pill.sent{background:#e4f6ec;color:#176b43}
.pill.warning,.pill.degraded,.pill.pairing_required,.pill.retryable,.pill.pending,.pill.scheduled,.pill.claimed,.pill.processing,.pill.starting,.pill.provisioning,.pill.inactive{background:#fdf3e0;color:#9a6300}
.pill.critical,.pill.failed,.pill.terminal,.pill.exhausted,.pill.disconnected,.pill.blocked,.pill.suspended,.pill.stale,.pill.unknown,.pill.unavailable,.pill.error{background:#fdeaea;color:#a32020}
.pill.na{background:#eef1f3;color:#7c8a94}
.mono{font-family:ui-monospace,monospace;font-size:.74rem;word-break:break-all}
.empty{padding:16px 0;color:#6d7b86}
.chart{display:flex;align-items:end;gap:3px;padding:14px 2px 26px;border-bottom:1px solid #dbe3e8;overflow-x:auto}
.bar{min-width:9px;background:#2a78d0;border-radius:3px 3px 0 0;position:relative;flex:1;max-width:34px}
.bar.amber{background:#e0a13c}.bar.red{background:#d06060}.bar.green{background:#3aa56d}.bar.gray{background:#b6c2ca}
.bar span{position:absolute;bottom:-22px;left:50%;transform:translateX(-50%);font-size:9px;color:#8b98a3;white-space:nowrap}
.legend{display:flex;gap:12px;flex-wrap:wrap;margin-top:6px}.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
details summary{cursor:pointer;font-weight:600;padding:6px 0;color:#2a78d0}
.tooltip{border-bottom:1px dotted #9aa7b0;cursor:help}
.na{color:#9aa7b0;font-style:italic}
.health-issue{padding:8px 12px;border-radius:10px;margin-top:8px;font-size:.84rem}
.health-issue.critical{background:#fdeaea;color:#a32020}.health-issue.warning{background:#fdf3e0;color:#9a6300}
.user-detail{background:#f8fafb;border:1px solid #e6ecf0;border-radius:10px;padding:12px;margin:8px 0}
.err-filters{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
.err-filters select,.err-filters button{border:1px solid #cbd5dc;border-radius:8px;background:#fff;padding:6px 10px;font:inherit;font-size:.82rem}
.counters{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.counters .chip{background:#f2f6f8;border:1px solid #e2e9ee;border-radius:8px;padding:6px 10px;font-size:.78rem}
.counters .chip b{display:block;font-size:.95rem}
@media(max-width:900px){.table{display:block;overflow-x:auto}.row>div{min-width:100%}}
</style></head><body><main class="wrap">
<header class="top"><div><h1>Echo Operations Center</h1><p class="muted" id="sub">Loading operational snapshot…</p></div>
<div class="controls">
<select id="period" title="Historical window for windowed counters (live sections always show current state)"><option value="15m">15 min</option><option value="1h">1 hour</option><option value="24h" selected>24 hours</option><option value="7d">7 days</option><option value="30d">30 days</option></select>
<select id="provider"><option value="all">All providers</option><option value="waha">WAHA</option><option value="baileys">Baileys</option></select>
<select id="worker"><option value="">All workers</option></select>
<input id="user" placeholder="User UUID" size="26" title="Filter per-user table by user id">
<button id="refresh" title="Refresh now">Refresh</button>
<button id="pause" title="Pause / resume auto-refresh (30s)">Pause auto</button>
<span class="tiny" id="clock"></span>
</div></header>
<div id="health" class="badge unknown">…</div>
<section id="overview" class="grid"></section>
<div id="issues"></div>

<section class="panel"><h2>Provider health</h2><p class="muted">Persisted connection state per provider; messages are canonical ingested rows. N/A means not measurable for that provider — never zero.</p><div id="providers"></div></section>
<section class="panel"><h2>WAHA infrastructure</h2><p class="muted">Echo-assigned session counts are authoritative; agent-reported counts are observational. Resource metrics appear once agents report them.</p>
<div id="waha-summary" class="grid"></div><div id="workers"></div></section>
<section class="panel"><h2>Per-user monitoring health</h2><p class="muted">Monitoring health and message inactivity are separate concepts. Click a row for operational detail (no message content).</p><div id="users"></div><div id="user-detail"></div></section>
<section class="panel"><h2>Message ingestion and processing</h2><p class="muted">Durable receipt vs. processed vs. canonical ingestion. Sync/catch-up rows are labeled, not counted as live latency.</p>
<div class="row"><div><h3>Throughput by provider</h3><div id="chart-throughput" class="chart"></div></div><div><h3>Inbox outcomes</h3><div id="chart-outcomes" class="chart"></div></div></div>
<div id="ingestion"></div><details><summary>Latency percentiles</summary><div id="latency"></div></details>
<details><summary>Batch delivery counters and rejected requests</summary><div id="batches"></div></details>
<details><summary>Backlog gauges (ops_metrics samples)</summary><div id="backlog-history" class="chart"></div></details></section>
<section class="panel"><h2>Media processing</h2><p class="muted">Retrieval / decryption / enrichment / transcription classified by typed errors. Success rate denominator: media with an enrichment row.</p><div id="media"></div></section>
<section class="panel"><h2>Guard analysis</h2><p class="muted">Committed assessments vs. AI call attempts. Decision distributions use the current analyzer schemas (parent_should_know / significant_exposure).</p><div id="guard"></div></section>
<section class="panel"><h2>Parent notification delivery</h2><p class="muted">"Sent" = provider API acceptance. No device delivery receipts exist anywhere in Echo — do not read it as delivered-to-device.</p><div id="notifications"></div></section>
<section class="panel"><h2>WAHA session management and onboarding</h2><p class="muted">Command lifecycle, session lifecycle, onboarding timings. Read-only — no management actions.</p><div id="management"></div></section>
<section class="panel"><h2>AI usage and costs</h2><p class="muted">estimated_cost_usd is list-price exposure from pricing.yaml, not the billed amount (incentivized tier).</p>
<div id="ai-overview" class="grid"></div><div id="ai-costs"></div></section>
<section class="panel"><h2>Recent errors and operational incidents</h2>
<div class="err-filters"><select id="err-sub"><option value="">All subsystems</option></select><select id="err-sev"><option value="">All severities</option><option value="retryable">Retryable</option><option value="terminal">Terminal</option></select><button id="err-more">Load more</button></div>
<div id="err-aggregates" class="counters"></div><div id="errors"></div></section>
<p class="tiny" style="margin-top:18px">UTC internally; times shown in your local timezone. Live sections (workers, backlogs, commands) always reflect current state regardless of the selected window.</p>
</main><script>
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&','<':'<','>':'>','"':'"',"'":'&#39;'}[c]));
const fmt=v=>v?new Date(v).toLocaleString():'—';
const ago=v=>{if(!v)return null;const s=(Date.now()-new Date(v).getTime())/1000;if(s<0)return'just now';if(s<3600)return Math.round(s/60)+'m ago';if(s<86400)return Math.round(s/3600)+'h ago';return Math.round(s/86400)+'d ago'};
const num=(v,d=0)=>v===null||v===undefined?'<span class="na">N/A</span>':(typeof v==='number'?v.toLocaleString(undefined,{maximumFractionDigits:d}):esc(v));
const usd=v=>v===null||v===undefined?'<span class="na">N/A</span>':'$'+Number(v).toFixed(4);
const secs=v=>v===null||v===undefined?'<span class="na">N/A</span>':(v<10?Number(v).toFixed(1)+'s':Math.round(v)+'s');
const pill=(v,extra='')=>v===null||v===undefined?'<span class="pill na">N/A</span>':`<span class="pill ${esc(v)} ${extra}">${esc(v)}</span>`;
const cell=v=>v===null||v===undefined?'<span class="na">N/A</span>':esc(v);
let state={paused:false,users:[],errors:[],errOffset:0,sortUsers:{key:null,dir:1}};
const $=id=>document.getElementById(id);

async function loadSnapshot(){
  const q=new URLSearchParams({period:$('period').value,provider:$('provider').value});
  if($('worker').value)q.set('worker_id',$('worker').value);
  if($('user').value.trim())q.set('user_id',$('user').value.trim());
  const r=await fetch('/api/debug/guard/admin?'+q);
  if(!r.ok){$('sub').textContent=r.status===401?'Admin access denied':'Snapshot failed ('+r.status+')';return}
  const d=await r.json();render(d);
}
async function loadErrors(append){
  const q=new URLSearchParams({hours:24,limit:50,offset:append?state.errOffset:0});
  if($('err-sub').value)q.set('subsystem',$('err-sub').value);
  if($('err-sev').value)q.set('severity',$('err-sev').value);
  const r=await fetch('/api/debug/guard/admin/errors?'+q);if(!r.ok)return;
  const d=await r.json();
  if(!append){state.errors=[];state.errOffset=0}
  state.errors=state.errors.concat(d.errors||[]);state.errOffset+=d.errors?d.errors.length:0;
  renderErrors(d);
}
async function loadUserDetail(uid){
  const r=await fetch('/api/debug/guard/admin/users/'+encodeURIComponent(uid));
  $('user-detail').innerHTML=r.ok?renderUserDetail(await r.json()):'<p class="empty">Detail unavailable.</p>';
}

function render(d){
  const s=d.sections,h=d.health||{status:'unknown',issues:[]};
  $('sub').textContent=`Generated ${fmt(d.generated_at)} · window ${d.period} · ${d.filters.provider==='all'?'all providers':d.filters.provider}`;
  $('clock').textContent=state.paused?'auto-refresh paused':'';
  const hb=$('health');hb.className='badge '+(h.status||'unknown');
  hb.textContent=(h.status||'unknown').toUpperCase()+(h.unavailable_sections&&h.unavailable_sections.length?` · ${h.unavailable_sections.length} section(s) unavailable`:'');
  $('issues').innerHTML=(h.issues||[]).map(i=>`<div class="health-issue ${i.severity}"><b>${esc(i.severity.toUpperCase())}</b> · ${esc(i.subsystem)} · ${esc(i.category)} — ${esc(i.message)}${i.worker_id?` <span class="mono">(${esc(i.worker_id)})</span>`:''}</div>`).join('');
  renderOverview(s.overview||{});
  renderProviders(s.providers||{});
  renderWaha(s.waha_infrastructure||{});
  renderUsers(s.users||{});
  renderIngestion(s.ingestion||{});
  renderMedia(s.media||{});
  renderGuard(s.guard_analysis||{});
  renderNotifications(s.notifications||{});
  renderManagement(s.management||{});
  renderAI(s.ai_costs||{});
}
function unavailable(name){return `<p class="empty">Section ${esc(name)} is currently unavailable.</p>`}

function windowLabel(){
  const v=$('period')?$('period').value:'24h';
  return {'15m':'15 min','1h':'1 hour','24h':'24 hours','7d':'7 days','30d':'30 days'}[v]||v;
}
function renderOverview(o){
  const cards=[
    ['Monitored users',o.monitored_users],['Connected',o.connected_users,'good'],['Disconnected',o.disconnected_users,'warn'],['Awaiting pairing',o.pairing_required_users,'warn'],['Stale status',o.stale_status_users,'warn'],
    ['Healthy workers',o.healthy_workers,'good'],['Unavailable workers',o.unavailable_workers,'bad'],
    [`Messages (${windowLabel()})`,o.messages_received_period],['Pending processing (now)',o.messages_pending_processing,'warn'],['Failed exhausted',o.messages_failed_exhausted,'bad'],
    [`Guard analyses (${windowLabel()})`,o.guard_analyses_committed_period],['Overdue guard chats',o.overdue_guard_chats,'warn'],
    [`Notifications sent (${windowLabel()})`,o.notifications_sent_period,'good'],[`Failed (${windowLabel()})`,o.notifications_failed_period,'bad'],
    [`AI cost (${windowLabel()})`,o.estimated_ai_cost_period===null||o.estimated_ai_cost_period===undefined?null:'$'+Number(o.estimated_ai_cost_period).toFixed(4)]
  ];
  $('overview').innerHTML=cards.map(([label,v,cls])=>`<div class="card ${v>0&&cls==='bad'?'bad':v>0&&cls==='warn'?'warn':cls==='good'?'good':''}"><span>${esc(label)}</span><strong>${v===null||v===undefined?'<span class="na">N/A</span>':num(v)}</strong></div>`).join('');
}
function table(headers,rows){
  if(!rows.length)return'<p class="empty">No data.</p>';
  return `<table class="table"><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table>`;
}
function renderProviders(p){
  if(!p.available){$('providers').innerHTML=unavailable('providers');return}
  $('providers').innerHTML=table(['Provider','Users','Connected','Pairing','Provisioning','Degraded/blocked','Stale','Messages (window)','Incoming','Phone-sent','Group','Last WhatsApp','Last ingest','Device→ingest p50/p95*','Pending backlog','Failed','Oldest pending'],
    (p.providers||[]).map(x=>`<tr><td><b>${esc(x.provider)}</b></td><td>${num(x.users)}</td><td>${num(x.connected)}</td><td>${num(x.pairing_required)}</td><td>${num(x.provisioning)}</td><td>${num(x.degraded_or_blocked)}</td><td>${num(x.stale)}</td><td>${num(x.messages_period)}</td><td>${num(x.incoming_period)}</td><td>${num(x.phone_sent_period)}</td><td>${num(x.group_messages_period)}</td><td>${fmt(x.last_whatsapp_at)}</td><td>${fmt(x.last_ingested_at)}</td><td>${secs(x.device_to_ingest_p50_s)} / ${secs(x.device_to_ingest_p95_s)}</td><td>${num(x.pending_backlog)}</td><td>${num(x.failed_backlog)}</td><td>${ago(x.oldest_pending_at)||'—'}</td></tr>`))
    +`<p class="tiny">* Device timestamp → Echo insert; affected by device clock skew and historical sync. ${esc(p.provider_neutrality_note||'')}</p>`;
}
function renderWaha(w){
  if(!w.available){$('waha-summary').innerHTML='';$('workers').innerHTML=unavailable('WAHA infrastructure');return}
  const s=w.worker_summary||{};
  $('waha-summary').innerHTML=[['Registered',s.registered],['Healthy',s.healthy,'good'],['Starting',s.starting],['Degraded',s.degraded,'warn'],['Stale',s.stale,'bad'],['Mgmt capable',s.management_capable],['Eligible capacity',s.eligible_capacity],['Assigned (eligible)',s.assigned_sessions],['Remaining capacity',s.remaining_capacity,s.capacity_exhausted?'bad':'good']]
    .map(([l,v,c])=>`<div class="card ${v>0&&c==='bad'?'bad':v>0&&c==='warn'?'warn':c==='good'?'good':''}"><span>${esc(l)}</span><strong>${num(v)}</strong></div>`).join('');
  const workers=w.workers||[];
  const sel=$('worker');const cur=sel.value;
  const ids=['',...workers.map(x=>x.worker_id)];
  if(sel.options.length!==ids.length||[...sel.options].every((o,i)=>o.value!==ids[i])){sel.innerHTML=ids.map(i=>`<option value="${esc(i)}">${i===''?'All workers':esc(i)}</option>`).join('');sel.value=cur}
  $('workers').innerHTML=table(['Worker','Region','Agent','Mgmt proto','Health','Derived','Heartbeat','First registered','Max','Assigned*','Reported','Util','Mismatch','Agent backlog','Pending cmds','Expired leases','Failed cmds','Oldest cmd','Pending effects','Resources'],
    workers.map(x=>`<tr><td class="mono">${esc(x.worker_id)}</td><td>${cell(x.region)}</td><td class="mono">${cell(x.agent_version)}</td><td>${num(x.management_protocol)}</td><td>${pill(x.reported_health)}</td><td>${pill(x.derived_availability)}</td><td>${ago(x.heartbeat_age_seconds!==null&&x.heartbeat_age_seconds!==undefined?new Date(Date.now()-x.heartbeat_age_seconds*1000).toISOString():null)||'—'}</td><td>${fmt(x.first_registered_at)}</td><td>${num(x.max_sessions)}</td><td>${num(x.assigned_sessions)}</td><td>${num(x.reported_active_sessions)}</td><td>${x.capacity_utilization===null||x.capacity_utilization===undefined?'<span class="na">N/A</span>':Math.round(x.capacity_utilization*100)+'%'}</td><td>${num(x.session_count_mismatch)}</td><td>${num(x.reported_pending_backlog)}</td><td>${num(x.pending_commands)}</td><td>${num(x.expired_command_leases)}</td><td>${num(x.failed_commands)}</td><td>${ago(x.oldest_outstanding_command_at)||'—'}</td><td>${num(x.sessions_with_pending_effects)}</td><td>${resourcesCell(x)}</td></tr>`))
    +`<p class="tiny">* ${esc((w.notes||{}).assigned_vs_reported||'')} ${esc((w.notes||{}).resources||'')}</p>`;
}
function resourcesCell(x){
  if(!x.resources_available)return'<span class="na">N/A</span>';
  const r=x.resources||{};const pct=r.vm_cpu_percent,mem=r.vm_ram_total_bytes?Math.round(100*(1-(r.vm_ram_available_bytes||0)/r.vm_ram_total_bytes)):null,disk=r.disk_total_bytes?Math.round(100*(r.disk_used_bytes||0)/r.disk_total_bytes):null;
  let parts=[];
  if(pct!==undefined&&pct!==null)parts.push('CPU '+Math.round(pct)+'%');
  if(mem!==null&&mem!==undefined)parts.push('RAM '+mem+'%');
  if(disk!==null&&disk!==undefined)parts.push('Disk '+disk+'%');
  if(r.waha_version)parts.push('WAHA '+r.waha_version);
  if(r.last_batch_delivery_at)parts.push('batch '+ago(new Date(r.last_batch_delivery_at*1000).toISOString()));
  return `<details><summary>metrics</summary><div class="tiny">observed ${fmt(x.resources_observed_at)}<br>${Object.entries(r).map(([k,v])=>esc(k)+': '+esc(v)).join('<br>')}</div></details>`;
}
function pipelineStates(x){
  // Four separate operational states — a connected session is not verified,
  // and verification is not monitoring. N/A for states that do not apply.
  const chip=(label,v)=>{
    if(v===null||v===undefined)return `<span class="na" title="${label}: unknown">?</span>`;
    return v?`<span class="pill good" title="${label}: yes">${label[0]}✓</span>`:`<span class="pill bad" title="${label}: no">${label[0]}✗</span>`;
  };
  return chip('Connected',x.whatsapp_connected)+chip('Identity',x.account_identity_available)+chip('Verified',x.child_verified)+chip('Guard',x.guard_monitoring_active);
}
function renderUsers(u){
  if(!u.available){$('users').innerHTML=unavailable('per-user health');return}
  state.users=u.users||[];
  const rows=sortedUsers().map(x=>`<tr style="cursor:pointer" onclick="loadUserDetail('${esc(x.user_id)}')"><td><b>${esc(x.display_name)}</b><br><span class="tiny mono">${esc(x.user_id).slice(0,8)}…</span></td><td>${pill(x.provider)}</td><td>${x.worker_id?esc(x.worker_id)+(x.worker_available===false?' ⚠':''):'—'}</td><td>${pill(x.persisted_status)}</td><td class="tiny">${pipelineStates(x)}</td><td>${pill(x.monitoring_health)}</td><td class="tiny">${esc(x.monitoring_health_reason||'')}</td><td>${secs(x.status_age_seconds)}</td><td>${num(x.messages_period)}</td><td>${num(x.incoming_period)}</td><td>${num(x.phone_sent_period)}</td><td>${num(x.group_messages_period)}</td><td>${fmt(x.last_whatsapp_at)}</td><td>${fmt(x.last_ingested_at)}</td><td>${num(x.inbox_pending)}</td><td>${num(x.inbox_failed_retryable)}</td><td>${num(x.inbox_failed_exhausted)}</td><td>${ago(x.oldest_pending_at)||'—'}</td><td>${num(x.media_discovered_period)}/${num(x.media_failed_period)}</td><td>${fmt(x.last_security_analysis_at)}</td><td>${fmt(x.last_exposure_analysis_at)}</td><td>${num(x.scheduled_analyses)} (${num(x.overdue_analyses)})</td><td>${num(x.alerts_period)}</td><td>${num(x.notifications_sent_period)}</td><td class="tiny">${x.message_activity}</td></tr>`);
  $('users').innerHTML=`<p class="tiny">Click a column header to sort. Click a row for detail.</p>`+table(['User','Provider','Worker','Status','Conn/ID/Verif/Guard','Monitoring health','Reason','Status age','Msgs','In','Phone-sent','Group','Last WhatsApp','Last ingest','Inbox pending','Retryable','Exhausted','Oldest pending','Media disc/failed','Last security','Last exposure','Sched (overdue)','Alerts','Notifs sent','Activity'],rows)+`<p class="tiny">${esc(u.notes||'')}</p>`;
}
function sortedUsers(){
  const {key,dir}=state.sortUsers;if(!key)return state.users;
  return [...state.users].sort((a,b)=>{const av=a[key],bv=b[key];if(av===bv)return 0;if(av===null||av===undefined)return 1;if(bv===null||bv===undefined)return -1;return (av<bv?-1:1)*dir});
}
document.addEventListener('click',e=>{
  const th=e.target.closest('#users th');if(!th)return;
  const headers=[...e.target.closest('thead').children[0].children].map(x=>x.textContent);
  const keyMap=['display_name','provider','worker_id','persisted_status','monitoring_health','monitoring_health_reason','status_age_seconds','messages_period','incoming_period','phone_sent_period','group_messages_period','last_whatsapp_at','last_ingested_at','inbox_pending','inbox_failed_retryable','inbox_failed_exhausted','oldest_pending_at','media_discovered_period','last_security_analysis_at','last_exposure_analysis_at','scheduled_analyses','alerts_period','notifications_sent_period','message_activity'];
  const idx=headers.indexOf(th.textContent);if(idx<0||!keyMap[idx])return;
  const key=keyMap[idx];
  state.sortUsers={key,dir:state.sortUsers.key===key?-state.sortUsers.dir:1};renderUsers({available:true,users:state.users,notes:''});
});
function renderUserDetail(d){
  if(!d.available)return`<div class="user-detail"><p class="empty">${esc(d.reason||'unavailable')}</p></div>`;
  const u=d.user,c=(d.connections||[])[0]||{};
  return `<div class="user-detail"><b>${esc(u.first_name||'User')}</b> <span class="tiny mono">${esc(u.user_id)}</span> · ${pill(c.connection_status)} · ${esc(c.provider||'—')}<br>
  <span class="tiny">Connected since ${fmt(c.created_at)} · last authenticated ${fmt(c.last_authenticated_at)}</span>
  <div class="counters">${(d.messages_7d||[]).map(m=>`<div class="chip">${new Date(m.day).toLocaleDateString()}<b>${num(m.messages)} msgs (${num(m.incoming)} in / ${num(m.phone_sent)} sent)</b></div>`).join('')||'<p class="empty">No messages in 7 days.</p>'}</div>
  <div class="counters">${(d.inbox&&d.inbox.waha_inbox||[]).map(i=>`<div class="chip">inbox ${esc(i.status)}<b>${num(i.count)}</b></div>`).join('')}</div>
  <div class="tiny">Security 7d: ${num(d.analyses_7d&&d.analyses_7d.security&&d.analyses_7d.security.committed)} committed (${num(d.analyses_7d&&d.analyses_7d.security&&d.analyses_7d.security.parent_should_know_true)} yes / ${num(d.analyses_7d&&d.analyses_7d.security&&d.analyses_7d.security.parent_should_know_false)} no / ${num(d.analyses_7d&&d.analyses_7d.security&&d.analyses_7d.security.parent_should_know_null)} uncertain: ${num(d.analyses_7d&&d.analyses_7d.security&&d.analyses_7d.security.parent_should_know_semantic_uncertain)} semantic / ${num(d.analyses_7d&&d.analyses_7d.security&&d.analyses_7d.security.parent_should_know_operational_uncertain)} operational) · Exposure: ${num(d.analyses_7d&&d.analyses_7d.exposure&&d.analyses_7d.exposure.committed)} (${num(d.analyses_7d&&d.analyses_7d.exposure&&d.analyses_7d.exposure.significant_true)} yes / ${num(d.analyses_7d&&d.analyses_7d.exposure&&d.analyses_7d.exposure.significant_false)} no / ${num(d.analyses_7d&&d.analyses_7d.exposure&&d.analyses_7d.exposure.significant_null)} uncertain)</div>
  ${(d.analyses_7d&&d.analyses_7d.pending_chats||[]).map(ch=>`<div class="tiny mono">${esc(ch.chat_name||ch.chat_id)} ${ch.overdue?'<b style="color:#b45b00">overdue</b>':''} pending Δ${ch.pending_version_delta}</div>`).join('')}
  <div class="tiny">Notifications 7d: ${((d.notifications_7d&&d.notifications_7d.security_outcomes)||[]).concat((d.notifications_7d&&d.notifications_7d.exposure_outcomes)||[]).map(o=>esc(o.status)+': '+o.count).join(', ')||'none'} · Incidents: ${num(d.notifications_7d&&d.notifications_7d.incidents&&d.notifications_7d.incidents.total)}</div>
  <p class="tiny">Full analysis history: <a href="/debug/guard/operations" target="_blank">Guard Activity →</a></p></div>`;
}
function barChart(el,series,opts){
  const el2=$(el);if(!el2)return;
  const entries=Object.entries(series);
  if(!entries.length){el2.innerHTML='<p class="empty">No data yet.</p>';return}
  const max=Math.max(1,...entries.map(([,v])=>Array.isArray(v)?Math.max(...v):v));
  el2.innerHTML=entries.map(([label,vals])=>{const arr=Array.isArray(vals)?vals:[vals];
    return `<div class="bar ${opts&&opts.cls?opts.cls(label):''}" style="height:${Math.max(6,Math.round(arr[0]/max*140))}px" title="${esc(label)}: ${arr.map(v=>num(v)).join(' / ')}"><span>${esc(label)}</span></div>`}).join('');
}
function renderIngestion(i){
  if(!i.available){$('ingestion').innerHTML=unavailable('ingestion');return}
  const w=i.waha_inbox||{},b=i.baileys_inbox||{},t=i.trends||{};
  const per=w.period||{},cur=w.current||{},bd=w.batch_delivery||{};
  $('ingestion').innerHTML=`<div class="row"><div><h3>WAHA inbox (window)</h3>${table(['Unique rows','Real messages','Non-real events','Incoming','Phone-sent','Pending','Processing','Processed','Retryable failed','Exhausted failed','Expired leases','Revised rows','Oldest retryable','Oldest exhausted'],
    [`<tr><td>${num(per.unique_rows)}</td><td>${num(per.real_messages)}</td><td>${num(per.non_real_events)}</td><td>${num(per.incoming)}</td><td>${num(per.phone_sent)}</td><td>${num(per.pending)}</td><td>${num(per.processing)}</td><td>${num(per.processed)}</td><td>${num(per.failed_retryable)}</td><td>${num(per.failed_exhausted)}</td><td>${num(per.expired_leases)}</td><td>${num(per.revised_rows)}</td><td>${fmt(per.oldest_retryable_at)}</td><td>${fmt(per.oldest_exhausted_at)}</td></tr>`])}</div>
  <div><h3>WAHA inbox (current)</h3>${table(['Pending','Processing','Retryable','Exhausted','Expired leases','Oldest pending','Unknown session rows'],
    [`<tr><td>${num(cur.pending)}</td><td>${num(cur.processing)}</td><td>${num(cur.failed_retryable)}</td><td>${num(cur.failed_exhausted)}</td><td>${num(cur.expired_leases)}</td><td>${fmt(cur.oldest_pending_at)}</td><td>${num(cur.unknown_session_rows)}</td></tr>`])}</div>
  <div><h3>Baileys inbox</h3><p class="tiny">Connector-owned inbox + Echo processing ledger.</p>${table(['Event type','Received (window)','Unprocessed','Oldest unprocessed','Last received'],
    (b.period||[]).map(x=>`<tr><td>${esc(x.event_type)}</td><td>${num(x.received)}</td><td>${num(x.unprocessed)}</td><td>${fmt(x.oldest_unprocessed_at)}</td><td>${fmt(x.last_received_at)}</td></tr>`))}
  ${table(['Ledger status','Total','Exhausted','Last updated','Claim→processed p50/p95'],
    (b.ledger||[]).map(x=>`<tr><td>${pill(x.status)}</td><td>${num(x.total)}</td><td>${num(x.exhausted)}</td><td>${fmt(x.last_updated_at)}</td><td>${secs(x.claim_to_processed_p50_s)} / ${secs(x.claim_to_processed_p95_s)}</td></tr>`))}
  <p class="tiny">Canonical ingestion (device ts → Echo insert) p50 ${secs(b.receipt_to_canonical_p50_s)} / p95 ${secs(b.receipt_to_canonical_p95_s)} over ${num(b.receipt_to_canonical_samples)} samples.</p></div></div>
  <p class="tiny">${esc(w.notes||'')} ${esc(b.equivalence_notes||'')}</p>`;
  const tp={};(t.throughput||[]).forEach(x=>{const k=new Date(x.bucket).toLocaleString([], {month:'numeric',day:'numeric',hour:x.bucket&&String(x.bucket).length>13?'2-digit':undefined,minute:x.bucket&&String(x.bucket).length>13?'2-digit':undefined})||x.bucket;const p=x.provider;tp[k+' '+p]=(tp[k+' '+p]||0)+x.messages});
  barChart('chart-throughput',tp,{cls:l=>l.includes('baileys')?'green':l.includes('waha')?'amber':''});
  const oc={};(t.inbox_outcomes||[]).forEach(x=>{const k=new Date(x.bucket).toLocaleString();oc[k]=(oc[k]||0)+x.count});
  barChart('chart-outcomes',oc,{cls:()=>'gray'});
  const lat=(w.latency||{});
  $('latency').innerHTML=table(['Stage','Samples','p50','p95','Sync/catch-up rows'],
    Object.entries(lat).map(([k,v])=>`<tr><td class="mono">${esc(k)}</td><td>${num(v.samples)}</td><td>${secs(v.p50_seconds)}</td><td>${secs(v.p95_seconds)}</td><td>${v.sync_rows===null||v.sync_rows===undefined?'—':num(v.sync_rows)}</td></tr>`));
  $('batches').innerHTML=table(['Batches','Rows submitted','New unique','Duplicates','Updated revisions','In-batch dup','Discarded revisions','Bytes','Persistence Σms','Request p50/p95 ms'],
    [`<tr><td>${num(bd.batches)}</td><td>${num(bd.message_rows_submitted)}</td><td>${num(bd.new_unique_rows)}</td><td>${num(bd.duplicate_deliveries)}</td><td>${num(bd.updated_revisions)}</td><td>${num(bd.in_batch_duplicates)}</td><td>${num(bd.discarded_revisions)}</td><td>${num(bd.bytes_received)}</td><td>${num(bd.persistence_ms_total,0)}</td><td>${num(bd.request_p50_ms,0)} / ${num(bd.request_p95_ms,0)}</td></tr>`])
    +table(['Rejected/failed'],Object.entries(w.rejected||{}).map(([k,v])=>`<tr><td class="mono">${esc(k)}</td><td>${num(v)}</td></tr>`));
  const bg={};(t.backlog_gauges||[]).forEach(x=>{const k=new Date(x.bucket).toLocaleTimeString();bg[k+' '+x.metric]=(bg[k+' '+x.metric]||0)+x.value});
  barChart('backlog-history',bg,{cls:()=>'red'});
}
function renderMedia(m){
  if(!m.available){$('media').innerHTML=unavailable('media');return}
  $('media').innerHTML=`<div class="row"><div>${table(['Media type','Discovered','Enriched','Pending','Processing','Failed','Terminal','No enrichment row'],
    (m.by_media_type||[]).map(x=>`<tr><td><b>${esc(x.media_type)}</b></td><td>${num(x.discovered)}</td><td>${num(x.enriched)}</td><td>${num(x.pending)}</td><td>${num(x.processing)}</td><td>${num(x.failed)}</td><td>${num(x.terminal)}</td><td>${num(x.no_enrichment_row)}</td></tr>`))}</div>
  <div>${table(['Current backlog','Count','Oldest','Max attempts','Next retry'],
    (m.current_backlog||[]).map(x=>`<tr><td>${pill(x.status)}</td><td>${num(x.count)}</td><td>${fmt(x.oldest_at)}</td><td>${num(x.max_attempts_seen)}</td><td>${fmt(x.next_retry_at)}</td></tr>`))}
  ${table(['Failure category','Count'],(m.failure_categories||[]).map(x=>`<tr><td class="mono">${esc(x.error_type)}</td><td>${num(x.count)}</td></tr>`))}
  <p class="tiny">Enrichment latency p50 ${num(m.enrichment_latency_p50_ms,0)}ms / p95 ${num(m.enrichment_latency_p95_ms,0)}ms over ${num(m.enrichment_latency_samples)} samples. Oldest pending: ${fmt(m.oldest_pending_at)}</p></div></div>
  ${table(['Enrichment kind','Status','Count'],(m.by_enrichment_kind||[]).map(x=>`<tr><td class="mono">${esc(x.kind)}</td><td>${pill(x.status)}</td><td>${num(x.count)}</td></tr>`))}
  <p class="tiny">${esc(m.notes||'')}</p>`;
}
function renderGuard(g){
  if(!g.available){$('guard').innerHTML=unavailable('guard analysis');return}
  const sec=g.security||{},exp=g.exposure||{},sch=g.scheduled||{};
  $('guard').innerHTML=`<div class="row"><div><h3>Security (Baseline B)</h3>${table(['Committed','YES','NO','UNCERTAIN','Analysis delay p50/p95'],
    [`<tr><td>${num(sec.committed)}</td><td>${num(sec.parent_should_know_true)}</td><td>${num(sec.parent_should_know_false)}</td><td>${num(sec.parent_should_know_null)} (${num(sec.parent_should_know_semantic_uncertain)} semantic / ${num(sec.parent_should_know_operational_uncertain)} operational)</td><td>${secs(sec.analysis_delay_p50_s)} / ${secs(sec.analysis_delay_p95_s)}</td></tr>`])}
  ${table(['Model','Analyzer version','Count'],(g.security_versions||[]).map(x=>`<tr><td class="mono">${esc(x.model)}</td><td class="mono">${esc(x.analyzer_version)}</td><td>${num(x.count)}</td></tr>`))}</div>
  <div><h3>Exposure</h3>${table(['Committed','YES','NO','UNCERTAIN'],
    [`<tr><td>${num(exp.committed)}</td><td>${num(exp.significant_true)}</td><td>${num(exp.significant_false)}</td><td>${num(exp.significant_null)}</td></tr>`])}
  ${table(['Category','Count'],(g.exposure_categories||[]).map(x=>`<tr><td>${esc(x.category)}</td><td>${num(x.count)}</td></tr>`))}</div>
  <div><h3>Scheduled work (current)</h3>${table(['Scheduled','Overdue','Significantly overdue','Oldest due','Oldest pending since'],
    [`<tr><td>${num(sch.scheduled)}</td><td>${num(sch.overdue)}</td><td>${num(sch.significantly_overdue)}</td><td>${fmt(sch.oldest_due_at)}</td><td>${fmt(sch.oldest_pending_since)}</td></tr>`])}
  ${table(['AI attempts (operation)','Status','Count','Latency p50/p95'],(g.ai_call_attempts||[]).map(x=>`<tr><td class="mono">${esc(x.operation)}</td><td>${pill(x.status)}</td><td>${num(x.count)}</td><td>${x.latency_p50_ms===null||x.latency_p50_ms===undefined?'—':num(x.latency_p50_ms,0)+'ms / '+num(x.latency_p95_ms,0)+'ms'}</td></tr>`))}
  ${table(['Analysis events (analyzer)','Status','Count','Latency p50/p95'],(g.analysis_events||[]).map(x=>`<tr><td class="mono">${esc(x.analyzer||'?')}</td><td>${esc(x.event_name)}</td><td>${num(x.count)}</td><td>${x.latency_p50_ms===null||x.latency_p50_ms===undefined?'—':num(x.latency_p50_ms,0)+'ms / '+num(x.latency_p95_ms,0)+'ms'}</td></tr>`))}</div></div>
  ${(g.legacy_attempts||[]).length?table(['Legacy attempts','Count','Latency p50/p95'],(g.legacy_attempts||[]).map(x=>`<tr><td>${pill(x.status)}</td><td>${num(x.count)}</td><td>${secs(x.latency_p50_s)} / ${secs(x.latency_p95_s)}</td></tr>`)):''}
  <p class="tiny">${esc(g.notes||'')}</p>`;
}
function renderNotifications(n){
  if(!n.available){$('notifications').innerHTML=unavailable('notifications');return}
  const sum=n.summary||{};
  $('notifications').innerHTML=`<div class="counters">${['sent','scheduled','failed'].map(k=>`<div class="chip">${k}<b>${num(sum[k])}</b></div>`).join('')}</div>
  <div class="row"><div><h3>Security outcomes</h3>${table(['Status','Count','Delivery p50/p95','Oldest scheduled','Last failed'],(n.security_outcomes||[]).map(x=>`<tr><td>${pill(x.status)}</td><td>${num(x.count)}</td><td>${secs(x.delivery_p50_s)} / ${secs(x.delivery_p95_s)}</td><td>${fmt(x.oldest_scheduled_at)}</td><td>${fmt(x.last_failed_at)}</td></tr>`))}</div>
  <div><h3>Exposure outcomes</h3>${table(['Status','Count','Delivery p50/p95','Oldest scheduled','Last failed'],(n.exposure_outcomes||[]).map(x=>`<tr><td>${pill(x.status)}</td><td>${num(x.count)}</td><td>${secs(x.delivery_p50_s)} / ${secs(x.delivery_p95_s)}</td><td>${fmt(x.oldest_scheduled_at)}</td><td>${fmt(x.last_failed_at)}</td></tr>`))}</div>
  <div><h3>Scheduled send actions</h3>${table(['Status','Count','Overdue pending','Oldest pending','Total latency p50/p95'],(n.scheduled_actions||[]).map(x=>`<tr><td>${pill(x.status)}</td><td>${num(x.count)}</td><td>${num(x.overdue_pending)}</td><td>${fmt(x.oldest_pending_at)}</td><td>${secs(x.total_latency_p50_s)} / ${secs(x.total_latency_p95_s)}</td></tr>`))}
  <h3>Incidents</h3>${table(['Created','Open','Recurrences','Re-notified'],[`<tr><td>${num(n.incidents&&n.incidents.created)}</td><td>${num(n.incidents&&n.incidents.open)}</td><td>${num(n.incidents&&n.incidents.recurrences)}</td><td>${num(n.incidents&&n.incidents.re_notified)}</td></tr>`])}
  <h3>Legacy alert funnel</h3>${table(['Evaluations','Count'],(n.legacy_alert_evaluations||[]).map(x=>`<tr><td>${esc(x.outcome)}</td><td>${num(x.count)}</td></tr>`))}${table(['Alerts (kind/status)','Count'],(n.legacy_alerts||[]).map(x=>`<tr><td>${esc(x.kind)}/${esc(x.status)}</td><td>${num(x.count)}</td></tr>`))}</div></div>
  <p class="tiny">${esc(n.notes||'')}</p>`;
}
function renderManagement(m){
  if(!m.available){$('management').innerHTML=unavailable('management');return}
  const o=m.onboarding||{},c=m.commands_current||{};
  $('management').innerHTML=`<div class="row"><div><h3>Session lifecycle</h3>${table(['Status','Count','Pending effects','Last status at'],(m.session_statuses||[]).map(x=>`<tr><td>${pill(x.status)}</td><td>${num(x.count)}</td><td>${num(x.pending_effects)}</td><td>${fmt(x.last_status_at)}</td></tr>`))}
  ${table(['Raw WAHA status','Count'],(m.raw_statuses||[]).map(x=>`<tr><td class="mono">${esc(x.raw_status)}</td><td>${num(x.count)}</td></tr>`))}</div>
  <div><h3>Commands (current)</h3>${table(['Pending','Claimed','Expired leases','Exhausted','Oldest outstanding'],[`<tr><td>${num(c.pending)}</td><td>${num(c.claimed)}</td><td>${num(c.expired_leases)}</td><td>${num(c.exhausted)}</td><td>${fmt(c.oldest_outstanding_at)}</td></tr>`])}
  ${table(['Action','Status','Count','Max attempts','Pickup p50/p95','Execution p50/p95','Total p50/p95'],(m.commands_by_action||[]).map(x=>`<tr><td class="mono">${esc(x.action)}</td><td>${pill(x.status)}</td><td>${num(x.count)}</td><td>${num(x.max_attempts)}</td><td>${secs(x.pickup_p50_s)} / ${secs(x.pickup_p95_s)}</td><td>${secs(x.execution_p50_s)} / ${secs(x.execution_p95_s)}</td><td>${secs(x.total_p50_s)} / ${secs(x.total_p95_s)}</td></tr>`))}</div>
  <div><h3>Onboarding</h3>${table(['Attempts','QR ready','Working','Paired','Stuck >24h','Alloc→QR p50/p95','QR→WORKING p50/p95'],[`<tr><td>${num(o.attempts)}</td><td>${num(o.qr_ready)}</td><td>${num(o.working)}</td><td>${num(o.paired)}</td><td>${num(o.stuck)}</td><td>${secs(o.allocation_to_qr_p50_s)} / ${secs(o.allocation_to_qr_p95_s)}</td><td>${secs(o.qr_to_working_p50_s)} / ${secs(o.qr_to_working_p95_s)}</td></tr>`])}
  ${table(['Failed commands (worker/action)','Count'],(m.failed_commands_by_worker||[]).map(x=>`<tr><td class="mono">${esc(x.worker_id)}/${esc(x.action)}</td><td>${num(x.count)}</td></tr>`))}</div></div>
  <p class="tiny">${esc(m.notes||'')}</p>`;
}
function renderAI(a){
  if(!a.available){$('ai-overview').innerHTML='';$('ai-costs').innerHTML=unavailable('AI costs');return}
  const o=a.overview||{};
  $('ai-overview').innerHTML=[['Cost (window)',o.total_cost_usd===null||o.total_cost_usd===undefined?null:'$'+Number(o.total_cost_usd).toFixed(4)],['Calls',o.calls],['Successful',o.successful_calls],['Failed',o.failed_calls],['Prompt tokens',o.prompt_tokens],['Completion tokens',o.completion_tokens],['Cached tokens',o.cached_tokens],['Per monitored user',a.cost_per_monitored_user_usd===null||a.cost_per_monitored_user_usd===undefined?null:'$'+a.cost_per_monitored_user_usd],['Per committed analysis',a.cost_per_committed_analysis_usd===null||a.cost_per_committed_analysis_usd===undefined?null:'$'+a.cost_per_committed_analysis_usd],['Latency p50/p95',o.latency_p50_ms===null||o.latency_p50_ms===undefined?null:num(o.latency_p50_ms,0)+'ms / '+num(o.latency_p95_ms,0)+'ms']]
    .map(([l,v])=>`<div class="card"><span>${esc(l)}</span><strong>${v===null||v===undefined?'<span class="na">N/A</span>':v}</strong></div>`).join('');
  $('ai-costs').innerHTML=`<div class="row"><div>${table(['Provider','Calls','Cost','Tokens'],(a.by_provider||[]).map(x=>`<tr><td>${esc(x.provider)}</td><td>${num(x.calls)}</td><td>${usd(x.cost_usd)}</td><td>${num(x.total_tokens)}</td></tr>`))}
  ${table(['Operation','Calls','Cost'],(a.by_operation||[]).map(x=>`<tr><td class="mono">${esc(x.operation)}</td><td>${num(x.calls)}</td><td>${usd(x.cost_usd)}</td></tr>`))}</div>
  <div>${table(['Model','Calls','Cost','Prompt','Completion','Cached'],(a.by_model||[]).map(x=>`<tr><td class="mono">${esc(x.model)}</td><td>${num(x.calls)}</td><td>${usd(x.cost_usd)}</td><td>${num(x.prompt_tokens)}</td><td>${num(x.completion_tokens)}</td><td>${num(x.cached_tokens)}</td></tr>`))}
  ${table(['Top children by cost','Calls','Cost'],(a.by_child||[]).map(x=>`<tr><td class="mono">${esc(x.child_user_id).slice(0,8)}…</td><td>${num(x.calls)}</td><td>${usd(x.cost_usd)}</td></tr>`))}</div></div>
  ${table(['Day','Calls','Cost','Tokens'],(a.daily||[]).map(x=>`<tr><td>${new Date(x.day).toLocaleDateString()}</td><td>${num(x.calls)}</td><td>${usd(x.cost_usd)}</td><td>${num(x.total_tokens)}</td></tr>`))}
  <p class="tiny">${esc(a.notes||'')}</p>`;
}
function renderErrors(d){
  const subs=[...new Set(state.errors.map(e=>e.subsystem))];
  const sel=$('err-sub');const cur=sel.value;
  const all=['',...new Set([...(sel.options?Array.from(sel.options).map(o=>o.value):[]),...subs])].filter(Boolean);
  sel.innerHTML='<option value="">All subsystems</option>'+all.map(s=>`<option ${s===cur?'selected':''} value="${esc(s)}">${esc(s)}</option>`).join('');sel.value=cur;
  $('err-aggregates').innerHTML=(d.aggregates||[]).slice(0,12).map(a=>`<div class="chip">${esc(a.subsystem)} / ${esc(a.category)}<b>${num(a.count)}</b></div>`).join('')||'<p class="empty">No errors in window.</p>';
  $('errors').innerHTML=(state.errors||[]).length?table(['When','Subsystem','Provider','User','Worker','Category','Description','State','Attempts'],
    state.errors.map(e=>`<tr><td>${fmt(e.timestamp)}</td><td>${esc(e.subsystem)}</td><td>${cell(e.provider)}</td><td>${e.user_id?'<span class="mono">'+esc(e.user_id).slice(0,8)+'…</span>':'—'}</td><td>${e.worker_id?'<span class="mono">'+esc(e.worker_id)+'</span>':'—'}</td><td class="mono">${esc(e.category)}</td><td class="tiny" style="white-space:normal;max-width:340px">${esc(e.description||'')}</td><td>${pill(e.state)}</td><td>${num(e.attempt_count)}</td></tr>`))
    +(state.errOffset<(d.total_in_window||0)?'<p class="tiny">More available — use Load more.</p>':''):'<p class="empty">No errors match the current filters.</p>';
}

$('refresh').onclick=()=>{loadSnapshot();loadErrors(false)};
$('pause').onclick=()=>{state.paused=!state.paused;$('pause').textContent=state.paused?'Resume auto':'Pause auto';$('clock').textContent=state.paused?'auto-refresh paused':''};
$('period').onchange=()=>{loadSnapshot();loadErrors(false)};
$('provider').onchange=loadSnapshot;$('worker').onchange=loadSnapshot;
$('user').onchange=loadSnapshot;
$('err-sub').onchange=()=>loadErrors(false);$('err-sev').onchange=()=>loadErrors(false);
$('err-more').onclick=()=>loadErrors(true);
loadSnapshot();loadErrors(false);
setInterval(()=>{if(!state.paused){loadSnapshot()}},30000);
setInterval(()=>{if(!state.paused){loadErrors(false)}},120000);
</script></body></html>'''
