import hashlib
import hmac

from app.services.whatsapp_client.signature import verify_meta_signature

SECRET = "test-app-secret"
BODY = b'{"object":"whatsapp_business_account","entry":[]}'


def _sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_valid_signature_passes():
    header = _sign(BODY, SECRET)
    assert verify_meta_signature(BODY, header, SECRET) is True


def test_wrong_secret_fails():
    header = _sign(BODY, "otro-secret")
    assert verify_meta_signature(BODY, header, SECRET) is False


def test_tampered_body_fails():
    header = _sign(BODY, SECRET)
    tampered = BODY + b" "
    assert verify_meta_signature(tampered, header, SECRET) is False


def test_missing_header_fails():
    assert verify_meta_signature(BODY, None, SECRET) is False


def test_malformed_header_fails():
    assert verify_meta_signature(BODY, "not-sha256-prefixed", SECRET) is False
