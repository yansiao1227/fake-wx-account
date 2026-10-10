# encoding:utf-8
import json
import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if "web" not in sys.modules:
    web_stub = types.ModuleType("web")
    web_stub.HTTPError = type("HTTPError", (Exception,), {})
    web_stub.cookies = lambda: {}
    web_stub.header = lambda *args, **kwargs: None
    web_stub.data = lambda: b"{}"
    web_stub.input = lambda **kwargs: types.SimpleNamespace(**kwargs)
    web_stub.setcookie = lambda *args, **kwargs: None
    web_stub.seeother = lambda *args, **kwargs: Exception("seeother")
    web_stub.notfound = lambda *args, **kwargs: Exception("notfound")
    web_stub.badrequest = lambda *args, **kwargs: Exception("badrequest")
    web_stub.application = lambda *args, **kwargs: types.SimpleNamespace(wsgifunc=lambda: None)
    web_stub.httpserver = types.SimpleNamespace(
        LogMiddleware=type("LogMiddleware", (), {"log": lambda *args, **kwargs: None}),
        StaticMiddleware=lambda app: app,
        WSGIServer=lambda *args, **kwargs: types.SimpleNamespace(serve_forever=lambda: None),
    )
    sys.modules["web"] = web_stub


class TestModelsHandler(unittest.TestCase):
    def test_set_asr_capability_persists_provider_and_model(self):
        from channel.web.web_channel import ModelsHandler

        local_config = {}
        file_config = {}
        handler = ModelsHandler()

        with patch("channel.web.web_channel.conf", return_value=local_config):
            with patch.object(ModelsHandler, "_read_file_config", return_value=file_config):
                with patch.object(ModelsHandler, "_write_file_config") as write_file:
                    with patch.object(ModelsHandler, "_refresh_voice_routing") as refresh_voice:
                        result = json.loads(handler._handle_set_capability({
                            "capability": "asr",
                            "provider_id": "dashscope",
                            "model": "qwen3-asr-flash",
                        }))

        self.assertEqual(result["status"], "success")
        self.assertEqual(local_config["voice_to_text"], "dashscope")
        self.assertEqual(local_config["voice_to_text_model"], "qwen3-asr-flash")
        self.assertEqual(file_config["voice_to_text"], "dashscope")
        self.assertEqual(file_config["voice_to_text_model"], "qwen3-asr-flash")
        write_file.assert_called_once_with(file_config)
        refresh_voice.assert_called_once()

    def test_set_asr_empty_model_keeps_existing(self):
        # Switching provider with an empty model must not wipe a user's
        # hand-configured voice_to_text_model.
        from channel.web.web_channel import ModelsHandler

        local_config = {"voice_to_text_model": "qwen3-asr-flash"}
        file_config = {"voice_to_text_model": "qwen3-asr-flash"}
        handler = ModelsHandler()

        with patch("channel.web.web_channel.conf", return_value=local_config):
            with patch.object(ModelsHandler, "_read_file_config", return_value=file_config):
                with patch.object(ModelsHandler, "_write_file_config"):
                    with patch.object(ModelsHandler, "_refresh_voice_routing"):
                        result = json.loads(handler._handle_set_capability({
                            "capability": "asr",
                            "provider_id": "zhipu",
                            "model": "",
                        }))

        self.assertEqual(result["status"], "success")
        self.assertEqual(local_config["voice_to_text"], "zhipu")
        # Existing model preserved, not overwritten with "".
        self.assertEqual(local_config["voice_to_text_model"], "qwen3-asr-flash")
        self.assertEqual(file_config["voice_to_text_model"], "qwen3-asr-flash")
        self.assertEqual(result["model"], "qwen3-asr-flash")

    def test_asr_capability_exposes_provider_models(self):
        from channel.web.web_channel import ModelsHandler

        cap = ModelsHandler._asr_capability({
            "voice_to_text": "dashscope",
            "voice_to_text_model": "qwen3-asr-flash",
        })

        self.assertTrue(cap["editable"])
        self.assertEqual(cap["current_provider"], "dashscope")
        self.assertEqual(cap["current_model"], "qwen3-asr-flash")
        self.assertIn("provider_models", cap)
        self.assertIn("dashscope", cap["provider_models"])

    def test_image_auto_uses_dedicated_env_credentials_without_chat_credentials(self):
        from channel.web.web_channel import ModelsHandler

        with patch.dict(os.environ, {
            "SKILL_IMAGE_GENERATION_API_KEY": "synthetic-image-key",
            "SKILL_IMAGE_GENERATION_API_BASE": "https://image.example.invalid/v1",
        }, clear=True):
            cap = ModelsHandler._image_capability({"bot_type": "deepseek", "deepseek_api_key": "synthetic-chat-key"})

        self.assertEqual(cap["fallback_provider"], "openai")
        self.assertEqual(cap["fallback_model"], "gpt-image-2")
        self.assertTrue(cap["runtime_active"])
        self.assertNotEqual(cap["note"], "router_pending")
        self.assertNotIn("synthetic-image-key", json.dumps(cap))

    def test_image_auto_recognizes_dedicated_config_credentials(self):
        from channel.web.web_channel import ModelsHandler

        local_config = {"skills": {"image-generation": {
            "api_key": "synthetic-image-config-key", "api_base": "https://image.example.invalid/v1",
        }}}
        with patch.dict(os.environ, {}, clear=True):
            cap = ModelsHandler._image_capability(local_config)

        self.assertEqual(cap["fallback_provider"], "openai")
        self.assertTrue(cap["runtime_active"])
        self.assertNotIn("synthetic-image-config-key", json.dumps(cap))

    def test_image_env_key_has_priority_over_config_key(self):
        from channel.web.web_channel import ModelsHandler

        local_config = {"skills": {"image-generation": {"api_key": "synthetic-config-key"}}}
        # An explicitly configured placeholder in env takes priority too;
        # the console must not claim that the shadowed config key is active.
        with patch.dict(os.environ, {"SKILL_IMAGE_GENERATION_API_KEY": "YOUR_API_KEY"}, clear=True):
            prediction = ModelsHandler._predict_image_auto(local_config)

        self.assertEqual(prediction, {"provider": "", "model": ""})

    def test_image_env_model_and_provider_override_json_selection(self):
        from channel.web.web_channel import ModelsHandler

        local_config = {"skills": {"image-generation": {
            "provider": "gemini", "model": "nano-banana",
        }}}
        with patch.dict(os.environ, {
            "SKILL_IMAGE_GENERATION_API_KEY": "synthetic-image-key",
            "SKILL_IMAGE_GENERATION_MODEL": "gpt-image-2",
            "SKILL_IMAGE_GENERATION_PROVIDER": "openai",
        }, clear=True):
            cap = ModelsHandler._image_capability(local_config)

        self.assertEqual(cap["current_model"], "gpt-image-2")
        self.assertEqual(cap["current_provider"], "openai")
        self.assertEqual(cap["strategy"], "specified")
        self.assertEqual(cap["fallback_provider"], "")
        self.assertEqual(cap["fallback_model"], "")

    def test_image_provider_only_uses_pinned_provider_default_without_global_fallback(self):
        from channel.web.web_channel import ModelsHandler

        defaults = {
            "openai": "gpt-image-2",
            "gemini": "gemini-3.1-flash-image-preview",
            "doubao": "seedream-5.0-lite",
            "dashscope": "qwen-image-2.0",
            "minimax": "image-01",
            "linkai": "gpt-image-2",
        }
        for source in ("env", "config"):
            for provider, default_model in defaults.items():
                with self.subTest(source=source, provider=provider):
                    # A usable global OpenAI key must not turn an explicit
                    # selection into auto routing, even if that vendor has no key.
                    environment = {"OPENAI_API_KEY": "synthetic-openai-key"}
                    local_config = {}
                    if source == "env":
                        environment["SKILL_IMAGE_GENERATION_PROVIDER"] = provider
                    else:
                        local_config = {"skills": {"image-generation": {"provider": provider}}}
                    with patch.dict(os.environ, environment, clear=True):
                        cap = ModelsHandler._image_capability(local_config)

                    self.assertEqual(cap["strategy"], "specified")
                    self.assertEqual(cap["current_provider"], provider)
                    self.assertEqual(cap["current_model"], default_model)
                    self.assertEqual(cap["fallback_provider"], "")
                    self.assertEqual(cap["fallback_model"], "")
                    self.assertTrue(cap["runtime_active"])
                    self.assertEqual(cap["note"], "")

    def test_image_model_only_does_not_report_global_auto_fallback(self):
        from channel.web.web_channel import ModelsHandler

        local_config = {"skills": {"image-generation": {"model": "qwen-image-2.0"}}}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-openai-key"}, clear=True):
            cap = ModelsHandler._image_capability(local_config)

        self.assertEqual(cap["strategy"], "specified")
        self.assertEqual(cap["current_provider"], "dashscope")
        self.assertEqual(cap["current_model"], "qwen-image-2.0")
        self.assertEqual(cap["fallback_provider"], "")
        self.assertEqual(cap["fallback_model"], "")

    def test_empty_image_env_model_and_provider_override_json_selection(self):
        from channel.web.web_channel import ModelsHandler

        local_config = {"skills": {"image-generation": {
            "provider": "gemini", "model": "nano-banana",
        }}}
        with patch.dict(os.environ, {
            "SKILL_IMAGE_GENERATION_API_KEY": "synthetic-image-key",
            "SKILL_IMAGE_GENERATION_MODEL": "",
            "SKILL_IMAGE_GENERATION_PROVIDER": "",
        }, clear=True):
            cap = ModelsHandler._image_capability(local_config)

        self.assertEqual(cap["current_model"], "")
        self.assertEqual(cap["current_provider"], "")
        self.assertEqual(cap["strategy"], "auto")
        self.assertEqual(cap["fallback_provider"], "openai")

    def test_image_base_only_never_borrows_chat_key(self):
        from channel.web.web_channel import ModelsHandler

        for source in ("env", "config"):
            with self.subTest(source=source):
                local_config = {"open_ai_api_key": "synthetic-openai-chat-key"}
                environment = {"OPENAI_API_KEY": "synthetic-openai-env-chat-key"}
                if source == "env":
                    environment["SKILL_IMAGE_GENERATION_API_BASE"] = "https://image.example.invalid/v1"
                else:
                    local_config["skills"] = {"image-generation": {
                        "api_base": "https://image.example.invalid/v1",
                    }}
                with patch.dict(os.environ, environment, clear=True):
                    cap = ModelsHandler._image_capability(local_config)

                self.assertEqual(cap["fallback_provider"], "")
                self.assertEqual(cap["fallback_model"], "")

    def test_image_base_only_does_not_predict_silent_fallback_to_another_vendor(self):
        from channel.web.web_channel import ModelsHandler

        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "synthetic-chat-key",
            "SKILL_IMAGE_GENERATION_API_BASE": "https://image.example.invalid/v1",
            "GEMINI_API_KEY": "synthetic-gemini-image-key",
        }, clear=True):
            cap = ModelsHandler._image_capability({})

        self.assertEqual(cap["fallback_provider"], "")
        self.assertEqual(cap["fallback_model"], "")

    def test_explicit_empty_image_env_credentials_override_config_values(self):
        from channel.web.web_channel import ModelsHandler

        local_config = {"skills": {"image-generation": {
            "api_key": "synthetic-image-config-key", "api_base": "https://image.example.invalid/v1",
        }}}
        with patch.dict(os.environ, {
            "SKILL_IMAGE_GENERATION_API_KEY": "  ", "SKILL_IMAGE_GENERATION_API_BASE": "  ",
        }, clear=True):
            cap = ModelsHandler._image_capability(local_config)

        self.assertEqual(cap["fallback_provider"], "")

    def test_empty_image_credentials_keep_legacy_openai_fallback(self):
        from channel.web.web_channel import ModelsHandler

        local_config = {"skills": {"image-generation": {"api_key": "", "api_base": ""}}}
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "synthetic-legacy-openai-key",
            "SKILL_IMAGE_GENERATION_API_KEY": "", "SKILL_IMAGE_GENERATION_API_BASE": "",
        }, clear=True):
            cap = ModelsHandler._image_capability(local_config)

        self.assertEqual(cap["fallback_provider"], "openai")

    def test_chat_only_provider_keys_are_not_image_credentials(self):
        from channel.web.web_channel import ModelsHandler

        local_config = {
            "bot_type": "custom", "custom_api_key": "synthetic-custom-chat-key",
            "deepseek_api_key": "synthetic-deepseek-chat-key", "mimo_api_key": "synthetic-mimo-chat-key",
        }
        with patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "synthetic-deepseek-env-key",
            "CUSTOM_API_KEY": "synthetic-custom-env-key",
        }, clear=True):
            cap = ModelsHandler._image_capability(local_config)

        self.assertEqual(cap["fallback_provider"], "")
        self.assertEqual(cap["fallback_model"], "")

    def test_chat_linkai_flag_does_not_hide_dedicated_image_routing(self):
        from channel.web.web_channel import ModelsHandler

        with patch.dict(os.environ, {"SKILL_IMAGE_GENERATION_API_KEY": "synthetic-image-key"}, clear=True):
            cap = ModelsHandler._image_capability({"use_linkai": True, "linkai_api_key": "synthetic-linkai-key"})

        self.assertEqual(cap["fallback_provider"], "openai")

    def test_set_image_pins_provider_and_model_and_updates_active_ui_state(self):
        from channel.web.web_channel import ModelsHandler

        local_config = {"skills": {"image-generation": {
            "api_key": "synthetic-image-key", "api_base": "https://image.example.invalid/v1",
        }}}
        file_config = {}
        handler = ModelsHandler()
        with patch.dict(os.environ, {}, clear=True):
            with patch("channel.web.web_channel.conf", return_value=local_config):
                with patch.object(ModelsHandler, "_read_file_config", return_value=file_config):
                    with patch.object(ModelsHandler, "_write_file_config") as write_file:
                        result = json.loads(handler._handle_set_capability({
                            "capability": "image", "provider_id": "openai", "model": "synthetic-image-alias",
                        }))
                        cap = ModelsHandler._image_capability(local_config)
            self.assertEqual(os.environ["SKILL_IMAGE_GENERATION_MODEL"], "synthetic-image-alias")
            self.assertEqual(os.environ["SKILL_IMAGE_GENERATION_PROVIDER"], "openai")

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["runtime_active"])
        self.assertFalse(result["router_pending"])
        self.assertEqual(cap["current_provider"], "openai")
        self.assertEqual(cap["current_model"], "synthetic-image-alias")
        self.assertTrue(cap["runtime_active"])
        self.assertEqual(local_config["skills"]["image-generation"]["api_key"], "synthetic-image-key")
        self.assertEqual(file_config["skills"]["image-generation"], {
            "provider": "openai", "model": "synthetic-image-alias",
        })
        write_file.assert_called_once_with(file_config)


if __name__ == "__main__":
    unittest.main()
