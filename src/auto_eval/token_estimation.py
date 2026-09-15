"""Offline Qwen text token counting and conservative image admission estimates.

This module never sends prompts anywhere or downloads a tokenizer at runtime.
The provider may use a different chat template or image resize policy, so local
counts keep a margin and are still calibrated against returned usage.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import logging
import math
import threading
from collections import OrderedDict
from functools import lru_cache
from importlib.resources import files

from PIL import Image


logger = logging.getLogger(__name__)
TOKENIZER_SHA256 = "5f9e4d4901a92b997e463c1f46055088b6cca5ca61a6522d1b9f64c4bb81cb42"
TEXT_MARGIN = 1.10
_load_lock = threading.Lock()
_cache_lock = threading.Lock()
_text_counts: OrderedDict[bytes, int] = OrderedDict()
_MAX_CACHED_TEXTS = 256
_fallback_reported = False


@lru_cache(maxsize=1)
def _tokenizer():
    # lru_cache permits simultaneous cache misses. Serialize initialization so
    # the first large batch does not create 128 copies of the BPE vocabulary.
    from tokenizers import Tokenizer

    data = gzip.decompress(files("auto_eval").joinpath(
        "tokenizer_data/qwen35-tokenizer.json.gz").read_bytes())
    if hashlib.sha256(data).hexdigest() != TOKENIZER_SHA256:
        raise ValueError("bundled Qwen tokenizer checksum mismatch")
    return Tokenizer.from_str(data.decode("utf-8"))


def count_text_tokens(text: str) -> int:
    """Count with the bundled vocabulary; cache counts, never the prompt text."""
    global _fallback_reported
    if not text:
        return 0
    encoded = text.encode("utf-8")
    digest = hashlib.sha256(encoded).digest()
    with _cache_lock:
        found = _text_counts.get(digest)
        if found is not None:
            _text_counts.move_to_end(digest)
            return found
    try:
        with _load_lock:
            tokenizer = _tokenizer()
        count = len(tokenizer.encode(text, add_special_tokens=False).ids)
    except (ImportError, OSError, ValueError) as error:
        # Partial deployments must not silently remove the rate limiter. This
        # fallback is the old byte upper bound; no text is included in the log.
        with _cache_lock:
            report = not _fallback_reported
            _fallback_reported = True
        if report:
            logger.warning("本地分词器不可用，使用保守字节估算：%s", type(error).__name__)
        return len(encoded)
    with _cache_lock:
        _text_counts[digest] = count
        _text_counts.move_to_end(digest)
        while len(_text_counts) > _MAX_CACHED_TEXTS:
            _text_counts.popitem(last=False)
    return count


def image_dimensions(url: str) -> tuple[int, int]:
    """Read only enough Base64 to open the image header, not all encoded pixels."""
    if not url.startswith("data:image/"):
        raise ValueError("remote image")
    start = url.find(",") + 1
    if start == 0 or ";base64" not in url[:start]:
        raise ValueError("unsupported image data URI")
    length = len(url) - start
    for size in (1024, 8192, 65536, length):
        prefix = url[start:start + min(size, length)]
        try:
            with Image.open(io.BytesIO(base64.b64decode(prefix))) as picture:
                return picture.size
        except (ValueError, OSError):
            if size >= length:
                raise
    raise ValueError("invalid image header")


def estimate_input_tokens(kwargs: dict) -> int:
    """Preserve image budget guardrails while avoiding text byte overestimates."""
    text_tokens = 0
    image_tokens = 0
    messages = kwargs.get("messages") or []
    for message in messages:
        content = message.get("content") or ""
        if isinstance(content, str):
            text_tokens += count_text_tokens(content)
        else:
            for part in content:
                if part.get("type") == "text":
                    text_tokens += count_text_tokens(part.get("text", ""))
                elif part.get("type") == "image_url":
                    url = (part.get("image_url") or {}).get("url", "")
                    try:
                        width, height = image_dimensions(url)
                        image_tokens += math.ceil(width / 32) * math.ceil(height / 32) + 256
                    except (ValueError, OSError):
                        image_tokens += 16_384
        # Tool/schema traffic is not currently used by the judge, but accounting
        # must remain conservative if a future caller supplies those fields.
        if message.get("tool_calls"):
            text_tokens += count_text_tokens(json.dumps(message["tool_calls"], ensure_ascii=False))
    for key in ("tools", "response_format"):
        if kwargs.get(key):
            text_tokens += count_text_tokens(json.dumps(kwargs[key], ensure_ascii=False))
    return 256 + 8 * len(messages) + math.ceil(text_tokens * TEXT_MARGIN) + image_tokens
