"""Offline tokenizer estimates should be useful without changing evidence."""
import base64
import io

from PIL import Image

from auto_eval import token_estimation as estimation
from auto_eval.judges.compare_protocols import resolve_compare_protocol


def test_calibrated_prompt_is_counted_as_tokens_instead_of_utf8_bytes():
    prompt = resolve_compare_protocol("qa_competitor_compare@0.2-simplified-calibrated").system_template.render(
        persona="终端用户", product_count=2, evidence_mode="video_frames")
    tokens = estimation.count_text_tokens(prompt)
    byte_count = len(prompt.encode("utf-8"))
    assert 0 < tokens < byte_count / 2
    estimate = estimation.estimate_input_tokens({"messages": [{"content": prompt}]})
    assert tokens < estimate < byte_count / 2


def test_tokenizer_counts_unicode_without_changing_request_text():
    text = "中文 ABC 123，🙂\n<json>{\"answer\": \"é é\"}</json>"
    tokenizer = estimation._tokenizer()
    encoded = tokenizer.encode(text, add_special_tokens=False)
    assert estimation.count_text_tokens(text) == len(encoded.ids)
    payload = {"messages": [{"content": text}]}
    estimation.estimate_input_tokens(payload)
    assert payload["messages"][0]["content"] == text


def test_image_header_estimation_avoids_decoding_full_data_url(monkeypatch):
    picture = io.BytesIO()
    Image.effect_noise((1600, 2400), 60).convert("RGB").save(picture, "JPEG", quality=90)
    raw = picture.getvalue()
    url = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
    original_decode = estimation.base64.b64decode
    decoded_sizes = []

    def decode(value):
        decoded_sizes.append(len(value))
        return original_decode(value)

    monkeypatch.setattr(estimation.base64, "b64decode", decode)
    assert estimation.image_dimensions(url) == (1600, 2400)
    assert sum(decoded_sizes) < len(raw) / 10
    payload = {"messages": [{"content": [{"type": "image_url", "image_url": {"url": url}}]}]}
    assert estimation.estimate_input_tokens(payload) == 264 + 50 * 75 + 256


def test_remote_or_broken_images_keep_conservative_budget():
    for url in ("https://example.invalid/image.png", "data:image/png;base64,broken"):
        assert estimation.estimate_input_tokens({"messages": [{"content": [
            {"type": "image_url", "image_url": {"url": url}},
        ]}]}) == 264 + 16_384


def test_missing_tokenizer_falls_back_to_old_safe_byte_estimate(monkeypatch):
    def missing():
        raise ImportError("test missing dependency")

    monkeypatch.setattr(estimation, "_tokenizer", missing)
    text = "仅用于缺失依赖回归的独立句子🔧"
    assert estimation.count_text_tokens(text) == len(text.encode("utf-8"))


def test_count_cache_is_bounded_and_does_not_retain_prompt_text():
    secret = "synthetic-secret-for-cache-test"
    estimation.count_text_tokens(secret)
    assert all(isinstance(key, bytes) and len(key) == 32 for key in estimation._text_counts)
    assert all(isinstance(value, int) for value in estimation._text_counts.values())
    assert len(estimation._text_counts) <= estimation._MAX_CACHED_TEXTS
