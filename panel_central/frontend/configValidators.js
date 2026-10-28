// ============================================================
// configValidators.js — reglas de validación (sin DOM)
// Cada función devuelve { [campo]: "mensaje" }; vacío = válido.
// ============================================================

export const PHONE_RE = /^57\d{10}$/;
const PHONE_MSG = "Usa el formato 57 seguido de 10 dígitos, sin + ni espacios. Ej: 573001234567";

/** Minúsculas, sin tildes y sin espacios sobrantes: "Cerró  Azul" → "cerro azul". */
export function normalize(text) {
  return String(text ?? "")
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
}

/** Entero ≥ 0 escrito solo con dígitos ("3.000" no vale: Number lo leería como 3). */
export function isNonNegativeInt(value) {
  return value !== null && value !== undefined && /^\d+$/.test(String(value).trim());
}

/** "$25.000". Agrupa a mano porque es-CO no separa los miles en números de 4 dígitos. */
export function formatCOP(value) {
  return "$" + String(Math.round(Number(value))).replace(/\B(?=(\d{3})+(?!\d))/g, ".");
}

export function maskNumber(numero) {
  return "****" + String(numero).slice(-4);
}

/** Error para un teléfono nuevo de la lista de admins, o "" si es válido. */
export function validateNewAdminPhone(phone, existing) {
  if (!PHONE_RE.test(phone)) return PHONE_MSG;
  if (existing.includes(phone)) return "Ese número ya está en la lista.";
  return "";
}

/** @param {import("./configService.js").ConfiguracionNegocio} d */
export function validateGeneral(d) {
  const errors = {};
  if ((d.configuracion.general.nombre_comercial || "").length > 60) {
    errors["configuracion.general.nombre_comercial"] = "Máximo 60 caracteres.";
  }
  return errors;
}

export function validateAccesos(d) {
  const errors = {};
  const { admin_phone_numbers: phones, encargada } = d.configuracion.accesos;
  if (phones.length === 0) errors["configuracion.accesos.admin_phone_numbers"] = "Debe haber al menos un número de administración.";
  else if (phones.some((p) => !PHONE_RE.test(p))) errors["configuracion.accesos.admin_phone_numbers"] = "Hay números con formato inválido. " + PHONE_MSG;
  else if (new Set(phones).size !== phones.length) errors["configuracion.accesos.admin_phone_numbers"] = "Hay números repetidos.";
  if ((encargada.nombre || "").length > 40) errors["configuracion.accesos.encargada.nombre"] = "Máximo 40 caracteres.";
  return errors;
}

export function validateDomicilios(d) {
  const errors = {};
  const { hora_cierre, tiempo_entrega_min } = d.configuracion.domicilios;
  if (hora_cierre && !/^([01]\d|2[0-3]):[0-5]\d$/.test(hora_cierre)) {
    errors["configuracion.domicilios.hora_cierre"] = "Usa una hora válida (HH:MM).";
  }
  const minutos = String(tiempo_entrega_min ?? "").trim();
  if (minutos && (!isNonNegativeInt(minutos) || Number(minutos) < 1 || Number(minutos) > 600)) {
    errors["configuracion.domicilios.tiempo_entrega_min"] = "Escribe minutos enteros entre 1 y 600, o déjalo vacío.";
  }
  return errors;
}

export function validateComandas(d) {
  const errors = {};
  const { destino, impresora } = d.configuracion.comandas;
  if (destino !== "impresora") return errors;
  let url = null;
  try {
    url = new URL(impresora.endpoint_url);
  } catch {
    /* URL inválida */
  }
  if (!impresora.endpoint_url) errors["configuracion.comandas.impresora.endpoint_url"] = "La URL de la impresora es obligatoria.";
  else if (!url || url.protocol !== "https:" || !impresora.endpoint_url.startsWith("https://")) {
    errors["configuracion.comandas.impresora.endpoint_url"] = "Debe ser una URL válida que empiece por https://";
  }
  if (!["58mm", "80mm"].includes(impresora.ancho)) errors["configuracion.comandas.impresora.ancho"] = "Elige 58mm u 80mm.";
  if (impresora.token && impresora.token.trim().length < 16) {
    errors["configuracion.comandas.impresora.token"] = "El token debe tener al menos 16 caracteres.";
  } else if (!impresora.token && !impresora.token_configurado) {
    errors["configuracion.comandas.impresora.token"] = "La impresora todavía no tiene token: escríbelo.";
  }
  return errors;
}

export function validateIntegraciones(d) {
  const errors = {};
  if (!/^\d+$/.test(d.negocio.whatsapp_phone_number_id || "")) {
    errors["negocio.whatsapp_phone_number_id"] = "Solo dígitos; es obligatorio.";
  }
  if (d.negocio.bussiness_vertical !== "restaurant") errors["negocio.bussiness_vertical"] = "Por ahora solo está disponible Restaurante.";
  return errors;
}

/**
 * @param {import("./configService.js").Zona} zona
 * @param {import("./configService.js").Zona[]} otrasZonas  todas menos la que se edita
 */
export function validateZona(zona, otrasZonas) {
  const errors = {};
  const nombre = normalize(zona.nombre);
  if (!nombre) errors.nombre = "El nombre es obligatorio.";
  else if (otrasZonas.some((z) => normalize(z.nombre) === nombre)) errors.nombre = "Ya existe una zona con ese nombre.";
  if (!isNonNegativeInt(zona.tarifa)) errors.tarifa = "Escribe un valor entero de 0 o más, sin puntos.";
  const aliasError = zona.alias.map((a) => validateAlias(a, zona.alias.filter((x) => x !== a), otrasZonas)).find(Boolean);
  if (aliasError) errors.alias = aliasError;
  return errors;
}

/** Error de un alias contra los demás alias de su zona y los de otras zonas, o "". */
export function validateAlias(alias, aliasDeLaZona, otrasZonas) {
  const a = normalize(alias);
  if (a.length < 4) return `"${alias}" es muy corto: cada alias necesita 4 caracteres o más.`;
  if (aliasDeLaZona.some((x) => normalize(x) === a)) return `"${alias}" ya está en esta zona.`;
  const otra = otrasZonas.find((z) => z.alias.some((x) => normalize(x) === a));
  if (otra) return `"${alias}" ya se usa en la zona ${otra.nombre}.`;
  return "";
}

export function validateCuenta(c) {
  const errors = {};
  if (!c.banco) errors.banco = "Elige un banco.";
  if (!c.tipo) errors.tipo = "Elige el tipo de cuenta.";
  if (!/^\d{6,20}$/.test(c.numero)) errors.numero = "Solo dígitos, entre 6 y 20.";
  if (!c.titular.trim()) errors.titular = "El titular es obligatorio.";
  return errors;
}

export function validateProducto(p) {
  const errors = {};
  if (!p.nombre.trim()) errors.nombre = "El nombre es obligatorio.";
  if (p.tipo_precio === "fijo" && !isNonNegativeInt(p.precio)) errors.precio = "Escribe un precio entero de 0 o más, sin puntos.";
  return errors;
}

// ------------------------------------------------------------
// Planes. Quien bloquea es el backend (app/services/plans.py); esto es
// solo para mostrarlos y avisar antes de bajar de plan. Tras guardar,
// las features que valen son las que devuelve el backend.
// ------------------------------------------------------------

export const PLANES = {
  basico: { label: "Básico", precio: 350000, features: [] },
  avanzado: { label: "Avanzado", precio: 450000, features: ["comprobante_auto", "impresora"] },
  premium: { label: "Premium", precio: 550000, features: ["comprobante_auto", "impresora", "domiciliarios", "reportes"] },
};

/** Cómo se nombra cada función en los avisos. */
export const FEATURE_LABEL = {
  comprobante_auto: "la lectura automática de comprobantes",
  impresora: "la impresora",
  domiciliarios: "el bot de domiciliarios (con liquidación y cierre diario)",
  reportes: "los reportes (estadísticas y Excel)",
};

/** Qué deja de funcionar al pasar al plan `to`. */
export function lostFeatures(currentFeatures, to) {
  const next = PLANES[to]?.features ?? [];
  return currentFeatures.filter((f) => !next.includes(f));
}

/** El plan más barato que incluye `feature`. */
export function planFor(feature) {
  return Object.values(PLANES).find((p) => p.features.includes(feature))?.label ?? "";
}
