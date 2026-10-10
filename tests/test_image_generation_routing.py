"""Pinned image routing must preserve the selected vendor and model."""

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def generate(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "skills/image-generation/scripts/generate.py"
    spec = importlib.util.spec_from_file_location("synthetic_image_routing", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in (
        "SKILL_IMAGE_GENERATION_API_KEY", "SKILL_IMAGE_GENERATION_API_BASE",
        "SKILL_IMAGE_GENERATION_MODEL", "SKILL_IMAGE_GENERATION_PROVIDER",
        "OPENAI_API_KEY", "OPENAI_API_BASE", "GEMINI_API_KEY", "GEMINI_API_BASE",
        "ARK_API_KEY", "ARK_API_BASE", "DASHSCOPE_API_KEY", "DASHSCOPE_API_BASE",
        "MINIMAX_API_KEY", "MINIMAX_API_BASE", "LINKAI_API_KEY", "LINKAI_API_BASE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(module, "_load_skill_environment", lambda: None)
    monkeypatch.setattr(module.requests, "post", lambda *_args, **_kwargs: pytest.fail("unmocked HTTP POST"))
    monkeypatch.setattr(module.requests, "get", lambda *_args, **_kwargs: pytest.fail("unmocked HTTP GET"))
    return module


@pytest.mark.parametrize("provider_id, model, key_name", [
    ("gemini", "nano-banana", "GEMINI_API_KEY"),
    ("doubao", "seedream-5.0-lite", "ARK_API_KEY"),
    ("dashscope", "qwen-image-2.0", "DASHSCOPE_API_KEY"),
    ("minimax", "image-01", "MINIMAX_API_KEY"),
    ("linkai", "synthetic-custom-model", "LINKAI_API_KEY"),
])
def test_pinned_provider_missing_key_never_uses_dedicated_openai(
    generate, monkeypatch, provider_id, model, key_name,
):
    monkeypatch.setenv("SKILL_IMAGE_GENERATION_API_KEY", "synthetic-image-key")
    with pytest.raises(ValueError, match=key_name):
        generate._build_providers(model, provider_id)


def test_pinned_openai_missing_key_never_uses_another_provider(generate, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-gemini-key")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        generate._build_providers("gpt-image-2", "openai")


@pytest.mark.parametrize("model, key_name", [
    ("nano-banana", "GEMINI_API_KEY"),
    ("seedream-5.0-lite", "ARK_API_KEY"),
    ("qwen-image-2.0", "DASHSCOPE_API_KEY"),
    ("image-01", "MINIMAX_API_KEY"),
])
def test_model_only_missing_key_does_not_change_models(generate, monkeypatch, model, key_name):
    monkeypatch.setenv("SKILL_IMAGE_GENERATION_API_KEY", "synthetic-image-key")
    with pytest.raises(ValueError, match=key_name):
        generate._build_providers(model)


def test_unknown_model_requires_explicit_provider(generate, monkeypatch):
    monkeypatch.setenv("SKILL_IMAGE_GENERATION_API_KEY", "synthetic-image-key")
    monkeypatch.setenv("LINKAI_API_KEY", "synthetic-linkai-key")
    with pytest.raises(ValueError, match="Specify provider explicitly"):
        generate._build_providers("synthetic-custom-model")
    providers = generate._build_providers("synthetic-custom-model", "openai")
    assert len(providers) == 1
    assert providers[0][0] == "OpenAI"
    assert providers[0][1].model == "synthetic-custom-model"


def test_unknown_provider_is_not_overridden_by_model_inference(generate, monkeypatch):
    monkeypatch.setenv("SKILL_IMAGE_GENERATION_API_KEY", "synthetic-image-key")
    with pytest.raises(ValueError, match="Unknown image provider"):
        generate._build_providers("gpt-image-2", "synthetic-unknown-provider")


@pytest.mark.parametrize("provider_id, label, key_name, default_model", [
    ("openai", "OpenAI", "OPENAI_API_KEY", "gpt-image-2"),
    ("gemini", "Gemini", "GEMINI_API_KEY", "gemini-3.1-flash-image-preview"),
    ("doubao", "Seedream", "ARK_API_KEY", "doubao-seedream-5-0-260128"),
    ("dashscope", "Qwen", "DASHSCOPE_API_KEY", "qwen-image-2.0"),
    ("minimax", "MiniMax", "MINIMAX_API_KEY", "image-01"),
    ("linkai", "LinkAI", "LINKAI_API_KEY", "gpt-image-2"),
])
def test_provider_only_uses_its_own_default_model(
    generate, monkeypatch, provider_id, label, key_name, default_model,
):
    monkeypatch.setenv("SKILL_IMAGE_GENERATION_API_KEY", "synthetic-image-key")
    monkeypatch.setenv(key_name, "synthetic-selected-key")
    providers = generate._build_providers("", provider_id)
    assert len(providers) == 1
    assert providers[0][0] == label
    assert providers[0][1].model == default_model


def test_auto_routing_tries_configured_providers_with_their_default_models(generate, monkeypatch):
    monkeypatch.setenv("SKILL_IMAGE_GENERATION_API_KEY", "synthetic-image-key")
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-gemini-key")
    monkeypatch.setenv("ARK_API_KEY", "synthetic-ark-key")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "synthetic-dashscope-key")
    monkeypatch.setenv("MINIMAX_API_KEY", "synthetic-minimax-key")
    monkeypatch.setenv("LINKAI_API_KEY", "synthetic-linkai-key")
    providers = generate._build_providers("")
    assert [(label, provider.model) for label, provider in providers] == [
        ("OpenAI", "gpt-image-2"),
        ("Gemini", "gemini-3.1-flash-image-preview"),
        ("Seedream", "doubao-seedream-5-0-260128"),
        ("Qwen", "qwen-image-2.0"),
        ("MiniMax", "image-01"),
        ("LinkAI", "gpt-image-2"),
    ]


def test_whitespace_key_is_missing_for_pinned_provider(generate, monkeypatch):
    monkeypatch.setenv("SKILL_IMAGE_GENERATION_API_KEY", "synthetic-image-key")
    monkeypatch.setenv("GEMINI_API_KEY", "   ")
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        generate._build_providers("", "gemini")


@pytest.mark.parametrize("source", ["args", "environment"])
def test_main_reports_missing_pinned_credentials_before_generation(generate, monkeypatch, capsys, source):
    monkeypatch.setenv("SKILL_IMAGE_GENERATION_API_KEY", "synthetic-image-key")
    args = {"prompt": "synthetic prompt"}
    if source == "args":
        args.update(provider="gemini", model="nano-banana")
    else:
        monkeypatch.setenv("SKILL_IMAGE_GENERATION_PROVIDER", "gemini")
        monkeypatch.setenv("SKILL_IMAGE_GENERATION_MODEL", "nano-banana")
    monkeypatch.setattr(generate.sys, "argv", ["generate.py", json.dumps(args)])
    with pytest.raises(SystemExit) as error:
        generate.main()
    assert error.value.code == 1
    output = capsys.readouterr()
    assert "GEMINI_API_KEY" in json.loads(output.out)["error"]
    assert "synthetic-image-key" not in output.out + output.err
    assert "Trying" not in output.err


def test_pinned_generation_failure_never_tries_other_configured_providers(generate, monkeypatch, capsys):
    monkeypatch.setenv("SKILL_IMAGE_GENERATION_API_KEY", "synthetic-image-key")
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-gemini-key")
    monkeypatch.setattr(generate.sys, "argv", [
        "generate.py", json.dumps({"prompt": "synthetic prompt", "model": "nano-banana"}),
    ])
    calls = []

    def failed_generation(self, *_args, **_kwargs):
        calls.append(self.model)
        raise RuntimeError("synthetic Gemini failure")

    monkeypatch.setattr(generate.GeminiProvider, "generate", failed_generation)
    with pytest.raises(SystemExit) as error:
        generate.main()
    assert error.value.code == 1
    assert calls == ["gemini-2.5-flash-image"]
    output = capsys.readouterr()
    assert "Gemini failure" in json.loads(output.out)["error"]
    assert "Trying OpenAI" not in output.err
