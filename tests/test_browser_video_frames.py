import base64
import io
from pathlib import Path

import pytest
from PIL import Image

from agent.tools.browser import browser_service as module
from agent.tools.browser.browser_service import BrowserService
from agent.tools.browser.browser_tool import BrowserTool


def png_bytes(color, size=(12, 10)):
    stream = io.BytesIO()
    Image.new("RGB", size, color).save(stream, format="PNG")
    return stream.getvalue()


class VideoHandle:
    def __init__(self, page, *, tainted=False, identical=False, seek_error_at=None):
        self.page = page
        self.state = {
            "url": page.url, "duration": 92.331, "durationTolerance": 0.5,
            "width": 1080, "height": 1920,
            "currentSrc": "", "srcAttribute": None, "currentTime": 7.5,
            "paused": False, "muted": False, "ranges": [[0, 92.331]],
        }
        self.current_time = self.state["currentTime"]
        self.tainted = tainted
        self.identical = identical
        self.seek_error_at = seek_error_at
        self.seeks = []
        self.screenshots = []
        self.restores = []
        self.disposed = False
        self.detached = False
        self.source_changed_on_frame = None
        self.capture_count = 0
        self.time_mismatch = False
        self.duration_delta_on_frame = 0.0
        self.src_changed_on_frame = False

    def as_element(self):
        return self

    def evaluate(self, script, arg=None):
        if script == module._VIDEO_INITIAL_STATE_JS:
            return dict(self.state)
        if script == module._VIDEO_CHECK_JS:
            return (self.page.url == arg["url"] and not self.detached and
                    abs(self.state["duration"] - arg["duration"]) <= arg["durationTolerance"] and
                    self.state["currentSrc"] == arg["currentSrc"] and
                    self.state["srcAttribute"] == arg["srcAttribute"])
        if script == module._VIDEO_SEEK_JS:
            self.seeks.append(arg)
            if self.seek_error_at == len(self.seeks):
                return {"error": "video_seek_timeout"}
            self.current_time = arg["time"]
            return {"time_seconds": self.current_time + (4 if self.time_mismatch else 0)}
        if script == module._VIDEO_CANVAS_JS:
            self.capture_count += 1
            if self.capture_count == 1:
                self.state["duration"] += self.duration_delta_on_frame
                if self.src_changed_on_frame:
                    self.state["srcAttribute"] = "replacement.mp4"
            if self.source_changed_on_frame == self.capture_count:
                self.page.url = "https://example.test/unrelated"
            if self.tainted:
                raise RuntimeError("SecurityError: Tainted canvases may not be exported")
            return "data:image/png;base64," + base64.b64encode(self.image()).decode("ascii")
        if script == module._VIDEO_RESTORE_JS:
            self.restores.append(arg)
            self.current_time = arg["currentTime"]
            return True
        if "ready: v.readyState" in script:
            return {"time": self.current_time, "ready": 4, "seeking": False}
        raise AssertionError(f"Unexpected evaluation: {script[:70]}")

    def image(self):
        return png_bytes("red" if self.identical else (int(self.current_time * 2) % 255, 30, 70))

    def screenshot(self, **kwargs):
        self.screenshots.append(kwargs)
        return self.image()

    def dispose(self):
        self.disposed = True


class Page:
    def __init__(self):
        self.url = "https://example.test/video"
        self.video = VideoHandle(self)
        self.error = None

    def evaluate_handle(self, script):
        assert script == module._VISIBLE_VIDEO_JS
        if self.error:
            raise RuntimeError(self.error)
        return self.video

    def screenshot(self, **_kwargs):
        raise AssertionError("must never capture an unrelated page screenshot")


@pytest.fixture
def service():
    # No browser is launched by these deterministic protocol tests.
    service = object.__new__(BrowserService)
    service._page = Page()
    return service


def test_extracts_only_timestamped_actual_frames_and_restores(service, tmp_path):
    result = service._do_video_frames(6, 20000, str(tmp_path))
    assert result["reading_status"] == "partial"
    assert result["audio_read"] is False
    assert result["duration_seconds"] == 92.331
    assert len(result["frames"]) == 6
    assert result["frames"][0]["time_seconds"] == 0
    assert result["frames"][-1]["time_seconds"] == pytest.approx(91.831)
    assert all(Path(frame["path"]).is_absolute() and Path(frame["path"]).is_file() for frame in result["frames"])
    assert all(Path(frame["path"]).parent == tmp_path / "tmp" for frame in result["frames"])
    restored = service._page.video.restores[0]
    assert restored["currentTime"] == 7.5
    assert restored["paused"] is False
    assert restored["muted"] is False
    assert result["restored"] is True
    assert service._page.video.disposed is True


@pytest.mark.parametrize("error", ["unique_visible_video_required", "loaded_seekable_video_required"])
def test_ambiguous_or_unloaded_video_fails_without_page_screenshot(service, tmp_path, error):
    service._page.error = error
    result = service._do_video_frames(6, 20000, str(tmp_path))
    assert "error" in result
    assert result["frames"] == []
    assert result["reading_status"] == "unavailable"
    assert not list(tmp_path.rglob("*.png"))


def test_navigation_drops_and_deletes_all_captured_frames(service, tmp_path):
    service._page.video.source_changed_on_frame = 2
    result = service._do_video_frames(6, 20000, str(tmp_path))
    assert result["frames"] == []
    assert result["reading_status"] == "unavailable"
    assert "来源发生变化" in result["error"]
    assert service._page.video.restores == []
    assert not list(tmp_path.rglob("*.png"))


def test_media_source_duration_rounding_keeps_fixed_source_and_times(service, tmp_path):
    service._page.video.duration_delta_on_frame = 0.102
    result = service._do_video_frames(6, 20000, str(tmp_path))
    assert result["reading_status"] == "partial"
    assert len(result["frames"]) == 6
    assert result["duration_seconds"] == 92.331
    assert result["frames"][-1]["time_seconds"] == pytest.approx(91.831)
    assert result["restored"] is True


@pytest.mark.parametrize("delta", [0.501, -0.501, 10.0])
def test_large_duration_change_invalidates_all_frames(service, tmp_path, delta):
    service._page.video.duration_delta_on_frame = delta
    result = service._do_video_frames(6, 20000, str(tmp_path))
    assert result["reading_status"] == "unavailable"
    assert result["frames"] == []
    assert not list(tmp_path.rglob("*.png"))


def test_source_attribute_change_is_not_allowed_as_duration_rounding(service, tmp_path):
    service._page.video.duration_delta_on_frame = 0.102
    service._page.video.src_changed_on_frame = True
    result = service._do_video_frames(6, 20000, str(tmp_path))
    assert result["reading_status"] == "unavailable"
    assert result["frames"] == []


def test_cross_origin_canvas_uses_bound_video_screenshot(service, tmp_path):
    service._page.video.tainted = True
    result = service._do_video_frames(2, 20000, str(tmp_path))
    assert len(result["frames"]) == 2
    assert all(f["strategy"] == "video_element_screenshot" for f in result["frames"])
    assert len(service._page.video.screenshots) == 2
    assert all(0 < k["timeout"] <= 20000 for k in service._page.video.screenshots)


def test_decode_failure_retains_only_proven_partial_frames(service, tmp_path):
    service._page.video.seek_error_at = 3
    result = service._do_video_frames(6, 20000, str(tmp_path))
    assert result["reading_status"] == "partial"
    assert len(result["frames"]) == 2
    assert "video_seek_timeout" in result["limitation"]
    assert "error" not in result
    assert len(service._page.video.restores) == 1


def test_first_seek_timeout_returns_unavailable(service, tmp_path):
    service._page.video.seek_error_at = 1
    result = service._do_video_frames(6, 20000, str(tmp_path))
    assert result["reading_status"] == "unavailable"
    assert "error" in result
    assert result["frames"] == []


def test_identical_images_are_not_counted_as_distinct_frames(service, tmp_path):
    service._page.video.identical = True
    result = service._do_video_frames(6, 20000, str(tmp_path))
    assert len(result["frames"]) == 1
    assert len(list(tmp_path.rglob("*.png"))) == 1


def test_time_mismatch_cannot_be_labeled_as_requested_frame(service, tmp_path):
    service._page.video.time_mismatch = True
    result = service._do_video_frames(6, 20000, str(tmp_path))
    assert "video_frame_timestamp_mismatch" in result["error"]
    assert result["frames"] == []


def test_samples_require_seekable_ranges(service, tmp_path):
    service._page.video.state["ranges"] = [[0, 10]]
    result = service._do_video_frames(6, 20000, str(tmp_path))
    assert "video_sample_range_not_seekable" in result["error"]
    assert service._page.video.seeks == []


def test_total_budget_stops_before_new_seek_and_reserves_restore(service, tmp_path, monkeypatch):
    clock = [0.0]
    original = service._page.video.evaluate

    def evaluate(script, arg=None):
        value = original(script, arg)
        if script == module._VIDEO_CANVAS_JS:
            clock[0] += 10.0
        return value

    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    service._page.video.evaluate = evaluate
    result = service._do_video_frames(8, 20000, str(tmp_path))
    assert len(service._page.video.seeks) == 2
    assert len(result["frames"]) == 2
    assert "总时间预算" in result["limitation"]
    assert len(service._page.video.restores) == 1


def test_png_pixel_budget_and_actual_image_deduplication():
    converted, digest = BrowserService._bounded_video_png(png_bytes("red", (2000, 1500)))
    with Image.open(io.BytesIO(converted)) as image:
        assert image.width * image.height <= 1500000
    assert len(digest) == 64
    with pytest.raises(ValueError):
        BrowserService._bounded_video_png(b"not an image")


@pytest.mark.parametrize("value", [0, 9, True, 2.3, "6"])
def test_tool_rejects_bad_frame_limit_before_service(value, monkeypatch):
    tool = BrowserTool()
    monkeypatch.setattr(tool, "_check_engine_ready", lambda: None)
    monkeypatch.setattr(tool, "_get_service", lambda: pytest.fail("must validate first"))
    result = tool.execute({"action": "video_frames", "max_frames": value})
    assert result.status == "error"


def test_tool_exposes_frame_paths_to_vision_and_caps_timeout(monkeypatch, tmp_path):
    calls = []

    class StubService:
        def video_frames(self, **kwargs):
            calls.append(kwargs)
            return {"reading_status": "partial", "audio_read": False, "duration_seconds": 2.5,
                    "frames": [{"path": str(tmp_path / "frame.png"), "time_seconds": 0.1}],
                    "limitation": "音轨未读取"}

    tool = BrowserTool({"cwd": str(tmp_path)})
    monkeypatch.setattr(tool, "_check_engine_ready", lambda: None)
    monkeypatch.setattr(tool, "_get_service", lambda: StubService())
    result = tool.execute({"action": "video_frames", "max_frames": 3, "timeout": 999999})
    assert result.status == "success"
    assert calls == [{"max_frames": 3, "timeout": 20000, "cwd": str(tmp_path)}]
    assert "vision" in result.result and "音轨尚未读取" in result.result
    assert str(tmp_path / "frame.png") in result.result
    assert result.ext_data["reading_status"] == "partial"
    assert "video_frames" in BrowserTool.params["properties"]["action"]["enum"]


@pytest.mark.parametrize("value", [0, -1, True, 1.2, "20000"])
def test_service_rejects_bad_time_budget_without_submit(service, value):
    service._submit = lambda *_a, **_kw: pytest.fail("invalid budget must not submit")
    result = service.video_frames(timeout=value)
    assert result["reading_status"] == "unavailable"
    assert result["frames"] == []


def test_public_service_uses_serial_dispatch_and_caps_budget(service):
    calls = []
    service._submit = lambda *args: calls.append(args) or {"test": True}
    assert service.video_frames(6, 999999, "cwd") == {"test": True}
    assert calls == [(service._do_video_frames, 6, 20000, "cwd")]
