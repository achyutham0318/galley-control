const PROD_API_URL = "https://galley-control-api.onrender.com/api/v1";

function normalizeApiUrl(raw) {
  let clean = String(raw || "").trim().replace(/\/+$/, "");
  if (!clean) return "";
  if (!clean.endsWith("/api/v1")) {
    clean = `${clean}/api/v1`;
  }
  return clean;
}

function resolveApiBaseUrl() {
  try {
    const params = new URLSearchParams(window.location.search);
    const apiParam = params.get("api");
    if (apiParam) {
      const normalized = normalizeApiUrl(apiParam);
      if (normalized) {
        localStorage.setItem("galley_api_url", normalized);
        return normalized;
      }
    }
    const saved = localStorage.getItem("galley_api_url");
    if (saved) {
      const normalizedSaved = normalizeApiUrl(saved);
      if (normalizedSaved) return normalizedSaved;
    }
  } catch {}
  const host = window.location.hostname;
  if (host === "localhost" || host === "127.0.0.1") {
    return "http://localhost:8000/api/v1";
  }
  return normalizeApiUrl(PROD_API_URL);
}

let API_BASE = resolveApiBaseUrl();

const state = {
  managerToken: null,
  staffToken: null,
  studentToken: null,
  escalations: [],
  selectedEscalationIndex: 0,
  auditLogs: [],
  auditFilter: "all",
  surplusItems: [],
  managerMenuItems: [],
  managerMenuSearch: "",
  managerMenuCategory: "ALL",
  restaurants: [],
  selectedRestaurantId: null,
  activeView: "manager-dashboard",
  kdsOrders: [],
  kdsCancelled: [],
  kdsEtag: null,
  menuItems: [],
  myOrders: [],
  wallet: null,
  walletFilter: "ALL",
  useGoldCoins: false,
  qrTimerInterval: null,
};

function formatPaise(paise) {
  const inr = Math.round((paise || 0) / 100);
  return `₹${inr.toLocaleString("en-IN")}`;
}

function showToast(title, desc, type = "success") {
  const toast = document.getElementById("action-toast");
  const toastTitle = document.getElementById("toast-title");
  const toastDesc = document.getElementById("toast-desc");
  const toastIcon = document.getElementById("toast-icon");
  const toastIconBg = document.getElementById("toast-icon-bg");
  if (!toast) return;

  toastTitle.textContent = title;
  toastDesc.textContent = desc;
  if (type === "success") {
    toastIcon.textContent = "check_circle";
    toastIconBg.className = "w-10 h-10 rounded-lg flex items-center justify-center flex-shrink-0 bg-emerald-500 text-white";
  } else if (type === "warn") {
    toastIcon.textContent = "warning";
    toastIconBg.className = "w-10 h-10 rounded-lg flex items-center justify-center flex-shrink-0 bg-amber-500 text-white";
  } else {
    toastIcon.textContent = "cancel";
    toastIconBg.className = "w-10 h-10 rounded-lg flex items-center justify-center flex-shrink-0 bg-rose-600 text-white";
  }

  toast.classList.remove("translate-y-[-250%]", "opacity-0", "pointer-events-none");
  toast.classList.add("translate-y-0", "opacity-100");
  setTimeout(() => {
    toast.classList.remove("translate-y-0", "opacity-100");
    toast.classList.add("translate-y-[-250%]", "opacity-0", "pointer-events-none");
  }, 4500);
}

function updateCoreNodeIndicator(isOnline) {
  const dot = document.getElementById("core-node-dot");
  const txt = document.getElementById("core-node-status-text");
  if (dot) {
    dot.className = isOnline
      ? "inline-block w-1.5 h-1.5 rounded-full bg-emerald-500"
      : "inline-block w-1.5 h-1.5 rounded-full bg-rose-500";
  }
  if (txt) {
    txt.textContent = isOnline ? "Core Node // Online" : "Core Node // Offline";
  }
}

function renderConnectionBanner(isOffline, errorDetail = "") {
  const mainEl = document.querySelector("main");
  if (!mainEl) return;
  let banner = document.getElementById("backend-connection-banner");
  if (!isOffline) {
    if (banner) banner.remove();
    updateCoreNodeIndicator(true);
    return;
  }
  updateCoreNodeIndicator(false);
  if (!banner) {
    banner = document.createElement("div");
    banner.id = "backend-connection-banner";
    banner.className = "mx-margin-desktop mt-4 mb-2 px-4 py-3 rounded-xl bg-amber-50 dark:bg-amber-950/60 border border-amber-300 dark:border-amber-700 flex flex-wrap items-center justify-between gap-3 text-xs";
    mainEl.insertBefore(banner, mainEl.firstChild);
  }
  banner.innerHTML = `
    <div class="text-amber-900 dark:text-amber-200">
      <span class="font-bold">Backend Node Unreachable:</span>
      <span class="font-mono">${API_BASE}</span>
      ${errorDetail ? `<span class="text-amber-700 dark:text-amber-300 ml-1">(${errorDetail})</span>` : ""}
    </div>
    <button id="banner-config-node" type="button" class="px-3 py-1.5 rounded-lg bg-amber-600 hover:bg-amber-700 text-white font-bold">Configure Backend Node</button>
  `;
  const btn = banner.querySelector("#banner-config-node");
  if (btn) btn.onclick = () => showBackendNodeModal();
}

async function apiFetch(path, options = {}, token = state.managerToken) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  try {
    const resp = await fetch(`${API_BASE}${path}`, { ...options, headers });
    const text = await resp.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = text;
    }
    renderConnectionBanner(false);
    return { status: resp.status, ok: resp.ok, data, headers: resp.headers };
  } catch (err) {
    renderConnectionBanner(true, err?.message || "Network error");
    return {
      status: 0,
      ok: false,
      data: { message: String(err) },
      headers: new Headers(),
      networkError: true,
    };
  }
}

async function ensureAuth() {
  try {
    if (!state.managerToken) {
      const res = await apiFetch("/auth/staff/pin-login", {
        method: "POST",
        body: JSON.stringify({ staff_id: "MGR01", pin: "112233" }),
      }, null);
      if (res.ok && res.data?.access_token) state.managerToken = res.data.access_token;
    }
    if (!state.staffToken) {
      const res = await apiFetch("/auth/staff/pin-login", {
        method: "POST",
        body: JSON.stringify({ staff_id: "COUNTER01", pin: "445566" }),
      }, null);
      if (res.ok && res.data?.access_token) state.staffToken = res.data.access_token;
    }
    if (!state.studentToken) {
      const res = await apiFetch("/auth/student/login", {
        method: "POST",
        body: JSON.stringify({ email: "student01@campus.edu", password: "Student@123" }),
      }, null);
      if (res.ok && res.data?.access_token) state.studentToken = res.data.access_token;
    }
  } catch {}
}

async function loadManagerDashboard() {
  try {
    await ensureAuth();
    const [metricsRes, escRes, auditRes, surplusRes, mgrMenuRes] = await Promise.all([
      apiFetch("/manager/metrics"),
      apiFetch("/manager/escalations"),
      apiFetch(buildAuditUrl()),
      apiFetch("/manager/menu/surplus-overview"),
      apiFetch("/manager/menu"),
    ]);

    if (metricsRes.ok && metricsRes.data) renderMetrics(metricsRes.data);
    if (escRes.ok && Array.isArray(escRes.data)) {
      state.escalations = escRes.data;
      if (state.selectedEscalationIndex >= state.escalations.length) {
        state.selectedEscalationIndex = 0;
      }
      renderEscalations();
    }
    if (auditRes.ok && auditRes.data) {
      state.auditLogs = auditRes.data.items || [];
      renderAuditLogs();
    }
    if (surplusRes.ok && Array.isArray(surplusRes.data)) {
      state.surplusItems = surplusRes.data;
      renderSurplusControlPanel(metricsRes.ok ? metricsRes.data : null);
    }
    if (mgrMenuRes.ok && Array.isArray(mgrMenuRes.data)) {
      state.managerMenuItems = mgrMenuRes.data;
      renderMenuEditorPanel();
    }
  } catch (err) {
    renderConnectionBanner(true, String(err));
  }
}

function buildAuditUrl() {
  const base = "/manager/audit-logs?limit=20";
  if (state.auditFilter === "auto") return `${base}&event_type=REFUND_EVAL&decision=AUTO_APPROVE`;
  if (state.auditFilter === "escalated") return `${base}&event_type=REFUND_EVAL&decision=ESCALATE`;
  if (state.auditFilter === "replay") return `${base}&event_type=QR_VERIFY&decision=RED`;
  return base;
}

function renderMetrics(m) {
  const kpiGrid = document.querySelector(".grid.grid-cols-1.md\\:grid-cols-2.lg\\:grid-cols-4");
  if (!kpiGrid) return;
  const cards = kpiGrid.children;
  if (cards[0]) {
    cards[0].querySelector(".font-headline-lg").textContent = formatPaise(m.revenue_protected_paise);
  }
  if (cards[1]) {
    cards[1].querySelector(".font-headline-lg").textContent = String(m.qr_attacks_blocked_count);
    const spans = cards[1].querySelectorAll(".mt-space-md span");
    if (spans[0]) spans[0].textContent = m.last_blocked_order_number ? `Last: ${m.last_blocked_order_number} blocked` : "No replay blocks today";
    if (spans[1]) spans[1].textContent = m.last_blocked_at_ist ? m.last_blocked_at_ist.split(", ")[1] || "Live" : "Live";
  }
  if (cards[2]) {
    cards[2].querySelector(".font-headline-lg").textContent = String(m.auto_refunds_processed_count);
    const badge = cards[2].querySelector(".bg-emerald-50.border-emerald-200");
    if (badge) badge.textContent = `Avg Decision: ${Math.round(m.avg_refund_eval_latency_ms || 0)}ms`;
  }
  if (cards[3]) {
    cards[3].querySelector(".font-headline-lg").textContent = `${m.scan_compliance_rate.toFixed(1)}%`;
    const bar = cards[3].querySelector(".bg-sky-600.h-full");
    if (bar) bar.style.width = `${m.scan_compliance_rate}%`;
    const verSpan = cards[3].querySelector(".text-sky-700.font-mono");
    if (verSpan) verSpan.textContent = `${m.verified_scans_count ?? 0} Verified`;
  }
  const latBadge = document.querySelector(".px-1\\.5.py-0\\.5.rounded.bg-slate-100.border");
  if (latBadge) latBadge.textContent = `${(m.avg_qr_verify_latency_ms || 0).toFixed(1)}ms avg`;
}

function renderSurplusControlPanel(metrics) {
  const dashWrapper = document.querySelector("main")?.firstElementChild;
  if (!dashWrapper) return;
  let panel = document.getElementById("surplus-control-panel");
  if (!panel) {
    panel = document.createElement("div");
    panel.id = "surplus-control-panel";
    panel.className = "px-margin-desktop mt-space-xl space-y-4";
    dashWrapper.appendChild(panel);
  }

  const rescuedPaise = metrics?.food_waste_prevented_paise || 0;
  const rescuedUnits = metrics?.surplus_units_rescued_count || 0;
  const activeDeals = metrics?.active_surplus_deals_count || 0;

  panel.innerHTML = `
    <div class="rounded-xl bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-700 p-space-lg shadow-sm space-y-4">
      <div class="flex flex-wrap items-center justify-between gap-4 border-b border-slate-100 dark:border-slate-800 pb-4">
        <div>
          <h2 class="font-headline-md text-headline-md text-slate-900 dark:text-slate-100 font-bold">Excess Food &amp; Bulk Discount Control</h2>
          <p class="text-xs text-slate-500 dark:text-slate-400">Apply real-time percentage discounts to high-stock bulk items to prevent food wastage.</p>
        </div>
        <div class="flex flex-wrap items-center gap-3">
          <div class="px-3.5 py-2 rounded-lg bg-emerald-50 dark:bg-emerald-950/60 border border-emerald-200 dark:border-emerald-800">
            <div class="text-[10px] font-semibold text-emerald-700 dark:text-emerald-300 uppercase">Food Waste Prevented</div>
            <div class="font-mono font-bold text-sm text-emerald-800 dark:text-emerald-200">${formatPaise(rescuedPaise)} (${rescuedUnits} units)</div>
          </div>
          <div class="px-3.5 py-2 rounded-lg bg-amber-50 dark:bg-amber-950/60 border border-amber-200 dark:border-amber-800">
            <div class="text-[10px] font-semibold text-amber-700 dark:text-amber-300 uppercase">Active Surplus Deals</div>
            <div class="font-mono font-bold text-sm text-amber-800 dark:text-amber-200">${activeDeals} Active</div>
          </div>
        </div>
      </div>
      <div class="space-y-2.5">
        ${state.surplusItems.map((item) => `
          <div class="p-3.5 rounded-xl bg-slate-50 dark:bg-slate-800/70 border border-slate-200 dark:border-slate-700 flex flex-col xl:flex-row xl:items-center justify-between gap-3" data-surplus-row="${item.id}">
            <div class="min-w-0 flex-1 space-y-1">
              <div class="flex flex-wrap items-center gap-2">
                <span class="font-semibold text-slate-900 dark:text-slate-100">${item.name}</span>
                ${item.is_bulk_excess ? `<span class="px-2 py-0.5 rounded bg-amber-100 dark:bg-amber-900/60 border border-amber-300 dark:border-amber-700 text-amber-800 dark:text-amber-200 text-[10px] font-bold uppercase">Bulk Stock (${item.stock_count})</span>` : `<span class="px-2 py-0.5 rounded bg-slate-200 dark:bg-slate-700 text-slate-700 dark:text-slate-200 text-[10px] font-mono">Stock: ${item.stock_count}</span>`}
                ${item.is_surplus_active ? `<span class="px-2 py-0.5 rounded bg-emerald-100 dark:bg-emerald-900/60 border border-emerald-300 dark:border-emerald-700 text-emerald-800 dark:text-emerald-200 text-[10px] font-bold uppercase">${item.active_discount_percent}% OFF (${formatPaise(item.effective_price_paise)})</span>` : ""}
              </div>
              <div class="text-xs text-slate-500 dark:text-slate-400">Base: ${formatPaise(item.price_paise)}${item.surplus_note ? ` • Note: ${item.surplus_note}` : ""}${item.surplus_expires_at_ist ? ` • Expires: ${item.surplus_expires_at_ist}` : ""}</div>
            </div>
            <div class="flex flex-wrap items-center gap-1.5">
              ${[15, 25, 40, 50].map((pct) => `
                <button data-quick-pct="${pct}" data-item-id="${item.id}" class="px-2.5 py-1.5 rounded-lg bg-white dark:bg-slate-900 border border-slate-300 dark:border-slate-600 hover:bg-sky-50 dark:hover:bg-sky-900/40 text-xs font-bold text-slate-700 dark:text-slate-200" type="button">${pct}%</button>
              `).join("")}
              <input data-custom-pct="${item.id}" type="number" min="5" max="90" value="${item.is_surplus_active ? item.active_discount_percent : 25}" class="w-16 rounded-lg border-slate-300 dark:border-slate-600 text-xs font-mono text-center" />
              <select data-dur="${item.id}" class="rounded-lg border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-900 text-xs text-slate-700 dark:text-slate-200">
                <option value="30">30 mins</option>
                <option value="60" selected>60 mins</option>
                <option value="">Until Closing</option>
              </select>
              <input data-note="${item.id}" type="text" placeholder="Note (e.g. Fresh batch)" value="${item.surplus_note || ""}" class="w-36 rounded-lg border-slate-300 dark:border-slate-600 text-xs" />
              <button data-apply-surplus="${item.id}" class="px-3 py-1.5 rounded-lg bg-sky-600 hover:bg-sky-700 text-white text-xs font-bold" type="button">Apply</button>
              ${item.is_surplus_active ? `<button data-end-surplus="${item.id}" class="px-3 py-1.5 rounded-lg bg-rose-50 dark:bg-rose-950/60 border border-rose-200 dark:border-rose-800 text-rose-700 dark:text-rose-300 text-xs font-bold" type="button">End Deal</button>` : ""}
            </div>
          </div>
        `).join("")}
      </div>
    </div>
  `;

  async function submitSurplus(itemId, isDeal, pctOverride = null) {
    const pctInput = panel.querySelector(`[data-custom-pct="${itemId}"]`);
    const durInput = panel.querySelector(`[data-dur="${itemId}"]`);
    const noteInput = panel.querySelector(`[data-note="${itemId}"]`);
    const pct = isDeal ? (pctOverride ?? (parseInt(pctInput?.value || "0", 10) || 0)) : 0;
    const durVal = durInput?.value ? parseInt(durInput.value, 10) : null;
    const noteVal = noteInput?.value?.trim() || null;

    const res = await apiFetch(`/manager/menu/${itemId}/surplus-deal`, {
      method: "POST",
      body: JSON.stringify({
        is_surplus_deal: isDeal,
        discount_percent: pct,
        surplus_note: isDeal ? noteVal : null,
        expires_in_minutes: isDeal ? durVal : null,
      }),
    });
    if (res.ok) {
      showToast(
        isDeal ? "Surplus Deal Active" : "Surplus Deal Ended",
        isDeal ? `${res.data.name}: ${res.data.active_discount_percent}% OFF (${formatPaise(res.data.effective_price_paise)})` : `${res.data.name} restored to base price`,
        "success"
      );
      await loadManagerDashboard();
    } else {
      showToast("Surplus Update Failed", res.data?.message || res.data?.detail || `HTTP ${res.status}`, "error");
    }
  }

  panel.querySelectorAll("[data-quick-pct]").forEach((btn) => {
    btn.onclick = () => submitSurplus(btn.getAttribute("data-item-id"), true, parseInt(btn.getAttribute("data-quick-pct"), 10));
  });
  panel.querySelectorAll("[data-apply-surplus]").forEach((btn) => {
    btn.onclick = () => submitSurplus(btn.getAttribute("data-apply-surplus"), true, null);
  });
  panel.querySelectorAll("[data-end-surplus]").forEach((btn) => {
    btn.onclick = () => submitSurplus(btn.getAttribute("data-end-surplus"), false, 0);
  });
}

function showMenuItemModal(existingItem = null) {
  const old = document.getElementById("menu-item-modal");
  if (old) old.remove();

  const isEdit = existingItem !== null;
  const modal = document.createElement("div");
  modal.id = "menu-item-modal";
  modal.className = "fixed inset-0 z-[200] flex items-center justify-center bg-slate-900/60 backdrop-blur-sm p-4";
  modal.innerHTML = `
    <div class="bg-white dark:bg-slate-900 rounded-2xl shadow-2xl p-6 w-full max-w-md space-y-4">
      <div class="flex items-center justify-between border-b border-slate-100 dark:border-slate-800 pb-3">
        <div class="font-bold text-lg text-slate-900 dark:text-slate-100">${isEdit ? `Edit ${existingItem.name}` : "Add Menu Item"}</div>
        <button id="mi-close" class="text-slate-400 hover:text-slate-700 font-bold" type="button">X</button>
      </div>
      <div class="space-y-3 text-sm">
        <div>
          <label class="block text-xs font-semibold text-slate-500 mb-1">Item Name</label>
          <input id="mi-name" type="text" value="${existingItem?.name || ""}" class="w-full rounded-lg border-slate-300 dark:border-slate-600 text-sm" />
        </div>
        <div class="grid grid-cols-2 gap-3">
          <div>
            <label class="block text-xs font-semibold text-slate-500 mb-1">Category</label>
            <input id="mi-category" type="text" value="${existingItem?.category || "General"}" class="w-full rounded-lg border-slate-300 dark:border-slate-600 text-sm" />
          </div>
          <div>
            <label class="block text-xs font-semibold text-slate-500 mb-1">Price (Rs)</label>
            <input id="mi-price" type="number" min="1" value="${existingItem ? Math.round(existingItem.price_paise / 100) : 60}" class="w-full rounded-lg border-slate-300 dark:border-slate-600 text-sm font-mono" />
          </div>
        </div>
        <div class="grid grid-cols-2 gap-3">
          <div>
            <label class="block text-xs font-semibold text-slate-500 mb-1">Stock Count</label>
            <input id="mi-stock" type="number" min="0" value="${existingItem?.stock_count ?? 25}" class="w-full rounded-lg border-slate-300 dark:border-slate-600 text-sm font-mono" />
          </div>
          <div>
            <label class="block text-xs font-semibold text-slate-500 mb-1">Prep Time (mins)</label>
            <input id="mi-prep" type="number" min="1" max="120" value="${existingItem ? 5 : 5}" class="w-full rounded-lg border-slate-300 dark:border-slate-600 text-sm font-mono" />
          </div>
        </div>
        <div>
          <label class="block text-xs font-semibold text-slate-500 mb-1">Description</label>
          <input id="mi-desc" type="text" value="${existingItem?.description || ""}" class="w-full rounded-lg border-slate-300 dark:border-slate-600 text-sm" />
        </div>
        <label class="flex items-center gap-2 text-xs font-semibold text-slate-700 dark:text-slate-300">
          <input id="mi-avail" type="checkbox" ${existingItem ? (existingItem.is_available ? "checked" : "") : "checked"} class="rounded border-slate-300 text-sky-600" />
          <span>Available for ordering</span>
        </label>
      </div>
      <div class="flex gap-3 pt-2">
        <button id="mi-cancel" class="flex-1 py-2.5 rounded-lg bg-white dark:bg-slate-800 border border-slate-300 dark:border-slate-600 text-slate-700 dark:text-slate-200 text-sm font-bold" type="button">Cancel</button>
        <button id="mi-save" class="flex-1 py-2.5 rounded-lg bg-sky-600 hover:bg-sky-700 text-white text-sm font-bold" type="button">${isEdit ? "Save Changes" : "Create Item"}</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);

  function closeMi() { modal.remove(); }
  modal.querySelector("#mi-close").onclick = closeMi;
  modal.querySelector("#mi-cancel").onclick = closeMi;
  modal.onclick = (e) => { if (e.target === modal) closeMi(); };

  modal.querySelector("#mi-save").onclick = async () => {
    const body = {
      name: modal.querySelector("#mi-name").value.trim(),
      category: modal.querySelector("#mi-category").value.trim() || "General",
      description: modal.querySelector("#mi-desc").value.trim(),
      price_paise: Math.max(100, (parseInt(modal.querySelector("#mi-price").value || "1", 10) || 1) * 100),
      stock_count: Math.max(0, parseInt(modal.querySelector("#mi-stock").value || "0", 10) || 0),
      estimated_prep_minutes: Math.max(1, parseInt(modal.querySelector("#mi-prep").value || "5", 10) || 5),
      is_available: modal.querySelector("#mi-avail").checked,
    };
    const res = await apiFetch(
      isEdit ? `/manager/menu/${existingItem.id}` : "/manager/menu",
      { method: isEdit ? "PATCH" : "POST", body: JSON.stringify(body) }
    );
    if (res.ok) {
      closeMi();
      showToast(isEdit ? "Menu Item Updated" : "Menu Item Created", `${res.data.name} saved`, "success");
      await loadManagerDashboard();
    } else {
      showToast("Menu Save Failed", res.data?.message || res.data?.detail || `HTTP ${res.status}`, "error");
    }
  };
}

function renderMenuEditorPanel() {
  const dashWrapper = document.querySelector("main")?.firstElementChild;
  if (!dashWrapper) return;
  let panel = document.getElementById("manager-menu-editor");
  if (!panel) {
    panel = document.createElement("div");
    panel.id = "manager-menu-editor";
    panel.className = "px-margin-desktop mt-space-xl space-y-4";
    dashWrapper.appendChild(panel);
  }

  const categories = ["ALL", ...new Set(state.managerMenuItems.map((i) => i.category || "General"))];
  const filtered = state.managerMenuItems.filter((item) => {
    const matchesCat = state.managerMenuCategory === "ALL" || item.category === state.managerMenuCategory;
    const q = state.managerMenuSearch.trim().toLowerCase();
    const matchesQ = !q || item.name.toLowerCase().includes(q) || (item.category || "").toLowerCase().includes(q);
    return matchesCat && matchesQ;
  });

  panel.innerHTML = `
    <div class="rounded-xl bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-700 p-space-lg shadow-sm space-y-4">
      <div class="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 dark:border-slate-800 pb-4">
        <div>
          <h2 class="font-headline-md text-headline-md text-slate-900 dark:text-slate-100 font-bold">Menu Management</h2>
          <p class="text-xs text-slate-500 dark:text-slate-400">Create, edit, toggle stock, or archive menu items for your restaurant.</p>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <input id="mm-search" type="text" placeholder="Search menu..." value="${state.managerMenuSearch}" class="rounded-lg border-slate-300 dark:border-slate-600 text-xs w-40" />
          <select id="mm-cat" class="rounded-lg border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-900 text-xs text-slate-700 dark:text-slate-200">
            ${categories.map((c) => `<option value="${c}" ${state.managerMenuCategory === c ? "selected" : ""}>${c}</option>`).join("")}
          </select>
          <button id="mm-add-btn" class="px-4 py-2 rounded-lg bg-sky-600 hover:bg-sky-700 text-white text-xs font-bold" type="button">+ Add Menu Item</button>
        </div>
      </div>
      <div class="space-y-2">
        ${filtered.map((item) => `
          <div class="p-3 rounded-xl bg-slate-50 dark:bg-slate-800/70 border border-slate-200 dark:border-slate-700 flex flex-wrap items-center justify-between gap-3">
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2">
                <span class="font-semibold text-slate-900 dark:text-slate-100">${item.name}</span>
                <span class="px-2 py-0.5 rounded bg-slate-200 dark:bg-slate-700 text-slate-700 dark:text-slate-200 text-[10px] font-bold">${item.category}</span>
                <span class="px-2 py-0.5 rounded ${item.is_available ? "bg-emerald-50 text-emerald-700 border border-emerald-200" : "bg-rose-50 text-rose-700 border border-rose-200"} text-[10px] font-bold">${item.is_available ? "Available" : "Out of Stock"}</span>
              </div>
              <div class="text-xs text-slate-500 dark:text-slate-400">${formatPaise(item.price_paise)} • Stock: <span class="font-mono font-bold">${item.stock_count}</span></div>
            </div>
            <div class="flex flex-wrap items-center gap-1.5">
              <button data-mm-stock="${item.id}" data-delta="-1" class="px-2.5 py-1 rounded bg-white dark:bg-slate-900 border border-slate-300 dark:border-slate-600 text-xs font-bold" type="button">-1</button>
              <button data-mm-stock="${item.id}" data-delta="1" class="px-2.5 py-1 rounded bg-white dark:bg-slate-900 border border-slate-300 dark:border-slate-600 text-xs font-bold" type="button">+1</button>
              <button data-mm-toggle="${item.id}" class="px-2.5 py-1 rounded bg-white dark:bg-slate-900 border border-slate-300 dark:border-slate-600 text-xs font-bold" type="button">${item.is_available ? "Mark Unavailable" : "Mark Available"}</button>
              <button data-mm-edit="${item.id}" class="px-2.5 py-1 rounded bg-sky-50 dark:bg-sky-900/40 border border-sky-200 dark:border-sky-700 text-sky-700 dark:text-sky-300 text-xs font-bold" type="button">Edit</button>
              <button data-mm-archive="${item.id}" class="px-2.5 py-1 rounded bg-rose-50 dark:bg-rose-950/60 border border-rose-200 dark:border-rose-800 text-rose-700 dark:text-rose-300 text-xs font-bold" type="button">Archive</button>
            </div>
          </div>
        `).join("")}
      </div>
    </div>
  `;

  panel.querySelector("#mm-add-btn").onclick = () => showMenuItemModal(null);
  panel.querySelector("#mm-search").oninput = (e) => {
    state.managerMenuSearch = e.target.value;
    renderMenuEditorPanel();
  };
  panel.querySelector("#mm-cat").onchange = (e) => {
    state.managerMenuCategory = e.target.value;
    renderMenuEditorPanel();
  };
  panel.querySelectorAll("[data-mm-stock]").forEach((b) => {
    b.onclick = async () => {
      const id = b.getAttribute("data-mm-stock");
      const delta = parseInt(b.getAttribute("data-delta"), 10);
      const item = state.managerMenuItems.find((i) => i.id === id);
      if (!item) return;
      const res = await apiFetch(`/manager/menu/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ stock_count: Math.max(0, item.stock_count + delta) }),
      });
      if (res.ok) await loadManagerDashboard();
    };
  });
  panel.querySelectorAll("[data-mm-toggle]").forEach((b) => {
    b.onclick = async () => {
      const id = b.getAttribute("data-mm-toggle");
      const item = state.managerMenuItems.find((i) => i.id === id);
      if (!item) return;
      const res = await apiFetch(`/manager/menu/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ is_available: !item.is_available }),
      });
      if (res.ok) await loadManagerDashboard();
    };
  });
  panel.querySelectorAll("[data-mm-edit]").forEach((b) => {
    b.onclick = () => {
      const item = state.managerMenuItems.find((i) => i.id === b.getAttribute("data-mm-edit"));
      if (item) showMenuItemModal(item);
    };
  });
  panel.querySelectorAll("[data-mm-archive]").forEach((b) => {
    b.onclick = async () => {
      const id = b.getAttribute("data-mm-archive");
      const item = state.managerMenuItems.find((i) => i.id === id);
      if (!item || !window.confirm(`Archive ${item.name}? Historical orders remain intact.`)) return;
      const res = await apiFetch(`/manager/menu/${id}`, { method: "DELETE" });
      if (res.ok) {
        showToast("Item Archived", `${item.name} archived safely`, "warn");
        await loadManagerDashboard();
      }
    };
  });
}

function renderEscalations() {
  const col = document.querySelector(".xl\\:col-span-5");
  if (!col) return;
  const queueBadge = col.querySelector(".bg-amber-50.border-amber-200.text-amber-700.rounded-full");
  if (queueBadge) queueBadge.textContent = `Tier 2 Queue (${state.escalations.length})`;

  const existingCards = col.querySelectorAll(".rounded-xl.bg-white");
  existingCards.forEach((el) => el.remove());

  if (state.escalations.length === 0) {
    const empty = document.createElement("div");
    empty.className = "rounded-xl bg-white border border-slate-200 p-space-lg shadow-sm text-center text-slate-500";
    empty.innerHTML = `<div class="font-semibold text-slate-800 mb-1">All Escalations Resolved</div><div class="text-sm">No Tier 2 refunds currently pending review. Use the Judge Demo Simulator to trigger a new case.</div>`;
    col.appendChild(empty);
    return;
  }

  const primary = state.escalations[state.selectedEscalationIndex];
  const fb = primary.factor_breakdown || {};
  const sPrep = Number(fb.s_prep_norm ?? 0).toFixed(2);
  const vUser = Number(fb.v_user_norm ?? 0).toFixed(2);
  const lSurge = Number(fb.l_surge ?? 0).toFixed(2);
  const itemsLabel = (primary.items || []).map((i) => `${i.quantity}x ${i.name}`).join(", ");
  const ageMin = Math.floor((primary.age_seconds || 0) / 60);
  const ageSec = (primary.age_seconds || 0) % 60;

  const mainCard = document.createElement("div");
  mainCard.className = "relative rounded-xl bg-white border border-slate-200 p-space-lg shadow-sm overflow-hidden transition-all duration-300";
  mainCard.innerHTML = `
    <div class="absolute top-0 left-0 right-0 h-1 ${primary.is_overdue ? "bg-rose-600" : "bg-amber-500"}"></div>
    <div class="flex items-start justify-between gap-space-sm mb-space-md mt-1">
      <div>
        <div class="flex items-center gap-space-sm mb-1">
          <span class="font-headline-md text-headline-md font-bold text-slate-900 font-telemetry-md">${primary.order_number}</span>
          <span class="px-2 py-0.5 rounded bg-amber-50 border border-amber-200 font-telemetry-xs text-telemetry-xs text-amber-700 font-semibold uppercase">${primary.is_overdue ? "SLA OVERDUE" : "UNDER REVIEW"}</span>
        </div>
        <div class="flex items-center gap-2 text-slate-500 font-body-sm text-body-sm">
          <span class="font-semibold text-slate-900">${primary.student_name}</span>
          <span>•</span>
          <span class="font-telemetry-xs font-mono">${primary.student_email || "Verified Student"}</span>
          <span>•</span>
          <span class="text-slate-900 font-bold font-telemetry-sm">${formatPaise(primary.total_paise)}</span>
        </div>
      </div>
      <div class="text-right">
        <span class="font-telemetry-xs text-slate-500 block">Initiated</span>
        <span class="font-telemetry-sm text-amber-600 font-mono font-bold">${ageMin}m ${ageSec}s ago</span>
      </div>
    </div>
    <div class="p-space-sm rounded-lg bg-slate-50 border border-slate-100 mb-space-md">
      <div class="font-body-md text-slate-900 font-medium">${itemsLabel || "Canteen Order"}</div>
      <div class="font-telemetry-xs text-slate-500">Stage at Request: ${primary.prep_stage_at_request} • 7d Refunds: ${primary.refund_count_7d}</div>
    </div>
    <div class="mb-space-md space-y-2">
      <div class="flex items-center justify-between">
        <span class="font-telemetry-xs text-slate-600 uppercase tracking-wider font-semibold">Risk Factor Decomposition</span>
        <span class="font-telemetry-sm text-amber-600 font-mono font-bold">R = ${Number(primary.risk_score).toFixed(2)}</span>
      </div>
      <div class="grid grid-cols-3 gap-1.5 font-telemetry-xs">
        <div class="p-2.5 rounded-lg bg-rose-50/60 border border-rose-200 flex flex-col gap-0.5"><span class="text-rose-700 font-mono font-medium">S_prep (0.50)</span><span class="text-rose-600 font-mono font-bold text-telemetry-sm">${sPrep}</span><span class="text-rose-500 text-[10px]">${primary.prep_stage_at_request}</span></div>
        <div class="p-2.5 rounded-lg bg-emerald-50/60 border border-emerald-200 flex flex-col gap-0.5"><span class="text-emerald-700 font-mono font-medium">V_user (0.30)</span><span class="text-emerald-600 font-mono font-bold text-telemetry-sm">${vUser}</span><span class="text-emerald-500 text-[10px]">${primary.refund_count_7d} in 7d</span></div>
        <div class="p-2.5 rounded-lg bg-amber-50/60 border border-amber-200 flex flex-col gap-0.5"><span class="text-amber-700 font-mono font-medium">L_surge (0.20)</span><span class="text-amber-600 font-mono font-bold text-telemetry-sm">${lSurge}</span><span class="text-amber-600 text-[10px]">${Number(lSurge) > 0 ? "Peak surge" : "Off-peak"}</span></div>
      </div>
    </div>
    <div class="p-space-sm rounded-lg bg-slate-50 border border-slate-100 mb-space-lg">
      <div class="flex items-start gap-2">
        <span class="material-symbols-outlined text-amber-600 text-[18px] mt-0.5">info</span>
        <div class="font-body-sm text-slate-600 leading-relaxed"><span class="text-slate-900 font-semibold">Engine Explanation:</span> ${primary.human_message || primary.reason_code || "Manager review required."}</div>
      </div>
    </div>
    <div class="grid grid-cols-2 gap-space-sm">
      <button id="btn-approve-esc" class="min-h-[48px] px-space-md py-3 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white font-bold flex items-center justify-center gap-2 transition-all active:scale-95 shadow-sm" type="button">
        <span class="material-symbols-outlined text-[20px]">check_circle</span><span>Approve Refund</span>
      </button>
      <button id="btn-reject-esc" class="min-h-[48px] px-space-md py-3 rounded-lg bg-rose-600 hover:bg-rose-700 text-white font-bold flex items-center justify-center gap-2 transition-all active:scale-95 shadow-sm" type="button">
        <span class="material-symbols-outlined text-[20px]">cancel</span><span>Reject Claim</span>
      </button>
    </div>
  `;
  col.appendChild(mainCard);

  mainCard.querySelector("#btn-approve-esc").onclick = () => resolveEscalation(primary.refund_id, "APPROVE");
  mainCard.querySelector("#btn-reject-esc").onclick = () => resolveEscalation(primary.refund_id, "REJECT");

  state.escalations.forEach((item, idx) => {
    if (idx === state.selectedEscalationIndex) return;
    const mini = document.createElement("div");
    mini.className = "rounded-xl bg-white border border-slate-200 p-space-md shadow-sm hover:shadow transition-all flex items-center justify-between";
    mini.innerHTML = `
      <div class="flex items-center gap-space-sm min-w-0">
        <div class="w-10 h-10 rounded-lg bg-slate-100 border border-slate-200 flex items-center justify-center font-telemetry-xs font-mono font-bold text-sky-700 flex-shrink-0">${item.order_number.replace("GC-", "#")}</div>
        <div class="min-w-0">
          <div class="font-body-md text-slate-900 font-semibold truncate">${item.student_name} • ${formatPaise(item.total_paise)}</div>
          <div class="font-telemetry-xs text-slate-500">R = ${Number(item.risk_score).toFixed(2)} (${item.prep_stage_at_request})</div>
        </div>
      </div>
      <button class="min-h-[48px] px-space-md rounded-lg bg-slate-100 hover:bg-slate-200 border border-slate-200 text-slate-700 font-telemetry-xs font-bold uppercase transition-all" type="button">Review</button>
    `;
    mini.querySelector("button").onclick = () => {
      state.selectedEscalationIndex = idx;
      renderEscalations();
    };
    col.appendChild(mini);
  });
}

async function resolveEscalation(refundId, action) {
  const res = await apiFetch(`/manager/escalations/${refundId}/decision`, {
    method: "POST",
    body: JSON.stringify({ action, notes: `Manager ${action} from Dashboard` }),
  });
  if (res.ok) {
    showToast(
      action === "APPROVE" ? "Refund Approved" : "Claim Rejected",
      `Order transitioned to ${res.data.order_status}.`,
      action === "APPROVE" ? "success" : "error"
    );
    await loadManagerDashboard();
  } else {
    showToast("Decision Failed", res.data?.message || "Could not resolve escalation", "error");
  }
}

function renderAuditLogs() {
  const col = document.querySelector(".xl\\:col-span-7");
  if (!col) return;
  const container = col.querySelector(".rounded-xl.bg-white.border.border-slate-200.shadow-sm.overflow-hidden");
  if (!container) return;

  if (state.auditLogs.length === 0) {
    container.innerHTML = `<div class="p-space-lg text-center text-slate-500">No audit entries match this filter.</div>`;
    return;
  }

  container.innerHTML = state.auditLogs
    .map((entry, idx) => {
      const badgeColor =
        entry.decision === "GREEN" || entry.decision === "AUTO_APPROVE" || entry.decision === "MANAGER_APPROVE"
          ? "bg-emerald-50 border-emerald-200 text-emerald-700"
          : entry.decision === "RED" || entry.decision === "BLOCK" || entry.decision === "MANAGER_REJECT"
          ? "bg-rose-50 border-rose-200 text-rose-700"
          : "bg-amber-50 border-amber-200 text-amber-700";
      const timeOnly = (entry.created_at_ist || "").split(", ")[1] || entry.created_at;
      const riskStr = entry.risk_score !== null && entry.risk_score !== undefined ? entry.risk_score.toFixed(2) : "-";
      return `
        <div class="p-space-md hover:bg-slate-50/70 transition-colors flex flex-col gap-2 ${idx % 2 === 1 ? "bg-slate-50/40" : "bg-white"}">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <div class="flex items-center gap-2">
              <span class="font-telemetry-xs text-slate-400 font-mono">${timeOnly}</span>
              <span class="font-telemetry-sm text-slate-900 font-mono font-bold">${entry.order_number || "-"}</span>
              <span class="px-2 py-0.5 rounded border font-telemetry-xs font-bold uppercase ${badgeColor}">${entry.decision}</span>
              <span class="px-1.5 py-0.5 rounded bg-slate-100 font-telemetry-xs text-slate-600 font-mono">${entry.tier}: ${entry.latency_ms.toFixed(1)}ms</span>
            </div>
            <div class="flex items-center gap-2">
              <span class="font-telemetry-xs text-slate-400 font-mono">Risk R:</span>
              <span class="font-telemetry-sm text-slate-800 font-mono font-bold">${riskStr}</span>
              <span class="font-telemetry-xs text-slate-900 font-bold">${entry.total_paise ? formatPaise(entry.total_paise) : ""}</span>
            </div>
          </div>
          <p class="font-body-sm text-slate-600"><span class="text-slate-900 font-medium">${entry.reason_code}:</span> ${entry.human_message}</p>
        </div>
      `;
    })
    .join("");
}

function wireAuditFiltersAndModal() {
  const filterRow = document.querySelector(".xl\\:col-span-7 .flex.items-center.gap-2");
  if (filterRow) {
    const map = ["all", "auto", "escalated", "replay"];
    const buttons = filterRow.querySelectorAll("button");
    buttons.forEach((btn, idx) => {
      const labels = ["All", "Auto-Approved", "Escalated", "Replay Blocked"];
      btn.textContent = labels[idx];
      btn.onclick = async () => {
        state.auditFilter = map[idx];
        buttons.forEach((b, bIdx) => {
          b.className =
            bIdx === idx
              ? "px-space-md py-2 rounded-lg bg-sky-600 text-white font-telemetry-xs font-bold uppercase shadow-sm"
              : "px-space-md py-2 rounded-lg bg-white border border-slate-200 hover:bg-slate-50 text-slate-600 font-telemetry-xs font-semibold uppercase transition-all";
        });
        const res = await apiFetch(buildAuditUrl());
        if (res.ok) {
          state.auditLogs = res.data.items || [];
          renderAuditLogs();
        }
      };
    });
  }

  window.toggleJsonModal = () => {
    const modal = document.getElementById("json-modal");
    if (!modal) return;
    const pre = modal.querySelector("pre");
    if (pre && state.auditLogs.length > 0) {
      pre.textContent = JSON.stringify(state.auditLogs[0], null, 2);
    }
    if (modal.classList.contains("hidden")) {
      modal.classList.remove("hidden");
      modal.classList.add("flex");
    } else {
      modal.classList.remove("flex");
      modal.classList.add("hidden");
    }
  };
}

function setupSimulatorDrawer() {
  const simBtn = document.querySelector("aside.fixed.bottom-5.right-5 button");
  if (!simBtn) return;

  const panel = document.createElement("div");
  panel.id = "sim-panel";
  panel.className = "fixed bottom-20 right-5 z-50 w-96 rounded-2xl bg-white border border-slate-200 shadow-2xl p-space-md hidden flex-col gap-3";
  panel.innerHTML = `
    <div class="flex items-center justify-between border-b border-slate-100 pb-2">
      <div class="font-bold text-slate-900">Judge Demo Simulator (Live API)</div>
      <button id="sim-close" class="text-slate-400 hover:text-slate-700 font-bold" type="button">✕</button>
    </div>
    <label class="flex items-center gap-2 text-sm text-slate-700">
      <input id="sim-surge-toggle" type="checkbox" checked class="rounded border-slate-300 text-sky-600" />
      <span>Force Peak Lunch Surge (12:00-13:00 IST)</span>
    </label>
    <div class="grid grid-cols-2 gap-2 text-xs font-bold">
      <button data-sc="normal_pickup" class="sim-run px-3 py-2.5 rounded-lg bg-emerald-50 text-emerald-700 border border-emerald-200 hover:bg-emerald-100" type="button">1. Normal Pickup</button>
      <button data-sc="qr_replay" class="sim-run px-3 py-2.5 rounded-lg bg-rose-50 text-rose-700 border border-rose-200 hover:bg-rose-100" type="button">2. QR Replay Attack</button>
      <button data-sc="pre_prep_cancel" class="sim-run px-3 py-2.5 rounded-lg bg-sky-50 text-sky-700 border border-sky-200 hover:bg-sky-100" type="button">3. Pre-Prep Cancel</button>
      <button data-sc="serial_abuse" class="sim-run px-3 py-2.5 rounded-lg bg-amber-50 text-amber-700 border border-amber-200 hover:bg-amber-100" type="button">4. Serial Abuse</button>
    </div>
    <pre id="sim-output" class="bg-slate-900 text-sky-300 text-[11px] font-mono p-2.5 rounded-lg max-h-48 overflow-y-auto">Click a scenario to execute against the live backend.</pre>
  `;
  document.body.appendChild(panel);

  simBtn.onclick = () => {
    panel.classList.toggle("hidden");
    panel.classList.toggle("flex");
  };
  panel.querySelector("#sim-close").onclick = () => {
    panel.classList.add("hidden");
    panel.classList.remove("flex");
  };

  panel.querySelectorAll(".sim-run").forEach((btn) => {
    btn.onclick = async () => {
      await ensureAuth();
      const sc = btn.getAttribute("data-sc");
      const surge = panel.querySelector("#sim-surge-toggle").checked;
      const out = panel.querySelector("#sim-output");
      out.textContent = `Running ${sc}...`;
      const res = await apiFetch(`/demo/simulate/${sc}`, {
        method: "POST",
        body: JSON.stringify({ surge_override: surge }),
      });
      if (res.ok) {
        const d = res.data;
        const fr = d.final_result;
        out.textContent = JSON.stringify(
          {
            scenario: d.scenario,
            total_latency_ms: d.total_latency_ms,
            steps: d.steps.map((s) => `${s.step_name}: ${s.status_code} (${s.latency_ms}ms)`),
            decision: fr.decision || fr.flash,
            risk_score: fr.risk_score ?? null,
            message: fr.human_message || fr.message,
          },
          null,
          2
        );
        showToast(
          `Simulated: ${sc}`,
          `Completed in ${d.total_latency_ms.toFixed(1)}ms (${fr.decision || fr.flash})`,
          fr.flash === "RED" || fr.decision === "BLOCK" ? "error" : "success"
        );
        await loadManagerDashboard();
      } else {
        out.textContent = JSON.stringify(res.data, null, 2);
      }
    };
  });
}

function renderQrInto(el, token, sizePx = 112) {
  if (!el || !token) return;
  try {
    if (typeof qrcode === "undefined") {
      el.innerHTML = `<div class="text-[10px] text-rose-600 font-mono p-2">QR lib loading</div>`;
      return;
    }
    const qr = qrcode(0, "L");
    qr.addData(token);
    qr.make();
    const svg = qr.createSvgTag({ scalable: true, margin: 2 });
    el.classList.add("qr-light-lock", "bg-white", "text-black", "dark:bg-white", "dark:text-black");
    el.style.backgroundColor = "#ffffff";
    el.style.color = "#000000";
    el.innerHTML = `<div class="qr-light-lock bg-white text-black dark:bg-white dark:text-black" style="width:${sizePx}px;height:${sizePx}px;background-color:#ffffff;color:#000000;">${svg}</div>`;
    const svgEl = el.querySelector("svg");
    if (svgEl) {
      svgEl.setAttribute("width", "100%");
      svgEl.setAttribute("height", "100%");
      svgEl.style.display = "block";
      svgEl.style.backgroundColor = "#ffffff";
    }
  } catch (err) {
    console.error("QR render error:", err);
    el.innerHTML = `<div class="text-[10px] text-rose-600 font-mono p-2">QR Error</div>`;
  }
}

function showQrModal(order, container) {
  const existing = document.getElementById("qr-order-modal");
  if (existing) existing.remove();

  const expAt = order.qr_expires_at || "";
  const modal = document.createElement("div");
  modal.id = "qr-order-modal";
  modal.className = "fixed inset-0 z-[200] flex items-center justify-center bg-slate-900/60 backdrop-blur-sm";
  modal.innerHTML = `
    <div class="bg-white rounded-2xl shadow-2xl p-7 w-full max-w-sm flex flex-col items-center gap-4 relative">
      <div class="w-full flex items-center justify-between">
        <div class="font-bold text-lg text-slate-900">${order.order_number}</div>
        <span class="px-2 py-0.5 rounded bg-emerald-50 border border-emerald-200 text-emerald-700 text-xs font-bold uppercase">PAID</span>
      </div>
      <div id="modal-qr-box" class="qr-light-lock bg-white text-black dark:bg-white dark:text-black p-4 rounded-xl border-2 border-slate-200 shadow-sm"></div>
      <div class="flex items-center gap-2">
        <span class="text-xs font-semibold text-slate-500 uppercase">Expires in:</span>
        <span id="modal-qr-timer" data-exp="${expAt}" class="px-2 py-0.5 rounded font-mono text-sm font-bold bg-emerald-50 text-emerald-700 border border-emerald-200">--:--</span>
      </div>
      <div class="w-full p-2 rounded-lg bg-slate-900 text-sky-300 font-mono text-[10px] break-all">${order.qr_token || ""}</div>
      <div class="flex gap-3 w-full">
        <button id="modal-copy-btn" class="flex-1 py-2.5 rounded-lg bg-white border border-slate-300 hover:bg-slate-100 text-slate-700 text-sm font-bold" type="button">Copy Token</button>
        <button id="modal-close-btn" class="flex-1 py-2.5 rounded-lg bg-sky-600 hover:bg-sky-700 text-white text-sm font-bold" type="button">Done</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);

  const qrBox = modal.querySelector("#modal-qr-box");
  if (order.qr_token) {
    renderQrInto(qrBox, order.qr_token, 200);
  }

  let timerIv = null;
  function tickModal() {
    const el = modal.querySelector("#modal-qr-timer");
    if (!el) return;
    const rem = Math.max(0, Math.floor((Date.parse(expAt) - Date.now()) / 1000));
    if (rem <= 0) {
      el.textContent = "EXPIRED";
      el.className = "px-2 py-0.5 rounded font-mono text-sm font-bold bg-rose-50 text-rose-700 border border-rose-200";
    } else {
      el.textContent = `${String(Math.floor(rem / 60)).padStart(2, "0")}:${String(rem % 60).padStart(2, "0")}`;
      el.className = rem <= 30
        ? "px-2 py-0.5 rounded font-mono text-sm font-bold bg-amber-50 text-amber-700 border border-amber-200"
        : "px-2 py-0.5 rounded font-mono text-sm font-bold bg-emerald-50 text-emerald-700 border border-emerald-200";
    }
  }
  if (expAt) { tickModal(); timerIv = setInterval(tickModal, 1000); }

  function closeModal() {
    if (timerIv) clearInterval(timerIv);
    modal.remove();
    renderRoleView("student-app", container);
  }

  modal.querySelector("#modal-close-btn").onclick = closeModal;
  modal.querySelector("#modal-copy-btn").onclick = async () => {
    if (order.qr_token && navigator.clipboard) await navigator.clipboard.writeText(order.qr_token);
    showToast("Token Copied", "Paste into Counter Scanner to verify");
  };
  modal.onclick = (e) => { if (e.target === modal) closeModal(); };
}

async function showCheckoutModal(item, container) {
  const existing = document.getElementById("checkout-modal");
  if (existing) existing.remove();

  await ensureAuth();
  const wRes = await apiFetch("/wallet/me", {}, state.studentToken);
  const wallet = wRes.ok ? wRes.data : (state.wallet || { gold_coins: 0 });
  state.wallet = wallet;

  const effPricePaise = item.effective_price_paise ?? item.price_paise;
  const surplusSavingsPaise = Math.max(0, item.price_paise - effPricePaise);
  const maxCoins = Math.min(wallet.gold_coins || 0, Math.floor(effPricePaise / 100));
  let useCoins = maxCoins > 0 && state.useGoldCoins;
  let coinsToUse = useCoins ? maxCoins : 0;

  const modal = document.createElement("div");
  modal.id = "checkout-modal";
  modal.className = "fixed inset-0 z-[200] flex items-center justify-center bg-slate-900/60 backdrop-blur-sm p-4";
  modal.innerHTML = `
    <div class="bg-white dark:bg-slate-900 rounded-2xl shadow-2xl p-6 w-full max-w-md space-y-4">
      <div class="flex items-center justify-between border-b border-slate-100 dark:border-slate-800 pb-3">
        <div class="font-bold text-lg text-slate-900 dark:text-slate-100">Checkout: ${item.name}</div>
        <button id="co-close" class="text-slate-400 hover:text-slate-700 font-bold" type="button">X</button>
      </div>
      <div class="p-3 rounded-xl bg-amber-50/70 dark:bg-amber-950/40 border border-amber-200 dark:border-amber-800 space-y-3">
        <div class="flex items-center justify-between">
          <label class="flex items-center gap-2 text-sm font-bold text-slate-800 dark:text-slate-200 cursor-pointer">
            <input id="co-toggle-coins" type="checkbox" ${useCoins ? "checked" : ""} ${maxCoins === 0 ? "disabled" : ""} class="rounded border-slate-300 text-amber-600" />
            <span>Use Gold Coins</span>
          </label>
          <span class="text-xs font-mono font-bold text-amber-700 dark:text-amber-300">Available: ${wallet.gold_coins || 0} Coins</span>
        </div>
        <div class="flex items-center justify-between">
          <span class="text-xs text-slate-600 dark:text-slate-300">Coins to redeem (max ${maxCoins})</span>
          <div class="flex items-center gap-2">
            <button id="co-minus" class="w-8 h-8 rounded-lg bg-white dark:bg-slate-800 border border-slate-300 dark:border-slate-600 font-bold text-slate-700 dark:text-slate-200" type="button">-</button>
            <input id="co-coins-input" type="number" min="0" max="${maxCoins}" value="${coinsToUse}" class="w-16 text-center rounded-lg border-slate-300 dark:border-slate-600 font-mono text-sm font-bold" />
            <button id="co-plus" class="w-8 h-8 rounded-lg bg-white dark:bg-slate-800 border border-slate-300 dark:border-slate-600 font-bold text-slate-700 dark:text-slate-200" type="button">+</button>
          </div>
        </div>
      </div>
      <div class="p-3 rounded-xl bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 space-y-1.5 text-sm">
        <div class="flex justify-between text-slate-600 dark:text-slate-300"><span>Base Price</span><span class="font-mono font-semibold">${formatPaise(item.price_paise)}</span></div>
        ${surplusSavingsPaise > 0 ? `<div class="flex justify-between text-emerald-700 dark:text-emerald-400"><span>Surplus Discount (${item.active_discount_percent}% OFF)</span><span class="font-mono font-semibold">-${formatPaise(surplusSavingsPaise)}</span></div>` : ""}
        <div class="flex justify-between text-amber-700 dark:text-amber-300"><span>Gold Coins Discount</span><span id="co-discount" class="font-mono font-semibold">-Rs 0</span></div>
        <div class="flex justify-between text-slate-900 dark:text-slate-100 font-bold text-base pt-1.5 border-t border-slate-200 dark:border-slate-700"><span>Net Payable</span><span id="co-net" class="font-mono">${formatPaise(effPricePaise)}</span></div>
      </div>
      <div class="flex gap-3">
        <button id="co-cancel" class="flex-1 py-2.5 rounded-lg bg-white dark:bg-slate-800 border border-slate-300 dark:border-slate-600 text-slate-700 dark:text-slate-200 text-sm font-bold" type="button">Cancel</button>
        <button id="co-submit" class="flex-1 py-2.5 rounded-lg bg-sky-600 hover:bg-sky-700 text-white text-sm font-bold" type="button">Place Order</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);

  const toggleEl = modal.querySelector("#co-toggle-coins");
  const inputEl = modal.querySelector("#co-coins-input");
  const discEl = modal.querySelector("#co-discount");
  const netEl = modal.querySelector("#co-net");
  const submitBtn = modal.querySelector("#co-submit");

  function syncTotals() {
    const val = useCoins ? Math.max(0, Math.min(maxCoins, parseInt(inputEl.value || "0", 10) || 0)) : 0;
    coinsToUse = val;
    inputEl.value = String(val);
    inputEl.disabled = !useCoins || maxCoins === 0;
    const discPaise = val * 100;
    const netPaise = Math.max(0, effPricePaise - discPaise);
    discEl.textContent = `-${formatPaise(discPaise)}`;
    netEl.textContent = formatPaise(netPaise);
    submitBtn.textContent = netPaise === 0 ? "Pay with Gold Coins" : `Pay ${formatPaise(netPaise)}`;
  }

  toggleEl.onchange = () => {
    useCoins = toggleEl.checked;
    if (useCoins && coinsToUse === 0) inputEl.value = String(maxCoins);
    syncTotals();
  };
  inputEl.oninput = syncTotals;
  modal.querySelector("#co-minus").onclick = () => {
    if (!useCoins) return;
    inputEl.value = String(Math.max(0, (parseInt(inputEl.value || "0", 10) || 0) - 1));
    syncTotals();
  };
  modal.querySelector("#co-plus").onclick = () => {
    if (!useCoins && maxCoins > 0) {
      useCoins = true;
      toggleEl.checked = true;
    }
    inputEl.value = String(Math.min(maxCoins, (parseInt(inputEl.value || "0", 10) || 0) + 1));
    syncTotals();
  };
  syncTotals();

  function closeCo() { modal.remove(); }
  modal.querySelector("#co-close").onclick = closeCo;
  modal.querySelector("#co-cancel").onclick = closeCo;
  modal.onclick = (e) => { if (e.target === modal) closeCo(); };

  submitBtn.onclick = async () => {
    submitBtn.disabled = true;
    try {
      const res = await apiFetch("/orders", {
        method: "POST",
        body: JSON.stringify({
          items: [{ menu_item_id: item.id, quantity: 1 }],
          coins_to_use: coinsToUse,
        }),
      }, state.studentToken);
      if (res.ok) {
        closeCo();
        showToast("Order Placed", `${res.data.order_number} PAID with dynamic QR`);
        showQrModal(res.data, container);
      } else {
        showToast("Order Failed", res.data?.message || res.data?.detail || `HTTP ${res.status}`, "error");
        submitBtn.disabled = false;
      }
    } catch (err) {
      showToast("Order Error", String(err), "error");
      submitBtn.disabled = false;
    }
  };
}

function showCancelConfirmModal(orderId, orderNumber, container) {
  const existing = document.getElementById("cancel-confirm-modal");
  if (existing) existing.remove();

  const modal = document.createElement("div");
  modal.id = "cancel-confirm-modal";
  modal.className = "fixed inset-0 z-[200] flex items-center justify-center bg-slate-900/60 backdrop-blur-sm p-4";
  modal.innerHTML = `
    <div class="bg-white rounded-2xl shadow-2xl p-6 w-full max-w-md space-y-4">
      <div class="font-bold text-lg text-slate-900">Cancel Order ${orderNumber}?</div>
      <p class="text-sm text-slate-600 leading-relaxed">Approved cancellations credit Gold Coins instantly to your Galley Wallet for your next order.</p>
      <div class="flex gap-3 pt-2">
        <button id="cc-keep" class="flex-1 py-2.5 rounded-lg bg-white border border-slate-300 text-slate-700 text-sm font-bold" type="button">Keep Order</button>
        <button id="cc-confirm" disabled class="flex-1 py-2.5 rounded-lg bg-rose-600 text-white text-sm font-bold opacity-60 cursor-not-allowed" type="button">Confirm Cancel (3s)</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);

  const confirmBtn = modal.querySelector("#cc-confirm");
  let secLeft = 3;
  const iv = setInterval(() => {
    secLeft -= 1;
    if (secLeft <= 0) {
      clearInterval(iv);
      confirmBtn.disabled = false;
      confirmBtn.classList.remove("opacity-60", "cursor-not-allowed");
      confirmBtn.textContent = "Confirm Cancel";
    } else {
      confirmBtn.textContent = `Confirm Cancel (${secLeft}s)`;
    }
  }, 1000);

  function closeModal() {
    clearInterval(iv);
    modal.remove();
  }
  modal.querySelector("#cc-keep").onclick = closeModal;
  modal.onclick = (e) => { if (e.target === modal) closeModal(); };

  confirmBtn.onclick = async () => {
    confirmBtn.disabled = true;
    try {
      await ensureAuth();
      const res = await apiFetch(`/orders/${orderId}/cancel`, { method: "POST" }, state.studentToken);
      if (res.ok && res.data) {
        const decision = res.data.decision || res.data.status || "DONE";
        const msg = res.data.human_message || res.data.message || "Order cancelled.";
        showToast(`Cancel: ${decision}`, msg, decision === "BLOCK" ? "error" : "warn");
      } else {
        const msg = res.data?.message || res.data?.detail || `HTTP ${res.status}`;
        showToast("Cancel Failed", msg, "error");
      }
    } catch (err) {
      showToast("Cancel Error", String(err), "error");
    }
    closeModal();
    renderRoleView("student-app", container);
  };
}

function setupRoleViews() {
  const mainEl = document.querySelector("main");
  const dashboardWrapper = mainEl.firstElementChild;

  const extraView = document.createElement("div");
  extraView.id = "role-view-container";
  extraView.className = "hidden px-margin-desktop pt-space-lg pb-20";
  mainEl.appendChild(extraView);

  const navLinks = document.querySelectorAll("aside nav a[data-path]");
  const headerLinks = document.querySelectorAll("header .md\\:flex a");
  const pathOrder = ["manager-dashboard", "student-app", "counter-scanner", "kitchen-kds"];

  function activateView(path) {
    state.activeView = path;
    navLinks.forEach((a) => {
      const isCurrent = a.getAttribute("data-path") === path;
      a.className = isCurrent
        ? "min-h-[48px] flex items-center px-space-md py-2.5 rounded-lg transition-all bg-sky-50 text-sky-700 border border-sky-200/60 font-semibold shadow-sm"
        : "min-h-[48px] flex items-center px-space-md py-2.5 rounded-lg text-slate-600 hover:bg-slate-100 hover:text-slate-900 font-body-md text-body-md transition-all";
    });
    headerLinks.forEach((a, idx) => {
      const isCurrent = pathOrder[idx] === path;
      a.className = isCurrent
        ? "min-h-[48px] px-3 py-1.5 rounded-md bg-white text-sky-700 border border-slate-200 font-semibold shadow-sm flex items-center gap-1.5"
        : "min-h-[48px] px-3 py-1.5 rounded-md text-slate-600 hover:text-slate-900 hover:bg-slate-200/60 transition-all flex items-center gap-1.5";
    });

    if (path === "manager-dashboard") {
      dashboardWrapper.classList.remove("hidden");
      extraView.classList.add("hidden");
      loadManagerDashboard();
    } else {
      dashboardWrapper.classList.add("hidden");
      extraView.classList.remove("hidden");
      renderRoleView(path, extraView);
    }
  }

  navLinks.forEach((a) => {
    a.onclick = (e) => {
      e.preventDefault();
      activateView(a.getAttribute("data-path"));
    };
  });
  headerLinks.forEach((a, idx) => {
    a.onclick = (e) => {
      e.preventDefault();
      activateView(pathOrder[idx]);
    };
  });
}

async function renderRoleView(path, container) {
  try {
    await ensureAuth();
    if (path === "student-app") {
      const restRes = await apiFetch("/restaurants", {}, null);
      state.restaurants = restRes.ok && Array.isArray(restRes.data) ? restRes.data : [];
      if (!state.selectedRestaurantId && state.restaurants.length > 0) {
        state.selectedRestaurantId = state.restaurants[0].id;
      }
      const menuPath = state.selectedRestaurantId ? `/restaurants/${state.selectedRestaurantId}/menu` : "/menu";
      const [menuRes, ordersRes, walletRes] = await Promise.all([
        apiFetch(menuPath, {}, null),
        state.studentToken ? apiFetch("/orders/me?limit=10", {}, state.studentToken) : Promise.resolve({ ok: false }),
        state.studentToken ? apiFetch("/wallet/me", {}, state.studentToken) : Promise.resolve({ ok: false }),
      ]);
      state.menuItems = menuRes.ok && Array.isArray(menuRes.data) ? menuRes.data : [];
      state.myOrders = ordersRes.ok && ordersRes.data?.items ? ordersRes.data.items : [];
      state.wallet = walletRes.ok && walletRes.data
        ? walletRes.data
        : { gold_coins: 0, loyalty_points: 0, gold_coins_value_paise: 0, convertible_coins_available: 0, transactions: [] };
    const w = state.wallet;
    const waitMin = state.menuItems[0]?.current_wait_minutes ?? 0;
    const milestonePct = (w.loyalty_points % 10) * 10;
    const txTypes = [
      { key: "ALL", label: "All" },
      { key: "REFUND_CREDITED_COINS", label: "Refunds" },
      { key: "POINTS_EARNED_PICKUP", label: "Earned" },
      { key: "POINTS_CONVERTED_TO_COINS", label: "Converted" },
      { key: "ORDER_SPENT_COINS", label: "Spent" },
    ];
    const filteredTxs = (w.transactions || []).filter(
      (tx) => state.walletFilter === "ALL" || tx.tx_type === state.walletFilter
    );
    container.innerHTML = `
      <div class="mb-6 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 class="font-headline-lg text-2xl font-bold text-slate-900">Student Terminal (student01@campus.edu)</h1>
          <p class="text-sm text-slate-500">Live kitchen wait estimate: <span class="font-bold text-sky-700">${waitMin} mins</span></p>
        </div>
        <a href="#student-wallet-panel" class="px-4 py-2 rounded-full bg-amber-50 border border-amber-200 hover:bg-amber-100 text-xs font-bold flex items-center gap-2 text-slate-800 shadow-sm">
          <span class="text-amber-700 font-mono">${w.gold_coins} Gold Coins</span>
          <span class="text-slate-300">|</span>
          <span class="text-sky-700 font-mono">${w.loyalty_points} Pts</span>
        </a>
      </div>
      <div id="student-wallet-panel" class="mb-6 rounded-xl bg-white border border-slate-200 p-5 shadow-sm space-y-4">
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div class="p-4 rounded-xl bg-amber-50/70 border border-amber-200 flex items-center justify-between gap-4">
            <div>
              <div class="text-xs font-semibold text-amber-800 uppercase">Galley Gold Coins</div>
              <div class="text-2xl font-bold text-amber-700 font-mono">${w.gold_coins} Coins</div>
              <div class="text-xs text-slate-600">Purchasing power: ${formatPaise(w.gold_coins_value_paise)} (1 Gold Coin = Rs 1)</div>
            </div>
            <a href="#student-menu-panel" class="px-3.5 py-2 rounded-lg bg-amber-600 hover:bg-amber-700 text-white text-xs font-bold">Order Food</a>
          </div>
          <div class="p-4 rounded-xl bg-sky-50/70 border border-sky-200 space-y-2">
            <div class="flex items-center justify-between">
              <div>
                <div class="text-xs font-semibold text-sky-800 uppercase">Loyalty Points</div>
                <div class="text-2xl font-bold text-sky-700 font-mono">${w.loyalty_points} Pts</div>
              </div>
              <button id="btn-convert-points" ${w.convertible_coins_available > 0 ? "" : "disabled"} class="px-3.5 py-2 rounded-lg ${w.convertible_coins_available > 0 ? "bg-sky-600 hover:bg-sky-700 text-white" : "bg-slate-200 text-slate-400 cursor-not-allowed"} text-xs font-bold" type="button">
                ${w.convertible_coins_available > 0 ? `Convert to ${w.convertible_coins_available} Gold Coins` : "Convert to Gold Coins"}
              </button>
            </div>
            <div class="w-full h-2 rounded-full bg-sky-100 overflow-hidden">
              <div class="h-full bg-sky-600 transition-all" style="width:${milestonePct}%"></div>
            </div>
            <div class="flex justify-between text-[11px] text-slate-600">
              <span>${w.loyalty_points % 10}/10 toward next Gold Coin</span>
              <span>Convertible: ${w.convertible_coins_available} Coins</span>
            </div>
          </div>
        </div>
        <div class="pt-2 border-t border-slate-100 space-y-2">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <div class="text-xs font-semibold text-slate-500 uppercase">Wallet Ledger</div>
            <div class="flex flex-wrap gap-1.5">
              ${txTypes.map((t) => `
                <button data-wallet-filter="${t.key}" class="px-2.5 py-1 rounded-md text-[11px] font-bold ${state.walletFilter === t.key ? "bg-sky-600 text-white" : "bg-slate-100 text-slate-600 hover:bg-slate-200"}" type="button">${t.label}</button>
              `).join("")}
            </div>
          </div>
          ${filteredTxs.length > 0 ? `
            <div class="divide-y divide-slate-100 max-h-48 overflow-y-auto">
              ${filteredTxs.slice(0, 10).map((tx) => `
                <div class="py-2 flex items-center justify-between gap-3 text-xs">
                  <div class="min-w-0">
                    <div class="font-medium text-slate-800 truncate">${tx.description}</div>
                    <div class="text-[10px] text-slate-400 font-mono">${tx.tx_type} • ${tx.created_at_ist || tx.created_at}</div>
                  </div>
                  <div class="text-right flex-shrink-0">
                    <div class="font-mono font-bold ${tx.coins_delta > 0 ? "text-emerald-600" : tx.coins_delta < 0 ? "text-rose-600" : "text-sky-700"}">
                      ${tx.coins_delta !== 0 ? `${tx.coins_delta > 0 ? "+" : ""}${tx.coins_delta} Coins` : `${tx.points_delta > 0 ? "+" : ""}${tx.points_delta} Points`}
                    </div>
                    <div class="text-[10px] text-slate-400 font-mono">Bal: ${tx.balance_coins_after}C / ${tx.balance_points_after}P</div>
                  </div>
                </div>
              `).join("")}
            </div>
          ` : `<div class="text-xs text-slate-400 py-2">No wallet transactions in this filter yet.</div>`}
        </div>
      </div>
      <div id="restaurant-selector" class="mb-6 space-y-2">
        <div class="text-xs font-semibold text-slate-500 uppercase">Campus Restaurants</div>
        <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
          ${state.restaurants.map((r) => `
            <button data-select-restaurant="${r.id}" class="p-3.5 rounded-xl border text-left transition-all ${state.selectedRestaurantId === r.id ? "bg-sky-50 dark:bg-sky-950/50 border-sky-400 shadow-sm" : "bg-white dark:bg-slate-900 border-slate-200 dark:border-slate-700 hover:border-sky-300"}" type="button">
              <div class="flex items-center justify-between gap-2">
                <span class="font-bold text-sm text-slate-900 dark:text-slate-100">${r.name}</span>
                ${r.active_surplus_deals_count > 0 ? `<span class="px-2 py-0.5 rounded bg-emerald-100 text-emerald-800 text-[10px] font-bold">${r.active_surplus_deals_count} Deals</span>` : ""}
              </div>
              <div class="text-xs text-slate-500 dark:text-slate-400 mt-0.5">${r.location_tag} • Wait: ${r.current_wait_minutes}m • ${r.active_items_count} items</div>
            </button>
          `).join("")}
        </div>
      </div>
      <div id="student-menu-panel" class="space-y-6">
        ${state.menuItems.some((m) => m.is_surplus_active && m.stock_count > 0) ? `
          <div id="surplus-deals-banner" class="rounded-xl bg-emerald-50/90 dark:bg-emerald-950/40 border border-emerald-200 dark:border-emerald-800 p-5 shadow-sm space-y-3">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <div>
                <h2 class="font-bold text-lg text-emerald-900 dark:text-emerald-200">Zero-Waste Surplus Deals</h2>
                <p class="text-xs text-emerald-700 dark:text-emerald-300">Limited-time discounts on fresh excess batches. Stackable with Gold Coins at checkout.</p>
              </div>
              <span class="px-2.5 py-1 rounded-full bg-emerald-600 text-white text-[11px] font-bold uppercase">Live Savings</span>
            </div>
            <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
              ${state.menuItems.filter((m) => m.is_surplus_active && m.stock_count > 0).map((m) => `
                <div class="p-3.5 rounded-xl bg-white dark:bg-slate-900 border border-emerald-200 dark:border-emerald-800 flex items-center justify-between gap-3 shadow-sm">
                  <div class="min-w-0">
                    <div class="flex items-center gap-2">
                      <span class="font-semibold text-slate-900 dark:text-slate-100 truncate">${m.name}</span>
                      <span class="px-1.5 py-0.5 rounded bg-emerald-100 dark:bg-emerald-900/60 text-emerald-800 dark:text-emerald-200 text-[10px] font-bold">${m.active_discount_percent}% OFF</span>
                    </div>
                    <div class="flex items-baseline gap-2 mt-0.5">
                      <span class="font-mono font-bold text-emerald-700 dark:text-emerald-400 text-sm">${formatPaise(m.effective_price_paise)}</span>
                      <span class="font-mono text-xs text-slate-400 line-through">${formatPaise(m.price_paise)}</span>
                      <span class="text-[11px] text-slate-500">Stock: ${m.stock_count}</span>
                    </div>
                    ${m.surplus_note ? `<div class="text-[11px] text-emerald-700 dark:text-emerald-300 truncate">${m.surplus_note}</div>` : ""}
                  </div>
                  <button data-order-item="${m.id}" class="px-3 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white text-xs font-bold flex-shrink-0" type="button">Claim Deal</button>
                </div>
              `).join("")}
            </div>
          </div>
        ` : ""}
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div class="bg-white rounded-xl border border-slate-200 p-5 shadow-sm">
          <div class="flex items-center justify-between mb-3">
            <h2 class="font-bold text-lg text-slate-900">Live Canteen Menu</h2>
            <label class="flex items-center gap-2 text-xs font-semibold text-slate-700 cursor-pointer">
              <input id="chk-use-coins" type="checkbox" ${state.useGoldCoins ? "checked" : ""} ${w.gold_coins > 0 ? "" : "disabled"} class="rounded border-slate-300 text-amber-600" />
              <span>Default Use Coins (${w.gold_coins})</span>
            </label>
          </div>
          <div class="space-y-2.5">
            ${state.menuItems.map((m) => `
              <div class="flex items-center justify-between p-3 rounded-lg bg-slate-50 border border-slate-100">
                <div>
                  <div class="flex items-center gap-2">
                    <span class="font-semibold text-slate-900">${m.name}</span>
                    ${m.is_surplus_active ? `<span class="px-1.5 py-0.5 rounded bg-emerald-100 dark:bg-emerald-900/60 text-emerald-800 dark:text-emerald-200 text-[10px] font-bold">${m.active_discount_percent}% OFF</span>` : ""}
                  </div>
                  <div class="text-xs text-slate-500">
                    ${m.is_surplus_active ? `<span class="font-bold text-emerald-700 dark:text-emerald-400">${formatPaise(m.effective_price_paise)}</span> <span class="line-through text-slate-400">${formatPaise(m.price_paise)}</span>` : formatPaise(m.price_paise)} • Stock: ${m.stock_count}
                  </div>
                </div>
                <button data-order-item="${m.id}" class="px-4 py-2 rounded-lg bg-sky-600 hover:bg-sky-700 text-white text-xs font-bold" type="button">Order 1x</button>
              </div>
            `).join("")}
          </div>
        </div>
        <div class="bg-white rounded-xl border border-slate-200 p-5 shadow-sm">
          <h2 class="font-bold text-lg text-slate-900 mb-3">My Orders & Dynamic QR Tokens</h2>
          <div class="space-y-3">
            ${state.myOrders.map((o) => `
              <div class="p-3 rounded-lg border border-slate-200 space-y-2">
                <div class="flex items-center justify-between gap-2">
                  <span class="font-mono font-bold text-slate-900">${o.order_number}</span>
                  <div class="flex items-center gap-1.5">
                    ${o.restaurant_name ? `<span class="px-2 py-0.5 rounded bg-slate-100 border border-slate-200 text-slate-700 text-[10px] font-semibold">${o.restaurant_name}</span>` : ""}
                    ${o.status === "COLLECTED" && Math.floor((o.cash_payable_paise || 0) / 1000) > 0 ? `<span class="px-2 py-0.5 rounded bg-sky-50 border border-sky-200 text-sky-700 text-[11px] font-bold">+${Math.floor(o.cash_payable_paise / 1000)} Pts</span>` : ""}
                    ${o.status === "REFUNDED" && o.refund_coins_credited > 0 ? `<span class="px-2 py-0.5 rounded bg-amber-50 border border-amber-200 text-amber-700 text-[11px] font-bold">+${o.refund_coins_credited} Gold Coins</span>` : ""}
                    <span class="px-2 py-0.5 rounded bg-slate-100 text-xs font-bold">${o.status} / ${o.prep_stage}</span>
                  </div>
                </div>
                <div class="text-xs text-slate-600">${formatPaise(o.total_paise)}${o.coins_redeemed > 0 ? ` (${o.coins_redeemed} Coins + ${formatPaise(o.cash_payable_paise)} Cash)` : ""} • ${o.items.map((i) => `${i.quantity}x ${i.item_name_snapshot}`).join(", ")}</div>
                ${
                  o.qr_token
                    ? `<div class="flex items-center gap-4 p-3 rounded-lg bg-slate-50 border border-slate-200">
                        <div data-qr-box="${o.id}" class="qr-light-lock bg-white text-black dark:bg-white dark:text-black p-3 rounded-xl border border-slate-200 flex-shrink-0"></div>
                        <div class="min-w-0 flex-1 space-y-1.5">
                          <div class="flex items-center gap-2">
                            <span class="text-xs font-semibold text-slate-500 uppercase">QR Timer:</span>
                            <span data-qr-timer="${o.qr_expires_at}" class="px-2 py-0.5 rounded font-mono text-xs font-bold bg-emerald-50 text-emerald-700 border border-emerald-200">--:--</span>
                          </div>
                          <div class="p-1.5 rounded bg-slate-900 text-sky-300 font-mono text-[10px] truncate">${o.qr_token}</div>
                          <button data-copy-qr="${o.qr_token}" class="px-2.5 py-1 rounded bg-white border border-slate-300 hover:bg-slate-100 text-slate-700 text-[11px] font-bold" type="button">Copy Token</button>
                        </div>
                      </div>`
                    : ""
                }
                <div class="flex gap-2">
                  ${o.status === "PAID" ? `<button data-refresh="${o.id}" class="px-3 py-1.5 rounded bg-sky-50 text-sky-700 border border-sky-200 text-xs font-bold" type="button">Refresh QR</button>` : ""}
                  ${o.status === "PAID" || o.status === "CREATED" ? `<button data-cancel="${o.id}" data-order-number="${o.order_number}" class="px-3 py-1.5 rounded bg-rose-50 text-rose-700 border border-rose-200 text-xs font-bold" type="button">Cancel Order</button>` : ""}
                </div>
              </div>
            `).join("")}
          </div>
        </div>
      </div>
      </div>
    `;
    if (state.qrTimerInterval) {
      clearInterval(state.qrTimerInterval);
      state.qrTimerInterval = null;
    }
    state.myOrders.forEach((o) => {
      if (!o.qr_token) return;
      const box = container.querySelector(`[data-qr-box="${o.id}"]`);
      if (box) renderQrInto(box, o.qr_token, 112);
    });
    function updateQrTimers() {
      const nowMs = Date.now();
      container.querySelectorAll("[data-qr-timer]").forEach((el) => {
        const expMs = Date.parse(el.getAttribute("data-qr-timer"));
        const remSec = Math.max(0, Math.floor((expMs - nowMs) / 1000));
        if (remSec <= 0) {
          el.textContent = "EXPIRED";
          el.className = "px-2 py-0.5 rounded font-mono text-xs font-bold bg-rose-50 text-rose-700 border border-rose-200";
        } else {
          const mins = String(Math.floor(remSec / 60)).padStart(2, "0");
          const secs = String(remSec % 60).padStart(2, "0");
          el.textContent = `${mins}:${secs}`;
          el.className = remSec <= 30
            ? "px-2 py-0.5 rounded font-mono text-xs font-bold bg-amber-50 text-amber-700 border border-amber-200"
            : "px-2 py-0.5 rounded font-mono text-xs font-bold bg-emerald-50 text-emerald-700 border border-emerald-200";
        }
      });
    }
    updateQrTimers();
    state.qrTimerInterval = setInterval(updateQrTimers, 1000);
    container.querySelectorAll("[data-copy-qr]").forEach((b) => {
      b.onclick = async () => {
        const tok = b.getAttribute("data-copy-qr");
        if (navigator.clipboard) await navigator.clipboard.writeText(tok);
        showToast("Token Copied", "Paste it into Counter Scanner to verify");
      };
    });
    container.querySelectorAll("[data-select-restaurant]").forEach((b) => {
      b.onclick = () => {
        state.selectedRestaurantId = b.getAttribute("data-select-restaurant");
        renderRoleView("student-app", container);
      };
    });
    container.querySelectorAll("[data-wallet-filter]").forEach((b) => {
      b.onclick = () => {
        state.walletFilter = b.getAttribute("data-wallet-filter");
        renderRoleView("student-app", container);
      };
    });
    const chkUseCoins = container.querySelector("#chk-use-coins");
    if (chkUseCoins) {
      chkUseCoins.onchange = () => { state.useGoldCoins = chkUseCoins.checked; };
    }
    const btnConvert = container.querySelector("#btn-convert-points");
    if (btnConvert) {
      btnConvert.onclick = async () => {
        const coinsToMint = state.wallet?.convertible_coins_available || 0;
        if (coinsToMint < 1) return;
        btnConvert.disabled = true;
        try {
          const res = await apiFetch("/wallet/convert-points", {
            method: "POST",
            body: JSON.stringify({ coins_to_mint: coinsToMint }),
          }, state.studentToken);
          if (res.ok) {
            showToast("Points Converted", `Minted ${coinsToMint} Gold Coins`);
            renderRoleView("student-app", container);
          } else {
            showToast("Conversion Failed", res.data?.message || res.data?.detail || `HTTP ${res.status}`, "error");
          }
        } catch (err) {
          showToast("Conversion Error", String(err), "error");
        }
      };
    }
    container.querySelectorAll("[data-order-item]").forEach((b) => {
      b.onclick = () => {
        const itemId = b.getAttribute("data-order-item");
        const item = state.menuItems.find((m) => m.id === itemId);
        if (item) showCheckoutModal(item, container);
      };
    });
    container.querySelectorAll("[data-refresh]").forEach((b) => {
      b.onclick = async () => {
        b.disabled = true;
        try {
          await ensureAuth();
          const res = await apiFetch(`/orders/${b.getAttribute("data-refresh")}/refresh-qr`, { method: "POST" }, state.studentToken);
          if (res.ok) {
            showToast("QR Refreshed", "Old nonce revoked; new QR issued");
            renderRoleView("student-app", container);
          } else {
            showToast("Refresh Failed", res.data?.message || res.data?.detail || `HTTP ${res.status}`, "error");
          }
        } catch (err) {
          showToast("Refresh Error", String(err), "error");
        } finally {
          b.disabled = false;
        }
      };
    });
    container.querySelectorAll("[data-cancel]").forEach((b) => {
      b.onclick = () => {
        showCancelConfirmModal(
          b.getAttribute("data-cancel"),
          b.getAttribute("data-order-number") || "",
          container
        );
      };
    });
  } else if (path === "counter-scanner") {
    container.innerHTML = `
      <div class="mb-6">
        <h1 class="font-headline-lg text-2xl font-bold text-slate-900">Counter Scanner Terminal (COUNTER01)</h1>
        <p class="text-sm text-slate-500">Scan the student's QR code with camera, or paste the token manually, to verify and redeem in &lt;150ms.</p>
      </div>
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div class="bg-white rounded-xl border border-slate-200 p-5 shadow-sm space-y-3">
          <h2 class="font-bold text-lg text-slate-900">Verify Dynamic QR Token</h2>
          <div class="rounded-xl border border-slate-200 overflow-hidden bg-slate-900 relative" id="cam-wrapper" style="display:none">
            <video id="cam-video" autoplay playsinline muted class="w-full" style="max-height:220px;object-fit:cover;"></video>
            <canvas id="cam-canvas" class="hidden"></canvas>
            <div class="absolute top-2 right-2">
              <button id="btn-stop-cam" class="px-3 py-1.5 rounded-lg bg-rose-600 text-white text-xs font-bold" type="button">Stop Camera</button>
            </div>
            <div id="cam-status" class="absolute bottom-2 left-2 px-2 py-0.5 rounded bg-slate-800/80 text-sky-300 text-[11px] font-mono">Scanning…</div>
          </div>
          <button id="btn-start-cam" class="w-full px-5 py-2.5 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white font-bold text-sm flex items-center justify-center gap-2" type="button">
            <span class="material-symbols-outlined text-[18px]">photo_camera</span> Scan with Camera
          </button>
          <textarea id="qr-input" rows="3" placeholder="Paste base64url qr_token from Student Terminal..." class="w-full rounded-lg border-slate-300 font-mono text-xs"></textarea>
          <button id="btn-verify-qr" class="px-5 py-2.5 rounded-lg bg-sky-600 hover:bg-sky-700 text-white font-bold text-sm" type="button">Scan &amp; Verify QR</button>
          <pre id="verify-result" class="bg-slate-900 text-sky-300 text-xs font-mono p-3 rounded-lg min-h-[120px] hidden"></pre>
        </div>
        <div class="bg-white rounded-xl border border-slate-200 p-5 shadow-sm space-y-3">
          <h2 class="font-bold text-lg text-slate-900">Manual Collect Override (HUMAN_OVERRIDE)</h2>
          <input id="mc-order" type="text" placeholder="Order Number (e.g. GC-1051)" class="w-full rounded-lg border-slate-300 text-sm" />
          <input id="mc-reason" type="text" placeholder="Mandatory reason (e.g. Phone battery dead)" class="w-full rounded-lg border-slate-300 text-sm" />
          <button id="btn-manual-collect" class="px-5 py-2.5 rounded-lg bg-amber-600 hover:bg-amber-700 text-white font-bold text-sm" type="button">Manual Collect</button>
        </div>
      </div>
    `;

    let camStream = null;
    let camRafId = null;

    async function doVerify(tok) {
      const res = await apiFetch("/verify-qr", {
        method: "POST",
        body: JSON.stringify({ qr_token: tok }),
      }, state.staffToken);
      const result = container.querySelector("#verify-result");
      result.textContent = JSON.stringify(res.data, null, 2);
      result.classList.remove("hidden");
      showToast(res.data.flash || "Result", res.data.message || "", res.data.flash === "GREEN" ? "success" : "error");
    }

    function stopCamera() {
      if (camRafId) { cancelAnimationFrame(camRafId); camRafId = null; }
      if (camStream) { camStream.getTracks().forEach((t) => t.stop()); camStream = null; }
      const wrapper = container.querySelector("#cam-wrapper");
      const startBtn = container.querySelector("#btn-start-cam");
      if (wrapper) wrapper.style.display = "none";
      if (startBtn) startBtn.style.display = "";
    }

    function scanLoop() {
      const video = container.querySelector("#cam-video");
      const canvas = container.querySelector("#cam-canvas");
      const status = container.querySelector("#cam-status");
      if (!video || !canvas || video.readyState < 2) { camRafId = requestAnimationFrame(scanLoop); return; }
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      const ctx = canvas.getContext("2d");
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      if (typeof jsQR !== "undefined" && canvas.width > 0) {
        const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
        const code = jsQR(img.data, img.width, img.height, { inversionAttempts: "dontInvert" });
        if (code && code.data) {
          if (status) status.textContent = "QR Detected!";
          const qrInput = container.querySelector("#qr-input");
          if (qrInput) qrInput.value = code.data;
          stopCamera();
          doVerify(code.data);
          return;
        }
      }
      camRafId = requestAnimationFrame(scanLoop);
    }

    container.querySelector("#btn-start-cam").onclick = async () => {
      try {
        camStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
        const video = container.querySelector("#cam-video");
        video.srcObject = camStream;
        await video.play();
        container.querySelector("#cam-wrapper").style.display = "";
        container.querySelector("#btn-start-cam").style.display = "none";
        camRafId = requestAnimationFrame(scanLoop);
      } catch (err) {
        showToast("Camera Error", String(err), "error");
      }
    };
    container.querySelector("#btn-stop-cam").onclick = stopCamera;

    container.querySelector("#btn-verify-qr").onclick = async () => {
      const tok = container.querySelector("#qr-input").value.trim();
      await doVerify(tok);
    };
    container.querySelector("#btn-manual-collect").onclick = async () => {
      const order_number = container.querySelector("#mc-order").value.trim();
      const reason = container.querySelector("#mc-reason").value.trim();
      const res = await apiFetch("/verify-qr/manual-collect", {
        method: "POST",
        body: JSON.stringify({ order_number, reason }),
      }, state.staffToken);
      showToast(res.ok ? "Manual Collect Recorded" : "Error", res.data.message, res.ok ? "warn" : "error");
    };
  } else if (path === "kitchen-kds") {
    const res = await apiFetch("/kds/orders", {}, state.staffToken);
    const data = res.ok ? res.data : { orders: [], recently_cancelled: [] };
    container.innerHTML = `
      <div class="mb-6 flex items-center justify-between">
        <div>
          <h1 class="font-headline-lg text-2xl font-bold text-slate-900">Kitchen Display Screen (KDS)</h1>
          <p class="text-sm text-slate-500">Live orders in RECEIVED, IN_PREP, READY, or HOLD (PENDING_REVIEW).</p>
        </div>
        <button id="kds-refresh" class="px-4 py-2 rounded-lg bg-white border border-slate-200 text-sm font-bold text-slate-700" type="button">Refresh Queue</button>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        ${data.orders.map((o) => `
          <div class="rounded-xl bg-white border ${o.hold ? "border-amber-400 bg-amber-50/30" : "border-slate-200"} p-4 shadow-sm space-y-3">
            <div class="flex items-center justify-between">
              <span class="font-mono font-bold text-lg text-slate-900">${o.order_number}</span>
              <span class="px-2 py-0.5 rounded text-xs font-bold ${o.hold ? "bg-amber-100 text-amber-800" : "bg-sky-50 text-sky-700"}">${o.hold ? "HOLD (REVIEW)" : o.prep_stage}</span>
            </div>
            <div class="text-sm text-slate-700">${o.items.map((i) => `${i.quantity}x ${i.name}`).join("<br/>")}</div>
            <div class="flex gap-2">
              ${!o.hold && o.prep_stage === "RECEIVED" ? `<button data-cook="${o.order_id}" class="flex-1 py-2 rounded-lg bg-amber-500 hover:bg-amber-600 text-white text-xs font-bold" type="button">Start Cooking</button>` : ""}
              ${!o.hold && o.prep_stage === "IN_PREP" ? `<button data-ready="${o.order_id}" class="flex-1 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-700 text-white text-xs font-bold" type="button">Mark Ready</button>` : ""}
            </div>
          </div>
        `).join("")}
      </div>
    `;
    container.querySelector("#kds-refresh").onclick = () => renderRoleView("kitchen-kds", container);
    container.querySelectorAll("[data-cook]").forEach((b) => {
      b.onclick = async () => {
        await apiFetch(`/kds/orders/${b.getAttribute("data-cook")}/start-cooking`, { method: "POST" }, state.staffToken);
        renderRoleView("kitchen-kds", container);
      };
    });
    container.querySelectorAll("[data-ready]").forEach((b) => {
      b.onclick = async () => {
        await apiFetch(`/kds/orders/${b.getAttribute("data-ready")}/mark-ready`, { method: "POST" }, state.staffToken);
        renderRoleView("kitchen-kds", container);
      };
    });
  }
  } catch (err) {
    renderConnectionBanner(true, String(err));
    container.innerHTML = `
      <div class="rounded-xl bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-700 p-6 shadow-sm space-y-3">
        <h2 class="font-bold text-lg text-slate-900 dark:text-slate-100">Unable to load ${path}</h2>
        <p class="text-sm text-slate-600 dark:text-slate-300">Active backend node: <span class="font-mono">${API_BASE}</span></p>
        <button id="fallback-node-btn" type="button" class="px-4 py-2 rounded-lg bg-sky-600 hover:bg-sky-700 text-white text-xs font-bold">Configure Backend Node</button>
      </div>
    `;
    const fb = container.querySelector("#fallback-node-btn");
    if (fb) fb.onclick = () => showBackendNodeModal();
  }
}

async function showBackendNodeModal() {
  const old = document.getElementById("backend-node-modal");
  if (old) old.remove();

  const modal = document.createElement("div");
  modal.id = "backend-node-modal";
  modal.className = "fixed inset-0 z-[220] flex items-center justify-center bg-slate-900/60 backdrop-blur-sm p-4";
  modal.innerHTML = `
    <div class="bg-white dark:bg-slate-900 rounded-2xl shadow-2xl p-6 w-full max-w-md space-y-4 border border-slate-200 dark:border-slate-700">
      <div class="flex items-center justify-between border-b border-slate-100 dark:border-slate-800 pb-3">
        <div class="font-bold text-lg text-slate-900 dark:text-slate-100">Backend Node Connector</div>
        <button id="bn-close" class="text-slate-400 hover:text-slate-700 font-bold" type="button">X</button>
      </div>
      <div class="p-3 rounded-xl bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 flex items-center justify-between gap-3">
        <div class="min-w-0">
          <div class="text-[11px] font-semibold text-slate-500 uppercase">Active API Base</div>
          <div id="bn-active-url" class="text-xs font-mono text-slate-800 dark:text-slate-200 truncate">${API_BASE}</div>
        </div>
        <span id="bn-health-badge" class="px-2.5 py-1 rounded-full text-[11px] font-bold bg-slate-200 text-slate-700">CHECKING</span>
      </div>
      <div class="space-y-2">
        <label class="block text-xs font-semibold text-slate-600 dark:text-slate-300">Backend URL (HTTPS Render, Railway, or Tunnel)</label>
        <input id="bn-url-input" type="url" value="${API_BASE}" placeholder="https://your-api.onrender.com/api/v1" class="w-full rounded-lg border-slate-300 dark:border-slate-600 font-mono text-xs" />
      </div>
      <div class="flex gap-2">
        <button id="bn-reset" class="px-3.5 py-2.5 rounded-lg bg-white dark:bg-slate-800 border border-slate-300 dark:border-slate-600 text-slate-700 dark:text-slate-200 text-xs font-bold" type="button">Reset</button>
        <button id="bn-connect" class="flex-1 py-2.5 rounded-lg bg-sky-600 hover:bg-sky-700 text-white text-xs font-bold" type="button">Connect &amp; Test /health</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);

  const badge = modal.querySelector("#bn-health-badge");
  const activeUrlEl = modal.querySelector("#bn-active-url");
  const urlInput = modal.querySelector("#bn-url-input");

  async function checkHealth(baseUrl) {
    badge.textContent = "CHECKING";
    badge.className = "px-2.5 py-1 rounded-full text-[11px] font-bold bg-slate-200 text-slate-700";
    try {
      const resp = await fetch(`${baseUrl}/health`);
      if (resp.ok) {
        badge.textContent = "ONLINE";
        badge.className = "px-2.5 py-1 rounded-full text-[11px] font-bold bg-emerald-100 text-emerald-800 border border-emerald-300";
        renderConnectionBanner(false);
        return true;
      }
    } catch {}
    badge.textContent = "OFFLINE";
    badge.className = "px-2.5 py-1 rounded-full text-[11px] font-bold bg-rose-100 text-rose-800 border border-rose-300";
    renderConnectionBanner(true);
    return false;
  }

  checkHealth(API_BASE);

  function closeModal() { modal.remove(); }
  modal.querySelector("#bn-close").onclick = closeModal;
  modal.onclick = (e) => { if (e.target === modal) closeModal(); };

  modal.querySelector("#bn-reset").onclick = async () => {
    localStorage.removeItem("galley_api_url");
    API_BASE = resolveApiBaseUrl();
    urlInput.value = API_BASE;
    activeUrlEl.textContent = API_BASE;
    state.managerToken = null;
    state.staffToken = null;
    state.studentToken = null;
    await checkHealth(API_BASE);
    if (state.activeView === "manager-dashboard") {
      loadManagerDashboard();
    } else {
      const container = document.getElementById("role-view-container");
      if (container) renderRoleView(state.activeView, container);
    }
  };

  modal.querySelector("#bn-connect").onclick = async () => {
    const normalized = normalizeApiUrl(urlInput.value);
    if (!normalized) return;
    localStorage.setItem("galley_api_url", normalized);
    API_BASE = normalized;
    activeUrlEl.textContent = API_BASE;
    state.managerToken = null;
    state.staffToken = null;
    state.studentToken = null;
    const ok = await checkHealth(API_BASE);
    showToast(
      ok ? "Backend Connected" : "Backend Offline",
      ok ? `Connected to ${API_BASE}` : `Could not reach ${API_BASE}/health`,
      ok ? "success" : "error"
    );
    if (ok) {
      closeModal();
      if (state.activeView === "manager-dashboard") {
        loadManagerDashboard();
      } else {
        const container = document.getElementById("role-view-container");
        if (container) renderRoleView(state.activeView, container);
      }
    }
  };
}

function setupThemeToggle() {
  const buttons = document.querySelectorAll("[data-theme-mode]");
  if (!buttons.length) return;
  const media = window.matchMedia("(prefers-color-scheme: dark)");

  function applyTheme(mode) {
    const isDark = mode === "dark" || (mode === "system" && media.matches);
    document.documentElement.classList.toggle("dark", isDark);
    buttons.forEach((btn) => {
      const active = btn.getAttribute("data-theme-mode") === mode;
      btn.setAttribute("aria-pressed", active ? "true" : "false");
      btn.className = active
        ? "px-2 py-1.5 rounded-md text-xs font-semibold flex items-center gap-1 bg-white dark:bg-slate-900 text-sky-700 dark:text-sky-400 border border-slate-200 dark:border-slate-700 shadow-sm"
        : "px-2 py-1.5 rounded-md text-xs font-semibold flex items-center gap-1 text-slate-600 dark:text-slate-300 hover:text-slate-900 dark:hover:text-white";
    });
  }

  let currentMode = localStorage.getItem("galley_theme") || "system";
  applyTheme(currentMode);

  buttons.forEach((btn) => {
    btn.onclick = () => {
      currentMode = btn.getAttribute("data-theme-mode") || "system";
      localStorage.setItem("galley_theme", currentMode);
      applyTheme(currentMode);
    };
  });

  media.addEventListener("change", () => {
    if ((localStorage.getItem("galley_theme") || "system") === "system") {
      applyTheme("system");
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  setupThemeToggle();
  const coreNodeBtn = document.getElementById("core-node-btn");
  if (coreNodeBtn) coreNodeBtn.onclick = () => showBackendNodeModal();
  wireAuditFiltersAndModal();
  setupSimulatorDrawer();
  setupRoleViews();
  loadManagerDashboard();
});
