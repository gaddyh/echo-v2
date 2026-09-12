"""HTML page constants for the waiting-list mini web app.

Two pages:
* :data:`WAITING_LIST_PAGE` — the main mobile-first single page.
* :data:`EXPIRED_LINK_PAGE` — shown when the token is invalid/expired.

Both are self-contained HTML with inline CSS+JS. No build step, no
external dependencies. ``dir="rtl"``, Hebrew-first.
"""

from __future__ import annotations

__all__ = ["EXPIRED_LINK_PAGE", "WAITING_LIST_PAGE"]


_WAITING_LIST_HTML = r"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>ממתינים לטיפול</title>
<style>
:root {
  --bg: #f0f2f5;
  --card: #ffffff;
  --primary: #25d366;
  --primary-hover: #1eb558;
  --text: #111b21;
  --text-secondary: #667781;
  --border: #e9edef;
  --danger: #ff4757;
  --warn: #f39c12;
  --shadow: 0 1px 2px rgba(0,0,0,0.08);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.5;
  -webkit-text-size-adjust: 100%;
}
.container { max-width: 500px; margin: 0 auto; padding: 16px; }
.header { padding: 12px 0 20px; text-align: center; }
.header h1 { font-size: 1.4rem; font-weight: 600; }
.header .oldest { font-size: 0.85rem; color: var(--text-secondary); margin-top: 4px; }
.back-bar { text-align: center; padding: 8px 0 16px; }
.back-bar a {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 20px;
  background: var(--card);
  color: var(--text-secondary);
  text-decoration: none;
  border-radius: 20px;
  font-size: 0.85rem;
  border: 1px solid var(--border);
}
.card {
  background: var(--card);
  border-radius: 12px;
  padding: 16px;
  margin-bottom: 12px;
  box-shadow: var(--shadow);
  transition: opacity 0.3s, transform 0.3s, max-height 0.3s;
  overflow: hidden;
  max-height: 500px;
}
.card.removing { opacity: 0; transform: scale(0.95); max-height: 0; padding: 0; margin: 0; border: 0; }
.card .name { font-weight: 600; font-size: 1.05rem; }
.card .meta { font-size: 0.8rem; color: var(--text-secondary); margin-top: 2px; }
.card .preview {
  font-size: 0.9rem;
  color: var(--text);
  margin-top: 8px;
  padding: 8px 12px;
  background: var(--bg);
  border-radius: 8px;
  border-right: 3px solid var(--primary);
}
.actions { margin-top: 12px; display: flex; flex-direction: column; gap: 8px; }
.btn-done {
  width: 100%;
  padding: 12px;
  border: none;
  border-radius: 8px;
  background: var(--primary);
  color: white;
  font-size: 1rem;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.2s;
}
.btn-done:hover { background: var(--primary-hover); }
.btn-done:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-row { display: flex; gap: 8px; }
.btn-secondary {
  flex: 1;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--card);
  color: var(--text);
  font-size: 0.9rem;
  cursor: pointer;
  transition: background 0.2s;
}
.btn-secondary:hover { background: var(--bg); }
.btn-secondary:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-secondary.danger { color: var(--danger); }
.snooze-other {
  display: block;
  text-align: center;
  margin-top: 6px;
  font-size: 0.85rem;
  color: var(--text-secondary);
  text-decoration: none;
  cursor: pointer;
}
.snooze-other:hover { color: var(--primary); }
.card .retry {
  margin-top: 8px;
  padding: 8px;
  border: 1px solid var(--warn);
  border-radius: 8px;
  background: #fff9e6;
  color: var(--warn);
  font-size: 0.85rem;
  text-align: center;
  cursor: pointer;
}
.summary {
  text-align: center;
  padding: 40px 20px;
}
.summary .icon { font-size: 3rem; }
.summary h2 { font-size: 1.2rem; margin-top: 12px; }
.summary .stats { font-size: 0.95rem; color: var(--text-secondary); margin-top: 8px; }
.summary .back-btn {
  display: inline-block;
  margin-top: 24px;
  padding: 14px 32px;
  background: var(--primary);
  color: white;
  text-decoration: none;
  border-radius: 24px;
  font-weight: 600;
  font-size: 1rem;
}
.overlay {
  display: none;
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  background: var(--card);
  border-radius: 16px 16px 0 0;
  box-shadow: 0 -2px 10px rgba(0,0,0,0.1);
  padding: 20px 16px 32px;
  z-index: 100;
  max-width: 500px;
  margin: 0 auto;
}
.overlay.active { display: block; }
.overlay .overlay-title { font-weight: 600; font-size: 1.1rem; margin-bottom: 16px; }
.overlay .overlay-option {
  display: block;
  width: 100%;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--card);
  margin-bottom: 8px;
  cursor: pointer;
  font-size: 1rem;
  text-align: right;
  color: var(--text);
}
.overlay .overlay-option:hover { background: var(--bg); }
.overlay .overlay-close {
  width: 100%;
  padding: 12px;
  border: none;
  border-radius: 8px;
  background: var(--bg);
  color: var(--text-secondary);
  font-size: 0.9rem;
  cursor: pointer;
  margin-top: 8px;
}
.overlay input[type="datetime-local"] {
  width: 100%;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  font-size: 1rem;
  margin-bottom: 8px;
}
.loading { text-align: center; padding: 40px; color: var(--text-secondary); }
.error-page { text-align: center; padding: 40px 20px; }
.error-page h2 { font-size: 1.2rem; margin-bottom: 12px; }
</style>
</head>
<body>
<div class="container" id="app">
  <div class="loading">טוען...</div>
</div>

<!-- Snooze overlay (זמן אחר) -->
<div class="overlay" id="snooze-overlay">
  <div class="overlay-title">מתי להזכיר?</div>
  <button class="overlay-option" data-preset="10m">עוד 10 דקות</button>
  <button class="overlay-option" data-preset="1h">עוד שעה</button>
  <button class="overlay-option" data-preset="3h">עוד 3 שעות</button>
  <button class="overlay-option" data-preset="tomorrow">מחר בבוקר</button>
  <button class="overlay-close" onclick="closeSnooze()">ביטול</button>
</div>

<!-- Not-today overlay (לא להיום) -->
<div class="overlay" id="not-today-overlay">
  <div class="overlay-title">למה לא להזכיר שוב היום?</div>
  <button class="overlay-option" data-not-today="tomorrow">יכול לחכות למחר</button>
  <button class="overlay-option" data-not-today="false_positive">זיהוי שגוי</button>
  <button class="overlay-close" onclick="closeNotToday()">ביטול</button>
</div>

<script>
const BOT_PHONE = "{{BOT_PHONE}}";
let pendingAction = null; // {activeId, action, cardEl}

async function fetchAPI(path, options) {
  const resp = await fetch("/api/waiting" + path, options);
  if (resp.status === 401) {
    window.location.href = "/q/expired";
    return null;
  }
  return resp.json();
}

async function loadItems() {
  const data = await fetchAPI("", {});
  if (!data) return;
  renderItems(data);
}

function renderItems(data) {
  const app = document.getElementById("app");
  if (data.items.length === 0) {
    renderSummary(data.summary);
    return;
  }
  const oldestHours = data.items[0].waiting_hours;
  const backUrl = "https://wa.me/" + BOT_PHONE;
  let html = '<div class="back-bar"><a href="' + backUrl + '">← חזרה ל־WhatsApp</a></div>';
  html += '<div class="header"><h1>' + data.items.length + ' ממתינים לטיפול</h1>';
  if (oldestHours > 0) {
    html += '<div class="oldest">הוותיק ביותר מחכה ' + formatHours(oldestHours) + '</div>';
  }
  html += '</div>';
  for (const item of data.items) {
    html += renderCard(item);
  }
  app.innerHTML = html;
  // Set text content safely (textContent, not innerHTML) for XSS safety.
  const cards = document.querySelectorAll(".card");
  data.items.forEach((item, i) => {
    if (cards[i]) setCardText(cards[i], item);
  });
  attachCardListeners();
}

function renderCard(item) {
  const name = item.contact_name || "לא ידוע";
  const preview = item.message_preview ? item.message_preview : "שלח/ה הודעה";
  const hours = formatHours(item.waiting_hours);
  return '<div class="card" data-active-id="' + item.active_id + '" data-version="' + item.expected_version + '">'
    + '<div class="name"></div>'
    + '<div class="meta">ממתין ' + hours + '</div>'
    + '<div class="preview"></div>'
    + '<div class="actions">'
    + '<button class="btn-done" data-action="done">בוצע</button>'
    + '<div class="btn-row">'
    + '<button class="btn-secondary" data-action="snooze">נודניק לשעה</button>'
    + '<button class="btn-secondary danger" data-action="not_today">לא להיום</button>'
    + '</div>'
    + '<a class="snooze-other" data-action="snooze_other">זמן אחר</a>'
    + '</div></div>';
}

function setCardText(card, item) {
  card.querySelector(".name").textContent = item.contact_name || "לא ידוע";
  card.querySelector(".preview").textContent = item.message_preview || "שלח/ה הודעה";
}

function attachCardListeners() {
  document.querySelectorAll(".card").forEach(card => {
    const itemId = card.dataset.activeId;
    const version = parseInt(card.dataset.version);
    // Set text content safely (textContent, not innerHTML).
    // The item data is stored in the card's dataset for XSS safety.
    card.querySelectorAll("button, .snooze-other").forEach(btn => {
      btn.addEventListener("click", () => {
        const action = btn.dataset.action;
        handleAction(card, itemId, version, action);
      });
    });
  });
}

async function handleAction(card, activeId, expectedVersion, action) {
  if (action === "snooze") {
    // "נודניק לשעה" — immediate snooze for 1 hour.
    await sendAction(card, activeId, expectedVersion, "snooze", {snooze_preset: "1h"});
    return;
  }
  if (action === "snooze_other") {
    pendingAction = {activeId, action: "snooze", card, expectedVersion};
    document.getElementById("snooze-overlay").classList.add("active");
    return;
  }
  if (action === "not_today") {
    pendingAction = {activeId, action: "not_today", card, expectedVersion};
    document.getElementById("not-today-overlay").classList.add("active");
    return;
  }
  // done
  await sendAction(card, activeId, expectedVersion, "done");
}

async function sendAction(card, activeId, expectedVersion, action, extra) {
  const buttons = card.querySelectorAll("button");
  buttons.forEach(b => b.disabled = true);
  const actionId = crypto.randomUUID();
  const body = {action_id: actionId, expected_version: expectedVersion, action: action};
  if (extra) Object.assign(body, extra);
  try {
    const resp = await fetchAPI("/items/" + activeId + "/actions", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    if (!resp) return;
    if (resp.outcome === "applied") {
      card.classList.add("removing");
      setTimeout(() => {
        card.remove();
        checkEmpty(resp.summary);
      }, 300);
    } else if (resp.outcome === "duplicate") {
      card.classList.add("removing");
      setTimeout(() => { card.remove(); checkEmpty(resp.summary); }, 300);
    } else if (resp.outcome === "stale") {
      if (resp.item) {
        card.dataset.version = resp.item.expected_version;
        setCardText(card, resp.item);
      }
      buttons.forEach(b => b.disabled = false);
      showRetry(card, "השיחה השתנתה. נסה שוב.");
    } else if (resp.outcome === "not_found") {
      card.classList.add("removing");
      setTimeout(() => { card.remove(); checkEmpty(resp.summary); }, 300);
    } else if (resp.outcome === "invalid_snooze") {
      buttons.forEach(b => b.disabled = false);
      showRetry(card, "זמן הנודניק לא תקין.");
    } else {
      buttons.forEach(b => b.disabled = false);
      showRetry(card, "שגיאה. נסה שוב.");
    }
  } catch (e) {
    buttons.forEach(b => b.disabled = false);
    showRetry(card, "בעיית רשת. נסה שוב.");
  }
}

function showRetry(card, msg) {
  let retry = card.querySelector(".retry");
  if (!retry) {
    retry = document.createElement("div");
    retry.className = "retry";
    card.querySelector(".actions").appendChild(retry);
  }
  retry.textContent = msg;
  retry.onclick = () => { retry.remove(); };
}

function checkEmpty(summary) {
  const cards = document.querySelectorAll(".card");
  if (cards.length === 0) {
    renderSummary(summary);
  }
}

function renderSummary(summary) {
  const app = document.getElementById("app");
  const backUrl = "https://wa.me/" + BOT_PHONE + "?text=" + encodeURIComponent("סיימתי לטפל ברשימה");
  app.innerHTML = '<div class="summary">'
    + '<div class="icon">✅</div>'
    + '<h2>סיימת לעבור על הרשימה</h2>'
    + '<div class="stats">' + summary.completed + ' טופלו · ' + summary.snoozed + ' נדחו</div>'
    + '<a class="back-btn" href="' + backUrl + '">חזרה ל־WhatsApp</a>'
    + '</div>';
}

function formatHours(h) {
  if (h < 1) return Math.round(h * 60) + " דקות";
  if (h < 24) return Math.round(h) + " שעות";
  return Math.round(h / 24) + " ימים";
}

// Snooze overlay handlers
document.querySelectorAll("#snooze-overlay .overlay-option[data-preset]").forEach(btn => {
  btn.addEventListener("click", () => {
    const preset = btn.dataset.preset;
    closeSnooze();
    if (pendingAction) {
      sendAction(pendingAction.card, pendingAction.activeId, pendingAction.expectedVersion, "snooze", {snooze_preset: preset});
      pendingAction = null;
    }
  });
});

// Not-today overlay handlers
document.querySelectorAll("#not-today-overlay .overlay-option[data-not-today]").forEach(btn => {
  btn.addEventListener("click", () => {
    const choice = btn.dataset.notToday;
    closeNotToday();
    if (pendingAction) {
      if (choice === "tomorrow") {
        // Snooze until tomorrow's digest (08:00).
        sendAction(pendingAction.card, pendingAction.activeId, pendingAction.expectedVersion, "snooze", {snooze_preset: "tomorrow"});
      } else if (choice === "false_positive") {
        // Resolve + FALSE_POSITIVE feedback.
        sendAction(pendingAction.card, pendingAction.activeId, pendingAction.expectedVersion, "dismiss", {dismiss_reason: "detected_incorrectly"});
      }
      pendingAction = null;
    }
  });
});

function closeSnooze() { document.getElementById("snooze-overlay").classList.remove("active"); }
function closeNotToday() { document.getElementById("not-today-overlay").classList.remove("active"); }

// Initial load
loadItems();
</script>
</body>
</html>
"""

WAITING_LIST_PAGE = _WAITING_LIST_HTML


_EXPIRED_LINK_HTML = r"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>הקישור פג</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  background: #f0f2f5;
  color: #111b21;
  line-height: 1.5;
}
.container { max-width: 500px; margin: 0 auto; padding: 40px 20px; text-align: center; }
.icon { font-size: 3rem; }
h2 { font-size: 1.2rem; margin-top: 12px; }
p { color: #667781; margin-top: 8px; }
.back-btn {
  display: inline-block;
  margin-top: 24px;
  padding: 14px 32px;
  background: #25d366;
  color: white;
  text-decoration: none;
  border-radius: 24px;
  font-weight: 600;
  font-size: 1rem;
}
</style>
</head>
<body>
<div class="container">
  <div class="icon">⏰</div>
  <h2>הקישור פג או כבר לא תקף</h2>
  <p>בקש סיכום חדש מ-Eco כדי לקבל רשימה מעודכנת.</p>
  <a class="back-btn" href="{{BOT_PHONE_LINK}}">בקש סיכום חדש</a>
</div>
</body>
</html>
"""

EXPIRED_LINK_PAGE = _EXPIRED_LINK_HTML
