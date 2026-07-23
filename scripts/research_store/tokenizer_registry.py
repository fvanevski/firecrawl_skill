"""Tokenizer registry for deterministic, real token counting.

This module provides a versioned tokenizer registry that maps tokenizer
identifiers to concrete implementations.  The registry is used by the
hierarchical chunker to enforce hard token limits and compute stable
chunk identities.

## Design principles

* **Deterministic identity:** identical inputs and versions always produce
  the same token counts and chunk boundaries.
* **Configurable maximum:** the chunker receives a hard ``max_tokens``
  limit and never produces a chunk that exceeds it.
* **Fallback safe:** when ``tiktoken`` is unavailable the registry falls
  back to a simple byte-pair encoding (BPE) tokenizer so that the chunker
  can still operate in restricted environments.

## Built-in tokenizers

| Identifier          | Implementation     | Notes                          |
|---------------------|--------------------|--------------------------------|
| ``cl100k_base``     | ``tiktoken``       | GPT-4 / GPT-3.5 Turbo encoding |
| ``p500k_base``      | ``tiktoken``       | GPT-2 encoding                 |
| ``r50k_base``       | ``tiktoken``       | CodeGPT encoding               |
| ``bpe_fake``        | BPE fallback       | Deterministic byte-pair split  |

## Usage

.. code-block:: python

    from research_store.tokenizer_registry import get_tokenizer, count_tokens

    tokenizer = get_tokenizer("cl100k_base")
    count = count_tokens("Hello world", tokenizer)
    assert count == 2

.. versionadded:: P5-06
   Introduced as part of tokenizer-backed hierarchical chunking.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tokenizer protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tokenizer:
    """Immutable tokenizer descriptor.

    Attributes:
        name: Stable identifier (e.g. ``"cl100k_base"``).
        encode: Callable that maps a text string to a list of token IDs.
        decode: Callable that maps a list of token IDs back to text.
        version: Tokenizer implementation version for derivation tracking.
    """

    name: str
    encode: Callable[[str], list[int]]
    decode: Callable[[list[int]], str]
    version: str = "1"

    def count(self, text: str) -> int:
        """Return the number of tokens in *text*."""
        return len(self.encode(text))


# ---------------------------------------------------------------------------
# BPE fallback tokenizer (pure-Python, no external deps)
# ---------------------------------------------------------------------------


def _build_bpe_vocab() -> dict[str, int]:
    """Build a small deterministic BPE vocabulary from character bigrams."""
    vocab: dict[str, int] = {}
    # All single-byte tokens
    for i in range(256):
        vocab[chr(i)] = i
    # Common bigrams for more realistic splitting
    bigram_pairs: list[tuple[str, str]] = []
    chars = [chr(i) for i in range(256)]
    for a in chars[:128]:
        for b in chars[:128]:
            pair = a + b
            if pair not in vocab:
                vocab[pair] = len(vocab)
                bigram_pairs.append((a, b))
                if len(bigram_pairs) >= 1000:
                    break
        if len(bigram_pairs) >= 1000:
            break
    return vocab


class _BpeTokenizer:
    """Deterministic byte-pair encoding tokenizer (no external dependencies).

    Uses a greedy merge-pass approach over a fixed vocabulary.  The encoding
    is fully deterministic: identical inputs always produce identical outputs.
    """

    def __init__(self) -> None:
        self._vocab = _build_bpe_vocab()
        self._merge_rules: list[tuple[str, str]] = sorted(
            self._vocab.keys(), key=len, reverse=True
        )
        # Filter to actual mergeable bigrams (length >= 2)
        self._merge_rules = [m for m in self._merge_rules if len(m) >= 2]

    def encode(self, text: str) -> list[int]:
        """Encode *text* into a list of token IDs using BPE merges."""
        if not text:
            return []
        # Initial split into individual bytes (as character tokens)
        tokens: list[str] = list(text)
        changed = True
        while changed:
            changed = False
            for pair in self._merge_rules:
                merged = pair[0] + pair[1]
                i = 0
                new_tokens: list[str] = []
                while i < len(tokens):
                    remaining = "".join(tokens[i:])
                    pos = remaining.find(merged)
                    if pos == -1:
                        new_tokens.extend(tokens[i:])
                        break
                    new_tokens.extend(tokens[i : i + pos])
                    new_tokens.append(merged)
                    i = i + pos + len(merged)
                    changed = True
                tokens = new_tokens
        return [self._vocab.get(t, 0) for t in tokens]

    def decode(self, token_ids: list[int]) -> str:
        """Decode a list of token IDs back to text."""
        reverse_vocab = {v: k for k, v in self._vocab.items()}
        return "".join(reverse_vocab.get(tid, "") for tid in token_ids)


# ---------------------------------------------------------------------------
# tiktoken-backed tokenizer (preferred)
# ---------------------------------------------------------------------------


def _try_tiktoken(name: str) -> Tokenizer | None:
    """Try to create a tiktoken-backed tokenizer.

    Returns ``None`` when tiktoken is not installed or the encoding name
    is unknown.
    """
    try:
        import tiktoken

        enc = tiktoken.get_encoding(name)

        def encode_fn(text: str) -> list[int]:
            return enc.encode(text)

        def decode_fn(token_ids: list[int]) -> str:
            return enc.decode(token_ids)

        return Tokenizer(
            name=name,
            encode=encode_fn,
            decode=decode_fn,
            version=tiktoken.__version__
            if hasattr(tiktoken, "__version__")
            else "latest",
        )
    except Exception as exc:
        logger.debug("tiktoken fallback for %s: %s", name, exc)
        return None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenizerRecord:
    """Immutable record describing a registered tokenizer.

    Attributes:
        name: Stable identifier.
        tokenizer: The ``Tokenizer`` instance.
        is_fallback: ``True`` when this is the BPE fallback.
    """

    name: str
    tokenizer: Tokenizer
    is_fallback: bool = False


_DEFAULT_TOKENIZERS: dict[str, str] = {
    "cl100k_base": "GPT-4 / GPT-3.5 Turbo",
    "p500k_base": "GPT-2",
    "r50k_base": "CodeGPT",
    "o200k_base": "GPT-4o",
}


class TokenizerRegistry:
    """Versioned registry of tokenizer implementations.

    The registry tries ``tiktoken`` first for each built-in encoding name,
    then falls back to the deterministic BPE tokenizer when tiktoken is
    unavailable or the encoding is not recognized.

    Attributes:
        default_name: The tokenizer name used when no explicit name is given.
    """

    def __init__(self, default_name: str = "cl100k_base") -> None:
        self.default_name = default_name
        self._records: dict[str, TokenizerRecord] = {}
        self._register_builtins()

    def _register_builtins(self) -> None:
        """Register all built-in tokenizer names."""
        for name, description in _DEFAULT_TOKENIZERS.items():
            tk = _try_tiktoken(name)
            if tk is not None:
                self._records[name] = TokenizerRecord(name=name, tokenizer=tk)
            else:
                fallback = Tokenizer(
                    name=f"{name}_bpe",
                    encode=_BpeTokenizer().encode,
                    decode=_BpeTokenizer().decode,
                    version="1",
                )
                self._records[name] = TokenizerRecord(
                    name=name, tokenizer=fallback, is_fallback=True
                )
                logger.info(
                    "Using BPE fallback for tokenizer '%s' (%s)", name, description
                )

        # Also register the raw BPE fallback
        bpe = _BpeTokenizer()
        self._records["bpe_fake"] = TokenizerRecord(
            name="bpe_fake",
            tokenizer=Tokenizer(
                name="bpe_fake",
                encode=bpe.encode,
                decode=bpe.decode,
                version="1",
            ),
            is_fallback=True,
        )

    def get(self, name: str | None = None) -> Tokenizer:
        """Return the tokenizer for *name*, or the default.

        Args:
            name: Tokenizer identifier.  When ``None``, returns the default.

        Returns:
            A ``Tokenizer`` instance.

        Raises:
            KeyError: When *name* is not registered.
        """
        key = name or self.default_name
        record = self._records.get(key)
        if record is None:
            raise KeyError(
                f"unknown tokenizer '{key}'; registered: {list(self._records.keys())}"
            )
        return record.tokenizer

    def count(self, text: str, name: str | None = None) -> int:
        """Return the token count for *text* using the named tokenizer.

        Args:
            text: Text to count tokens for.
            name: Tokenizer identifier.  Defaults to the registry default.

        Returns:
            Number of tokens.
        """
        return self.get(name).count(text)

    @property
    def registered_names(self) -> list[str]:
        """Return all registered tokenizer names."""
        return list(self._records.keys())

    @property
    def fingerprint(self) -> str:
        """Return a fingerprint of the registry configuration.

        The fingerprint is a SHA-256 hex digest over the sorted set of
        registered tokenizer names and their versions.  It is used for
        derivation tracking so that tokenizer upgrades produce new
        derivations rather than mutating existing chunks.
        """
        parts: list[str] = []
        for name in sorted(self._records.keys()):
            tk = self._records[name].tokenizer
            parts.append(f"{name}={tk.version}")
        payload = "|".join(parts)
        return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Singleton instance
# ---------------------------------------------------------------------------


def get_registry(default_name: str = "cl100k_base") -> TokenizerRegistry:
    """Return the default ``TokenizerRegistry`` singleton.

    Args:
        default_name: Tokenizer name to use when no explicit name is given.

    Returns:
        A ``TokenizerRegistry`` instance.
    """
    return _get_default_registry()


# Module-level singleton — created on first import.
_DEFAULT_REGISTRY: TokenizerRegistry | None = None


def _get_default_registry() -> TokenizerRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = TokenizerRegistry()
    return _DEFAULT_REGISTRY


# Convenience functions that use the singleton.


def get_tokenizer(name: str | None = None) -> Tokenizer:
    """Return the tokenizer for *name* from the default registry."""
    return _get_default_registry().get(name)


def count_tokens(text: str, name: str | None = None) -> int:
    """Return the token count for *text* using the default registry."""
    return _get_default_registry().count(text, name)


def registry_fingerprint() -> str:
    """Return the fingerprint of the default registry."""
    return _get_default_registry().fingerprint
