"""在真实 JavaScript 运行时验证视频恢复，避免协议替身掩盖异步播放失败。"""

import pytest

from agent.tools.browser import browser_service as module


_RESTORE_SCENARIO_JS = """async ({script, scenario}) => {
    let playCalls = 0, playSettled = false, seekSettled = false;
    const video = new EventTarget();
    Object.assign(video, {
        isConnected: true, currentSrc: 'test.mp4', duration: 42,
        paused: true, muted: true, seeking: false,
        getAttribute: () => 'test.mp4',
        pause() { this.paused = true; },
        play() {
            playCalls += 1;
            if (scenario.mode === 'throw') throw new Error('play failed');
            if (scenario.mode === 'pending') return new Promise(() => {});
            if (scenario.mode === 'no_promise') {
                this.paused = false;
                playSettled = true;
                return undefined;
            }
            return new Promise((resolve, reject) => setTimeout(() => {
                playSettled = true;
                if (scenario.mode === 'reject') { reject(new Error('autoplay denied')); return; }
                this.paused = scenario.mode === 'still_paused';
                if (scenario.mode === 'wrong_time') currentTime += 1;
                if (scenario.mode === 'still_seeking') this.seeking = true;
                if (scenario.mode === 'wrong_muted') this.muted = true;
                if (scenario.mode === 'changed_source') this.currentSrc = 'replacement.mp4';
                resolve();
            }, scenario.playDelay));
        },
    });
    let currentTime = 23;
    Object.defineProperty(video, 'currentTime', {
        get: () => currentTime,
        set(value) {
            if (scenario.seekError) throw new Error('seek failed');
            currentTime = value;
            if (!scenario.seekDelay) { seekSettled = true; return; }
            video.seeking = true;
            setTimeout(() => {
                video.seeking = false;
                seekSettled = true;
                video.dispatchEvent(new Event('seeked'));
            }, scenario.seekDelay);
        },
    });
    const state = {
        url: location.href, currentSrc: 'test.mp4', srcAttribute: 'test.mp4',
        duration: 42, durationTolerance: 0.5,
        currentTime: 7.5, paused: scenario.paused, muted: false,
        timeout: scenario.timeout,
    };
    const restore = eval(script);
    let watchdog;
    const started = performance.now();
    try {
        const restored = await Promise.race([
            restore(video, state),
            new Promise((_, reject) => {
                watchdog = setTimeout(() => reject(new Error('restore did not finish')), 2000);
            }),
        ]);
        return {restored, playCalls, playSettled, seekSettled,
            elapsed: performance.now() - started,
            currentTime: video.currentTime, paused: video.paused,
            muted: video.muted, seeking: video.seeking};
    } finally { clearTimeout(watchdog); }
}"""


@pytest.fixture(scope="module")
def run_restore_js():
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as playwright:
        browser = None
        failures = []
        for channel in (None, "chrome"):
            try:
                browser = playwright.chromium.launch(
                    headless=True, channel=channel, timeout=5000,
                )
                break
            except playwright_api.Error as error:
                failures.append(str(error).splitlines()[0])
        if browser is None:
            pytest.skip("无可用的 Chromium 或 Chrome：" + "；".join(failures))
        page = browser.new_page()

        def run(mode="success", *, paused=False, timeout=500, play_delay=60,
                seek_delay=0, seek_error=False):
            return page.evaluate(_RESTORE_SCENARIO_JS, {
                "script": module._VIDEO_RESTORE_JS,
                "scenario": {"mode": mode, "paused": paused, "timeout": timeout,
                             "playDelay": play_delay, "seekDelay": seek_delay,
                             "seekError": seek_error},
            })

        try:
            yield run
        finally:
            browser.close()


def test_restore_waits_for_delayed_play_success(run_restore_js):
    result = run_restore_js(seek_delay=30)
    assert result["restored"] is True
    assert result["seekSettled"] is True
    assert result["playSettled"] is True
    assert result["playCalls"] == 1
    assert result["paused"] is False
    assert result["muted"] is False
    assert result["currentTime"] == 7.5


@pytest.mark.parametrize("mode", ["reject", "throw"])
def test_restore_fails_when_play_rejects_or_throws(run_restore_js, mode):
    result = run_restore_js(mode)
    assert result["restored"] is False
    assert result["playCalls"] == 1


@pytest.mark.parametrize("mode", [
    "still_paused", "wrong_time", "still_seeking", "wrong_muted", "changed_source",
])
def test_restore_rechecks_state_after_play_resolves(run_restore_js, mode):
    result = run_restore_js(mode)
    assert result["playSettled"] is True
    assert result["restored"] is False


def test_restore_keeps_originally_paused_video_paused(run_restore_js):
    result = run_restore_js("throw", paused=True)
    assert result["restored"] is True
    assert result["playCalls"] == 0
    assert result["paused"] is True
    assert result["muted"] is False
    assert result["currentTime"] == 7.5


def test_restore_supports_play_without_promise(run_restore_js):
    result = run_restore_js("no_promise")
    assert result["restored"] is True
    assert result["paused"] is False
    assert result["playCalls"] == 1


def test_pending_play_finishes_within_restore_timeout(run_restore_js):
    result = run_restore_js("pending", timeout=120)
    assert result["restored"] is False
    assert result["playSettled"] is False
    assert result["playCalls"] == 1
    assert 100 <= result["elapsed"] < 1000


@pytest.mark.parametrize("paused", [True, False])
def test_seek_setter_error_restores_mute_without_claiming_success(run_restore_js, paused):
    result = run_restore_js("no_promise", paused=paused, seek_error=True)
    assert result["restored"] is False
    assert result["muted"] is False
    assert result["paused"] is paused
