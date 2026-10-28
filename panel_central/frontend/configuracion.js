// ============================================================
// configuracion.js — Pantalla "Configuración del negocio"
// Cada sección edita un borrador (draft) y se compara contra lo
// último guardado (saved). Guardar solo se habilita si hay
// cambios y no hay errores. Productos fijos se guardan por ítem.
// ============================================================

import { showToast, escapeHtml } from "./ui.js";
import * as api from "./configService.js";
import {
  formatCOP, maskNumber, validateNewAdminPhone, validateGeneral, validateAccesos,
  validateComandas, validateDomicilios, validateIntegraciones, validateZona, validateAlias, validateCuenta, validateProducto,
  PLANES, FEATURE_LABEL, lostFeatures,
} from "./configValidators.js";

// ------------------------------------------------------------
// Sesión: la decide IAP delante del proxy del panel (solo entra el
// equipo de J2YS). ?business=<id> elige el negocio; sin él se
// muestra la lista. ?role=admin oculta Integraciones.
// ------------------------------------------------------------
const params = new URLSearchParams(location.search);
const user = { role: params.get("role") || "superadmin" };
const BUSINESS_ID = params.get("business");

const SECTIONS = {
  general: { label: "General", paths: ["configuracion.general", "negocio.status"], validate: validateGeneral },
  plan: { label: "Plan", paths: ["negocio.plan"], validate: () => ({}) },
  accesos: { label: "Accesos", paths: ["configuracion.accesos"], validate: validateAccesos },
  recordatorio: { label: "Recordatorio del menú", paths: ["configuracion.recordatorio"], validate: () => ({}) },
  domicilios: { label: "Domicilios", paths: ["configuracion.domicilios"], validate: validateDomicilios },
  pagos: { label: "Pagos", paths: ["configuracion.pagos"], validate: () => ({}) },
  comandas: { label: "Comandas", paths: ["configuracion.comandas"], validate: validateComandas },
  integraciones: { label: "Integraciones", paths: ["negocio.whatsapp_phone_number_id", "negocio.bussiness_vertical"], validate: validateIntegraciones },
};

const state = {
  saved: null,
  draft: null,
  productos: [],
  revealed: new Set(), // índices de cuentas con el número visible
};

const $ = (sel, root = document) => root.querySelector(sel);
const sectionEl = (key) => $(`[data-section="${key}"]`);
const clone = (v) => structuredClone(v);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function getPath(obj, path) {
  return path.split(".").reduce((o, k) => o?.[k], obj);
}
function setPath(obj, path, value) {
  const keys = path.split(".");
  const last = keys.pop();
  keys.reduce((o, k) => o[k], obj)[last] = value;
}

const isDirty = (key) => SECTIONS[key].paths.some((p) => !same(getPath(state.draft, p), getPath(state.saved, p)));
const anyDirty = () => state.draft && Object.keys(SECTIONS).some(isDirty);

// ------------------------------------------------------------
// Carga
// ------------------------------------------------------------
function showState(name) {
  for (const s of ["loading", "error", "ready"]) $(`#state-${s}`).hidden = s !== name;
}

/** Sin ?business=: lista de negocios para elegir. */
async function showBusinessPicker() {
  showState("loading");
  try {
    const businesses = await api.listBusinesses();
    $("#state-error h2").textContent = "Elige un negocio";
    $("#state-error-text").innerHTML = `<ul class="business-list">${businesses
      .map(
        (b) => `<li><a href="?business=${encodeURIComponent(b.business_id)}">${escapeHtml(b.name || b.business_id)}</a>
          <span class="mono muted">${escapeHtml(b.whatsapp_phone_number || "")}</span>
          ${b.status === "active" ? "" : '<span class="chip">bot apagado</span>'}</li>`
      )
      .join("")}</ul>`;
    $('[data-action="retry"]').hidden = true;
  } catch (err) {
    $("#state-error-text").textContent = err.message;
  }
  showState("error");
}

async function load() {
  showState("loading");
  try {
    const [config, productos] = await Promise.all([
      api.getConfiguration(BUSINESS_ID),
      api.listFixedItems(BUSINESS_ID),
    ]);
    state.saved = config;
    state.draft = clone(config);
    state.productos = productos;
    Object.keys(SECTIONS).forEach(renderSection);
    renderProductos();
    renderHero();
    applyPlan();
    showState("ready");
    loadPrinterVersion();
  } catch (err) {
    $("#state-error-text").textContent = err.message;
    showState("error");
  }
}

// ------------------------------------------------------------
// Render de secciones
// ------------------------------------------------------------
function renderHero() {
  const { negocio, configuracion } = state.saved;
  const active = negocio.status === "active";
  $("#hero-name").textContent = configuracion.general.nombre_comercial || negocio.name || negocio.business_id;
  $("#hero-status").classList.toggle("is-active", active);
  $(".biz-hero").classList.toggle("is-live", active);
  $("#hero-status-text").textContent = active
    ? "El bot está respondiendo a tus clientes"
    : "El bot está pausado: no responde a ningún cliente";
  $("#hero-plan").textContent = `Plan ${PLANES[negocio.plan]?.label ?? negocio.plan}`;
}

/** Con el plan GUARDADO (el que aplica el backend): deshabilita lo que no
 * incluye (data-requires) y muestra por qué (data-missing). */
function applyPlan() {
  const features = state.saved.negocio.features;
  document.querySelectorAll("[data-requires]").forEach((el) => (el.disabled = !features.includes(el.dataset.requires)));
  document.querySelectorAll("[data-missing]").forEach((el) => (el.hidden = features.includes(el.dataset.missing)));
}

/** Copia el borrador a los controles de la sección y pinta sus listas. */
function renderSection(key) {
  const section = sectionEl(key);
  section.querySelectorAll("[data-path]").forEach((input) => {
    const value = getPath(state.draft, input.dataset.path);
    if (input.dataset.path === "negocio.status") input.checked = value === "active";
    else if (input.type === "radio") input.checked = input.value === value;
    else input.value = value ?? "";
  });
  if (key === "accesos") {
    renderAdmins();
    $("#f-admin-new").value = "";
    showError($("#err-admin-new"), "");
  }
  if (key === "domicilios") renderZonas();
  if (key === "pagos") renderCuentas();
  if (key === "integraciones") {
    $("#f-phone-id").readOnly = true;
    $("#unlock-phone-id").hidden = false;
  }
  refresh(key);
}

/** Recalcula errores, avisos y el estado de los botones. No re-renderiza inputs (para no perder el foco). */
function refresh(key) {
  const section = sectionEl(key);
  const errors = SECTIONS[key].validate(state.draft);
  section.querySelectorAll("[data-error-for]").forEach((el) => showError(el, errors[el.dataset.errorFor]));

  const dirty = isDirty(key);
  $('[data-action="save"]', section).disabled = !dirty || Object.keys(errors).length > 0;
  $('[data-action="discard"]', section).disabled = !dirty;
  $(".dirty-flag", section).hidden = !dirty;
  $(`[data-nav="${key}"] .nav-dot`).hidden = !dirty;

  if (key === "general") $("#status-label").textContent = state.draft.negocio.status === "active" ? "activo" : "inactivo";
  if (key === "plan") {
    const table = $("#plan-table");
    const changed = table.dataset.selected && table.dataset.selected !== state.draft.negocio.plan;
    table.dataset.selected = state.draft.negocio.plan;
    if (changed) {
      // Haz que recorre la columna elegida: reinicia la animación.
      table.classList.remove("is-switching");
      void table.offsetWidth;
      table.classList.add("is-switching");
    }
  }
  if (key === "comandas") {
    $("#printer-fields").hidden = state.draft.configuracion.comandas.destino !== "impresora";
    $('[data-action="update-raspberry"]').disabled = !printerSaved() || state.updatingRaspberry;
    $('[data-action="save-wifi"]').disabled = !printerSaved() || state.savingWifi;
    $('[data-action="test-print"]').disabled = !printerSaved() || state.testPrinting;
  }
}

function showError(el, message) {
  el.textContent = message || "";
  el.hidden = !message;
  const input = el.id && document.querySelector(`[aria-describedby~="${el.id}"]`);
  if (input) input.setAttribute("aria-invalid", message ? "true" : "false");
}

function renderAdmins() {
  const phones = state.draft.configuracion.accesos.admin_phone_numbers;
  const onlyOne = phones.length <= 1;
  $("#admin-list").innerHTML = phones
    .map(
      (p, i) => `
      <li>
        <span class="mono">${escapeHtml(p)}</span>
        <button type="button" class="btn-ghost btn-ghost--danger" data-action="remove-admin" data-index="${i}"
          ${onlyOne ? 'disabled title="Debe quedar al menos un número"' : ""}
          aria-label="Quitar ${escapeHtml(p)}">Quitar</button>
      </li>`
    )
    .join("");
}

function renderZonas() {
  const zonas = state.draft.configuracion.domicilios.zonas;
  if (!zonas.length) {
    $("#zonas-body").innerHTML = `<p class="empty-state">Aún no hay zonas. Agrega la primera para que el bot cobre el domicilio solo.</p>`;
    return;
  }
  $("#zonas-body").innerHTML = `
    <div class="table-scroll">
      <table class="stack-table">
        <thead><tr><th scope="col">Zona</th><th scope="col">Tarifa</th><th scope="col">Alias</th><th scope="col"><span class="sr-only">Acciones</span></th></tr></thead>
        <tbody>${zonas
          .map(
            (z, i) => `
          <tr>
            <td data-label="Zona"><strong>${escapeHtml(z.nombre)}</strong></td>
            <td data-label="Tarifa" class="mono">${formatCOP(z.tarifa)}</td>
            <td data-label="Alias"><span>${z.alias.length ? z.alias.map((a) => `<span class="chip">${escapeHtml(a)}</span>`).join(" ") : '<span class="muted">Sin alias</span>'}</span></td>
            <td class="row-actions">
              <button type="button" class="btn-ghost" data-action="edit-zona" data-index="${i}" aria-label="Editar ${escapeHtml(z.nombre)}">Editar</button>
              <button type="button" class="btn-ghost btn-ghost--danger" data-action="delete-zona" data-index="${i}" aria-label="Eliminar ${escapeHtml(z.nombre)}">Eliminar</button>
            </td>
          </tr>`
          )
          .join("")}</tbody>
      </table>
    </div>`;
}

function renderCuentas() {
  const cuentas = state.draft.configuracion.pagos.cuentas_transferencia;
  if (!cuentas.length) {
    $("#cuentas-body").innerHTML = `<li class="empty-state">Aún no hay cuentas. Sin cuentas, el bot no puede ofrecer transferencia.</li>`;
    return;
  }
  $("#cuentas-body").innerHTML = cuentas
    .map((c, i) => {
      const visible = state.revealed.has(i);
      return `
      <li class="account">
        <div class="account-main">
          <strong>${escapeHtml(c.banco)}</strong> <span class="muted">${escapeHtml(c.tipo)}</span>
          <div class="account-number">
            <span class="mono">${escapeHtml(visible ? c.numero : maskNumber(c.numero))}</span>
            <button type="button" class="toggle-eye toggle-eye--inline" data-action="reveal-cuenta" data-index="${i}"
              aria-pressed="${visible}" aria-label="${visible ? "Ocultar" : "Mostrar"} número de ${escapeHtml(c.banco)}">
              ${visible ? EYE_OFF : EYE_ON}
            </button>
          </div>
          <span class="muted">${escapeHtml(c.titular)}${c.llave ? ` · llave ${escapeHtml(c.llave)}` : ""}</span>
        </div>
        <div class="row-actions">
          <button type="button" class="btn-ghost" data-action="edit-cuenta" data-index="${i}">Editar</button>
          <button type="button" class="btn-ghost btn-ghost--danger" data-action="delete-cuenta" data-index="${i}">Eliminar</button>
        </div>
      </li>`;
    })
    .join("");
}

const EYE_ON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>`;
const EYE_OFF = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17.94 17.94A10.94 10.94 0 0 1 12 20c-7 0-11-8-11-8a21.8 21.8 0 0 1 5.06-6.06"></path><path d="M9.9 4.24A10.94 10.94 0 0 1 12 4c7 0 11 8 11 8a21.6 21.6 0 0 1-3.22 4.44"></path><path d="M14.12 14.12a3 3 0 1 1-4.24-4.24"></path><line x1="1" y1="1" x2="23" y2="23"></line></svg>`;

const ORIGEN_LABEL = { catalogo: "Catálogo", carta: "Carta" };

function renderProductos() {
  const body = $("#productos-body");
  if (!state.productos.length) {
    body.innerHTML = `<p class="empty-state">Aún no hay productos fijos.</p>`;
    return;
  }
  body.innerHTML = `
    <div class="table-scroll">
      <table class="stack-table">
        <thead><tr><th scope="col">Producto</th><th scope="col">Categoría</th><th scope="col">Precio</th><th scope="col">Activo</th><th scope="col"><span class="sr-only">Acciones</span></th></tr></thead>
        <tbody>${state.productos
          .map((p) => {
            const readonly = p.origen !== "manual";
            const tip = "Se actualiza desde el catálogo de WhatsApp o la foto de la carta";
            return `
          <tr>
            <td data-label="Producto">
              <strong>${escapeHtml(p.nombre)}</strong>
              ${readonly ? `<span class="badge tip" tabindex="0" data-tip="${tip}" aria-label="${ORIGEN_LABEL[p.origen]}: ${tip}">${ORIGEN_LABEL[p.origen]}</span>` : ""}
              ${p.descripcion ? `<div class="muted">${escapeHtml(p.descripcion)}</div>` : ""}
            </td>
            <td data-label="Categoría">${escapeHtml(p.categoria) || '<span class="muted">—</span>'}</td>
            <td data-label="Precio" class="mono">${p.tipo_precio === "no_legible" ? '<span class="muted">No legible</span>' : formatCOP(p.precio)}</td>
            <td data-label="Activo">
              <input type="checkbox" role="switch" class="switch" data-action="toggle-producto" data-id="${p.id}"
                ${p.is_active ? "checked" : ""} ${readonly ? "disabled" : ""} aria-label="${escapeHtml(p.nombre)} activo" />
            </td>
            <td class="row-actions">
              ${
                readonly
                  ? `<span class="muted">Solo lectura</span>`
                  : `<button type="button" class="btn-ghost" data-action="edit-producto" data-id="${p.id}" aria-label="Editar ${escapeHtml(p.nombre)}">Editar</button>
                     <button type="button" class="btn-ghost btn-ghost--danger" data-action="delete-producto" data-id="${p.id}" aria-label="Eliminar ${escapeHtml(p.nombre)}">Eliminar</button>`
              }
            </td>
          </tr>`;
          })
          .join("")}</tbody>
      </table>
    </div>`;
}

// ------------------------------------------------------------
// Guardar / descartar
// ------------------------------------------------------------
/** Antes de bajar de plan: qué deja de funcionar. */
async function confirmPlanChange() {
  const to = state.draft.negocio.plan;
  const lost = lostFeatures(state.saved.negocio.features, to);
  if (!lost.length) return true;
  const list = lost.map((f) => FEATURE_LABEL[f]).join(", ").replace(/, ([^,]*)$/, " y $1");
  return confirmar({
    title: `Cambiar al plan ${PLANES[to].label}`,
    message: `Desde que lo guardes, el negocio deja de tener ${list}. Los datos no se borran.`,
    confirmLabel: "Cambiar plan",
    danger: true,
  });
}

async function saveSection(key, button) {
  if (key === "plan" && !(await confirmPlanChange())) return;
  button.disabled = true;
  button.textContent = "Guardando…";
  try {
    const negocioPatch = {};
    for (const path of SECTIONS[key].paths) {
      const value = getPath(state.draft, path);
      if (same(value, getPath(state.saved, path))) continue;
      const [root, field] = path.split(".");
      if (root === "negocio") negocioPatch[field] = value;
      // Lo que devuelve el backend es lo guardado (ej. el token de la impresora se borra).
      else setPath(state.draft, path, await api.updateSection(BUSINESS_ID, field, value));
    }
    if (Object.keys(negocioPatch).length) {
      const saved = await api.updateSection(BUSINESS_ID, "negocio", negocioPatch);
      if (saved.features) state.saved.negocio.features = state.draft.negocio.features = saved.features;
    }
    for (const path of SECTIONS[key].paths) setPath(state.saved, path, clone(getPath(state.draft, path)));
    renderSection(key);
    renderHero();
    applyPlan();
    if (key === "comandas") loadPrinterVersion();
    showToast(`${SECTIONS[key].label}: cambios guardados`);
  } catch (err) {
    showToast(`No se guardó ${SECTIONS[key].label}. ${err.message}`, "error");
  } finally {
    button.textContent = "Guardar";
    refresh(key);
  }
}

// ------------------------------------------------------------
// Raspberry de la impresora (usa lo GUARDADO, no el borrador)
// ------------------------------------------------------------
function printerSaved() {
  const { destino, impresora } = state.saved.configuracion.comandas;
  return destino === "impresora" && Boolean(impresora.endpoint_url) && impresora.token_configurado;
}

async function loadPrinterVersion() {
  const el = $("#printer-version");
  if (!printerSaved()) {
    el.textContent = "guarda la URL y el token para conectarla";
    return;
  }
  el.textContent = "consultando…";
  loadPrinterDevices();
  try {
    el.textContent = `versión ${(await api.getPrinterDevice(BUSINESS_ID)).version}`;
  } catch (err) {
    el.textContent = `sin conexión (${err.message})`;
  }
}

const DEVICE_STATUS = {
  operativo: ["Operativo", "status-pill--ok"],
  degradado: ["Degradado", "status-pill--warn"],
  desconectado: ["Desconectado", "status-pill--error"],
  sin_informacion: ["Sin información", ""],
};

function ago(seconds) {
  if (seconds === null || seconds === undefined) return "nunca";
  if (seconds < 60) return `hace ${seconds} s`;
  if (seconds < 3600) return `hace ${Math.round(seconds / 60)} min`;
  if (seconds < 86400) return `hace ${Math.round(seconds / 3600)} h`;
  return `hace ${Math.round(seconds / 86400)} d`;
}

const when = (iso) => (iso ? new Date(iso).toLocaleString("es-CO") : "—");

const EVENT_LABEL = {
  registrada: ["Conectada por primera vez", ""],
  sin_conexion: ["Sin conexión", "event--bad"],
  reiniciada: ["Reinicio", "event--warn"],
  impresora_desconectada: ["Impresora desconectada", "event--bad"],
  impresora_conectada: ["Impresora conectada", "event--ok"],
  wifi_cambiado: ["Cambio de Wi-Fi", ""],
  version_actualizada: ["Actualización", ""],
  error_impresion: ["Error al imprimir", "event--bad"],
};

async function loadPrinterEvents() {
  const list = $("#printer-events");
  try {
    const events = await api.listPrinterEvents(BUSINESS_ID);
    list.innerHTML = events.length
      ? events
          .map((e) => {
            const [label, cls] = EVENT_LABEL[e.event_type] || [e.event_type, ""];
            const time = e.until ? `${when(e.at)} → ${when(e.until)}` : when(e.at);
            return `<li class="event-item ${cls}">
              <span class="event-time mono">${time}</span>
              <span><strong>${escapeHtml(label)}</strong>${e.detail ? ` · ${escapeHtml(e.detail)}` : ""}</span>
            </li>`;
          })
          .join("")
      : `<li class="hint">Sin eventos todavía.</li>`;
  } catch (err) {
    list.innerHTML = `<li class="hint device-error">No se pudo leer el historial: ${escapeHtml(err.message)}</li>`;
  }
}

// Estado e historial en vivo mientras la pestaña está a la vista.
setInterval(() => {
  if (document.visibilityState === "visible" && state.saved && printerSaved()) loadPrinterDevices();
}, 30_000);

async function loadPrinterDevices() {
  const list = $("#printer-devices");
  if (!printerSaved()) {
    list.innerHTML = "";
    $("#printer-events").innerHTML = "";
    return;
  }
  loadPrinterEvents();
  try {
    const devices = await api.listPrinterDevices(BUSINESS_ID);
    const checked = new Date().toLocaleTimeString("es-CO");
    list.innerHTML = devices.length
      ? devices
          .map((d) => {
            const [label, cls] = DEVICE_STATUS[d.status] || DEVICE_STATUS.sin_informacion;
            return `<li class="device-item">
              <div class="device-head">
                <span class="status-pill ${cls}"><span class="status-dot"></span>${label}</span>
                <span class="mono">${escapeHtml(d.device_id)}</span>
                <span class="muted">último contacto ${ago(d.seconds_since_seen)}</span>
              </div>
              <p class="hint">Impresora USB: ${d.printer_usb ? "detectada" : "no detectada"}
                · Wi-Fi: ${escapeHtml(d.wifi_ssid || "—")} · versión ${escapeHtml(d.version || "—")}
                · último print OK: ${when(d.last_print_ok_at)}</p>
              ${d.last_error ? `<p class="hint device-error">Último error (${when(d.last_error_at)}): ${escapeHtml(d.last_error)}</p>` : ""}
            </li>`;
          })
          .join("") + `<li class="hint">Consultado a las ${checked}</li>`
      : `<li class="hint">Todavía no llega ningún heartbeat. Configura HEARTBEAT_URL en la Raspberry (RUNBOOK §4).</li>`;
  } catch (err) {
    list.innerHTML = `<li class="hint device-error">No se pudo leer el estado: ${escapeHtml(err.message)}</li>`;
  }
}

async function updateRaspberry(btn) {
  const ok = await confirmar({
    title: "Actualizar Raspberry",
    message: "La Raspberry bajará el código nuevo de GitLab y se reiniciará. Durante unos segundos no imprimirá comandas.",
    confirmLabel: "Actualizar",
  });
  if (!ok) return;
  state.updatingRaspberry = true;
  btn.disabled = true;
  btn.textContent = "Actualizando…";
  try {
    const r = await api.updatePrinterDevice(BUSINESS_ID);
    if (r.updated) {
      showToast(`Raspberry actualizada: ${r.from} → ${r.to}`);
      $("#printer-version").textContent = `versión ${r.to} (reiniciando…)`;
      setTimeout(loadPrinterVersion, 8000); // confirma que volvió a levantar
    } else {
      showToast(`La Raspberry ya estaba al día (versión ${r.version})`);
    }
  } catch (err) {
    showToast(`No se actualizó la Raspberry. ${err.message}`, "error");
  } finally {
    state.updatingRaspberry = false;
    btn.textContent = "Actualizar Raspberry";
    refresh("comandas");
  }
}

async function testPrint(btn) {
  state.testPrinting = true;
  btn.disabled = true;
  btn.textContent = "Imprimiendo…";
  try {
    await api.printTestPage(BUSINESS_ID);
    showToast("Ticket de prueba enviado. Revisa que haya salido el papel.");
  } catch (err) {
    showToast(`No se imprimió la prueba. ${err.message}`, "error");
  } finally {
    state.testPrinting = false;
    btn.textContent = "Imprimir prueba";
    refresh("comandas");
    setTimeout(loadPrinterDevices, 2000);
  }
}

async function saveWifi(btn) {
  const ssidInput = $("#f-wifi-ssid");
  const passInput = $("#f-wifi-password");
  const ssid = ssidInput.value.trim();
  const password = passInput.value;
  if (!ssid || new TextEncoder().encode(ssid).length > 32) {
    showToast("El nombre del Wi-Fi debe tener entre 1 y 32 caracteres.", "error");
    return ssidInput.focus();
  }
  if (password.length < 8 || password.length > 63) {
    showToast("La clave del Wi-Fi debe tener entre 8 y 63 caracteres.", "error");
    return passInput.focus();
  }
  state.savingWifi = true;
  btn.disabled = true;
  btn.textContent = "Guardando…";
  try {
    await api.setPrinterWifi(BUSINESS_ID, ssid, password);
    passInput.value = "";
    showToast(`Wi-Fi "${ssid}" guardado. Apaga el hotspot para que la Raspberry se cambie.`);
  } catch (err) {
    showToast(`No se guardó el Wi-Fi. ${err.message}`, "error");
  } finally {
    state.savingWifi = false;
    btn.textContent = "Guardar Wi-Fi en la Raspberry";
    refresh("comandas");
  }
}

function discardSection(key) {
  for (const path of SECTIONS[key].paths) setPath(state.draft, path, clone(getPath(state.saved, path)));
  state.revealed.clear();
  renderSection(key);
}

// ------------------------------------------------------------
// Diálogos
// ------------------------------------------------------------
const confirmDialog = $("#confirm-dialog");

/** Modal de confirmación; resuelve true solo si se pulsa el botón de confirmar. */
function confirmar({ title, message, confirmLabel = "Confirmar", danger = false }) {
  $("#confirm-title").textContent = title;
  $("#confirm-message").textContent = message;
  const ok = $("#confirm-ok");
  ok.textContent = confirmLabel;
  ok.classList.toggle("btn-danger", danger);
  confirmDialog.returnValue = "";
  confirmDialog.showModal();
  return new Promise((resolve) => {
    confirmDialog.addEventListener("close", () => resolve(confirmDialog.returnValue === "confirm"), { once: true });
  });
}

function showFormErrors(form, errors) {
  form.querySelectorAll("[data-error-for]").forEach((el) => showError(el, errors[el.dataset.errorFor]));
  const first = Object.keys(errors)[0];
  if (first) form.querySelector(`[name="${first}"], #z-alias`)?.focus();
}

document.querySelectorAll("dialog [data-close]").forEach((btn) =>
  btn.addEventListener("click", () => btn.closest("dialog").close())
);

// --- Zona ----------------------------------------------------
const zonaDialog = $("#zona-dialog");
const zonaForm = $("#zona-form");
let zonaEditIndex = null;
let zonaAlias = [];

const otrasZonas = () => state.draft.configuracion.domicilios.zonas.filter((_, i) => i !== zonaEditIndex);

function renderChips() {
  const input = $("#z-alias");
  $("#z-chips").querySelectorAll(".chip").forEach((c) => c.remove());
  zonaAlias.forEach((a, i) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.innerHTML = `${escapeHtml(a)}<button type="button" data-chip="${i}" aria-label="Quitar alias ${escapeHtml(a)}">×</button>`;
    input.before(chip);
  });
}

/** Agrega el texto del input como alias; devuelve false si no es válido. */
function commitAlias() {
  const input = $("#z-alias");
  const value = input.value.replace(/,/g, "").trim();
  if (!value) return true;
  const error = validateAlias(value, zonaAlias, otrasZonas());
  showError($("#zerr-alias"), error);
  if (error) return false;
  zonaAlias.push(value);
  input.value = "";
  renderChips();
  return true;
}

function openZona(index) {
  zonaEditIndex = index;
  const z = index === null ? { nombre: "", tarifa: "", alias: [] } : state.draft.configuracion.domicilios.zonas[index];
  $("#zona-title").textContent = index === null ? "Agregar zona" : "Editar zona";
  zonaForm.nombre.value = z.nombre;
  zonaForm.tarifa.value = z.tarifa;
  $("#z-alias").value = "";
  zonaAlias = [...z.alias];
  renderChips();
  showFormErrors(zonaForm, {});
  zonaDialog.showModal();
}

$("#z-alias").addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === ",") {
    e.preventDefault();
    commitAlias();
  } else if (e.key === "Backspace" && !e.target.value && zonaAlias.length) {
    zonaAlias.pop();
    renderChips();
  }
});
$("#z-chips").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-chip]");
  if (btn) {
    zonaAlias.splice(Number(btn.dataset.chip), 1);
    renderChips();
    $("#z-alias").focus();
  } else if (e.target === e.currentTarget) {
    $("#z-alias").focus();
  }
});

zonaForm.addEventListener("submit", (e) => {
  e.preventDefault();
  if (!commitAlias()) return;
  const zona = { nombre: zonaForm.nombre.value.trim(), tarifa: zonaForm.tarifa.value.trim(), alias: zonaAlias };
  const errors = validateZona(zona, otrasZonas());
  if (Object.keys(errors).length) return showFormErrors(zonaForm, errors);
  zona.tarifa = Number(zona.tarifa);
  const zonas = state.draft.configuracion.domicilios.zonas;
  if (zonaEditIndex === null) zonas.push(zona);
  else zonas[zonaEditIndex] = zona;
  zonaDialog.close();
  renderZonas();
  refresh("domicilios");
});

// --- Cuenta --------------------------------------------------
const cuentaDialog = $("#cuenta-dialog");
const cuentaForm = $("#cuenta-form");
let cuentaEditIndex = null;

function openCuenta(index) {
  cuentaEditIndex = index;
  const c = index === null
    ? { banco: "", tipo: "", numero: "", titular: "", llave: "" }
    : state.draft.configuracion.pagos.cuentas_transferencia[index];
  $("#cuenta-title").textContent = index === null ? "Agregar cuenta" : "Editar cuenta";
  for (const k of ["banco", "tipo", "numero", "titular", "llave"]) cuentaForm[k].value = c[k];
  showFormErrors(cuentaForm, {});
  cuentaDialog.showModal();
}

cuentaForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const cuenta = Object.fromEntries(["banco", "tipo", "numero", "titular", "llave"].map((k) => [k, cuentaForm[k].value.trim()]));
  const errors = validateCuenta(cuenta);
  if (Object.keys(errors).length) return showFormErrors(cuentaForm, errors);
  const cuentas = state.draft.configuracion.pagos.cuentas_transferencia;
  if (cuentaEditIndex === null) cuentas.push(cuenta);
  else cuentas[cuentaEditIndex] = cuenta;
  cuentaDialog.close();
  renderCuentas();
  refresh("pagos");
});

// --- Producto ------------------------------------------------
const productoDialog = $("#producto-dialog");
const productoForm = $("#producto-form");
let productoEditId = null;

function syncPrecioEnabled() {
  const noLegible = productoForm.tipo_precio.value === "no_legible";
  productoForm.precio.disabled = noLegible;
  if (noLegible) showError($("#perr-precio"), "");
}

function openProducto(id) {
  productoEditId = id;
  const p = id === null
    ? { nombre: "", descripcion: "", categoria: "", tipo_precio: "fijo", precio: "", is_active: true }
    : state.productos.find((x) => x.id === id);
  $("#producto-title").textContent = id === null ? "Agregar producto" : "Editar producto";
  productoForm.nombre.value = p.nombre;
  productoForm.descripcion.value = p.descripcion ?? "";
  productoForm.categoria.value = p.categoria ?? "";
  productoForm.tipo_precio.value = p.tipo_precio;
  productoForm.precio.value = p.precio ?? "";
  productoForm.is_active.checked = p.is_active;
  syncPrecioEnabled();
  showFormErrors(productoForm, {});
  productoDialog.showModal();
}

productoForm.tipo_precio.addEventListener("change", syncPrecioEnabled);

productoForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const data = {
    nombre: productoForm.nombre.value.trim(),
    descripcion: productoForm.descripcion.value.trim() || null,
    categoria: productoForm.categoria.value.trim(),
    tipo_precio: productoForm.tipo_precio.value,
    precio: productoForm.precio.value.trim(),
    is_active: productoForm.is_active.checked,
  };
  const errors = validateProducto(data);
  if (Object.keys(errors).length) return showFormErrors(productoForm, errors);
  data.precio = data.tipo_precio === "fijo" ? Number(data.precio) : null;

  const submit = productoForm.querySelector('[type="submit"]');
  submit.disabled = true;
  submit.textContent = "Guardando…";
  try {
    if (productoEditId === null) {
      state.productos.push(await api.createFixedItem(BUSINESS_ID, data));
      showToast("Producto agregado");
    } else {
      const updated = await api.updateFixedItem(BUSINESS_ID, productoEditId, data);
      state.productos = state.productos.map((p) => (p.id === updated.id ? updated : p));
      showToast("Producto guardado");
    }
    productoDialog.close();
    renderProductos();
  } catch (err) {
    showToast(`No se guardó el producto. ${err.message}`, "error");
  } finally {
    submit.disabled = false;
    submit.textContent = "Guardar producto";
  }
});

async function toggleProducto(input) {
  const id = input.dataset.id;
  input.disabled = true;
  try {
    const updated = await api.updateFixedItem(BUSINESS_ID, id, { is_active: input.checked });
    state.productos = state.productos.map((p) => (p.id === id ? updated : p));
    showToast(`${updated.nombre}: ${updated.is_active ? "activo" : "inactivo"}`);
  } catch (err) {
    input.checked = !input.checked;
    showToast(`No se cambió el estado. ${err.message}`, "error");
  } finally {
    input.disabled = false;
  }
}

// ------------------------------------------------------------
// Acciones (delegación de eventos)
// ------------------------------------------------------------
function addAdmin() {
  const input = $("#f-admin-new");
  const phone = input.value.trim();
  const phones = state.draft.configuracion.accesos.admin_phone_numbers;
  const error = validateNewAdminPhone(phone, phones);
  showError($("#err-admin-new"), error);
  if (error) return input.focus();
  phones.push(phone);
  input.value = "";
  renderAdmins();
  refresh("accesos");
}

const actions = {
  save: (btn, key) => saveSection(key, btn),
  discard: (btn, key) => discardSection(key),
  retry: () => load(),
  "add-admin": addAdmin,
  "remove-admin": async (btn) => {
    const phones = state.draft.configuracion.accesos.admin_phone_numbers;
    const phone = phones[btn.dataset.index];
    if (phones.length <= 1) return;
    const ok = await confirmar({ title: "Quitar número", message: `${phone} dejará de poder administrar el bot.`, confirmLabel: "Quitar", danger: true });
    if (!ok) return;
    phones.splice(Number(btn.dataset.index), 1);
    renderAdmins();
    refresh("accesos");
    $("#f-admin-new").focus();
  },
  "update-raspberry": (btn) => updateRaspberry(btn),
  "save-wifi": (btn) => saveWifi(btn),
  "test-print": (btn) => testPrint(btn),
  "add-zona": () => openZona(null),
  "edit-zona": (btn) => openZona(Number(btn.dataset.index)),
  "delete-zona": async (btn) => {
    const zonas = state.draft.configuracion.domicilios.zonas;
    const zona = zonas[btn.dataset.index];
    const ok = await confirmar({ title: "Eliminar zona", message: `Las direcciones de ${zona.nombre} pasarán a cotizarse a mano.`, confirmLabel: "Eliminar", danger: true });
    if (!ok) return;
    zonas.splice(Number(btn.dataset.index), 1);
    renderZonas();
    refresh("domicilios");
  },
  "add-cuenta": () => openCuenta(null),
  "edit-cuenta": (btn) => openCuenta(Number(btn.dataset.index)),
  "delete-cuenta": async (btn) => {
    const cuentas = state.draft.configuracion.pagos.cuentas_transferencia;
    const c = cuentas[btn.dataset.index];
    const ok = await confirmar({ title: "Eliminar cuenta", message: `El bot dejará de ofrecer la cuenta ${c.banco} ${maskNumber(c.numero)}.`, confirmLabel: "Eliminar", danger: true });
    if (!ok) return;
    cuentas.splice(Number(btn.dataset.index), 1);
    state.revealed.clear();
    renderCuentas();
    refresh("pagos");
  },
  "reveal-cuenta": (btn) => {
    const i = Number(btn.dataset.index);
    state.revealed.has(i) ? state.revealed.delete(i) : state.revealed.add(i);
    renderCuentas();
    $(`[data-action="reveal-cuenta"][data-index="${i}"]`).focus();
  },
  "add-producto": () => openProducto(null),
  "edit-producto": (btn) => openProducto(btn.dataset.id),
  "delete-producto": async (btn) => {
    const p = state.productos.find((x) => x.id === btn.dataset.id);
    const ok = await confirmar({ title: "Eliminar producto", message: `${p.nombre} dejará de ofrecerse por el bot. Esto no se puede deshacer.`, confirmLabel: "Eliminar", danger: true });
    if (!ok) return;
    try {
      await api.deleteFixedItem(BUSINESS_ID, p.id);
      state.productos = state.productos.filter((x) => x.id !== p.id);
      renderProductos();
      showToast("Producto eliminado");
    } catch (err) {
      showToast(`No se eliminó el producto. ${err.message}`, "error");
    }
  },
  "unlock-phone-id": async (btn) => {
    const ok = await confirmar({ title: "Editar el ID del número", message: "Si este valor no coincide con Meta, el bot dejará de recibir mensajes.", confirmLabel: "Editar de todos modos", danger: true });
    if (!ok) return;
    const input = $("#f-phone-id");
    input.readOnly = false;
    btn.hidden = true;
    input.focus();
  },
};

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-action]");
  if (!btn || btn.disabled || btn.matches('input[type="checkbox"]')) return;
  actions[btn.dataset.action]?.(btn, btn.closest("[data-section]")?.dataset.section);
});

document.addEventListener("change", (e) => {
  if (e.target.matches('[data-action="toggle-producto"]')) toggleProducto(e.target);
});

// Campos con data-path: actualizan el borrador en cada tecla
document.addEventListener("input", async (e) => {
  const input = e.target;
  const path = input.dataset?.path;
  if (!path || !state.draft) return;
  const key = input.closest("[data-section]").dataset.section;

  if (path === "negocio.status") {
    if (!input.checked) {
      const ok = await confirmar({
        title: "Desactivar el bot",
        message: "El bot dejará de responder a todos los clientes.",
        confirmLabel: "Desactivar",
        danger: true,
      });
      if (!ok) {
        input.checked = true;
        return;
      }
    }
    setPath(state.draft, path, input.checked ? "active" : "inactive");
  } else if (input.type === "radio") {
    if (input.checked) setPath(state.draft, path, input.value);
  } else {
    setPath(state.draft, path, input.value);
  }
  refresh(key);
});

$("#f-admin-new").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    addAdmin();
  }
});

// Aviso de cambios sin guardar al salir
window.addEventListener("beforeunload", (e) => {
  if (anyDirty()) {
    e.preventDefault();
    e.returnValue = "";
  }
});

// ------------------------------------------------------------
// Arranque
// ------------------------------------------------------------
const isSuperadmin = user.role === "superadmin";
document.querySelectorAll("[data-superadmin]").forEach((el) => (el.hidden = !isSuperadmin));
$("#user-name").textContent = `Rol: ${user.role}`;
$("#user-avatar").textContent = user.role.charAt(0);
if (!isSuperadmin) delete SECTIONS.integraciones;

// La navegación marca la sección que se está leyendo.
document.querySelectorAll("#plan-table tbody tr").forEach((tr, i) => tr.style.setProperty("--r", i));
const spy = new IntersectionObserver(
  (entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      document.querySelectorAll(".config-nav a").forEach((a) => {
        if (a.getAttribute("href") === `#${e.target.id}`) a.setAttribute("aria-current", "location");
        else a.removeAttribute("aria-current");
      });
    }
  },
  { rootMargin: "-30% 0px -60% 0px" }
);
document.querySelectorAll(".config-section").forEach((s) => spy.observe(s));

if (BUSINESS_ID) load();
else showBusinessPicker();
