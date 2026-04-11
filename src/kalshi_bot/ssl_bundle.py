"""Point OpenSSL/urllib3 at certifi's CA bundle (fixes macOS python.org SSL errors)."""

from __future__ import annotations

import os


def apply_certifi_ca_bundle() -> None:
    """Set SSL_CERT_FILE / REQUESTS_CA_BUNDLE if unset — helps [SSL: CERTIFICATE_VERIFY_FAILED] on macOS."""
    try:
        import certifi

        path = certifi.where()
        os.environ.setdefault("SSL_CERT_FILE", path)
        os.environ.setdefault("REQUESTS_CA_BUNDLE", path)
    except ImportError:
        pass
