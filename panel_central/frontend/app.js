// ============================================================
// app.js — Panel Central: alta y listado de negocios
// Habla con la API admin a través del proxy del panel (mismo origen,
// detrás de Cloudflare Access): el login lo hace Cloudflare, aquí no
// hay usuario/contraseña ni API keys.
// ============================================================

import { showToast, escapeHtml } from "./ui.js";
import * as api from "./configService.js";
import { PLANES } from "./configValidators.js";

const statusEl = document.getElementById("connection-status");
const statusTextEl = document.getElementById("connection-status-text");
const form = document.getElementById("client-form");
const formMessage = document.getElementById("form-message");
const result = document.getElementById("create-result");
const tbody = document.getElementById("clients-tbody");
const countBadge = document.getElementById("count-badge");
const searchInput = document.getElementById("search-input");
const sortNombreBtn = document.getElementById("sort-nombre");
const submitBtn = form.querySelector("button[type='submit']");

let allClients = [];
let sortAsc = true;

function setStatus(state, text) {
  statusEl.className = `status-pill status-pill--${state}`;
  statusTextEl.textContent = text;
}

function setFormMessage(type, text) {
  formMessage.textContent = text;
  formMessage.className = `form-message ${type}`;
}

// ------------------------------------------------------------
// Tabla de negocios
// ------------------------------------------------------------
function renderClients(clients) {
  countBadge.textContent = allClients.length;

  if (!clients.length) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="5">${
      allClients.length ? "Ningún negocio coincide con la búsqueda." : "Aún no hay negocios. Registra el primero con el formulario."
    }</td></tr>`;
    return;
  }

  tbody.innerHTML = clients
    .map(
      (c) => `
      <tr>
        <td>${escapeHtml(c.name || c.business_id)}</td>
        <td class="cell-fecha">${escapeHtml(c.whatsapp_phone_number) || "—"}</td>
        <td>${escapeHtml(PLANES[c.plan]?.label ?? c.plan)}</td>
        <td><span class="bot-signal${c.status === "active" ? " is-live" : ""}"><span class="bars" aria-hidden="true"><i></i><i></i><i></i></span>${c.status === "active" ? "Bot encendido" : "Bot apagado"}</span></td>
        <td><a href="configuracion.html?business=${encodeURIComponent(c.business_id)}">Configurar</a></td>
      </tr>`
    )
    .join("");
}

function applyFilter() {
  const term = searchInput.value.trim().toLowerCase();
  const list = allClients.filter(
    (c) => !term || (c.name || "").toLowerCase().includes(term) || (c.whatsapp_phone_number || "").includes(term)
  );
  list.sort((a, b) => (sortAsc ? 1 : -1) * (a.name || "").localeCompare(b.name || ""));
  renderClients(list);
}

function toggleSort() {
  sortAsc = !sortAsc;
  sortNombreBtn.querySelector(".sort-icon").classList.toggle("desc", !sortAsc);
  applyFilter();
}

async function cargarNegocios() {
  try {
    setStatus("pending", "Cargando negocios…");
    allClients = await api.listBusinesses();
    applyFilter();
    setStatus("ok", "Conectado al backend");
  } catch (err) {
    console.error(err);
    setStatus("error", err.message || "No se pudo conectar con el backend");
  }
}

// ------------------------------------------------------------
// Alta de negocio (por ahora todos los números van por Kapso)
// ------------------------------------------------------------

async function handleSubmit(event) {
  event.preventDefault();
  setFormMessage("", "");
  result.hidden = true;

  const val = (name) => form[name].value.trim();
  const data = {
    name: val("name"),
    legal_name: val("legal_name"),
    bussiness_vertical: form.bussiness_vertical.value,
    phone: val("phone"),
    whatsapp_phone_number: val("whatsapp_phone_number"),
    whatsapp_phone_number_id: val("whatsapp_phone_number_id"),
    whatsapp_provider: "kapso",
    address: val("address"),
    plan: form.plan.value,
  };
  const kapso = { api_key: val("kapso_api_key"), webhook_secret: val("kapso_webhook_secret") };

  submitBtn.disabled = true;
  try {
    const created = await api.createBusiness(data, kapso);
    form.reset();
    showToast("Negocio registrado");
    showResult(created, data.name);
    cargarNegocios();
  } catch (err) {
    console.error(err);
    setFormMessage("error", err.message || "No se pudo registrar el negocio.");
  } finally {
    submitBtn.disabled = false;
  }
}

function showResult({ business_id, webhook_url, kapso_error }, name) {
  const configUrl = `configuracion.html?business=${encodeURIComponent(business_id)}`;
  result.innerHTML = `
    <p><strong>${escapeHtml(name)}</strong> quedó registrado.
      <a href="${configUrl}">Terminar su configuración</a></p>
    ${webhook_url ? `
      <div class="field">
        <label for="webhook-url">URL del webhook para registrar en Kapso</label>
        <input id="webhook-url" type="text" readonly value="${escapeHtml(webhook_url)}" />
      </div>
      <button type="button" class="btn-secondary" id="copy-webhook">Copiar URL</button>` : ""}
    ${kapso_error ? `<p class="field-error">No se guardaron las credenciales de Kapso (${escapeHtml(kapso_error)}). Cárgalas desde Configuración.</p>` : ""}`;
  result.hidden = false;
  document.getElementById("copy-webhook")?.addEventListener("click", async () => {
    await navigator.clipboard.writeText(webhook_url);
    showToast("URL copiada");
  });
}

form.addEventListener("submit", handleSubmit);
searchInput.addEventListener("input", applyFilter);
sortNombreBtn.addEventListener("click", toggleSort);

cargarNegocios();
