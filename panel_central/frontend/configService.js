// ============================================================
// configService.js — Configuración del negocio (bot de WhatsApp)
// Habla con la API admin del backend a través del proxy del panel
// (j2ys-panel, mismo origen): el proxy agrega la API key y el token.
// Aquí NO deben vivir API keys ni tokens.
//
// La pantalla trabaja con un modelo por secciones (ver los tipos);
// este archivo lo traduce a los endpoints reales del backend:
//   GET/PATCH  /businesses/{id}            negocio
//   PUT        /businesses/{id}/bot-status estado del bot
//   PUT        /businesses/{id}/plan       plan comercial
//   GET/PATCH  /configurations/{id}        configuración (plana)
//   PUT        /configurations/{id}/admin-phone-numbers
//   GET/PUT    /configurations/{id}/printer
//   /restaurant/fixed-menu-items           productos fijos
// ============================================================

import { PLANES } from "./configValidators.js";

const API = "/api/v1/admin";

// ------------------------------------------------------------
// Tipos (modelo de la pantalla)
// ------------------------------------------------------------

/**
 * @typedef {"active" | "inactive"} EstadoNegocio
 *
 * @typedef {Object} Negocio
 * @property {string} business_id
 * @property {string} name
 * @property {EstadoNegocio} status
 * @property {string} whatsapp_phone_number_id
 * @property {string} bussiness_vertical  // sic: así lo llama el backend
 *
 * @typedef {Object} Encargada
 * @property {string} nombre
 *
 * @typedef {Object} Accesos
 * @property {string[]} admin_phone_numbers
 * @property {Encargada} encargada
 *
 * @typedef {Object} Zona
 * @property {string} nombre
 * @property {number} tarifa
 * @property {string[]} alias
 *
 * @typedef {Object} CuentaTransferencia
 * @property {string} banco
 * @property {"ahorros" | "corriente" | "billetera"} tipo
 * @property {string} numero
 * @property {string} titular
 * @property {string} llave
 *
 * @typedef {Object} Comandas
 * @property {"impresora" | "chat_admin"} destino
 * @property {{ endpoint_url: string, ancho: "58mm" | "80mm", token: string, token_configurado: boolean }} impresora
 *   token: solo de escritura; vacío = conservar el que ya tiene.
 *
 * @typedef {Object} Configuracion
 * @property {{ nombre_comercial: string }} general
 * @property {Accesos} accesos
 * @property {{ zonas: Zona[], hora_cierre: string, tiempo_entrega_min: string }} domicilios
 *   hora_cierre: "HH:MM" o "" (sin cierre). tiempo_entrega_min: minutos como texto o "" (nunca se presume entregado).
 * @property {{ cuentas_transferencia: CuentaTransferencia[] }} pagos
 * @property {Comandas} comandas
 *
 * @typedef {Object} ProductoFijo
 * @property {string} id
 * @property {string} nombre
 * @property {string | null} descripcion
 * @property {number | null} precio
 * @property {"fijo" | "no_legible"} tipo_precio
 * @property {string} categoria
 * @property {boolean} is_active
 * @property {"manual" | "catalogo"} origen  // catalogo = lo mantiene la sincronización
 *
 * @typedef {Object} ConfiguracionNegocio
 * @property {Negocio} negocio
 * @property {Configuracion} configuracion
 *
 * @typedef {"general" | "accesos" | "domicilios" | "pagos" | "comandas" | "negocio"} Seccion
 */

// ------------------------------------------------------------
// Red
// ------------------------------------------------------------

/** Mensaje legible de un error del backend (400/404/409: texto; 422: lista de pydantic). */
function errorMessage(status, body) {
  const detail = body?.detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg.replace(/^Value error, /, "")).join(" · ");
  if (typeof detail === "string") return detail;
  if (status === 401 || status === 403) return "Sin permiso. Vuelve a entrar al panel.";
  if (status === 404) return "No encontrado.";
  return `Error del servidor (${status}).`;
}

async function request(method, path, body) {
  let response;
  try {
    response = await fetch(`${API}${path}`, {
      method,
      credentials: "same-origin",
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new Error("No se pudo contactar al servidor.");
  }
  const data = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(errorMessage(response.status, data));
    error.status = response.status;
    throw error;
  }
  return data;
}

const enc = encodeURIComponent;

// ------------------------------------------------------------
// Traducción backend <-> pantalla
// ------------------------------------------------------------

function toScreen(business, config, printer) {
  return {
    negocio: {
      business_id: business.bussiness_id,
      name: business.name,
      status: business.status,
      whatsapp_phone_number_id: business.whatsapp_phone_number_id || "",
      bussiness_vertical: business.bussiness_vertical,
      // Sin plan guardado el backend responde "premium". Un backend anterior
      // a los planes no manda features: se usan las del plan.
      plan: business.plan || "premium",
      features: business.features ?? PLANES[business.plan || "premium"].features,
    },
    configuracion: {
      general: { nombre_comercial: config.restaurant_name || "" },
      accesos: {
        admin_phone_numbers: config.admin_phone_numbers || [],
        encargada: { nombre: config.manager_name || "" },
      },
      recordatorio: { hora: config.recordatorio_menu_hora || "", texto: config.recordatorio_menu_texto || "" },
      domicilios: {
        zonas: config.delivery_zones || [],
        hora_cierre: config.hora_cierre_domicilios || "",
        tiempo_entrega_min: config.tiempo_entrega_estimado_min == null ? "" : String(config.tiempo_entrega_estimado_min),
      },
      pagos: {
        cuentas_transferencia: (config.cuentas_transferencia || []).map((c) => ({ ...c, llave: c.llave || "" })),
      },
      comandas: {
        destino: !config.orders_to_admin_chat && printer.enabled ? "impresora" : "chat_admin",
        impresora: {
          endpoint_url: printer.endpoint_url || "",
          ancho: printer.paper_width || "58mm",
          token: "",
          token_configurado: printer.token_configured,
        },
      },
    },
  };
}

const toProducto = (item) => ({
  id: item.fixed_item_id,
  nombre: item.nombre,
  descripcion: item.descripcion ?? null,
  precio: item.precio ?? null,
  tipo_precio: item.tipo_precio,
  categoria: item.categoria,
  is_active: item.is_active,
  origen: item.catalog_retailer_id ? "catalogo" : "manual",
});

const fromProducto = ({ nombre, descripcion, precio, tipo_precio, categoria, is_active }) => ({
  nombre, descripcion: descripcion || null, precio, tipo_precio, categoria, is_active,
});

// ------------------------------------------------------------
// API pública
// ------------------------------------------------------------

/** @returns {Promise<{business_id: string, name: string, status: string, whatsapp_phone_number: string, whatsapp_provider: string | null}[]>} */
export async function listBusinesses() {
  const businesses = await request("GET", "/businesses");
  return businesses.map((b) => ({
    business_id: b.bussiness_id, name: b.name, status: b.status, whatsapp_phone_number: b.whatsapp_phone_number,
    whatsapp_provider: b.whatsapp_provider ?? null, plan: b.plan || "premium",
  }));
}

/**
 * Alta de un negocio: el backend crea también su configuración vacía.
 * Si es de Kapso y llegan credenciales, se guardan en Secret Manager.
 * Devuelve el id y la URL del webhook a registrar en Kapso.
 * @param {{name: string, legal_name: string, bussiness_vertical: string, phone: string,
 *   whatsapp_phone_number: string, whatsapp_phone_number_id: string,
 *   whatsapp_provider: "kapso", address: string, plan: "basico" | "avanzado" | "premium"}} data
 * @param {{api_key: string, webhook_secret: string}} [kapso]
 */
export async function createBusiness(data, kapso) {
  const business = await request("POST", "/businesses", { ...data, address: data.address || null });
  const id = enc(business.bussiness_id);
  let kapsoError = null;
  if (kapso && (kapso.api_key || kapso.webhook_secret)) {
    try {
      await request("PUT", `/businesses/${id}/kapso-credentials`, {
        ...(kapso.api_key ? { api_key: kapso.api_key } : {}),
        ...(kapso.webhook_secret ? { webhook_secret: kapso.webhook_secret } : {}),
      });
    } catch (err) {
      // El negocio ya quedó creado: no se deshace, se avisa para cargarlas en Configuración.
      kapsoError = err.message;
    }
  }
  const { url } = await request("GET", `/businesses/${id}/webhook-url`).catch(() => ({ url: null }));
  return { business_id: business.bussiness_id, webhook_url: url, kapso_error: kapsoError };
}

/** @returns {Promise<ConfiguracionNegocio>} */
export async function getConfiguration(businessId) {
  const id = enc(businessId);
  const [business, config, printer] = await Promise.all([
    request("GET", `/businesses/${id}`),
    request("GET", `/configurations/${id}`),
    request("GET", `/configurations/${id}/printer`),
  ]);
  return toScreen(business, config, printer);
}

/**
 * Guarda una sección completa. "negocio" acepta un parche parcial
 * (status, whatsapp_phone_number_id). Devuelve lo que quedó guardado.
 * @param {string} businessId
 * @param {Seccion} section
 * @param {object} data
 */
export async function updateSection(businessId, section, data) {
  const id = enc(businessId);
  const patchConfig = (body) => request("PATCH", `/configurations/${id}`, body);

  switch (section) {
    case "negocio": {
      if ("bussiness_vertical" in data) throw new Error("El tipo de negocio no se cambia después del alta.");
      if ("status" in data) {
        // actor lo pone el proxy con el correo que verificó IAP.
        await request("PUT", `/businesses/${id}/bot-status`, { status: data.status, actor: "panel" });
      }
      if ("whatsapp_phone_number_id" in data) {
        await request("PATCH", `/businesses/${id}`, { whatsapp_phone_number_id: data.whatsapp_phone_number_id });
      }
      if ("plan" in data) {
        // actor lo pone el proxy, igual que en bot-status. Las features que
        // valen son las que devuelve el backend.
        const saved = await request("PUT", `/businesses/${id}/plan`, { plan: data.plan, actor: "panel" });
        return { ...data, features: saved.features };
      }
      return data;
    }
    case "general":
      await patchConfig({ restaurant_name: data.nombre_comercial.trim() || null });
      return data;
    case "accesos":
      await request("PUT", `/configurations/${id}/admin-phone-numbers`, { phone_numbers: data.admin_phone_numbers });
      // manager_phone va siempre vacío: sin él, el bot avisa a los números de administración.
      await patchConfig({ manager_name: data.encargada.nombre.trim() || null, manager_phone: null });
      return data;
    case "recordatorio": {
      // El backend lo deja en una línea y sin punto final (va en una plantilla).
      const config = await patchConfig({
        recordatorio_menu_hora: data.hora || null,
        recordatorio_menu_texto: data.texto.trim() || null,
      });
      return { hora: config.recordatorio_menu_hora || "", texto: config.recordatorio_menu_texto || "" };
    }
    case "domicilios": {
      const minutos = String(data.tiempo_entrega_min ?? "").trim();
      // null = sin cierre / nunca se presume entregado.
      const config = await patchConfig({
        delivery_zones: data.zonas,
        hora_cierre_domicilios: data.hora_cierre || null,
        tiempo_entrega_estimado_min: minutos ? Number(minutos) : null,
      });
      return {
        ...data,
        hora_cierre: config.hora_cierre_domicilios || "",
        tiempo_entrega_min: config.tiempo_entrega_estimado_min == null ? "" : String(config.tiempo_entrega_estimado_min),
      };
    }
    case "pagos":
      await patchConfig({
        cuentas_transferencia: data.cuentas_transferencia.map((c) => ({ ...c, llave: c.llave || null })),
      });
      return data;
    case "comandas": {
      const { destino, impresora } = data;
      if (destino === "impresora") {
        const printer = await request("PUT", `/configurations/${id}/printer`, {
          enabled: true,
          endpoint_url: impresora.endpoint_url,
          paper_width: impresora.ancho,
          // Omitido = conserva el token actual.
          ...(impresora.token ? { token: impresora.token } : {}),
        });
        await patchConfig({ orders_to_admin_chat: false });
        return { ...data, impresora: { ...impresora, token: "", token_configurado: printer.token_configured } };
      }
      await patchConfig({ orders_to_admin_chat: true });
      return data;
    }
    default:
      throw new Error(`Sección desconocida: ${section}`);
  }
}

// ------------------------------------------------------------
// Raspberry de la impresora: el backend la llama con la URL y el
// token guardados (el token no pasa por aquí).
// ------------------------------------------------------------

/** @returns {Promise<{version: string}>} */
export const getPrinterDevice = (businessId) => request("GET", `/configurations/${enc(businessId)}/printer/device`);

/**
 * Baja el código nuevo de GitLab en la Raspberry y la reinicia.
 * @returns {Promise<{updated: false, version: string} | {updated: true, from: string, to: string, commit: string}>}
 */
export const updatePrinterDevice = (businessId) => request("POST", `/configurations/${enc(businessId)}/printer/update`);

/** Imprime un ticket corto de prueba. @returns {Promise<{ok: true}>} */
export const printTestPage = (businessId) => request("POST", `/configurations/${enc(businessId)}/printer/test`);

/**
 * Guarda en la Raspberry el Wi-Fi del negocio (la clave no se guarda en el backend).
 * @returns {Promise<{saved: true, ssid: string}>}
 */
export const setPrinterWifi = (businessId, ssid, password) =>
  request("POST", `/configurations/${enc(businessId)}/printer/wifi`, { ssid, password });

/**
 * Raspberries del negocio con su último heartbeat. `status` lo calcula el
 * backend al leer: "operativo" | "degradado" | "desconectado" | "sin_informacion".
 * @returns {Promise<Array<{device_id: string, status: string, seconds_since_seen: number|null,
 *   version: string|null, printer_usb: boolean|null, wifi_ssid: string|null,
 *   last_print_ok_at: string|null, last_error: string|null, last_error_at: string|null}>>}
 */
export const listPrinterDevices = (businessId) => request("GET", `/configurations/${enc(businessId)}/printer/devices`);

/**
 * Historial de las Raspberries: un evento por cambio (conexión, impresora USB,
 * Wi-Fi, versión, errores), del más nuevo al más viejo.
 * @returns {Promise<Array<{device_id: string, event_type: string, at: string, until: string|null, detail: string|null}>>}
 */
export const listPrinterEvents = (businessId, limit = 50) =>
  request("GET", `/configurations/${enc(businessId)}/printer/events?limit=${limit}`);

/** @returns {Promise<ProductoFijo[]>} */
export async function listFixedItems(businessId) {
  const items = await request("GET", `/restaurant/fixed-menu-items/${enc(businessId)}`);
  return items.map(toProducto);
}

/** @param {Omit<ProductoFijo, "id" | "origen">} item */
export async function createFixedItem(businessId, item) {
  return toProducto(await request("POST", "/restaurant/fixed-menu-items", { business_id: businessId, ...fromProducto(item) }));
}

/** Solo productos con origen "manual"; los del catálogo los pisa la sincronización. */
export async function updateFixedItem(businessId, itemId, item) {
  const body = "nombre" in item ? fromProducto(item) : item; // { is_active } al prender/apagar
  return toProducto(await request("PATCH", `/restaurant/fixed-menu-items/${enc(itemId)}?business_id=${enc(businessId)}`, body));
}

export async function deleteFixedItem(businessId, itemId) {
  await request("DELETE", `/restaurant/fixed-menu-items/${enc(itemId)}?business_id=${enc(businessId)}`);
  return null;
}
