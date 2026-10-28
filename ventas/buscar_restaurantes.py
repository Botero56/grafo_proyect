#!/usr/bin/env python3
"""Lista de restaurantes de una ciudad colombiana sacada de OpenStreetMap.

Hace lo mismo que el buscador gratis de Harvey (Overpass), pero con las
categorías que nos sirven (restaurante, comida rápida, café) y guarda un CSV
listo para trabajar: nombre, teléfono, WhatsApp probable, web, Instagram y
dirección. Solo usa la librería estándar de Python.

    python3 ventas/buscar_restaurantes.py                    # Medellín y Bello
    python3 ventas/buscar_restaurantes.py --ciudad envigado itagui --salida sur.csv
    python3 ventas/buscar_restaurantes.py --lat 4.65 --lon -74.06 --radio 3

OpenStreetMap no tiene todos los restaurantes y muchos no traen teléfono. El
CSV pone primero los que sí se pueden contactar.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
USER_AGENT = "grafo-proyect-ventas/1.0 (lista de restaurantes para contacto comercial)"

# Centro aproximado y radio (km) que cubre la zona urbana.
CIUDADES = {
    "bogota": (4.6533, -74.0836, 12),
    "medellin": (6.2442, -75.5812, 8),
    "bello": (6.3373, -75.5579, 4),
    "envigado": (6.1759, -75.5917, 3),
    "itagui": (6.1846, -75.5991, 3),
    "cali": (3.4516, -76.5320, 9),
    "barranquilla": (10.9685, -74.7813, 8),
    "bucaramanga": (7.1193, -73.1227, 5),
    "cartagena": (10.3910, -75.4794, 7),
    "pereira": (4.8133, -75.6961, 5),
}
TIPOS = ("restaurant", "fast_food", "cafe", "food_court")
NOMBRE_TIPO = {"restaurant": "restaurante", "fast_food": "comida rápida",
               "cafe": "café", "food_court": "plazoleta"}
CAMPOS = ("nombre", "tipo", "cocina", "telefono", "whatsapp", "web", "instagram",
          "facebook", "email", "direccion", "barrio", "horario", "lat", "lon", "osm")


def consulta(lat: float, lon: float, radio_km: float) -> str:
    r = int(radio_km * 1000)
    partes = "".join(
        f'node["amenity"="{t}"]["name"](around:{r},{lat},{lon});'
        f'way["amenity"="{t}"]["name"](around:{r},{lat},{lon});'
        for t in TIPOS)
    return f"[out:json][timeout:120];({partes});out center tags;"


def pedir(query: str) -> dict:
    data = urllib.parse.urlencode({"data": query}).encode()
    ultimo = None
    for url in ENDPOINTS:
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=150) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            ultimo = e
            print(f"  {url} falló ({e}); probando el siguiente...", file=sys.stderr)
            time.sleep(3)
    raise SystemExit(f"Ningún servidor de OpenStreetMap respondió: {ultimo}")


def solo_digitos(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def telefonos(tags: dict) -> list[str]:
    crudo = ";".join(tags.get(k, "") for k in
                     ("phone", "contact:phone", "mobile", "contact:mobile", "contact:whatsapp"))
    return [p.strip() for p in re.split(r"[;,/]", crudo) if solo_digitos(p)]


def whatsapp(numeros: list[str], tags: dict) -> str:
    """Primer celular colombiano (3xx, 10 dígitos) en formato +57. Los fijos no tienen WhatsApp casi nunca."""
    for p in [tags.get("contact:whatsapp", "")] + numeros:
        d = solo_digitos(p)
        if d.startswith("57") and len(d) == 12:
            d = d[2:]
        if len(d) == 10 and d.startswith("3"):
            return "+57" + d
    return ""


def instagram(tags: dict) -> str:
    v = tags.get("contact:instagram") or tags.get("instagram") or ""
    v = v.strip()
    if v and not v.startswith("http"):
        v = "https://instagram.com/" + v.lstrip("@")
    return v


def fila(el: dict) -> dict | None:
    tags = el.get("tags", {})
    nombre = tags.get("name", "").strip()
    if not nombre:
        return None
    lat = el.get("lat") or el.get("center", {}).get("lat")
    lon = el.get("lon") or el.get("center", {}).get("lon")
    nums = telefonos(tags)
    direccion = tags.get("addr:full") or " ".join(
        x for x in (tags.get("addr:street", ""), tags.get("addr:housenumber", "")) if x)
    return {
        "nombre": nombre,
        "tipo": NOMBRE_TIPO.get(tags.get("amenity", ""), tags.get("amenity", "")),
        "cocina": tags.get("cuisine", "").replace(";", ", "),
        "telefono": " / ".join(nums),
        "whatsapp": whatsapp(nums, tags),
        "web": tags.get("website") or tags.get("contact:website", ""),
        "instagram": instagram(tags),
        "facebook": tags.get("contact:facebook", ""),
        "email": tags.get("email") or tags.get("contact:email", ""),
        "direccion": direccion,
        "barrio": tags.get("addr:suburb") or tags.get("addr:neighbourhood", ""),
        "horario": tags.get("opening_hours", ""),
        "lat": lat, "lon": lon,
        "osm": f"https://www.openstreetmap.org/{el.get('type')}/{el.get('id')}",
    }


def contactable(f: dict) -> int:
    """Orden: WhatsApp > teléfono > Instagram > web > nada."""
    return (0 if f["whatsapp"] else 1 if f["telefono"] else 2 if f["instagram"]
            else 3 if f["web"] else 4)


def procesar(respuesta: dict) -> list[dict]:
    vistos, filas = set(), []
    for el in respuesta.get("elements", []):
        if (el.get("type"), el.get("id")) in vistos:  # zonas que se cruzan (Medellín y Bello)
            continue
        vistos.add((el.get("type"), el.get("id")))
        f = fila(el)
        if not f:
            continue
        clave = (f["nombre"].lower(), solo_digitos(f["telefono"])[-7:] or f["direccion"].lower())
        if clave in vistos:
            continue
        vistos.add(clave)
        filas.append(f)
    filas.sort(key=lambda f: (contactable(f), f["nombre"].lower()))
    return filas


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ciudad", nargs="+", choices=sorted(CIUDADES), default=["medellin", "bello"],
                    help="una o varias ciudades (por defecto: medellin bello)")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--radio", type=float, help="km alrededor del centro (para --lat/--lon o una sola ciudad)")
    ap.add_argument("--solo-contactables", action="store_true",
                    help="deja fuera los que no tienen teléfono, Instagram ni web")
    ap.add_argument("--salida", default="restaurantes.csv")
    a = ap.parse_args()

    if a.lat is not None and a.lon is not None:
        zonas = [(a.lat, a.lon, a.radio or 3)]
    else:
        zonas = [(lat, lon, a.radio if a.radio and len(a.ciudad) == 1 else r)
                 for lat, lon, r in (CIUDADES[c] for c in a.ciudad)]

    elementos = []
    for i, (lat, lon, radio) in enumerate(zonas):
        if i:
            time.sleep(5)  # Overpass es un servicio comunitario: una consulta a la vez
        print(f"Buscando restaurantes a {radio} km de {lat},{lon} en OpenStreetMap...", file=sys.stderr)
        elementos += pedir(consulta(lat, lon, radio)).get("elements", [])
    filas = procesar({"elements": elementos})
    if a.solo_contactables:
        filas = [f for f in filas if contactable(f) < 4]

    with open(a.salida, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=CAMPOS)
        w.writeheader()
        w.writerows(filas)

    con_wa = sum(1 for f in filas if f["whatsapp"])
    con_tel = sum(1 for f in filas if f["telefono"])
    con_ig = sum(1 for f in filas if f["instagram"])
    print(f"{len(filas)} restaurantes guardados en {a.salida}: "
          f"{con_wa} con celular (WhatsApp probable), {con_tel} con algún teléfono, "
          f"{con_ig} con Instagram.", file=sys.stderr)


if __name__ == "__main__":
    main()
