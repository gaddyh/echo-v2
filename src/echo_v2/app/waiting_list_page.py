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
.progress-bar {
  text-align: center;
  padding: 8px 0 4px;
}
.progress-bar .count { font-size: 0.9rem; color: var(--text-secondary); }
.progress-bar .bar {
  max-width: 300px;
  height: 4px;
  background: var(--border);
  border-radius: 2px;
  margin: 6px auto 0;
  overflow: hidden;
}
.progress-bar .bar .fill {
  height: 100%;
  background: var(--primary);
  border-radius: 2px;
  transition: width 0.3s ease;
}
.card-stack { position: relative; }
.card-peek {
  position: absolute;
  top: 8px;
  left: 8px;
  right: 8px;
  bottom: -8px;
  background: var(--card);
  border-radius: 12px;
  box-shadow: var(--shadow);
  z-index: 0;
  opacity: 0.4;
}
.card-current {
  position: relative;
  z-index: 1;
}
.card-current.removing { opacity: 0; transform: translateY(-20px); transition: opacity 0.25s, transform 0.25s; }
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
.card .name { font-weight: 600; font-size: 1.05rem; display: flex; align-items: center; gap: 6px; }
.card .name .star-btn {
  background: none; border: none; cursor: pointer;
  font-size: 1.2rem; line-height: 1; padding: 8px;
  min-width: 44px; min-height: 44px;
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: 50%; transition: background 0.15s;
  -webkit-tap-highlight-color: transparent;
}
.card .name .star-btn:hover { background: rgba(0,0,0,0.05); }
.card .name .star-btn:disabled { opacity: 0.5; cursor: wait; }
.card .name .star-btn.starred { color: #e8a800; }
.card .name .star-btn:not(.starred) { color: var(--text-secondary); opacity: 0.5; }
.card .name .name-text { flex: 1; }
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
.card .summary {
  font-size: 0.95rem;
  color: var(--text);
  margin-top: 8px;
  line-height: 1.4;
}
.card .quote {
  font-size: 0.82rem;
  color: var(--text-secondary);
  margin-top: 6px;
  padding: 6px 10px;
  background: var(--bg);
  border-radius: 6px;
  border-right: 2px solid var(--border);
  font-style: italic;
}
.context-link {
  display: inline-block;
  margin-top: 6px;
  color: var(--primary);
  font-size: 0.82rem;
  text-decoration: none;
  cursor: pointer;
}
.context-link:hover { text-decoration: underline; }
.actions { margin-top: 12px; display: flex; flex-direction: column; gap: 8px; }
.btn-done {
  flex: 1;
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
.btn-secondary.muted {
  color: var(--text);
  background: #f1f3f5;
  border-color: #e0e4e8;
}
.btn-secondary.muted:hover { background: #e4e7eb; }
.btn-secondary.send {
  border-color: #3b82f6;
  color: #3b82f6;
  background: #eff6ff;
}
.btn-secondary.send:hover { background: #3b82f6; color: white; }
.snooze-other {
  font-size: 0.85rem;
  color: var(--text-secondary);
  text-decoration: none;
  cursor: pointer;
}
.snooze-other:hover { color: var(--primary); }
.snooze-other.false-positive { color: var(--danger); }
.snooze-other.false-positive:hover { color: #d33a47; }
.link-row { display: flex; gap: 8px; justify-content: center; margin-top: 6px; }
.link-row .snooze-other { flex: 1; text-align: center; }
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
.summary .stats-line { font-size: 0.95rem; color: var(--text); margin-top: 10px; line-height: 1.8; }
.summary .stats-line .sep { color: var(--border); margin: 0 6px; }
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
.overlay textarea {
  width: 100%;
  min-height: 80px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  font-size: 1rem;
  font-family: inherit;
  resize: vertical;
  margin-bottom: 12px;
  box-sizing: border-box;
}
.context-messages {
  max-height: 55vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 12px;
}
.context-message {
  max-width: 86%;
  padding: 8px 10px;
  border-radius: 10px;
  font-size: 0.88rem;
  line-height: 1.4;
  white-space: pre-wrap;
  word-break: break-word;
}
.context-message.inbound {
  align-self: flex-start;
  background: #e8f5e9;
  border-right: 3px solid var(--primary);
}
.context-message.outbound {
  align-self: flex-end;
  background: var(--bg);
  border-left: 3px solid var(--border);
}
.context-message-meta {
  display: block;
  color: var(--text-secondary);
  font-size: 0.72rem;
  margin-bottom: 3px;
}
.send-templates {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 12px;
}
.send-template {
  padding: 6px 12px;
  border: 1px solid var(--border);
  border-radius: 16px;
  background: var(--card);
  color: var(--text-secondary);
  font-size: 0.82rem;
  cursor: pointer;
  transition: background 0.2s, color 0.2s;
}
.send-template:hover { background: var(--primary); color: white; border-color: var(--primary); }
.overlay .overlay-submit {
  width: 100%;
  padding: 12px;
  border: none;
  border-radius: 8px;
  background: var(--primary);
  color: white;
  font-size: 1rem;
  font-weight: 600;
  cursor: pointer;
  margin-top: 8px;
}
.overlay .overlay-submit:hover { background: var(--primary-hover); }
.overlay .overlay-submit:disabled { opacity: 0.5; cursor: not-allowed; }
.card .toast {
  margin-top: 8px;
  padding: 8px 12px;
  border: 1px solid var(--primary);
  border-radius: 8px;
  background: #eafbf2;
  color: #1a8f4d;
  font-size: 0.9rem;
  text-align: center;
}
.loading { text-align: center; padding: 40px; color: var(--text-secondary); }
.error-page { text-align: center; padding: 40px 20px; }
.error-page h2 { font-size: 1.2rem; margin-bottom: 12px; }

/* --- filter chips bar --- */
.filter-bar {
  position: sticky;
  top: 0;
  z-index: 50;
  background: var(--bg);
  padding: 8px 0;
  margin: 0 -16px 8px;
  overflow-x: auto;
  white-space: nowrap;
  scrollbar-width: none;
  -ms-overflow-style: none;
}
.filter-bar::-webkit-scrollbar { display: none; }
.filter-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 6px 12px;
  border: 1px solid var(--border);
  border-radius: 16px;
  background: var(--card);
  color: var(--text-secondary);
  font-size: 0.82rem;
  cursor: pointer;
  transition: background 0.15s, color 0.15s, border-color 0.15s;
  -webkit-tap-highlight-color: transparent;
}
.filter-chip:hover { background: var(--bg); }
.filter-chip.active {
  background: var(--text);
  color: white;
  border-color: var(--text);
}

/* --- swipe navigation --- */
.card-swipe-wrap {
  position: relative;
  overflow: hidden;
}
.card-current.swiping {
  transition: none;
}
.card-nav-chevrons {
  display: flex;
  justify-content: space-between;
  margin-top: 4px;
  gap: 8px;
}
.card-nav-chevrons button {
  flex: 1;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--card);
  color: var(--text-secondary);
  font-size: 0.85rem;
  cursor: pointer;
  transition: background 0.15s;
}
.card-nav-chevrons button:hover { background: var(--bg); }
.card-nav-chevrons button:disabled { opacity: 0.3; cursor: default; }
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

<!-- Send overlay (תזמון הודעה) -->
<div class="overlay" id="send-overlay">
  <div class="overlay-title">תזמון הודעה</div>
  <div class="send-templates">
    <button class="send-template" data-template="קיבלתי, בודק וחוזר אלייך">קיבלתי, בודק וחוזר</button>
    <button class="send-template" data-template="אחזור אלייך בהמשך היום">אחזור בהמשך היום</button>
    <button class="send-template" data-template="אפשר לדבר מחר בבוקר?">לדבר מחר בבוקר?</button>
    <button class="send-template" data-template="תודה, מטפל בזה">תודה, מטפל בזה</button>
  </div>
  <textarea id="send-message" placeholder="מה לשלוח?" maxlength="1000"></textarea>
  <button class="overlay-option" data-send-preset="now">עכשיו</button>
  <button class="overlay-option" data-send-preset="1h">עוד שעה</button>
  <button class="overlay-option" data-send-preset="3h">עוד 3 שעות</button>
  <button class="overlay-option" data-send-preset="tomorrow">מחר בבוקר</button>
  <input type="datetime-local" id="send-at">
  <button class="overlay-submit" id="send-submit" onclick="submitSend()">תזמן</button>
  <button class="overlay-close" onclick="closeSend()">ביטול</button>
</div>

<!-- Context overlay (הודעות +) -->
<div class="overlay" id="context-overlay">
  <div class="overlay-title">הודעות אחרונות</div>
  <div class="context-messages" id="context-messages"></div>
  <button class="overlay-close" onclick="closeContext()">סגירה</button>
</div>

<script>
const BOT_PHONE = "{{BOT_PHONE}}";
let pendingAction = null; // {activeId, action, cardEl, expectedVersion, requestId}
let queue = [];        // authoritative current items from server
let filteredQueue = []; // derived from queue + active filters
let currentIndex = 0; // index inside filteredQueue
let totalStarted = 0;  // queue length at load (for progress)
let stats = {done: 0, snoozed: 0, scheduled: 0, not_needed: 0};
let activeFilters = {starred: false};

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
  queue = data.items;
  totalStarted = queue.length;
  stats = {done: 0, snoozed: 0, scheduled: 0, not_needed: 0};
  activeFilters = {starred: false};
  recomputeFilteredQueue(null);
  renderCurrent();
}

// --- Queue/filter state machine ---

function recomputeFilteredQueue(preserveActiveId) {
  filteredQueue = queue.filter(item => {
    if (activeFilters.starred && !item.is_starred) return false;
    return true;
  });
  // Preserve current selection if possible, else clamp.
  if (preserveActiveId) {
    const idx = filteredQueue.findIndex(i => i.active_id === preserveActiveId);
    if (idx >= 0) {
      currentIndex = idx;
      return;
    }
  }
  if (currentIndex >= filteredQueue.length) {
    currentIndex = Math.max(0, filteredQueue.length - 1);
  }
}

function currentItem() {
  return filteredQueue[currentIndex] || null;
}

function renderCurrent() {
  const app = document.getElementById("app");
  if (filteredQueue.length === 0) {
    if (queue.length === 0) {
      renderComplete();
    } else {
      renderEmptyFiltered();
    }
    return;
  }
  const item = currentItem();
  const next = filteredQueue[currentIndex + 1];
  const prev = filteredQueue[currentIndex - 1];
  const processed = totalStarted - queue.length;
  const backUrl = "https://wa.me/" + BOT_PHONE + "?text=" + encodeURIComponent("סיימתי לעבור על רשימת ההמתנה ✅, תודה");
  let html = '<div class="back-bar"><a href="' + backUrl + '">← חזרה ל־WhatsApp</a></div>';
  // Progress bar
  html += '<div class="progress-bar">'
    + '<div class="count">' + (currentIndex + 1) + ' מתוך ' + filteredQueue.length + '</div>'
    + '<div class="bar"><div class="fill" style="width:' + (currentIndex / Math.max(filteredQueue.length, 1) * 100) + '%"></div></div>'
    + '</div>';
  // Filter chips bar
  html += renderFilterBar();
  // Card stack with peek
  html += '<div class="card-stack card-swipe-wrap">';
  if (next) {
    html += '<div class="card-peek"></div>';
  }
  html += renderCard(item);
  html += '</div>';
  // Navigation chevrons (desktop-friendly)
  html += '<div class="card-nav-chevrons">'
    + '<button onclick="navPrev()" ' + (currentIndex <= 0 ? "disabled" : "") + '>→ הקודם</button>'
    + '<button onclick="navNext()" ' + (currentIndex >= filteredQueue.length - 1 ? "disabled" : "") + '>הבא ←</button>'
    + '</div>';
  app.innerHTML = html;
  const card = app.querySelector(".card");
  card.classList.add("card-current");
  setCardText(card, item);
  attachCardListeners(card);
  attachSwipeHandlers(card);
}

function renderFilterBar() {
  let html = '<div class="filter-bar" id="filter-bar">';
  // All chip
  const noFilters = !activeFilters.starred;
  html += '<span class="filter-chip' + (noFilters ? " active" : "") + '" data-filter="all" onclick="clearFilters()">הכל</span>';
  // Starred chip
  html += '<span class="filter-chip' + (activeFilters.starred ? " active" : "") + '" data-filter="starred" onclick="toggleFilterStarred()">★</span>';
  html += '</div>';
  return html;
}

function clearFilters() {
  activeFilters = {starred: false};
  recomputeFilteredQueue(currentItem() ? currentItem().active_id : null);
  renderCurrent();
}

function toggleFilterStarred() {
  activeFilters.starred = !activeFilters.starred;
  recomputeFilteredQueue(currentItem() ? currentItem().active_id : null);
  renderCurrent();
}

function navNext() {
  if (currentIndex < filteredQueue.length - 1) {
    currentIndex++;
    renderCurrent();
  }
}

function navPrev() {
  if (currentIndex > 0) {
    currentIndex--;
    renderCurrent();
  }
}

function renderCard(item) {
  const waitingTime = formatWaitingTime(item.waiting_hours);
  return '<div class="card" data-active-id="' + item.active_id + '" data-version="' + item.expected_version + '" data-starred="' + (item.is_starred ? "1" : "0") + '">'
    + '<div class="name">'
    + '<button class="star-btn' + (item.is_starred ? " starred" : "") + '" data-action="star" aria-label="סמן איש קשר כחשוב" aria-pressed="' + (item.is_starred ? "true" : "false") + '">' + (item.is_starred ? "★" : "☆") + '</button>'
    + '<span class="name-text"></span>'
    + '</div>'
    + '<div class="meta">ממתין ' + waitingTime + '</div>'
    + '<div class="summary"></div>'
    + '<div class="quote"></div>'
    + '<a class="context-link" data-action="context">הודעות +</a>'
    + '<div class="actions">'
    + '<div class="btn-row">'
    + '<button class="btn-done" data-action="done">בוצע</button>'
    + '<button class="btn-secondary send" data-action="send">תזמן הודעה</button>'
    + '</div>'
    + '<div class="btn-row">'
    + '<button class="btn-secondary" data-action="snooze">נודניק לשעה</button>'
    + '<button class="btn-secondary muted" data-action="not_needed">לא מעניין, שיחכו</button>'
    + '</div>'
    + '<div class="link-row">'
    + '<a class="snooze-other" data-action="snooze_other">זמן אחר</a>'
    + '<a class="snooze-other false-positive" data-action="false_positive">לא מחכים לי</a>'
    + '</div>'
    + '</div></div>';
}

function setCardText(card, item) {
  card.querySelector(".name-text").textContent = item.contact_name || "לא ידוע";
  const summaryEl = card.querySelector(".summary");
  const quoteEl = card.querySelector(".quote");
  const summary = item.situation_summary || item.message_preview || "שלח/ה הודעה";
  summaryEl.textContent = summary;
  if (item.situation_summary && item.message_preview) {
    quoteEl.textContent = "הודעה אחרונה: \u201C" + item.message_preview + "\u201D";
    quoteEl.style.display = "";
  } else {
    quoteEl.style.display = "none";
  }
}

function attachCardListeners(card) {
  const itemId = card.dataset.activeId;
  const version = parseInt(card.dataset.version);
  card.querySelectorAll("button, .snooze-other, .context-link").forEach(btn => {
    btn.addEventListener("click", (e) => {
      // Prevent swipe handler from also firing.
      e.stopPropagation();
      const action = btn.dataset.action;
      handleAction(card, itemId, version, action);
    });
  });
}

// --- Swipe navigation ---

function attachSwipeHandlers(card) {
  let startX = 0, startY = 0, dx = 0, dy = 0, swiping = false;
  let gestureOnControl = false;

  card.addEventListener("touchstart", (e) => {
    if (e.touches.length !== 1) return;
    startX = e.touches[0].clientX;
    startY = e.touches[0].clientY;
    dx = 0; dy = 0;
    swiping = true;
    // Check if gesture started on a button or input.
    const target = e.target;
    gestureOnControl = !!target.closest("button, .snooze-other, input, .filter-chip");
  }, {passive: true});

  card.addEventListener("touchmove", (e) => {
    if (!swiping || e.touches.length !== 1) return;
    dx = e.touches[0].clientX - startX;
    dy = e.touches[0].clientY - startY;
    if (gestureOnControl) return;
    // Only translate if horizontal-dominant.
    if (Math.abs(dx) > Math.abs(dy)) {
      card.classList.add("swiping");
      card.style.transform = "translateX(" + dx + "px)";
    }
  }, {passive: true});

  card.addEventListener("touchend", () => {
    if (!swiping) return;
    swiping = false;
    card.classList.remove("swiping");
    card.style.transform = "";
    if (gestureOnControl) return;
    const threshold = 80;
    if (Math.abs(dx) > threshold && Math.abs(dx) > Math.abs(dy) * 1.2) {
      // RTL: swipe right (positive dx) = previous, swipe left (negative dx) = next.
      if (dx > 0) {
        navPrev();
      } else {
        navNext();
      }
    }
  });
}

// --- Star toggle ---

async function toggleStar(card, activeId) {
  const starBtn = card.querySelector(".star-btn");
  if (!starBtn || starBtn.disabled) return;
  const wasStarred = starBtn.classList.contains("starred");
  const newStarred = !wasStarred;
  // Optimistic update.
  starBtn.classList.toggle("starred", newStarred);
  starBtn.textContent = newStarred ? "★" : "☆";
  starBtn.setAttribute("aria-pressed", newStarred ? "true" : "false");
  starBtn.disabled = true;
  // Update local queue immediately.
  const item = queue.find(i => i.active_id === activeId);
  if (item) item.is_starred = newStarred;
  try {
    const resp = await fetchAPI("/items/" + activeId + "/star", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({is_starred: newStarred}),
    });
    if (!resp) return;
    if (resp.outcome === "updated") {
      card.dataset.starred = resp.is_starred ? "1" : "0";
      // If starred filter is active and we just un-starred, recompute.
      if (activeFilters.starred && !newStarred) {
        recomputeFilteredQueue(null);
        renderCurrent();
      }
    } else if (resp.outcome === "not_found") {
      // Revert.
      starBtn.classList.toggle("starred", wasStarred);
      starBtn.textContent = wasStarred ? "★" : "☆";
      starBtn.setAttribute("aria-pressed", wasStarred ? "true" : "false");
      if (item) item.is_starred = wasStarred;
      showRetry(card, "הפריט לא נמצא.");
    }
  } catch (e) {
    starBtn.classList.toggle("starred", wasStarred);
    starBtn.textContent = wasStarred ? "★" : "☆";
    starBtn.setAttribute("aria-pressed", wasStarred ? "true" : "false");
    if (item) item.is_starred = wasStarred;
    showRetry(card, "בעיית רשת. נסה שוב.");
  } finally {
    starBtn.disabled = false;
  }
}

async function openContextViewer(activeId) {
  const overlay = document.getElementById("context-overlay");
  const messagesEl = document.getElementById("context-messages");
  messagesEl.textContent = "טוען הודעות...";
  overlay.classList.add("active");

  try {
    const data = await fetchAPI("/items/" + activeId + "/context?limit=8", {});
    if (!data) return;
    messagesEl.textContent = "";
    if (!data.messages || data.messages.length === 0) {
      messagesEl.textContent = "אין הודעות להצגה.";
      return;
    }
    for (const message of data.messages) {
      const bubble = document.createElement("div");
      bubble.className = "context-message " + (message.direction === "outbound" ? "outbound" : "inbound");
      const meta = document.createElement("span");
      meta.className = "context-message-meta";
      meta.textContent = new Date(message.timestamp).toLocaleString("he-IL");
      const text = document.createElement("span");
      text.textContent = message.text || "[הודעה ללא טקסט]";
      bubble.appendChild(meta);
      bubble.appendChild(text);
      messagesEl.appendChild(bubble);
    }
  } catch (_err) {
    messagesEl.textContent = "לא ניתן לטעון את ההודעות כרגע.";
  }
}

function closeContext() {
  document.getElementById("context-overlay").classList.remove("active");
  document.getElementById("context-messages").textContent = "";
}

// --- Actions ---

function actionStatType(action) {
  if (action === "done") return "done";
  if (action === "snooze" || action === "tomorrow" || action === "snooze_other") return "snoozed";
  if (action === "not_needed") return "not_needed";
  if (action === "false_positive") return "done";
  return null;
}

async function handleAction(card, activeId, expectedVersion, action) {
  if (action === "star") {
    await toggleStar(card, activeId);
    return;
  }
  if (action === "context") {
    await openContextViewer(activeId);
    return;
  }
  if (action === "snooze") {
    await sendAction(card, activeId, expectedVersion, "snooze", {snooze_preset: "1h"});
    return;
  }
  if (action === "tomorrow") {
    await sendAction(card, activeId, expectedVersion, "snooze", {snooze_preset: "tomorrow"});
    return;
  }
  if (action === "not_needed") {
    await sendAction(card, activeId, expectedVersion, "dismiss", {dismiss_reason: "no_response_required"});
    return;
  }
  if (action === "false_positive") {
    await sendAction(card, activeId, expectedVersion, "dismiss", {dismiss_reason: "detected_incorrectly"});
    return;
  }
  if (action === "snooze_other") {
    pendingAction = {activeId, action: "snooze", card, expectedVersion};
    document.getElementById("snooze-overlay").classList.add("active");
    return;
  }
  if (action === "send") {
    pendingAction = {
      activeId,
      action: "send",
      card,
      expectedVersion,
      requestId: crypto.randomUUID(),
    };
    document.getElementById("send-message").value = "";
    document.getElementById("send-at").value = "";
    document.getElementById("send-submit").disabled = false;
    document.getElementById("send-overlay").classList.add("active");
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
    if (resp.outcome === "applied" || resp.outcome === "duplicate" || resp.outcome === "not_found") {
      removeFromQueue(activeId, actionStatType(action));
    } else if (resp.outcome === "stale") {
      if (resp.item) {
        card.dataset.version = resp.item.expected_version;
        setCardText(card, resp.item);
      }
      buttons.forEach(b => b.disabled = false);
      showRetry(card, "השיחה השתנתה. נסה שוב.");
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

function removeFromQueue(activeId, statType) {
  if (statType) stats[statType]++;
  const card = document.querySelector(".card-current");
  if (card) card.classList.add("removing");
  setTimeout(() => {
    // Find and remove from queue.
    const queueIdx = queue.findIndex(i => i.active_id === activeId);
    if (queueIdx >= 0) queue.splice(queueIdx, 1);
    // Recompute filtered queue, preserving position.
    const wasAt = currentIndex;
    recomputeFilteredQueue(null);
    // If we removed the current item, stay at same index (now points to next).
    // If we removed an item before current, index already adjusted by recompute.
    // Clamp.
    if (currentIndex >= filteredQueue.length) {
      currentIndex = Math.max(0, filteredQueue.length - 1);
    }
    renderCurrent();
  }, 250);
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

function renderEmptyFiltered() {
  const app = document.getElementById("app");
  const backUrl = "https://wa.me/" + BOT_PHONE + "?text=" + encodeURIComponent("סיכום חדש בבקשה");
  app.innerHTML = '<div class="summary">'
    + '<div class="icon">🔍</div>'
    + '<h2>אין פריטים בסינון הנוכחי</h2>'
    + '<div class="stats-line">סה"כ ' + queue.length + ' פריטים ברשימה</div>'
    + '<a class="back-btn" href="#" onclick="clearFilters(); return false;" style="margin-top:16px;display:inline-block;padding:10px 24px;background:var(--text);color:white;border-radius:20px;font-size:0.9rem;text-decoration:none;">נקה סינון</a>'
    + '</div>';
}

function renderComplete() {
  const app = document.getElementById("app");
  const backUrl = "https://wa.me/" + BOT_PHONE + "?text=" + encodeURIComponent("סיימתי לעבור על רשימת ההמתנה ✅, תודה");
  const parts = [];
  if (stats.done) parts.push(stats.done + " בוצעו");
  if (stats.snoozed) parts.push(stats.snoozed + " נדחו");
  if (stats.scheduled) parts.push(stats.scheduled + " הודעות תוזמנו");
  if (stats.not_needed) parts.push(stats.not_needed + " לא לטיפול");
  const statsLine = parts.length ? parts.join(" · ") : "אין פעולות";
  app.innerHTML = '<div class="summary">'
    + '<div class="icon">✅</div>'
    + '<h2>סיימת לעבור על הרשימה</h2>'
    + '<div class="stats-line">' + statsLine + '</div>'
    + '<a class="back-btn" href="' + backUrl + '">חזרה ל־WhatsApp</a>'
    + '</div>';
}

function formatWaitingTime(hours) {
  const minutes = Math.max(0, Math.floor(hours * 60));
  if (minutes < 1) return "עכשיו";
  if (minutes < 60) return minutes + " דקות";

  const roundedHours = Math.floor(minutes / 60);
  if (roundedHours < 24) {
    return roundedHours === 1 ? "שעה" : roundedHours + " שעות";
  }

  const days = Math.floor(roundedHours / 24);
  return days === 1 ? "יום" : days + " ימים";
}

// --- Keyboard navigation (desktop) ---
document.addEventListener("keydown", (e) => {
  // Don't interfere with inputs.
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
  // Don't fire if an overlay is open.
  if (document.querySelector(".overlay.active")) return;
  if (e.key === "ArrowLeft") { navNext(); e.preventDefault(); }
  if (e.key === "ArrowRight") { navPrev(); e.preventDefault(); }
});

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

function closeSnooze() { document.getElementById("snooze-overlay").classList.remove("active"); }
function closeSend() {
  document.getElementById("send-overlay").classList.remove("active");
  pendingAction = null;
}

// Send overlay: template chips fill the textarea.
document.querySelectorAll("#send-overlay .send-template").forEach(btn => {
  btn.addEventListener("click", () => {
    const tpl = btn.dataset.template;
    document.getElementById("send-message").value = tpl;
    document.getElementById("send-message").focus();
  });
});

// Send overlay: preset buttons select a preset and submit.
document.querySelectorAll("#send-overlay .overlay-option[data-send-preset]").forEach(btn => {
  btn.addEventListener("click", () => {
    const preset = btn.dataset.sendPreset;
    if (pendingAction) {
      submitSendWith({send_preset: preset});
    }
  });
});

async function submitSend() {
  const msg = document.getElementById("send-message").value;
  const at = document.getElementById("send-at").value;
  if (!msg.trim()) {
    showSendError("יש להזין הודעה.");
    return;
  }
  if (!at) {
    showSendError("יש לבחור זמן או להשתמש באחד הכפתורים.");
    return;
  }
  // Convert datetime-local (naive) to offset-aware ISO via Date.
  const sendAt = new Date(at).toISOString();
  await submitSendWith({send_at: sendAt});
}

async function submitSendWith(extra) {
  if (!pendingAction) return;
  const msg = document.getElementById("send-message").value;
  if (!msg.trim()) {
    showSendError("יש להזין הודעה.");
    return;
  }
  const submitBtn = document.getElementById("send-submit");
  submitBtn.disabled = true;
  const body = {
    request_id: pendingAction.requestId,
    message: msg,
  };
  Object.assign(body, extra);
  try {
    const resp = await fetchAPI("/items/" + pendingAction.activeId + "/send", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    if (!resp) {
      submitBtn.disabled = false;
      return;
    }
    if (resp.outcome === "scheduled" || resp.outcome === "duplicate") {
      const activeId = pendingAction.activeId;
      closeSend();
      removeFromQueue(activeId, "scheduled");
    } else if (resp.outcome === "not_found") {
      showSendError("הפריט לא נמצא או שייך למשתמש אחר.");
      submitBtn.disabled = false;
    } else if (resp.outcome === "invalid") {
      showSendError("ההודעה או הזמן לא תקינים.");
      submitBtn.disabled = false;
    } else {
      showSendError("שגיאה. נסה שוב.");
      submitBtn.disabled = false;
    }
  } catch (e) {
    showSendError("בעיית רשת. נסה שוב.");
    submitBtn.disabled = false;
  }
}

function showSendError(msg) {
  let retry = document.querySelector("#send-overlay .retry");
  if (!retry) {
    retry = document.createElement("div");
    retry.className = "retry";
    document.getElementById("send-overlay").appendChild(retry);
  }
  retry.textContent = msg;
  retry.onclick = () => { retry.remove(); };
}

function formatScheduledTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const today = new Date();
  const isToday = d.toDateString() === today.toDateString();
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  if (isToday) return "היום ב־" + hh + ":" + mm;
  const dd = String(d.getDate()).padStart(2, "0");
  const mo = String(d.getMonth() + 1).padStart(2, "0");
  return "ב־" + dd + "/" + mo + " " + hh + ":" + mm;
}

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
