"""Sin re-exports a propósito.

Este paquete contiene tanto código pesado (`outbound_message_service`, que
arrastra fireo y Firestore) como código puro (`webhook_payload`, que el
webhook liviano importa para no cargar nada de eso). Un `from
.outbound_message_service import ...` acá haría que importar CUALQUIER
submódulo cargue Firestore, y el ingress dejaría de ser liviano -- que es
justamente la razón de separarlo. Importar siempre por ruta completa.
"""
