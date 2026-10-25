"""Static HTML shell for the Guard Health admin dashboard."""

HEALTH_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Guard Health</title><style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;background:#0d1117;color:#e6edf3;padding:20px}
.wrap{max-width:1100px;margin:0 auto}
h1{font-size:1.5rem;margin-bottom:4px}
.muted{color:#7d8590;font-size:0.85rem}
.grid{display:grid;gap:12px;margin:16px 0}
.cards{grid-template-columns:repeat(auto-fill,minmax(160px,1fr))}
.card{background:#161b22;border:1px solid#30363d;border-radius:8px;padding:12px}
.card .label{font-size:0.75rem;color:#7d8590;text-transform:uppercase;letter-spacing:0.5px}
.card .value{font-size:1.6rem;font-weight:600;margin-top:4px}
.card .sub{font-size:0.75rem;color:#7d8590;margin-top:2px}
.card.ok .value{color:#3fb950}
.card.warn .value{color:#d29922}
.card.err .value{color:#f85149}
.section{background:#161b22;border:1px solid#30363d;border-radius:8px;padding:16px;margin-bottom:16px}
.section h2{font-size:1.1rem;margin-bottom:12px}
table{width:100%;border-collapse:collapse;font-size:0.85rem}
th{text-align:left;color:#7d8590;font-weight:600;padding:6px 8px;border-bottom:1px solid#30363d}
td{padding:6px 8px;border-bottom:1px solid#21262d}
.tag{display:inline-block;padding:2px 8px;border-radius:12px;font-size:0.75rem;font-weight:600}
.tag.protected{background:#1a3a2e;color:#3fb950}
.tag.attention{background#3a2a1a;color:#d29922}
.tag.incomplete{background:#3a1a1a;color:#f85149}
.tag.connected{background:#1a3a2e;color:#3fb950}
.tag.provisioning{background:#3a2a1a;color:#d29922}
.bar{height:6px;border-radius:3px;background#21262d;margin-top:4px;overflow:hidden}
.bar>div{height:100%;border-radius:3px}
.btn{background:#21262d;border:1px solid#30363d;color:#e6edf3;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:0.85rem}
.btn:hover{background#30363d}
.error-text{color:#f85149;font-size:0.8rem;word-break:break-all}
</style></head><body><main class="wrap">
<h1>Guard Health</h1>
<p class="muted" id="ts">Loading…</p>
<div style="margin:12px 0"><button class="btn" onclick="load()">Refresh</button></div>

<div id="cards" class="grid cards"></div>

<div class="section"><h2>Connections</h2><div id="connections"></div></div>
<div class="section"><h2>Enrichment Status (24h)</h2><div id="enrichment"></div></div>
<div class="section"><h2>Terminal Errors (24h)</h2><div id="terminal-errors"></div></div>
<div class="section"><h2>Stuck Enrichments</h2><div id="stuck"></div></div>
<div class="section"><h2>Pipeline Events (1h)</h2><div id="pipeline"></div></div>
<div class="section"><h2>Delivery Outcomes (24h)</h2><div id="delivery"></div></div>
<div class="section"><h2>Connector Events (24h)</h2><div id="connector"></div></div>

</main><script>
function esc(s){if(s==null)return'';return String(s).replace(/[&<>'\"]/g,c=>{'&':'&','<':'<','>':'>','"':'"',"'":'&#39;'}[c])}
function fmt(dt){if(!dt)return'—';return new Date(dt).toLocaleString()}
function ago(dt){if(!dt)return'—';const s=(Date.now()-new Date(dt).getTime())/1000;if(s<60)return Math.round(s)+'s ago';if(s<3600)return Math.round(s/60)+'m ago';return Math.round(s/3600)+'h ago'}

async function load(){
try{
const r=await fetch('/api/admin/guard/health/snapshot');
if(!r.ok){if(r.status===401){document.body.innerHTML='<main class=wrap><h1>Admin access required</h1><p class=muted>Open a waiting-list link first.</p></main>';return}throw new Error('HTTP '+r.status)}
const d=await r.json();
render(d);
}catch(e){document.getElementById('ts').textContent='Error: '+e.message}
}

function render(d){
document.getElementById('ts').textContent='Generated '+fmt(d.generated_at)+' ('+ago(d.generated_at)+')';
const cards=[
{label:'Connections',value:Object.values(d.connection_summary).reduce((a,b)=>a+b,0),sub:Object.entries(d.connection_summary).map(([k,v])=>k+': '+v).join(', '),cls:'ok'},
{label:'Messages (1h)',value:d.messages_last_1h,sub:'24h: '+d.messages_last_24h+' · latest '+ago(d.latest_message_at),cls:d.messages_last_1h>0?'ok':'warn'},
{label:'Security (1h)',value:d.security_assessments_last_1h,sub:'24h: '+d.security_assessments_last_24h,cls:d.security_assessments_last_1h>0?'ok':'warn'},
{label:'Exposure (1h)',value:d.exposure_assessments_last_1h,sub:'24h: '+d.exposure_assessments_last_24h,cls:d.exposure_assessments_last_1h>0?'ok':'warn'},
{label:'AI Cost (1h)',value:'$'+d.ai_usage_cost_1h.toFixed(4),sub:'24h: $'+d.ai_usage_cost_24h.toFixed(4)+' ('+d.ai_usage_events_24h+' events)',cls:'ok'},
{label:'Guard Alerts (24h)',value:d.guard_alerts_24h,sub:d.guard_alerts_24h===0?'none':'attention needed',cls:d.guard_alerts_24h===0?'ok':'warn'},
{label:'PSK None (24h)',value:d.psk_none_last_24h,sub:'PSK True: '+d.psk_true_last_24h,cls:d.psk_none_last_24h===0?'ok':'warn'},
{label:'Stuck Enrichments',value:d.stuck_enrichments.length,sub:d.stuck_enrichments.length===0?'none':'needs reset',cls:d.stuck_enrichments.length===0?'ok':'err'},
{label:'Failed (24h)',value:d.failed_enrichments_24h,sub:'will retry',cls:d.failed_enrichments_24h<5?'ok':'warn'},
{label:'Daily Stats Rows',value:d.daily_stats_rows,sub:'updated '+ago(d.daily_stats_last_updated),cls:'ok'}
];
document.getElementById('cards').innerHTML=cards.map(c=>'<div class="card '+c.cls+'"><div class=label>'+c.label+'</div><div class=value>'+esc(c.value)+'</div><div class=sub>'+esc(c.sub)+'</div></div>').join('');

// Connections
let html='<table><tr><th>User</th><th>Status</th><th>Provider</th><th>Updated</th><th>State v</th></tr>';
d.connections.forEach(c=>{html+='<tr><td>'+esc(String(c.user_id).slice(0,8))+'</td><td><span class="tag '+c.status+'">'+esc(c.status)+'</span></td><td>'+esc(c.provider)+'</td><td>'+ago(c.updated_at)+'</td><td>'+esc(c.state_version||'—')+'</td></tr>'});
html+='</table>';
document.getElementById('connections').innerHTML=html;

// Enrichment
html='<table><tr><th>Status</th><th>Count</th></tr>';
Object.entries(d.enrichment_status_24h).sort((a,b)=>b[1]-a[1]).forEach(([k,v])=>{html+='<tr><td>'+esc(k)+'</td><td>'+v+'</td></tr>'});
html+='</table>';
document.getElementById('enrichment').innerHTML=html;

// Terminal errors
html='<table><tr><th>Error</th><th>Count</th></tr>';
if(d.terminal_errors_24h.length===0){html+='<tr><td colspan=2 class=muted>No terminal errors.</td></tr>'}
d.terminal_errors_24h.forEach(e=>{html+='<tr><td class=error-text>'+esc(e.error)+'</td><td>'+e.count+'</td></tr>'});
html+='</table>';
document.getElementById('terminal-errors').innerHTML=html;

// Stuck
html='<table><tr><th>ID</th><th>Message</th><th>Created</th><th>Lease Expires</th></tr>';
if(d.stuck_enrichments.length===0){html+='<tr><td colspan=4 class=muted>No stuck enrichments.</td></tr>'}
d.stuck_enrichments.forEach(s=>{html+='<tr><td>'+esc(String(s.id).slice(0,8))+'</td><td>'+esc(String(s.message_id).slice(0,8))+'</td><td>'+fmt(s.created_at)+'</td><td>'+esc(s.lease_expires_at||'none')+'</td></tr>'});
html+='</table>';
document.getElementById('stuck').innerHTML=html;

// Pipeline
html='<table><tr><th>Event</th><th>Count</th></tr>';
if(Object.keys(d.pipeline_events_1h).length===0){html+='<tr><td colspan=2 class=muted>No pipeline events.</td></tr>'}
Object.entries(d.pipeline_events_1h).sort((a,b)=>b[1]-a[1]).forEach(([k,v])=>{html+='<tr><td>'+esc(k)+'</td><td>'+v+'</td></tr>'});
html+='</table>';
document.getElementById('pipeline').innerHTML=html;

// Delivery
html='<table><tr><th>Status</th><th>Count</th></tr>';
if(Object.keys(d.delivery_outcomes_24h).length===0){html+='<tr><td colspan=2 class=muted>No delivery outcomes.</td></tr>'}
Object.entries(d.delivery_outcomes_24h).forEach(([k,v])=>{html+='<tr><td>'+esc(k)+'</td><td>'+v+'</td></tr>'});
html+='</table>';
document.getElementById('delivery').innerHTML=html;

// Connector
html='<table><tr><th>Status</th><th>Count</th></tr>';
if(Object.keys(d.connector_events_24h).length===0){html+='<tr><td colspan=2 class=muted>No connector events.</td></tr>'}
Object.entries(d.connector_events_24h).forEach(([k,v])=>{html+='<tr><td>'+esc(k)+'</td><td>'+v+'</td></tr>'});
html+='</table>';
document.getElementById('connector').innerHTML=html;
}
load();
setInterval(load,30000);
</script></body></html>"""
