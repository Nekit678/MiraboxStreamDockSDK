"""Codecs for converting extensible JSON payloads into plugin-owned objects."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from .errors import JsonCodecDecodeError, JsonCodecEncodeError, JsonCodecError
from .json_types import JsonObject, is_json_value

DecodedT = TypeVar("DecodedT")


class JsonCodec(Protocol[DecodedT]):
    """Bidirectional conversion between a JSON object and one typed value.

    Implement this protocol when action settings, global settings, or Property
    Inspector messages should be represented by a plugin-owned Python type.
    Codec implementations may raise :class:`TypeError`, :class:`ValueError`, or
    a :class:`JsonCodecError`; SDK helpers normalize those failures and add wire
    paths where possible.
    """

    def decode(self, value: JsonObject) -> DecodedT:
        """Convert an isolated JSON object into the plugin-owned value."""

        ...

    def encode(self, value: DecodedT) -> JsonObject:
        """Convert a plugin-owned value into a valid JSON object."""

        ...


@dataclass(frozen=True, slots=True)
class FunctionalJsonCodec(Generic[DecodedT]):
    """Adapt decoder and encoder callables to the :class:`JsonCodec` protocol.

    Attributes:
        decoder: Callable receiving a deep-copied JSON object and returning the
            typed representation.
        encoder: Callable receiving the typed representation and returning a
            JSON object. The result is validated and deep-copied.

    ``TypeError`` and ``ValueError`` raised by either callable are translated to
    the corresponding SDK codec error while existing :class:`JsonCodecError`
    instances are preserved.
    """

    decoder: Callable[[JsonObject], DecodedT]
    encoder: Callable[[DecodedT], JsonObject]

    def decode(self, value: JsonObject) -> DecodedT:
        """Decode an isolated JSON object with :attr:`decoder`.

        Raises:
            JsonCodecDecodeError: If the decoder rejects the input.
        """

        try:
            return self.decoder(_copy_json_object(value, decoding=True))
        except JsonCodecError:
            raise
        except (TypeError, ValueError) as exc:
            raise JsonCodecDecodeError(str(exc) or type(exc).__name__) from exc

    def encode(self, value: DecodedT) -> JsonObject:
        """Encode a value with :attr:`encoder` and validate its result.

        Raises:
            JsonCodecEncodeError: If the encoder fails or does not return a
                JSON-compatible object.
        """

        try:
            encoded = self.encoder(value)
        except JsonCodecError:
            raise
        except (TypeError, ValueError) as exc:
            raise JsonCodecEncodeError(str(exc) or type(exc).__name__) from exc
        return _copy_json_object(encoded, decoding=False)


@dataclass(frozen=True, slots=True)
class JsonObjectCodec:
    """Validate and isolate mutable JSON objects without changing their shape.

    Both directions return a deep copy, preventing plugin code, command models,
    and parsed wire messages from accidentally sharing nested mutable values.
    """

    def decode(self, value: JsonObject) -> JsonObject:
        """Validate ``value`` and return a deep copy.

        Raises:
            JsonCodecDecodeError: If ``value`` is not a finite JSON object.
        """

        return _copy_json_object(value, decoding=True)

    def encode(self, value: JsonObject) -> JsonObject:
        """Validate ``value`` and return a deep copy.

        Raises:
            JsonCodecEncodeError: If ``value`` is not a finite JSON object.
        """

        return _copy_json_object(value, decoding=False)


def decode_with_codec(value: JsonObject, codec: JsonCodec[DecodedT]) -> DecodedT:
    """Validate and isolate wire data before handing it to a plugin codec.

    Args:
        value: JSON object received from Stream Dock.
        codec: Decoder for the desired plugin-owned type.

    Returns:
        Value returned by ``codec.decode``.

    Raises:
        JsonCodecDecodeError: If the input is invalid, or if the codec raises
            :class:`TypeError` or :class:`ValueError`.
        JsonCodecError: Any more specific codec error raised by the codec.
    """

    try:
        return codec.decode(_copy_json_object(value, decoding=True))
    except JsonCodecError:
        raise
    except (TypeError, ValueError) as exc:
        raise JsonCodecDecodeError(str(exc) or type(exc).__name__) from exc


def encode_with_codec(value: DecodedT, codec: JsonCodec[DecodedT]) -> JsonObject:
    """Encode a typed value and validate the resulting JSON object.

    Args:
        value: Plugin-owned value to encode.
        codec: Encoder associated with the value's type.

    Returns:
        A deep-copied, finite JSON object suitable for a wire command.

    Raises:
        JsonCodecEncodeError: If encoding fails or the result is not a JSON
            object.
        JsonCodecError: Any more specific codec error raised by the codec.
    """

    try:
        encoded = codec.encode(value)
    except JsonCodecError:
        raise
    except (TypeError, ValueError) as exc:
        raise JsonCodecEncodeError(str(exc) or type(exc).__name__) from exc
    return _copy_json_object(encoded, decoding=False)


def _copy_json_object(value: object, *, decoding: bool) -> JsonObject:
    error_type = JsonCodecDecodeError if decoding else JsonCodecEncodeError
    if not isinstance(value, dict) or not is_json_value(value):
        raise error_type("expected a JSON object")
    return deepcopy(value)


JSON_OBJECT_CODEC = JsonObjectCodec()
