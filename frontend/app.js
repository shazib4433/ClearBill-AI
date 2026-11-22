/* ClearBill AI — Clinical Discharge Billing Reviewer Dashboard */

const state = {
  employees: [],
  activeEmployeeUid: "EMP-DEL-101",
  patients: [],
  skills: [],
  currentSession: null,
  activeTab: "staging",
  pendingModalContext: null,
  activeFetchController: null,
  reconcileRequestId: 0,
  loaderStepTimer: null,
};

const BUTTON_DEFAULT_LABELS = {
  "btn-run-selected": "Audit",
  "btn-rerun-uhid": "Re-Run",
  "btn-manual-search": "Go",
};

const $ = (id) => document.getElementById(id);

function escapeHtml(val) {
  return String(val ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatINR(amount) {
  const num = Number(amount || 0);
  return "₹" + num.toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

function friendlyBillState(billState) {
  switch (billState) {
    case "PENDING_AUDIT":
      return "Not Audited";
    case "FLAGGED_STAGING":
      return "Flagged";
    case "VERIFIED_CLEAN":
      return "Ready to Finalize";
    case "FINAL_APPROVED":
      return "Finalized";
    default:
      return billState || "Not Audited";
  }
}

function showGlobalAlert(message, type = "info", action = null) {
  const el = $("global-alert");
  el.classList.remove(
    "hidden",
    "border-l-blue-600",
    "border-l-emerald-600",
    "border-l-rose-600",
    "bg-rose-50",
    "text-rose-800",
    "bg-emerald-50",
    "text-emerald-800",
    "bg-blue-50",
    "text-blue-800"
  );
  el.classList.add("flex", "flex-wrap", "items-center", "justify-between", "gap-2");
  if (type === "error") {
    el.classList.add("border-l-rose-600", "bg-rose-50", "text-rose-800");
  } else if (type === "success") {
    el.classList.add("border-l-emerald-600", "bg-emerald-50", "text-emerald-800");
  } else {
    el.classList.add("border-l-blue-600", "bg-blue-50", "text-blue-800");
  }
  el.innerHTML = "";
  const span = document.createElement("span");
  span.className = "min-w-0 flex-1";
  span.textContent = message;
  el.appendChild(span);

  if (action && action.label && typeof action.onClick === "function") {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "cb-btn cb-btn-outline text-xs py-1 px-2.5 bg-white shrink-0";
    btn.textContent = action.label;
    btn.addEventListener("click", action.onClick);
    el.appendChild(btn);
  }
}

function hideGlobalAlert() {
  $("global-alert").classList.add("hidden");
}

function getEmployeeByUid(uid) {
  const normalized = (uid || "").trim().toUpperCase();
  return state.employees.find((e) => e.employee_uid === normalized) || null;
}

function classificationBadge(classification) {
  switch (classification) {
    case "VERIFIED_CLEAN":
      return `<span class="cb-badge cb-badge-clean">CLEAN</span>`;
    case "FLAGGED_DUPLICATE":
      return `<span class="cb-badge cb-badge-dup">DUPLICATE</span>`;
    case "FLAGGED_UNBUNDLED":
      return `<span class="cb-badge cb-badge-unbundled">UNBUNDLED</span>`;
    case "FLAGGED_NO_MEDICAL_NECESSITY":
      return `<span class="cb-badge cb-badge-necessity">NO NECESSITY</span>`;
    case "FLAGGED_TARIFF_CAP_EXCEEDED":
      return `<span class="cb-badge cb-badge-cap">TARIFF CAP</span>`;
    default:
      return `<span class="cb-badge cb-badge-info">${escapeHtml(classification)}</span>`;
  }
}

function stateBadge(billState) {
  switch (billState) {
    case "FINAL_APPROVED":
      return `<span class="cb-badge cb-badge-clean">FINALIZED</span>`;
    case "VERIFIED_CLEAN":
      return `<span class="cb-badge cb-badge-clean">CLEAN</span>`;
    case "RESTORED_WITH_OVERRIDE":
      return `<span class="cb-badge cb-badge-info">OVERRIDDEN</span>`;
    case "REMOVED_CONFIRMED":
      return `<span class="cb-badge cb-badge-unbundled">REMOVED</span>`;
    case "FLAGGED_STAGING":
      return `<span class="cb-badge cb-badge-dup">STAGING</span>`;
    default:
      return `<span class="cb-badge cb-badge-info">${escapeHtml(friendlyBillState(billState))}</span>`;
  }
}

function renderActiveEmployeeBar() {
  const emp = getEmployeeByUid(state.activeEmployeeUid);
  const meta = $("employee-meta");
  if (!emp) {
    meta.innerHTML = `<span class="text-rose-300">Unknown Reviewer</span>`;
    return;
  }
  const limitClass =
    emp.max_override_limit === 0
      ? "bg-rose-500/20 text-rose-200 border-rose-400/40"
      : emp.max_override_limit === null
      ? "bg-emerald-500/20 text-emerald-200 border-emerald-400/40"
      : "bg-amber-500/20 text-amber-200 border-amber-400/40";
  meta.innerHTML = `
    <span class="cb-badge bg-white/15 text-slate-100 border border-white/25">${escapeHtml(emp.role)}</span>
    <span class="cb-badge border ${limitClass}">Cap: ${escapeHtml(emp.override_limit_display)}</span>
  `;
}

function renderPatientDropdown(selectedUhid = null) {
  const sel = $("uhid-select");
  const prev = selectedUhid || sel.value;
  sel.innerHTML = state.patients
    .map((p) => {
      const label = `${p.uhid} · ${p.payer_category} · ${p.department} (${formatINR(p.gross_billed_amount)}) — ${friendlyBillState(p.bill_state)}`;
      return `<option value="${escapeHtml(p.uhid)}">${escapeHtml(label)}</option>`;
    })
    .join("");
  if (prev && state.patients.some((p) => p.uhid === prev)) {
    sel.value = prev;
  }
}

async function fetchJson(url, options = {}) {
  const res = await fetch(url, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.detail || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

async function refreshPatientsList(preserveUhid = null) {
  state.patients = await fetchJson("/api/patients");
  renderPatientDropdown(preserveUhid);
}

function resetActionButtonLabels() {
  Object.entries(BUTTON_DEFAULT_LABELS).forEach(([id, label]) => {
    const btn = $(id);
    if (btn) {
      btn.textContent = label;
    }
  });
}

function showGlassLoader(title, subtitle, triggerBtnId = null) {
  resetActionButtonLabels();
  if (state.loaderStepTimer) {
    clearInterval(state.loaderStepTimer);
    state.loaderStepTimer = null;
  }

  const titleEl = $("glass-loader-title");
  const subtitleEl = $("glass-loader-subtitle");
  if (titleEl) titleEl.textContent = title;
  if (subtitleEl) subtitleEl.textContent = subtitle;

  const steps = Array.from(document.querySelectorAll(".cb-glass-step"));
  if (steps.length > 0) {
    let idx = 0;
    steps.forEach((s, i) => s.classList.toggle("active", i === 0));
    state.loaderStepTimer = setInterval(() => {
      idx = (idx + 1) % steps.length;
      steps.forEach((s, i) => s.classList.toggle("active", i === idx));
    }, 1100);
  }

  ["btn-run-selected", "btn-rerun-uhid", "btn-manual-search", "uhid-select"].forEach((id) => {
    const el = $(id);
    if (el) el.disabled = true;
  });

  if (triggerBtnId && $(triggerBtnId)) {
    const label = BUTTON_DEFAULT_LABELS[triggerBtnId] || "Loading";
    $(triggerBtnId).innerHTML = `<span class="cb-btn-spinner" aria-hidden="true"></span><span>${escapeHtml(label)}</span>`;
  }

  $("audit-glass-overlay").classList.remove("hidden");
}

function hideGlassLoader() {
  $("audit-glass-overlay").classList.add("hidden");
  if (state.loaderStepTimer) {
    clearInterval(state.loaderStepTimer);
    state.loaderStepTimer = null;
  }
  resetActionButtonLabels();
  ["btn-run-selected", "btn-manual-search", "uhid-select"].forEach((id) => {
    const el = $(id);
    if (el) el.disabled = false;
  });
  const rerunBtn = $("btn-rerun-uhid");
  if (rerunBtn) {
    rerunBtn.disabled = state.currentSession?.bill_state === "FINAL_APPROVED";
  }
}

async function triggerUhidReconciliation(uhid, forceRerun = false, opts = {}) {
  const cleanUhid = (uhid || "").trim().toUpperCase();
  if (!cleanUhid) return;

  if (state.activeFetchController) {
    state.activeFetchController.abort();
  }
  const controller = new AbortController();
  state.activeFetchController = controller;
  const reqId = ++state.reconcileRequestId;

  hideGlobalAlert();
  const title = forceRerun ? `Re-Running Audit · ${cleanUhid}` : `Auditing Bill · ${cleanUhid}`;
  const subtitle = forceRerun
    ? "Re-evaluating discharge items with live clinical rules & preserving audit log decisions..."
    : "Loading staged ledger, payer rules, and clinical justification status...";
  showGlassLoader(title, subtitle, opts.triggerBtnId || null);

  const startedAt = performance.now();
  try {
    const qs = forceRerun ? "?force_rerun=true" : "";
    const session = await fetchJson(`/api/reconcile/uhid/${encodeURIComponent(cleanUhid)}${qs}`, {
      method: "POST",
      signal: controller.signal,
    });
    if (reqId !== state.reconcileRequestId) return;

    const minMs = Number(opts.minDisplayMs || 0);
    const remainingMs = minMs - (performance.now() - startedAt);
    if (remainingMs > 0) {
      await new Promise((resolve) => setTimeout(resolve, remainingMs));
      if (reqId !== state.reconcileRequestId) return;
    }

    state.currentSession = session;
    setActiveTab(session.staging_count > 0 ? "staging" : "clean");
    renderSession();
    await Promise.all([refreshPatientsList(session.uhid), loadAuditLogs()]);
    if (reqId !== state.reconcileRequestId) return;

    if (forceRerun && !session.fallback_notice) {
      const preserved = (session.line_items || []).filter((it) => Boolean(it.last_modified_by_uid)).length;
      const msg =
        preserved > 0
          ? `Re-ran audit for ${session.uhid}; preserved ${preserved} previously audited decision(s) recorded in the Audit Log.`
          : `Re-ran audit for ${session.uhid}.`;
      showGlobalAlert(msg, "info");
    }
  } catch (err) {
    if (err.name === "AbortError" || reqId !== state.reconcileRequestId) return;
    showGlobalAlert(err.message, "error");
  } finally {
    if (reqId === state.reconcileRequestId) {
      state.activeFetchController = null;
      hideGlassLoader();
    }
  }
}

function renderSession() {
  const ses = state.currentSession;
  if (!ses) return;

  $("workspace").classList.remove("hidden");

  if (ses.fallback_notice) {
    const retryAction =
      ses.source_type === "HIS_UHID" && ses.bill_state !== "FINAL_APPROVED"
        ? {
            label: "Retry with Live Gemini",
            onClick: () =>
              triggerUhidReconciliation(ses.uhid, true, {
                triggerBtnId: "btn-rerun-uhid",
                minDisplayMs: 320,
              }),
          }
        : null;
    showGlobalAlert(ses.fallback_notice, "error", retryAction);
  }
  const modePill = $("engine-mode-pill");
  if (modePill) {
    if (ses.engine_mode === "GEMINI_LIVE") {
      modePill.textContent = `Live Gemini (${ses.models_used})`;
    } else if (ses.fallback_notice) {
      modePill.textContent = "Local Fallback (Gemini Unavailable)";
    } else {
      modePill.textContent = "Demo Rule Engine";
    }
  }

  // Compact Patient Context Bar
  const ctxBar = $("patient-context-bar");
  ctxBar.classList.remove("hidden");
  ctxBar.innerHTML = `
    <div class="flex items-center gap-2 flex-wrap">
      <span class="font-bold text-slate-900">${escapeHtml(ses.uhid)}</span>
      <span class="text-slate-400">·</span>
      <span class="font-semibold text-slate-700">${escapeHtml(ses.patient_display_masked)}</span>
      <span class="cb-badge cb-badge-info">${escapeHtml(ses.payer_category)}</span>
      <span class="cb-badge bg-slate-100 text-slate-700 border border-slate-300">${escapeHtml(ses.department)}</span>
      <button id="btn-loaded-skill-pill" class="cb-badge bg-indigo-50 text-indigo-700 border border-indigo-200 cursor-pointer hover:bg-indigo-100">
        Rulebook: ${escapeHtml(ses.skill_loaded)}
      </button>
    </div>
    <div class="text-slate-600 truncate max-w-xl" title="${escapeHtml(ses.primary_diagnosis)}">
      <strong>Dx:</strong> ${escapeHtml(ses.primary_diagnosis)}
    </div>
  `;
  const skillPill = $("btn-loaded-skill-pill");
  if (skillPill) {
    skillPill.addEventListener("click", () => openSkillsModal(ses.skill_loaded));
  }

  // KPI Cards
  $("kpi-total-billed").textContent = formatINR(ses.total_billed_amount);
  $("kpi-net-payable").textContent = `Net Payable: ${formatINR(ses.net_payable_amount)}`;
  $("kpi-potential-savings").textContent = formatINR(ses.potential_savings_amount);
  $("kpi-confirmed-savings").textContent = `Confirmed: ${formatINR(ses.confirmed_savings_amount)}`;
  $("kpi-clean-count").textContent = String(ses.clean_count);
  $("kpi-clean-amount").textContent = formatINR(ses.clean_verified_amount);
  $("kpi-staging-count").textContent = String(ses.staging_count);
  $("kpi-staging-amount").textContent = formatINR(ses.flagged_staging_amount);
  $("kpi-readiness-score").textContent = `${ses.readiness_score}%`;
  $("kpi-readiness-bar").style.width = `${Math.min(100, Math.max(0, ses.readiness_score))}%`;
  $("kpi-bill-state").innerHTML = stateBadge(ses.bill_state);

  // Finalize & Consolidated Final Bill Banner
  const finBanner = $("finalize-banner");
  const finBtn = $("btn-open-finalize");
  const rerunBtn = $("btn-rerun-uhid");
  const pdfBtn = $("btn-download-pdf");
  const printBtn = $("btn-print-bill");
  const finText = $("finalize-status-text");
  finBanner.classList.remove("bg-emerald-50", "border-emerald-300");
  if (rerunBtn) {
    rerunBtn.disabled = ses.bill_state === "FINAL_APPROVED";
  }

  if (ses.bill_state === "FINAL_APPROVED") {
    finBanner.classList.add("bg-emerald-50", "border-emerald-300");
    finText.innerHTML = `
      <span class="font-bold text-emerald-800">Finalized &amp; Locked</span> by
      <strong>${escapeHtml(ses.finalized_by_name)} (${escapeHtml(ses.finalized_by_uid)})</strong>
      &bull; Net Payable: <strong>${formatINR(ses.net_payable_amount)}</strong>
    `;
    finBtn.classList.add("hidden");
    pdfBtn.href = `/api/sessions/${encodeURIComponent(ses.session_id)}/final-bill.pdf`;
    printBtn.href = `/api/sessions/${encodeURIComponent(ses.session_id)}/final-bill/print`;
    pdfBtn.classList.remove("hidden");
    printBtn.classList.remove("hidden");
  } else {
    finBtn.classList.remove("hidden");
    pdfBtn.classList.add("hidden");
    printBtn.classList.add("hidden");
    if (ses.staging_count > 0) {
      finText.innerHTML = `Resolve <strong>${ses.staging_count}</strong> flagged item(s) to unlock Finalize &amp; Consolidated PDF.`;
      finBtn.disabled = true;
    } else {
      finText.innerHTML = `<span class="font-semibold text-emerald-800">Ready to Finalize:</span> All items cleared (100% readiness).`;
      finBtn.disabled = false;
    }
  }

  // Tab Badges & Content
  const stagingItems = ses.line_items.filter((i) => i.bucket === "STAGING");
  const cleanItems = ses.line_items.filter((i) => i.bucket === "CLEAN");
  const removedItems = ses.line_items.filter((i) => i.bucket === "REMOVED");

  $("tab-badge-staging").textContent = String(stagingItems.length);
  $("tab-badge-clean").textContent = String(cleanItems.length);
  $("tab-badge-removed").textContent = String(removedItems.length);

  renderBucket("panel-staging", stagingItems, "staging", ses.bill_state === "FINAL_APPROVED");
  renderBucket("panel-clean", cleanItems, "clean", ses.bill_state === "FINAL_APPROVED");
  renderBucket("panel-removed", removedItems, "removed", ses.bill_state === "FINAL_APPROVED");
}

function renderEvidenceToggle(citations, payerRule) {
  if ((!citations || citations.length === 0) && !payerRule) return "";
  const count = citations ? citations.length : 0;
  const rows = (citations || [])
    .map(
      (c) => `
      <div class="text-xs bg-slate-50 border border-slate-200 rounded px-2.5 py-1.5 text-slate-700">
        <span class="font-bold text-slate-900">[${escapeHtml(c.source_type)} · ${escapeHtml(c.source_id)}]</span>
        ${escapeHtml(c.excerpt)}
      </div>`
    )
    .join("");
  return `
    <details class="cb-evidence-toggle mt-2">
      <summary>Clinical Evidence (${count}) &amp; Rule Applied</summary>
      <div class="mt-2 space-y-1.5">
        <div class="text-xs text-slate-600"><strong>Rule:</strong> ${escapeHtml(payerRule || "")}</div>
        ${rows}
      </div>
    </details>
  `;
}

function renderBucket(containerId, items, mode, isLocked) {
  const el = $(containerId);
  if (items.length === 0) {
    const emptyMsg =
      mode === "staging"
        ? "No flagged items in staging."
        : mode === "removed"
        ? "No confirmed deductions in this session."
        : "No clean items.";
    el.innerHTML = `<div class="text-sm text-slate-500 py-6 text-center bg-slate-50 rounded-lg border border-dashed border-slate-300">${escapeHtml(emptyMsg)}</div>`;
    return;
  }

  el.innerHTML = items
    .map((item) => {
      const capDisplay =
        item.allowed_cap_amount !== null && item.allowed_cap_amount !== undefined
          ? `<span class="text-xs text-slate-500 ml-2">Cap: <strong class="text-slate-800">${formatINR(item.allowed_cap_amount)}</strong></span>`
          : "";

      const savingsPill =
        item.potential_savings > 0
          ? `<span class="cb-badge bg-emerald-100 text-emerald-800 border border-emerald-300">Save ${formatINR(item.potential_savings)}</span>`
          : "";

      const overrideHistory = item.last_modified_by_uid
        ? `<div class="mt-2 text-xs text-blue-900 bg-blue-50 border border-blue-200 rounded px-2.5 py-1">
            <strong>${escapeHtml(item.last_modified_by_uid)}</strong>: ${escapeHtml(item.last_justification_remark)}
          </div>`
        : "";

      let actionsHtml = "";
      if (!isLocked) {
        if (mode === "staging") {
          const confirmLabel = item.allowed_cap_amount !== null ? "Apply Cap" : "Confirm Removal";
          actionsHtml = `
            <button class="cb-btn cb-btn-danger text-xs py-1.5 px-3" data-action="CONFIRM_REMOVAL" data-item-id="${escapeHtml(item.item_id)}">
              ${escapeHtml(confirmLabel)}
            </button>
            <button class="cb-btn cb-btn-outline text-xs py-1.5 px-3" data-action="RESTORE_WITH_OVERRIDE" data-item-id="${escapeHtml(item.item_id)}">
              Restore (Override)
            </button>
          `;
        } else if (mode === "clean") {
          actionsHtml = `
            <button class="cb-btn cb-btn-outline text-xs py-1.5 px-3" data-action="REFLAG_ITEM" data-item-id="${escapeHtml(item.item_id)}">
              Re-Flag
            </button>
          `;
        } else if (mode === "removed") {
          actionsHtml = `
            <button class="cb-btn cb-btn-outline text-xs py-1.5 px-3" data-action="RESTORE_WITH_OVERRIDE" data-item-id="${escapeHtml(item.item_id)}">
              Restore (Override)
            </button>
          `;
        }
      }

      const primaryBadge =
        item.current_state === "RESTORED_WITH_OVERRIDE" || item.current_state === "REMOVED_CONFIRMED" || item.current_state === "FINAL_APPROVED"
          ? stateBadge(item.current_state)
          : classificationBadge(item.ai_classification);

      return `
        <article class="border border-slate-200 rounded-xl p-3.5 bg-white">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div class="space-y-1 min-w-0 flex-1">
              <div class="flex items-center gap-2 flex-wrap">
                <span class="font-mono text-xs font-bold px-1.5 py-0.5 rounded bg-slate-100 text-slate-700">${escapeHtml(item.service_code)}</span>
                <span class="font-bold text-sm text-slate-900">${escapeHtml(item.service_name)}</span>
                ${primaryBadge}
                ${savingsPill}
              </div>
              <div class="text-xs text-slate-500">
                ${escapeHtml(item.service_date)} &middot; ${escapeHtml(item.posted_by_shift)} Shift &middot; ${escapeHtml(item.category)} &middot; Qty ${item.quantity} &times; ${formatINR(item.unit_price)}
              </div>
            </div>

            <div class="text-right shrink-0">
              <div class="text-base font-extrabold text-slate-900">${formatINR(item.total_amount)}</div>
              ${capDisplay}
            </div>
          </div>

          <p class="mt-2 mb-0 text-xs text-slate-700">${escapeHtml(item.plain_english_remark)}</p>
          ${renderEvidenceToggle(item.clinical_evidence_citations, item.payer_rule_cited)}
          ${overrideHistory}

          ${actionsHtml ? `<div class="mt-2.5 pt-2.5 border-t border-slate-100 flex flex-wrap justify-end gap-2">${actionsHtml}</div>` : ""}
        </article>
      `;
    })
    .join("");
}

function setActiveTab(tabName) {
  state.activeTab = tabName;
  document.querySelectorAll(".cb-tab-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tabName);
  });
  ["staging", "clean", "removed", "audit"].forEach((t) => {
    $(`panel-${t}`).classList.toggle("hidden", t !== tabName);
  });
  if (tabName === "audit") {
    loadAuditLogs();
  }
}

async function loadAuditLogs() {
  const filterByUhid = $("audit-filter-uhid").checked && state.currentSession;
  const qs = filterByUhid ? `?uhid=${encodeURIComponent(state.currentSession.uhid)}` : "";
  try {
    const logs = await fetchJson(`/api/audit-logs${qs}`);
    $("tab-badge-audit").textContent = String(logs.length);
    const tbody = $("audit-log-tbody");
    if (logs.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" class="text-center text-slate-500 py-5">No audit log records yet.</td></tr>`;
      return;
    }
    tbody.innerHTML = logs
      .map(
        (log) => `
      <tr>
        <td>
          <div class="font-mono font-bold text-slate-800">${escapeHtml(log.audit_id)}</div>
          <div class="text-[11px] text-slate-500">${escapeHtml(log.timestamp)}</div>
        </td>
        <td>
          <div class="font-bold text-slate-900">${escapeHtml(log.uhid)}</div>
          <div class="text-[11px] text-slate-600">${escapeHtml(log.service_code)}</div>
        </td>
        <td>
          <div class="font-bold text-xs text-slate-800">${escapeHtml(log.action_type)}</div>
          <div class="text-[11px] font-mono text-blue-700">${escapeHtml(log.previous_state)} &rarr; ${escapeHtml(log.new_state)}</div>
        </td>
        <td class="font-bold ${log.financial_impact < 0 ? "text-emerald-700" : "text-slate-800"}">
          ${formatINR(log.financial_impact)}
        </td>
        <td>
          <div class="font-mono font-bold text-slate-900">${escapeHtml(log.employee_uid)}</div>
          <div class="text-[11px] text-slate-600">${escapeHtml(log.employee_name)}</div>
        </td>
        <td class="text-slate-700 max-w-xs">${escapeHtml(log.justification_remark)}</td>
      </tr>`
      )
      .join("");
  } catch (err) {
    showGlobalAlert(`Could not load audit logs: ${err.message}`, "error");
  }
}

function updateModalEmployeePreview() {
  const uid = $("modal-employee-uid").value;
  const emp = getEmployeeByUid(uid);
  const preview = $("modal-employee-preview");
  if (!uid.trim()) {
    preview.innerHTML = `<span class="text-rose-600">Enter an Employee UID.</span>`;
  } else if (!emp) {
    preview.innerHTML = `<span class="text-rose-600 font-semibold">UID '${escapeHtml(uid)}' unrecognized — will fail HIS authentication.</span>`;
  } else {
    preview.innerHTML = `<span class="text-slate-700 font-semibold">${escapeHtml(emp.full_name)}</span> · ${escapeHtml(emp.role)} · Cap: <strong>${escapeHtml(emp.override_limit_display)}</strong>`;
  }
}

function openGovernedModal(mode, itemId = null) {
  const ses = state.currentSession;
  if (!ses) return;
  state.pendingModalContext = { mode, itemId };

  $("modal-error-box").classList.add("hidden");
  $("modal-remark").value = "";
  $("modal-remark-counter").textContent = "0 / 10 min";
  $("modal-employee-uid").value = state.activeEmployeeUid;
  $("modal-employee-quick").value = "";
  updateModalEmployeePreview();

  const summary = $("modal-item-summary");
  if (mode === "FINALIZE") {
    $("modal-title").textContent = "Finalize & Generate Consolidated Bill";
    $("modal-subtitle").textContent = `Locks ${ses.uhid} and generates the final PDF & printable consolidated discharge bill.`;
    summary.innerHTML = `
      <div><strong>UHID:</strong> ${escapeHtml(ses.uhid)} (${escapeHtml(ses.payer_category)})</div>
      <div><strong>Gross:</strong> ${formatINR(ses.total_billed_amount)} | <strong>Savings:</strong> ${formatINR(ses.confirmed_savings_amount)} | <strong>Net Payable:</strong> ${formatINR(ses.net_payable_amount)}</div>
    `;
    $("btn-submit-governed").textContent = "Finalize & Generate Bill";
  } else {
    const item = ses.line_items.find((i) => i.item_id === itemId);
    if (!item) return;
    const actionTitles = {
      CONFIRM_REMOVAL: item.allowed_cap_amount !== null ? "Apply Cap & Deduct Excess" : "Confirm Removal",
      RESTORE_WITH_OVERRIDE: "Restore Item with Override",
      REFLAG_ITEM: "Re-Flag Clean Item",
    };
    const exposure = item.potential_savings > 0 ? item.potential_savings : item.total_amount;
    $("modal-title").textContent = actionTitles[mode] || "Modify Line Item";
    $("modal-subtitle").textContent = `${item.service_code} · Current State: ${item.current_state}`;
    summary.innerHTML = `
      <div><strong>Service:</strong> ${escapeHtml(item.service_name)}</div>
      <div><strong>Billed:</strong> ${formatINR(item.total_amount)} | <strong>Exposure:</strong> ${formatINR(exposure)}</div>
    `;
    $("btn-submit-governed").textContent = "Confirm";
  }

  $("governed-modal").classList.remove("hidden");
  $("modal-remark").focus();
}

function closeGovernedModal() {
  $("governed-modal").classList.add("hidden");
  state.pendingModalContext = null;
}

function openFinalBillModal(ses) {
  $("final-modal-subtitle").textContent = `${ses.uhid} · ${ses.payer_category} — Signed off by ${ses.finalized_by_name} (${ses.finalized_by_uid})`;
  $("final-modal-summary").innerHTML = `
    <div class="flex justify-between"><span>Gross Billed:</span><strong>${formatINR(ses.total_billed_amount)}</strong></div>
    <div class="flex justify-between"><span>Deductions / Savings:</span><strong>-${formatINR(ses.confirmed_savings_amount)}</strong></div>
    <div class="flex justify-between pt-1.5 border-t border-emerald-200 text-base"><span>Net Consolidated Payable:</span><strong>${formatINR(ses.net_payable_amount)}</strong></div>
  `;
  $("modal-download-pdf").href = `/api/sessions/${encodeURIComponent(ses.session_id)}/final-bill.pdf`;
  $("modal-print-bill").href = `/api/sessions/${encodeURIComponent(ses.session_id)}/final-bill/print`;
  $("final-bill-modal").classList.remove("hidden");
}

async function submitGovernedModal() {
  const ctx = state.pendingModalContext;
  const ses = state.currentSession;
  if (!ctx || !ses) return;

  const uid = $("modal-employee-uid").value.trim();
  const remark = $("modal-remark").value.trim();
  const errBox = $("modal-error-box");
  errBox.classList.add("hidden");

  $("btn-submit-governed").disabled = true;
  try {
    let updatedSession;
    const isFinalize = ctx.mode === "FINALIZE";
    if (isFinalize) {
      updatedSession = await fetchJson(`/api/sessions/${encodeURIComponent(ses.session_id)}/finalize`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ employee_uid: uid, justification_remark: remark }),
      });
      showGlobalAlert(`Discharge bill for ${ses.uhid} finalized by ${uid}. Consolidated PDF is ready.`, "success");
    } else {
      updatedSession = await fetchJson(
        `/api/sessions/${encodeURIComponent(ses.session_id)}/items/${encodeURIComponent(ctx.itemId)}/transition`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: ctx.mode, employee_uid: uid, justification_remark: remark }),
        }
      );
      showGlobalAlert(`Updated ${ctx.itemId} (${ctx.mode}) under ${uid}.`, "success");
    }
    state.currentSession = updatedSession;
    closeGovernedModal();
    await refreshPatientsList(updatedSession.uhid);
    renderSession();
    await loadAuditLogs();
    if (isFinalize) {
      openFinalBillModal(updatedSession);
    }
  } catch (err) {
    errBox.textContent = err.message;
    errBox.classList.remove("hidden");
  } finally {
    $("btn-submit-governed").disabled = false;
  }
}

function openSkillsModal(highlightSlug = null) {
  const body = $("skills-modal-body");
  body.innerHTML = state.skills
    .map((s) => {
      const activeBorder =
        highlightSlug && s.name === highlightSlug ? "border-indigo-500 ring-2 ring-indigo-200" : "border-slate-200";
      return `
        <details class="border ${activeBorder} rounded-lg p-3 bg-slate-50" ${highlightSlug && s.name === highlightSlug ? "open" : ""}>
          <summary class="cursor-pointer font-bold text-sm text-slate-900 flex items-center justify-between">
            <span>${escapeHtml(s.name)}</span>
            <span class="font-mono text-xs font-normal text-slate-500">${escapeHtml(s.path)}</span>
          </summary>
          <p class="text-xs text-slate-600 mt-1.5 mb-2">${escapeHtml(s.description)}</p>
          <pre class="cb-pre bg-white border border-slate-200 text-slate-800">${escapeHtml(s.content)}</pre>
        </details>
      `;
    })
    .join("");
  $("skills-modal").classList.remove("hidden");
}

function bindEvents() {
  $("employee-select").addEventListener("change", (e) => {
    state.activeEmployeeUid = e.target.value;
    renderActiveEmployeeBar();
  });

  $("btn-run-selected").addEventListener("click", () => {
    triggerUhidReconciliation($("uhid-select").value, false, {
      triggerBtnId: "btn-run-selected",
      minDisplayMs: 320,
    });
  });

  $("btn-rerun-uhid").addEventListener("click", () => {
    triggerUhidReconciliation($("uhid-select").value, true, {
      triggerBtnId: "btn-rerun-uhid",
      minDisplayMs: 320,
    });
  });

  $("uhid-select").addEventListener("change", () => {
    triggerUhidReconciliation($("uhid-select").value, false, { minDisplayMs: 0 });
  });

  $("btn-manual-search").addEventListener("click", () => {
    triggerUhidReconciliation($("manual-uhid-input").value, false, {
      triggerBtnId: "btn-manual-search",
      minDisplayMs: 320,
    });
  });

  $("manual-uhid-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      triggerUhidReconciliation($("manual-uhid-input").value, false, {
        triggerBtnId: "btn-manual-search",
        minDisplayMs: 320,
      });
    }
  });

  document.querySelectorAll(".cb-tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => setActiveTab(btn.dataset.tab));
  });

  ["panel-staging", "panel-clean", "panel-removed"].forEach((panelId) => {
    $(panelId).addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-action]");
      if (!btn) return;
      openGovernedModal(btn.dataset.action, btn.dataset.itemId);
    });
  });

  $("btn-open-finalize").addEventListener("click", () => openGovernedModal("FINALIZE"));
  $("btn-close-governed").addEventListener("click", closeGovernedModal);
  $("btn-cancel-governed").addEventListener("click", closeGovernedModal);
  $("btn-submit-governed").addEventListener("click", submitGovernedModal);

  $("btn-close-final-modal").addEventListener("click", () => $("final-bill-modal").classList.add("hidden"));
  $("btn-dismiss-final-modal").addEventListener("click", () => $("final-bill-modal").classList.add("hidden"));

  $("modal-employee-uid").addEventListener("input", updateModalEmployeePreview);
  $("modal-employee-quick").addEventListener("change", (e) => {
    if (e.target.value) {
      $("modal-employee-uid").value = e.target.value;
      updateModalEmployeePreview();
    }
  });
  $("modal-remark").addEventListener("input", (e) => {
    const len = e.target.value.trim().length;
    $("modal-remark-counter").textContent = `${len} / 10 min`;
    $("modal-remark-counter").className =
      len >= 10 ? "text-xs font-semibold text-emerald-700" : "text-xs text-slate-500";
  });

  $("audit-filter-uhid").addEventListener("change", loadAuditLogs);
  $("btn-refresh-audit").addEventListener("click", loadAuditLogs);

  $("btn-open-upload").addEventListener("click", () => {
    $("upload-error-box").classList.add("hidden");
    $("upload-modal").classList.remove("hidden");
  });
  $("btn-close-upload").addEventListener("click", () => $("upload-modal").classList.add("hidden"));
  $("btn-cancel-upload").addEventListener("click", () => $("upload-modal").classList.add("hidden"));

  $("upload-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errBox = $("upload-error-box");
    errBox.classList.add("hidden");
    const fileInput = $("upload-file");
    if (!fileInput.files || !fileInput.files[0]) return;

    const submitBtn = e.target.querySelector('button[type="submit"]');
    const originalText = submitBtn ? submitBtn.textContent : "";
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.textContent = "Auditing...";
    }

    const formData = new FormData();
    formData.append("file", fileInput.files[0]);
    formData.append("payer_category", $("upload-payer").value);
    formData.append("room_entitlement_cap_per_day", $("upload-room-cap").value || "4500");
    formData.append("clinical_notes_text", $("upload-notes").value || "");

    try {
      showGlassLoader(
        "Auditing Uploaded Bill",
        `Parsing & reconciling ${fileInput.files[0].name}...`
      );
      const ses = await fetchJson("/api/reconcile/upload", {
        method: "POST",
        body: formData,
      });
      const uploadedName = fileInput.files[0].name;
      $("upload-modal").classList.add("hidden");
      e.target.reset();
      state.currentSession = ses;
      setActiveTab(ses.staging_count > 0 ? "staging" : "clean");
      renderSession();
      await loadAuditLogs();
      showGlobalAlert(`Uploaded & audited '${uploadedName}' as ${ses.uhid}.`, "success");
    } catch (err) {
      errBox.textContent = err.message;
      errBox.classList.remove("hidden");
    } finally {
      hideGlassLoader();
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.textContent = originalText || "Upload & Audit";
      }
    }
  });

  $("btn-view-skills").addEventListener("click", () => openSkillsModal());
  $("btn-close-skills").addEventListener("click", () => $("skills-modal").classList.add("hidden"));
}

async function init() {
  bindEvents();
  try {
    const [health, employees, patients, skills] = await Promise.all([
      fetchJson("/api/health"),
      fetchJson("/api/employees"),
      fetchJson("/api/patients"),
      fetchJson("/api/skills"),
    ]);

    const pill = $("engine-mode-pill");
    if (health.engine_mode === "GEMINI_LIVE") {
      const via = health.auth_mode === "VERTEX_EXPRESS" ? "Vertex Express" : "AI Studio";
      pill.textContent = `Live Gemini · ${via}`;
    } else {
      pill.textContent = "Demo Rule Engine";
    }

    state.employees = employees;
    state.patients = patients;
    state.skills = skills;

    $("employee-select").innerHTML = employees
      .map(
        (e) =>
          `<option value="${escapeHtml(e.employee_uid)}">${escapeHtml(e.employee_uid)} — ${escapeHtml(e.full_name)}</option>`
      )
      .join("");
    $("modal-employee-quick").innerHTML =
      `<option value="">Quick pick...</option>` +
      employees
        .map(
          (e) =>
            `<option value="${escapeHtml(e.employee_uid)}">${escapeHtml(e.employee_uid)} (${escapeHtml(e.override_limit_display)})</option>`
        )
        .join("");

    renderActiveEmployeeBar();
    renderPatientDropdown();

    if (patients.length > 0) {
      await triggerUhidReconciliation(patients[0].uhid, false, { minDisplayMs: 0 });
    }
  } catch (err) {
    showGlobalAlert(`Startup check failed: ${err.message}`, "error");
  }
}

document.addEventListener("DOMContentLoaded", init);
