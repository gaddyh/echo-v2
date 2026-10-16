"""Self-contained HTML for the parent-facing Guard alert page."""

from __future__ import annotations

__all__ = ["GUARD_PARENT_ALERT_EXPIRED_PAGE", "GUARD_PARENT_ALERT_PAGE"]


GUARD_PARENT_ALERT_PAGE = r'''<!doctype html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>התראת Guard</title>
<style>
:root{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;color:#17202a;background:#f2f5f7;line-height:1.55}*{box-sizing:border-box}body{margin:0}.wrap{max-width:560px;margin:auto;padding:24px 16px 48px}.card{background:#fff;border:1px solid #dfe6eb;border-radius:18px;box-shadow:0 4px 18px #1b344014;padding:22px}.brand{color:#61727e;font-size:.82rem;font-weight:700;letter-spacing:.06em;margin-bottom:10px}.eyebrow{color:#b42318;font-size:.85rem;font-weight:700}.eyebrow.notify{color:#8a5a00}.title{font-size:1.65rem;line-height:1.25;margin:8px 0 4px}.muted{color:#647480;font-size:.9rem}.summary{font-size:1.08rem;margin:22px 0 16px}.section{border-top:1px solid #e5eaee;padding-top:15px;margin-top:15px}.section h2{font-size:.88rem;color:#647480;margin:0 0 6px}.section p{margin:0}.chips{display:flex;gap:6px;flex-wrap:wrap}.chip{background:#eef3f6;border-radius:999px;color:#40515d;font-size:.86rem;padding:4px 10px}.notice{background:#fff7e6;border:1px solid #f2d49a;border-radius:10px;color:#694b00;margin-top:18px;padding:11px 13px;font-size:.9rem}.error{text-align:center;padding:28px 10px}.error h1{font-size:1.35rem}.loading{text-align:center;color:#647480;padding:42px 8px}@media(max-width:380px){.wrap{padding:14px 10px 32px}.card{padding:18px}.title{font-size:1.45rem}}
</style>
</head>
<body>
<main class="wrap"><section id="content" class="card"><div class="loading">טוען את פרטי ההתרעה…</div></section></main>
<script>
const esc=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const fmt=v=>v?new Date(v).toLocaleString('he-IL',{dateStyle:'medium',timeStyle:'short'}):'';
const labels={notify:'התראה',urgent:'התראה דחופה'};
function render(a){const severity=labels[a.severity]||'התראה';const category=a.categories?.length?a.categories.map(x=>`<span class="chip">${esc(x)}</span>`).join(''):'<span class="muted">כללי</span>';document.getElementById('content').innerHTML=`<div class="brand">ECHO GUARD</div><div class="eyebrow ${a.severity==='notify'?'notify':''}">${esc(severity)}</div><h1 class="title">התראת בטיחות עבור ${esc(a.child_name)}</h1><div class="muted">${esc(fmt(a.detected_at))}</div><p class="summary">${esc(a.summary)}</p><div class="section"><h2>למה זה מדאיג?</h2><p>${esc(a.concern)}</p></div><div class="section"><h2>קטגוריה</h2><div class="chips">${category}</div></div>${a.expired?'<div class="notice">ההתראה אינה פעילה יותר, אך הפרטים נשמרים כאן לעיונך.</div>':''}`}
async function load(){const response=await fetch('/api/guard/alert');if(response.status===401||response.status===404){document.getElementById('content').innerHTML='<div class="error"><h1>הקישור אינו זמין</h1><p class="muted">ייתכן שהקישור פג או שאינו שייך לחשבון הזה.</p></div>';return}if(!response.ok){document.getElementById('content').innerHTML='<div class="error"><h1>אירעה שגיאה</h1><p class="muted">לא ניתן לטעון את ההתרעה כרגע.</p></div>';return}render(await response.json())}
load();
</script>
</body>
</html>'''


GUARD_PARENT_ALERT_EXPIRED_PAGE = r'''<!doctype html>
<html lang="he" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>הקישור אינו זמין</title><style>body{font-family:system-ui,sans-serif;background:#f2f5f7;color:#17202a;margin:0}.wrap{max-width:520px;margin:auto;padding:48px 20px;text-align:center}.card{background:#fff;border:1px solid #dfe6eb;border-radius:16px;padding:28px}.muted{color:#647480}</style></head><body><main class="wrap"><section class="card"><h1>הקישור אינו זמין</h1><p class="muted">הקישור להתרעה פג או שכבר נעשה בו שימוש.</p></section></main></body></html>'''
