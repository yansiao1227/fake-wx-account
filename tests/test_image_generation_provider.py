"""Image-only compatible API routing and safe local artifacts; no external requests."""

import base64
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image


_image_buffer = io.BytesIO()
Image.new("RGB", (1, 1), "blue").save(_image_buffer, format="PNG")
PNG = _image_buffer.getvalue()
IMAGE_KEY = "synthetic-image-key"
CHAT_KEY = "synthetic-chat-key"
IMAGE_BASE = "https://image.synthetic.invalid/v1"
SIGNED_URL = "https://cdn.synthetic.invalid/output.png?signature=synthetic-signature"


@pytest.fixture
def generate(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "skills/image-generation/scripts/generate.py"
    spec = importlib.util.spec_from_file_location("synthetic_image_generation", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key in (
        "SKILL_IMAGE_GENERATION_API_KEY", "SKILL_IMAGE_GENERATION_API_BASE",
        "SKILL_IMAGE_GENERATION_MODEL", "SKILL_IMAGE_GENERATION_PROVIDER", "IMAGE_OUTPUT_DIR",
        "OPENAI_API_KEY", "OPENAI_API_BASE", "ARK_API_KEY", "ARK_API_BASE",
        "GEMINI_API_KEY", "DASHSCOPE_API_KEY", "MINIMAX_API_KEY", "LINKAI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    module._canonical_env_loader = module._load_skill_environment
    monkeypatch.setattr(module, "_load_skill_environment", lambda: None)
    monkeypatch.setattr(module.requests, "post", lambda *_args, **_kwargs: pytest.fail("unmocked HTTP POST"))
    monkeypatch.setattr(module.requests, "get", lambda *_args, **_kwargs: pytest.fail("unmocked HTTP GET"))
    return module


def provider(generate):
    return generate.OpenAIProvider(IMAGE_KEY, IMAGE_BASE, "gpt-image-2")


def response(payload, status=200):
    return SimpleNamespace(status_code=status, json=lambda: payload, text="synthetic response", reason="error")


def test_dedicated_image_credentials_do_not_modify_or_borrow_chat_credentials(generate, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", CHAT_KEY)
    monkeypatch.setenv("OPENAI_API_BASE", "https://chat.synthetic.invalid/v1")
    monkeypatch.setenv("SKILL_IMAGE_GENERATION_API_KEY", IMAGE_KEY)
    monkeypatch.setenv("SKILL_IMAGE_GENERATION_API_BASE", IMAGE_BASE)
    labels = generate._build_providers("gpt-image-2")
    assert len(labels) == 1 and labels[0][0] == "OpenAI"
    image_provider = labels[0][1]
    assert image_provider.api_key == IMAGE_KEY and image_provider.api_base == IMAGE_BASE
    assert generate.os.environ["OPENAI_API_KEY"] == CHAT_KEY
    assert generate.os.environ["OPENAI_API_BASE"] == "https://chat.synthetic.invalid/v1"


def test_dedicated_key_without_base_uses_official_image_endpoint(generate, monkeypatch):
    monkeypatch.setenv("SKILL_IMAGE_GENERATION_API_KEY", IMAGE_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", CHAT_KEY)
    monkeypatch.setenv("OPENAI_API_BASE", "https://chat.synthetic.invalid/v1")
    assert generate._build_providers("gpt-image-2")[0][1].api_base == "https://api.openai.com/v1"


def test_dedicated_base_without_key_is_explicit_configuration_error(generate, monkeypatch):
    monkeypatch.setenv("SKILL_IMAGE_GENERATION_API_BASE", IMAGE_BASE)
    monkeypatch.setenv("OPENAI_API_KEY", CHAT_KEY)
    monkeypatch.setenv("ARK_API_KEY", "synthetic-ark-key")
    with pytest.raises(ValueError, match="requires SKILL_IMAGE_GENERATION_API_KEY"):
        generate._build_providers("gpt-image-2")


def test_legacy_openai_and_other_provider_routing_still_work(generate, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", CHAT_KEY)
    monkeypatch.setenv("OPENAI_API_BASE", "https://chat.synthetic.invalid/v1")
    assert generate._build_providers("gpt-image-1")[0][1].api_key == CHAT_KEY
    monkeypatch.setenv("ARK_API_KEY", "synthetic-ark-key")
    providers = generate._build_providers("doubao-seedream-5-0-260128")
    assert len(providers) == 1 and providers[0][0] == "Seedream"


def test_generation_requests_exactly_one_image_and_saves_base64_absolute_path(generate, monkeypatch, tmp_path):
    calls = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(generate.requests, "post", lambda url, **kwargs: (
        calls.append((url, kwargs)) or response({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})))
    paths = provider(generate).generate("synthetic prompt", size="1K", quality="high", output_dir="images")
    url, request = calls[0]
    assert url == IMAGE_BASE + "/images/generations"
    assert request["json"] == {"model": "gpt-image-2", "prompt": "synthetic prompt", "n": 1,
                                "size": "1024x1024", "quality": "high"}
    assert request["headers"]["Authorization"] == "Bearer " + IMAGE_KEY
    assert len(paths) == 1 and Path(paths[0]).is_absolute()
    assert Path(paths[0]).read_bytes() == PNG


def test_signed_image_url_is_downloaded_immediately_without_generation_authorization(generate, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(generate.requests, "post", lambda *_args, **_kwargs: response({"data": [{"url": SIGNED_URL}]}))
    monkeypatch.setattr(generate.requests, "get", lambda url, **kwargs: (
        calls.append((url, kwargs)) or SimpleNamespace(content=PNG, raise_for_status=lambda: None)))
    paths = provider(generate).generate("synthetic prompt", output_dir=str(tmp_path))
    assert calls == [(SIGNED_URL, {"timeout": 60})]
    assert Path(paths[0]).is_absolute() and Path(paths[0]).read_bytes() == PNG
    assert SIGNED_URL not in paths[0]


@pytest.mark.parametrize("result", [{}, {"data": []}, {"data": None}, {"data": [{}]},
                                    {"data": [{"b64_json": ""}]}, {"data": [{"b64_json": "not-base64"}]}])
def test_empty_or_invalid_image_response_cannot_report_success(generate, monkeypatch, tmp_path, result):
    monkeypatch.setattr(generate.requests, "post", lambda *_args, **_kwargs: response(result))
    with pytest.raises(RuntimeError):
        provider(generate).generate("synthetic prompt", output_dir=str(tmp_path))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("download", [b"", b"<html>expired synthetic link</html>", PNG[:20]])
def test_empty_or_html_download_is_not_saved_as_image(generate, monkeypatch, tmp_path, download):
    monkeypatch.setattr(generate.requests, "post", lambda *_args, **_kwargs: response({"data": [{"url": SIGNED_URL}]}))
    monkeypatch.setattr(generate.requests, "get", lambda *_args, **_kwargs: SimpleNamespace(
        content=download, raise_for_status=lambda: None))
    with pytest.raises(RuntimeError):
        provider(generate).generate("synthetic prompt", output_dir=str(tmp_path))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("http_status", [200, 403])
def test_api_error_body_redacts_echoed_credentials_and_signed_url(generate, monkeypatch, tmp_path, http_status):
    payload = {"error": {"message": f"synthetic denied key={IMAGE_KEY} url={SIGNED_URL}"}}
    monkeypatch.setattr(generate.requests, "post", lambda *_args, **_kwargs: response(payload, http_status))
    with pytest.raises(RuntimeError) as error:
        provider(generate).generate("synthetic prompt", output_dir=str(tmp_path))
    assert IMAGE_KEY not in str(error.value) and "synthetic-signature" not in str(error.value)
    assert "synthetic denied" in str(error.value)


def test_download_transport_error_redacts_url_and_key(generate, monkeypatch, tmp_path):
    monkeypatch.setattr(generate.requests, "post", lambda *_args, **_kwargs: response({"data": [{"url": SIGNED_URL}]}))

    def failed_download(*_args, **_kwargs):
        raise RuntimeError(f"synthetic timeout {SIGNED_URL} {IMAGE_KEY}")

    monkeypatch.setattr(generate.requests, "get", failed_download)
    with pytest.raises(RuntimeError) as error:
        provider(generate).generate("synthetic prompt", output_dir=str(tmp_path))
    assert IMAGE_KEY not in str(error.value) and "synthetic-signature" not in str(error.value)
    assert list(tmp_path.iterdir()) == []


def test_main_loads_synthetic_canonical_env_before_provider_resolution(generate, monkeypatch, tmp_path, capsys):
    env_file = tmp_path / ".cow" / ".env"
    env_file.parent.mkdir()
    env_file.write_text("SKILL_IMAGE_GENERATION_API_KEY=synthetic-env-image-key\n", encoding="utf-8")
    monkeypatch.setattr(generate.os.path, "expanduser", lambda value: str(env_file) if value == "~/.cow/.env" else value)
    monkeypatch.setattr(generate, "_load_skill_environment", generate._canonical_env_loader)
    monkeypatch.setattr(generate.sys, "argv", ["generate.py", json.dumps({"prompt": "synthetic prompt"})])
    image = tmp_path / "result.png"
    image.write_bytes(PNG)
    seen = []

    def build(_model, **_kwargs):
        seen.append(generate.os.environ.get("SKILL_IMAGE_GENERATION_API_KEY"))
        return [("OpenAI", SimpleNamespace(model="gpt-image-2", generate=lambda *_args, **_kwargs: [str(image)]))]

    monkeypatch.setattr(generate, "_build_providers", build)
    generate.main()
    assert seen == ["synthetic-env-image-key"]
    assert json.loads(capsys.readouterr().out)["images"] == [{"url": str(image)}]


@pytest.mark.parametrize("failure", ["config", "environment", "provider", "empty_paths"])
def test_main_failure_is_json_and_does_not_echo_credentials_or_signed_url(generate, monkeypatch, tmp_path, capsys, failure):
    monkeypatch.setenv("SKILL_IMAGE_GENERATION_API_KEY", IMAGE_KEY)
    monkeypatch.setattr(generate.sys, "argv", ["generate.py", json.dumps({"prompt": "synthetic prompt"})])

    def fail(*_args, **_kwargs):
        raise RuntimeError(f"synthetic failure {IMAGE_KEY} {SIGNED_URL}")

    if failure == "config":
        monkeypatch.setattr(generate, "_build_providers", fail)
    elif failure == "environment":
        monkeypatch.setattr(generate, "_load_skill_environment", fail)
    else:
        fake = SimpleNamespace(model="gpt-image-2", api_key=IMAGE_KEY,
                               generate=fail if failure == "provider" else lambda *_args, **_kwargs: [])
        monkeypatch.setattr(generate, "_build_providers", lambda *_args, **_kwargs: [("OpenAI", fake)])
    with pytest.raises(SystemExit) as error:
        generate.main()
    assert error.value.code == 1
    output = capsys.readouterr()
    assert "error" in json.loads(output.out)
    assert IMAGE_KEY not in output.out + output.err
    assert "synthetic-signature" not in output.out + output.err
    assert "succeeded" not in output.err


def test_edit_request_never_falls_back_to_generation(generate, monkeypatch, tmp_path):
    image = tmp_path / "reference.png"
    image.write_bytes(PNG)
    calls = []
    monkeypatch.setattr(generate.requests, "post", lambda url, **kwargs: (
        calls.append(url) or response({"error": {"message": "synthetic edits unsupported"}}, 400)))
    with pytest.raises(RuntimeError, match="edits unsupported"):
        provider(generate).generate("synthetic edit", image_url=str(image), output_dir=str(tmp_path))
    assert calls == [IMAGE_BASE + "/images/edits"]


def test_live_auto_selection_survives_skill_dotenv_reload(generate, monkeypatch, tmp_path):
    from channel.web.web_channel import ModelsHandler

    env_file = tmp_path / ".env"
    env_file.write_text(
        "SKILL_IMAGE_GENERATION_MODEL=nano-banana\n"
        "SKILL_IMAGE_GENERATION_PROVIDER=gemini\n", encoding="utf-8")
    monkeypatch.setattr(generate.os.path, "expanduser", lambda _path: str(env_file))
    monkeypatch.setenv("SKILL_IMAGE_GENERATION_API_KEY", IMAGE_KEY)
    monkeypatch.setenv("SKILL_IMAGE_GENERATION_MODEL", "nano-banana")
    monkeypatch.setenv("SKILL_IMAGE_GENERATION_PROVIDER", "gemini")
    local_config = {"skills": {"image-generation": {"model": "nano-banana", "provider": "gemini"}}}
    monkeypatch.setattr("channel.web.web_channel.conf", lambda: local_config)
    monkeypatch.setattr(ModelsHandler, "_read_file_config", lambda _self: {})
    monkeypatch.setattr(ModelsHandler, "_write_file_config", lambda *_args: None)

    result = json.loads(ModelsHandler()._set_image("", ""))
    generate._canonical_env_loader()
    assert result["status"] == "success"
    assert generate.os.environ["SKILL_IMAGE_GENERATION_MODEL"] == ""
    assert generate.os.environ["SKILL_IMAGE_GENERATION_PROVIDER"] == ""
    cap = ModelsHandler._image_capability(local_config)
    assert cap["strategy"] == "auto" and cap["current_model"] == ""
    assert cap["fallback_provider"] == "openai"
    assert generate._build_providers("")[0][0] == "OpenAI"
