"""split_events debe separar TODO lo que Meta puede agrupar sin hacer
desaparecer los tipos de evento que no se separan (status, history,
smb_app_state_sync). Esa asimetría es la parte delicada: el splitter que
usa la rama feat/testing-flow solo mira `messages` y devolvería [] para un
echo, lo que dejaría sin efecto todo el handoff bot<->humano."""

from app.services.messaging.webhook_payload import event_id, split_events


def _envelope(value: dict, field: str = "messages") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA1", "changes": [{"field": field, "value": value}]}],
    }


def _value_with_messages(*ids: str) -> dict:
    return {
        "metadata": {"display_phone_number": "573000000000", "phone_number_id": "1234567890"},
        "contacts": [{"wa_id": f"57300{i}", "profile": {"name": f"Cliente {i}"}} for i in range(len(ids))],
        "messages": [
            {"id": wamid, "from": f"57300{i}", "type": "text", "text": {"body": f"msg {i}"}}
            for i, wamid in enumerate(ids)
        ],
    }


def test_single_message_returns_one_payload():
    events = split_events(_envelope(_value_with_messages("wamid.A")))

    assert len(events) == 1
    assert event_id(events[0]) == "wamid.A"


def test_batched_messages_are_split_one_per_payload():
    events = split_events(_envelope(_value_with_messages("wamid.A", "wamid.B", "wamid.C")))

    assert [event_id(e) for e in events] == ["wamid.A", "wamid.B", "wamid.C"]
    for e in events:
        value = e["entry"][0]["changes"][0]["value"]
        assert len(value["messages"]) == 1


def test_split_keeps_each_message_with_its_own_contact():
    events = split_events(_envelope(_value_with_messages("wamid.A", "wamid.B")))

    for i, e in enumerate(events):
        value = e["entry"][0]["changes"][0]["value"]
        assert value["contacts"] == [{"wa_id": f"57300{i}", "profile": {"name": f"Cliente {i}"}}]


def test_split_preserves_field_and_metadata():
    events = split_events(_envelope(_value_with_messages("wamid.A")))

    change = events[0]["entry"][0]["changes"][0]
    assert change["field"] == "messages"
    assert change["value"]["metadata"]["phone_number_id"] == "1234567890"


def test_echoes_are_split_too_so_handoff_never_loses_one():
    value = {
        "metadata": {"display_phone_number": "573000000000", "phone_number_id": "1234567890"},
        "message_echoes": [
            {"id": "wamid.E1", "from": "573000000000", "to": "573001234567", "type": "text"},
            {"id": "wamid.E2", "from": "573000000000", "to": "573001234567", "type": "text"},
        ],
    }

    events = split_events(_envelope(value, field="smb_message_echoes"))

    assert [event_id(e) for e in events] == ["wamid.E1", "wamid.E2"]
    for e in events:
        assert e["entry"][0]["changes"][0]["field"] == "smb_message_echoes"
        assert len(e["entry"][0]["changes"][0]["value"]["message_echoes"]) == 1


def test_status_payload_passes_through_whole():
    """_handle_status_update ya recorre todos los statuses: separarlos
    duplicaría el trabajo, y devolver [] los perdería."""
    value = {
        "metadata": {"display_phone_number": "573000000000", "phone_number_id": "1234567890"},
        "statuses": [{"id": "wamid.S1", "status": "delivered"}, {"id": "wamid.S2", "status": "read"}],
    }
    body = _envelope(value, field="messages")

    assert split_events(body) == [body]


def test_history_and_app_state_sync_pass_through_whole():
    for field in ("history", "smb_app_state_sync"):
        body = _envelope({"metadata": {"phone_number_id": "1234567890"}, "threads": []}, field=field)
        assert split_events(body) == [body]


def test_malformed_body_passes_through_instead_of_disappearing():
    for body in ({}, {"entry": "no-es-lista"}, {"entry": [None]}, {"entry": [{"changes": None}]}):
        assert split_events(body) == [body]


def test_several_entries_and_changes_are_all_collected():
    body = {
        "object": "whatsapp_business_account",
        "entry": [
            {"id": "W1", "changes": [{"field": "messages", "value": _value_with_messages("wamid.A")}]},
            {"id": "W2", "changes": [{"field": "messages", "value": _value_with_messages("wamid.B", "wamid.C")}]},
        ],
    }

    assert [event_id(e) for e in split_events(body)] == ["wamid.A", "wamid.B", "wamid.C"]


def test_event_id_is_none_when_there_is_no_wamid():
    assert event_id(_envelope({"statuses": []})) is None
    assert event_id({}) is None
