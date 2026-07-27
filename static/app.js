const state = { sessions: [], jobs: [], activityScans: [], health: null, telegramSettings: null, rotationSettings: null, onboarding: null, currentJobId: null, loginPhone: "" };

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (response.status === 401 && !path.startsWith("/api/auth/")) configureAuthOverlay(false);
  if (!response.ok) throw new Error(body.detail || "İşlem tamamlanamadı.");
  return body;
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);
}

function badge(status) {
  const labels = { active:"Aktif", flood_wait:"FloodWait", ready:"Hazır", preview:"Önizleme", previewed:"Önizlendi", approved:"Onaylandı", running:"Çalışıyor", completed:"Tamamlandı", failed:"Hatalı", group:"Grup", channel:"Kanal", success:"Uygun", warning:"Zaten grupta", bot:"Bot", deleted:"Silinmiş", admin:"Grup yöneticisi", previously_used:"Daha önce alındı", queued:"Sırada", scheduled:"Planlandı", waiting:"FloodWait", waiting_join:"Katılım onayı", waiting_budget:"Güvenli bütçe", paused:"Duraklatıldı", error:"Hatalı" };
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
  element.textContent = message;
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
  jobs:["Kuyruk","Hazır, çalışan ve tamamlanan iş tanımları"],
  logs:["Loglar","Session, doğrulama ve hata kayıtları"],
  settings:["Ayarlar","Koruma ve Telegram API yapılandırması"],
};

function navigate(page) {
  $$(".page").forEach(item => item.classList.toggle("active", item.id === `page-${page}`));
  $$(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.page === page));
  $("#page-title").textContent = pageMeta[page][0];
  $("#page-subtitle").textContent = pageMeta[page][1];
  if (page === "logs") loadLogs();
  if (page === "groups") fillSessionSelects();
  if (page === "activity") loadActivityScans();
  if (page === "settings") Promise.all([loadBackups(), loadRotationSettings(), loadProxySettings()]);
}

function fillSessionSelects() {
  const options = state.sessions.length
    ? state.sessions.map(s => `<option value="${s.id}">${escapeHtml(s.label)} · ${escapeHtml(s.phone_masked)}</option>`).join("")
    : `<option value="">Önce bir telefon ekleyin</option>`;
  ["#quick-session", "#group-session", "#job-session"].forEach(id => $(id).innerHTML = options);
  $("#activity-session").innerHTML = `<option value="">Uygun session'ı otomatik seç</option>` + (state.sessions.length
    ? state.sessions.map(s => `<option value="${s.id}">${escapeHtml(s.label)} · ${escapeHtml(s.phone_masked)}</option>`).join("")
    : "");
  const proxySelect = $("#proxy-session");
  const previousProxySession = proxySelect.value;
  proxySelect.innerHTML = state.sessions.length
    ? state.sessions.map(s => `<option value="${s.id}">${escapeHtml(s.label)} · ${escapeHtml(s.phone_masked)}</option>`).join("")
    : `<option value="">Önce bir telefon ekleyin</option>`;
  if (previousProxySession && state.sessions.some(s => String(s.id) === previousProxySession)) proxySelect.value = previousProxySession;
}

async function loadDashboard() {
  const dashboard = await api("/api/dashboard");
  $("#metric-sessions").textContent = dashboard.sessions_total;
  $("#metric-active").textContent = dashboard.sessions_active;
  $("#metric-jobs").textContent = dashboard.jobs_total;
  $("#metric-waiting").textContent = dashboard.sessions_waiting;
  $("#top-session-count").textContent = `${dashboard.sessions_total} session`;
}

async function loadSessions() {
  state.sessions = await api("/api/sessions");
  fillSessionSelects();
  const counts = status => state.sessions.filter(item => status.includes(item.status)).length;
  $("#session-total").textContent = state.sessions.length;
  $("#session-active").textContent = counts(["active"]);
  $("#session-wait").textContent = counts(["flood_wait"]);
  $("#session-error").textContent = counts(["error", "invalid", "banned"]);
  $("#session-table").innerHTML = state.sessions.length ? `
    <table><thead><tr><th>#</th><th>Etiket</th><th>Numara</th><th>Hesap</th><th>Durum</th><th>Proxy</th><th>Sağlık</th><th>FloodWait</th></tr></thead><tbody>
      ${state.sessions.map(s => `<tr><td class="mono">#${s.id}</td><td>${escapeHtml(s.label)}</td><td class="mono">${escapeHtml(s.phone_masked)}</td><td>${escapeHtml(s.display_name || "—")} ${s.username ? `<span class="mono">@${escapeHtml(s.username)}</span>` : ""}</td><td>${badge(s.status)}</td><td>${s.proxy_enabled ? `<span class="badge ${s.proxy_last_status === "failed" ? "error" : "active"}">${escapeHtml((s.proxy_type || "proxy").toUpperCase())}</span><small class="mono">${escapeHtml(s.proxy_host || "—")}:${s.proxy_port || "—"}${s.proxy_latency_ms ? ` · ${s.proxy_latency_ms} ms` : ""}</small>` : `<span class="badge">Kapalı</span>`}</td><td><div class="health"><div class="health-bar"><i style="width:${s.health_score}%"></i></div><small>${s.health_score}/100</small></div></td><td class="mono">${s.flood_wait_seconds ? `<span class="countdown" data-seconds="${s.flood_wait_seconds}">${formatDuration(s.flood_wait_seconds)}</span>` : "—"}</td></tr>`).join("")}
    </tbody></table>` : emptyTable("Henüz telefon eklenmedi", "Numara ekle düğmesiyle ilk Telegram hesabınızı bağlayın.");
}

async function loadJobs() {
  state.jobs = await api("/api/jobs");
  const count = status => state.jobs.filter(item => item.status === status).length;
  $("#job-ready").textContent = count("ready");
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
    ${jobs.map(j => `<tr><td><strong>${escapeHtml(j.name)}</strong><div class="mono">JOB-${String(j.id).padStart(4,"0")}</div></td><td>${escapeHtml(j.source_title || j.source_ref)} → ${escapeHtml(j.target_title || j.target_ref)}</td>${compact ? "" : `<td>${escapeHtml(j.session_label)}<br><span class="mono">${escapeHtml(j.phone_masked)}</span></td>`}<td>${j.scheduled_at ? new Date(j.scheduled_at).toLocaleString("tr-TR") : "Hemen"}<br><span class="mono">${escapeHtml(j.working_start || "09:00")}–${escapeHtml(j.working_end || "22:00")}</span></td><td>${badge(j.status)}${j.candidate_count ? `<br><small>${j.candidate_count} uygun aday</small>` : ""}</td>${compact ? "" : `<td><div class="job-actions"><button class="mini-button" data-preview-job="${j.id}">Önizle</button>${j.previewed_at ? `<button class="mini-button" data-view-candidates="${j.id}">Sonuçlar</button><a class="mini-button button-link" href="/api/jobs/${j.id}/report.csv">CSV</a>` : ""}</div></td>`}</tr>`).join("")}
  </tbody></table>`;
}

function emptyTable(title, text) {
  return `<div class="empty-state"><div class="empty-icon">◇</div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(text)}</span></div>`;
}

function renderLogs(logs, selector) {
  $(selector).innerHTML = logs.length ? logs.map(log => {
    const date = new Date(log.created_at);
    return `<div class="log-line ${escapeHtml(log.level)}"><span>[${date.toLocaleTimeString("tr-TR")}]</span><span>${escapeHtml(log.category)}</span><span>${escapeHtml(log.message)}</span></div>`;
  }).join("") : `<div class="empty-state"><strong>Henüz log kaydı yok</strong></div>`;
}

async function loadLogs() {
  const logs = await api("/api/logs?limit=100");
  renderLogs(logs, "#all-logs");
  renderLogs(logs.slice(0, 10), "#live-logs");
}

async function resolveGroup(sessionId, reference) {
  return api("/api/groups/resolve", { method:"POST", body:JSON.stringify({ session_id:Number(sessionId), reference }) });
}

function groupCard(group, label = "Grup") {
  return `<strong>${escapeHtml(label)}:</strong> ${escapeHtml(group.title)} · <span class="mono">ID ${group.id}</span> · ${escapeHtml(group.kind)} · ${group.admin_rights || group.creator ? "yönetici erişimi" : "standart erişim"}`;
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
    const groups = await api(`/api/sessions/${sessionId}/groups`);
    container.className = "table-wrap";
    container.innerHTML = groups.length ? `<table><thead><tr><th>Grup</th><th>ID</th><th>Kullanıcı adı</th><th>Tür</th></tr></thead><tbody>${groups.map(group => `<tr><td>${escapeHtml(group.title)}</td><td class="mono">${group.id}</td><td class="mono">${group.username ? "@"+escapeHtml(group.username) : "—"}</td><td>${badge(group.kind)}</td></tr>`).join("")}</tbody></table>` : emptyTable("Grup bulunamadı", "Bu hesabın erişebildiği grup görünmüyor.");
  } catch (error) {
    container.className = "table-wrap empty-state";
    container.innerHTML = `<strong>Gruplar alınamadı</strong><span>${escapeHtml(error.message)}</span>`;
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
  $("#activity-users").textContent = state.activityScans.reduce((sum, item) => sum + (item.unique_users || 0), 0);
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
        <td><div class="job-actions"><button class="mini-button" data-activity-run="${scan.id}">Çalıştır</button>${scan.status === "paused" ? `<button class="mini-button" data-activity-resume="${scan.id}">Devam</button>` : `<button class="mini-button" data-activity-pause="${scan.id}">Duraklat</button>`}${scan.last_run_at ? `<button class="mini-button" data-activity-results="${scan.id}">Sonuçlar</button><a class="mini-button button-link" href="/api/activity-scans/${scan.id}/report.csv">CSV</a>` : ""}</div></td>
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
    toast(`SCAN-${String(result.scan_id).padStart(4,"0")} otomatik kuyruğa eklendi.`);
    await Promise.all([loadActivityScans(), loadNotifications()]);
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = "Taramayı başlat"; }
}

async function activityAction(scanId, action) {
  try {
    await api(`/api/activity-scans/${scanId}/${action}`, {method:"POST"});
    toast(action === "pause" ? "Tarama duraklatıldı." : action === "resume" ? "Tarama yeniden kuyruğa alındı." : "Tarama kuyruğa alındı.");
    await loadActivityScans();
  } catch (error) { toast(error.message); }
}

async function openActivityResults(scanId) {
  try {
    const data = await api(`/api/activity-scans/${scanId}/results`);
    const scan = data.scan;
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
    openModal("#activity-results-modal");
  } catch (error) { toast(error.message); }
}

function openModal(selector) { $(selector).classList.remove("hidden"); }
function closeModals() { $$(".modal-backdrop").forEach(item => item.classList.add("hidden")); }

async function requestCode() {
  const phone = $("#login-phone").value.trim();
  const label = $("#login-label").value.trim();
  showMessage("#login-message", "Telegram bağlantısı kuruluyor…");
  try {
    await api("/api/sessions/login/start", { method:"POST", body:JSON.stringify({ phone, label }) });
    state.loginPhone = phone;
    $("#login-step-phone").classList.add("hidden");
    $("#login-step-code").classList.remove("hidden");
    showMessage("#login-message", "Kod gönderildi. Telegram uygulamanızı kontrol edin.", true);
  } catch (error) { showMessage("#login-message", error.message); }
}

async function verifyCode() {
  showMessage("#login-message", "Kod doğrulanıyor…");
  try {
    const result = await api("/api/sessions/login/verify", { method:"POST", body:JSON.stringify({
      phone:state.loginPhone, code:$("#login-code").value.trim(), password:$("#login-password").value || null,
    }) });
    if (result.password_required) return showMessage("#login-message", result.message);
    closeModals();
    toast("Telegram hesabı başarıyla bağlandı.");
    await refreshAll();
  } catch (error) { showMessage("#login-message", error.message); }
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
}

async function saveTelegramSettings() {
  showMessage("#api-settings-message", "API bilgileri güvenli biçimde kaydediliyor…");
  try {
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
    toast("Telegram API bağlantısı yapılandırıldı.");
  } catch (error) { showMessage("#api-settings-message", error.message); }
}

async function loadRotationSettings() {
  state.rotationSettings = await api("/api/settings/rotation");
  $("#rotation-daily-quota").value = state.rotationSettings.daily_quota;
}

async function saveRotationSettings() {
  showMessage("#rotation-settings-message", "Round-Robin kotası kaydediliyor…");
  try {
    const dailyQuota = Number($("#rotation-daily-quota").value);
    if (!Number.isInteger(dailyQuota) || dailyQuota < 1 || dailyQuota > 1000) {
      throw new Error("Günlük kota 1 ile 1000 arasında tam sayı olmalı.");
    }
    await api("/api/settings/rotation", {
      method:"POST",
      body:JSON.stringify({daily_quota:dailyQuota}),
    });
    showMessage("#rotation-settings-message", `Her session için günlük kota ${dailyQuota} olarak kaydedildi.`, true);
    await loadRotationSettings();
  } catch (error) { showMessage("#rotation-settings-message", error.message); }
}

function setProxyStatus(message, kind = "") {
  const element = $("#proxy-status");
  element.textContent = message;
  element.classList.remove("success", "error");
  if (kind) element.classList.add(kind);
}

async function loadProxySettings() {
  const sessionId = Number($("#proxy-session").value);
  const controls = ["#proxy-enabled", "#proxy-type", "#proxy-host", "#proxy-port", "#proxy-username", "#proxy-password", "#save-proxy-settings", "#test-proxy"];
  controls.forEach(selector => $(selector).disabled = !sessionId);
  if (!sessionId) {
    setProxyStatus("Proxy yapılandırmak için önce bir Telegram hesabı ekleyin.");
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
    else setProxyStatus(config.enabled ? "Proxy etkin; bağlantı testi önerilir." : "Bu session doğrudan bağlantı kullanıyor.");
  } catch (error) { setProxyStatus(error.message, "error"); }
}

async function saveProxySettings(showSuccess = true) {
  const sessionId = Number($("#proxy-session").value);
  if (!sessionId) throw new Error("Bir Telegram session seçin.");
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
  await loadSessions();
  return sessionId;
}

async function testProxyConnection() {
  const button = $("#test-proxy");
  button.disabled = true;
  setProxyStatus("Proxy üzerinden Telegram bağlantısı test ediliyor…");
  try {
    const sessionId = await saveProxySettings(false);
    if (!$("#proxy-enabled").checked) throw new Error("Test için önce proxy kullanımını etkinleştirin.");
    const result = await api(`/api/sessions/${sessionId}/proxy/test`, {method:"POST"});
    setProxyStatus(`Bağlantı başarılı · ${result.latency_ms} ms`, "success");
    await loadSessions();
  } catch (error) { setProxyStatus(error.message, "error"); }
  finally { button.disabled = false; }
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
  await loadNotifications();
  $("#notification-drawer").classList.remove("hidden");
  await api("/api/notifications/read", {method:"POST"});
  $("#notification-count").classList.add("hidden");
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
    await Promise.all([loadBackups(), loadNotifications()]);
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = "＋ Yeni yedek oluştur"; }
}

function candidateStatusLabel(status) {
  return {eligible:"Uygun", existing:"Zaten grupta", bot:"Bot", deleted:"Silinmiş", admin:"Grup yöneticisi", previously_used:"Daha önce alındı"}[status] || status;
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

async function openCandidateResults(jobId, previewSummary = null) {
  const data = await api(`/api/jobs/${jobId}/candidates`);
  const job = state.jobs.find(item => item.id === Number(jobId));
  state.currentJobId = Number(jobId);
  $("#candidate-job-name").textContent = job ? `${job.name} · JOB-${String(job.id).padStart(4,"0")}` : `JOB-${jobId}`;
  $("#candidate-csv").href = `/api/jobs/${jobId}/report.csv`;
  const counts = data.counts || {};
  const scanned = data.items.length;
  const summaryItems = [
    ["Taranan", scanned, "blue"], ["Uygun", counts.eligible || 0, "green"],
    ["Zaten grupta", counts.existing || 0, "yellow"], ["Bot", counts.bot || 0, ""],
    ["Yönetici", counts.admin || 0, "yellow"], ["Daha önce alındı", counts.previously_used || 0, "yellow"],
    ["Silinmiş", counts.deleted || 0, "red"],
  ];
  $("#candidate-summary").innerHTML = summaryItems.map(item => `<article><small>${item[0]}</small><strong class="${item[2]}">${item[1]}</strong></article>`).join("");
  const permissionNote = previewSummary ? `<div class="validation-result ${previewSummary.permissions.can_invite_users ? "" : "error"}">${previewSummary.permissions.can_invite_users ? "Gönderilecek grupta kullanıcı davet etme yetkisi doğrulandı." : "Gönderilecek grupta kullanıcı davet etme yetkisi görünmüyor. Önizleme kullanılabilir fakat işlem başlatılamaz."}</div>` : "";
  $("#candidate-table").innerHTML = permissionNote + (data.items.length ? `
    <table><thead><tr><th>Kullanıcı</th><th>Telegram ID</th><th>Kullanıcı adı</th><th>Durum</th><th>Neden</th></tr></thead><tbody>
    ${data.items.map(item => `<tr><td>${escapeHtml(item.display_name)}</td><td class="mono">${item.telegram_user_id}</td><td class="mono">${item.username ? "@"+escapeHtml(item.username) : "—"}</td><td>${badge(item.status === "eligible" ? "success" : item.status === "existing" ? "warning" : item.status)}</td><td>${escapeHtml(item.reason || candidateStatusLabel(item.status))}</td></tr>`).join("")}
    </tbody></table>` : emptyTable("Aday bulunamadı", "Grup erişimini ve filtreleri kontrol edin."));
  $("#approve-job").disabled = !counts.eligible || job?.status === "approved";
  $("#approve-job").textContent = job?.status === "approved" ? "Onaylandı" : "Önizlemeyi onayla";
  openModal("#candidate-modal");
}

async function approveCurrentJob() {
  if (!state.currentJobId) return;
  showMessage("#candidate-message", "Yönetici onayı kaydediliyor…");
  try {
    await api(`/api/jobs/${state.currentJobId}/approve`, {method:"POST"});
    showMessage("#candidate-message", "İş yönetici tarafından onaylandı.", true);
    $("#approve-job").disabled = true;
    $("#approve-job").textContent = "Onaylandı";
    await Promise.all([loadJobs(), loadNotifications()]);
  } catch (error) { showMessage("#candidate-message", error.message); }
}

function candidateStatusLabel(status) {
  return {eligible:"Uygun", invited:"Davet edildi", skipped:"Atlandı", failed:"Başarısız", existing:"Zaten grupta", bot:"Bot", deleted:"Silinmiş", admin:"Grup yöneticisi", previously_used:"Daha önce alındı"}[status] || status;
}

async function openCandidateResults(jobId, previewSummary = null) {
  const data = await api(`/api/jobs/${jobId}/candidates`);
  const job = state.jobs.find(item => item.id === Number(jobId));
  state.currentJobId = Number(jobId);
  $("#candidate-job-name").textContent = job ? `${job.name} · JOB-${String(job.id).padStart(4,"0")}` : `JOB-${jobId}`;
  $("#candidate-csv").href = `/api/jobs/${jobId}/report.csv`;
  const counts = data.counts || {};
  const summaryItems = [
    ["Taranan", data.items.length, "blue"], ["Uygun", counts.eligible || 0, "green"],
    ["Seçili", data.selected_count || 0, "blue"], ["Davet edildi", counts.invited || 0, "green"],
    ["Atlandı", (counts.skipped || 0) + (counts.existing || 0), "yellow"], ["Başarısız", counts.failed || 0, "red"],
  ];
  $("#candidate-summary").innerHTML = summaryItems.map(item => `<article><small>${item[0]}</small><strong class="${item[2]}">${item[1]}</strong></article>`).join("");
  const permissionNote = previewSummary ? `<div class="validation-result ${previewSummary.permissions.can_invite_users ? "" : "error"}">${previewSummary.permissions.can_invite_users ? "Hedef grupta kullanıcı davet etme yetkisi doğrulandı." : "Hedef grupta kullanıcı davet etme yetkisi bulunmuyor."}</div>` : "";
  $("#candidate-table").innerHTML = permissionNote + (data.items.length ? `
    <table><thead><tr><th>Seç</th><th>Kullanıcı</th><th>Telegram ID</th><th>Kullanıcı adı</th><th>Durum</th><th>Neden</th></tr></thead><tbody>
    ${data.items.map(item => `<tr class="${item.selected ? "selected" : ""}"><td>${item.status === "eligible" ? `<input class="candidate-select" type="checkbox" data-candidate-id="${item.id}" ${item.selected ? "checked" : ""}>` : "—"}</td><td>${escapeHtml(item.display_name)}</td><td class="mono">${item.telegram_user_id}</td><td class="mono">${item.username ? "@"+escapeHtml(item.username) : "—"}</td><td>${badge(item.status === "eligible" || item.status === "invited" ? "success" : item.status === "existing" || item.status === "skipped" ? "warning" : item.status)}</td><td>${escapeHtml(item.reason || candidateStatusLabel(item.status))}</td></tr>`).join("")}
    </tbody></table>` : emptyTable("Aday bulunamadı", "Grup erişimini ve filtreleri kontrol edin."));
  const approved = ["approved", "running", "paused_quota", "flood_wait", "completed"].includes(job?.status);
  $("#approve-job").disabled = !counts.eligible || approved;
  $("#approve-job").textContent = approved ? "Seçim onaylandı" : "Seçimi onayla";
  $("#select-all-eligible").classList.toggle("hidden", approved);
  $("#execute-job").classList.toggle("hidden", !["approved", "paused_quota"].includes(job?.status));
  openModal("#candidate-modal");
}

async function approveCurrentJob() {
  if (!state.currentJobId) return;
  const candidateIds = $$(".candidate-select:checked").map(item => Number(item.dataset.candidateId));
  if (!candidateIds.length) return showMessage("#candidate-message", "Rızası ve aktif üyeliği doğrulanmış en az bir adayı seçin.");
  showMessage("#candidate-message", "Seçim ve yönetici onayı kaydediliyor…");
  try {
    await api(`/api/jobs/${state.currentJobId}/candidates/selection`, {method:"PUT", body:JSON.stringify({candidate_ids:candidateIds})});
    await api(`/api/jobs/${state.currentJobId}/approve`, {method:"POST"});
    showMessage("#candidate-message", `${candidateIds.length} aday onaylandı. Davetleri başlatabilirsiniz.`, true);
    $("#approve-job").disabled = true;
    $("#approve-job").textContent = "Seçim onaylandı";
    $("#select-all-eligible").classList.add("hidden");
    $("#execute-job").classList.remove("hidden");
    await Promise.all([loadJobs(), loadNotifications()]);
  } catch (error) { showMessage("#candidate-message", error.message); }
}

async function executeCurrentJob() {
  if (!state.currentJobId) return;
  showMessage("#candidate-message", "Davet işi başlatılıyor…");
  try {
    const result = await api(`/api/jobs/${state.currentJobId}/execute`, {method:"POST"});
    showMessage("#candidate-message", `${result.selected_count} seçili aday için işlem başlatıldı.`, true);
    $("#execute-job").classList.add("hidden");
    await Promise.all([loadJobs(), loadNotifications()]);
  } catch (error) { showMessage("#candidate-message", error.message); }
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
  if (!state.onboarding.complete && sessionStorage.getItem("pawgram_onboarding_dismissed") !== "1") openModal("#onboarding-modal");
}

function continueOnboarding() {
  closeModals();
  if (!state.onboarding?.api_configured) return navigate("settings");
  if (!state.onboarding?.session_configured) { navigate("sessions"); openModal("#session-modal"); }
}

async function startApp() {
  await refreshAll();
  await Promise.all([loadNotifications(), loadOnboarding()]);
}

async function bootstrap() {
  try {
    const auth = await api("/api/auth/status");
    if (!auth.configured) return configureAuthOverlay(true);
    if (!auth.authenticated) return configureAuthOverlay(false);
    await startApp();
  } catch (error) { toast(error.message); }
}

async function refreshAll() {
  try {
    state.health = await api("/api/health");
    await Promise.all([loadDashboard(), loadSessions(), loadJobs(), loadActivityScans(), loadLogs(), loadTelegramSettings(), loadRotationSettings()]);
  } catch (error) { toast(error.message); }
}

$("#navigation").addEventListener("click", event => {
  const button = event.target.closest("[data-page]"); if (button) navigate(button.dataset.page);
});
$$('[data-page-link]').forEach(button => button.addEventListener("click", () => navigate(button.dataset.pageLink)));
$("#open-session-modal").addEventListener("click", () => openModal("#session-modal"));
$$('[data-open-job]').forEach(button => button.addEventListener("click", () => {
  if (!state.sessions.length) return toast("İş oluşturmadan önce bir Telegram hesabı bağlayın.");
  openModal("#job-modal");
}));
$$('[data-close-modal]').forEach(button => button.addEventListener("click", closeModals));
$$('.modal-backdrop').forEach(item => item.addEventListener("click", event => { if (event.target === item) closeModals(); }));
$("#request-code").addEventListener("click", requestCode);
$("#verify-code").addEventListener("click", verifyCode);
$("#create-job").addEventListener("click", createJob);
$("#quick-validate").addEventListener("click", validateQuick);
$("#refresh-groups").addEventListener("click", loadGroups);
$("#refresh-logs").addEventListener("click", loadLogs);
$("#refresh-activity").addEventListener("click", loadActivityScans);
$("#create-activity-scan").addEventListener("click", createActivityScan);
$("#save-api-settings").addEventListener("click", saveTelegramSettings);
$("#save-rotation-settings").addEventListener("click", saveRotationSettings);
$("#proxy-session").addEventListener("change", loadProxySettings);
$("#save-proxy-settings").addEventListener("click", async () => { try { await saveProxySettings(true); } catch (error) { setProxyStatus(error.message, "error"); } });
$("#test-proxy").addEventListener("click", testProxyConnection);
$("#create-backup").addEventListener("click", createBackup);
$("#approve-job").addEventListener("click", approveCurrentJob);
$("#execute-job").addEventListener("click", executeCurrentJob);
$("#select-all-eligible").addEventListener("click", () => {
  $$(".candidate-select").forEach(item => { item.checked = true; item.closest("tr")?.classList.add("selected"); });
});
$("#open-notifications").addEventListener("click", openNotifications);
$("#close-notifications").addEventListener("click", () => $("#notification-drawer").classList.add("hidden"));
$("#auth-submit").addEventListener("click", submitAuth);
$("#admin-password").addEventListener("keydown", event => { if (event.key === "Enter") submitAuth(); });
$("#continue-onboarding").addEventListener("click", continueOnboarding);
$("#dismiss-onboarding").addEventListener("click", () => { sessionStorage.setItem("pawgram_onboarding_dismissed", "1"); closeModals(); });
$("#logout-button").addEventListener("click", async () => { await api("/api/auth/logout", {method:"POST"}); location.reload(); });
document.addEventListener("click", event => {
  const previewButton = event.target.closest("[data-preview-job]");
  if (previewButton) previewJob(previewButton.dataset.previewJob);
  const resultButton = event.target.closest("[data-view-candidates]");
  if (resultButton) openCandidateResults(resultButton.dataset.viewCandidates);
  const activityRun = event.target.closest("[data-activity-run]");
  if (activityRun) activityAction(activityRun.dataset.activityRun, "run");
  const activityPause = event.target.closest("[data-activity-pause]");
  if (activityPause) activityAction(activityPause.dataset.activityPause, "pause");
  const activityResume = event.target.closest("[data-activity-resume]");
  if (activityResume) activityAction(activityResume.dataset.activityResume, "resume");
  const activityResults = event.target.closest("[data-activity-results]");
  if (activityResults) openActivityResults(activityResults.dataset.activityResults);
});
document.addEventListener("change", event => {
  const checkbox = event.target.closest(".candidate-select");
  if (checkbox) checkbox.closest("tr")?.classList.toggle("selected", checkbox.checked);
});
$("#resolve-single-group").addEventListener("click", async () => {
  const result = $("#resolved-group");
  try {
    const group = await resolveGroup($("#group-session").value, $("#group-reference").value);
    result.className = "validation-result"; result.innerHTML = groupCard(group);
  } catch (error) { result.className = "validation-result error"; result.textContent = error.message; }
});

bootstrap();
setInterval(() => {
  if (!$("#auth-overlay").classList.contains("hidden")) return;
  loadDashboard(); loadLogs(); loadNotifications();
  if ($("#page-activity").classList.contains("active")) loadActivityScans();
  $$(".countdown[data-seconds]").forEach(element => {
    const next = Math.max(0, Number(element.dataset.seconds) - 15);
    element.dataset.seconds = next;
    element.textContent = formatDuration(next);
  });
}, 15000);
