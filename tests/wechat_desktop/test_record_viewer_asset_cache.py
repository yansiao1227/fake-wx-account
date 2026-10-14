"""记录查看器按已验证图片身份复用原像素证据，仍执行关闭与恢复。"""

from hashlib import sha256

import pytest

from .test_record_image_viewer import _capture_client


ASSET_A = "PreviewWindow_0123456789abcdef0123456789abcdef_0_0"
ASSET_B = "PreviewWindow_fedcba9876543210fedcba9876543210_0_0"


def _cached_capture_client(monkeypatch):
    client, state, require, image_class = _capture_client(monkeypatch)
    state.aid = ASSET_A
    state.pid = 42
    state.closed_identities = []
    state.on_close = None
    state.on_restore = None
    state.close_succeeds = True
    state.restore_succeeds = True

    monkeypatch.setattr(client, "_is_image_viewer_window", lambda hwnd, main: hwnd == 200)
    monkeypatch.setattr(client, "_image_viewer_is_open", lambda hwnd: hwnd == 200 and state.clicked)
    monkeypatch.setattr(client._image_viewer, "_native_image_viewer_identity", lambda hwnd, main:
        (200, state.pid, "图片和视频", "Qt51514QWindowIcon") if hwnd == 200 else None)
    monkeypatch.setattr(client._image_viewer, "_image_viewer_uia_identity",
                        lambda hwnd: ("mmui::XView", state.aid))

    def copy_original(*args, **kwargs):
        state.calls.append("copy_original")
        kwargs["validate_context"]()
        return image_class()

    def close(*args, **kwargs):
        state.calls.append("close_viewer")
        state.closed_identities.append(kwargs["expected_uia_identity"])
        if state.on_close:
            state.on_close()
        if state.close_succeeds:
            state.clicked = False
        return state.close_succeeds

    def restore(hwnd, validate):
        validate()
        state.calls.append(("restore", hwnd))
        if state.on_restore:
            state.on_restore()
        if state.restore_succeeds:
            state.foreground = hwnd
        return state.restore_succeeds

    monkeypatch.setattr(client._attachment_reader, "_copy_verified_viewer_image", copy_original)
    monkeypatch.setattr(client, "_close_image_viewer", close)
    monkeypatch.setattr(client._attachment_reader, "_restore_verified_window", restore)
    return client, state, require, image_class


def _capture(client, require, target, cache, metadata=None):
    return client._capture_image_viewer_from_point(
        (40, 50), target, validate_before_click=require, validate_context=require,
        return_hwnd=300, validate_return_window=require, prefer_original=True,
        capture_metadata=metadata, image_asset_cache=cache,
    )


def _assert_asset_metadata(metadata, aid, *, hit):
    assert metadata["asset_key_hash"] == sha256(aid.encode("utf-8")).hexdigest()[:12]
    assert metadata["asset_cache_hit"] is hit
    assert aid not in str(metadata)


@pytest.mark.parametrize("aid", [ASSET_A, "PreviewWindow_0123456789ABCDEF0123456789ABCDEF_0_0"])
def test_same_asset_reuses_proven_path_but_still_opens_closes_and_restores(monkeypatch, tmp_path, aid):
    client, state, require, _ = _cached_capture_client(monkeypatch)
    state.aid = aid
    cache, first_metadata, second_metadata = {}, {}, {}
    first_path, second_path = tmp_path / "first.png", tmp_path / "duplicate.png"

    assert _capture(client, require, first_path, cache, first_metadata) == str(first_path)
    assert len(cache) == 1
    assert first_path.read_bytes() == b"original pixels"
    _assert_asset_metadata(first_metadata, aid, hit=False)
    state.calls.clear()

    assert _capture(client, require, second_path, cache, second_metadata) == str(first_path)
    assert len(cache) == 1 and not second_path.exists()
    assert state.calls == ["open_viewer", "close_viewer", ("restore", 300)]
    assert state.closed_identities == [("mmui::XView", aid), ("mmui::XView", aid)]
    _assert_asset_metadata(second_metadata, aid, hit=True)
    for key in ("capture_kind", "width", "height"):
        assert second_metadata[key] == first_metadata[key]


@pytest.mark.parametrize("changed_identity", ["aid", "suffix", "pid"])
def test_different_asset_or_process_captures_new_evidence(monkeypatch, tmp_path, changed_identity):
    client, state, require, _ = _cached_capture_client(monkeypatch)
    cache = {}
    first_path, second_path = tmp_path / "first.png", tmp_path / "second.png"
    assert _capture(client, require, first_path, cache) == str(first_path)
    state.calls.clear()
    if changed_identity == "aid":
        state.aid = ASSET_B
    elif changed_identity == "suffix":
        state.aid = ASSET_A[:-1] + "1"
    else:
        state.pid = 43
    metadata = {}

    assert _capture(client, require, second_path, cache, metadata) == str(second_path)
    assert len(cache) == 2
    assert state.calls == ["open_viewer", "copy_original", "save_original", "close_viewer", ("restore", 300)]
    _assert_asset_metadata(metadata, state.aid, hit=False)


@pytest.mark.parametrize("aid", [
    "PreviewWindow_test_0_0",
    "PreviewWindow_0123456789abcdef0123456789abcdeg_0_0",
    "PreviewWindow_0123456789abcdef0123456789abcde_0_0",
    ASSET_A + "_suffix",
])
def test_noncanonical_or_nonhex_asset_identity_is_never_cached(monkeypatch, tmp_path, aid):
    client, state, require, _ = _cached_capture_client(monkeypatch)
    state.aid = aid
    cache = {}
    for number in range(2):
        metadata = {}
        target = tmp_path / f"uncached-{number}.png"
        assert _capture(client, require, target, cache, metadata) == str(target)
        assert "asset_key_hash" not in metadata
        assert "asset_cache_hit" not in metadata
    assert not cache
    assert state.calls.count("copy_original") == 2


@pytest.mark.parametrize("damage", ["missing", "tampered"])
def test_missing_or_tampered_cached_file_is_recaptured(monkeypatch, tmp_path, damage):
    client, state, require, _ = _cached_capture_client(monkeypatch)
    cache = {}
    first_path, replacement_path = tmp_path / "first.png", tmp_path / "replacement.png"
    assert _capture(client, require, first_path, cache) == str(first_path)
    if damage == "missing":
        first_path.unlink()
    else:
        # 长度保持一致，只有内容摘要才能识别已被替换的证据。
        first_path.write_bytes(b"tampered pixels")
        assert first_path.stat().st_size == len(b"original pixels")
    state.calls.clear()
    metadata = {}

    assert _capture(client, require, replacement_path, cache, metadata) == str(replacement_path)
    assert replacement_path.read_bytes() == b"original pixels"
    assert state.calls == ["open_viewer", "copy_original", "save_original", "close_viewer", ("restore", 300)]
    _assert_asset_metadata(metadata, ASSET_A, hit=False)
    state.calls.clear()
    assert _capture(client, require, tmp_path / "another-duplicate.png", cache) == str(replacement_path)
    assert "copy_original" not in state.calls


@pytest.mark.parametrize("failure", ["source_after_save", "source_after_close", "source_after_restore", "close", "restore"])
def test_failed_source_close_or_return_does_not_publish_cache(monkeypatch, tmp_path, failure):
    client, state, require, image_class = _cached_capture_client(monkeypatch)
    cache, metadata = {}, {}
    if failure == "source_after_save":
        class InvalidatingImage(image_class):
            def save(self, path, **kwargs):
                super().save(path, **kwargs)
                state.valid = False

        monkeypatch.setattr(client._attachment_reader, "_copy_verified_viewer_image",
                            lambda *args, **kwargs: InvalidatingImage())
    elif failure == "source_after_close":
        state.on_close = lambda: setattr(state, "valid", False)
    elif failure == "source_after_restore":
        state.on_restore = lambda: setattr(state, "valid", False)
    elif failure == "close":
        state.close_succeeds = False
    else:
        state.restore_succeeds = False

    if failure == "source_after_restore":
        with pytest.raises(RuntimeError, match="attachment_target_changed"):
            _capture(client, require, tmp_path / "failed.png", cache, metadata)
    else:
        assert _capture(client, require, tmp_path / "failed.png", cache, metadata) == ""
    assert not cache
    assert metadata.get("asset_cache_hit") is not True
    if failure == "source_after_save":
        assert "close_viewer" not in state.calls and ("restore", 300) not in state.calls
    if failure in {"source_after_close", "close"}:
        assert ("restore", 300) not in state.calls


@pytest.mark.parametrize("failure", ["close", "restore", "source_after_restore"])
def test_cache_hit_still_requires_successful_close_return_and_current_source(monkeypatch, tmp_path, failure):
    client, state, require, _ = _cached_capture_client(monkeypatch)
    cache = {}
    proven_path = tmp_path / "proven.png"
    assert _capture(client, require, proven_path, cache) == str(proven_path)
    state.calls.clear()
    if failure == "close":
        state.close_succeeds = False
    elif failure == "restore":
        state.restore_succeeds = False
    else:
        state.on_restore = lambda: setattr(state, "valid", False)

    if failure == "source_after_restore":
        with pytest.raises(RuntimeError, match="attachment_target_changed"):
            _capture(client, require, tmp_path / "duplicate.png", cache)
    else:
        assert _capture(client, require, tmp_path / "duplicate.png", cache) == ""
    assert "copy_original" not in state.calls and "save_original" not in state.calls
    assert "open_viewer" in state.calls and "close_viewer" in state.calls


def test_cache_metadata_is_absent_without_this_round_cache(monkeypatch, tmp_path):
    client, state, require, _ = _cached_capture_client(monkeypatch)
    metadata = {}
    target = tmp_path / "uncached.png"

    assert _capture(client, require, target, None, metadata) == str(target)
    assert "asset_key_hash" not in metadata and "asset_cache_hit" not in metadata
    assert state.calls.count("copy_original") == 1
