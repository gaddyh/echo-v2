"""HTML for the public landing page.

Self-contained HTML with inline CSS+JS (no build step, no external
dependencies), matching the visual language of the waiting-list mini app.
``dir="rtl"``, Hebrew-first. Contains a waitlist signup form (name +
phone) that POSTs to ``/api/waitlist``.
"""

from __future__ import annotations

__all__ = ["LANDING_PAGE"]


LANDING_PAGE = r"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Echo — כמה דברים עדיין פתוחים לך מול לקוחות בוואטסאפ?</title>
<meta name="description" content="Echo מזהה אוטומטית למי צריך לחזור, מה הבטחת לשלוח ומה עדיין מחכה לטיפול — לפני שזה הופך ללקוח מאוכזב או עסקה שנופלת.">
<meta property="og:type" content="website">
<meta property="og:title" content="Echo — כמה דברים עדיין פתוחים לך מול לקוחות בוואטסאפ?">
<meta property="og:description" content="Echo מזהה אוטומטית למי צריך לחזור, מה הבטחת לשלוח ומה עדיין מחכה לטיפול — לפני שזה הופך ללקוח מאוכזב או עסקה שנופלת.">
<meta property="og:url" content="{{BASE_URL}}/">
<meta property="og:image" content="{{BASE_URL}}/og.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:locale" content="he_IL">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="Echo — כמה דברים עדיין פתוחים לך מול לקוחות בוואטסאפ?">
<meta name="twitter:description" content="Echo מזהה למי צריך לחזור, מה הבטחת לשלוח ומה עדיין מחכה לטיפול.">
<meta name="twitter:image" content="{{BASE_URL}}/og.png">
<style>
:root {
  --bg: #f0f2f5;
  --card: #ffffff;
  --primary: #25d366;
  --primary-hover: #1eb558;
  --dark: #075e54;
  --text: #111b21;
  --text-secondary: #667781;
  --border: #e9edef;
  --danger: #ff4757;
  --shadow: 0 1px 2px rgba(0,0,0,0.08);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
  -webkit-text-size-adjust: 100%;
}
.container { max-width: 560px; margin: 0 auto; padding: 0 20px; }

/* --- hero --- */
.hero {
  background: linear-gradient(160deg, var(--dark) 0%, #128c7e 60%, var(--primary) 130%);
  color: white;
  text-align: center;
  padding: 56px 0 72px;
}
.hero .logo {
  font-size: 1.1rem;
  font-weight: 700;
  letter-spacing: 0.06em;
  opacity: 0.92;
  margin-bottom: 28px;
}
.hero h1 {
  font-size: 2rem;
  font-weight: 700;
  line-height: 1.3;
  margin-bottom: 14px;
}
.hero .sub {
  font-size: 1.08rem;
  opacity: 0.92;
  max-width: 440px;
  margin: 0 auto 30px;
}
.hero .cta {
  display: inline-block;
  background: white;
  color: var(--dark);
  font-size: 1.05rem;
  font-weight: 700;
  padding: 14px 36px;
  border-radius: 28px;
  text-decoration: none;
  box-shadow: 0 4px 14px rgba(0,0,0,0.18);
  transition: transform 0.15s;
}
.hero .cta:hover { transform: translateY(-2px); }

/* --- pain --- */
.pain {
  text-align: center;
  padding: 44px 0 8px;
}
.pain h2 { font-size: 1.35rem; margin-bottom: 10px; }
.pain p { color: var(--text-secondary); max-width: 460px; margin: 0 auto; }

/* --- demo (faithful replica of the real mini-app) --- */
.demo { padding: 28px 0 8px; }
.demo-frame {
  max-width: 380px;
  margin: 0 auto;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 20px;
  box-shadow: 0 8px 28px rgba(0,0,0,0.10);
  padding: 16px 14px 12px;
  pointer-events: none;
  user-select: none;
  position: relative;
  overflow: hidden;
}
/* --- demo animation --- */
.demo-pointer {
  position: absolute;
  width: 34px;
  height: 34px;
  border-radius: 50%;
  background: rgba(17,27,33,0.28);
  border: 2px solid rgba(255,255,255,0.9);
  box-shadow: 0 2px 8px rgba(0,0,0,0.25);
  z-index: 5;
  top: 40%;
  left: 50%;
  opacity: 0;
  transition: top 0.55s cubic-bezier(.5,0,.3,1), left 0.55s cubic-bezier(.5,0,.3,1), opacity 0.3s, transform 0.12s;
  pointer-events: none;
}
.demo-pointer.visible { opacity: 1; }
.demo-pointer.tap { transform: scale(0.72); }
.demo-press {
  filter: brightness(0.88);
  transform: scale(0.97);
}
.demo-card .btn-done, .demo-card .btn-sec, .demo-card .link-row span {
  transition: filter 0.15s, transform 0.15s;
}
.demo-toast {
  position: absolute;
  top: 12px;
  right: 50%;
  transform: translateX(50%) translateY(-8px);
  background: var(--text);
  color: white;
  padding: 8px 18px;
  border-radius: 20px;
  font-size: 0.85rem;
  white-space: nowrap;
  opacity: 0;
  transition: opacity 0.3s, transform 0.3s;
  z-index: 6;
}
.demo-toast.show { opacity: 1; transform: translateX(50%) translateY(0); }
.demo-card-inner { transition: opacity 0.3s, transform 0.3s; }
.demo-card-inner.swap { opacity: 0; transform: translateX(-24px); }
.demo-overlay {
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  background: var(--card);
  border-radius: 16px 16px 0 0;
  box-shadow: 0 -2px 12px rgba(0,0,0,0.14);
  padding: 14px 14px 16px;
  z-index: 4;
  transform: translateY(105%);
  transition: transform 0.35s cubic-bezier(.4,0,.2,1);
}
.demo-overlay.open { transform: translateY(0); }
.demo-overlay .ov-title { font-weight: 600; font-size: 0.95rem; margin-bottom: 10px; }
.demo-overlay .ov-templates { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 8px; }
.demo-overlay .ov-tpl {
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: 14px;
  background: var(--card);
  font-size: 0.76rem;
  color: var(--text-secondary);
  transition: filter 0.15s, transform 0.15s, background 0.2s, color 0.2s;
}
.demo-overlay .ov-tpl.selected { background: var(--primary); color: white; border-color: var(--primary); }
.demo-overlay .ov-msg {
  min-height: 44px;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 0.85rem;
  color: var(--text);
  background: var(--bg);
  margin-bottom: 8px;
}
.demo-overlay .ov-msg .caret {
  display: inline-block;
  width: 1px;
  border-left: 1.5px solid var(--text);
  animation: demo-blink 0.8s step-end infinite;
}
@keyframes demo-blink { 50% { border-color: transparent; } }
.demo-overlay .ov-msg:empty::before { content: "מה לשלוח?"; color: var(--text-secondary); }
.demo-overlay .ov-presets { display: flex; gap: 6px; margin-bottom: 8px; }
.demo-overlay .ov-preset {
  flex: 1;
  padding: 8px 4px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--card);
  font-size: 0.78rem;
  text-align: center;
  color: var(--text);
  transition: filter 0.15s, transform 0.15s;
}
@media (prefers-reduced-motion: reduce) {
  .demo-pointer, .demo-toast, .demo-overlay { display: none !important; }
}
.demo-progress { text-align: center; color: var(--text-secondary); font-size: 0.85rem; }
.demo-progress-bar {
  height: 4px;
  background: var(--border);
  border-radius: 2px;
  margin: 8px 24px 12px;
  overflow: hidden;
}
.demo-progress-bar span {
  display: block;
  width: 33%;
  height: 100%;
  background: var(--primary);
  border-radius: 2px;
}
.demo-chips { display: flex; gap: 6px; justify-content: flex-start; flex-wrap: wrap; margin-bottom: 12px; }
.demo-chip {
  padding: 4px 12px;
  border-radius: 16px;
  border: 1px solid var(--border);
  background: var(--card);
  font-size: 0.78rem;
  color: var(--text);
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.demo-chip.active { background: var(--text); color: white; border-color: var(--text); }
.demo-card {
  background: var(--card);
  border-radius: 14px;
  box-shadow: var(--shadow);
  padding: 16px;
  position: relative;
}
.demo-card .name { font-weight: 700; font-size: 1.05rem; }
.demo-card .name .star { color: #f5a623; }
.demo-card .meta { color: var(--text-secondary); font-size: 0.82rem; margin-top: 2px; }
.demo-card .summary { font-size: 0.95rem; margin: 14px 0 10px; text-align: center; }
.demo-card .context-link { color: var(--primary); font-size: 0.82rem; }
.demo-card .btn-done {
  margin-top: 10px;
  width: 100%;
  padding: 12px;
  border: none;
  border-radius: 8px;
  background: var(--primary);
  color: white;
  font-weight: 600;
  font-size: 1rem;
}
.demo-card .btn-row { display: flex; gap: 8px; margin-top: 8px; }
.demo-card .btn-sec {
  flex: 1;
  padding: 9px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--card);
  color: var(--text);
  font-size: 0.85rem;
  text-align: center;
}
.demo-card .btn-sec.send { border-color: #3b82f6; color: #3b82f6; background: #eff6ff; }
.demo-card .btn-sec.muted { background: #f1f3f5; }
.demo-card .link-row { display: flex; justify-content: space-around; margin-top: 10px; font-size: 0.82rem; }
.demo-card .link-row .other { color: var(--text-secondary); }
.demo-card .link-row .fp { color: var(--danger); }
.demo-nav { display: flex; gap: 8px; margin-top: 12px; }
.demo-nav div {
  flex: 1;
  padding: 9px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--card);
  color: var(--text);
  font-size: 0.85rem;
  text-align: center;
}
.demo-nav .disabled { color: var(--border); }
.demo-caption {
  text-align: center;
  color: var(--text-secondary);
  font-size: 0.85rem;
  margin-top: 14px;
}

/* --- how it works --- */
.how { padding: 44px 0 12px; }
.how h2 { font-size: 1.35rem; text-align: center; margin-bottom: 26px; }
.step {
  display: flex;
  gap: 14px;
  align-items: flex-start;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px;
  margin-bottom: 12px;
  box-shadow: var(--shadow);
}
.step .num {
  flex-shrink: 0;
  width: 34px;
  height: 34px;
  border-radius: 50%;
  background: var(--primary);
  color: white;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
}
.step h3 { font-size: 1rem; margin-bottom: 2px; }
.step p { color: var(--text-secondary); font-size: 0.9rem; }

/* --- transparency --- */
.transparency {
  padding: 44px 0 12px;
}
.transparency h2 {
  font-size: 1.35rem;
  text-align: center;
  margin-bottom: 8px;
}
.transparency .section-sub {
  text-align: center;
  color: var(--text-secondary);
  font-size: 0.92rem;
  margin-bottom: 22px;
}
.transparency .grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  max-width: 640px;
  margin: 0 auto;
}
.transparency .card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px 18px;
}
.transparency .card h3 {
  font-size: 0.95rem;
  margin-bottom: 4px;
}
.transparency .card p {
  color: var(--text-secondary);
  font-size: 0.82rem;
  line-height: 1.45;
}
@media (max-width: 600px) {
  .transparency .grid { grid-template-columns: 1fr; }
}

/* --- faq --- */
.faq {
  padding: 32px 0 12px;
  max-width: 640px;
  margin: 0 auto;
}
.faq h2 {
  font-size: 1.35rem;
  text-align: center;
  margin-bottom: 20px;
}
.faq details {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  margin-bottom: 8px;
  overflow: hidden;
}
.faq summary {
  padding: 14px 18px;
  font-size: 0.92rem;
  font-weight: 600;
  cursor: pointer;
  list-style: none;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.faq summary::-webkit-details-marker { display: none; }
.faq summary::after {
  content: "›";
  font-size: 1.3rem;
  color: var(--text-secondary);
  transition: transform 0.2s;
}
.faq details[open] summary::after { transform: rotate(90deg); }
.faq details p {
  padding: 0 18px 14px;
  color: var(--text-secondary);
  font-size: 0.85rem;
  line-height: 1.5;
}
.faq details a {
  color: var(--primary);
  text-decoration: underline;
  font-weight: 600;
}

/* --- waitlist form --- */
.waitlist { padding: 44px 0 60px; }
.waitlist .form-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 16px;
  box-shadow: 0 4px 18px rgba(0,0,0,0.08);
  padding: 28px 24px;
  max-width: 420px;
  margin: 0 auto;
  text-align: center;
}
.waitlist h2 { font-size: 1.3rem; margin-bottom: 6px; }
.waitlist .form-sub { color: var(--text-secondary); font-size: 0.92rem; margin-bottom: 20px; }
.waitlist input {
  width: 100%;
  padding: 13px 14px;
  border: 1px solid var(--border);
  border-radius: 10px;
  font-size: 1rem;
  font-family: inherit;
  margin-bottom: 10px;
  background: var(--bg);
}
.waitlist input:focus { outline: 2px solid var(--primary); border-color: transparent; }
.waitlist button {
  width: 100%;
  padding: 14px;
  border: none;
  border-radius: 10px;
  background: var(--primary);
  color: white;
  font-size: 1.05rem;
  font-weight: 700;
  cursor: pointer;
  transition: background 0.2s;
  margin-top: 4px;
}
.waitlist button:hover { background: var(--primary-hover); }
.waitlist button:disabled { opacity: 0.6; cursor: not-allowed; }
.waitlist .scarcity {
  display: inline-block;
  background: #fff4e5;
  color: #b45309;
  border: 1px solid #fcd9a8;
  border-radius: 14px;
  padding: 3px 12px;
  font-size: 0.8rem;
  font-weight: 600;
  margin-bottom: 10px;
}
.waitlist .counter {
  color: var(--text-secondary);
  font-size: 0.85rem;
  margin-bottom: 8px;
}
.waitlist .wtp-label {
  font-size: 0.85rem;
  color: var(--text-secondary);
  margin: 6px 0 6px;
  text-align: right;
}
.waitlist .wtp-chips { display: flex; gap: 6px; margin-bottom: 10px; }
.waitlist .wtp-chip {
  flex: 1;
  padding: 8px 4px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg);
  font-size: 0.8rem;
  color: var(--text);
  cursor: pointer;
  transition: background 0.15s, color 0.15s, border-color 0.15s;
  text-align: center;
}
.waitlist .wtp-chip.selected {
  background: var(--primary);
  color: white;
  border-color: var(--primary);
}
.waitlist .error {
  display: none;
  color: var(--danger);
  font-size: 0.88rem;
  margin-top: 10px;
}
.waitlist .success {
  display: none;
  padding: 10px 0;
}
.waitlist .success .icon { font-size: 2.6rem; }
.waitlist .success h3 { margin-top: 8px; font-size: 1.15rem; }
.waitlist .success p { color: var(--text-secondary); font-size: 0.92rem; margin-top: 4px; }

/* --- footer --- */
.footer {
  text-align: center;
  padding: 22px 0 34px;
  color: var(--text-secondary);
  font-size: 0.82rem;
}
</style>
</head>
<body>

<section class="hero">
  <div class="container">
    <div class="logo">ECHO</div>
    <h1>כמה דברים עדיין פתוחים לך מול לקוחות בוואטסאפ?</h1>
    <p class="sub">Echo מזהה אוטומטית למי צריך לחזור, מה הבטחת לשלוח ומה עדיין מחכה לטיפול — לפני שזה הופך ללקוח מאוכזב או עסקה שנופלת.</p>
    <a class="cta" href="#waitlist">אני רוצה גישה מוקדמת</a>
  </div>
</section>

<section class="pain">
  <div class="container">
    <h2>העסק שלך רץ על וואטסאפ? Echo שומר על כל מה שנשאר פתוח</h2>
    <p>Echo נבנה לעצמאים ובעלי עסקים קטנים שמנהלים 15+ שיחות ביום — בלי CRM מסודר או עם CRM שלא באמת פותחים. הוא מזהה מי מחכה לתשובה, מה הבטחת לשלוח ומה עדיין דורש טיפול, כדי שלא תצטרך לזכור הכול בראש.</p>
  </div>
</section>

<section class="demo">
  <div class="container">
    <div class="demo-frame" id="demo-frame" aria-hidden="true">
      <div class="demo-toast" id="demo-toast"></div>
      <div class="demo-pointer" id="demo-pointer"></div>
      <div class="demo-progress" id="demo-progress">1 מתוך 3</div>
      <div class="demo-progress-bar"><span id="demo-progress-fill" style="width:33%"></span></div>
      <div class="demo-chips">
        <span class="demo-chip active">הכל</span>
        <span class="demo-chip">★</span>
      </div>
      <div class="demo-card">
        <div class="demo-card-inner" id="demo-card-inner">
          <div class="name"><span class="star" id="demo-star">★</span> <span id="demo-name">דמי אבירם</span></div>
          <div class="meta" id="demo-meta">ממתין שעה</div>
          <div class="summary" id="demo-summary">דמי שלח מסמכים ומחכה לאישור קבלה</div>
          <div class="context-link">הודעות +</div>
          <div class="btn-done" id="demo-done">בוצע</div>
          <div class="btn-row">
            <div class="btn-sec send" id="demo-send">תזמן הודעה</div>
          </div>
          <div class="btn-row">
            <div class="btn-sec" id="demo-snooze">נודניק לשעה</div>
            <div class="btn-sec muted">לא מעניין, שיחכו</div>
          </div>
          <div class="link-row">
            <span class="other">זמן אחר</span>
            <span class="fp" id="demo-fp">לא מחכים לי</span>
          </div>
        </div>
      </div>
      <div class="demo-nav">
        <div class="disabled">→ הקודם</div>
        <div>הבא ←</div>
      </div>
      <div class="demo-overlay" id="demo-overlay">
        <div class="ov-title">תזמון הודעה</div>
        <div class="ov-templates">
          <span class="ov-tpl" id="demo-tpl">קיבלתי, בודק וחוזר</span>
          <span class="ov-tpl">אחזור בהמשך היום</span>
          <span class="ov-tpl">תודה, מטפל בזה</span>
        </div>
        <div class="ov-msg" id="demo-msg"></div>
        <div class="ov-presets">
          <span class="ov-preset">עוד 10 דקות</span>
          <span class="ov-preset">עוד שעה</span>
          <span class="ov-preset" id="demo-preset">מחר בבוקר</span>
        </div>
      </div>
    </div>
    <div class="demo-caption">ככה זה נראה — כרטיס אחד לכל דבר שנשאר פתוח, פעולה אחת וממשיכים.</div>
  </div>
</section>

<section class="how">
  <div class="container">
    <h2>איך זה עובד?</h2>
    <div class="step">
      <div class="num">1</div>
      <div>
        <h3>מחברים את הוואטסאפ</h3>
        <p>סורקים קוד QR — בדיוק כמו לחבר את WhatsApp Web. בלי להתקין שום דבר, ובתוך דקה־שתיים מחוברים — דרך Green API. (אם אין מצלמה, אפשר גם בקוד חד־פעמי.)</p>
      </div>
    </div>
    <div class="step">
      <div class="num">2</div>
      <div>
        <h3>Echo מבין את השיחות</h3>
        <p>לא רק "מי כתב אחרון" — Echo מזהה למי צריך לחזור, מה הבטחת לשלוח ומה עדיין מחכה לטיפול.</p>
      </div>
    </div>
    <div class="step">
      <div class="num">3</div>
      <div>
        <h3>סיכום בוקר אחד, אפס פספוסים</h3>
        <p>כל בוקר: רשימה קצרה של מי שמחכה לך. סימנת בוצע, דחית, או תזמנת תשובה — וממשיכים ביום.</p>
      </div>
    </div>
  </div>
</section>

<section class="transparency">
  <div class="container">
    <h2>שקיפות מלאה — כי זה המחיר של האמון</h2>
    <p class="section-sub">וואטסאפ הוא אישי. אז הנה בדיוק מה Echo רואה — ומה לא.</p>
    <div class="grid">
      <div class="card">
        <h3>מה Echo רואה?</h3>
        <p>Echo מקבל את הודעות הטקסט הנכנסות והיוצאות ב־WhatsApp כדי להבין מי מחכה למי. בתמונות, הקלטות, וידאו ומסמכים Echo מזהה כרגע רק את סוג ההודעה — ולא קורא או מנתח את התוכן שלהם.</p>
      </div>
      <div class="card">
        <h3>מה נשמר?</h3>
        <p>Echo שומר את הודעות הטקסט שהמערכת מקבלת לצורך הניתוח, פרטי שיחה, תוצאות הניתוח והפעולות שביצעת. המידע נשמר מוצפן במסד הנתונים של Echo. מחיקת החשבון מוחקת את המידע המשויך אליו.</p>
      </div>
      <div class="card">
        <h3>מי מעבד את המידע?</h3>
        <p>החיבור ל־WhatsApp נעשה באמצעות Green API. קטעי טקסט רלוונטיים מהשיחה נשלחים ל־OpenAI API ללא פרטי הזיהוי, כדי לזהות מי מחכה לתשובה. המידע נשמר מוצפן, לא נמכר ולא משמש לפרסום. OpenAI אינה משתמשת בנתוני API לאימון המודלים כברירת מחדל.</p>
      </div>
      <div class="card">
        <h3>איך מפסיקים?</h3>
        <p>אפשר בכל רגע להסיר את Echo דרך WhatsApp ← הגדרות ← מכשירים מקושרים. זה מנתק מיד את הגישה להודעות חדשות. מחיקת חשבון Echo היא פעולה נפרדת שמוחקת את הנתונים שכבר נשמרו.</p>
      </div>
    </div>
  </div>
</section>

<section class="faq">
  <div class="container">
    <h2>שאלות נפוצות</h2>
    <details>
      <summary>רגע, Echo מקבל גישה להודעות שלי?</summary>
      <p>כן. כדי לזהות מי מחכה לך, Echo צריך לקרוא את הודעות הטקסט הרלוונטיות. זו גישה רגישה ואנחנו לא מנסים להסתיר את זה. לכן אנחנו מסבירים כאן בדיוק מה נקרא, מה נשמר ולאן המידע נשלח — ואפשר לנתק את Echo בכל רגע.</p>
    </details>
    <details>
      <summary>מי יכול לגשת למידע?</summary>
      <p>המידע נשמר מוצפן, ונשלח למודל ללא פרטי הזיהוי. Green API ו־OpenAI מעבדים מידע כחלק מהשירות, כפי שמפורט למעלה.</p>
    </details>
    <details>
      <summary>ואם אני רוצה למחוק הכל?</summary>
      <p>ניתק את Echo מהגדרות וואטסאפ כדי לעצור הודעות חדשות. למחיקת הנתונים שכבר נשמרו, <a href="https://wa.me/972546610653?text=מחק%20לי%20הכל">שלח לי הודעת WhatsApp</a> ואמחק את כל הנתונים המשויכים לחשבון. בקרוב יהיה גם כפתור מחיקה עצמית.</p>
    </details>
    <details>
      <summary>מה קורה אם אני רק רוצה לנסות?</summary>
      <p>מתחבר, משתמש כמה ימים. לא מתאים? מנתקים מהגדרות וואטסאפ והגישה להודעות חדשות נעצרת מיד. בלי התחייבות.</p>
    </details>
  </div>
</section>

<section class="waitlist" id="waitlist">
  <div class="container">
    <div class="form-card">
      <div id="form-view">
        <div class="scarcity">⏳ הגל הראשון מוגבל ל־50 מקומות</div>
        {{COUNTER}}
        <h2>רשימת המתנה לגישה מוקדמת</h2>
        <p class="form-sub">אנחנו פותחים את Echo בהדרגה. השאירו שם וטלפון ונחזור אליכם בוואטסאפ.</p>
        <form id="waitlist-form">
          <input type="text" id="wl-name" placeholder="שם מלא" maxlength="80" autocomplete="name" required>
          <input type="tel" id="wl-phone" placeholder="מספר וואטסאפ (למשל 050-1234567)" maxlength="20" autocomplete="tel" required>
          <div class="wtp-label">כמה היית משלם/ת על זה בחודש? (לא חובה)</div>
          <div class="wtp-chips" id="wtp-chips">
            <span class="wtp-chip" data-wtp="free">חינם</span>
            <span class="wtp-chip" data-wtp="under_30">עד 30 ₪</span>
            <span class="wtp-chip" data-wtp="30_70">30–70 ₪</span>
            <span class="wtp-chip" data-wtp="70_120">70–120 ₪</span>
            <span class="wtp-chip" data-wtp="120_plus">120+ ₪</span>
          </div>
          <button type="submit" id="wl-submit">שריינו לי מקום</button>
        </form>
        <div class="error" id="wl-error"></div>
      </div>
      <div class="success" id="success-view">
        <div class="icon">🎉</div>
        <h3>את/ה ברשימה!</h3>
        <p>נשלח לך הודעת וואטסאפ ברגע שהתור שלך מגיע.</p>
      </div>
    </div>
  </div>
</section>

<div class="footer">Echo · עוד שיחה לא תיפול בין הכיסאות</div>

<script>
const form = document.getElementById("waitlist-form");
const errEl = document.getElementById("wl-error");
const submitBtn = document.getElementById("wl-submit");

// Optional willingness-to-pay chips: single select, tap again to clear.
let selectedWtp = null;
document.querySelectorAll("#wtp-chips .wtp-chip").forEach(chip => {
  chip.addEventListener("click", () => {
    const value = chip.dataset.wtp;
    if (selectedWtp === value) {
      selectedWtp = null;
      chip.classList.remove("selected");
      return;
    }
    selectedWtp = value;
    document.querySelectorAll("#wtp-chips .wtp-chip").forEach(c => c.classList.remove("selected"));
    chip.classList.add("selected");
  });
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  errEl.style.display = "none";
  const name = document.getElementById("wl-name").value.trim();
  const phone = document.getElementById("wl-phone").value.trim();
  if (!name || !phone) return;

  submitBtn.disabled = true;
  submitBtn.textContent = "רק רגע...";
  try {
    const payload = {name: name, phone: phone};
    if (selectedWtp) payload.wtp = selectedWtp;
    const resp = await fetch("/api/waitlist", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    if (resp.status === 422) {
      showError("המספר לא נראה תקין — נסו שוב עם מספר וואטסאפ ישראלי.");
      return;
    }
    if (!resp.ok) {
      showError("משהו השתבש. נסו שוב בעוד רגע.");
      return;
    }
    document.getElementById("form-view").style.display = "none";
    document.getElementById("success-view").style.display = "block";
  } catch (_err) {
    showError("משהו השתבש. נסו שוב בעוד רגע.");
  }
});

function showError(msg) {
  errEl.textContent = msg;
  errEl.style.display = "block";
  submitBtn.disabled = false;
  submitBtn.textContent = "שריינו לי מקום";
}

// --- demo card animation: a looping tour of the possible actions ---
(function () {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  const frame = document.getElementById("demo-frame");
  const pointer = document.getElementById("demo-pointer");
  const toast = document.getElementById("demo-toast");
  const inner = document.getElementById("demo-card-inner");

  const CARDS = [
    {name: "דמי אבירם", meta: "ממתין שעה", star: true,
     summary: "דמי שלח מסמכים ומחכה לאישור קבלה", progress: "1 מתוך 3", fill: "33%"},
    {name: "שרה כהן", meta: "ממתינה 3 שעות", star: false,
     summary: "שרה שאלה אם אתם מגיעים בשבת ומחכה לתשובה", progress: "2 מתוך 3", fill: "66%"},
    {name: "יוסי לוי", meta: "ממתין 26 שעות", star: false,
     summary: "יוסי מחכה להצעת המחיר שהבטחת לשלוח", progress: "3 מתוך 3", fill: "100%"},
  ];

  function setCard(i) {
    const c = CARDS[i];
    document.getElementById("demo-name").textContent = c.name;
    document.getElementById("demo-meta").textContent = c.meta;
    document.getElementById("demo-summary").textContent = c.summary;
    document.getElementById("demo-progress").textContent = c.progress;
    document.getElementById("demo-progress-fill").style.width = c.fill;
    document.getElementById("demo-star").style.display = c.star ? "" : "none";
  }

  function moveTo(el) {
    const fr = frame.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    pointer.style.top = (r.top - fr.top + r.height / 2 - 17) + "px";
    pointer.style.left = (r.left - fr.left + r.width / 2 - 17) + "px";
  }

  function sleep(ms) { return new Promise(res => setTimeout(res, ms)); }

  async function press(el) {
    moveTo(el);
    await sleep(650);
    pointer.classList.add("tap");
    el.classList.add("demo-press");
    await sleep(180);
    pointer.classList.remove("tap");
    el.classList.remove("demo-press");
  }

  async function showToast(text) {
    toast.textContent = text;
    toast.classList.add("show");
    await sleep(1400);
    toast.classList.remove("show");
    await sleep(250);
  }

  async function swapCard(i) {
    inner.classList.add("swap");
    await sleep(300);
    setCard(i);
    inner.classList.remove("swap");
    await sleep(350);
  }

  async function typeInto(el, text) {
    el.textContent = "";
    const caret = document.createElement("span");
    caret.className = "caret";
    el.appendChild(caret);
    for (const ch of text) {
      caret.insertAdjacentText("beforebegin", ch);
      await sleep(28);
    }
    await sleep(250);
    caret.remove();
  }

  async function sendMessageScene() {
    const overlay = document.getElementById("demo-overlay");
    const tpl = document.getElementById("demo-tpl");
    const msg = document.getElementById("demo-msg");
    const preset = document.getElementById("demo-preset");

    // Open the scheduling popup.
    await press(document.getElementById("demo-send"));
    overlay.classList.add("open");
    await sleep(600);

    // Pick a template — it fills the message box (typed).
    await press(tpl);
    tpl.classList.add("selected");
    await typeInto(msg, "קיבלתי, בודק וחוזר");

    // Pick a time preset — submits immediately, like the real app.
    await press(preset);
    await sleep(150);
    overlay.classList.remove("open");
    tpl.classList.remove("selected");
    msg.textContent = "";
    await sleep(350);
    await showToast("📤 ההודעה תישלח מחר ב־9:00");
  }

  async function loop() {
    pointer.classList.add("visible");
    for (;;) {
      // Scene 1: done → next card.
      setCard(0);
      await sleep(900);
      await press(document.getElementById("demo-done"));
      await showToast("✓ טופל");
      await swapCard(1);

      // Scene 2: snooze → next card.
      await press(document.getElementById("demo-snooze"));
      await showToast("⏰ אזכיר בעוד שעה");
      await swapCard(2);

      // Scene 3: schedule a message — popup, template, time, send.
      await sendMessageScene();
      await sleep(300);

      // Scene 4: false-positive feedback → back to start.
      await press(document.getElementById("demo-fp"));
      await showToast("🧠 Echo לומד מהפידבק");
      await swapCard(0);
    }
  }

  // Start when the demo scrolls into view.
  let started = false;
  const observer = new IntersectionObserver((entries) => {
    if (!started && entries.some(e => e.isIntersecting)) {
      started = true;
      observer.disconnect();
      loop();
    }
  }, {threshold: 0.4});
  observer.observe(frame);
})();
</script>
</body>
</html>
"""
