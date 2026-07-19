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
    """Bidirectional conversion between a JSON object and one typed value."""

    def decode(self, value: JsonObject) -> DecodedT: ...

    def encode(self, value: DecodedT) -> JsonObject: ...


@dataclass(frozen=True, slots=True)
class FunctionalJsonCodec(Generic[DecodedT]):
    """Build a validated codec from a decoder and an encoder function."""

    decoder: Callable[[JsonObject], DecodedT]
    encoder: Callable[[DecodedT], JsonObject]

    def decode(self, value: JsonObject) -> DecodedT:
        try:
            return self.decoder(_copy_json_object(value, decoding=True))
        except JsonCodecError:
            raise
        except (TypeError, ValueError) as exc:
            raise JsonCodecDecodeError(str(exc) or type(exc).__name__) from exc

    def encode(self, value: DecodedT) -> JsonObject:
        try:
            encoded = self.encoder(value)
        except JsonCodecError:
            raise
        except (TypeError, ValueError) as exc:
            raise JsonCodecEncodeError(str(exc) or type(exc).__name__) from exc
        return _copy_json_object(encoded, decoding=False)


@dataclass(frozen=True, slots=True)
class JsonObjectCodec:
    """Identity codec that validates and isolates a mutable JSON object."""

    def decode(self, value: JsonObject) -> JsonObject:
        return _copy_json_object(value, decoding=True)

    def encode(self, value: JsonObject) -> JsonObject:
        return _copy_json_object(value, decoding=False)


def decode_with_codec(value: JsonObject, codec: JsonCodec[DecodedT]) -> DecodedT:
    """Validate and isolate wire data before handing it to a plugin codec."""

    try:
        return codec.decode(_copy_json_object(value, decoding=True))
    except JsonCodecError:
        raise
    except (TypeError, ValueError) as exc:
        raise JsonCodecDecodeError(str(exc) or type(exc).__name__) from exc


def encode_with_codec(value: DecodedT, codec: JsonCodec[DecodedT]) -> JsonObject:
    """Validate and isolate the JSON object produced by a plugin codec."""

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
