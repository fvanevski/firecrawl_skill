"""Extension point stubs for PDF, code, and legal-document parsers.

These classes define the interface for future parser implementations.
They raise ``UnsupportedFormatError`` by default, signaling that the
format is not yet supported.  Subclasses should override ``parse()``
with a concrete implementation.

## Extension points

| Class              | MIME type(s)          | Status     |
|--------------------|-----------------------|------------|
| ``PdfParser``      | ``application/pdf``   | Stub       |
| ``CodeParser``     | ``text/x-code`` etc.  | Stub       |
| ``LegalDocumentParser`` | ``application/x-legal`` | Stub |

## Usage

To implement a new parser, subclass ``Parser`` and register it with the
registry:

.. code-block:: python

    from research_store.parsing import ParserRegistry, Parser
    from research_store.parsing.interfaces import ParseResult

    class MyParser(Parser):
        parser_version = "my-parser-v1"

        def parse(self, raw, *, mime_type=None, source_length=None):
            # ... implementation ...
            return ParseResult(...)

    registry = ParserRegistry()
    registry.register(MyParser, mime_types=["application/x-my-format"])

.. versionchanged:: P5-04
   Introduced as part of Phase 5 canonical parser interfaces.
"""

from __future__ import annotations


from .interfaces import ParseResult, Parser, UnsupportedFormatError


class PdfParser(Parser):
    """PDF extraction parser — extension point.

    Raises ``UnsupportedFormatError`` by default.  Override ``parse()``
    in a subclass to implement actual PDF extraction (e.g. via ``pymupdf``,
    ``pdfplumber``, or ``pdfminer``).

    Attributes:
        parser_version: Fixed version string ``"pdf-v1"``.
    """

    parser_version = "pdf-v1"

    def parse(
        self,
        raw: bytes,
        *,
        mime_type: str | None = None,
        source_length: int | None = None,
    ) -> ParseResult:
        """PDF parsing is not yet implemented.

        Raises:
            UnsupportedFormatError: Always — PDF extraction is an
                extension point not yet implemented.
        """
        raise UnsupportedFormatError(
            mime_type="application/pdf",
            suggestion=(
                "PDF extraction is an extension point. "
                "Implement PdfParser.parse() using a PDF library "
                "(pymupdf, pdfplumber, pdfminer) and register with "
                "ParserRegistry."
            ),
        )


class CodeParser(Parser):
    """Source-code parser — extension point.

    Raises ``UnsupportedFormatError`` by default.  Override ``parse()``
    to implement language-aware code parsing (syntax highlighting,
    token boundaries, language detection).

    Attributes:
        parser_version: Fixed version string ``"code-v1"``.
    """

    parser_version = "code-v1"

    def parse(
        self,
        raw: bytes,
        *,
        mime_type: str | None = None,
        source_length: int | None = None,
    ) -> ParseResult:
        """Code parsing is not yet implemented.

        Raises:
            UnsupportedFormatError: Always — code-aware parsing is an
                extension point not yet implemented.
        """
        raise UnsupportedFormatError(
            mime_type=mime_type or "text/x-code",
            suggestion=(
                "Code-aware parsing is an extension point. "
                "Implement CodeParser.parse() with language detection "
                "(e.g. pygments, tree-sitter) and register with "
                "ParserRegistry."
            ),
        )


class LegalDocumentParser(Parser):
    """Legal-document parser — extension point.

    Raises ``UnsupportedFormatError`` by default.  Override ``parse()``
    to implement legal-document semantics (section numbering, hierarchical
    outlines, cross-references).

    Attributes:
        parser_version: Fixed version string ``"legal-v1"``.
    """

    parser_version = "legal-v1"

    def parse(
        self,
        raw: bytes,
        *,
        mime_type: str | None = None,
        source_length: int | None = None,
    ) -> ParseResult:
        """Legal-document parsing is not yet implemented.

        Raises:
            UnsupportedFormatError: Always — legal-document semantics
                are an extension point not yet implemented.
        """
        raise UnsupportedFormatError(
            mime_type=mime_type or "application/x-legal",
            suggestion=(
                "Legal-document parsing is an extension point. "
                "Implement LegalDocumentParser.parse() with section "
                "numbering, hierarchical outline, and cross-reference "
                "support, then register with ParserRegistry."
            ),
        )
