"""HTML for the Echo Guard public landing page.

Self-contained HTML with inline CSS + JS (no build step, no external
runtime dependencies). Hebrew-first and RTL.

Integration contract with ``landing_routes.py``:
* ``{{BASE_URL}}`` is replaced server-side for Open Graph metadata.
* ``{{COUNTER}}`` is replaced server-side with the live waitlist counter.
* The signup form POSTs to ``/api/waitlist``.

NOTE: The form already collects ``children_count`` and ``children_ages`` for
Echo Guard pilot qualification. The current ``WaitlistRequest`` / persistence
schema must be extended before these two fields are persisted. Until then,
they may be ignored by the current API model depending on Pydantic config.
"""

from __future__ import annotations

__all__ = ["LANDING_PAGE"]


LANDING_PAGE = r"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Echo Guard — לדעת לפני שזה מסלים</title>
<meta name="description" content="Echo Guard מזהה דפוסים מדאיגים בשיחות WhatsApp של הילדים ומתריע כשבאמת צריך לשים לב — בלי לקרוא כל הודעה ובלי לחכות שיהיה מאוחר.">
<meta property="og:type" content="website">
<meta property="og:title" content="Echo Guard — לדעת לפני שזה מסלים">
<meta property="og:description" content="אם משהו מסוכן מתחיל בוואטסאפ של הילד — כדאי לדעת לפני שהוא מסלים.">
<meta property="og:url" content="{{BASE_URL}}/">
<meta property="og:image" content="{{BASE_URL}}/og-guard-v2.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:locale" content="he_IL">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="Echo Guard — לדעת לפני שזה מסלים">
<meta name="twitter:description" content="הגנה חכמה ל-WhatsApp של הילדים — בלי לקרוא כל הודעה ובלי לחכות שיהיה מאוחר.">
<meta name="twitter:image" content="{{BASE_URL}}/og-guard-v2.png">
<style>
:root {
  --bg: #f7fbff;
  --surface: #ffffff;
  --text: #163047;
  --muted: #5f768a;
  --line: #dbe9f5;
  --primary: #1976d2;
  --primary-2: #4aa3ff;
  --primary-soft: #e8f4ff;
  --green: #168b59;
  --green-soft: #eaf8f0;
  --danger: #d9485f;
  --danger-soft: #fff0f3;
  --navy: #23455f;
  --amber-bg: #fff6e8;
  --amber-text: #ad6600;
  --amber-line: #f4d7a6;
  --shadow: 0 20px 60px rgba(48, 94, 139, 0.11);
  --soft-shadow: 0 10px 28px rgba(48, 94, 139, 0.07);
  --radius: 28px;
  --max: 1140px;
}

* { box-sizing: border-box; margin: 0; padding: 0; }
html { scroll-behavior: smooth; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  color: var(--text);
  background:
    radial-gradient(circle at 10% 8%, rgba(74, 163, 255, 0.14), transparent 28%),
    radial-gradient(circle at 92% 18%, rgba(22, 139, 89, 0.09), transparent 22%),
    linear-gradient(180deg, #fbfdff 0%, #f3f9fe 48%, #f8fbff 100%);
  line-height: 1.68;
  -webkit-text-size-adjust: 100%;
}

a { color: inherit; text-decoration: none; }
button, input, select { font-family: inherit; }
.wrap { max-width: var(--max); margin: 0 auto; padding: 0 24px; }

/* --- navigation --- */
.nav {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 24px 0;
}
.brand {
  color: var(--navy);
  font-size: 1.7rem;
  font-weight: 900;
  letter-spacing: -0.04em;
}
.brand span { color: var(--primary); }
.pill {
  display: inline-flex;
  align-items: center;
  padding: 9px 14px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.86);
  color: var(--muted);
  box-shadow: var(--soft-shadow);
  font-size: 0.86rem;
  font-weight: 800;
}

/* --- hero --- */
.hero {
  display: grid;
  grid-template-columns: 1.08fr 0.92fr;
  gap: 54px;
  align-items: center;
  padding: 48px 0 76px;
}
.eyebrow {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 18px;
  padding: 9px 14px;
  border: 1px solid #dcecff;
  border-radius: 999px;
  background: var(--primary-soft);
  color: var(--primary);
  font-size: 0.86rem;
  font-weight: 900;
}
.hero h1 {
  margin-bottom: 20px;
  color: var(--navy);
  font-size: clamp(2.6rem, 6vw, 4.6rem);
  line-height: 1.02;
  letter-spacing: -0.05em;
}
.hero .sub {
  max-width: 720px;
  margin-bottom: 28px;
  color: var(--muted);
  font-size: 1.25rem;
}
.actions { display: flex; gap: 12px; flex-wrap: wrap; }
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 15px 22px;
  border: 1px solid transparent;
  border-radius: 16px;
  box-shadow: var(--soft-shadow);
  font-weight: 900;
  transition: transform 0.18s ease;
}
.btn:hover { transform: translateY(-1px); }
.btn.primary {
  background: linear-gradient(135deg, var(--primary), var(--primary-2));
  color: white;
}
.btn.secondary {
  border-color: var(--line);
  background: white;
  color: var(--navy);
}
.proof {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 18px;
}
.proof span {
  padding: 7px 10px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: white;
  color: var(--muted);
  font-size: 0.8rem;
  font-weight: 800;
}

/* --- hero conversation mock --- */
.phone {
  padding: 15px;
  border: 1px solid rgba(255, 255, 255, 0.8);
  border-radius: 36px;
  background: linear-gradient(180deg, #dcefff, #eef8ff);
  box-shadow: var(--shadow);
}
.screen {
  min-height: 590px;
  padding: 18px;
  border-radius: 26px;
  background: linear-gradient(180deg, #fbfdff, #eef7fc);
}
.status {
  display: flex;
  justify-content: space-between;
  margin-bottom: 18px;
  color: #7c91a3;
  font-size: 0.75rem;
}
.chat-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 16px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 18px;
  background: white;
}
.avatar {
  display: grid;
  place-items: center;
  width: 42px;
  height: 42px;
  border-radius: 50%;
  background: var(--primary-soft);
  color: var(--primary);
  font-weight: 900;
}
.chat-name { color: var(--navy); font-weight: 900; }
.chat-sub { color: #7b91a5; font-size: 0.75rem; }
.bubble {
  max-width: 86%;
  margin: 8px 0;
  padding: 10px 13px;
  border-radius: 16px;
  box-shadow: 0 2px 8px rgba(48, 94, 139, 0.05);
  font-size: 0.88rem;
}
.bubble.in { margin-left: auto; background: white; }
.bubble.out { margin-right: auto; background: #dff7e8; }
.alert-card {
  margin-top: 24px;
  padding: 16px;
  border: 1px solid #ffd6de;
  border-radius: 20px;
  background: white;
  box-shadow: 0 12px 34px rgba(217, 72, 95, 0.09);
}
.alert-title {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--danger);
  font-weight: 900;
}
.alert-meta { margin-top: 6px; color: var(--muted); font-size: 0.82rem; }
.tags { display: flex; gap: 7px; flex-wrap: wrap; margin-top: 12px; }
.tag {
  padding: 6px 9px;
  border-radius: 999px;
  background: var(--danger-soft);
  color: var(--danger);
  font-size: 0.74rem;
  font-weight: 900;
}

/* --- shared sections --- */
section { padding: 76px 0; }
.section-title {
  margin-bottom: 16px;
  color: var(--navy);
  font-size: 2.5rem;
  line-height: 1.14;
  letter-spacing: -0.04em;
}
.section-lead {
  max-width: 780px;
  margin-bottom: 34px;
  color: var(--muted);
  font-size: 1.18rem;
}
.parent-disclaimer {
  max-width: 780px;
  margin: 22px auto 0;
  padding: 14px 18px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.72);
  color: var(--muted);
  font-size: 0.92rem;
  text-align: center;
}

/* --- pattern explanation --- */
.scenario {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 34px;
  align-items: center;
  padding: 38px;
  border-radius: 30px;
  background: linear-gradient(135deg, #1f4d73, #2d6d9c 58%, #4aa3ff 120%);
  color: white;
  box-shadow: var(--shadow);
}
.scenario h2 { margin-bottom: 12px; font-size: 2.1rem; line-height: 1.2; }
.scenario p { color: #d9edf9; }
.thread {
  padding: 18px;
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 22px;
  background: rgba(255, 255, 255, 0.10);
}
.thread .bubble { box-shadow: none; }
.thread .bubble.in { background: rgba(255, 255, 255, 0.16); color: white; }
.thread .flag {
  margin-top: 14px;
  padding: 12px;
  border: 1px solid rgba(255, 243, 205, 0.28);
  border-radius: 14px;
  background: rgba(255, 243, 205, 0.18);
  color: #fff3cd;
  font-size: 0.84rem;
  font-weight: 800;
}

/* --- visual story --- */
.story-visual {
  padding-top: 18px;
}
.story-visual img {
  display: block;
  width: 100%;
  height: auto;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
}

/* --- categories --- */
.grid3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 18px; }
.card {
  padding: 26px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: rgba(255, 255, 255, 0.94);
  box-shadow: var(--soft-shadow);
}
.card h3 { margin: 10px 0; color: var(--navy); font-size: 1.35rem; }
.card p { color: var(--muted); }
.icon {
  display: grid;
  place-items: center;
  width: 46px;
  height: 46px;
  border-radius: 14px;
  background: linear-gradient(135deg, #edf6ff, #dff1ff);
  font-size: 1.35rem;
}

/* --- whatsapp connection --- */
.connect-wrap {
  padding: 38px;
  border: 1px solid var(--line);
  border-radius: 30px;
  background: linear-gradient(135deg, #f1f9ff, #ffffff);
  box-shadow: var(--soft-shadow);
}
.connect-head {
  max-width: 760px;
  margin: 0 auto 30px;
  text-align: center;
}
.connect-head h2 {
  margin-bottom: 12px;
  color: var(--navy);
  font-size: 2.5rem;
  line-height: 1.14;
  letter-spacing: -0.04em;
}
.connect-head p { color: var(--muted); font-size: 1.18rem; }
.connect-steps { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
.connect-step {
  padding: 22px;
  border: 1px solid var(--line);
  border-radius: 22px;
  background: white;
  box-shadow: var(--soft-shadow);
}
.connect-num {
  display: grid;
  place-items: center;
  width: 36px;
  height: 36px;
  margin-bottom: 12px;
  border-radius: 50%;
  background: linear-gradient(135deg, var(--primary), var(--primary-2));
  color: white;
  font-weight: 900;
}
.connect-step h3 { margin-bottom: 7px; color: var(--navy); font-size: 1.2rem; }
.connect-step p { color: var(--muted); font-size: 0.93rem; }
.connect-trust {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  margin-top: 18px;
  padding: 16px 18px;
  border: 1px solid var(--line);
  border-radius: 18px;
  background: white;
  color: var(--muted);
  font-size: 0.88rem;
}
.connect-trust strong { color: var(--navy); }
.connect-icon {
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  width: 34px;
  height: 34px;
  border-radius: 12px;
  background: var(--green-soft);
  color: var(--green);
  font-size: 1.1rem;
}


/* --- waitlist --- */
.waitlist-section { padding: 32px 0 84px; }
.waitlist-card {
  max-width: 520px;
  margin: 0 auto;
  padding: 34px 28px;
  border: 1px solid var(--line);
  border-radius: 30px;
  background: white;
  box-shadow: var(--shadow);
  text-align: center;
}
.scarcity {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  margin-bottom: 14px;
  padding: 7px 13px;
  border: 1px solid var(--amber-line);
  border-radius: 999px;
  background: var(--amber-bg);
  color: var(--amber-text);
  font-size: 0.82rem;
  font-weight: 900;
}
.counter { margin: -4px 0 10px; color: var(--muted); font-size: 0.88rem; }
.waitlist-card h2 {
  margin-bottom: 8px;
  color: var(--navy);
  font-size: 2.1rem;
  line-height: 1.15;
  letter-spacing: -0.03em;
}
.waitlist-card .form-sub { margin-bottom: 22px; color: var(--muted); font-size: 1rem; }
.waitlist-field { margin-bottom: 10px; text-align: right; }
.waitlist-field label {
  display: block;
  margin: 0 3px 6px;
  color: var(--navy);
  font-size: 0.8rem;
  font-weight: 900;
}
.waitlist-card input,
.waitlist-card select {
  width: 100%;
  margin-bottom: 10px;
  padding: 14px 15px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: #f8fbfe;
  color: var(--text);
  font-size: 1rem;
}
.waitlist-card input:focus,
.waitlist-card select:focus { outline: 2px solid #b9dcff; border-color: transparent; }
.waitlist-card button {
  width: 100%;
  margin-top: 2px;
  padding: 15px;
  border: 0;
  border-radius: 14px;
  background: linear-gradient(135deg, var(--primary), var(--primary-2));
  color: white;
  box-shadow: var(--soft-shadow);
  cursor: pointer;
  font-size: 1rem;
  font-weight: 900;
}
.waitlist-card button:disabled { opacity: 0.6; cursor: not-allowed; }
.waitlist-note { margin-top: 12px; color: var(--muted); font-size: 0.8rem; }
.waitlist-error { display: none; margin-top: 12px; color: var(--danger); font-size: 0.86rem; font-weight: 700; }
.waitlist-success { display: none; padding: 12px 0 4px; }
.waitlist-success .success-icon { margin-bottom: 8px; color: var(--green); font-size: 2.6rem; }
.waitlist-success h3 { margin-bottom: 5px; color: var(--navy); font-size: 1.45rem; }
.waitlist-success p { color: var(--muted); }

.footer { padding: 42px 0 58px; color: var(--muted); font-size: 0.82rem; text-align: center; }

@media (max-width: 920px) {
  .hero,
  .scenario,
  .grid3,
  .connect-steps { grid-template-columns: 1fr; }

  .hero { padding-top: 28px; }
  .section-title,
  .connect-head h2 { font-size: 2.05rem; }
}

@media (max-width: 560px) {
  .wrap { padding: 0 16px; }
  .nav { padding: 18px 0; }
  .pill { display: none; }
  .hero { gap: 34px; padding-bottom: 44px; }
  .hero h1 { font-size: 2.65rem; }
  .hero .sub { font-size: 1.08rem; }
  section { padding: 54px 0; }
  .scenario,
  .connect-wrap { padding: 24px 18px; }
  .waitlist-card { padding: 28px 18px; }
}
</style>
</head>
<body>
<div class="wrap">
  <nav class="nav">
    <div class="brand">Echo <span>Guard</span></div>
    <div class="pill">פיילוט למשפחות</div>
  </nav>

  <main>
    <section class="hero">
      <div>
        <div class="eyebrow">הגנה חכמה ל-WhatsApp</div>
        <h1>אם משהו מסוכן מתחיל בוואטסאפ של הילד — כדאי לדעת לפני שהוא מסלים.</h1>
        <p class="sub">
          Echo Guard מנתח את ההקשר של שיחות פעילות ומתריע כשמופיעים סימנים למצבים
          שדורשים תשומת לב — כמו הטרדה, חרם, פנייה חשודה מאדם לא מוכר או מצוקה רגשית.
        </p>
        <div class="actions">
          <a class="btn primary" href="#waitlist">אני רוצה להצטרף לפיילוט</a>
          <a class="btn secondary" href="#connect">איך מתחברים</a>
        </div>
        <div class="proof">
          <span>בלי לקרוא כל הודעה</span>
          <span>בלי להיבהל מכל שטות</span>
          <span>בלי לחכות שיהיה מאוחר</span>
        </div>
      </div>

      <div class="phone" aria-label="דוגמה להתראת Echo Guard">
        <div class="screen">
          <div class="status"><span>22:14</span><span>WhatsApp</span></div>
          <div class="chat-head">
            <div class="avatar">?</div>
            <div>
              <div class="chat-name">איש קשר לא מוכר</div>
              <div class="chat-sub">שיחה פעילה</div>
            </div>
          </div>

          <div class="bubble in">ראיתי אותך ליד הבית ספר היום.</div>
          <div class="bubble out">מי זה?</div>
          <div class="bubble in">עזבי. רק רציתי לדבר.</div>
          <div class="bubble in">את עדיין מחכה ליד השער האחורי?</div>
          <div class="bubble in">ואל תספרי להורים עדיין.</div>

          <div class="alert-card">
            <div class="alert-title">⚠️ Echo Guard זיהה הסלמה חריגה</div>
            <div class="alert-meta">
              איש קשר לא מוכר מציג ידע על שגרת הילד, מנסה לברר מיקום ומבקש לשמור את הקשר בסוד.
            </div>
            <div class="tags">
              <span class="tag">איש קשר לא מוכר</span>
              <span class="tag">בירור מיקום</span>
              <span class="tag">סודיות</span>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section class="story-visual">
      <img
        src="/og-guard-v2.png"
        alt="Echo Guard — רשת ביטחון חכמה ל-WhatsApp של הילדים"
        width="1734"
        height="907"
      >
    </section>

    <section>
      <h2 class="section-title">מה Echo Guard מחפש?</h2>
      <div class="grid3">
        <div class="card">
          <div class="icon">🕵️</div>
          <h3>פניות חשודות וזרים</h3>
          <p>בקשות למידע אישי, מיקום, תמונות, סודיות, מעבר לפלטפורמה אחרת או ניסיון להיפגש.</p>
        </div>
        <div class="card">
          <div class="icon">💬</div>
          <h3>חרם, השפלה והטרדה</h3>
          <p>שפה פוגענית שחוזרת, לחץ קבוצתי, הדרה, איומים והטרדה מתמשכת.</p>
        </div>
        <div class="card">
          <div class="icon">💙</div>
          <h3>מצוקה רגשית</h3>
          <p>ניסיון לזהות כשהשיחה עוברת מביטוי רגעי לדפוס שמצריך תשומת לב.</p>
        </div>
      </div>
      <p class="parent-disclaimer">
        Echo Guard הוא כלי עזר להורים — הוא לא מחליף שיחה, שיקול דעת או עזרה מקצועית.
      </p>
    </section>

    <section id="connect">
      <div class="connect-wrap">
        <div class="connect-head">
          <div class="eyebrow">חיבור פשוט ל-WhatsApp</div>
          <h2>דקה־שתיים, והמערכת מחוברת.</h2>
          <p>
            מחברים את ה-WhatsApp של הילד כמו שמחברים WhatsApp Web —
            בלי להתקין אפליקציה נוספת על הטלפון.
          </p>
        </div>

        <div class="connect-steps">
          <div class="connect-step">
            <div class="connect-num">1</div>
            <h3>סורקים QR</h3>
            <p>
              פותחים ב-WhatsApp את “מכשירים מקושרים” וסורקים את הקוד.
              אם אין מצלמה זמינה, אפשר להתחבר גם באמצעות קוד חד־פעמי.
            </p>
          </div>
          <div class="connect-step">
            <div class="connect-num">2</div>
            <h3>Echo Guard מתחיל לנתח</h3>
            <p>
              הודעות טקסט נכנסות ויוצאות משמשות כדי להבין הקשר,
              לזהות דפוסים ולהתריע כשמשהו באמת דורש תשומת לב.
            </p>
          </div>
          <div class="connect-step">
            <div class="connect-num">3</div>
            <h3>אפשר לנתק בכל רגע</h3>
            <p>
              מסירים את החיבור מתוך WhatsApp ← הגדרות ← מכשירים מקושרים,
              והגישה להודעות חדשות נעצרת מיד.
            </p>
          </div>
        </div>

        <div class="connect-trust">
          <div class="connect-icon">✓</div>
          <div>
            <strong>שקוף מההתחלה.</strong>
            החיבור מופיע ב-WhatsApp כמכשיר מקושר. הוא לא מוסתר, ולא דורש גישה מרחוק לטלפון.
          </div>
        </div>
      </div>
    </section>

    <section class="waitlist-section" id="waitlist">
      <div class="waitlist-card">
        <div id="form-view">
          <div class="scarcity">⏳ 50 המקומות הראשונים בלבד</div>
          {{COUNTER}}
          <h2>מצטרפים לפיילוט של Echo Guard</h2>
          <p class="form-sub">
            השאירו שם ומספר WhatsApp. ניצור קשר כשנפתח את הגל הראשון למשפחות.
          </p>

          <form id="waitlist-form">
            <input
              type="text"
              id="wl-name"
              placeholder="שם מלא"
              maxlength="80"
              autocomplete="name"
              required
            >
            <input
              type="tel"
              id="wl-phone"
              placeholder="מספר WhatsApp (למשל 050-1234567)"
              maxlength="20"
              autocomplete="tel"
              required
            >

            <div class="waitlist-field">
              <label for="wl-children-count">כמה ילדים תרצו לחבר?</label>
              <select id="wl-children-count" required>
                <option value="" selected disabled>בחרו מספר ילדים</option>
                <option value="1">ילד אחד</option>
                <option value="2">2 ילדים</option>
                <option value="3">3 ילדים</option>
                <option value="4">4 ילדים</option>
                <option value="5_plus">5 ומעלה</option>
              </select>
            </div>

            <div class="waitlist-field">
              <label for="wl-children-ages">מה הגילאים?</label>
              <input
                type="text"
                id="wl-children-ages"
                placeholder="למשל: 9, 12, 15"
                maxlength="60"
                inputmode="numeric"
                required
              >
            </div>

            <button type="submit" id="wl-submit">שריינו לי מקום</button>
          </form>

          <div class="waitlist-note">בלי התחייבות. ניצור קשר ב-WhatsApp.</div>
          <div class="waitlist-error" id="wl-error"></div>
        </div>

        <div class="waitlist-success" id="success-view">
          <div class="success-icon">✓</div>
          <h3>אתם ברשימה.</h3>
          <p>נשלח הודעת WhatsApp כשהפיילוט ייפתח עבורכם.</p>
        </div>
      </div>
    </section>
  </main>

  <div class="footer">Echo Guard</div>
</div>

<script>
const form = document.getElementById("waitlist-form");
const errEl = document.getElementById("wl-error");
const submitBtn = document.getElementById("wl-submit");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errEl.style.display = "none";

  const name = document.getElementById("wl-name").value.trim();
  const phone = document.getElementById("wl-phone").value.trim();
  const childrenCount = document.getElementById("wl-children-count").value;
  const childrenAges = document.getElementById("wl-children-ages").value.trim();

  if (!name || !phone || !childrenCount || !childrenAges) return;

  submitBtn.disabled = true;
  submitBtn.textContent = "רק רגע...";

  try {
    const response = await fetch("/api/waitlist", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        name,
        phone,
        children_count: childrenCount,
        children_ages: childrenAges,
      }),
    });

    if (response.status === 422) {
      showError("המספר לא נראה תקין — נסו שוב עם מספר WhatsApp ישראלי.");
      return;
    }

    if (!response.ok) {
      showError("משהו השתבש. נסו שוב בעוד רגע.");
      return;
    }

    document.getElementById("form-view").style.display = "none";
    document.getElementById("success-view").style.display = "block";
  } catch (_error) {
    showError("משהו השתבש. נסו שוב בעוד רגע.");
  }
});

function showError(message) {
  errEl.textContent = message;
  errEl.style.display = "block";
  submitBtn.disabled = false;
  submitBtn.textContent = "שריינו לי מקום";
}
</script>
</body>
</html>
"""
