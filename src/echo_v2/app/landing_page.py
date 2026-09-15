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
<title>Echo — מי באמת מחכה לך בוואטסאפ?</title>
<meta name="description" content="Echo עוקב אחרי השיחות שלך בוואטסאפ ומזהה מי מחכה לתשובה ממך — כדי ששום דבר לא ייפול בין הכיסאות.">
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

/* --- demo card --- */
.demo { padding: 28px 0 8px; }
.demo-card {
  background: var(--card);
  border-radius: 14px;
  box-shadow: var(--shadow);
  padding: 18px;
  max-width: 400px;
  margin: 0 auto;
  border: 1px solid var(--border);
}
.demo-card .name { font-weight: 700; margin-bottom: 4px; }
.demo-card .meta { color: var(--text-secondary); font-size: 0.82rem; margin-bottom: 10px; }
.demo-card .summary { font-size: 0.95rem; margin-bottom: 8px; }
.demo-card .quote {
  font-size: 0.82rem;
  color: var(--text-secondary);
  font-style: italic;
  background: var(--bg);
  border-radius: 6px;
  border-right: 2px solid var(--border);
  padding: 6px 10px;
}
.demo-card .demo-btn {
  margin-top: 12px;
  width: 100%;
  padding: 10px;
  border: none;
  border-radius: 8px;
  background: var(--primary);
  color: white;
  font-weight: 600;
  font-size: 0.95rem;
  pointer-events: none;
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

/* --- privacy --- */
.privacy {
  padding: 32px 0 8px;
  text-align: center;
}
.privacy .box {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 18px 20px;
  max-width: 460px;
  margin: 0 auto;
  font-size: 0.9rem;
  color: var(--text-secondary);
}
.privacy .box strong { color: var(--text); }

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
    <h1>מי באמת מחכה לך בוואטסאפ?</h1>
    <p class="sub">שאלה שלא ענית עליה. הבטחה ששכחת. לקוח שממתין כבר יומיים. Echo מזהה את השיחות שמחכות לך — לפני שהן נופלות בין הכיסאות.</p>
    <a class="cta" href="#waitlist">אני רוצה גישה מוקדמת</a>
  </div>
</section>

<section class="pain">
  <div class="container">
    <h2>עשרות שיחות ביום. מי נשאר בלי תשובה?</h2>
    <p>וואטסאפ לא מזכיר לך. ההודעה הלא־נענית צוללת למטה, ומי שחיכה לך — עדיין מחכה.</p>
  </div>
</section>

<section class="demo">
  <div class="container">
    <div class="demo-card">
      <div class="name">☆ דנה לוי</div>
      <div class="meta">ממתינה 26 שעות</div>
      <div class="summary">דנה שלחה את החוזה ומחכה לאישור שלך לפני שהיא שולחת ללקוח</div>
      <div class="quote">הודעה אחרונה: &ldquo;תעבור על הסעיף האחרון כשתוכל?&rdquo;</div>
      <button class="demo-btn">בוצע ✓</button>
    </div>
  </div>
</section>

<section class="how">
  <div class="container">
    <h2>איך זה עובד?</h2>
    <div class="step">
      <div class="num">1</div>
      <div>
        <h3>מחברים את הוואטסאפ</h3>
        <p>חיבור מאובטח בדקה, בלי סריקת QR ובלי אפליקציה.</p>
      </div>
    </div>
    <div class="step">
      <div class="num">2</div>
      <div>
        <h3>Echo מבין את השיחות</h3>
        <p>לא רק "מי כתב אחרון" — Echo מזהה בקשות פתוחות, הבטחות שנתת ושאלות שלא נענו.</p>
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

<section class="privacy">
  <div class="container">
    <div class="box">
      <strong>🔒 הפרטיות שלך קודמת לכל.</strong><br>
      ההודעות שלך נשארות שלך. בלי פרסום, בלי מכירת מידע, עם הצפנה ואבטחה ברמה שאנחנו היינו דורשים לעצמנו.
    </div>
  </div>
</section>

<section class="waitlist" id="waitlist">
  <div class="container">
    <div class="form-card">
      <div id="form-view">
        <h2>רשימת המתנה לגישה מוקדמת</h2>
        <p class="form-sub">אנחנו פותחים את Echo בהדרגה. השאירו שם וטלפון ונחזור אליכם בוואטסאפ.</p>
        <form id="waitlist-form">
          <input type="text" id="wl-name" placeholder="שם מלא" maxlength="80" autocomplete="name" required>
          <input type="tel" id="wl-phone" placeholder="מספר וואטסאפ (למשל 050-1234567)" maxlength="20" autocomplete="tel" required>
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

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  errEl.style.display = "none";
  const name = document.getElementById("wl-name").value.trim();
  const phone = document.getElementById("wl-phone").value.trim();
  if (!name || !phone) return;

  submitBtn.disabled = true;
  submitBtn.textContent = "רק רגע...";
  try {
    const resp = await fetch("/api/waitlist", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: name, phone: phone}),
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
</script>
</body>
</html>
"""
