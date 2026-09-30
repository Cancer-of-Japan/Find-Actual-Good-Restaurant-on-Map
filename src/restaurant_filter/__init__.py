"""Google Maps Restaurant Trust Filter.

A personal tool that collects Google Maps restaurant data over repeated runs,
detects rating-manipulation patterns, and renders a static HTML dashboard.
"""

__version__ = "0.1.0"

# Use the OS certificate store for TLS verification so corporate proxies that
# intercept HTTPS with their own root CA are trusted (same store pip uses).
# No-op if truststore isn't installed.
try:  # pragma: no cover - environment dependent
    import truststore

    truststore.inject_into_ssl()
except Exception:  # noqa: BLE001
    pass
