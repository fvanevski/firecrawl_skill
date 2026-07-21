from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING = {"fbclid", "gclid", "mc_cid", "mc_eid"}
SENSITIVE_PARAMS = {
    "api_key",
    "apikey",
    "token",
    "access_token",
    "auth",
    "secret",
    "signature",
    "sig",
    "password",
    "pass",
    "session_id",
    "sessionid",
    "jwt",
    "bearer",
    "credential",
    "private_key",
}


def is_sensitive_param(name: str) -> bool:
    name_lower = name.lower()
    if name_lower in SENSITIVE_PARAMS:
        return True
    if name_lower.endswith("_key") or name_lower.endswith("key"):
        return True
    if name_lower.endswith("_token") or name_lower.endswith("token"):
        return True
    if name_lower.endswith("_secret") or name_lower.endswith("secret"):
        return True
    return False


def redact_sensitive_url(value: str) -> str:
    """Redact sensitive query parameter values in a URL, replacing them with [REDACTED]."""
    parts = urlsplit(value.strip())
    if not parts.scheme or not parts.netloc:
        return value.strip()

    query_tuples = parse_qsl(parts.query, keep_blank_values=True)
    if not query_tuples:
        return value.strip()

    redacted_query = []
    for k, v in query_tuples:
        if is_sensitive_param(k):
            redacted_query.append((k, "[REDACTED]"))
        else:
            redacted_query.append((k, v))

    query_str = urlencode(redacted_query, safe="[]")

    return urlunsplit((parts.scheme, parts.netloc, parts.path, query_str, parts.fragment))


def canonicalize_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("source URL must be absolute HTTP(S)")
    host = parts.hostname.encode("idna").decode("ascii").lower()
    port = parts.port
    if port and not (
        (parts.scheme.lower() == "http" and port == 80)
        or (parts.scheme.lower() == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in TRACKING
        )
    )
    return urlunsplit((parts.scheme.lower(), host, path, query, ""))


def canonicalize_candidate_url(value: str) -> tuple[str, str]:
    """Canonicalize a search candidate URL and return (canonical_url, redacted_original_url)."""
    cleaned = value.strip()
    redacted_orig = redact_sensitive_url(cleaned)

    parts = urlsplit(cleaned)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("candidate URL must be absolute HTTP(S)")

    host = parts.hostname.encode("idna").decode("ascii").lower()
    port = parts.port
    if port and not (
        (parts.scheme.lower() == "http" and port == 80)
        or (parts.scheme.lower() == "https" and port == 443)
    ):
        host = f"{host}:{port}"

    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")

    filtered_params = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        k_lower = k.lower()
        if k_lower.startswith("utm_") or k_lower in TRACKING:
            continue
        if is_sensitive_param(k):
            continue
        filtered_params.append((k, v))

    query = urlencode(sorted(filtered_params))
    canonical = urlunsplit((parts.scheme.lower(), host, path, query, ""))

    return (canonical, redacted_orig)
