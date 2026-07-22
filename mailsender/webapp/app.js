/* ============================================================
   MailSender web UI — логика интерфейса.
   Общается с Python через window.pywebview.api.*
   ============================================================ */

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

// Активный бэкенд: pywebview (desktop), HTTP (web) или mock (превью дизайна).
let BACKEND = null;
let IS_WEB = false;
function api() { return BACKEND; }

// HTTP-прокси: api().method(a,b) -> POST /api/method  с телом [a,b]
function makeHttpApi() {
  const call = (method, args) => fetch("/api/" + method, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(args),
  }).then(async r => {
    if (r.status === 401) { showLogin(); throw new Error("unauthorized"); }
    return r.json();
  });
  return new Proxy({}, { get: (_, method) => (...args) => call(method, args) });
}

const PAGE_TITLES = {
  board: "Контакты", contacts: "Контакты", finder: "Поиск почты",
  compose: "Письмо и рассылка", replies: "Ответы", settings: "Настройки",
};

let state = { page: "board", contacts: [], search: "" };

/* ---------------- утилиты UI ---------------- */

function toast(msg, kind = "") {
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.textContent = msg;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), 3400);
}

function initials(name, email) {
  const base = (name || email || "?").trim();
  const parts = base.split(/\s+/);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return base.slice(0, 2).toUpperCase();
}

function esc(s) {
  return (s || "").replace(/[&<>"]/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function modal(html) {
  $("#modal").innerHTML = html;
  $("#modal-back").classList.add("show");
}
function closeModal() { $("#modal-back").classList.remove("show"); }
$("#modal-back").addEventListener("click", e => {
  if (e.target.id === "modal-back") closeModal();
});

/* ---------------- навигация ---------------- */

function showPage(page) {
  state.page = page;
  $$(".nav-btn").forEach(b => b.classList.toggle("active", b.dataset.page === page));
  $$(".page").forEach(p => p.classList.remove("active"));
  $("#page-" + page).classList.add("active");
  $("#page-title").textContent = PAGE_TITLES[page] || "";
  $("#board-seg").style.display = page === "board" ? "" : "none";
  // Плавающая кнопка «Новая рассылка» — только на рабочих страницах,
  // не на настройках и не на самом композере.
  const fab = $("#fab");
  if (fab) fab.style.display = (page === "settings" || page === "compose") ? "none" : "";

  if (page === "board") loadBoard();
  if (page === "contacts") loadContacts();
  if (page === "compose") loadComposeMeta();
  if (page === "settings") loadSettings();
}

$$(".nav-btn").forEach(b =>
  b.addEventListener("click", () => showPage(b.dataset.page)));

$$("#board-seg button").forEach(b => b.addEventListener("click", () => {
  $$("#board-seg button").forEach(x => x.classList.remove("active"));
  b.classList.add("active");
  if (b.dataset.view === "list") showPage("contacts"); else showPage("board");
}));

/* ---------------- ДОСКА (канбан) ---------------- */

async function loadBoard() {
  const data = await api().board_data();
  const board = $("#board");
  board.innerHTML = "";
  const fu = $("#board-followups");
  if (fu) fu.textContent = data.followups
    ? `⏰ Ждут повторного касания: ${data.followups} (нет ответа ${data.followup_days}+ дн.)`
    : "";
  const q = state.search.toLowerCase();
  for (const col of data.columns) {
    const cards = col.cards.filter(c =>
      !q || c.name.toLowerCase().includes(q) || c.email.toLowerCase().includes(q)
        || (c.company || "").toLowerCase().includes(q));
    const el = document.createElement("div");
    el.className = "column";
    el.dataset.status = col.status;
    el.innerHTML = `
      <div class="col-head">
        <span class="title">${esc(col.title)}</span>
        <span class="chip">${col.count}</span>
      </div>
      <div class="col-cards"></div>
      <div class="add-card" data-status="${col.status}">
        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>
        Добавить контакт
      </div>`;
    const holder = $(".col-cards", el);
    if (!cards.length) holder.innerHTML = `<div class="empty-hint">Пусто</div>`;
    cards.forEach(c => holder.appendChild(cardEl(c)));
    setupDropzone(el);
    board.appendChild(el);
  }
  $$(".add-card").forEach(a => a.addEventListener("click", () => addContactDialog()));
}

function cardEl(c) {
  const el = document.createElement("div");
  el.className = "card";
  el.draggable = true;
  el.dataset.email = c.email;
  el.innerHTML = `
    <div class="card-top">
      <div class="avatar">${esc(initials(c.name, c.email))}</div>
      <div class="card-name">${esc(c.name)}</div>
      ${c.followup ? '<span class="badge-fu" title="Нет ответа — стоит написать повторно">⏰</span>' : ""}
    </div>
    <div class="card-email">${esc(c.email)}</div>
    ${c.company ? `<div class="card-meta">${esc(c.company)}</div>` : ""}
    ${c.reply ? `<div class="card-reply">↩ ${esc(c.reply.slice(0, 90))}</div>` : ""}`;
  el.addEventListener("click", e => { if (!el.classList.contains("dragging")) openContact(c.id); });
  el.addEventListener("dragstart", e => {
    el.classList.add("dragging");
    e.dataTransfer.setData("text/email", c.email);
  });
  el.addEventListener("dragend", () => el.classList.remove("dragging"));
  return el;
}

const btnBoardSync = $("#btn-board-sync");
if (btnBoardSync) btnBoardSync.onclick = async () => {
  btnBoardSync.disabled = true; btnBoardSync.textContent = "Проверяю…";
  const r = await api().sync_replies();
  btnBoardSync.disabled = false; btnBoardSync.textContent = "↻ Проверить ответы";
  if (!r.ok) { toast(r.message, "err"); return; }
  const n = (r.replied || []).length, u = (r.unsubscribed || []).length, b = (r.bounced || []).length;
  toast(n || u || b ? `Ответили: ${n}, отказов: ${u}, отскоков: ${b}` : "Новых ответов нет", "ok");
  loadBoard();
};

function setupDropzone(col) {
  col.addEventListener("dragover", e => { e.preventDefault(); col.classList.add("drag-over"); });
  col.addEventListener("dragleave", () => col.classList.remove("drag-over"));
  col.addEventListener("drop", async e => {
    e.preventDefault();
    col.classList.remove("drag-over");
    const email = e.dataTransfer.getData("text/email");
    if (!email) return;
    await api().move_card(email, col.dataset.status);
    loadBoard();
  });
}

/* ---------------- КОНТАКТЫ ---------------- */

async function loadContacts() {
  const [contacts, summary, supp] = await Promise.all([
    api().list_contacts(), api().contacts_summary(), api().list_suppression()]);
  state.contacts = contacts;

  $("#contacts-stats").innerHTML = `
    <div class="stat"><div class="n">${summary.total}</div><div class="l">Всего контактов</div></div>
    <div class="stat"><div class="n">${summary.active}</div><div class="l">Активных</div></div>
    <div class="stat"><div class="n">${summary.suppressed}</div><div class="l">В стоп-листе</div></div>`;

  const q = state.search.toLowerCase();
  const rows = contacts.filter(c => !q || c.name.toLowerCase().includes(q)
    || c.email.toLowerCase().includes(q) || (c.company || "").toLowerCase().includes(q));
  $("#contacts-body").innerHTML = rows.map(c => `
    <tr>
      <td><a class="lnk" data-open="${c.id}">${esc(c.name)}</a></td>
      <td class="mono">${esc(c.email)}</td>
      <td>${esc(c.company || "")}</td>
      <td><span class="tag ${c.status}">${statusLabel(c.status)}</span></td>
      <td class="row">
        <button class="btn btn-ghost" data-supp="${esc(c.email)}">В стоп-лист</button>
        <button class="btn btn-ghost" data-del="${c.id}">Удалить</button>
      </td>
    </tr>`).join("") || `<tr><td colspan="5" class="empty-hint">Контактов нет — импортируйте базу.</td></tr>`;

  $$("#contacts-body [data-del]").forEach(b => b.onclick = async () => {
    await api().delete_contact(+b.dataset.del); loadContacts();
  });
  $$("#contacts-body [data-supp]").forEach(b => b.onclick = async () => {
    await api().suppress(b.dataset.supp); toast("Добавлено в стоп-лист"); loadContacts();
  });
  $$("#contacts-body [data-open]").forEach(a => a.onclick = () => openContact(+a.dataset.open));

  $("#supp-body").innerHTML = supp.map(s => `
    <tr><td class="mono">${esc(s.email)}</td><td>${esc(s.reason)}</td>
    <td>${esc((s.created_at || "").slice(0, 19))}</td>
    <td><button class="btn btn-ghost" data-unsupp="${esc(s.email)}">Убрать</button></td></tr>`).join("")
    || `<tr><td colspan="4" class="empty-hint">Стоп-лист пуст.</td></tr>`;
  $$("#supp-body [data-unsupp]").forEach(b => b.onclick = async () => {
    await api().remove_suppression(b.dataset.unsupp); loadContacts();
  });
}

function statusLabel(s) {
  return { active: "Новый", sent: "Отправлено", replied: "Ответил",
           unsubscribed: "Отписался", bounced: "Ошибка" }[s] || s;
}

/* ---------------- карточка контакта (мини-CRM) ---------------- */

const ACT_LABEL = {
  sent: "📤 Отправлено письмо", replied: "↩ Ответил", bounced: "⚠ Не доставлено",
  note: "📝 Заметка", status: "🔀 Статус изменён", import: "📥 Импортирован",
  found: "🔎 Найден на сайте",
};

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString("ru", { day: "2-digit", month: "2-digit",
    year: "2-digit", hour: "2-digit", minute: "2-digit" });
}

async function openContact(id) {
  const r = await api().contact_detail(id);
  if (!r.ok) { toast(r.message, "err"); return; }
  const c = r.contact;
  const timeline = r.activity.length ? r.activity.map(a => `
    <div class="tl-item">
      <div class="tl-kind">${ACT_LABEL[a.kind] || a.kind}</div>
      <div class="tl-detail">${a.detail ? esc(a.detail) : ""}</div>
      <div class="tl-date">${fmtDate(a.created_at)}</div>
    </div>`).join("") : '<div class="empty-hint">Активности пока нет.</div>';

  modal(`
    <div class="row" style="justify-content:space-between;align-items:flex-start">
      <div>
        <h3 style="margin-bottom:2px">${esc(c.name)}</h3>
        <div class="mono" style="color:var(--text-muted);font-size:12.5px">${esc(c.email)}</div>
        ${c.company ? `<div class="hint">${esc(c.company)}</div>` : ""}
      </div>
      <span class="tag ${c.status}">${statusLabel(c.status)}</span>
    </div>
    ${c.last_reply_snippet ? `<div class="card-reply" style="margin-top:12px">↩ ${esc(c.last_reply_snippet)}</div>` : ""}
    <div class="sep"></div>
    <label class="hint">Заметки</label>
    <textarea id="ct-notes" style="margin-top:6px;min-height:70px" placeholder="Договорённости, контекст, следующий шаг…">${esc(c.notes)}</textarea>
    <div class="row" style="justify-content:space-between;margin-top:8px">
      <button class="btn btn-ghost" id="ct-supp">В стоп-лист</button>
      <button class="btn btn-primary" id="ct-save">Сохранить заметки</button>
    </div>
    <div class="sep"></div>
    <label class="hint">История</label>
    <div class="timeline" style="margin-top:8px">${timeline}</div>
    <div class="actions"><button class="btn" id="m-close">Закрыть</button></div>`);
  $("#m-close").onclick = closeModal;
  $("#ct-save").onclick = async () => {
    await api().save_notes(id, $("#ct-notes").value);
    toast("Заметки сохранены", "ok");
  };
  $("#ct-supp").onclick = async () => {
    await api().suppress(c.email);
    closeModal(); toast("В стоп-листе", "ok"); refreshCurrent();
  };
}

/* ---------------- добавление / импорт ---------------- */

function addContactDialog() {
  modal(`
    <h3>Новый контакт</h3>
    <div class="form-grid">
      <label>Email *</label><input class="field" id="m-email">
      <label>Имя</label><input class="field" id="m-first">
      <label>Фамилия</label><input class="field" id="m-last">
      <label>Компания</label><input class="field" id="m-company">
    </div>
    <div class="actions">
      <button class="btn" id="m-cancel">Отмена</button>
      <button class="btn btn-primary" id="m-ok">Добавить</button>
    </div>`);
  $("#m-cancel").onclick = closeModal;
  $("#m-ok").onclick = async () => {
    const r = await api().add_contact($("#m-email").value, $("#m-first").value,
      $("#m-last").value, $("#m-company").value);
    if (r.ok) { closeModal(); toast("Контакт добавлен", "ok"); refreshCurrent(); }
    else toast(r.message, "err");
  };
}

const FIELD_LABELS = { email: "Email *", first_name: "Имя", last_name: "Фамилия", company: "Компания" };

// В web — загрузка файла на сервер; в desktop — нативный диалог.
function pickImport() {
  if (!IS_WEB) return api().pick_import_file();
  return new Promise(resolve => {
    const inp = document.createElement("input");
    inp.type = "file";
    inp.accept = ".csv,.tsv,.txt,.xlsx,.xlsm";
    inp.onchange = async () => {
      if (!inp.files.length) { resolve({ ok: false, cancelled: true }); return; }
      const fd = new FormData();
      fd.append("file", inp.files[0]);
      try {
        const r = await fetch("/api/upload_import", { method: "POST", body: fd });
        if (r.status === 401) { showLogin(); resolve({ ok: false, cancelled: true }); return; }
        resolve(await r.json());
      } catch (e) { resolve({ ok: false, message: String(e) }); }
    };
    inp.click();
  });
}

async function importDialog() {
  const res = await pickImport();
  if (!res.ok) { if (!res.cancelled) toast(res.message, "err"); return; }
  const opts = ['<option value="-1">— нет —</option>']
    .concat(res.headers.map((h, i) => `<option value="${i}">${i + 1}. ${esc(h)}</option>`)).join("");
  const selects = Object.keys(FIELD_LABELS).map(key => `
    <label>${FIELD_LABELS[key]}</label>
    <select id="map-${key}">${opts}</select>`).join("");
  modal(`
    <h3>Импорт: сопоставьте колонки</h3>
    <div class="sub">Файл: <span class="mono">${esc(res.path.split(/[\\/]/).pop())}</span> · строк: ${res.total_rows}</div>
    <div class="form-grid" style="margin-top:12px">${selects}</div>
    <div class="hint" style="margin-top:10px">Остальные колонки сохранятся как поля для подстановки {{...}}.</div>
    <div class="actions">
      <button class="btn" id="m-cancel">Отмена</button>
      <button class="btn btn-primary" id="m-ok">Импортировать</button>
    </div>`);
  for (const [key, idx] of Object.entries(res.guess)) {
    const sel = $("#map-" + key); if (sel) sel.value = idx;
  }
  $("#m-cancel").onclick = closeModal;
  $("#m-ok").onclick = async () => {
    const mapping = {};
    for (const key of Object.keys(FIELD_LABELS)) {
      const v = +$("#map-" + key).value;
      if (v >= 0) mapping[key] = v;
    }
    if (mapping.email === undefined) { toast("Укажите колонку Email", "err"); return; }
    const r = await api().run_import(res.path, mapping);
    closeModal();
    if (r.ok) { toast(r.summary, "ok"); refreshCurrent(); }
    else toast(r.message, "err");
  };
}

/* ---------------- ПИСЬМО И РАССЫЛКА ---------------- */

let templatesCache = [];

async function loadComposeMeta() {
  const s = await api().contacts_summary();
  $("#send-recipients").textContent =
    `Активных получателей: ${s.active}   (в стоп-листе: ${s.suppressed})`;
  await loadTemplates();
}

async function loadTemplates() {
  templatesCache = await api().list_templates();
  const sel = $("#tpl-select");
  const cur = sel.value;
  sel.innerHTML = '<option value="">— шаблон —</option>' +
    templatesCache.map(t => `<option value="${t.id}">${esc(t.name)}</option>`).join("");
  sel.value = cur;
}

$("#btn-tpl-load").onclick = () => {
  const id = +$("#tpl-select").value;
  const t = templatesCache.find(x => x.id === id);
  if (!t) { toast("Выберите шаблон", "err"); return; }
  $("#c-subject").value = t.subject || "";
  $("#c-text").value = t.body_text || "";
  $("#c-html").value = t.body_html || "";
  toast("Шаблон загружен", "ok");
};

$("#btn-tpl-save").onclick = () => {
  modal(`
    <h3>Сохранить как шаблон</h3>
    <input class="field" id="tpl-name" placeholder="Название шаблона (напр. «Первое касание»)">
    <div class="actions">
      <button class="btn" id="m-cancel">Отмена</button>
      <button class="btn btn-primary" id="m-ok">Сохранить</button>
    </div>`);
  $("#m-cancel").onclick = closeModal;
  $("#m-ok").onclick = async () => {
    const name = $("#tpl-name").value.trim();
    if (!name) { toast("Введите название", "err"); return; }
    const r = await api().save_template(name, $("#c-subject").value,
      $("#c-text").value, $("#c-html").value, null);
    closeModal();
    if (r.ok) { toast("Шаблон сохранён", "ok"); loadTemplates(); }
    else toast(r.message, "err");
  };
};

$("#btn-tpl-del").onclick = async () => {
  const id = +$("#tpl-select").value;
  if (!id) { toast("Выберите шаблон", "err"); return; }
  await api().delete_template(id);
  toast("Шаблон удалён", "ok");
  loadTemplates();
};

$("#btn-preview").onclick = async () => {
  const r = await api().preview_message($("#c-subject").value, $("#c-text").value, $("#c-html").value);
  const box = $("#preview");
  if (r.html) box.innerHTML = `<b>Тема:</b> ${esc(r.subject)}<hr style="border-color:var(--stroke);margin:10px 0">${r.html}`;
  else box.textContent = `Тема: ${r.subject}\n\n${r.text}`;
};

$("#btn-test").onclick = async () => {
  const btn = $("#btn-test"); btn.disabled = true; btn.textContent = "Отправка…";
  const r = await api().send_test($("#c-subject").value, $("#c-text").value, $("#c-html").value);
  btn.disabled = false; btn.textContent = "Тест себе";
  toast(r.message, r.ok ? "ok" : "err");
};

$("#btn-send").onclick = async () => {
  const active = (await api().contacts_summary()).active;
  if (!active) { toast("Нет активных получателей", "err"); return; }
  modal(`
    <h3>Подтверждение рассылки</h3>
    <p style="color:var(--text-muted);line-height:1.6">
      Отправить письмо <b style="color:var(--text)">${active}</b> получателям?<br>
      Убедитесь, что у всех есть согласие на рассылку.</p>
    <div class="actions">
      <button class="btn" id="m-cancel">Отмена</button>
      <button class="btn btn-primary" id="m-ok">Начать</button>
    </div>`);
  $("#m-cancel").onclick = closeModal;
  $("#m-ok").onclick = async () => {
    closeModal();
    const name = "Кампания " + new Date().toLocaleString("ru");
    $("#send-log").innerHTML = "";
    $("#progress").style.width = "0%";
    const r = await api().start_campaign(name, $("#c-subject").value,
      $("#c-text").value, $("#c-html").value);
    if (!r.ok) { toast(r.message, "err"); return; }
    $("#btn-send").disabled = true; $("#btn-stop").disabled = false;
    $("#btn-test").disabled = true;
  };
};

$("#btn-stop").onclick = async () => { await api().stop_campaign(); $("#send-status").textContent = "Останавливаю…"; };

// колбэки из Python (движок рассылки в фоновом потоке)
window.onCampaignProgress = function (p) {
  if (p.total) {
    const done = p.sent + p.failed + p.skipped;
    $("#progress").style.width = Math.round(done / p.total * 100) + "%";
  }
  $("#send-status").textContent = `${p.message}   ✓${p.sent}  ✗${p.failed}  ⊘${p.skipped}`;
  if (p.finished) {
    $("#btn-send").disabled = false; $("#btn-stop").disabled = true; $("#btn-test").disabled = false;
    toast("Рассылка завершена", "ok");
  }
};
window.onCampaignLog = function (l) {
  const cls = l.level === "error" ? "err" : l.level === "warn" ? "warn" : "";
  const line = document.createElement("div");
  if (cls) line.className = cls;
  line.textContent = l.message;
  $("#send-log").appendChild(line);
  $("#send-log").scrollTop = $("#send-log").scrollHeight;
};

/* ---------------- ОТВЕТЫ ---------------- */

$("#btn-fetch").onclick = async () => {
  const btn = $("#btn-fetch"); btn.disabled = true; btn.textContent = "Загрузка…";
  const r = await api().fetch_replies();
  btn.disabled = false; btn.textContent = "Загрузить";
  if (!r.ok) { toast(r.message, "err"); return; }
  $("#replies-body").innerHTML = r.replies.map(rep => `
    <tr style="${rep.is_unsubscribe ? "color:var(--danger)" : ""}">
      <td>${esc(rep.from_name)} <span class="mono">${esc(rep.from_email)}</span></td>
      <td>${esc(rep.subject)}</td><td>${esc(rep.date)}</td>
      <td>${esc(rep.snippet)}</td>
      <td><button class="btn btn-ghost" data-supp="${esc(rep.from_email)}">В стоп-лист</button></td>
    </tr>`).join("") || `<tr><td colspan="5" class="empty-hint">Писем нет.</td></tr>`;
  $$("#replies-body [data-supp]").forEach(b => b.onclick = async () => {
    await api().suppress(b.dataset.supp); toast("В стоп-листе", "ok");
  });
};

$("#btn-sync").onclick = async () => {
  const r = await api().sync_unsubscribes();
  if (!r.ok) { toast(r.message, "err"); return; }
  toast(r.added.length ? `В стоп-лист добавлено: ${r.added.length}` : "Новых отписок нет", "ok");
};

/* ---------------- ПОИСК ПОЧТЫ НА САЙТЕ ---------------- */

let lastFindDomain = "";

function emailRow(email) {
  return `<label class="check" style="display:flex;padding:7px 4px;gap:9px">
    <input type="checkbox" class="find-cb" value="${esc(email)}" checked>
    <span class="mono">${esc(email)}</span></label>`;
}

$("#btn-find").onclick = async () => {
  const url = $("#find-url").value.trim();
  if (!url) { toast("Укажите адрес сайта", "err"); return; }
  const btn = $("#btn-find"); btn.disabled = true; btn.textContent = "Ищу…";
  $("#find-status").textContent = "Открываю сайт и страницы контактов…";
  $("#find-results").style.display = "none";
  let r;
  try { r = await api().find_site_emails(url); }
  catch (e) { r = { ok: false, message: String(e) }; }
  btn.disabled = false; btn.textContent = "Найти";
  if (!r.ok) { $("#find-status").textContent = ""; toast(r.message || "Не найдено", "err"); return; }

  lastFindDomain = r.domain || "";
  const total = r.primary.length + r.other.length;
  $("#find-status").textContent = total
    ? `Найдено адресов: ${total} (проверено страниц: ${r.pages_checked.length})`
    : "Публичных адресов на сайте не найдено.";
  $("#find-primary").innerHTML = r.primary.length
    ? r.primary.map(emailRow).join("")
    : `<div class="empty-hint">На домене ${esc(r.domain)} адресов не найдено.</div>`;
  if (r.other.length) {
    $("#find-other").innerHTML = r.other.map(emailRow).join("");
    $("#find-other-wrap").style.display = "";
  } else {
    $("#find-other-wrap").style.display = "none";
  }
  $("#find-pages").textContent = "Проверено: " + r.pages_checked.join("  ");
  $("#find-results").style.display = total ? "" : "none";
};

$("#btn-add-found").onclick = async () => {
  const emails = $$(".find-cb").filter(c => c.checked).map(c => c.value);
  if (!emails.length) { toast("Отметьте адреса галочкой", "err"); return; }
  const r = await api().add_found_emails(emails, lastFindDomain);
  if (!r.ok) { toast(r.message || "Ошибка", "err"); return; }
  toast(`Добавлено: ${r.added}` + (r.skipped ? `, пропущено: ${r.skipped}` : ""), "ok");
  $("#find-results").style.display = "none";
  $("#find-url").value = "";
  $("#find-status").textContent = "";
};

/* ---------------- НАСТРОЙКИ ---------------- */

async function loadSettings() {
  const c = await api().get_config();
  const smtp = c.smtp || {}, imap = c.imap || {}, snd = c.sender || {}, lim = c.limits || {};
  const s = v => v == null ? "" : v;         // строка без "undefined"
  const n = (v, d) => (v == null || v === "") ? d : v;  // число с дефолтом

  $("#s-smtp-host").value = s(smtp.host); $("#s-smtp-port").value = n(smtp.port, 587);
  $("#s-smtp-user").value = s(smtp.username);
  $("#s-tls").checked = smtp.use_tls !== false && !smtp.use_ssl;
  $("#s-ssl").checked = !!smtp.use_ssl;
  if (c.has_password) $("#s-smtp-pass").placeholder = "•••••••• (сохранён)";

  $("#s-from-name").value = s(snd.from_name); $("#s-from-email").value = s(snd.from_email);
  $("#s-reply").value = s(snd.reply_to); $("#s-org").value = s(snd.org_name);
  $("#s-postal").value = s(snd.postal_address); $("#s-signature").value = s(snd.signature);

  $("#s-imap-host").value = s(imap.host); $("#s-imap-port").value = n(imap.port, 993);
  $("#s-imap-user").value = s(imap.username); $("#s-imap-ssl").checked = imap.use_ssl !== false;

  $("#s-per-hour").value = n(lim.per_hour, 100); $("#s-per-day").value = n(lim.per_day, 500);
  $("#s-delay").value = n(lim.delay_seconds, 3); $("#s-batch").value = n(lim.batch_size, 50);
  $("#s-batch-pause").value = n(lim.batch_pause_seconds, 60);
}

function collectSettings() {
  const data = {
    smtp: { host: $("#s-smtp-host").value.trim(), port: +$("#s-smtp-port").value,
            username: $("#s-smtp-user").value.trim(),
            use_tls: $("#s-tls").checked, use_ssl: $("#s-ssl").checked },
    imap: { host: $("#s-imap-host").value.trim(), port: +$("#s-imap-port").value,
            username: $("#s-imap-user").value.trim(), use_ssl: $("#s-imap-ssl").checked },
    sender: { from_name: $("#s-from-name").value.trim(), from_email: $("#s-from-email").value.trim(),
              reply_to: $("#s-reply").value.trim(), org_name: $("#s-org").value.trim(),
              postal_address: $("#s-postal").value.trim(), signature: $("#s-signature").value },
    limits: { per_hour: +$("#s-per-hour").value, per_day: +$("#s-per-day").value,
              delay_seconds: +$("#s-delay").value, batch_size: +$("#s-batch").value,
              batch_pause_seconds: +$("#s-batch-pause").value },
  };
  const pw = $("#s-smtp-pass").value;
  if (pw) data.password = pw;
  return data;
}

$("#btn-save-settings").onclick = async () => {
  await api().save_config(collectSettings());
  toast("Настройки сохранены", "ok");
};
$("#btn-test-smtp").onclick = async () => {
  await api().save_config(collectSettings());
  const btn = $("#btn-test-smtp"); btn.disabled = true; btn.textContent = "Проверка…";
  const r = await api().test_smtp();
  btn.disabled = false; btn.textContent = "Проверить SMTP";
  toast(r.message, r.ok ? "ok" : "err");
};
$("#btn-deliver").onclick = async () => {
  await api().save_config(collectSettings());
  const btn = $("#btn-deliver"); btn.disabled = true; btn.textContent = "Проверяю…";
  const r = await api().check_deliverability();
  btn.disabled = false; btn.textContent = "Проверить домен";
  const box = $("#deliver-result");
  if (!r.ok) { box.innerHTML = `<div class="hint" style="color:var(--danger);margin-top:10px">${esc(r.message)}</div>`; return; }
  const row = (label, c) => {
    const icon = c.ok ? "✅" : (c.soft ? "⚠️" : "❌");
    return `<div class="deliver-row">
      <div><b>${icon} ${label}</b> — ${esc(c.title)}</div>
      ${c.detail ? `<div class="mono hint">${esc(c.detail)}</div>` : ""}
      ${c.hint ? `<div class="hint">${esc(c.hint)}</div>` : ""}
    </div>`;
  };
  box.innerHTML = `<div class="sub" style="margin:10px 0 6px">Домен: <b>${esc(r.domain)}</b> · ${esc(r.summary)}</div>`
    + row("SPF", r.spf) + row("DKIM", r.dkim) + row("DMARC", r.dmarc);
};

$("#btn-test-imap").onclick = async () => {
  await api().save_config(collectSettings());
  const btn = $("#btn-test-imap"); btn.disabled = true; btn.textContent = "Проверка…";
  const r = await api().test_imap();
  btn.disabled = false; btn.textContent = "Проверить IMAP";
  toast(r.message, r.ok ? "ok" : "err");
};

/* ---------------- общее ---------------- */

function refreshCurrent() {
  if (state.page === "board") loadBoard();
  else if (state.page === "contacts") loadContacts();
}

$("#search").addEventListener("input", e => {
  state.search = e.target.value;
  if (state.page === "board") loadBoard();
  else if (state.page === "contacts") loadContacts();
});

$("#btn-import").onclick = importDialog;
$("#btn-import2").onclick = importDialog;
$("#btn-new").onclick = addContactDialog;
$("#btn-add2").onclick = addContactDialog;
$("#fab").onclick = () => showPage("compose");

/* ---------------- вход (общий пароль, web) ---------------- */

function showLogin() {
  let ov = $("#login-overlay");
  if (!ov) {
    ov = document.createElement("div");
    ov.id = "login-overlay";
    ov.className = "modal-back show";
    ov.innerHTML = `
      <div class="modal" style="width:min(380px,90vw)">
        <h3>MailSender</h3>
        <div class="sub" style="margin-bottom:14px">Введите общий пароль доступа</div>
        <input class="field" id="login-pass" type="password" placeholder="Пароль" autofocus>
        <div id="login-err" class="hint" style="color:var(--danger);min-height:16px;margin-top:8px"></div>
        <div class="actions"><button class="btn btn-primary" id="login-btn">Войти</button></div>
      </div>`;
    document.body.appendChild(ov);
    const submit = async () => {
      const r = await fetch("/auth/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: $("#login-pass").value }),
      }).then(x => x.json()).catch(() => ({ ok: false, message: "Ошибка сети" }));
      if (r.ok) { ov.remove(); startWeb(); }
      else $("#login-err").textContent = r.message || "Неверный пароль";
    };
    $("#login-btn").onclick = submit;
    $("#login-pass").addEventListener("keydown", e => { if (e.key === "Enter") submit(); });
  }
  ov.classList.add("show");
}

/* ---------------- поток событий рассылки (web, SSE) ---------------- */

function initSSE() {
  try {
    const es = new EventSource("/events");
    es.addEventListener("onCampaignProgress", e => window.onCampaignProgress(JSON.parse(e.data)));
    es.addEventListener("onCampaignLog", e => window.onCampaignLog(JSON.parse(e.data)));
  } catch (e) { /* браузер без SSE — прогресс просто не будет стримиться */ }
}

/* ---------------- инициализация ---------------- */

function boot() { showPage("board"); }
function startWeb() { initSSE(); boot(); }

async function initApp() {
  // Desktop (pywebview): страница открыта как файл, api приходит асинхронно.
  if (location.protocol === "file:") {
    if (window.pywebview && window.pywebview.api) { BACKEND = window.pywebview.api; boot(); }
    else window.addEventListener("pywebviewready", () => { BACKEND = window.pywebview.api; boot(); });
    return;
  }
  // Web: проверяем бэкенд и статус авторизации.
  let status = null;
  try {
    status = await fetch("/auth/status").then(r => r.json());
  } catch (e) {
    // Бэкенда нет (например, статический сервер) — демо-режим для превью дизайна.
    installMockApi(); BACKEND = window.pywebview.api; boot(); return;
  }
  IS_WEB = true;
  BACKEND = makeHttpApi();
  if (status.auth_enabled && !status.authenticated) { showLogin(); return; }
  startWeb();
}

initApp();

function installMockApi() {
  const demo = {
    active: [
      { id: 1, email: "d.martinez@northline.com", name: "David Martinez", company: "Northline Capital", status: "active", source: "" },
      { id: 2, email: "emily.carter@brightpath.io", name: "Emily Carter", company: "BrightPath", status: "active", source: "" },
      { id: 3, email: "olivia.nguyen@cedarworks.co", name: "Olivia Nguyen", company: "CedarWorks", status: "active", source: "" },
    ],
    sent: [
      { id: 4, email: "drew.c@evergreen.com", name: "Drew Chernsyhuk", company: "Evergreen", status: "sent", source: "" },
    ],
    replied: [
      { id: 5, email: "m.lee@oakavenue.com", name: "Michael Lee", company: "Oak Avenue", status: "replied", source: "" },
    ],
    unsubscribed: [],
  };
  const P = v => Promise.resolve(v);
  window.pywebview = { api: {
    board_data: () => P({ followups: 1, followup_days: 4, columns: [
      { status: "active", title: "Новые", count: demo.active.length, cards: demo.active },
      { status: "sent", title: "Отправлено", count: demo.sent.length,
        cards: demo.sent.map(c => ({ ...c, followup: true })) },
      { status: "replied", title: "Ответили", count: demo.replied.length,
        cards: demo.replied.map(c => ({ ...c, reply: "Спасибо, интересно — давайте созвонимся на неделе" })) },
      { status: "unsubscribed", title: "Отписались", count: 0, cards: [] },
    ]}),
    sync_replies: () => P({ ok: true, replied: ["m.lee@oakavenue.com"], unsubscribed: [] }),
    list_contacts: () => P([...demo.active, ...demo.sent, ...demo.replied]),
    contacts_summary: () => P({ total: 5, active: 3, suppressed: 0 }),
    list_suppression: () => P([]),
    get_config: () => P({ smtp: {}, imap: {}, sender: {}, limits: {}, has_password: false }),
    preview_message: (s, t) => P({ subject: s.replace("{{company}}", "Northline Capital"),
      text: t.replace("{{first_name}}", "David").replace("{{first_name|коллеги}}", "David")
        + "\n\n—\nООО «Ваша компания»\nг. Москва\nОтписаться: mailto:unsub@company.ru", html: "" }),
    move_card: () => P({ ok: true }), add_contact: () => P({ ok: true }),
    delete_contact: () => P({ ok: true }), suppress: () => P({ ok: true }),
    remove_suppression: () => P({ ok: true }), save_config: () => P({ ok: true }),
    test_smtp: () => P({ ok: false, message: "Демо-режим: бэкенд недоступен в браузере" }),
    test_imap: () => P({ ok: false, message: "Демо-режим" }),
    send_test: () => P({ ok: false, message: "Демо-режим" }),
    fetch_replies: () => P({ ok: false, message: "Демо-режим" }),
    sync_unsubscribes: () => P({ ok: false, message: "Демо-режим" }),
    pick_import_file: () => P({ ok: false, cancelled: true }),
    find_site_emails: (url) => P({ ok: true, url, domain: "northline.com",
      pages_checked: ["https://northline.com", "https://northline.com/contacts"],
      primary: ["info@northline.com", "pr@northline.com", "sales@northline.com"],
      other: ["hello@partners.io"] }),
    add_found_emails: (e) => P({ ok: true, added: e.length, skipped: 0, invalid: 0 }),
    list_templates: () => P([{ id: 1, name: "Первое касание", subject: "{{company}} — сотрудничество", body_text: "Здравствуйте, {{first_name|коллеги}}!", body_html: "" }]),
    save_template: () => P({ ok: true, id: 2 }), delete_template: () => P({ ok: true }),
    contact_detail: (id) => P({ ok: true, contact: { id, name: "David Martinez",
      email: "d.martinez@northline.com", company: "Northline Capital", status: "sent",
      notes: "Просил КП, созвон в пятницу", last_reply_snippet: "" },
      activity: [
        { kind: "sent", detail: "письмо отправлено", created_at: "2026-07-20T09:12:00Z" },
        { kind: "import", detail: "импортирован из списка", created_at: "2026-07-19T14:00:00Z" }] }),
    save_notes: () => P({ ok: true }),
    check_deliverability: () => P({ ok: true, domain: "northline.com", score: 2,
      summary: "Настроено 2 из 3 ключевых записей.",
      spf: { ok: true, title: "SPF настроен", hint: "", detail: "v=spf1 include:_spf.google.com -all", soft: false },
      dkim: { ok: true, title: "DKIM найден (селектор: google)", hint: "", detail: "", soft: false },
      dmarc: { ok: false, title: "DMARC-запись не найдена", soft: false,
        hint: "Добавьте TXT _dmarc с «v=DMARC1; p=none; rua=mailto:…».", detail: "" } }),
    start_campaign: () => P({ ok: false, message: "Демо-режим: запустите через python run.py" }),
    stop_campaign: () => P({ ok: true }),
  }};
  document.body.dataset.demo = "1";
}
