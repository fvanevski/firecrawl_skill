"""Deterministic parser registry with MIME-type routing.

The registry maps MIME type prefixes and exact types to ``Parser``
implementations.  Selection is deterministic: the same MIME type always
yields the same parser (first match wins).

## Selection algorithm

1. **Exact match** — ``registry.select("text/markdown")`` returns the
   parser registered for that exact MIME type.
2. **Prefix match** — ``registry.select("text/html")`` matches
   ``"text/"`` prefix registrations.
3. **Content sniffing** — When ``mime_type`` is ``None``, the registry
   inspects the first 512 bytes and falls back to registered sniffers.
4. **Explicit fallback** — A ``"application/octet-stream"`` or
   ``"*/*"`` registration acts as the catch-all.

## Recording

Every ``select()`` call records the decision in the returned
``SelectionRecord`` so that downstream code (``CorpusService``, CLI)
can log or audit which parser was chosen and why.

## Supported registrations (built-in)

| MIME type / prefix        | Parser class              |
|---------------------------|---------------------------|
| ``text/markdown``         | ``MarkdownParser``        |
| ``text/html``             | ``HtmlNormalizedParser``  |
| ``application/json``      | ``JsonParser``            |
| ``text/plain``            | ``PlainTextParser``       |
| ``application/pdf``       | Extension point (stub)    |
| ``text/x-code``           | Extension point (stub)    |
| ``application/x-legal``   | Extension point (stub)    |

.. versionchanged:: P5-04
   Introduced as part of Phase 5 canonical parser interfaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .interfaces import Parser, ParserSelectionError, ParseResult


@dataclass(frozen=True)
class SelectionRecord:
    """Immutable record of a parser selection decision.

    Attributes:
        requested_mime_type: The MIME type that was requested.
        selected_parser_type: Fully-qualified class name of the selected parser.
        selection_method: How the parser was chosen (``"exact"``, ``"prefix"``,
            ``"sniff"``, ``"fallback"``, ``"none"``).
        parser_version: The parser's version string.
    """

    requested_mime_type: str | None
    selected_parser_type: str
    selection_method: str
    parser_version: str


class ParserRegistry:
    """MIME-type-driven parser registry.

    The registry is populated at import time with the built-in adapters.
    Additional parsers can be registered via ``register()``.

    Attributes:
        parsers: Mapping from MIME type (or prefix) to parser class.
        sniffers: Mapping from MIME type prefix to a callable that
            inspects raw bytes and returns ``True`` when the content
            matches.
    """

    def __init__(self) -> None:
        self._parsers: dict[str, type[Parser]] = {}
        self._sniffers: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Registration API
    # ------------------------------------------------------------------

    def register(
        self,
        parser: type[Parser],
        *,
        mime_types: list[str],
        sniffer: Any | None = None,
    ) -> None:
        """Register a parser class for one or more MIME types.

        Args:
            parser: A concrete ``Parser`` subclass.
            mime_types: Exact MIME types or prefix patterns
                (e.g. ``"text/"``).
            sniffer: Optional callable ``(raw: bytes) -> bool`` that
                inspects raw bytes.  Registered under the first MIME type
                in *mime_types*.
        """
        for mime_type in mime_types:
            self._parsers[mime_type] = parser
        if sniffer is not None and mime_types:
            self._sniffers[mime_types[0]] = sniffer

    def unregister(self, mime_type: str) -> None:
        """Remove a parser registration. No-op when the key is absent."""
        self._parsers.pop(mime_type, None)

    def get_parser(self, mime_type: str) -> Parser | None:
        """Return a parser instance for *mime_type*, or ``None``."""
        cls = self._parsers.get(mime_type)
        if cls is not None:
            return cls()
        return None

    def list_registered(self) -> list[dict[str, str]]:
        """Return a list of ``{mime_type, parser, version}`` dicts."""
        result: list[dict[str, str]] = []
        for mime_type, cls in sorted(self._parsers.items()):
            instance = cls()
            result.append(
                {
                    "mime_type": mime_type,
                    "parser": f"{cls.__module__}.{cls.__name__}",
                    "version": instance.parser_version,
                }
            )
        return result

    # ------------------------------------------------------------------
    # Selection API
    # ------------------------------------------------------------------

    def select(
        self,
        mime_type: str | None,
        raw: bytes | None = None,
    ) -> SelectionRecord:
        """Deterministically select a parser for *mime_type*.

        Selection order:
        1. Exact MIME type match.
        2. Prefix match (``"text/"`` → all ``text/*``).
        3. Content sniffing when *mime_type* is ``None`` and *raw* is
           provided.
        4. Fallback to ``"application/octet-stream"`` registration.

        Args:
            mime_type: MIME type string or ``None``.
            raw: Raw bytes for content sniffing when *mime_type* is
                ``None``.

        Returns:
            A ``SelectionRecord`` documenting the decision.

        Raises:
            ParserSelectionError: When no parser can be selected.
        """
        # 1. Exact match
        if mime_type is not None and mime_type in self._parsers:
            cls = self._parsers[mime_type]
            instance = cls()
            return SelectionRecord(
                requested_mime_type=mime_type,
                selected_parser_type=f"{cls.__module__}.{cls.__name__}",
                selection_method="exact",
                parser_version=instance.parser_version,
            )

        # 2. Prefix match
        if mime_type is not None:
            for prefix, cls in sorted(self._parsers.items()):
                if mime_type.startswith(prefix + "/") or mime_type.startswith(
                    prefix + ";"
                ):
                    instance = cls()
                    return SelectionRecord(
                        requested_mime_type=mime_type,
                        selected_parser_type=f"{cls.__module__}.{cls.__name__}",
                        selection_method="prefix",
                        parser_version=instance.parser_version,
                    )

        # 3. Content sniffing
        if mime_type is None and raw is not None:
            for prefix, sniffer in sorted(self._sniffers.items()):
                try:
                    if sniffer(raw[:512]):
                        cls = self._parsers[prefix]
                        instance = cls()
                        return SelectionRecord(
                            requested_mime_type=None,
                            selected_parser_type=f"{cls.__module__}.{cls.__name__}",
                            selection_method="sniff",
                            parser_version=instance.parser_version,
                        )
                except Exception:
                    continue

        # 4. Fallback
        fallback = self._parsers.get("application/octet-stream")
        if fallback is not None:
            instance = fallback()
            return SelectionRecord(
                requested_mime_type=mime_type,
                selected_parser_type=f"{fallback.__module__}.{fallback.__name__}",
                selection_method="fallback",
                parser_version=instance.parser_version,
            )

        # 5. Nothing found
        available = sorted(self._parsers.keys())
        raise ParserSelectionError(mime_type, available)


# ---------------------------------------------------------------------------
# Default registry (built-in parsers)
# ---------------------------------------------------------------------------


def build_default_registry() -> ParserRegistry:
    """Build and return a ``ParserRegistry`` pre-populated with built-in adapters.

    Returns:
        A fully configured ``ParserRegistry`` instance.
    """
    from .extensions import (
        PdfParser,
        CodeParser,
        LegalDocumentParser,
    )
    from .html_parser import HtmlNormalizedParser
    from .json_parser import JsonParser
    from .markdown_parser import MarkdownParser
    from .text_parser import PlainTextParser

    registry = ParserRegistry()

    # Markdown
    registry.register(
        MarkdownParser,
        mime_types=["text/markdown", "text/x-markdown"],
    )

    # HTML
    registry.register(
        HtmlNormalizedParser,
        mime_types=["text/html", "application/xhtml+xml"],
    )

    # JSON
    registry.register(
        JsonParser,
        mime_types=["application/json", "application/json; charset=utf-8"],
    )

    # Plain text
    registry.register(
        PlainTextParser,
        mime_types=["text/plain", "text/x-plain"],
    )

    # Extension points
    registry.register(
        PdfParser,
        mime_types=["application/pdf"],
    )
    registry.register(
        CodeParser,
        mime_types=["text/x-code", "text/x-script", "text/x-source"],
    )
    registry.register(
        LegalDocumentParser,
        mime_types=["application/x-legal", "text/x-legal"],
    )

    return registry


# Singleton — shared across the application
_DEFAULT_REGISTRY: ParserRegistry | None = None


def get_registry() -> ParserRegistry:
    """Return the default (singleton) parser registry.

    Lazily initializes on first call.  Callers that need an isolated
    registry should instantiate ``ParserRegistry`` directly.
    """
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = build_default_registry()
    return _DEFAULT_REGISTRY


def parse(
    raw: bytes,
    *,
    mime_type: str | None = None,
    registry: ParserRegistry | None = None,
) -> ParseResult:
    """Convenience function: select a parser and parse *raw* bytes.

    Args:
        raw: Raw byte payload.
        mime_type: MIME type hint.
        registry: Optional explicit registry.  Uses the default singleton
            when ``None``.

    Returns:
        A ``ParseResult`` from the selected parser.

    Raises:
        ParserSelectionError: When no parser can be selected.
        UnsupportedFormatError: When the selected parser rejects the content.
    """
    if registry is None:
        registry = get_registry()
    record = registry.select(mime_type, raw=raw)
    parser = registry.get_parser(
        mime_type or record.selected_parser_type.split(".")[-1].lower()
    )
    if parser is None:
        # Fallback: instantiate from the selected parser type name
        from importlib import import_module

        module_name, class_name = record.selected_parser_type.rsplit(".", 1)
        mod = import_module(module_name)
        parser = getattr(mod, class_name)()
    return parser.parse(raw, mime_type=mime_type)
