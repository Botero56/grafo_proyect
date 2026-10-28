# Configuración por cliente (negocio)

Catálogo de lo que cada negocio debe poder configurar por separado desde el
panel (`https://panel.3b-technology.com`, front en `frontend.git`). Revisado
contra el código el 2026-10-20.

**Hoy:** ✅ ya es por cliente · 🌐 hoy es igual para todos y debería ser por cliente · 🆕 no existe.

## 1. Identidad y región
| Parámetro | Para qué sirve | Hoy | En el front | Dónde vive / se lee |
|---|---|---|---|---|
| Nombre comercial | Cómo se presenta el bot; encabezado de la comanda | ✅ | Texto | `configurations.restaurant_name` (`tenant_profile.py`, `print_jobs.py`) |
| Razón social, teléfono | Datos administrativos | ✅ | Texto | `businesses.legal_name`, `phone` (`PATCH /admin/businesses/{id}`) |
| Tipo de negocio (vertical) | Qué agente lo atiende | ✅ (solo restaurante) | Lista, solo al crear | `businesses.bussiness_vertical` (`vertical_registry.py`) |
| Zona horaria | Horario, fecha de pedidos, recordatorios, cierre | 🌐 Bogotá | Lista | `DEFAULT_TZ` en `excel_report_service/constants.py:42`; `opening_hours.timezone` ya existe pero no todo lo usa |
| País (indicativo) | Normalizar teléfonos | 🌐 Colombia | Lista | `phones.py:31`, `handoff_service.py:37`, `print_jobs.py:38` |
| Moneda | Precios, totales, comprobantes | 🌐 COP | Lista | `firebase_schemas.py` (`moneda`), `format_cop` |
| Idioma | En qué responde el bot | 🌐 español | Lista | prompt de clientes, línea 29 |

## 2. Canal WhatsApp (protegido)
| Parámetro | Para qué sirve | Hoy | En el front | Dónde |
|---|---|---|---|---|
| Número y su ID en Meta | Recibir y enviar por ese número | ✅ | Texto con confirmación | `businesses.whatsapp_phone_number(_id)` |
| Proveedor (Meta/Kapso) | Por dónde se envía | ✅ | Lista | `businesses.whatsapp_provider` |
| Credenciales de Kapso | Envío y firma del webhook | ✅ (Secret Manager) | Solo escritura, "configurada sí/no" | `PUT .../kapso-credentials` |
| URL del webhook | Lo que se registra en Kapso | ✅ | Copiar + generar otra | `GET .../webhook-url`, `POST .../rotate` |
| Bot encendido/apagado | Kill switch | ✅ | Interruptor | `PUT .../bot-status` |

## 3. Personas
| Parámetro | Para qué sirve | Hoy | En el front | Dónde |
|---|---|---|---|---|
| Administradores | Quién maneja el bot por chat | ✅ | Lista de teléfonos | `PUT .../admin-phone-numbers` |
| Encargada (nombre, teléfono) | A quién se avisa y cómo se le nombra | ✅ | Texto | `manager_name`, `manager_phone` |
| Destinatario por tipo de aviso | Comandas a cocina, cotizaciones a despacho… | 🆕 (todo va a la encargada o a todos los admins) | Teléfono por aviso | `handoff_service.notify_admins` (`:219`) |
| Domiciliarios | Liquidar comandas, reportar novedades | ✅ | Lista nombre + teléfono | `PUT .../couriers` |

## 4. Horario
| Parámetro | Para qué sirve | Hoy | En el front | Dónde |
|---|---|---|---|---|
| Horario semanal y festivos | Cuándo atiende y qué dice | ✅ | Rangos por día + excepciones | `opening_hours` |
| Horario estricto | No confirmar fuera de horario | ✅ | Interruptor | `opening_hours.enforce` |

## 5. Menú y productos
| Parámetro | Para qué sirve | Hoy | En el front | Dónde |
|---|---|---|---|---|
| Usa menú del día | Foto diaria o solo carta | 🆕 (se deduce de la hora del recordatorio) | Interruptor | — |
| Hora del recordatorio del menú | Avisar al admin si no lo subió | ✅ | Hora; vacío = no recordar | `recordatorio_menu_hora` (`reminders.py`) |
| Hora en que suele estar el menú | Lo que dice el bot mientras tanto | ✅ | Hora | `daily_menu_expected_ready_time` |
| Enviar menú al iniciar | Foto en el primer mensaje del día | ✅ | Interruptor | `enviar_menu_al_iniciar` |
| Productos fijos | Lo que se vende siempre | ✅ | Lista | `/admin/restaurant/fixed-menu-items` |
| Catálogo de WhatsApp | Sincronizar desde Meta | ✅ | ID numérico | `whatsapp_catalog_id` |

## 6. Pedidos
| Parámetro | Para qué sirve | Hoy | En el front | Dónde |
|---|---|---|---|---|
| Modalidades (domicilio/recoger/en el local) | Hoy no se puede vender sin dirección | 🆕 | Casillas | `order_tools.py:85` (`_missing` exige dirección) |
| Dirección del local | Para quien recoge | 🆕 | Texto | — |
| Pedido mínimo | No confirmar pedidos pequeños | ✅ | Número | `pedido_minimo` |
| Cantidad máxima por producto | Errores o pedidos grandes | 🌐 20 | Número | `customer_tools.py:216` |

## 7. Pagos
| Parámetro | Para qué sirve | Hoy | En el front | Dónde |
|---|---|---|---|---|
| Formas de pago | Efectivo y/o transferencia | ✅ | Casillas | `metodos_pago` ([] = las dos) |
| Cuentas de transferencia | Lo que el bot da para pagar | ✅ | Lista | `cuentas_transferencia` (`confirm_order`) |
| Validación del comprobante | Automática o revisión humana | 🆕 (siempre automática; no revisa la cuenta destino) | Opción | `transfer_receipt.py` |
| Acuerdos de pago (tiquetera) | Si el bot los ofrece | 🆕 (siempre) | Interruptor | `order_tools.py:160`, prompt línea 143 |
| Pregunta de propina | Si pregunta al transferir de más | 🆕 (siempre) | Interruptor | `transfer_receipt.py` |

## 8. Domicilios
| Parámetro | Para qué sirve | Hoy | En el front | Dónde |
|---|---|---|---|---|
| Zonas con tarifa | Cobrar sin humano | ✅ | Tabla | `delivery_zones` |
| Dirección fuera de zona | Cotizar a mano o tarifa base | 🆕 (siempre a mano) | Opción + valor | `delivery/quotes.py` |
| Minutos para "presunto entregado" | Cerrar pedidos sin reclamo | ✅ | Número | `tiempo_entrega_estimado_min` |
| Hora del cierre diario | Resumen de liquidación | ✅ | Hora | `hora_cierre_domicilios` |
| Espera y reintentos de incidencias | Cuando no se logra entregar | ✅ | Dos números | `incidencia_espera_min`, `incidencia_reintentos` |

## 9. Comandas
| Parámetro | Para qué sirve | Hoy | En el front | Dónde |
|---|---|---|---|---|
| Destino (impresora o chat) | A dónde llega el pedido | ✅ | Opción | `orders_to_admin_chat` |
| Impresora (URL, token, papel, copias, espera) | Imprimir en el local | ✅ | Campos; token solo escritura | `PUT .../printer` |

## 10. Comportamiento del bot
| Parámetro | Para qué sirve | Hoy | En el front | Dónde |
|---|---|---|---|---|
| Tono | Estilo de respuesta | ✅ | Texto | `tono` |
| Preguntas frecuentes | Respuestas exactas | ✅ | Lista | `faq` |
| Mensaje al pasar con una persona | Lo que recibe el cliente | ✅ | Texto | `handoff_transfer_message` |
| Mensajes de error y "fuera de tema" | Hoy dicen "restaurante" a todos | 🌐 | Texto | `agent_runner.py:34`, `reply_guard.py:55` |
| Tiempo para retomar tras un humano | 30 s para todos | 🌐 | Segundos | `HANDOFF_QUICK_REACTIVATION_SECONDS` (`config.py:93`) |
| Reactivación automática | 4 h para todos | 🌐 | Horas | `HANDOFF_INACTIVITY_MINUTES` (`config.py:87`) |

## 11. Plantillas fuera de 24 h
| Parámetro | Para qué sirve | Hoy | En el front | Dónde |
|---|---|---|---|---|
| Plantilla por aviso (5) | Avisar al admin sin ventana de 24 h | ✅ | Nombre + idioma por aviso | `admin_templates` |

## Resumen y siguiente paso
34 parámetros: 21 existen (✅), 6 son globales y deben pasar a por cliente (🌐),
7 son nuevos (🆕). Pendiente decidir por qué grupo empezar:
1. ✅ que el front aún no muestra (solo `frontend.git`, rama `feat/conectar-backend`).
2. 🌐 globales → por cliente (backend aquí + front allá).
3. 🆕 nuevos (backend aquí + front allá).

Se queda global a propósito: modelo de IA, ventana de agrupado de mensajes,
memoria de la conversación, umbral de zonas y tolerancia del comprobante.
