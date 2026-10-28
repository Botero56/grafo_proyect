// Ejecutar: node configValidators.test.mjs
import assert from "node:assert/strict";
import * as v from "./configValidators.js";

assert.equal(v.formatCOP(25000), "$25.000");
assert.equal(v.formatCOP(5000), "$5.000");
assert.equal(v.formatCOP(0), "$0");
assert.equal(v.maskNumber("3001234567"), "****4567");
assert.equal(v.normalize("  Cerró   AZUL "), "cerro azul");

assert.equal(v.validateNewAdminPhone("573001234567", []), "");
assert.ok(v.validateNewAdminPhone("+573001234567", []));
assert.ok(v.validateNewAdminPhone("57300123456", []));
assert.ok(v.validateNewAdminPhone("573001234567", ["573001234567"]));

const zonas = [{ nombre: "Cerro Azul", tarifa: 3000, alias: ["cerro azul"] }];
assert.deepEqual(v.validateZona({ nombre: "La Ceiba", tarifa: 2000, alias: ["ceiba"] }, zonas), {});
assert.ok(v.validateZona({ nombre: "cerro azúl", tarifa: 1, alias: [] }, zonas).nombre);
assert.ok(v.validateZona({ nombre: "X", tarifa: 1.5, alias: [] }, zonas).tarifa);
assert.ok(v.validateZona({ nombre: "X", tarifa: -1, alias: [] }, zonas).tarifa);
assert.ok(v.validateZona({ nombre: "X", tarifa: "", alias: [] }, zonas).tarifa);
assert.ok(v.validateZona({ nombre: "X", tarifa: "3.000", alias: [] }, zonas).tarifa);
assert.deepEqual(v.validateZona({ nombre: "X", tarifa: 0, alias: [] }, zonas), {});
assert.ok(v.validateZona({ nombre: "X", tarifa: 0, alias: ["abc"] }, zonas).alias);
assert.ok(v.validateZona({ nombre: "X", tarifa: 0, alias: ["Cerro Azúl"] }, zonas).alias);

const domicilios = (hora_cierre, tiempo_entrega_min) => ({ configuracion: { domicilios: { zonas: [], hora_cierre, tiempo_entrega_min } } });
assert.deepEqual(v.validateDomicilios(domicilios("", "")), {});
assert.deepEqual(v.validateDomicilios(domicilios("21:30", "45")), {});
assert.ok(v.validateDomicilios(domicilios("25:00", ""))["configuracion.domicilios.hora_cierre"]);
for (const malo of ["0", "601", "4.5", "-3", "abc"]) {
  assert.ok(v.validateDomicilios(domicilios("", malo))["configuracion.domicilios.tiempo_entrega_min"], malo);
}

const cuenta = { banco: "Nequi", tipo: "billetera", numero: "3001234567", titular: "Edy", llave: "" };
assert.deepEqual(v.validateCuenta(cuenta), {});
assert.ok(v.validateCuenta({ ...cuenta, numero: "12345" }).numero);
assert.ok(v.validateCuenta({ ...cuenta, numero: "12a456" }).numero);
assert.ok(v.validateCuenta({ ...cuenta, titular: " " }).titular);

const comandas = (destino, endpoint_url, impresora = { token: "", token_configurado: true }) =>
  ({ configuracion: { comandas: { destino, impresora: { endpoint_url, ancho: "58mm", ...impresora } } } });
assert.deepEqual(v.validateComandas(comandas("chat_admin", "")), {});
assert.deepEqual(v.validateComandas(comandas("impresora", "https://print.example.com/x")), {});
assert.ok(Object.keys(v.validateComandas(comandas("impresora", "http://print.example.com"))).length);
assert.ok(Object.keys(v.validateComandas(comandas("impresora", ""))).length);
// Impresora nueva: sin token no se puede encender; uno corto tampoco.
const url = "https://print.example.com/x";
assert.ok(v.validateComandas(comandas("impresora", url, { token: "", token_configurado: false }))["configuracion.comandas.impresora.token"]);
assert.ok(v.validateComandas(comandas("impresora", url, { token: "corto", token_configurado: true }))["configuracion.comandas.impresora.token"]);
assert.deepEqual(v.validateComandas(comandas("impresora", url, { token: "x".repeat(16), token_configurado: false })), {});

assert.deepEqual(v.validateProducto({ nombre: "Sopa", tipo_precio: "no_legible", precio: null }), {});
assert.ok(v.validateProducto({ nombre: "Sopa", tipo_precio: "fijo", precio: "" }).precio);

assert.deepEqual(v.lostFeatures(v.PLANES.premium.features, "avanzado"), ["domiciliarios", "reportes"]);
assert.deepEqual(v.lostFeatures(v.PLANES.basico.features, "premium"), []);
assert.equal(v.planFor("impresora"), "Avanzado");
assert.equal(v.planFor("reportes"), "Premium");

console.log("configValidators: ok");
