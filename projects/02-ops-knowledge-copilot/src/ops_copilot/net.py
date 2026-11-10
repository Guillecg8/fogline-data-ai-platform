"""Network setup shared by the command-line entry points."""

import truststore


def use_system_certificates() -> None:
    """Verify HTTPS with the operating system's certificate store.

    Python ships its own list of trusted certificates. On networks that inspect HTTPS traffic
    (corporate proxies, some antivirus tools) that list is not enough and downloads fail with
    CERTIFICATE_VERIFY_FAILED. The operating system store does include the right certificate.
    """
    truststore.inject_into_ssl()
