const state = { sessions: [], groups: [], jobs: [], logs: [], pendingLogs: [], logVisibleCount: 100, logsAutoRefresh: true, logsLastSeenId: 0, activityScans: [], heartbeat: null, releaseNotes: null, groupAccessBatches: [], sessionHealthBatches: [], health: null, license: null, telegramSettings: null, rotationSettings: null, settingsOverview: null, onboarding: null, defaultLoginProxy: null, currentJobId: null, currentActivityScanId: null, currentGroupAccessBatchId: null, currentGroupAccessDetail: null, currentGroupSessionId: null, currentSessionHealthBatchId: null, runningScanIds: new Set(), loginPhone: "" };

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function errorMessage(value, fallback = "İşlem tamamlanamadı.") {
  if (value === null || value === undefined || value === "") return fallback;
  if (typeof value === "string") return value;
  if (value instanceof Error) return errorMessage(value.message, fallback);
  if (Array.isArray(value)) {
    const messages = value.map(item => errorMessage(item, "")).filter(Boolean);
    return messages.length ? messages.join(" · ") : fallback;
  }
  if (typeof value === "object") {
    if (value.detail !== undefined) return errorMessage(value.detail, fallback);
    if (value.message !== undefined) return errorMessage(value.message, fallback);
    if (value.msg !== undefined) {
      const location = Array.isArray(value.loc) ? value.loc.filter(item => item !== "body").join(".") : "";
      return (location ? location + ": " : "") + errorMessage(value.msg, fallback);
    }
    try {
      return JSON.stringify(value);
    } catch (_) {
      return fallback;
    }
  }
  return String(value);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (response.status === 401 && !path.startsWith("/api/auth/")) configureAuthOverlay(false);
  if (response.status === 402) showLicenseOverlay(body.detail);
  if (!response.ok) throw new Error(errorMessage(body.detail ?? body));
  return body;
}

function runUi(promise, { silent = false } = {}) {
  return Promise.resolve(promise).catch(error => {
    if (!silent) toast(error?.message || "İşlem tamamlanamadı.");
    return null;
  });
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);
}

function badge(status) {
  const labels = { active:"Aktif", flood_wait:"24 saat dinleniyor", batch_wait:"Parti beklemesi", paused_batch:"Parti beklemesi", paused_quota:"Günlük kota bekliyor", proxy_pending:"Proxy test edilecek", proxy_error:"Proxy hatası", awaiting_code:"Kod bekliyor", invalid:"Geçersiz", banned:"Kullanılamıyor", ready:"Hazır", attention:"Uyarı", busy:"Meşgul", already_member:"Zaten grupta", preview:"Önizleme", previewed:"Önizlendi", approved:"Onaylandı", queued_execution:"Başlatılıyor", running:"Çalışıyor", checking:"Kontrol ediliyor", joined:"Katıldı", approval_pending:"Onay bekliyor", stopped:"Durduruldu", completed:"Tamamlandı", failed:"Hatalı", group:"Grup", megagroup:"Megagroup", channel:"Kanal", success:"Uygun", warning:"Zaten grupta", bot:"Bot", deleted:"Silinmiş", admin:"Grup yöneticisi", previously_used:"Daha önce alındı", queued:"Sırada", scheduled:"Planlandı", waiting:"FloodWait", waiting_join:"Katılım onayı", waiting_budget:"Güvenli bütçe", paused:"Duraklatıldı", error:"Hatalı" };
  return `<span class="badge ${escapeHtml(status)}">${labels[status] || escapeHtml(status)}</span>`;
}

function formatDuration(totalSeconds) {
  const seconds = Math.max(0, Number(totalSeconds) || 0);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  return [hours, minutes, rest].map(value => String(value).padStart(2, "0")).join(":");
}

function toast(message) {
  const element = $("#toast");
  element.textContent = errorMessage(message);
  element.classList.remove("hidden");
  setTimeout(() => element.classList.add("hidden"), 3500);
}

function showMessage(selector, message, success = false) {
  const element = $(selector);
  element.textContent = message;
  element.classList.toggle("success", success);
  element.classList.remove("hidden");
}

const pageMeta = {
  dashboard:["Panel","Telegram hesapları ve işler için genel görünüm"],
  sessions:["Session'lar","Telefon havuzu, durumlar ve güvenli bağlantılar"],
  groups:["Gruplarım","Grup kimlikleri, erişim ve yetki kontrolleri"],
  activity:["Aktiflik","Mesaj yazarı analizi ve zaman aralığı filtreleri"],
  heartbeat:["Heartbeat","Aktif session'ların bağımsız Telegram heartbeat durumu"],
  jobs:["Kuyruk","Hazır, çalışan ve tamamlanan iş tanımları"],
  logs:["Loglar","Session, doğrulama ve hata kayıtları"],
  settings:["Ayarlar","Koruma ve Telegram API yapılandırması"],
};

function navigate(page) {
  if (page !== "settings" && $("#page-settings").classList.contains("active") && settingsHasUnsavedChanges()) {
    if (!window.confirm("Ayarlar sayfasında kaydedilmemiş değişiklikler var. Kaydetmeden ayrılmak istiyor musunuz?")) return;
  }
  closeModals();
  $$(".page").forEach(item => item.classList.toggle("active", item.id === `page-${page}`));
  $$(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.page === page));
  $("#page-title").textContent = pageMeta[page][0];
  $("#page-subtitle").textContent = pageMeta[page][1];
  if (page === "sessions") runUi(loadSessions());
  if (page === "logs") runUi(loadLogs());
  if (page === "groups") { fillSessionSelects(); runUi(Promise.all([loadGroupAccessBatches(), loadSessionHealthBatches()])); }
  if (page === "activity") runUi(loadActivityScans());
  if (page === "heartbeat") runUi(loadHeartbeat({syncForm:true}));
  if (page === "settings") runUi(loadSettingsPage());
}

function formatDateTime(value) {
  return value ? new Date(value).toLocaleString("tr-TR") : "—";
}

function showLicenseOverlay(message = "Pawgram lisansı gerekli.") {
  $("#license-overlay").classList.remove("hidden");
  $("#license-description").textContent = message;
}

function renderLicenseStatus(status) {
  state.license = status;
  const label = !status.required ? "Kişisel kullanım modu" : status.valid ? "Lisans aktif" : "Lisans gerekli";
  $("#license-state").innerHTML = `<strong>${escapeHtml(label)}</strong><small>${escapeHtml(status.message || "")}</small>`;
  $("#license-expiry").textContent = status.license_expires_at ? `Bitiş: ${formatDateTime(status.license_expires_at)}` : "Süre sınırı uygulanmıyor";
  $("#license-offline").textContent = status.offline ? "Çevrimdışı imzalı süre kullanılıyor" : status.required ? "Sunucu doğrulaması etkin" : "Ticari paketlerde etkinleştirilebilir";
  $("#license-state").classList.toggle("valid", Boolean(status.valid));
}

async function loadLicenseStatus() {
  try {
    const status = await api("/api/license/status");
    renderLicenseStatus(status);
    return status;
  } catch (error) {
    showLicenseOverlay(error.message);
    return {required:true, valid:false, message:error.message};
  }
}

async function activateLicense() {
  const licenseKey = $("#license-key").value.trim();
  if (!licenseKey) return showMessage("#license-message", "Lisans kodunu girin.");
  showMessage("#license-message", "Lisans güvenli sunucuda doğrulanıyor…");
  try {
    const status = await api("/api/license/activate", {method:"POST", body:JSON.stringify({license_key:licenseKey})});
    renderLicenseStatus(status);
    $("#license-key").value = "";
    $("#license-overlay").classList.add("hidden");
    showMessage("#license-message", "Lisans başarıyla etkinleştirildi.", true);
    await bootstrap();
  } catch (error) { showMessage("#license-message", error.message); }
}

function fillSessionSelects() {
  const options = state.sessions.length
    ? state.sessions.map(s => `<option value="${s.id}">${escapeHtml(s.label)} · ${escapeHtml(s.phone_masked)}</option>`).join("")
    : `<option value="">Önce bir telefon ekleyin</option>`;
  ["#quick-session", "#group-session", "#job-session"].forEach(id => {
    const select = $(id);
    const previous = select.value;
    select.innerHTML = options;
    if (previous && state.sessions.some(session => String(session.id) === previous)) select.value = previous;
  });
  $("#activity-session").innerHTML = `<option value="">Uygun session'ı otomatik seç</option>` + (state.sessions.length
    ? state.sessions.map(s => `<option value="${s.id}">${escapeHtml(s.label)} · ${escapeHtml(s.phone_masked)}</option>`).join("")
    : "");
  const proxySelect = $("#proxy-session");
  const previousProxySession = proxySelect.value;
  proxySelect.innerHTML = state.sessions.length
    ? state.sessions.map(s => `<option value="${s.id}">${escapeHtml(s.label)} · ${escapeHtml(s.phone_masked)}</option>`).join("")
    : `<option value="">Önce bir telefon ekleyin</option>`;
  if (previousProxySession && state.sessions.some(s => String(s.id) === previousProxySession)) proxySelect.value = previousProxySession;
  renderGroupAccessSessions();
}

function renderGroupAccessSessions() {
  const container = $("#group-access-sessions");
  if (!container) return;
  const selected = new Set($$(".group-access-session-checkbox:checked").map(item => Number(item.value)));
  container.innerHTML = state.sessions.length ? state.sessions.map(session => `
    <label class="group-access-session">
      <input class="group-access-session-checkbox" type="checkbox" value="${session.id}" ${selected.has(Number(session.id)) ? "checked" : ""}>
      <span><strong>${escapeHtml(session.label)}</strong><small>${escapeHtml(session.phone_masked)} · ${escapeHtml(session.status)}</small>${session.operation_label ? `<small class="session-lock-label">Meşgul: ${escapeHtml(session.operation_label)}</small>` : ""}</span>
    </label>`).join("") : `<div class="empty-state"><strong>Henüz session yok</strong><span>Önce bir Telegram hesabı ekleyin.</span></div>`;
  syncGroupAccessSelectAll();
}

function syncGroupAccessSelectAll() {
  const boxes = $$(".group-access-session-checkbox");
  const selectedCount = boxes.filter(item => item.checked).length;
  const selectAll = $("#group-access-select-all");
  if (!selectAll) return;
  selectAll.checked = Boolean(boxes.length) && selectedCount === boxes.length;
  selectAll.indeterminate = selectedCount > 0 && selectedCount < boxes.length;
}

async function loadDashboard() {
  const dashboard = await api("/api/dashboard");
  $("#metric-sessions").textContent = dashboard.sessions_total;
  $("#metric-active").textContent = dashboard.sessions_active;
  $("#metric-jobs").textContent = dashboard.jobs_total;
  $("#metric-waiting").textContent = dashboard.sessions_waiting;
  $("#top-session-count").textContent = `${dashboard.sessions_total} session`;

  const alertDefinitions = [
    ["proxy_attention", "Proxy kontrolü gerekiyor", "settings", "error"],
    ["flood_wait", "FloodWait beklemesinde", "sessions", "warning"],
    ["batch_wait", "Parti beklemesinde", "sessions", "warning"],
    ["pending_group_approvals", "Grup onayı bekliyor", "groups", "warning"],
    ["job_attention", "İş müdahale bekliyor", "jobs", "error"],
    ["activity_attention", "Tarama müdahale bekliyor", "activity", "warning"],
  ];
  const visibleAlerts = alertDefinitions.filter(([key]) => Number(dashboard.alerts?.[key] || 0) > 0);
  $("#dashboard-alerts").innerHTML = visibleAlerts.length
    ? visibleAlerts.map(([key, label, page, level]) => `
        <button class="dashboard-alert ${level}" data-dashboard-page="${page}">
          <span>${escapeHtml(label)}</span><strong>${Number(dashboard.alerts[key])}</strong>
        </button>`).join("")
    : `<div class="dashboard-all-clear">Şu anda müdahale gerektiren bir durum yok.</div>`;

  const health = dashboard.latest_health;
  $("#dashboard-health-summary").innerHTML = health
    ? `<button class="ghost" data-dashboard-page="groups">Son sağlık kontrolü</button>
       <strong>${Number(health.ready_count || 0)} hazır</strong> ·
       <span>${Number(health.warning_count || 0)} uyarı · ${Number(health.failed_count || 0)} hatalı</span>
       <small>${escapeHtml(formatDateTime(health.finished_at || health.created_at))}</small>`
    : `Henüz toplu session sağlık kontrolü yapılmadı.`;

  const today = dashboard.today || {};
  const todayItems = [
    ["Eklenen üye", today.invited],
    ["Atlanan aday", today.skipped],
    ["Başarısız aday", today.failed],
    ["Benzersiz aktif üye", today.unique_active_users],
    ["Tamamlanan tarama", today.completed_scans],
    ["Bekleyen seçili aday", today.remaining_candidates],
  ];
  $("#dashboard-today-summary").innerHTML = todayItems.map(([label, value]) => `
    <article><small>${escapeHtml(label)}</small><strong>${Number(value || 0)}</strong></article>`).join("");

  const operations = dashboard.active_operations || [];
  $("#dashboard-active-operations").innerHTML = operations.length ? `
    <table><thead><tr><th>Session</th><th>İşlem</th><th>İşlem anahtarı</th><th>Başlangıç</th></tr></thead><tbody>
      ${operations.map(operation => `<tr>
        <td><strong>${escapeHtml(operation.session_label)}</strong><br><span class="mono">${escapeHtml(operation.phone_masked)}</span></td>
        <td>${escapeHtml(operation.operation_label || operation.operation_type)}</td>
        <td class="mono">${escapeHtml(operation.operation_type)} · ${escapeHtml(operation.operation_key)}</td>
        <td>${escapeHtml(formatDateTime(operation.acquired_at))}</td>
      </tr>`).join("")}
    </tbody></table>` : emptyTable("Şu anda aktif Telegram işlemi yok", "Session'lar yeni bir iş için kullanılabilir.");
}

function sessionStatusInfo(session) {
  if (session.operation_type) return { group:"busy", badge:"busy", title:"İşlem yapıyor", description:session.operation_label || "Aktif Telegram işlemi yürütülüyor." };
  if (session.status === "active" && (!session.proxy_enabled || session.proxy_last_status !== "success")) {
    return {group:"problem", badge:"proxy_pending", title:"Proxy kontrolü gerekiyor", description:"Session bağlı ancak Telegram işlemlerinden önce proxy doğrulanmalı."};
  }
  const statuses = {
    active:{group:"ready", badge:"active", title:"Kullanıma hazır", description:"Proxy ve session yeni bir işlem için hazır."},
    batch_wait:{group:"waiting", badge:"batch_wait", title:"Parti beklemesi", description:`Parti limiti sonrası ${session.invite_cooldown_minutes || 20} dakikalık dinlenme uygulanıyor.`},
    flood_wait:{group:"waiting", badge:"flood_wait", title:"FloodWait", description:"Telegram tarafından verilen zorunlu bekleme süresi devam ediyor."},
    proxy_pending:{group:"problem", badge:"proxy_pending", title:"Proxy testi bekliyor", description:"İşleme başlamadan önce proxy bağlantısı doğrulanmalı."},
    proxy_error:{group:"problem", badge:"proxy_error", title:"Proxy hatası", description:"Ana IP kullanılmadan session güvenli biçimde durduruldu."},
    awaiting_code:{group:"problem", badge:"awaiting_code", title:"Doğrulama bekliyor", description:"Telegram giriş işlemi henüz tamamlanmadı."},
    invalid:{group:"problem", badge:"invalid", title:"Geçersiz session", description:"Session yeniden doğrulanmalı."},
    banned:{group:"problem", badge:"banned", title:"Kullanılamıyor", description:"Telegram hesabı bu session üzerinden kullanılamıyor."},
    error:{group:"problem", badge:"error", title:"İşlem hatası", description:"Son hata ayrıntısını session detayından inceleyin."},
  };
  return statuses[session.status] || {group:"problem", badge:session.status, title:session.status || "Bilinmiyor", description:"Session durumu kontrol edilmeli."};
}

function sessionRecentActivity(session) {
  const entries = [
    {date:session.last_successful_invite_at, label:"Başarılı üye ekleme"},
    {date:session.last_activity_at, label:"Aktiflik taraması"},
    {date:session.last_event_at, label:session.last_event_category ? `Sistem olayı · ${session.last_event_category}` : "Sistem olayı"},
  ].filter(item => item.date);
  return entries.sort((a, b) => new Date(b.date) - new Date(a.date))[0] || null;
}

function renderSessionTable() {
  const search = ($("#session-search")?.value || "").trim().toLocaleLowerCase("tr-TR");
  const statusFilter = $("#session-status-filter")?.value || "all";
  const proxyFilter = $("#session-proxy-filter")?.value || "all";
  const sort = $("#session-sort")?.value || "newest";
  let sessions = state.sessions.filter(session => {
    const status = sessionStatusInfo(session);
    const searchable = [session.label, session.phone_masked, session.display_name, session.username, session.telegram_user_id]
      .filter(value => value !== null && value !== undefined).join(" ").toLocaleLowerCase("tr-TR");
    const matchesSearch = !search || searchable.includes(search);
    const matchesStatus = statusFilter === "all" || status.group === statusFilter;
    const proxyHealthy = session.proxy_enabled && session.proxy_last_status === "success" && session.status !== "proxy_error";
    const matchesProxy = proxyFilter === "all" || (proxyFilter === "healthy" ? proxyHealthy : !proxyHealthy);
    return matchesSearch && matchesStatus && matchesProxy;
  });
  const recentTime = session => {
    const recent = sessionRecentActivity(session);
    return recent ? new Date(recent.date).getTime() || 0 : 0;
  };
  sessions.sort((a, b) => {
    if (sort === "label") return String(a.label || "").localeCompare(String(b.label || ""), "tr");
    if (sort === "health") return Number(b.health_score || 0) - Number(a.health_score || 0) || b.id - a.id;
    if (sort === "usage") return Number(b.today_invite_count || 0) - Number(a.today_invite_count || 0) || b.id - a.id;
    if (sort === "recent") return recentTime(b) - recentTime(a) || b.id - a.id;
    return b.id - a.id;
  });
  if ($("#session-filter-count")) $("#session-filter-count").textContent = `${sessions.length} / ${state.sessions.length} session`;
  if (!state.sessions.length) {
    $("#session-table").innerHTML = emptyTable("Henüz telefon eklenmedi", "Numara ekle düğmesiyle ilk Telegram hesabınızı bağlayın.");
    return;
  }
  if (!sessions.length) {
    $("#session-table").innerHTML = emptyTable("Filtreye uygun session yok", "Arama veya filtre seçimini değiştirin.");
    return;
  }
  $("#session-table").innerHTML = `
    <table><thead><tr><th>Session</th><th>Hesap</th><th>Durum</th><th>Kullanım</th><th>Proxy ve sağlık</th><th>Son işlem</th><th>Bekleme</th><th></th></tr></thead><tbody>
      ${sessions.map(session => {
        const status = sessionStatusInfo(session);
        const recent = sessionRecentActivity(session);
        const batchLimit = Math.max(1, Number(session.invite_batch_limit || 3));
        const batchUsed = Math.max(0, Number(session.batch_success_count || 0));
        const batchPercent = Math.min(100, Math.round((batchUsed / batchLimit) * 100));
        const waitSeconds = Number(session.flood_wait_seconds || session.batch_cooldown_seconds || 0);
        return `<tr>
          <td><strong>${escapeHtml(session.label)}</strong><div class="mono">#${session.id} · ${escapeHtml(session.phone_masked)}</div></td>
          <td>${escapeHtml(session.display_name || "—")}${session.username ? `<br><span class="mono">@${escapeHtml(session.username)}</span>` : ""}</td>
          <td>${badge(status.badge)}<small class="session-status-note">${escapeHtml(status.title)} · ${escapeHtml(status.description)}</small></td>
          <td><div class="session-usage"><strong>Bugün ${Number(session.today_invite_count || 0)} kişi</strong><small>Parti ${batchUsed} / ${batchLimit}</small><div><i style="width:${batchPercent}%"></i></div></div></td>
          <td>${session.proxy_enabled ? `<span class="badge ${session.proxy_last_status === "success" ? "active" : session.proxy_last_status === "failed" ? "error" : "warning"}">${escapeHtml((session.proxy_type || "proxy").toUpperCase())}</span><small class="mono session-table-note">${escapeHtml(session.proxy_host || "—")}:${session.proxy_port || "—"}${session.proxy_latency_ms ? ` · ${session.proxy_latency_ms} ms` : ""}</small>` : `<span class="badge error">Proxy yok</span>`}<div class="health session-row-health" title="${escapeHtml(session.health_label || "")}"><div class="health-bar"><i style="width:${Number(session.health_score || 0)}%"></i></div><small>${escapeHtml(session.health_label || `${session.health_score}/100`)}</small></div></td>
          <td>${session.operation_type ? `<strong>${escapeHtml(session.operation_label || session.operation_type)}</strong><small class="session-table-note">${escapeHtml(formatDateTime(session.operation_acquired_at))}</small>` : recent ? `<strong>${escapeHtml(recent.label)}</strong><small class="session-table-note">${escapeHtml(formatDateTime(recent.date))}</small>` : `<span class="session-table-note">Henüz işlem yok</span>`}</td>
          <td class="mono">${waitSeconds ? `<span class="countdown" data-seconds="${waitSeconds}">${formatDuration(waitSeconds)}</span><small class="session-table-note">${escapeHtml(formatDateTime(session.flood_wait_until || session.batch_cooldown_until))}</small>` : "—"}</td>
          <td><button class="mini-button" data-session-detail="${session.id}">Detay</button></td>
        </tr>`;
      }).join("")}
    </tbody></table>`;
}

function openSessionDetail(sessionId) {
  const session = state.sessions.find(item => Number(item.id) === Number(sessionId));
  if (!session) return toast("Session bulunamadı.");
  const status = sessionStatusInfo(session);
  const batchLimit = Math.max(1, Number(session.invite_batch_limit || 3));
  const batchUsed = Math.max(0, Number(session.batch_success_count || 0));
  const errorText = session.last_error || session.proxy_last_error || "Kayıtlı hata yok.";
  $("#session-detail-content").innerHTML = `
    <div class="session-detail-heading"><div><h2>${escapeHtml(session.label)}</h2><p>${escapeHtml(session.display_name || "Telegram hesabı")} · <span class="mono">${escapeHtml(session.phone_masked)}</span></p></div>${badge(status.badge)}</div>
    <div class="session-detail-status"><strong>${escapeHtml(status.title)}</strong><span>${escapeHtml(status.description)}</span></div>
    <div class="session-detail-grid">
      <article><small>BUGÜNKÜ EKLEME</small><strong>${Number(session.today_invite_count || 0)}</strong><span>Başarılı doğrudan ekleme</span></article>
      <article><small>PARTİ KULLANIMI</small><strong>${batchUsed} / ${batchLimit}</strong><span>Sonra ${Number(session.invite_cooldown_minutes || 20)} dk dinlenir</span></article>
      <article><small>BUGÜNKÜ TARAMA KULLANIMI</small><strong>${Number(session.today_activity_count || 0)}</strong><span>Aktiflik tarama operasyonu</span></article>
      <article><small>SESSION SAĞLIĞI</small><strong>${Number(session.health_score || 0)} / 100</strong><span>${escapeHtml(session.health_label || "—")}</span></article>
    </div>
    <div class="session-detail-sections">
      <section><h3>Bağlantı</h3><dl><div><dt>Proxy</dt><dd>${session.proxy_enabled ? `${escapeHtml((session.proxy_type || "proxy").toUpperCase())} · ${escapeHtml(session.proxy_host || "—")}:${session.proxy_port || "—"}` : "Proxy tanımlı değil"}</dd></div><div><dt>Son proxy testi</dt><dd>${escapeHtml(formatDateTime(session.proxy_last_test_at))}${session.proxy_latency_ms ? ` · ${session.proxy_latency_ms} ms` : ""}</dd></div><div><dt>Telegram kullanıcı ID</dt><dd class="mono">${escapeHtml(session.telegram_user_id || "—")}</dd></div></dl></section>
      <section><h3>Son kullanımlar</h3><dl><div><dt>Son başarılı ekleme</dt><dd>${escapeHtml(formatDateTime(session.last_successful_invite_at))}</dd></div><div><dt>Son aktiflik taraması</dt><dd>${escapeHtml(formatDateTime(session.last_activity_at))}</dd></div><div><dt>Mevcut işlem</dt><dd>${session.operation_label ? `${escapeHtml(session.operation_label)} · ${escapeHtml(formatDateTime(session.operation_acquired_at))}` : "Aktif işlem yok"}</dd></div></dl></section>
      <section class="span-2"><h3>Son sistem olayı</h3><dl><div><dt>Zaman</dt><dd>${escapeHtml(formatDateTime(session.last_event_at))}</dd></div><div><dt>Kategori</dt><dd>${escapeHtml(session.last_event_category || "—")}</dd></div><div><dt>Mesaj</dt><dd>${escapeHtml(session.last_event_message || "Henüz sistem olayı yok.")}</dd></div></dl></section>
      <section class="span-2 session-detail-error"><h3>Son hata</h3><p>${escapeHtml(errorText)}</p></section>
    </div>
    <div class="session-detail-footer"><span>Oluşturma: ${escapeHtml(formatDateTime(session.created_at))}</span><span>Son güncelleme: ${escapeHtml(formatDateTime(session.updated_at))}</span></div>`;
  openModal("#session-detail-modal");
}

async function loadSessions() {
  state.sessions = await api("/api/sessions");
  fillSessionSelects();
  const groupCount = groups => state.sessions.filter(item => groups.includes(sessionStatusInfo(item).group)).length;
  $("#session-total").textContent = state.sessions.length;
  $("#session-active").textContent = groupCount(["ready", "busy"]);
  $("#session-wait").textContent = groupCount(["waiting"]);
  $("#session-error").textContent = groupCount(["problem"]);
  renderSessionTable();
}

async function loadJobs() {
  state.jobs = await api("/api/jobs");
  const count = status => state.jobs.filter(item => item.status === status).length;
  $("#job-ready").textContent = state.jobs.filter(item => ["ready", "previewed", "approved", "scheduled"].includes(item.status)).length;
  $("#job-running").textContent = count("running");
  $("#job-completed").textContent = count("completed");
  $("#job-failed").textContent = count("failed");
  const table = jobsTable(state.jobs);
  $("#jobs-table").innerHTML = table;
  $("#recent-jobs").innerHTML = jobsTable(state.jobs.slice(0, 5), true);
}

function jobsTable(jobs, compact = false) {
  if (!jobs.length) return emptyTable("Henüz iş oluşturulmadı", "Çekilecek ve gönderilecek grubu seçerek ilk işi oluşturun.");
  return `<table><thead><tr><th>İş</th><th>Çekilecek → Gönderilecek</th>${compact ? "" : "<th>Session</th>"}<th>Plan</th><th>Durum</th>${compact ? "" : "<th>İşlemler</th>"}</tr></thead><tbody>
    ${jobs.map(j => `<tr><td><strong>${escapeHtml(j.name)}</strong><div class="mono">JOB-${String(j.id).padStart(4,"0")}</div></td><td>${escapeHtml(j.source_title || j.source_ref)} → ${escapeHtml(j.target_title || j.target_ref)}</td>${compact ? "" : `<td>${escapeHtml(j.session_label)}<br><span class="mono">${escapeHtml(j.phone_masked)}</span></td>`}<td>${j.scheduled_at ? new Date(j.scheduled_at).toLocaleString("tr-TR") : "Hemen"}<br><span class="mono">${escapeHtml(j.working_start || "09:00")}–${escapeHtml(j.working_end || "22:00")}</span>${j.resume_at ? `<br><small>Devam: ${new Date(j.resume_at).toLocaleString("tr-TR")}</small>` : ""}</td><td>${badge(j.status)}${j.candidate_count ? `<br><small>${j.candidate_count} uygun aday</small>` : ""}</td>${compact ? "" : `<td><div class="job-actions">${["ready", "previewed"].includes(j.status) ? `<button class="mini-button" data-preview-job="${j.id}">Önizle</button>` : ""}${j.previewed_at ? `<button class="mini-button" data-view-candidates="${j.id}">Sonuçlar</button><a class="mini-button button-link" href="/api/jobs/${j.id}/report.csv">CSV</a>` : ""}</div></td>`}</tr>`).join("")}
  </tbody></table>`;
}

function emptyTable(title, text) {
  return `<div class="empty-state"><div class="empty-icon">◇</div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(text)}</span></div>`;
}

const LOG_FILTER_SELECTORS = ["#log-search", "#log-level-filter", "#log-category-filter", "#log-session-filter", "#log-job-filter", "#log-date-from", "#log-date-to"];

function logLevelLabel(level) {
  return {success:"Başarılı", warning:"Uyarı", error:"Hata", info:"Bilgi"}[level] || level || "Bilinmiyor";
}

function logLocalDate(log) {
  const date = new Date(log.created_at);
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function currentLogFilters() {
  return {
    search: $("#log-search").value.trim().toLocaleLowerCase("tr-TR"),
    level: $("#log-level-filter").value,
    category: $("#log-category-filter").value,
    sessionId: $("#log-session-filter").value.trim(),
    jobId: $("#log-job-filter").value.trim(),
    dateFrom: $("#log-date-from").value,
    dateTo: $("#log-date-to").value,
  };
}

function filteredLogs() {
  const filters = currentLogFilters();
  return state.logs.filter(log => {
    const searchable = `${log.category || ""} ${log.message || ""}`.toLocaleLowerCase("tr-TR");
    const date = logLocalDate(log);
    return (!filters.search || searchable.includes(filters.search))
      && (filters.level === "all" || log.level === filters.level)
      && (filters.category === "all" || log.category === filters.category)
      && (!filters.sessionId || Number(log.session_id) === Number(filters.sessionId))
      && (!filters.jobId || Number(log.job_id) === Number(filters.jobId))
      && (!filters.dateFrom || date >= filters.dateFrom)
      && (!filters.dateTo || date <= filters.dateTo);
  });
}

function renderLogCategoryOptions() {
  const select = $("#log-category-filter");
  const current = select.value;
  const categories = [...new Set(state.logs.map(log => log.category).filter(Boolean))].sort((a, b) => a.localeCompare(b, "tr"));
  select.innerHTML = `<option value="all">Tüm kategoriler</option>${categories.map(category => `<option value="${escapeHtml(category)}">${escapeHtml(category)}</option>`).join("")}`;
  select.value = categories.includes(current) ? current : "all";
}

function renderLogs(logs, selector, detailed = false) {
  $(selector).innerHTML = logs.length ? logs.map(log => {
    const date = new Date(log.created_at);
    const context = [log.session_id ? `<i>Session #${Number(log.session_id)}</i>` : "", log.job_id ? `<i>JOB-${Number(log.job_id)}</i>` : ""].filter(Boolean).join("");
    if (!detailed) return `<div class="log-line ${escapeHtml(log.level)}"><span>[${date.toLocaleTimeString("tr-TR")}]</span><span>${escapeHtml(log.category)}</span><span>${escapeHtml(log.message)}</span></div>`;
    return `<div class="log-line ${escapeHtml(log.level)}" data-log-detail="${Number(log.id)}" title="Detayı aç"><span>[${date.toLocaleTimeString("tr-TR")}]</span><span>${escapeHtml(log.category)}</span><span class="log-message-text">${escapeHtml(log.message)}${context ? `<small class="log-context">${context}</small>` : ""}</span></div>`;
  }).join("") : `<div class="empty-state"><strong>Filtreye uygun log kaydı yok</strong><span>Filtreleri temizleyerek tüm kayıtları görebilirsiniz.</span></div>`;
}

function renderLogPage() {
  const matches = filteredLogs();
  const visible = matches.slice(0, state.logVisibleCount);
  renderLogs(visible, "#all-logs", true);
  $("#log-total").textContent = matches.length;
  $("#log-success").textContent = matches.filter(log => log.level === "success").length;
  $("#log-warning").textContent = matches.filter(log => log.level === "warning").length;
  $("#log-error").textContent = matches.filter(log => log.level === "error").length;
  $("#log-filter-count").textContent = `${visible.length} / ${matches.length} kayıt`;
  $("#load-more-logs").classList.toggle("hidden", visible.length >= matches.length);
}

function setLogRefreshStatus(newCount = 0) {
  const status = $("#log-refresh-status");
  $("#toggle-log-refresh").textContent = state.logsAutoRefresh ? "Otomatik yenilemeyi duraklat" : "Otomatik yenilemeyi sürdür";
  if (!state.logsAutoRefresh && newCount > 0) {
    status.className = "new";
    status.textContent = `${newCount} yeni kayıt bekliyor. Yenile veya otomatik yenilemeyi sürdür.`;
  } else {
    status.className = "";
    status.textContent = state.logsAutoRefresh ? "Otomatik yenileme açık; kayıtlar 15 saniyede bir güncellenir." : "Otomatik yenileme duraklatıldı; arka planda log yazımı devam eder.";
  }
}

async function loadLogs({background = false, resetVisible = false, force = false} = {}) {
  const logs = await api("/api/logs?limit=500");
  renderLogs(logs.slice(0, 10), "#live-logs");
  if (background && !state.logsAutoRefresh && !force) {
    state.pendingLogs = logs;
    const newCount = logs.filter(log => Number(log.id) > Number(state.logsLastSeenId || 0)).length;
    setLogRefreshStatus(newCount);
    return;
  }
  state.logs = logs;
  state.pendingLogs = [];
  state.logsLastSeenId = logs.length ? Number(logs[0].id) : 0;
  if (resetVisible) state.logVisibleCount = 100;
  renderLogCategoryOptions();
  renderLogPage();
  setLogRefreshStatus();
}

function logGuidance(message) {
  const guides = [
    [/FloodWait/i, "Telegram bu session için zorunlu bekleme süresi verdi. Süre dolmadan tekrar denemeyin; Pawgram session'ı otomatik olarak bekletir."],
    [/UserPrivacyRestricted/i, "Kullanıcının gizlilik ayarı doğrudan gruba eklenmesini engelliyor. Bu aday atlanır; manuel işlem de aynı Telegram kısıtına tabidir."],
    [/UserIdInvalid/i, "Session bu kullanıcı için geçerli InputUser/access hash bilgisine sahip değil. Kaynak taramasını erişimi olan aynı session ile yenileyin."],
    [/ChatAdminRequired/i, "Hedef grupta seçili session'ın üye ekleme yönetici yetkisi yok. Session'a gerekli yetkiyi verip grup sağlık kontrolünü çalıştırın."],
    [/PeerFlood|Too many requests/i, "Telegram hesap düzeyinde işlem kısıtı bildirdi. Hesabı zorlamayın; bildirilen dinlenme süresini tamamlamasını bekleyin."],
    [/proxy|socks|connection refused|timed? out|timeout/i, "Proxy bağlantısı kurulamadı veya yanıt vermedi. Proxy sağlık testini çalıştırın; host, port ve kimlik bilgilerini kontrol edin. Ana IP'ye geri dönüş yapılmaz."],
    [/AuthKey|session.*invalid|SessionRevoked/i, "Telegram session anahtarı geçersiz veya iptal edilmiş olabilir. Hesabı Session'lar sayfasından yeniden doğrulayın."],
  ];
  return guides.find(([pattern]) => pattern.test(message))?.[1] || "Bu kayıt için özel bir otomatik açıklama bulunmuyor. Kategori, session/JOB bilgisi ve zaman damgasını destek incelemesinde kullanabilirsiniz.";
}

function openLogDetail(logId) {
  const log = [...state.logs, ...state.pendingLogs].find(item => Number(item.id) === Number(logId));
  if (!log) return toast("Log kaydı artık listede bulunmuyor.");
  $("#log-detail-content").innerHTML = `
    <div class="log-detail-heading"><div><h2>Log kaydı #${Number(log.id)}</h2><p>${escapeHtml(log.category)} · ${formatDateTime(log.created_at)}</p></div>${badge(log.level)}</div>
    <div class="log-detail-meta"><div><small>SEVİYE</small><strong>${escapeHtml(logLevelLabel(log.level))}</strong></div><div><small>KATEGORİ</small><strong>${escapeHtml(log.category)}</strong></div><div><small>SESSION</small><strong>${log.session_id ? `#${Number(log.session_id)}` : "—"}</strong></div><div><small>İŞ</small><strong>${log.job_id ? `JOB-${Number(log.job_id)}` : "—"}</strong></div></div>
    <div class="log-detail-message">${escapeHtml(log.message)}</div>
    <div class="log-guidance"><strong>Açıklama ve öneri</strong><span>${escapeHtml(logGuidance(log.message))}</span></div>
    <div class="log-detail-actions"><button class="secondary" data-copy-log="${Number(log.id)}">Mesajı kopyala</button></div>`;
  openModal("#log-detail-modal");
}

async function copyText(value) {
  if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(value);
  else {
    const input = document.createElement("textarea");
    input.value = value;
    document.body.appendChild(input);
    input.select();
    document.execCommand("copy");
    input.remove();
  }
}

async function copyLogMessage(logId) {
  const log = [...state.logs, ...state.pendingLogs].find(item => Number(item.id) === Number(logId));
  if (!log) throw new Error("Log kaydı bulunamadı.");
  await copyText(log.message);
  toast("Log mesajı kopyalandı.");
}

function clearLogFilters() {
  $("#log-search").value = "";
  $("#log-level-filter").value = "all";
  $("#log-category-filter").value = "all";
  $("#log-session-filter").value = "";
  $("#log-job-filter").value = "";
  $("#log-date-from").value = "";
  $("#log-date-to").value = "";
  state.logVisibleCount = 100;
  renderLogPage();
}

function exportLogs(format) {
  const filters = currentLogFilters();
  const parameters = new URLSearchParams({format});
  if (filters.search) parameters.set("search", filters.search);
  if (filters.level !== "all") parameters.set("level", filters.level);
  if (filters.category !== "all") parameters.set("category", filters.category);
  if (filters.sessionId) parameters.set("session_id", filters.sessionId);
  if (filters.jobId) parameters.set("job_id", filters.jobId);
  if (filters.dateFrom) parameters.set("date_from", filters.dateFrom);
  if (filters.dateTo) parameters.set("date_to", filters.dateTo);
  window.location.href = `/api/logs/export?${parameters}`;
}

async function resolveGroup(sessionId, reference) {
  return api("/api/groups/resolve", { method:"POST", body:JSON.stringify({ session_id:Number(sessionId), reference }) });
}

function groupCard(group, label = "Grup") {
  const sourceSuitable = group.source_suitable ?? ["group", "megagroup"].includes(group.kind);
  const targetSuitable = group.target_suitable ?? (sourceSuitable && Boolean(group.can_invite_users));
  return `<strong>${escapeHtml(label)}:</strong> ${escapeHtml(group.title)} · <span class="mono">ID ${group.id}</span> · ${escapeHtml(groupKindLabel(group.kind))} · ${group.admin_rights || group.creator ? "yönetici erişimi" : "standart erişim"} · <span class="permission-ok">Kaynak: ${sourceSuitable ? "uygun" : "uygun değil"}</span> · <span class="${targetSuitable ? "permission-ok" : "permission-missing"}">Hedef: ${targetSuitable ? "uygun" : "yetki gerekli"}</span>`;
}

function groupKindLabel(kind) {
  return {group:"Grup", megagroup:"Megagroup", channel:"Kanal"}[kind] || kind || "Bilinmiyor";
}

function groupIsAdmin(group) {
  return Boolean(group.creator || group.admin_rights);
}

function groupSourceSuitable(group) {
  return group.source_suitable ?? ["group", "megagroup"].includes(group.kind);
}

function groupTargetSuitable(group) {
  return group.target_suitable ?? (groupSourceSuitable(group) && Boolean(group.can_invite_users));
}

function renderGroupSummary() {
  $("#group-total").textContent = state.groups.length;
  $("#group-source-ready").textContent = state.groups.filter(groupSourceSuitable).length;
  $("#group-target-ready").textContent = state.groups.filter(groupTargetSuitable).length;
  $("#group-admin").textContent = state.groups.filter(groupIsAdmin).length;
}

function renderGroups() {
  const container = $("#groups-table");
  const search = ($("#group-search")?.value || "").trim().toLocaleLowerCase("tr-TR");
  const kindFilter = $("#group-kind-filter")?.value || "all";
  const suitabilityFilter = $("#group-suitability-filter")?.value || "all";
  const sort = $("#group-sort")?.value || "title";
  let groups = state.groups.filter(group => {
    const searchable = [group.title, group.username, group.id].filter(value => value !== null && value !== undefined).join(" ").toLocaleLowerCase("tr-TR");
    const matchesSearch = !search || searchable.includes(search);
    const matchesKind = kindFilter === "all" || group.kind === kindFilter;
    const matchesSuitability = suitabilityFilter === "all"
      || (suitabilityFilter === "source" && groupSourceSuitable(group))
      || (suitabilityFilter === "target" && groupTargetSuitable(group))
      || (suitabilityFilter === "admin" && groupIsAdmin(group));
    return matchesSearch && matchesKind && matchesSuitability;
  });
  groups.sort((a, b) => {
    if (sort === "participants") return Number(b.participants_count || 0) - Number(a.participants_count || 0) || String(a.title || "").localeCompare(String(b.title || ""), "tr");
    if (sort === "unread") return Number(b.unread_count || 0) - Number(a.unread_count || 0) || String(a.title || "").localeCompare(String(b.title || ""), "tr");
    if (sort === "id") return Math.abs(Number(a.id || 0)) - Math.abs(Number(b.id || 0));
    return String(a.title || "").localeCompare(String(b.title || ""), "tr");
  });
  renderGroupSummary();
  $("#group-filter-count").textContent = `${groups.length} / ${state.groups.length} grup`;
  if (state.currentGroupSessionId === null) {
    container.className = "table-wrap empty-state";
    container.innerHTML = `<div class="empty-icon">◫</div><strong>Henüz grup yüklenmedi</strong><span>Bir session seçip “Grupları getir” düğmesine basın.</span>`;
    return;
  }
  container.className = "table-wrap";
  if (!state.groups.length) {
    container.innerHTML = emptyTable("Grup bulunamadı", "Bu hesabın erişebildiği grup görünmüyor.");
    return;
  }
  if (!groups.length) {
    container.innerHTML = emptyTable("Filtreye uygun grup yok", "Arama veya filtre seçimini değiştirin.");
    return;
  }
  container.innerHTML = `<table><thead><tr><th>Grup</th><th>Tür</th><th>Üye / mesaj</th><th>Erişim</th><th>Uygunluk</th><th>İşlemler</th></tr></thead><tbody>${groups.map(group => `
    <tr><td><strong>${escapeHtml(group.title)}</strong><div class="mono">${group.id}</div>${group.username ? `<small class="mono">@${escapeHtml(group.username)}</small>` : ""}</td><td>${badge(group.kind)}</td><td><strong>${group.participants_count === null || group.participants_count === undefined ? "—" : Number(group.participants_count).toLocaleString("tr-TR")}</strong> üye<br><small>${Number(group.unread_count || 0)} okunmamış</small></td><td>${group.creator ? `<span class="badge active">Kurucu</span>` : group.admin_rights ? `<span class="badge active">Yönetici</span>` : `<span class="badge">Standart</span>`}${group.can_invite_users ? `<small class="permission-ok group-table-note">Üye ekleme yetkisi var</small>` : `<small class="group-table-note">Üye ekleme yetkisi doğrulanmadı</small>`}</td><td><span class="group-suitability ${groupSourceSuitable(group) ? "ok" : "bad"}">Kaynak ${groupSourceSuitable(group) ? "uygun" : "uygun değil"}</span><span class="group-suitability ${groupTargetSuitable(group) ? "ok" : "warn"}">Hedef ${groupTargetSuitable(group) ? "uygun" : "yetki gerekli"}</span></td><td><div class="job-actions"><button class="mini-button" data-group-detail="${group.id}">Detay</button><button class="mini-button" data-copy-group="${group.id}" data-copy-kind="id">ID kopyala</button>${group.username ? `<button class="mini-button" data-copy-group="${group.id}" data-copy-kind="username">@ kopyala</button>` : ""}</div></td></tr>`).join("")}</tbody></table>`;
}

function openGroupDetail(groupId) {
  const group = state.groups.find(item => Number(item.id) === Number(groupId));
  if (!group) return toast("Grup bulunamadı.");
  const session = state.sessions.find(item => Number(item.id) === Number(state.currentGroupSessionId));
  const sourceSuitable = groupSourceSuitable(group);
  const targetSuitable = groupTargetSuitable(group);
  $("#group-detail-content").innerHTML = `
    <div class="group-detail-heading"><div><h2>${escapeHtml(group.title)}</h2><p>${group.username ? `<span class="mono">@${escapeHtml(group.username)}</span> · ` : ""}<span class="mono">${group.id}</span></p></div>${badge(group.kind)}</div>
    <div class="group-detail-grid">
      <article><small>ÜYE SAYISI</small><strong>${group.participants_count === null || group.participants_count === undefined ? "—" : Number(group.participants_count).toLocaleString("tr-TR")}</strong><span>Telegram dialog bilgisi</span></article>
      <article><small>OKUNMAMIŞ</small><strong>${Number(group.unread_count || 0)}</strong><span>Seçili session için</span></article>
      <article><small>ERİŞİM</small><strong>${group.creator ? "Kurucu" : group.admin_rights ? "Yönetici" : "Standart"}</strong><span>${group.can_invite_users ? "Üye ekleme yetkisi var" : "Üye ekleme yetkisi yok veya doğrulanmadı"}</span></article>
      <article><small>SEÇİLİ SESSION</small><strong>${escapeHtml(session?.label || "—")}</strong><span>${escapeHtml(session?.phone_masked || "Session bulunamadı")}</span></article>
    </div>
    <div class="group-suitability-grid">
      <section class="${sourceSuitable ? "ok" : "bad"}"><strong>Kaynak grup uygunluğu</strong><span>${sourceSuitable ? "Üye ve aktiflik taraması için grup türü uygundur." : "Yayın kanalı kaynak üye taraması için uygun değildir."}</span></section>
      <section class="${targetSuitable ? "ok" : "warn"}"><strong>Hedef grup uygunluğu</strong><span>${targetSuitable ? "Seçili session için üye ekleme yetkisi doğrulandı." : "Hedef olarak kullanmadan önce session'a üye ekleme yetkisi verin ve sağlık kontrolünü çalıştırın."}</span></section>
    </div>
    <div class="group-detail-sections"><section><h3>Grup bilgileri</h3><dl><div><dt>ID</dt><dd class="mono">${group.id}</dd></div><div><dt>Kullanıcı adı</dt><dd>${group.username ? `@${escapeHtml(group.username)}` : "Özel grup / kullanıcı adı yok"}</dd></div><div><dt>Tür</dt><dd>${escapeHtml(groupKindLabel(group.kind))}</dd></div></dl></section><section><h3>Yetkiler</h3><dl><div><dt>Kurucu</dt><dd>${group.creator ? "Evet" : "Hayır"}</dd></div><div><dt>Yönetici erişimi</dt><dd>${group.admin_rights ? "Var" : "Yok"}</dd></div><div><dt>Üye ekleme</dt><dd>${group.can_invite_users ? "İzin var" : "İzin yok veya doğrulanmadı"}</dd></div></dl></section></div>
    <div class="group-detail-actions"><button class="secondary" data-copy-group="${group.id}" data-copy-kind="id">Grup ID'sini kopyala</button>${group.username ? `<button class="secondary" data-copy-group="${group.id}" data-copy-kind="username">@kullanıcı adını kopyala</button>` : ""}</div>`;
  openModal("#group-detail-modal");
}

async function copyGroupValue(groupId, kind) {
  const group = state.groups.find(item => Number(item.id) === Number(groupId));
  if (!group) throw new Error("Grup bulunamadı.");
  const value = kind === "username" ? group.username ? `@${group.username}` : "" : String(group.id);
  if (!value) throw new Error("Bu grubun kullanıcı adı yok.");
  if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(value);
  else {
    const input = document.createElement("textarea");
    input.value = value;
    document.body.appendChild(input);
    input.select();
    document.execCommand("copy");
    input.remove();
  }
  toast(`${kind === "username" ? "Kullanıcı adı" : "Grup ID"} kopyalandı.`);
}

function groupAccessPurposeLabel(purpose) {
  return purpose === "target" ? "Hedef grup / yetki" : "Kaynak grup erişimi";
}

function renderGroupAccessBatches(detail = null) {
  const container = $("#group-access-batches");
  if (!container) return;
  if (detail?.batch) state.currentGroupAccessDetail = detail;
  if (!state.groupAccessBatches.length) {
    state.currentGroupAccessDetail = null;
    $("#group-access-result-count").textContent = "0 sonuç";
    container.innerHTML = emptyTable("Henüz hazırlama kuyruğu yok", "Grubu ve sessionları seçerek ilk sıralı kontrolü başlatın.");
    return;
  }
  const batchesTable = `<table><thead><tr><th>Kuyruk</th><th>Grup</th><th>Amaç</th><th>İlerleme</th><th>Durum</th><th>İşlem</th></tr></thead><tbody>
    ${state.groupAccessBatches.map(batch => `<tr${Number(batch.id) === Number(state.currentGroupAccessBatchId) ? ' class="selected"' : ""}><td class="mono">#${batch.id}</td><td>${escapeHtml(batch.group_ref)}</td><td>${escapeHtml(groupAccessPurposeLabel(batch.purpose))}</td><td>${batch.processed_count || 0}/${batch.total_count || 0}<br><small>${batch.joined_count || 0} katıldı · ${batch.pending_count || 0} onay · ${batch.failed_count || 0} hata</small></td><td>${badge(batch.status)}${batch.next_action_at ? `<br><small>Devam: ${formatDateTime(batch.next_action_at)}</small>` : ""}</td><td><div class="job-actions"><button class="mini-button" data-group-access-view="${batch.id}">Detay</button>${["running","queued","paused"].includes(batch.status) ? `<button class="mini-button" data-group-access-stop="${batch.id}">Durdur</button>` : ""}${["paused","stopped"].includes(batch.status) || (batch.status === "completed" && ((batch.pending_count || 0) + (batch.failed_count || 0) > 0)) ? `<button class="mini-button" data-group-access-resume="${batch.id}">Yeniden kontrol et</button>` : ""}</div></td></tr>`).join("")}
    </tbody></table>`;
  let detailHtml = "";
  const activeDetail = detail?.batch ? detail : state.currentGroupAccessDetail;
  if (activeDetail?.batch) {
    const batch = activeDetail.batch;
    const search = ($("#group-access-result-search")?.value || "").trim().toLocaleLowerCase("tr-TR");
    const statusFilter = $("#group-access-result-filter")?.value || "all";
    const matchesStatus = item => statusFilter === "all"
      || (statusFilter === "ready" && ["joined", "already_member"].includes(item.status))
      || (statusFilter === "approval_pending" && item.status === "approval_pending")
      || (statusFilter === "failed" && item.status === "failed")
      || (statusFilter === "pending" && ["queued", "running"].includes(item.status));
    const items = activeDetail.items.filter(item => {
      const searchable = [item.session_label, item.phone_masked, item.resolved_group_title, item.resolved_group_id, item.reason]
        .filter(value => value !== null && value !== undefined).join(" ").toLocaleLowerCase("tr-TR");
      return (!search || searchable.includes(search)) && matchesStatus(item);
    });
    $("#group-access-result-count").textContent = `${items.length} / ${activeDetail.items.length} sonuç`;
    detailHtml = `<div class="group-access-detail"><div class="section-heading"><div><h3>Kuyruk #${batch.id} ayrıntısı</h3><p>${escapeHtml(batch.group_ref)} · ${escapeHtml(groupAccessPurposeLabel(batch.purpose))}</p></div><span>${items.length}/${activeDetail.items.length} gösteriliyor</span></div>
      <table><thead><tr><th>Sıra</th><th>Session</th><th>Durum</th><th>Grup</th><th>Üye ekleme yetkisi</th><th>Açıklama</th></tr></thead><tbody>
      ${items.length ? items.map(item => `<tr><td>${item.position}</td><td><strong>${escapeHtml(item.session_label)}</strong><br><span class="mono">${escapeHtml(item.phone_masked)}</span></td><td>${badge(item.status)}</td><td>${item.resolved_group_title ? `${escapeHtml(item.resolved_group_title)}<br><span class="mono">${item.resolved_group_id}</span>` : "—"}</td><td>${batch.purpose !== "target" || item.can_invite_users === null ? "—" : item.can_invite_users ? '<span class="permission-ok">Var</span>' : '<span class="permission-missing">Yok</span>'}</td><td>${escapeHtml(item.reason || "Sırasını bekliyor")}</td></tr>`).join("") : `<tr><td colspan="6"><div class="group-filter-empty">Filtreye uygun hazırlama sonucu yok.</div></td></tr>`}
      </tbody></table></div>`;
  } else {
    $("#group-access-result-count").textContent = "0 sonuç";
  }
  container.innerHTML = batchesTable + detailHtml;
}

async function loadGroupAccessBatches() {
  state.groupAccessBatches = await api("/api/group-access-batches");
  if (!state.groupAccessBatches.length) {
    state.currentGroupAccessBatchId = null;
    state.currentGroupAccessDetail = null;
    renderGroupAccessBatches();
    return;
  }
  if (!state.groupAccessBatches.some(batch => Number(batch.id) === Number(state.currentGroupAccessBatchId))) {
    state.currentGroupAccessBatchId = Number(state.groupAccessBatches[0].id);
  }
  const detail = await api(`/api/group-access-batches/${state.currentGroupAccessBatchId}`);
  renderGroupAccessBatches(detail);
}

async function showGroupAccessBatch(batchId) {
  state.currentGroupAccessBatchId = Number(batchId);
  const detail = await api(`/api/group-access-batches/${batchId}`);
  renderGroupAccessBatches(detail);
}

async function startGroupAccessBatch() {
  const sessionIds = $$(".group-access-session-checkbox:checked").map(item => Number(item.value));
  const groupRef = $("#group-access-reference").value.trim();
  const minDelay = Number($("#group-access-min-delay").value);
  const maxDelay = Number($("#group-access-max-delay").value);
  if (!groupRef) return showMessage("#group-access-message", "Grup ID, @kullanıcı adı veya davet bağlantısı girin.");
  if (!sessionIds.length) return showMessage("#group-access-message", "En az bir session seçin.");
  if (minDelay > maxDelay) return showMessage("#group-access-message", "En az bekleme, en fazla beklemeden büyük olamaz.");
  const button = $("#start-group-access");
  button.disabled = true;
  button.classList.add("is-loading");
  button.textContent = "Kuyruk başlatılıyor";
  showMessage("#group-access-message", "Sessionlar sıralı kuyruğa alınıyor…");
  try {
    const result = await api("/api/group-access-batches", {method:"POST", body:JSON.stringify({
      group_ref:groupRef,
      purpose:$("#group-access-purpose").value,
      session_ids:sessionIds,
      min_delay_seconds:minDelay,
      max_delay_seconds:maxDelay,
    })});
    state.currentGroupAccessBatchId = Number(result.batch.id);
    showMessage("#group-access-message", `Kuyruk #${result.batch.id} başlatıldı. Sessionlar tek tek kontrol ediliyor.`, true);
    await loadGroupAccessBatches();
  } catch (error) {
    showMessage("#group-access-message", error.message);
  } finally {
    button.disabled = false;
    button.classList.remove("is-loading");
    button.textContent = "Sessionları gruba hazırla";
  }
}

async function groupAccessAction(batchId, action) {
  try {
    await api(`/api/group-access-batches/${batchId}/${action}`, {method:"POST"});
    state.currentGroupAccessBatchId = Number(batchId);
    toast(action === "stop" ? "Hazırlama kuyruğu durduruldu." : "Hazırlama kuyruğu yeniden başlatıldı.");
    await loadGroupAccessBatches();
  } catch (error) {
    toast(error.message);
  }
}

function healthCheckChip(label, value) {
  const statusClass = value === true ? "ok" : value === false ? "bad" : "unknown";
  const statusText = value === true ? "OK" : value === false ? "Sorun" : "—";
  return `<span class="${statusClass}">${escapeHtml(label)}: ${statusText}</span>`;
}

function renderSessionHealth(detail = null) {
  const container = $("#session-health-results");
  if (!container) return;
  if (!state.sessionHealthBatches.length) {
    container.innerHTML = emptyTable("Henüz sağlık kontrolü yok", "Seçili sessionlar için ilk ön kontrolü başlatın.");
    return;
  }
  if (!detail?.batch) return;
  const batch = detail.batch;
  const actions = `<div class="job-actions">${["running","queued","paused"].includes(batch.status) ? `<button class="mini-button" data-session-health-stop="${batch.id}">Durdur</button>` : ""}${["paused","stopped"].includes(batch.status) || (batch.status === "completed" && ((batch.warning_count || 0) + (batch.failed_count || 0) > 0)) ? `<button class="mini-button" data-session-health-resume="${batch.id}">Sorunluları yeniden kontrol et</button>` : ""}</div>`;
  container.innerHTML = `<div class="section-heading"><div><h3>Sağlık kontrolü #${batch.id}</h3><p>${batch.processed_count || 0}/${batch.total_count || 0} tamamlandı · ${batch.ready_count || 0} hazır · ${batch.warning_count || 0} uyarı · ${batch.failed_count || 0} hata</p></div><div>${badge(batch.status)}${actions}</div></div>
    <table><thead><tr><th>Session</th><th>Sonuç</th><th>Kontroller</th><th>Gecikme</th><th>Açıklama</th></tr></thead><tbody>
    ${detail.items.map(item => `<tr><td><strong>${escapeHtml(item.session_label)}</strong><br><span class="mono">${escapeHtml(item.phone_masked)}</span></td><td>${badge(item.status)}${item.busy_operation ? `<small class="session-lock-label">${escapeHtml(item.busy_operation)}</small>` : ""}</td><td><div class="health-checks">${healthCheckChip("Proxy", item.proxy_ok)}${healthCheckChip("Session", item.session_ok)}${healthCheckChip("Kaynak", item.source_access)}${healthCheckChip("Hedef", item.target_access)}${healthCheckChip("Ekleme yetkisi", item.target_can_invite)}</div></td><td class="mono">${item.latency_ms ? `${item.latency_ms} ms` : "—"}</td><td>${escapeHtml(item.reason || "Sırasını bekliyor")}</td></tr>`).join("")}
    </tbody></table>`;
}

async function loadSessionHealthBatches() {
  state.sessionHealthBatches = await api("/api/session-health-batches");
  if (!state.sessionHealthBatches.length) {
    state.currentSessionHealthBatchId = null;
    renderSessionHealth();
    return;
  }
  if (!state.sessionHealthBatches.some(batch => Number(batch.id) === Number(state.currentSessionHealthBatchId))) {
    state.currentSessionHealthBatchId = Number(state.sessionHealthBatches[0].id);
  }
  const detail = await api(`/api/session-health-batches/${state.currentSessionHealthBatchId}`);
  renderSessionHealth(detail);
}

async function startSessionHealthBatch() {
  const sessionIds = $$(".group-access-session-checkbox:checked").map(item => Number(item.value));
  if (!sessionIds.length) return showMessage("#session-health-message", "En az bir session seçin.");
  const button = $("#start-session-health");
  button.disabled = true;
  button.classList.add("is-loading");
  button.textContent = "Kontrol başlatılıyor";
  showMessage("#session-health-message", "Proxy ve Telegram erişimi sessionlar üzerinde sırayla kontrol ediliyor…");
  try {
    const result = await api("/api/session-health-batches", {method:"POST", body:JSON.stringify({
      session_ids:sessionIds,
      source_ref:$("#session-health-source").value.trim() || null,
      target_ref:$("#session-health-target").value.trim() || null,
    })});
    state.currentSessionHealthBatchId = Number(result.batch.id);
    showMessage("#session-health-message", `Sağlık kontrolü #${result.batch.id} başlatıldı.`, true);
    await loadSessionHealthBatches();
  } catch (error) {
    showMessage("#session-health-message", error.message);
  } finally {
    button.disabled = false;
    button.classList.remove("is-loading");
    button.textContent = "Seçili sessionları kontrol et";
  }
}

async function sessionHealthAction(batchId, action) {
  try {
    await api(`/api/session-health-batches/${batchId}/${action}`, {method:"POST"});
    state.currentSessionHealthBatchId = Number(batchId);
    toast(action === "stop" ? "Sağlık kontrolü durduruldu." : "Sorunlu sessionlar yeniden kontrol ediliyor.");
    await loadSessionHealthBatches();
  } catch (error) {
    toast(error.message);
  }
}

async function validateQuick() {
  const result = $("#quick-result");
  try {
    result.className = "validation-result";
    result.textContent = "Gruplar Telegram üzerinden doğrulanıyor…";
    const sessionId = $("#quick-session").value;
    if (!sessionId) throw new Error("Önce bir Telegram hesabı bağlayın.");
    const [source, target] = await Promise.all([
      resolveGroup(sessionId, $("#quick-source").value),
      resolveGroup(sessionId, $("#quick-target").value),
    ]);
    if (source.id === target.id) throw new Error("Çekilecek ve gönderilecek grup aynı olamaz.");
    result.innerHTML = `${groupCard(source,"Çekilecek grup")}<br>${groupCard(target,"Gönderilecek grup")}`;
  } catch (error) {
    result.className = "validation-result error";
    result.textContent = error.message;
  }
}

async function loadGroups() {
  const sessionId = $("#group-session").value;
  if (!sessionId) return toast("Önce bir Telegram hesabı bağlayın.");
  const container = $("#groups-table");
  container.className = "table-wrap empty-state";
  container.innerHTML = "Gruplar Telegram'dan alınıyor…";
  try {
    state.groups = await api(`/api/sessions/${sessionId}/groups`);
    state.currentGroupSessionId = Number(sessionId);
    renderGroups();
  } catch (error) {
    state.groups = [];
    state.currentGroupSessionId = null;
    renderGroupSummary();
    $("#group-filter-count").textContent = "0 grup";
    container.className = "table-wrap empty-state";
    container.innerHTML = `<strong>Gruplar alınamadı</strong><span>${escapeHtml(error.message)}</span>`;
  }
}

function heartbeatStatusBadge(status) {
  const labels = {
    never_run:"Henüz çalışmadı",
    pending:"İlk çalışma bekleniyor",
    scheduled:"Planlandı",
    running:"Çalışıyor",
    success:"Başarılı",
    failed:"Hatalı",
    skipped_busy:"Meşgul olduğu için atlandı",
    inactive:"Session aktif değil",
    disabled:"Kapalı",
  };
  return `<span class="badge ${escapeHtml(status)}">${escapeHtml(labels[status] || status)}</span>`;
}

function renderHeartbeat() {
  const data = state.heartbeat;
  if (!data) return;
  const settings = data.settings;
  const sessions = data.sessions || [];
  $("#heartbeat-enabled-state").textContent = settings.enabled ? "Açık" : "Kapalı";
  $("#heartbeat-enabled-state").className = settings.enabled ? "green" : "yellow";
  $("#heartbeat-interval-state").textContent = `${settings.interval_minutes} dk`;
  $("#heartbeat-success-total").textContent = sessions.reduce((total, item) => total + Number(item.success_count || 0), 0);
  $("#heartbeat-failure-total").textContent = sessions.reduce((total, item) => total + Number(item.failure_count || 0), 0);
  $("#heartbeat-table").innerHTML = sessions.length ? `<table><thead><tr><th>SESSION ID</th><th>SESSION</th><th>SON HEARTBEAT</th><th>SON BAŞARI</th><th>SON HATA</th><th>BAŞARI</th><th>HATA</th><th>DURUM</th><th>SONRAKİ HEARTBEAT</th></tr></thead><tbody>${sessions.map(item => `
    <tr>
      <td class="mono">#${Number(item.session_id)}</td>
      <td><strong>${escapeHtml(item.session_label || "İsimsiz")}</strong><br><small>${badge(item.session_status)}</small></td>
      <td>${escapeHtml(formatDateTime(item.last_heartbeat_at))}</td>
      <td>${escapeHtml(formatDateTime(item.last_success_at))}</td>
      <td>${escapeHtml(formatDateTime(item.last_failure_at))}${item.last_error ? `<br><small class="red">${escapeHtml(item.last_error)}</small>` : ""}</td>
      <td class="green">${Number(item.success_count || 0)}</td>
      <td class="red">${Number(item.failure_count || 0)}</td>
      <td>${heartbeatStatusBadge(item.current_status)}</td>
      <td>${escapeHtml(formatDateTime(item.next_heartbeat_at))}</td>
    </tr>`).join("")}</tbody></table>` : emptyTable("Heartbeat session kaydı yok", "Telegram session eklendiğinde burada görünecektir.");
}

async function loadHeartbeat({syncForm = false} = {}) {
  state.heartbeat = await api("/api/heartbeat");
  if (syncForm) {
    const settings = state.heartbeat.settings;
    $("#heartbeat-enabled").checked = Boolean(settings.enabled);
    $("#heartbeat-interval").value = settings.interval_minutes;
    $("#heartbeat-group-id").value = settings.group_id || "";
    $("#heartbeat-message").value = settings.message_template || "Merhabaa";
  }
  renderHeartbeat();
}

async function saveHeartbeatSettings() {
  const button = $("#save-heartbeat-settings");
  button.disabled = true;
  button.classList.add("is-loading");
  button.textContent = "Kaydediliyor…";
  try {
    const enabled = $("#heartbeat-enabled").checked;
    const intervalMinutes = Number($("#heartbeat-interval").value);
    const groupId = $("#heartbeat-group-id").value.trim();
    const messageTemplate = $("#heartbeat-message").value.trim();
    if (!Number.isInteger(intervalMinutes) || intervalMinutes < 1 || intervalMinutes > 10080) {
      throw new Error("Heartbeat aralığı 1 ile 10080 dakika arasında tam sayı olmalıdır.");
    }
    if (groupId && !/^-?\d+$/.test(groupId)) {
      throw new Error("Heartbeat Group ID yalnızca sayısal Telegram grup ID'si olmalıdır.");
    }
    if (enabled && !groupId) throw new Error("Heartbeat etkinleştirilmeden önce Group ID girilmelidir.");
    if (!messageTemplate) throw new Error("Heartbeat mesajı boş olamaz.");
    await api("/api/heartbeat/settings", {
      method:"POST",
      body:JSON.stringify({
        enabled,
        interval_minutes:intervalMinutes,
        group_id:groupId,
        message_template:messageTemplate,
      }),
    });
    await loadHeartbeat({syncForm:true});
    showMessage("#heartbeat-settings-message", "Heartbeat ayarları kaydedildi.", true);
    toast("Heartbeat ayarları kaydedildi.");
  } catch (error) {
    showMessage("#heartbeat-settings-message", error.message);
  } finally {
    button.disabled = false;
    button.classList.remove("is-loading");
    button.textContent = "Kaydet";
  }
}

function activityWindowLabel(hours) {
  return {24:"24 saat",72:"3 gün",168:"7 gün",720:"30 gün"}[hours] || `${hours} saat`;
}

async function loadActivityScans() {
  state.activityScans = await api("/api/activity-scans");
  const activeStatuses = ["queued", "running", "scheduled"];
  $("#activity-total").textContent = state.activityScans.length;
  $("#activity-running").textContent = state.activityScans.filter(item => activeStatuses.includes(item.status)).length;
  $("#activity-users").textContent = state.activityScans.length ? (state.activityScans[0].global_unique_users || 0) : 0;
  $("#activity-waiting").textContent = state.activityScans.filter(item => item.status === "waiting").length;
  $("#activity-table").innerHTML = state.activityScans.length ? `
    <table><thead><tr><th>Tarama</th><th>Grup</th><th>Aralık</th><th>Session</th><th>Sonuç</th><th>Durum</th><th>Sonraki çalışma</th><th>İşlemler</th></tr></thead><tbody>
      ${state.activityScans.map(scan => `<tr>
        <td><strong>${escapeHtml(scan.name)}</strong><div class="mono">SCAN-${String(scan.id).padStart(4,"0")}</div></td>
        <td>${escapeHtml(scan.group_title || scan.group_ref)}<br><span class="mono">${scan.group_id || escapeHtml(scan.group_ref)}</span></td>
        <td>${activityWindowLabel(scan.window_hours)}${scan.recurring ? `<br><small>Otomatik · ${Math.round(scan.interval_minutes/60)} saatte bir</small>` : ""}</td>
        <td>${scan.session_label ? `${escapeHtml(scan.session_label)}<br><span class="mono">${escapeHtml(scan.phone_masked)}</span>` : "Otomatik seçim"}</td>
        <td><strong>${scan.unique_users || 0}</strong> kullanıcı<br><small>${scan.message_count || 0} mesaj</small></td>
        <td><div class="activity-status ${escapeHtml(scan.status)}"><i></i>${badge(scan.status)}</div>${scan.last_error ? `<small class="red">${escapeHtml(scan.last_error)}</small>` : ""}</td>
        <td>${scan.next_run_at ? new Date(scan.next_run_at).toLocaleString("tr-TR") : "—"}</td>
        <td><div class="job-actions"><button class="mini-button ${state.runningScanIds.has(scan.id) || ["queued","running"].includes(scan.status) ? "is-loading" : ""}" data-activity-run="${scan.id}" ${state.runningScanIds.has(scan.id) || ["queued","running"].includes(scan.status) ? "disabled" : ""}>${state.runningScanIds.has(scan.id) || ["queued","running"].includes(scan.status) ? "Analiz ediliyor" : "Çalıştır"}</button>${scan.status === "paused" ? `<button class="mini-button" data-activity-resume="${scan.id}">Devam</button>` : ["queued","running","scheduled","waiting","waiting_join","waiting_budget"].includes(scan.status) ? `<button class="mini-button" data-activity-pause="${scan.id}">Duraklat</button>` : ""}${scan.last_run_at ? `<button class="mini-button" data-activity-results="${scan.id}">Sonuçlar</button><a class="mini-button button-link" href="/api/activity-scans/${scan.id}/report.csv">CSV</a>` : ""}${!["queued","running"].includes(scan.status) ? `<button class="mini-button danger-mini" data-activity-delete="${scan.id}">Sil</button>` : ""}</div></td>
      </tr>`).join("")}
    </tbody></table>` : emptyTable("Henüz aktivite taraması yok", "Bir grup ve zaman aralığı seçerek ilk taramayı başlatın.");
}

async function createActivityScan() {
  const button = $("#create-activity-scan");
  button.disabled = true;
  button.textContent = "Kuyruğa ekleniyor…";
  try {
    const payload = {
      name:$("#activity-name").value.trim(),
      session_id:$("#activity-session").value ? Number($("#activity-session").value) : null,
      group_ref:$("#activity-group").value.trim(),
      window_hours:Number($("#activity-window").value),
      recurring:$("#activity-recurring").checked,
      interval_minutes:Number($("#activity-interval").value),
    };
    const result = await api("/api/activity-scans", {method:"POST", body:JSON.stringify(payload)});
    state.runningScanIds.add(result.scan_id);
    button.classList.add("is-loading");
    button.textContent = "Grup analiz ediliyor";
    toast(`SCAN-${String(result.scan_id).padStart(4,"0")} çalıştırıldı.`);
    await Promise.all([loadActivityScans(), loadNotifications()]);
    await waitForActivityScan(result.scan_id, button);
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; button.classList.remove("is-loading"); button.textContent = "Taramayı başlat"; }
}

const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

async function waitForActivityScan(scanId, button = null) {
  for (let attempt = 0; attempt < 600; attempt += 1) {
    const data = await api(`/api/activity-scans/${scanId}/results`);
    const scan = data.scan;
    if (button) button.textContent = scan.status === "running" ? "Mesajlar analiz ediliyor" : "Tarama başlatılıyor";
    if (["completed", "scheduled"].includes(scan.status)) {
      state.runningScanIds.delete(Number(scanId));
      await Promise.all([loadActivityScans(), loadNotifications()]);
      await openActivityResults(scanId);
      return scan;
    }
    if (["error", "paused", "waiting", "waiting_join", "waiting_budget"].includes(scan.status)) {
      state.runningScanIds.delete(Number(scanId));
      await loadActivityScans();
      throw new Error(errorMessage(
        scan.last_error,
        `Tarama ${badge(scan.status).replace(/<[^>]+>/g, "")} durumunda bekliyor.`,
      ));
    }
    if (attempt % 2 === 0) await loadActivityScans();
    await delay(1000);
  }
  state.runningScanIds.delete(Number(scanId));
  throw new Error("Tarama beklenenden uzun sürdü. Durumu Aktivite tablosundan takip edin.");
}

async function activityAction(scanId, action, button = null) {
  try {
    await api(`/api/activity-scans/${scanId}/${action}`, {method:"POST"});
    if (action === "run" || action === "resume") {
      state.runningScanIds.add(Number(scanId));
      if (button) { button.disabled = true; button.classList.add("is-loading"); button.textContent = "Analiz ediliyor"; }
      toast("Tarama doğrudan çalıştırıldı.");
      await loadActivityScans();
      await waitForActivityScan(scanId, button);
    } else {
      state.runningScanIds.delete(Number(scanId));
      toast("Tarama duraklatıldı.");
      await loadActivityScans();
    }
  } catch (error) { toast(error.message); }
  finally { if (button) { button.disabled = false; button.classList.remove("is-loading"); } }
}

async function deleteActivityScan(scanId, button = null) {
  const scan = state.activityScans.find(item => Number(item.id) === Number(scanId));
  if (!scan) return toast("Silinecek tarama bulunamadı; listeyi yenileyin.");
  const confirmed = window.confirm(
    `"${scan.name}" taraması ve bu taramaya ait ${scan.unique_users || 0} kullanıcı sonucu silinecek. Bu işlem geri alınamaz. Devam edilsin mi?`
  );
  if (!confirmed) return;
  if (button) { button.disabled = true; button.classList.add("is-loading"); button.textContent = "Siliniyor"; }
  try {
    const result = await api(`/api/activity-scans/${scanId}`, {method:"DELETE"});
    state.runningScanIds.delete(Number(scanId));
    if (Number(state.currentActivityScanId) === Number(scanId)) {
      state.currentActivityScanId = null;
      $("#activity-results-modal").classList.add("hidden");
    }
    toast(`${scan.name} silindi. Benzersiz aktif kullanıcı toplamı: ${result.unique_active_users}.`);
    await Promise.all([loadActivityScans(), loadNotifications()]);
  } catch (error) {
    toast(error.message);
  } finally {
    if (button) { button.disabled = false; button.classList.remove("is-loading"); button.textContent = "Sil"; }
  }
}

async function openActivityResults(scanId) {
  try {
    const data = await api(`/api/activity-scans/${scanId}/results`);
    const scan = data.scan;
    state.currentActivityScanId = Number(scanId);
    $("#activity-results-name").textContent = `${scan.name} · ${activityWindowLabel(scan.window_hours)} · ${scan.group_title || scan.group_ref}`;
    $("#activity-results-csv").href = `/api/activity-scans/${scanId}/report.csv`;
    $("#activity-results-summary").innerHTML = [
      ["Taranan mesaj", scan.message_count || 0, "blue"],
      ["Aktif kullanıcı", scan.unique_users || 0, "green"],
      ["Zaman aralığı", activityWindowLabel(scan.window_hours), ""],
      ["Durum", badge(scan.status), ""],
    ].map(item => `<article><small>${item[0]}</small><strong class="${item[2]}">${item[1]}</strong></article>`).join("");
    $("#activity-results-table").innerHTML = data.items.length ? `
      <table><thead><tr><th>Kullanıcı</th><th>Telegram ID</th><th>Kullanıcı adı</th><th>Mesaj</th><th>Son mesaj</th></tr></thead><tbody>
      ${data.items.map(item => `<tr><td>${escapeHtml(item.display_name)}</td><td class="mono">${item.telegram_user_id}</td><td class="mono">${item.username ? "@"+escapeHtml(item.username) : "—"}</td><td>${item.message_count}</td><td>${new Date(item.last_message_at).toLocaleString("tr-TR")}</td></tr>`).join("")}
      </tbody></table>` : emptyTable("Sonuç bulunamadı", "Seçilen zaman aralığında mesaj atan uygun kullanıcı yok.");
    $("#open-activity-transfer").disabled = !data.items.length || !["completed", "scheduled"].includes(scan.status);
    $("#activity-transfer-source").textContent = `${scan.group_title || scan.group_ref} grubunda ${scan.unique_users || 0} aktif kullanıcı bulundu.`;
    $("#activity-transfer-max").value = Math.min(Math.max(Number(scan.unique_users) || 100, 1), 1000);
    openModal("#activity-results-modal");
  } catch (error) { toast(error.message); }
}

function openActivityTransfer() {
  if (!state.currentActivityScanId) return toast("Önce tamamlanmış bir tarama sonucu açın.");
  $("#activity-transfer-target").value = "";
  $("#activity-transfer-message").classList.add("hidden");
  openModal("#activity-transfer-modal");
  setTimeout(() => $("#activity-transfer-target").focus(), 50);
}

async function prepareActivityTransfer() {
  if (!state.currentActivityScanId) return;
  const button = $("#prepare-activity-transfer");
  button.disabled = true;
  button.classList.add("is-loading");
  button.textContent = "Grup doğrulanıyor ve adaylar hazırlanıyor";
  showMessage("#activity-transfer-message", "Hedef grup bulunuyor, aktif üyeler uygunluk kontrolünden geçiriliyor…");
  try {
    const payload = {
      target_ref: $("#activity-transfer-target").value.trim(),
      max_users: Number($("#activity-transfer-max").value),
      daily_limit: Number($("#activity-transfer-daily").value),
      min_delay_seconds: Number($("#activity-transfer-min-delay").value),
      max_delay_seconds: Number($("#activity-transfer-max-delay").value),
    };
    const result = await api(`/api/activity-scans/${state.currentActivityScanId}/prepare-transfer`, {method:"POST", body:JSON.stringify(payload)});
    await loadJobs();
    $("#activity-transfer-modal").classList.add("hidden");
    $("#activity-results-modal").classList.add("hidden");
    await openCandidateResults(result.job_id, result.summary);
    if (result.selected_count > 0 && result.summary?.permissions?.can_invite_users) {
      showMessage("#candidate-message", `${result.selected_count} uygun aday hazırlandı. Seçimi kontrol edip “Seçimi onayla” düğmesine basın.`, true);
    } else {
      showMessage("#candidate-message", result.selected_count ? "Hedef grupta üye ekleme yetkisi bulunamadı." : "Geçerli Telegram kullanıcı referansı olan uygun üye bulunamadı. Önizlemeyi yeniden çalıştırın.");
    }
    await loadNotifications();
  } catch (error) {
    showMessage("#activity-transfer-message", error.message);
  } finally {
    button.disabled = false;
    button.classList.remove("is-loading");
    button.textContent = "Grubu bul ve adayları hazırla";
  }
}

function openModal(selector) { $(selector).classList.remove("hidden"); }

function resetLoginFlowState() {
  state.loginPhone = "";
  $("#login-step-phone").classList.remove("hidden");
  $("#login-step-code").classList.add("hidden");
  $("#login-phone").value = "";
  $("#login-code").value = "";
  $("#login-password").value = "";
  $("#login-message").textContent = "";
  $("#login-message").classList.add("hidden");
  $("#login-message").classList.remove("success");
  $("#request-code").disabled = false;
  $("#request-code").classList.remove("is-loading");
  $("#request-code").textContent = "Doğrulama kodu iste";
  $("#verify-code").disabled = false;
  $("#verify-code").classList.remove("is-loading");
  $("#verify-code").textContent = "Hesabı bağla";
}

function closeModals({ keepLoginState = false } = {}) {
  const pendingPhone = state.loginPhone;
  if (!keepLoginState && pendingPhone) {
    runUi(api("/api/sessions/login/pending", {
      method:"DELETE",
      body:JSON.stringify({phone:pendingPhone}),
    }), {silent:true});
  }
  if (!keepLoginState) resetLoginFlowState();
  $$(".modal-backdrop").forEach(item => item.classList.add("hidden"));
}

async function openSessionModalFresh() {
  const pendingPhone = state.loginPhone;
  if (pendingPhone) {
    await api("/api/sessions/login/pending", {
      method:"DELETE",
      body:JSON.stringify({phone:pendingPhone}),
    });
  }
  resetLoginFlowState();
  await loadDefaultLoginProxy();
  openModal("#session-modal");
}

async function loadDefaultLoginProxy() {
  const proxy = await api("/api/sessions/login/default-proxy");
  state.defaultLoginProxy = proxy;
  if (!proxy.configured) {
    syncLoginProxyMode();
    return;
  }
  $("#login-proxy-type").value = proxy.proxy_type || "socks5";
  $("#login-proxy-host").value = proxy.host || "";
  $("#login-proxy-port").value = proxy.port || "";
  $("#login-proxy-username").value = proxy.username || "";
  $("#login-proxy-password").value = "";
  $("#login-proxy-password").placeholder = proxy.password_configured
    ? "Kayıtlı parola güvenli biçimde kullanılacak"
    : "Proxy parolası";
  $("#login-proxy-memory").textContent = "Kayıtlı varsayılan proxy otomatik kullanılacak. Değiştirirseniz başarılı bağlantıdan sonra yeni proxy varsayılan olur.";
  syncLoginProxyMode();
}

function syncLoginProxyMode() {
  const useProxy = $("#login-use-proxy").checked;
  ["#login-proxy-type", "#login-proxy-host", "#login-proxy-port", "#login-proxy-username", "#login-proxy-password"].forEach(selector => {
    $(selector).disabled = !useProxy;
  });
  if (!useProxy) {
    $("#login-proxy-memory").textContent = "Proxy kullanılmadan yalnızca hesap girişi yapılacak. Bu hesap, Ayarlar'dan proxy atanmadan tarama veya üye ekleme işlemlerinde çalıştırılmaz.";
  } else if (state.defaultLoginProxy?.configured) {
    $("#login-proxy-memory").textContent = "Kayıtlı varsayılan proxy otomatik kullanılacak. Değiştirirseniz başarılı bağlantıdan sonra yeni proxy varsayılan olur.";
  } else {
    $("#login-proxy-memory").textContent = "Tek seferlik ayar: İlk başarılı proxy şifreli olarak kaydedilir ve sonraki telefonlarda otomatik kullanılır.";
  }
}

async function requestCode() {
  const phone = $("#login-phone").value.trim();
  const label = $("#login-label").value.trim();
  const useProxy = $("#login-use-proxy").checked;
  const proxyHost = $("#login-proxy-host").value.trim();
  const proxyPort = Number($("#login-proxy-port").value);
  if (useProxy && (!proxyHost || !proxyPort)) return showMessage("#login-message", "Proxy kullanacaksanız çalışan proxy IP/sunucu ve portunu girin.");
  const button = $("#request-code");
  button.disabled = true;
  button.classList.add("is-loading");
  button.textContent = "Bağlanıyor";
  showMessage("#login-message", useProxy ? "Telegram bağlantısı proxy üzerinden kuruluyor…" : "Telegram bağlantısı doğrudan kuruluyor…");
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 50000);
    try {
      await api("/api/sessions/login/start", { method:"POST", signal:controller.signal, body:JSON.stringify({
        phone,
        label,
        use_proxy:useProxy,
        proxy_type:$("#login-proxy-type").value,
        proxy_host:useProxy ? proxyHost : null,
        proxy_port:useProxy ? proxyPort : null,
        proxy_username:useProxy ? ($("#login-proxy-username").value.trim() || null) : null,
        proxy_password:useProxy ? ($("#login-proxy-password").value || null) : null,
      }) });
    } catch (error) {
      if (error.name === "AbortError") throw new Error("Telegram bağlantısı 50 saniyede tamamlanmadı. Proxyyi veya doğrudan internet bağlantısını kontrol edin.");
      throw error;
    } finally { clearTimeout(timeout); }
    await loadDefaultLoginProxy();
    state.loginPhone = phone;
    $("#login-step-phone").classList.add("hidden");
    $("#login-step-code").classList.remove("hidden");
    showMessage("#login-message", "Kod gönderildi. Telegram uygulamanızı kontrol edin.", true);
  } catch (error) { showMessage("#login-message", error.message); }
  finally {
    button.disabled = false;
    button.classList.remove("is-loading");
    button.textContent = "Doğrulama kodu iste";
  }
}

async function verifyCode() {
  const button = $("#verify-code");
  button.disabled = true;
  button.classList.add("is-loading");
  button.textContent = "Doğrulanıyor";
  showMessage("#login-message", "Kod doğrulanıyor…");
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 45000);
    let result;
    try {
      result = await api("/api/sessions/login/verify", { method:"POST", signal:controller.signal, body:JSON.stringify({
        phone:state.loginPhone, code:$("#login-code").value.trim(), password:$("#login-password").value || null,
      }) });
    } catch (error) {
      if (error.name === "AbortError") throw new Error("Telegram doğrulaması 45 saniyede tamamlanmadı. Yeniden deneyin.");
      throw error;
    } finally { clearTimeout(timeout); }
    if (result.password_required) {
      showMessage("#login-message", result.message);
      return;
    }
    resetLoginFlowState();
    closeModals({keepLoginState:true});
    toast("Telegram hesabı başarıyla bağlandı.");
    await refreshAll();
  } catch (error) { showMessage("#login-message", error.message); }
  finally {
    button.disabled = false;
    button.classList.remove("is-loading");
    button.textContent = "Hesabı bağla";
  }
}

async function createJob() {
  showMessage("#job-message", "Gruplar doğrulanıyor ve iş oluşturuluyor…");
  try {
    const payload = {
      name:$("#job-name").value.trim(), session_id:Number($("#job-session").value),
      source_ref:$("#job-source").value.trim(), target_ref:$("#job-target").value.trim(),
      max_users:Number($("#job-max").value), min_delay_seconds:Number($("#job-min-delay").value),
      max_delay_seconds:Number($("#job-max-delay").value), daily_limit:Number($("#job-daily").value),
      dry_run:$("#job-dry-run").checked,
      scheduled_at:$("#job-scheduled").value ? new Date($("#job-scheduled").value).toISOString() : null,
      working_start:$("#job-working-start").value || "09:00",
      working_end:$("#job-working-end").value || "22:00",
    };
    const result = await api("/api/jobs", { method:"POST", body:JSON.stringify(payload) });
    showMessage("#job-message", `JOB-${String(result.job_id).padStart(4,"0")} oluşturuldu.`, true);
    setTimeout(closeModals, 850);
    await refreshAll();
  } catch (error) { showMessage("#job-message", error.message); }
}

const SETTINGS_FIELD_GROUPS = {
  rotation:["#rotation-daily-quota"],
  telegram:["#settings-api-id", "#settings-api-hash"],
  defaultProxy:["#default-proxy-type", "#default-proxy-host", "#default-proxy-port", "#default-proxy-username", "#default-proxy-password"],
  sessionProxy:["#proxy-enabled", "#proxy-type", "#proxy-host", "#proxy-port", "#proxy-username", "#proxy-password"],
  invitePolicy:["#invite-batch-limit", "#invite-cooldown-minutes"],
};
const SETTINGS_TRACKED_SELECTORS = Object.values(SETTINGS_FIELD_GROUPS).flat();
const settingsBaseline = new Map();
let settingsLoading = false;

function settingsControlValue(element) {
  return element.type === "checkbox" ? String(element.checked) : element.value;
}

function markSettingsFieldsSaved(selectors = SETTINGS_TRACKED_SELECTORS) {
  selectors.forEach(selector => {
    const element = $(selector);
    if (element) settingsBaseline.set(selector, settingsControlValue(element));
  });
  renderSettingsUnsavedWarning();
}

function settingsChangedSelectors() {
  if (!settingsBaseline.size) return [];
  return SETTINGS_TRACKED_SELECTORS.filter(selector => {
    const element = $(selector);
    return element && settingsBaseline.has(selector) && settingsControlValue(element) !== settingsBaseline.get(selector);
  });
}

function settingsHasUnsavedChanges() {
  return settingsChangedSelectors().length > 0;
}

function renderSettingsUnsavedWarning() {
  const warning = $("#settings-unsaved-warning");
  if (!warning) return;
  const changed = settingsChangedSelectors();
  warning.classList.toggle("hidden", !changed.length);
  if (changed.length) warning.querySelector("strong").textContent = `${changed.length} kaydedilmemiş değişiklik var.`;
}

function settingValidationMessage(element) {
  const value = element.value.trim();
  const numericValue = Number(value);
  if (element.id === "rotation-daily-quota" && (!Number.isInteger(numericValue) || numericValue < 1 || numericValue > 1000)) return "Kota 1-1000 arasında tam sayı olmalı.";
  if (element.id === "settings-api-id" && value && (!Number.isInteger(numericValue) || numericValue < 1)) return "API ID pozitif tam sayı olmalı.";
  if (element.id === "settings-api-hash" && value && !/^[a-f0-9]{32}$/i.test(value)) return "API Hash 32 karakterli hexadecimal değer olmalı.";
  if (["default-proxy-port", "proxy-port"].includes(element.id) && value && (!Number.isInteger(numericValue) || numericValue < 1 || numericValue > 65535)) return "Port 1-65535 arasında olmalı.";
  if (element.id === "default-proxy-host" && !value && $("#default-proxy-port").value) return "Port girildiyse proxy sunucusu da zorunludur.";
  if (element.id === "default-proxy-port" && $("#default-proxy-host").value.trim() && !value) return "Proxy sunucusu girildiyse port zorunludur.";
  if (element.id === "proxy-host" && $("#proxy-enabled").checked && !value) return "Proxy etkinse sunucu zorunludur.";
  if (element.id === "proxy-port" && $("#proxy-enabled").checked && !value) return "Proxy etkinse port zorunludur.";
  if (element.id === "invite-batch-limit" && (!Number.isInteger(numericValue) || numericValue < 1 || numericValue > 20)) return "Ekleme limiti 1-20 arasında olmalı.";
  if (element.id === "invite-cooldown-minutes" && (!Number.isInteger(numericValue) || numericValue < 5 || numericValue > 240)) return "Dinlenme süresi 5-240 dakika arasında olmalı.";
  return "";
}

function renderSettingFieldValidation(element) {
  const message = settingValidationMessage(element);
  element.classList.toggle("settings-field-invalid", Boolean(message));
  let note = element.parentElement?.querySelector(".settings-field-error");
  if (message && !note) {
    note = document.createElement("small");
    note.className = "settings-field-error";
    element.insertAdjacentElement("afterend", note);
  }
  if (note) {
    note.textContent = message;
    note.classList.toggle("hidden", !message);
  }
  return message;
}

function validateSettingsControls() {
  const errors = SETTINGS_TRACKED_SELECTORS.map(selector => $(selector)).filter(Boolean).map(renderSettingFieldValidation).filter(Boolean);
  const summary = $("#settings-validation-summary");
  if (summary) {
    summary.classList.toggle("hidden", !errors.length);
    summary.textContent = errors.length ? `${errors.length} alan düzeltilmeden ilgili ayar kaydedilemez.` : "";
  }
  return errors;
}

function filterSettingsSections() {
  const query = ($("#settings-search")?.value || "").trim().toLocaleLowerCase("tr-TR");
  const sections = $$('[data-settings-section]');
  let visible = 0;
  sections.forEach(section => {
    const searchable = `${section.dataset.settingsKeywords || ""} ${section.textContent || ""}`.toLocaleLowerCase("tr-TR");
    const matches = !query || searchable.includes(query);
    section.classList.toggle("settings-section-hidden", !matches);
    if (matches) visible += 1;
  });
  $("#settings-search-count").textContent = query ? `${visible} / ${sections.length} bölüm gösteriliyor` : "Tüm bölümler gösteriliyor";
}

function renderSettingsOverview(overview) {
  state.settingsOverview = overview;
  const apiReady = Boolean(overview.configuration.telegram_api_configured);
  $("#settings-health-api").textContent = apiReady ? "Hazır" : "Kurulum gerekli";
  $("#settings-health-api").className = apiReady ? "green" : "red";
  $("#settings-health-api-note").textContent = apiReady ? `${overview.configuration.telegram_api_source === "environment" ? "Sunucu" : "Panel"} tarafından yapılandırıldı` : "API ID ve API Hash eksik";
  $("#settings-health-proxy").textContent = `${overview.sessions.proxy_healthy} / ${overview.sessions.total}`;
  $("#settings-health-proxy").className = overview.sessions.proxy_attention ? "yellow" : "green";
  $("#settings-health-proxy-note").textContent = overview.sessions.proxy_attention ? `${overview.sessions.proxy_attention} session kontrol bekliyor` : "Tüm session proxyleri sağlıklı";
  const backup = overview.backup;
  $("#settings-health-backup").textContent = backup.latest_created_at ? formatDateTime(backup.latest_created_at) : "Yedek yok";
  $("#settings-health-backup").className = backup.latest_created_at && Number(backup.age_hours || 0) <= 168 ? "green" : "yellow";
  $("#settings-health-backup-note").textContent = backup.latest_created_at ? `${backup.age_hours} saat önce · ${backup.count} yedek` : "İlk güvenli yedeği oluşturun";
  $("#settings-health-version").textContent = overview.app.version;
  $("#settings-health-version-note").textContent = `${overview.update.channel} kanal · ${overview.app.environment}`;
  $("#settings-current-version").textContent = overview.app.version;
}

async function loadSettingsOverview() {
  const overview = await api("/api/settings/overview");
  renderSettingsOverview(overview);
  return overview;
}

async function checkSettingsUpdate() {
  const button = $("#check-settings-update");
  button.disabled = true;
  button.classList.add("is-loading");
  button.textContent = "Kontrol ediliyor";
  $("#settings-update-message").textContent = "Resmi imzalı manifest okunuyor…";
  try {
    const status = await api("/api/settings/update-status");
    $("#settings-latest-version").textContent = status.latest_version || "Yayın bulunamadı";
    $("#settings-update-checked").textContent = formatDateTime(status.checked_at);
    $("#settings-update-message").textContent = status.message;
    $("#settings-update-message").classList.toggle("success", status.reachable && !status.update_available);
    $("#settings-update-message").classList.toggle("error", !status.reachable);
  } catch (error) {
    $("#settings-update-message").textContent = error.message;
    $("#settings-update-message").classList.add("error");
  } finally {
    button.disabled = false;
    button.classList.remove("is-loading");
    button.textContent = "Güncellemeyi kontrol et";
  }
}

function renderReleaseHistory() {
  const container = $("#release-history-list");
  if (!container || !state.releaseNotes) return;
  const history = state.releaseNotes.history || [];
  container.innerHTML = history.length ? history.map(item => `
    <article class="release-history-item">
      <div><strong>Pawgram ${escapeHtml(item.version)}</strong><small>${escapeHtml(item.release_date)} · ${escapeHtml(item.title)}</small></div>
      <ul>${(item.changes || []).map(change => `<li>${escapeHtml(change)}</li>`).join("")}</ul>
    </article>`).join("") : emptyTable("Sürüm geçmişi bulunamadı", "Yayın notları yeni sürümlerle birlikte burada gösterilir.");
}

async function loadReleaseNotes({showPending = false} = {}) {
  state.releaseNotes = await api("/api/release-notes");
  renderReleaseHistory();
  if (!showPending || !state.releaseNotes.pending_version || !state.releaseNotes.current) return;
  const current = state.releaseNotes.current;
  $("#release-notes-title").textContent = `Pawgram ${current.version}`;
  $("#release-notes-date").textContent = `${current.release_date} · ${current.title}`;
  $("#release-notes-changes").innerHTML = (current.changes || []).map(change => `<div><span>✓</span><strong>${escapeHtml(change)}</strong></div>`).join("");
  openModal("#release-notes-modal");
}

async function acknowledgeReleaseNotes() {
  const version = state.releaseNotes?.pending_version;
  if (!version) return closeModals();
  const button = $("#acknowledge-release-notes");
  button.disabled = true;
  try {
    state.releaseNotes = await api(`/api/release-notes/${encodeURIComponent(version)}/acknowledge`, {method:"POST"});
    closeModals();
    renderReleaseHistory();
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

async function loadSettingsPage() {
  settingsLoading = true;
  try {
    await Promise.all([loadSettingsOverview(), loadBackups(), loadTelegramSettings(), loadRotationSettings(), loadDefaultProxySettings(), loadProxySettings(), loadInvitePolicy(), loadLicenseStatus(), loadReleaseNotes()]);
    markSettingsFieldsSaved();
    validateSettingsControls();
    filterSettingsSections();
  } finally {
    settingsLoading = false;
  }
}

async function loadTelegramSettings() {
  state.telegramSettings = await api("/api/settings/telegram");
  const configured = state.telegramSettings.configured;
  const sourceLabel = state.telegramSettings.source === "environment" ? "Sunucu ayarları" : "Panel";
  $("#api-config-state").innerHTML = configured
    ? `<span class="badge active">Bağlantıya hazır</span> API bilgileri ${sourceLabel.toLowerCase()} üzerinden yapılandırıldı.`
    : `<span class="badge warning">Kurulum gerekli</span> Telefon eklemeden önce API bilgilerini kaydedin.`;
  $("#settings-api-id").value = state.telegramSettings.api_id || "";
  $("#settings-api-hash").value = "";
  $("#settings-api-hash").placeholder = configured ? state.telegramSettings.api_hash_masked : "32 karakterli API Hash";
  const locked = state.telegramSettings.source === "environment";
  $("#settings-api-id").disabled = locked;
  $("#settings-api-hash").disabled = locked;
  $("#save-api-settings").disabled = locked;
  $("#save-api-settings").textContent = locked ? "Sunucu tarafından yönetiliyor" : configured ? "API bilgilerini güncelle" : "API bilgilerini kaydet";
  markSettingsFieldsSaved(SETTINGS_FIELD_GROUPS.telegram);
}

async function saveTelegramSettings() {
  showMessage("#api-settings-message", "API bilgileri güvenli biçimde kaydediliyor…");
  try {
    const validationErrors = SETTINGS_FIELD_GROUPS.telegram.map(selector => renderSettingFieldValidation($(selector))).filter(Boolean);
    if (validationErrors.length) throw new Error(validationErrors[0]);
    const apiId = Number($("#settings-api-id").value);
    const apiHash = $("#settings-api-hash").value.trim();
    if (!apiId || !apiHash) throw new Error("API ID ve API Hash alanlarını doldurun.");
    await api("/api/settings/telegram", {
      method:"POST",
      body:JSON.stringify({api_id:apiId, api_hash:apiHash}),
    });
    showMessage("#api-settings-message", "Telegram API bilgileri şifrelenerek kaydedildi.", true);
    await loadTelegramSettings();
    state.health = await api("/api/health");
    await loadSettingsOverview();
    toast("Telegram API bağlantısı yapılandırıldı.");
  } catch (error) { showMessage("#api-settings-message", error.message); }
}

async function loadRotationSettings() {
  state.rotationSettings = await api("/api/settings/rotation");
  $("#rotation-daily-quota").value = state.rotationSettings.daily_quota;
  markSettingsFieldsSaved(SETTINGS_FIELD_GROUPS.rotation);
}

async function saveRotationSettings() {
  showMessage("#rotation-settings-message", "Round-Robin kotası kaydediliyor…");
  try {
    const validationMessage = renderSettingFieldValidation($("#rotation-daily-quota"));
    if (validationMessage) throw new Error(validationMessage);
    const dailyQuota = Number($("#rotation-daily-quota").value);
    if (!Number.isInteger(dailyQuota) || dailyQuota < 1 || dailyQuota > 1000) {
      throw new Error("Günlük kota 1 ile 1000 arasında tam sayı olmalı.");
    }
    await api("/api/settings/rotation", {
      method:"POST",
      body:JSON.stringify({daily_quota:dailyQuota}),
    });
    showMessage("#rotation-settings-message", `Her session için günlük kota ${dailyQuota} olarak kaydedildi.`, true);
    await Promise.all([loadRotationSettings(), loadSettingsOverview()]);
  } catch (error) { showMessage("#rotation-settings-message", error.message); }
}

function setProxyStatus(message, kind = "") {
  const element = $("#proxy-status");
  element.textContent = message;
  element.classList.remove("success", "error");
  if (kind) element.classList.add(kind);
}

function setDefaultProxyStatus(message, kind = "") {
  const element = $("#default-proxy-status");
  element.textContent = message;
  element.classList.remove("success", "error");
  if (kind) element.classList.add(kind);
}

async function loadDefaultProxySettings() {
  try {
    const config = await api("/api/settings/default-proxy");
    if (!config.configured) {
      $("#default-proxy-host").value = "";
      $("#default-proxy-port").value = "";
      $("#default-proxy-username").value = "";
      $("#default-proxy-password").value = "";
      setDefaultProxyStatus("Henüz Pawgram varsayılan proxy ayarlanmamış.", "error");
      markSettingsFieldsSaved(SETTINGS_FIELD_GROUPS.defaultProxy);
      return;
    }
    $("#default-proxy-type").value = config.proxy_type || "socks5";
    $("#default-proxy-host").value = config.host || "";
    $("#default-proxy-port").value = config.port || "";
    $("#default-proxy-username").value = config.username || "";
    $("#default-proxy-password").value = "";
    $("#default-proxy-password").placeholder = config.password_configured ? "Mevcut parola şifreli kayıtlı" : "Proxy parolası";
    setDefaultProxyStatus("Varsayılan proxy aktif; yeni telefon ekleme ekranına otomatik uygulanacak.", "success");
    markSettingsFieldsSaved(SETTINGS_FIELD_GROUPS.defaultProxy);
  } catch (error) { setDefaultProxyStatus(error.message, "error"); }
}

async function saveDefaultProxySettings(showSuccess = true) {
  const validationErrors = SETTINGS_FIELD_GROUPS.defaultProxy.map(selector => renderSettingFieldValidation($(selector))).filter(Boolean);
  if (validationErrors.length) throw new Error(validationErrors[0]);
  const payload = {
    proxy_type: $("#default-proxy-type").value,
    host: $("#default-proxy-host").value.trim(),
    port: Number($("#default-proxy-port").value),
    username: $("#default-proxy-username").value.trim() || null,
    password: $("#default-proxy-password").value || null,
  };
  if (!payload.host || !payload.port) throw new Error("Varsayılan proxy için sunucu ve port zorunludur.");
  await api("/api/settings/default-proxy", {method:"PUT", body:JSON.stringify(payload)});
  await Promise.all([loadDefaultProxySettings(), loadDefaultLoginProxy(), loadSettingsOverview()]);
  if (showSuccess) setDefaultProxyStatus("Varsayılan proxy şifrelenerek kaydedildi.", "success");
}

async function testDefaultProxyConnection() {
  const button = $("#test-default-proxy");
  const saveButton = $("#save-default-proxy");
  const originalText = button.textContent;
  button.disabled = true;
  saveButton.disabled = true;
  button.classList.add("is-loading");
  button.textContent = "Test ediliyor";
  setDefaultProxyStatus("Varsayılan proxy üzerinden gerçek Telegram bağlantısı test ediliyor…");
  try {
    await saveDefaultProxySettings(false);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 40000);
    let result;
    try {
      result = await api("/api/settings/default-proxy/test", {method:"POST", signal:controller.signal});
    } catch (error) {
      if (error.name === "AbortError") throw new Error("Proxy testi 40 saniyede tamamlanmadı.");
      throw error;
    } finally { clearTimeout(timeout); }
    $("#default-proxy-type").value = result.proxy_type || $("#default-proxy-type").value;
    setDefaultProxyStatus(`Telegram bağlantısı başarılı · ${result.latency_ms} ms · ${result.proxy_type.toUpperCase()}`, "success");
  } catch (error) { setDefaultProxyStatus(error.message, "error"); }
  finally {
    button.disabled = false;
    saveButton.disabled = false;
    button.classList.remove("is-loading");
    button.textContent = originalText;
  }
}

async function loadProxySettings() {
  const sessionId = Number($("#proxy-session").value);
  const controls = ["#proxy-enabled", "#proxy-type", "#proxy-host", "#proxy-port", "#proxy-username", "#proxy-password", "#save-proxy-settings", "#test-proxy", "#delete-proxy"];
  controls.forEach(selector => $(selector).disabled = !sessionId);
  if (!sessionId) {
    setProxyStatus("Proxy yapılandırmak için önce bir Telegram hesabı ekleyin.");
    markSettingsFieldsSaved(SETTINGS_FIELD_GROUPS.sessionProxy);
    return;
  }
  try {
    const config = await api(`/api/sessions/${sessionId}/proxy`);
    $("#proxy-enabled").checked = config.enabled;
    $("#proxy-type").value = config.proxy_type || "socks5";
    $("#proxy-host").value = config.host || "";
    $("#proxy-port").value = config.port || "";
    $("#proxy-username").value = config.username || "";
    $("#proxy-password").value = "";
    $("#proxy-password").placeholder = config.password_configured ? "Mevcut parola kayıtlı" : "Proxy parolası";
    if (config.last_status === "success") setProxyStatus(`Son test başarılı · ${config.latency_ms} ms`, "success");
    else if (config.last_status === "failed") setProxyStatus(`Son test başarısız · ${config.last_error || "Bağlantı kurulamadı"}`, "error");
    else setProxyStatus(config.enabled ? "Proxy etkin; hesap işe başlamadan önce otomatik test edilecek." : "Proxy kapalı: bu session güvenlik gereği çalıştırılmaz.", config.enabled ? "" : "error");
    markSettingsFieldsSaved(SETTINGS_FIELD_GROUPS.sessionProxy);
  } catch (error) { setProxyStatus(error.message, "error"); }
}

function setInvitePolicyStatus(message, kind = "") {
  const element = $("#invite-policy-status");
  element.textContent = message;
  element.classList.remove("success", "error");
  if (kind) element.classList.add(kind);
}

async function loadInvitePolicy() {
  const sessionId = Number($("#proxy-session").value);
  ["#invite-batch-limit", "#invite-cooldown-minutes", "#save-invite-policy"].forEach(selector => {
    $(selector).disabled = !sessionId;
  });
  if (!sessionId) {
    setInvitePolicyStatus("Otomatik devir limiti belirlemek için bir Telegram session seçin.");
    markSettingsFieldsSaved(SETTINGS_FIELD_GROUPS.invitePolicy);
    return;
  }
  try {
    const policy = await api(`/api/sessions/${sessionId}/invite-policy`);
    $("#invite-batch-limit").value = policy.batch_limit || 3;
    $("#invite-cooldown-minutes").value = policy.cooldown_minutes || 20;
    setInvitePolicyStatus(
      `${policy.batch_success_count}/${policy.batch_limit} başarılı ekleme · Limit dolunca sıradaki sağlıklı session'a otomatik geçilecek · Mevcut tarama tekrar edilmeyecek.`,
      "success",
    );
    markSettingsFieldsSaved(SETTINGS_FIELD_GROUPS.invitePolicy);
  } catch (error) { setInvitePolicyStatus(error.message, "error"); }
}

async function saveInvitePolicy() {
  const sessionId = Number($("#proxy-session").value);
  if (!sessionId) return setInvitePolicyStatus("Bir Telegram session seçin.", "error");
  const batchLimit = Number($("#invite-batch-limit").value);
  const cooldownMinutes = Number($("#invite-cooldown-minutes").value);
  if (batchLimit < 1 || batchLimit > 20) return setInvitePolicyStatus("X limiti 1-20 arasında olmalı.", "error");
  if (cooldownMinutes < 5 || cooldownMinutes > 240) return setInvitePolicyStatus("Dinlenme süresi 5-240 dakika arasında olmalı.", "error");
  const button = $("#save-invite-policy");
  button.disabled = true;
  try {
    await api(`/api/sessions/${sessionId}/invite-policy`, {
      method:"PUT",
      body:JSON.stringify({batch_limit:batchLimit, cooldown_minutes:cooldownMinutes}),
    });
    await Promise.all([loadInvitePolicy(), loadSessions()]);
    setInvitePolicyStatus(`Session #${sessionId}: ${batchLimit} başarılı eklemeden sonra otomatik devir, ${cooldownMinutes} dakika dinlenme kaydedildi.`, "success");
  } catch (error) { setInvitePolicyStatus(error.message, "error"); }
  finally { button.disabled = false; }
}

async function saveProxySettings(showSuccess = true) {
  const sessionId = Number($("#proxy-session").value);
  if (!sessionId) throw new Error("Bir Telegram session seçin.");
  const validationErrors = SETTINGS_FIELD_GROUPS.sessionProxy.map(selector => renderSettingFieldValidation($(selector))).filter(Boolean);
  if (validationErrors.length) throw new Error(validationErrors[0]);
  const payload = {
    enabled: $("#proxy-enabled").checked,
    proxy_type: $("#proxy-type").value,
    host: $("#proxy-host").value.trim() || null,
    port: Number($("#proxy-port").value) || null,
    username: $("#proxy-username").value.trim() || null,
    password: $("#proxy-password").value || null,
  };
  if (payload.enabled && (!payload.host || !payload.port)) throw new Error("Proxy etkinse sunucu ve port zorunludur.");
  await api(`/api/sessions/${sessionId}/proxy`, {method:"PUT", body:JSON.stringify(payload)});
  if (showSuccess) setProxyStatus("Proxy ayarları şifrelenerek kaydedildi.", "success");
  await Promise.all([loadSessions(), loadSettingsOverview()]);
  markSettingsFieldsSaved(SETTINGS_FIELD_GROUPS.sessionProxy);
  return sessionId;
}

async function testProxyConnection() {
  const button = $("#test-proxy");
  const saveButton = $("#save-proxy-settings");
  const originalText = button.textContent;
  button.disabled = true;
  saveButton.disabled = true;
  button.classList.add("is-loading");
  button.textContent = "Test ediliyor";
  setProxyStatus("Proxy üzerinden Telegram bağlantısı test ediliyor…");
  try {
    const sessionId = await saveProxySettings(false);
    if (!$("#proxy-enabled").checked) throw new Error("Test için önce proxy kullanımını etkinleştirin.");
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 40000);
    let result;
    try {
      result = await api(`/api/sessions/${sessionId}/proxy/test`, {method:"POST", signal:controller.signal});
    } catch (error) {
      if (error.name === "AbortError") throw new Error("Proxy testi 40 saniyede yanıt vermedi. Host, port, kullanıcı/parola ve proxy paketini kontrol edin.");
      throw error;
    } finally { clearTimeout(timeout); }
    $("#proxy-type").value = result.proxy_type || $("#proxy-type").value;
    const detected = result.auto_detected ? ` · ${result.proxy_type.toUpperCase()} otomatik algılandı` : ` · ${result.proxy_type.toUpperCase()}`;
    setProxyStatus(`Bağlantı başarılı · ${result.latency_ms} ms${detected}`, "success");
    await loadSessions();
  } catch (error) { setProxyStatus(error.message, "error"); }
  finally {
    button.disabled = false;
    saveButton.disabled = false;
    button.classList.remove("is-loading");
    button.textContent = originalText;
  }
}

async function deleteProxySettings() {
  const sessionId = Number($("#proxy-session").value);
  if (!sessionId) return setProxyStatus("Bir Telegram session seçin.", "error");
  if (!window.confirm("Bu hesaba kayıtlı proxy adresi ve şifreli kullanıcı/parola tamamen silinsin mi? Hesap yeni proxy eklenene kadar çalıştırılmayacak.")) return;
  const button = $("#delete-proxy");
  button.disabled = true;
  try {
    const result = await api(`/api/sessions/${sessionId}/proxy`, {method:"DELETE"});
    await Promise.all([loadSessions(), loadProxySettings(), loadSettingsOverview()]);
    setProxyStatus(result.message, "success");
  } catch (error) { setProxyStatus(error.message, "error"); }
  finally { button.disabled = false; }
}

function setBulkProxyStatus(message, kind = "") {
  const element = $("#bulk-proxy-status");
  element.textContent = message;
  element.classList.remove("success", "error");
  if (kind) element.classList.add(kind);
}

async function bulkAssignProxies() {
  const file = $("#bulk-proxy-file").files[0];
  if (!file) return setBulkProxyStatus("Önce proxy listesini içeren bir TXT dosyası seçin.", "error");
  const button = $("#bulk-assign-proxies");
  button.disabled = true;
  button.classList.add("is-loading");
  button.textContent = "Proxyler dağıtılıyor";
  setBulkProxyStatus("TXT okunuyor ve proxy bilgisi boş hesaplara dağıtılıyor…");
  try {
    const content = await file.text();
    const result = await api("/api/proxies/bulk-assign", {
      method:"POST",
      body:JSON.stringify({content, default_proxy_type:$("#bulk-proxy-type").value}),
    });
    const invalid = result.invalid_lines?.length || 0;
    const detail = invalid ? ` Geçersiz ${invalid} satır atlandı.` : "";
    setBulkProxyStatus(
      `${result.assigned_count} hesaba sabit proxy atandı. ${result.unused_proxy_count} proxy kullanılmadı, ${result.unassigned_session_count} hesap boş kaldı.${detail}`,
      "success",
    );
    await Promise.all([loadSessions(), loadProxySettings(), loadNotifications(), loadSettingsOverview()]);
  } catch (error) { setBulkProxyStatus(error.message, "error"); }
  finally {
    button.disabled = false;
    button.classList.remove("is-loading");
    button.textContent = "Toplu Proxy Ekle";
  }
}

async function loadNotifications() {
  const data = await api("/api/notifications");
  $("#notification-count").textContent = data.unread;
  $("#notification-count").classList.toggle("hidden", data.unread === 0);
  $("#notification-list").innerHTML = data.items.length ? data.items.map(item => `
    <div class="notification-item ${escapeHtml(item.level)}">
      <i></i><div><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.message)}</small><time>${new Date(item.created_at).toLocaleString("tr-TR")}</time></div>
    </div>`).join("") : emptyTable("Bildirim yok", "Yeni sistem olayları burada görünecek.");
}

async function openNotifications() {
  try {
    await loadNotifications();
    $("#notification-drawer").classList.remove("hidden");
    await api("/api/notifications/read", {method:"POST"});
    $("#notification-count").classList.add("hidden");
  } catch (error) { toast(error.message); }
}

async function loadBackups() {
  const backups = await api("/api/backups");
  $("#backup-list").innerHTML = backups.length ? `
    <table><thead><tr><th>Dosya</th><th>Boyut</th><th>Oluşturulma</th><th></th></tr></thead><tbody>
    ${backups.map(item => `<tr><td class="mono">${escapeHtml(item.name)}</td><td>${Math.max(1, Math.round(item.size / 1024))} KB</td><td>${new Date(item.created_at).toLocaleString("tr-TR")}</td><td><a href="/api/backups/${encodeURIComponent(item.name)}">İndir</a></td></tr>`).join("")}
    </tbody></table>` : emptyTable("Henüz yedek yok", "Yeni yedek oluştur düğmesiyle güvenli bir kopya oluşturun.");
}

async function createBackup() {
  const button = $("#create-backup");
  button.disabled = true;
  button.textContent = "Yedekleniyor…";
  try {
    const result = await api("/api/backups", {method:"POST"});
    toast(`${result.name} oluşturuldu.`);
    await Promise.all([loadBackups(), loadNotifications(), loadSettingsOverview()]);
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = "＋ Yeni yedek oluştur"; }
}

async function previewJob(jobId) {
  toast("Telegram üyeleri güvenli önizleme için analiz ediliyor…");
  try {
    const summary = await api(`/api/jobs/${jobId}/preview`, {method:"POST"});
    await loadJobs();
    await openCandidateResults(jobId, summary);
    await loadNotifications();
  } catch (error) { toast(error.message); }
}

function candidateStatusLabel(status) {
  return {eligible:"Uygun", invited:"Gruba eklendi", skipped:"Atlandı", failed:"Başarısız", existing:"Zaten grupta", bot:"Bot", deleted:"Silinmiş", admin:"Grup yöneticisi", previously_used:"Daha önce alındı"}[status] || status;
}

async function openCandidateResults(jobId, previewSummary = null) {
  const data = await api(`/api/jobs/${jobId}/candidates`);
  const job = state.jobs.find(item => item.id === Number(jobId));
  state.currentJobId = Number(jobId);
  $("#candidate-message").classList.add("hidden");
  $("#candidate-job-name").textContent = job ? `${job.name} · JOB-${String(job.id).padStart(4,"0")}` : `JOB-${jobId}`;
  $("#candidate-csv").href = `/api/jobs/${jobId}/report.csv`;
  const counts = data.counts || {};
  const approved = ["approved", "scheduled", "queued_execution", "running", "paused_quota", "paused_batch", "proxy_error", "flood_wait", "completed", "failed"].includes(job?.status);
  const summaryItems = [
    ["Taranan", data.items.length, "blue"], ["Uygun", counts.eligible || 0, "green"],
    ["Seçili", data.selected_count || 0, "blue"], ["Gruba eklendi", counts.invited || 0, "green"],
    ["Atlandı", (counts.skipped || 0) + (counts.existing || 0), "yellow"], ["Başarısız", counts.failed || 0, "red"],
  ];
  $("#candidate-summary").innerHTML = summaryItems.map(item => `<article><small>${item[0]}</small><strong class="${item[2]}">${item[1]}</strong></article>`).join("");
  const permissionNote = previewSummary ? `<div class="validation-result ${previewSummary.permissions.can_invite_users ? "" : "error"}">${previewSummary.permissions.can_invite_users ? "Hedef grupta doğrudan üye ekleme yetkisi doğrulandı." : "Hedef grupta üye ekleme yetkisi bulunmuyor."}</div>` : "";
  $("#candidate-table").innerHTML = permissionNote + (data.items.length ? `
    <table><thead><tr><th>Seç</th><th>Kullanıcı</th><th>Telegram ID</th><th>Kullanıcı adı</th><th>Durum</th><th>Neden</th></tr></thead><tbody>
    ${data.items.map(item => `<tr class="${item.selected ? "selected" : ""}"><td>${item.status === "eligible" ? `<input class="candidate-select" type="checkbox" data-candidate-id="${item.id}" ${item.selected ? "checked" : ""} ${approved ? "disabled" : ""}>` : "—"}</td><td>${escapeHtml(item.display_name)}</td><td class="mono">${item.telegram_user_id}</td><td class="mono">${item.username ? "@"+escapeHtml(item.username) : "—"}</td><td>${badge(item.status === "eligible" || item.status === "invited" ? "success" : item.status === "existing" || item.status === "skipped" ? "warning" : item.status)}</td><td>${escapeHtml(item.reason || candidateStatusLabel(item.status))}</td></tr>`).join("")}
    </tbody></table>` : emptyTable("Aday bulunamadı", "Grup erişimini ve filtreleri kontrol edin."));
  $("#approve-job").disabled = !counts.eligible || approved;
  $("#approve-job").textContent = approved ? "Seçim onaylandı" : "Seçimi onayla";
  $("#select-all-eligible").classList.toggle("hidden", approved);
  const resumable = ["approved", "paused_quota", "paused_batch", "proxy_error", "flood_wait"].includes(job?.status);
  $("#execute-job").classList.toggle("hidden", !resumable);
  $("#execute-job").textContent = job?.status === "proxy_error" ? "Proxy düzeltildi, tekrar dene" : job?.status === "approved" ? "Üyeleri hedef gruba ekle" : "İşe devam et";
  openModal("#candidate-modal");
  if (job?.last_error && resumable) showMessage("#candidate-message", job.last_error);
}

async function approveCurrentJob() {
  if (!state.currentJobId) return;
  const candidateIds = $$(".candidate-select:checked").map(item => Number(item.dataset.candidateId));
  if (!candidateIds.length) return showMessage("#candidate-message", "En az bir uygun aday seçin.");
  showMessage("#candidate-message", "Seçim ve yönetici onayı kaydediliyor…");
  try {
    await api(`/api/jobs/${state.currentJobId}/candidates/selection`, {method:"PUT", body:JSON.stringify({candidate_ids:candidateIds})});
    await api(`/api/jobs/${state.currentJobId}/approve`, {method:"POST"});
    showMessage("#candidate-message", `${candidateIds.length} aday onaylandı. Hedef gruba eklemeyi başlatabilirsiniz.`, true);
    $("#approve-job").disabled = true;
    $("#approve-job").textContent = "Seçim onaylandı";
    $("#select-all-eligible").classList.add("hidden");
    $("#execute-job").classList.remove("hidden");
    await Promise.all([loadJobs(), loadNotifications()]);
  } catch (error) { showMessage("#candidate-message", error.message); }
}

async function waitForInviteJob(jobId) {
  for (let attempt = 0; attempt < 900; attempt += 1) {
    const job = await api(`/api/jobs/${jobId}`);
    if (["queued_execution", "running"].includes(job.status)) {
      showMessage(
        "#candidate-message",
        `Üye ekleme çalışıyor: ${job.processed || 0} işlendi, ${job.succeeded || 0} eklendi, ${job.skipped || 0} atlandı, ${job.failed || 0} başarısız.`,
      );
      await delay(1000);
      continue;
    }
    return job;
  }
  return null;
}

async function executeCurrentJob() {
  if (!state.currentJobId) return;
  const button = $("#execute-job");
  button.disabled = true;
  button.classList.add("is-loading");
  button.textContent = "Üyeler ekleniyor";
  showMessage("#candidate-message", "Hedef gruba üye ekleme işi başlatılıyor…");
  try {
    await api(`/api/jobs/${state.currentJobId}/execute`, {method:"POST"});
    const result = await waitForInviteJob(state.currentJobId);
    if (!result) {
      showMessage("#candidate-message", "İş arka planda devam ediyor. İlerlemeyi Kuyruk ekranından veya Sonuçlar penceresinden takip edebilirsiniz.", true);
      await loadJobs();
      return;
    }
    const resultMessage = result.status === "completed"
      ? `İşlem tamamlandı: ${result.succeeded || 0} kişi gruba eklendi, ${result.skipped || 0} atlandı, ${result.failed || 0} başarısız.`
      : result.status === "failed"
        ? `İşlem durdu: ${result.last_error || "Bilinmeyen bir hata oluştu."}`
        : `${result.succeeded || 0} kişi gruba eklendi. ${result.last_error || "Kalan adaylar beklemeye alındı."}`;
    showMessage("#candidate-message", resultMessage, result.status === "completed");
    button.classList.add("hidden");
    await Promise.all([loadJobs(), loadNotifications()]);
    await openCandidateResults(state.currentJobId);
    showMessage("#candidate-message", resultMessage, result.status === "completed");
  } catch (error) { showMessage("#candidate-message", error.message); }
  finally { button.disabled = false; button.classList.remove("is-loading"); button.textContent = "Üyeleri hedef gruba ekle"; }
}

function configureAuthOverlay(setupMode) {
  $("#auth-overlay").classList.remove("hidden");
  $("#auth-description").textContent = setupMode ? "İlk kullanım için en az 8 karakterli bir yönetici parolası oluşturun." : "Yönetim paneline erişmek için parolanızı girin.";
  $("#admin-password-confirm-label").classList.toggle("hidden", !setupMode);
  $("#auth-submit").textContent = setupMode ? "Güvenli kurulumu tamamla" : "Giriş yap";
  $("#auth-submit").dataset.mode = setupMode ? "setup" : "login";
}

async function submitAuth() {
  const password = $("#admin-password").value;
  const setupMode = $("#auth-submit").dataset.mode === "setup";
  if (setupMode && password !== $("#admin-password-confirm").value) return showMessage("#auth-message", "Parolalar eşleşmiyor.");
  showMessage("#auth-message", setupMode ? "Güvenli panel hazırlanıyor…" : "Giriş yapılıyor…");
  try {
    await api(setupMode ? "/api/auth/setup" : "/api/auth/login", {method:"POST", body:JSON.stringify({password})});
    $("#auth-overlay").classList.add("hidden");
    $("#admin-password").value = "";
    $("#admin-password-confirm").value = "";
    await startApp();
  } catch (error) { showMessage("#auth-message", error.message); }
}

async function loadOnboarding() {
  state.onboarding = await api("/api/onboarding");
  ["admin", "api", "session"].forEach(key => {
    const complete = state.onboarding[`${key}_configured`];
    $("#onboard-"+key).classList.toggle("done", complete);
  });
  const customerRelease = Boolean(state.onboarding.customer_release);
  $("#onboard-admin").classList.toggle("hidden", customerRelease);
  $("#onboard-api").classList.toggle("hidden", customerRelease);
  if (customerRelease) {
    $("#onboarding-description").textContent = "Başlamak için Telegram hesabınızı bağlayın.";
    $("#onboard-session strong").textContent = "Telegram hesabınızı bağlayın";
    $("#onboard-session small").textContent = "Telefon numaranızı girin ve Telegram'dan gelen kodu onaylayın.";
    $("#continue-onboarding").textContent = "Telegram hesabı ekle";
  }
  if (!state.onboarding.complete && sessionStorage.getItem("pawgram_onboarding_dismissed") !== "1") openModal("#onboarding-modal");
}

function continueOnboarding() {
  closeModals();
  if (state.onboarding?.customer_release && !state.onboarding?.session_configured) {
    navigate("sessions");
    return runUi(openSessionModalFresh());
  }
  if (!state.onboarding?.api_configured) return navigate("settings");
  if (!state.onboarding?.session_configured) { navigate("sessions"); runUi(openSessionModalFresh()); }
}

async function startApp() {
  await refreshAll();
  await Promise.all([loadNotifications(), loadOnboarding()]);
  await loadReleaseNotes({showPending:true});
}

async function bootstrap() {
  try {
    const license = await loadLicenseStatus();
    if (license.required && !license.valid) return showLicenseOverlay(license.message);
    $("#license-overlay").classList.add("hidden");
    const auth = await api("/api/auth/status");
    if (auth.required && !auth.configured) return configureAuthOverlay(true);
    if (auth.required && !auth.authenticated) return configureAuthOverlay(false);
    await startApp();
  } catch (error) { toast(error.message); }
}

async function refreshAll() {
  try {
    state.health = await api("/api/health");
    await Promise.all([loadDashboard(), loadSessions(), loadJobs(), loadActivityScans(), loadHeartbeat(), loadLogs(), loadTelegramSettings(), loadRotationSettings(), loadDefaultLoginProxy()]);
  } catch (error) { toast(error.message); }
}

$("#navigation").addEventListener("click", event => {
  const button = event.target.closest("[data-page]"); if (button) navigate(button.dataset.page);
});
$$('[data-page-link]').forEach(button => button.addEventListener("click", () => navigate(button.dataset.pageLink)));
$("#session-search").addEventListener("input", renderSessionTable);
["#session-status-filter", "#session-proxy-filter", "#session-sort"].forEach(selector => {
  $(selector).addEventListener("change", renderSessionTable);
});
$("#group-search").addEventListener("input", renderGroups);
["#group-kind-filter", "#group-suitability-filter", "#group-sort"].forEach(selector => {
  $(selector).addEventListener("change", renderGroups);
});
$("#group-session").addEventListener("change", () => {
  state.groups = [];
  state.currentGroupSessionId = null;
  renderGroups();
});
$("#group-access-result-search").addEventListener("input", () => renderGroupAccessBatches(state.currentGroupAccessDetail));
$("#group-access-result-filter").addEventListener("change", () => renderGroupAccessBatches(state.currentGroupAccessDetail));
$("#settings-search").addEventListener("input", filterSettingsSections);
$("#clear-settings-search").addEventListener("click", () => {
  $("#settings-search").value = "";
  filterSettingsSections();
  $("#settings-search").focus();
});
SETTINGS_TRACKED_SELECTORS.forEach(selector => {
  const element = $(selector);
  ["input", "change"].forEach(eventName => element.addEventListener(eventName, () => {
    if (settingsLoading) return;
    renderSettingFieldValidation(element);
    validateSettingsControls();
    renderSettingsUnsavedWarning();
  }));
});
$("#open-session-modal").addEventListener("click", () => runUi(openSessionModalFresh()));
$$('[data-open-job]').forEach(button => button.addEventListener("click", () => {
  if (!state.sessions.length) return toast("İş oluşturmadan önce bir Telegram hesabı bağlayın.");
  openModal("#job-modal");
}));
$$('[data-close-modal]').forEach(button => button.addEventListener("click", closeModals));
$$('.modal-backdrop').forEach(item => item.addEventListener("click", event => { if (event.target === item) closeModals(); }));
$("#request-code").addEventListener("click", requestCode);
$("#login-use-proxy").addEventListener("change", syncLoginProxyMode);
$("#verify-code").addEventListener("click", verifyCode);
$("#create-job").addEventListener("click", createJob);
$("#quick-validate").addEventListener("click", validateQuick);
$("#refresh-groups").addEventListener("click", loadGroups);
$("#refresh-group-access").addEventListener("click", () => runUi(loadGroupAccessBatches()));
$("#start-group-access").addEventListener("click", startGroupAccessBatch);
$("#refresh-session-health").addEventListener("click", () => runUi(loadSessionHealthBatches()));
$("#start-session-health").addEventListener("click", startSessionHealthBatch);
$("#group-access-select-all").addEventListener("change", event => {
  $$(".group-access-session-checkbox").forEach(item => { item.checked = event.target.checked; });
  syncGroupAccessSelectAll();
});
$("#refresh-logs").addEventListener("click", () => runUi(loadLogs({resetVisible:false, force:true})));
$("#toggle-log-refresh").addEventListener("click", () => {
  state.logsAutoRefresh = !state.logsAutoRefresh;
  if (state.logsAutoRefresh) runUi(loadLogs({force:true}));
  else setLogRefreshStatus();
});
LOG_FILTER_SELECTORS.forEach(selector => {
  const eventName = selector === "#log-search" || selector.includes("session") || selector.includes("job") ? "input" : "change";
  $(selector).addEventListener(eventName, () => {
    state.logVisibleCount = 100;
    renderLogPage();
  });
});
$("#clear-log-filters").addEventListener("click", clearLogFilters);
$("#load-more-logs").addEventListener("click", () => { state.logVisibleCount += 100; renderLogPage(); });
$("#export-logs-json").addEventListener("click", () => exportLogs("json"));
$("#export-logs-csv").addEventListener("click", () => exportLogs("csv"));
$("#refresh-activity").addEventListener("click", () => runUi(loadActivityScans()));
$("#refresh-heartbeat").addEventListener("click", () => runUi(loadHeartbeat({syncForm:true})));
$("#save-heartbeat-settings").addEventListener("click", saveHeartbeatSettings);
$("#create-activity-scan").addEventListener("click", createActivityScan);
$("#open-activity-transfer").addEventListener("click", openActivityTransfer);
$("#prepare-activity-transfer").addEventListener("click", prepareActivityTransfer);
$("#save-api-settings").addEventListener("click", saveTelegramSettings);
$("#save-rotation-settings").addEventListener("click", saveRotationSettings);
$("#refresh-settings-overview").addEventListener("click", () => runUi(loadSettingsOverview()));
$("#check-settings-update").addEventListener("click", () => runUi(checkSettingsUpdate()));
$("#acknowledge-release-notes").addEventListener("click", () => runUi(acknowledgeReleaseNotes()));
$("#download-diagnostics").addEventListener("click", () => {
  $("#diagnostics-status").textContent = "Maskelenmiş tanılama raporu hazırlanıyor…";
  setTimeout(() => { $("#diagnostics-status").textContent = "Rapor indirildi; gizli bilgiler dahil edilmedi."; }, 800);
});
$("#save-default-proxy").addEventListener("click", async () => { try { await saveDefaultProxySettings(true); } catch (error) { setDefaultProxyStatus(error.message, "error"); } });
$("#test-default-proxy").addEventListener("click", testDefaultProxyConnection);
$("#proxy-session").addEventListener("change", () => runUi(Promise.all([loadProxySettings(), loadInvitePolicy()])));
$("#save-proxy-settings").addEventListener("click", async () => { try { await saveProxySettings(true); } catch (error) { setProxyStatus(error.message, "error"); } });
$("#test-proxy").addEventListener("click", testProxyConnection);
$("#delete-proxy").addEventListener("click", deleteProxySettings);
$("#save-invite-policy").addEventListener("click", saveInvitePolicy);
$("#bulk-assign-proxies").addEventListener("click", bulkAssignProxies);
$("#create-backup").addEventListener("click", createBackup);
$("#approve-job").addEventListener("click", approveCurrentJob);
$("#execute-job").addEventListener("click", executeCurrentJob);
$("#select-all-eligible").addEventListener("click", () => {
  $$(".candidate-select").forEach(item => { item.checked = true; item.closest("tr")?.classList.add("selected"); });
});
$("#open-notifications").addEventListener("click", () => runUi(openNotifications()));
$("#close-notifications").addEventListener("click", () => $("#notification-drawer").classList.add("hidden"));
$("#auth-submit").addEventListener("click", submitAuth);
$("#license-activate").addEventListener("click", activateLicense);
$("#license-key").addEventListener("keydown", event => { if (event.key === "Enter") activateLicense(); });
$("#admin-password").addEventListener("keydown", event => { if (event.key === "Enter") submitAuth(); });
$("#continue-onboarding").addEventListener("click", continueOnboarding);
$("#dismiss-onboarding").addEventListener("click", () => { sessionStorage.setItem("pawgram_onboarding_dismissed", "1"); closeModals(); });
$("#logout-button").addEventListener("click", () => runUi((async () => { await api("/api/auth/logout", {method:"POST"}); location.reload(); })()));
document.addEventListener("click", event => {
  const dashboardPage = event.target.closest("[data-dashboard-page]");
  if (dashboardPage) navigate(dashboardPage.dataset.dashboardPage);
  const sessionDetail = event.target.closest("[data-session-detail]");
  if (sessionDetail) openSessionDetail(sessionDetail.dataset.sessionDetail);
  const groupDetail = event.target.closest("[data-group-detail]");
  if (groupDetail) openGroupDetail(groupDetail.dataset.groupDetail);
  const copyGroup = event.target.closest("[data-copy-group]");
  if (copyGroup) runUi(copyGroupValue(copyGroup.dataset.copyGroup, copyGroup.dataset.copyKind));
  const logDetail = event.target.closest("[data-log-detail]");
  if (logDetail) openLogDetail(logDetail.dataset.logDetail);
  const copyLog = event.target.closest("[data-copy-log]");
  if (copyLog) runUi(copyLogMessage(copyLog.dataset.copyLog));
  const previewButton = event.target.closest("[data-preview-job]");
  if (previewButton) previewJob(previewButton.dataset.previewJob);
  const resultButton = event.target.closest("[data-view-candidates]");
  if (resultButton) runUi(openCandidateResults(resultButton.dataset.viewCandidates));
  const activityRun = event.target.closest("[data-activity-run]");
  if (activityRun) activityAction(activityRun.dataset.activityRun, "run", activityRun);
  const activityPause = event.target.closest("[data-activity-pause]");
  if (activityPause) activityAction(activityPause.dataset.activityPause, "pause");
  const activityResume = event.target.closest("[data-activity-resume]");
  if (activityResume) activityAction(activityResume.dataset.activityResume, "resume", activityResume);
  const activityResults = event.target.closest("[data-activity-results]");
  if (activityResults) openActivityResults(activityResults.dataset.activityResults);
  const activityDelete = event.target.closest("[data-activity-delete]");
  if (activityDelete) deleteActivityScan(activityDelete.dataset.activityDelete, activityDelete);
  const groupAccessView = event.target.closest("[data-group-access-view]");
  if (groupAccessView) runUi(showGroupAccessBatch(groupAccessView.dataset.groupAccessView));
  const groupAccessStop = event.target.closest("[data-group-access-stop]");
  if (groupAccessStop) groupAccessAction(groupAccessStop.dataset.groupAccessStop, "stop");
  const groupAccessResume = event.target.closest("[data-group-access-resume]");
  if (groupAccessResume) groupAccessAction(groupAccessResume.dataset.groupAccessResume, "resume");
  const sessionHealthStop = event.target.closest("[data-session-health-stop]");
  if (sessionHealthStop) sessionHealthAction(sessionHealthStop.dataset.sessionHealthStop, "stop");
  const sessionHealthResume = event.target.closest("[data-session-health-resume]");
  if (sessionHealthResume) sessionHealthAction(sessionHealthResume.dataset.sessionHealthResume, "resume");
});
document.addEventListener("change", event => {
  const checkbox = event.target.closest(".candidate-select");
  if (checkbox) checkbox.closest("tr")?.classList.toggle("selected", checkbox.checked);
  if (event.target.closest(".group-access-session-checkbox")) syncGroupAccessSelectAll();
});
window.addEventListener("beforeunload", event => {
  if (!settingsHasUnsavedChanges()) return;
  event.preventDefault();
  event.returnValue = "";
});
$("#resolve-single-group").addEventListener("click", async () => {
  const result = $("#resolved-group");
  try {
    const group = await resolveGroup($("#group-session").value, $("#group-reference").value);
    result.className = "validation-result"; result.innerHTML = groupCard(group);
  } catch (error) { result.className = "validation-result error"; result.textContent = error.message; }
});

let backgroundRefreshRunning = false;

async function refreshBackground() {
  if (!$("#auth-overlay").classList.contains("hidden") || !$("#license-overlay").classList.contains("hidden")) return;
  if (backgroundRefreshRunning) return;
  backgroundRefreshRunning = true;
  try {
    const requests = [loadDashboard(), loadLogs({background:true}), loadNotifications()];
    if ($("#page-activity").classList.contains("active")) requests.push(loadActivityScans());
    if ($("#page-heartbeat").classList.contains("active")) requests.push(loadHeartbeat());
    if ($("#page-groups").classList.contains("active")) requests.push(loadGroupAccessBatches(), loadSessionHealthBatches(), loadSessions());
    if ($("#page-sessions").classList.contains("active")) requests.push(loadSessions());
    await Promise.allSettled(requests);
  } finally {
    backgroundRefreshRunning = false;
  }
}

runUi(bootstrap());
setInterval(() => {
  runUi(refreshBackground(), {silent:true});
  $$(".countdown[data-seconds]").forEach(element => {
    const next = Math.max(0, Number(element.dataset.seconds) - 15);
    element.dataset.seconds = next;
    element.textContent = formatDuration(next);
  });
}, 15000);
