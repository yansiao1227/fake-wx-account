"""UIA 附件操作保留，接收与消息身份始终由数据库提供。"""

from dataclasses import replace

import pytest

from channel.wechat_desktop.models import UiaChatMessage, UiaReferencedMessage, WechatDesktopEvent
from channel.wechat_desktop.uia.gateway import WechatUiaGateway
from channel.wechat_desktop.uia.materializer import WechatUiaMaterializer
from .helpers import FakeClient


def native_event(content_type="text", content="hello", **kwargs):
    return WechatDesktopEvent(
        "message", "native-conversation", "Alice", "native-sender", "Alice",
        content_type, content, account_id="synthetic-account",
        source_stream_id="message_0/Msg_synthetic", source_message_id="message_0/Msg_synthetic/1",
        source_local_id=1, native_timestamp=100, receipt_phase="live", **kwargs,
    )


def materializer_for(client):
    return WechatUiaMaterializer({}, gateway=WechatUiaGateway({}, client=client))


def materialize_native_target(materializer, message, *, local_id=1, account_id="synthetic-account"):
    """旧操作测试也必须提供合成原生身份，不保留生产宽松入口。"""
    event = native_event(message.message_type, message.content)
    event.source_message_id = f"message_0/Msg_synthetic/{local_id}"
    event.source_local_id = local_id
    event.account_id = account_id
    if message.reference is not None:
        event.reference = {"content_type": message.reference.message_type,
                           "content": message.reference.content,
                           "sender_name": message.reference.sender_name,
                           "resolved": message.reference.resolved}
    return materializer.materialize_event(event, target_message=message)


def test_private_file_target_is_fetched_and_event_uses_local_path(tmp_path):
    local_file = tmp_path / "report.pdf"
    local_file.write_bytes(b"%PDF-test")
    client = FakeClient()
    client.file_paths["report.pdf 8 KB"] = str(local_file)
    materializer = materializer_for(client)
    event = native_event("file", "report.pdf 8 KB")
    target = UiaChatMessage("Alice", event.content, "file", "incoming", "file-runtime")

    assert client.file_fetches == []
    materialized, resolve_count = materializer.materialize_event(event, target_message=target)

    assert resolve_count == 1
    assert materialized.content_type == "file"
    assert materialized.content == str(local_file)
    assert materialized.attachment_status == "materialized"
    assert client.file_fetches == ["report.pdf 8 KB"]
    assert client.file_cache_policies == [False]


def test_private_image_target_is_captured_and_event_uses_local_path(tmp_path):
    local_image = tmp_path / "photo.png"
    local_image.write_bytes(b"png")
    client = FakeClient()
    client.image_paths["[图片]"] = str(local_image)
    materializer = materializer_for(client)
    event = native_event("image", "[图片]")
    target = UiaChatMessage("Alice", "[图片]", "image", "incoming", "image-runtime", (100, 100, 300, 260))

    materialized, resolve_count = materializer.materialize_event(event, target_message=target)

    assert resolve_count == 1
    assert materialized.content == str(local_image)
    assert materialized.evidence_path == str(local_image)
    assert client.image_fetches == ["[图片]"]


def test_private_text_target_does_not_resolve_preceding_visible_file(tmp_path):
    local_file = tmp_path / "report.pdf"
    local_file.write_bytes(b"%PDF-test")
    client = FakeClient()
    client.file_paths["report.pdf"] = str(local_file)
    materializer = materializer_for(client)
    event = native_event(history=[{"content_type": "file", "content": "report.pdf"}])

    materialized, resolve_count = materializer.materialize_event(
        event, target_message=UiaChatMessage("Alice", "hello", runtime_id="text-runtime")
    )

    assert resolve_count == 0
    assert client.file_fetches == []
    assert materialized.history == [{"content_type": "file", "content": "report.pdf"}]


def test_materialization_preserves_native_identity_and_reference_id(tmp_path):
    image = tmp_path / "quoted.png"
    image.write_bytes(b"png")
    client = FakeClient()
    def fetch_reference_image(_message, *, strict=False, validate_target=None):
        assert strict is True
        return str(image)

    client.fetch_referenced_message_image = fetch_reference_image
    materializer = materializer_for(client)
    event = native_event(
        reference={"source_message_id": "native-reference", "content_type": "image"},
        history=[{"is_reference": True, "source_message_id": "native-reference"}],
    )
    identity = {key: getattr(event, key) for key in (
        "account_id", "conversation_id", "sender_id", "event_id", "source_stream_id",
        "source_message_id", "source_local_id", "native_timestamp", "receipt_phase",
        "message_runtime_id", "message_stable_id",
    )}
    fingerprint = event.fingerprint()
    target = UiaChatMessage(
        "Alice", "hello", runtime_id="uia-reference-runtime",
        reference=UiaReferencedMessage("Alice", "[图片]", "image"),
    )

    result, count = materializer.materialize_event(event, target_message=target)

    assert count == 1
    assert result.reference["source_message_id"] == "native-reference"
    assert result.reference["file_path"] == str(image)
    assert result.history[0]["source_message_id"] == "native-reference"
    assert result.fingerprint() == fingerprint
    assert {key: getattr(result, key) for key in identity} == identity


def test_validation_failure_prevents_any_attachment_action():
    client = FakeClient()
    materializer = materializer_for(client)

    def invalid_target():
        raise RuntimeError("window switched")

    with pytest.raises(RuntimeError, match="window switched"):
        materializer.materialize_event(
            native_event("image", "[图片]"),
            target_message=UiaChatMessage("Alice", "[图片]", "image"),
            validate=invalid_target,
        )
    assert client.image_fetches == []
    assert client.file_fetches == []


def test_false_validation_is_rejected_before_attachment_action():
    client = FakeClient()
    with pytest.raises(RuntimeError, match="attachment_target_changed"):
        materializer_for(client).materialize_event(
            native_event("image", "[图片]"),
            target_message=UiaChatMessage("Alice", "[图片]", "image"),
            validate=lambda: False,
        )
    assert client.image_fetches == []


@pytest.mark.parametrize("kind", ["image", "file"])
def test_media_reference_requires_an_existing_local_file(tmp_path, kind):
    client = FakeClient()
    missing = str(tmp_path / "missing-attachment.bin")
    client.fetch_referenced_message_image = lambda _message, **kwargs: missing
    client.resolve_message_reference = lambda message, **kwargs: replace(
        message, reference=replace(message.reference, resolved=True, file_path=missing)
    )
    event = native_event(reference={"content_type": kind, "source_message_id": "quoted-native"})
    target = UiaChatMessage("Alice", "hello", runtime_id="quote",
                            reference=UiaReferencedMessage("Alice", "synthetic media", kind))

    result, _ = materializer_for(client).materialize_event(event, target_message=target)

    assert result.attachment_status == "unavailable"
    assert result.reference["resolved"] is False
    assert result.reference["degraded"] is True
    assert result.reference["file_path"] == ""
    assert result.reference["source_message_id"] == "quoted-native"
    assert result.history[-1]["resolved"] is False


def test_native_reference_never_uses_filename_only_cache(tmp_path):
    old_file, referenced_file = tmp_path / "old.pdf", tmp_path / "referenced.pdf"
    old_file.write_bytes(b"old")
    referenced_file.write_bytes(b"referenced")
    client = FakeClient()
    client.file_paths["report.pdf"] = str(old_file)
    materializer = materializer_for(client)
    materialize_native_target(
        materializer, UiaChatMessage("Alice", "report.pdf", "file", runtime_id="old-file"), local_id=2)
    calls = []

    def resolve_reference(message, *, allow_filename_cache=True, validate_target=None):
        assert allow_filename_cache is False
        calls.append(message.reference.content)
        return replace(message, reference=replace(
            message.reference, file_path=str(referenced_file), resolved=True
        ))

    client.resolve_message_reference = resolve_reference
    quoted = UiaChatMessage("Alice", "hello", runtime_id="quote", reference=UiaReferencedMessage("Alice", "report.pdf", "file"))

    result, count = materializer.materialize_event(native_event(), target_message=quoted)

    assert count == 1
    assert calls == ["report.pdf"]
    assert result.reference["file_path"] == str(referenced_file)


def test_share_url_from_validated_uia_target_is_available_to_agent_without_prefetch(monkeypatch):
    client = FakeClient()
    gateway = WechatUiaGateway({}, client=client)
    calls = []

    def forbid_fetch(*_args, **_kwargs):
        raise AssertionError("UIA must leave URL reading to Agent tools")

    def resolve_reference(message, *, allow_filename_cache=True, validate_target=None):
        assert allow_filename_cache is False
        assert gateway.priority._owner is not None
        calls.append("resolve")
        return replace(message, reference=replace(
            message.reference, url="https://example.com/article", resolved=True
        ))

    def validate():
        assert gateway.priority._owner is not None
        calls.append("validate")
        return True

    monkeypatch.setattr("agent.tools.web_fetch.web_fetch.WebFetch.execute", forbid_fetch)
    client.resolve_message_reference = resolve_reference
    materializer = WechatUiaMaterializer({}, gateway=gateway)
    target = UiaChatMessage("Alice", "hello", reference=UiaReferencedMessage("Alice", "article", "share_card"))

    result, count = materializer.materialize_event(
        native_event(reference={"source_message_id": "synthetic-reference"}),
        target_message=target, validate=validate,
    )

    assert count == 1
    assert calls == ["validate", "resolve"]
    assert gateway.priority._owner is None
    assert result.reference["url"] == "https://example.com/article"
    assert result.reference["source_message_id"] == "synthetic-reference"
    assert result.reference["fetch_status"] == "link_available"
    assert result.reference["fetched_content"] == ""
    assert result.reference["resolved"] is False


def test_share_reference_preserves_native_url_when_uia_returns_only_body():
    client = FakeClient()

    def resolve_reference(message, *, allow_filename_cache=True, validate_target=None):
        return replace(message, reference=replace(
            message.reference, browser_content="合成浏览器正文", browser_status="success",
        ))

    client.resolve_message_reference = resolve_reference
    materializer = materializer_for(client)
    event = native_event(reference={
        "url": "https://example.com/article?access=synthetic",
        "source_message_id": "synthetic-reference",
    })
    target = UiaChatMessage(
        "Alice", "hello", reference=UiaReferencedMessage("Alice", "article", "share_card"),
    )

    result, count = materializer.materialize_event(event, target_message=target)

    assert count == 1
    assert result.reference["url"] == "https://example.com/article?access=synthetic"
    assert result.reference["browser_content"] == "合成浏览器正文"
    assert result.reference["fetch_status"] == "direct_browser"
    assert result.reference["fetched_content"] == ""


def test_share_reference_preserves_existing_browser_body_when_uia_returns_only_url():
    client = FakeClient()

    def resolve_reference(message, *, allow_filename_cache=True, validate_target=None):
        return replace(message, reference=replace(
            message.reference, url="https://example.com/article",
        ))

    client.resolve_message_reference = resolve_reference
    materializer = materializer_for(client)
    event = native_event(reference={
        "browser_content": "此前已验证读取的合成浏览器正文",
        "browser_status": "success",
    })
    target = UiaChatMessage(
        "Alice", "hello", reference=UiaReferencedMessage("Alice", "article", "share_card"),
    )

    result, count = materializer.materialize_event(event, target_message=target)

    assert count == 1
    assert result.reference["url"] == "https://example.com/article"
    assert result.reference["browser_content"] == "此前已验证读取的合成浏览器正文"
    assert result.reference["fetch_status"] == "direct_browser"
    assert result.reference["resolved"] is True


def test_share_reference_keeps_explicit_browser_failure_as_unread():
    event = native_event(reference={"content_type": "share_card"})
    resolved = UiaChatMessage("Alice", "question", reference=UiaReferencedMessage(
        "Alice", "synthetic title", "share_card", url="https://example.invalid/article",
        browser_content="synthetic access failure", browser_status="error",
        resolved=True, degraded=False,
    ))

    WechatUiaMaterializer._apply_resolved_message(event, resolved)

    assert event.reference["browser_status"] == "error"
    assert event.reference["fetch_status"] == "link_available"
    assert not event.reference["resolved"] and event.reference["degraded"]
    assert event.reference["fetch_source"] == ""


@pytest.mark.parametrize("field", ["account_id", "source_message_id", "conversation_id"])
def test_materializer_rejects_missing_native_identity_before_ui_actions(field):
    client = FakeClient()
    event = native_event("file", "synthetic.pdf")
    setattr(event, field, "")
    with pytest.raises(RuntimeError, match="attachment_native_identity_required"):
        materializer_for(client).materialize_event(
            event, target_message=UiaChatMessage("Alice", "synthetic.pdf", "file", runtime_id="file"))
    assert client.file_fetches == []


@pytest.mark.parametrize("kind", ["file", "image", "share_card"])
def test_materializer_rejects_known_reference_type_before_uia_call(kind):
    client = FakeClient()
    client.resolve_message_reference = lambda *_args, **_kwargs: pytest.fail("错类型不应触发定位")
    event = native_event(reference={"content_type": kind, "content": "synthetic preview"})
    target = UiaChatMessage("Alice", "hello", runtime_id="quote",
        reference=UiaReferencedMessage("Alice", "synthetic preview", "text"))
    with pytest.raises(RuntimeError, match="attachment_reference_type_changed"):
        materializer_for(client).materialize_event(event, target_message=target)


def test_materializer_rejects_type_change_returned_by_resolver():
    client = FakeClient()
    client.resolve_message_reference = lambda message, **kwargs: replace(
        message, reference=replace(message.reference, message_type="text", resolved=True))
    event = native_event(reference={"content_type": "file", "content": "synthetic.pdf"})
    target = UiaChatMessage("Alice", "hello", runtime_id="quote",
        reference=UiaReferencedMessage("Alice", "synthetic.pdf", "file"))
    with pytest.raises(RuntimeError, match="attachment_reference_type_changed"):
        materializer_for(client).materialize_event(event, target_message=target)
    assert event.reference == {"content_type": "file", "content": "synthetic.pdf"}


def test_materialization_preserves_native_sender_type_and_share_metadata():
    event = native_event(is_group=True, reference={
        "sender_name": "Native sender", "sender_id": "wxid_synthetic",
        "content_type": "share_card", "content": "Native title", "title": "Native title",
        "platform": "Native platform", "description": "Native description",
        "source_message_id": "quoted-source", "url": "https://example.com/native",
    })
    resolved = UiaChatMessage("UI sender", "hello", reference=UiaReferencedMessage(
        "UI sender", "UI title", "share_card", platform="UI platform",
        url="https://example.com/copied", browser_content="synthetic article", browser_status="success"))

    WechatUiaMaterializer._apply_resolved_message(event, resolved)

    assert event.reference["sender_name"] == "Native sender"
    assert event.reference["sender_id"] == "wxid_synthetic"
    assert event.reference["content_type"] == "share_card"
    assert event.reference["content"] == event.reference["title"] == "Native title"
    assert event.reference["platform"] == "Native platform"
    assert event.reference["description"] == "Native description"
    assert event.reference["url"] == "https://example.com/native"
    assert event.reference["browser_content"] == "synthetic article"
    assert event.history[-1]["sender_name"] == "Native sender"


@pytest.mark.parametrize("kind", ["app_message", "unsupported", "text"])
def test_unknown_or_unresolved_reference_can_gain_verified_media_type(tmp_path, kind):
    path = tmp_path / "synthetic.pdf"
    path.write_bytes(b"synthetic file")
    client = FakeClient()
    client.resolve_message_reference = lambda message, **kwargs: replace(
        message, reference=replace(message.reference, message_type="file", file_path=str(path),
                                   resolved=True, strategy="wechat_locate_original"))
    event = native_event(reference={"content_type": kind, "resolved": False,
                                  "sender_name": "Native sender", "sender_id": "synthetic-sender"})
    target = UiaChatMessage("UI sender", "hello", runtime_id="quote",
        reference=UiaReferencedMessage("UI sender", "synthetic.pdf", kind))

    result, count = materializer_for(client).materialize_event(event, target_message=target)

    assert count == 1
    assert result.reference["content_type"] == "file"
    assert result.reference["sender_name"] == "Native sender"
    assert result.reference["sender_id"] == "synthetic-sender"
    assert result.reference["file_path"] == str(path)


@pytest.mark.parametrize("changed", ["account", "source", "runtime", "content"])
def test_path_cache_never_crosses_account_native_runtime_or_content(tmp_path, changed):
    path = tmp_path / "synthetic.png"
    path.write_bytes(b"synthetic image")
    client = FakeClient()
    client.image_paths["synthetic image"] = str(path)
    client.image_paths["changed image"] = str(path)
    materializer = materializer_for(client)
    first = UiaChatMessage("Alice", "synthetic image", "image", runtime_id="runtime")
    _, count1 = materialize_native_target(materializer, first)
    _, cache_count = materialize_native_target(materializer, first)
    second = replace(first, runtime_id="changed" if changed == "runtime" else first.runtime_id,
                     content="changed image" if changed == "content" else first.content)
    _, count2 = materialize_native_target(materializer, second,
        local_id=2 if changed == "source" else 1,
        account_id="other-synthetic-account" if changed == "account" else "synthetic-account")

    assert (count1, cache_count, count2) == (1, 0, 1)
    assert len(client.image_fetches) == 2


def test_swallowed_target_invalidation_still_prevents_event_update_and_cache(tmp_path):
    path = tmp_path / "synthetic.png"
    path.write_bytes(b"synthetic image")
    client = FakeClient()
    state = {"valid": True}

    def fetch_image(message, *, prefer_viewer=True, strict=False, validate_target=None):
        state["valid"] = False
        try:
            validate_target()
        except RuntimeError:
            pass
        # 附件 API 吞掉错误后，即使窗口恢复，也不能接受此次产物。
        state["valid"] = True
        return str(path)

    client.fetch_message_image = fetch_image
    materializer = materializer_for(client)
    event = native_event("image", "synthetic image")
    target = UiaChatMessage("Alice", "synthetic image", "image", runtime_id="image")

    with pytest.raises(RuntimeError, match="attachment_target_changed"):
        materializer.materialize_event(event, target_message=target, validate_target=lambda: state["valid"])

    assert event.content == "synthetic image"
    assert not materializer._attachment_path_cache
