"""Prueba sin red: procesa una respuesta de Overpass de ejemplo (datos inventados)."""
import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location(
    "buscar", pathlib.Path(__file__).with_name("buscar_restaurantes.py"))
buscar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(buscar)

EJEMPLO = {"elements": [
    {"type": "node", "id": 1, "lat": 4.6, "lon": -74.0,
     "tags": {"amenity": "restaurant", "name": "Corrientazo Ejemplo", "phone": "+57 310 000 0000",
              "contact:instagram": "@corrientazo.ejemplo", "cuisine": "regional;colombian"}},
    {"type": "way", "id": 2, "center": {"lat": 4.61, "lon": -74.01},
     "tags": {"amenity": "cafe", "name": "Café Fijo", "phone": "601 2000000"}},
    {"type": "node", "id": 3, "lat": 4.62, "lon": -74.02,
     "tags": {"amenity": "fast_food", "name": "Sin Contacto"}},
    {"type": "node", "id": 4, "lat": 4.6, "lon": -74.0,
     "tags": {"amenity": "restaurant", "name": "Corrientazo Ejemplo", "phone": "3100000000"}},
    {"type": "node", "id": 5, "lat": 4.6, "lon": -74.0, "tags": {"amenity": "restaurant"}},
]}


def test_procesa_ordena_y_deduplica():
    filas = buscar.procesar(EJEMPLO)
    assert [f["nombre"] for f in filas] == ["Corrientazo Ejemplo", "Café Fijo", "Sin Contacto"]
    primero = filas[0]
    assert primero["whatsapp"] == "+573100000000"
    assert primero["instagram"] == "https://instagram.com/corrientazo.ejemplo"
    assert primero["cocina"] == "regional, colombian"
    assert filas[1]["whatsapp"] == ""          # un fijo no cuenta como WhatsApp
    assert filas[1]["lat"] == 4.61             # los "way" usan el centro


def test_consulta_incluye_las_categorias():
    q = buscar.consulta(4.65, -74.08, 2)
    for t in buscar.TIPOS:
        assert f'"amenity"="{t}"' in q
    assert "around:2000,4.65,-74.08" in q
