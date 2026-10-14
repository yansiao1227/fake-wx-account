"""
Browser service - Playwright wrapper managing browser lifecycle and page operations.

All Playwright calls run on a dedicated background thread so that callers from
any worker thread can safely use the service.  An idle-timeout mechanism
automatically shuts down the browser (and its thread) after a configurable
period of inactivity to free resources.
"""

import os
import sys
import json
import uuid
import queue
import threading
import base64
import hashlib
import io
import math
import time
from typing import Optional, Dict, Any, List, Callable

from common.log import logger
from common.utils import expand_path, is_cloud_deployment


_DEFAULT_USER_DATA_DIR = "~/.cow/browser_profile"


# Retain the actual ElementHandle throughout sampling. Re-querying a locator after
# a SPA update could silently capture a different video with the same selector.
_VISIBLE_VIDEO_JS = """() => {
    const visible = v => {
        const r = v.getBoundingClientRect(), s = getComputedStyle(v);
        return v.isConnected && r.width > 0 && r.height > 0 &&
            r.right > 0 && r.bottom > 0 && r.left < innerWidth && r.top < innerHeight &&
            s.display !== 'none' && s.visibility === 'visible' && Number(s.opacity) > 0;
    };
    const videos = Array.from(document.querySelectorAll('video')).filter(visible);
    if (videos.length !== 1) throw new Error('unique_visible_video_required');
    return videos[0];
}"""

_VIDEO_CHECK_JS = """(v, state) => {
    const visible = el => {
        const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        return el.isConnected && r.width > 0 && r.height > 0 &&
            r.right > 0 && r.bottom > 0 && r.left < innerWidth && r.top < innerHeight &&
            s.display !== 'none' && s.visibility === 'visible' && Number(s.opacity) > 0;
    };
    const videos = Array.from(document.querySelectorAll('video')).filter(visible);
    return location.href === state.url && videos.length === 1 && videos[0] === v &&
        v.currentSrc === state.currentSrc && v.getAttribute('src') === state.srcAttribute &&
        Number.isFinite(v.duration) && Math.abs(v.duration - state.duration) <= state.durationTolerance &&
        v.videoWidth === state.width &&
        v.videoHeight === state.height && !v.error;
}"""

_VIDEO_INITIAL_STATE_JS = """(v, url) => {
    if (!v.isConnected || location.href !== url || !Number.isFinite(v.duration) ||
        v.duration <= 0 || v.readyState < 2 || v.videoWidth <= 0 || v.videoHeight <= 0 ||
        v.seekable.length === 0 || v.error) throw new Error('loaded_seekable_video_required');
    const ranges = [];
    for (let i = 0; i < v.seekable.length; ++i) ranges.push([v.seekable.start(i), v.seekable.end(i)]);
    return {url, duration: v.duration, durationTolerance: Math.min(0.5, v.duration * 0.01),
        width: v.videoWidth, height: v.videoHeight,
        currentSrc: v.currentSrc, srcAttribute: v.getAttribute('src'),
        currentTime: v.currentTime, paused: v.paused, muted: v.muted, ranges};
}"""

_VIDEO_SEEK_JS = """(v, args) => new Promise(resolve => {
    let done = false, timer;
    const sameSource = () => v.isConnected && location.href === args.state.url &&
        v.currentSrc === args.state.currentSrc && v.getAttribute('src') === args.state.srcAttribute &&
        Number.isFinite(v.duration) && Math.abs(v.duration - args.state.duration) <= args.state.durationTolerance &&
        v.videoWidth === args.state.width &&
        v.videoHeight === args.state.height;
    const finish = value => {
        if (done) return;
        done = true; clearTimeout(timer); v.removeEventListener('seeked', ready);
        v.removeEventListener('error', failed); resolve(value);
    };
    const failed = () => finish({error: 'video_decode_failed'});
    const ready = () => {
        if (!sameSource()) { finish({error: 'video_source_changed'}); return; }
        if (v.seeking || v.readyState < 2 || Math.abs(v.currentTime - args.time) > 0.25) return;
        // Two animation frames allow the decoded image to reach the compositor.
        requestAnimationFrame(() => requestAnimationFrame(() => {
            if (!sameSource()) finish({error: 'video_source_changed'});
            else if (!v.seeking && v.readyState >= 2 && Math.abs(v.currentTime - args.time) <= 0.25)
                finish({time_seconds: v.currentTime});
        }));
    };
    timer = setTimeout(() => finish({error: 'video_seek_timeout'}), args.timeout);
    v.addEventListener('seeked', ready); v.addEventListener('error', failed);
    v.pause(); v.muted = true;
    try { v.currentTime = args.time; ready(); } catch (_) { finish({error: 'video_not_seekable'}); }
})"""

_VIDEO_CANVAS_JS = """(v, args) => {
    if (v.seeking || v.readyState < 2 || Math.abs(v.currentTime - args.time) > 0.25)
        throw new Error('video_frame_not_ready');
    const scale = Math.min(1, Math.sqrt(args.maxPixels / (v.videoWidth * v.videoHeight)));
    const c = document.createElement('canvas');
    c.width = Math.max(1, Math.floor(v.videoWidth * scale));
    c.height = Math.max(1, Math.floor(v.videoHeight * scale));
    c.getContext('2d').drawImage(v, 0, 0, c.width, c.height);
    return c.toDataURL('image/png');
}"""

_VIDEO_RESTORE_JS = """(v, args) => new Promise(resolve => {
    let restoring = false, finished = false, timer;
    const sameSource = () => v.isConnected && location.href === args.url &&
        v.currentSrc === args.currentSrc && v.getAttribute('src') === args.srcAttribute &&
        Number.isFinite(v.duration) && Math.abs(v.duration - args.duration) <= args.durationTolerance;
    const finish = value => {
        if (finished) return;
        finished = true; clearTimeout(timer);
        v.removeEventListener('seeked', restore); resolve(value);
    };
    const restore = async () => {
        if (restoring || finished) return;
        restoring = true; v.removeEventListener('seeked', restore);
        try {
            if (!sameSource()) { finish(false); return; }
            v.muted = args.muted;
            if (args.paused) v.pause();
            else await v.play();
            finish(sameSource() && Math.abs(v.currentTime - args.currentTime) <= 0.25 &&
                !v.seeking && v.paused === args.paused && v.muted === args.muted);
        } catch (_) {
            finish(false);
        }
    };
    v.pause(); v.muted = true;
    // Bound both seeking and the asynchronous play() result with one timer.
    timer = setTimeout(() => { restore(); finish(false); }, args.timeout);
    v.addEventListener('seeked', restore);
    try { v.currentTime = args.currentTime; if (!v.seeking) restore(); }
    catch (_) { restore(); finish(false); }
})"""

try:
    from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page, Playwright
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False


# ---------------------------------------------------------------------------
# Snapshot DOM helpers
# ---------------------------------------------------------------------------

# Tags that typically carry useful content for an agent
_INTERACTIVE_TAGS = {
    "a", "button", "input", "textarea", "select", "option",
    "label", "details", "summary",
}
_SEMANTIC_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "li", "td", "th", "caption", "figcaption", "blockquote", "pre", "code",
    "nav", "main", "article", "section", "header", "footer", "form", "table",
    "img", "video", "audio",
}
_KEEP_TAGS = _INTERACTIVE_TAGS | _SEMANTIC_TAGS

_SNAPSHOT_JS = """
() => {
    const KEEP = new Set(%s);
    const INTERACTIVE = new Set(%s);
    const SKIP = new Set(["script","style","noscript","svg","path","meta","link","br","hr"]);
    const CLICKABLE_ROLES = new Set([
        "button","link","tab","menuitem","menuitemcheckbox","menuitemradio",
        "option","switch","checkbox","radio","combobox","searchbox","slider",
        "spinbutton","textbox","treeitem"
    ]);
    let refCounter = 0;
    const refMap = {};

    function visible(el) {
        if (!(el instanceof HTMLElement)) return true;
        const st = window.getComputedStyle(el);
        if (st.display === "none" || st.visibility === "hidden") return false;
        if (parseFloat(st.opacity) === 0) return false;
        return true;
    }

    // Strong signals: these attributes alone are enough to mark as interactive
    function hasStrongInteractiveSignal(el) {
        const role = el.getAttribute("role");
        if (role && CLICKABLE_ROLES.has(role)) return true;
        if (el.hasAttribute("onclick") || el.hasAttribute("tabindex")) return true;
        if (el.hasAttribute("data-click") || el.hasAttribute("data-action")) return true;
        if (el.getAttribute("contenteditable") === "true") return true;
        return false;
    }

    // Check if cursor:pointer is set directly (not just inherited from parent)
    function hasOwnPointerCursor(el) {
        try {
            const st = window.getComputedStyle(el);
            if (st.cursor !== "pointer") return false;
            const parent = el.parentElement;
            if (parent) {
                const pst = window.getComputedStyle(parent);
                if (pst.cursor === "pointer") return false;
            }
            return true;
        } catch(e) {}
        return false;
    }

    function hasTextOrContent(el) {
        const t = el.textContent || "";
        if (t.trim().length > 0) return true;
        if (el.querySelector("img,video,audio,canvas")) return true;
        const ariaLabel = el.getAttribute("aria-label");
        if (ariaLabel && ariaLabel.trim()) return true;
        const title = el.getAttribute("title");
        if (title && title.trim()) return true;
        return false;
    }

    function isImplicitInteractive(el) {
        if (hasStrongInteractiveSignal(el)) return true;
        if (hasOwnPointerCursor(el) && hasTextOrContent(el)) return true;
        return false;
    }

    function getTextContent(el) {
        let text = "";
        for (const ch of el.childNodes) {
            if (ch.nodeType === Node.TEXT_NODE) {
                text += ch.textContent;
            }
        }
        return text.trim();
    }

    function walk(node) {
        if (node.nodeType === Node.TEXT_NODE) {
            const t = node.textContent.trim();
            return t ? t : null;
        }
        if (node.nodeType !== Node.ELEMENT_NODE) return null;
        const tag = node.tagName.toLowerCase();
        if (SKIP.has(tag)) return null;
        if (!visible(node)) return null;

        const children = [];
        for (const ch of node.childNodes) {
            const r = walk(ch);
            if (r !== null) {
                if (typeof r === "string") children.push(r);
                else children.push(r);
            }
        }

        const nativeInteractive = INTERACTIVE.has(tag);
        const implicitInteractive = !nativeInteractive && (node instanceof HTMLElement) && isImplicitInteractive(node);
        const keep = KEEP.has(tag) || implicitInteractive;

        if (!keep) {
            if (children.length === 0) return null;
            if (children.length === 1) return children[0];
            return children;
        }

        const obj = { tag };
        if (nativeInteractive || implicitInteractive) {
            refCounter++;
            obj.ref = refCounter;
            refMap[refCounter] = node;
        }

        if (implicitInteractive) {
            const role = node.getAttribute("role");
            if (role) obj.role = role;
            const directText = getTextContent(node);
            if (!directText && children.length === 0) {
                const ariaLabel = node.getAttribute("aria-label");
                const title = node.getAttribute("title");
                if (ariaLabel) obj.ariaLabel = ariaLabel;
                else if (title) obj.ariaLabel = title;
            }
        }

        // Attributes
        if (tag === "a" && node.href) obj.href = node.getAttribute("href");
        if (tag === "img") {
            obj.alt = node.alt || "";
            obj.src = node.getAttribute("src") || "";
        }
        if (tag === "input" || tag === "textarea" || tag === "select") {
            obj.type = node.type || "text";
            obj.name = node.name || undefined;
            obj.value = node.value || undefined;
            obj.placeholder = node.placeholder || undefined;
            if (node.disabled) obj.disabled = true;
            if (tag === "input" && node.type === "checkbox") obj.checked = node.checked;
        }
        if (tag === "button") {
            if (node.disabled) obj.disabled = true;
        }
        if (tag === "option") {
            obj.value = node.value;
            if (node.selected) obj.selected = true;
        }
        if (tag === "label" && node.htmlFor) obj.for = node.htmlFor;

        // Role / aria-label for native interactive & semantic elements
        if (!implicitInteractive) {
            const role = node.getAttribute("role");
            if (role) obj.role = role;
            const ariaLabel = node.getAttribute("aria-label");
            if (ariaLabel) obj.ariaLabel = ariaLabel;
        }

        // Children
        if (children.length === 1 && typeof children[0] === "string") {
            obj.text = children[0];
        } else if (children.length > 0) {
            obj.children = children;
        }

        return obj;
    }

    const result = walk(document.body);
    window.__cowRefMap = refMap;
    return { tree: result, refCount: refCounter };
}
""" % (
    str(list(_KEEP_TAGS)),
    str(list(_INTERACTIVE_TAGS)),
)

# Returning the snapshot as ONE JSON string instead of a nested object is a big
# win in the frozen desktop build: Playwright serializes a nested return value
# node-by-node over many driver<->python protocol round trips, and each round
# trip carries fixed overhead that is dramatically amplified in the frozen
# bundle (a ~300-node tree can take 20s+). JSON.stringify in-page collapses it
# to a single string transfer; Python then json.loads it. Behaviour identical.
_SNAPSHOT_JS_STR = "() => JSON.stringify((%s)())" % _SNAPSHOT_JS.strip()


_BROWSER_DEAD_HINTS = (
    "has been closed",
    "browser has disconnected",
    "target closed",
    "browser closed",
    "context or browser has been closed",
)


def _is_browser_dead_error(err: Exception) -> bool:
    """Return True if *err* indicates the browser / page died out from under us."""
    msg = str(err).lower()
    return any(h in msg for h in _BROWSER_DEAD_HINTS)


def _should_use_headless() -> bool:
    """Decide headless mode: headless on Linux servers without display, headed elsewhere."""
    if sys.platform in ("win32", "darwin"):
        return False
    # Linux: check for display
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return False
    return True


def _flatten_tree(node, indent=0) -> List[str]:
    """Convert snapshot tree to compact text lines for LLM consumption."""
    if node is None:
        return []
    if isinstance(node, str):
        return [" " * indent + node]
    if isinstance(node, list):
        lines = []
        for child in node:
            lines.extend(_flatten_tree(child, indent))
        return lines
    if not isinstance(node, dict):
        return []

    tag = node.get("tag", "?")
    ref = node.get("ref")
    parts = [tag]
    if ref:
        parts[0] = f"[{ref}] {tag}"

    # Inline attributes
    for attr in ("type", "name", "href", "alt", "role", "ariaLabel", "placeholder", "value"):
        val = node.get(attr)
        if val:
            # Truncate long values
            s = str(val)
            if len(s) > 80:
                s = s[:77] + "..."
            parts.append(f'{attr}="{s}"')

    for flag in ("disabled", "checked", "selected"):
        if node.get(flag):
            parts.append(flag)

    prefix = " " * indent
    header = prefix + " ".join(parts)

    text = node.get("text")
    if text:
        # Truncate long text
        if len(text) > 120:
            text = text[:117] + "..."
        header += f": {text}"

    lines = [header]
    children = node.get("children", [])
    for child in children:
        lines.extend(_flatten_tree(child, indent + 2))
    return lines


class BrowserService:
    """Manages a Playwright browser on a dedicated background thread.

    All Playwright operations are dispatched to a single long-lived thread via
    a task queue.  Callers from *any* worker thread can use the public API
    safely.  An idle timer automatically shuts the browser down after
    ``idle_timeout`` seconds of inactivity (default 300 = 5 min).
    """

    _IDLE_TIMEOUT_DEFAULT = 300  # seconds

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self._config = config or {}
        self._headless: Optional[bool] = None
        self._screenshot_dir: Optional[str] = None

        # Background thread state
        self._thread: Optional[threading.Thread] = None
        self._task_queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._activity_lock = threading.RLock()
        self._pending_operations = 0
        self._idle_generation = 0
        self._alive = False
        self._ready = threading.Event()

        # Playwright objects (only accessed on the background thread)
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

        # When we drive a system Chrome/Edge, we spawn it ourselves with a
        # debugging port and attach over CDP (see chrome_launcher). This avoids
        # the macOS Automation prompt + multi-second stall that
        # chromium.launch(channel=...) incurs. Holds the child process owner.
        self._chrome_launcher = None
        # Path to the system browser executable when using system-chrome mode.
        self._system_exe: Optional[str] = None

        # Launch mode: one of "fresh" | "persistent" | "cdp".
        # - cdp: connect to an externally launched Chrome via CDP endpoint.
        # - persistent: launch with launch_persistent_context using a user_data_dir
        #   so cookies / login state survive across runs (default).
        # - fresh: classic launch + new_context, clean state every run.
        #
        # Within persistent/fresh, the actual Chromium binary is resolved by
        # browser_env.resolve_engine(): a system Chrome/Edge (channel-based, zero
        # download) is preferred, falling back to Playwright's own downloaded
        # Chromium. `self._channel` is the Playwright channel ("chrome"/"msedge")
        # when driving a system browser, else None (bundled Chromium).
        cdp_endpoint = self._config.get("cdp_endpoint") or ""
        persistent_flag = self._config.get("persistent", True)
        user_data_dir_cfg = self._config.get("user_data_dir")
        if user_data_dir_cfg is None:
            user_data_dir_cfg = _DEFAULT_USER_DATA_DIR

        self._channel: Optional[str] = None
        self._cdp_endpoint: str = cdp_endpoint.strip() if isinstance(cdp_endpoint, str) else ""
        if self._cdp_endpoint:
            self._launch_mode = "cdp"
            self._user_data_dir: str = ""
        elif persistent_flag and user_data_dir_cfg:
            self._launch_mode = "persistent"
            self._user_data_dir = expand_path(str(user_data_dir_cfg))
        else:
            self._launch_mode = "fresh"
            self._user_data_dir = ""

        # Resolve which browser engine to drive (system Chrome vs downloaded
        # Chromium). Deferred detection failures are surfaced at launch time.
        #
        # For a system Chrome/Edge we DON'T use chromium.launch(channel=...):
        # that "takes over" another app and triggers the macOS Automation
        # prompt + a long stall. Instead we spawn the browser ourselves with a
        # debugging port and attach over CDP (self._launch_mode = "system-cdp").
        # `self._system_exe` is the browser executable; the persistent
        # user_data_dir keeps login state across sessions.
        if self._launch_mode != "cdp":
            try:
                from agent.tools.browser.browser_env import resolve_engine
                engine = resolve_engine(self._config)
                if engine["mode"] == "system-chrome":
                    self._channel = engine["channel"]
                    self._system_exe = engine.get("path")
                    # Only switch to spawn+CDP when we actually know the exe
                    # path (macOS/Windows/Linux detection returns it). Persist
                    # login state in a dedicated profile dir.
                    if self._system_exe:
                        self._launch_mode = "system-cdp"
                        if not self._user_data_dir:
                            self._user_data_dir = expand_path(_DEFAULT_USER_DATA_DIR)
                    logger.info(f"[Browser] Engine resolved: {engine['reason']} "
                                f"(spawn+CDP={bool(self._system_exe)})")
                elif engine["mode"] == "playwright-chromium":
                    logger.info(f"[Browser] Engine resolved: {engine['reason']}")
                else:
                    logger.info(f"[Browser] No ready engine yet: {engine['reason']}")
            except Exception as e:
                logger.debug(f"[Browser] Engine resolution skipped: {e}")

        # Idle auto-release
        idle_cfg = self._config.get("idle_timeout")
        try:
            idle_seconds = float(idle_cfg) if idle_cfg is not None else self._IDLE_TIMEOUT_DEFAULT
            if (isinstance(idle_cfg, bool) or not math.isfinite(idle_seconds) or
                    not 0 < idle_seconds <= threading.TIMEOUT_MAX):
                raise ValueError("idle_timeout must be finite and positive")
        except (TypeError, ValueError, OverflowError):
            idle_seconds = self._IDLE_TIMEOUT_DEFAULT
            logger.warning("[Browser] Invalid idle_timeout; using 300 seconds")
        self._idle_timeout: float = idle_seconds
        self._idle_timer: Optional[threading.Timer] = None

        # Set when the browser / page is detected to have died externally
        # (e.g. user manually closed the window). The next _submit() will then
        # tear down the stale thread and relaunch.
        self._needs_restart = False

    # ------------------------------------------------------------------
    # Background-thread lifecycle
    # ------------------------------------------------------------------

    def _start_thread(self):
        """Start the dedicated Playwright thread if not already running."""
        with self._lock:
            old = self._thread if not self._alive else None
        # Joining under a lifecycle lock can block the old worker's cleanup.
        if old and old.is_alive():
            old.join(timeout=5)
            if old.is_alive():
                raise RuntimeError("Previous browser thread is still shutting down; retry after it exits")
        with self._lock:
            if self._alive and self._thread and self._thread.is_alive():
                ready = self._ready
            else:
                # Another caller may have started or stopped a generation while
                # we waited. Never replace a thread that has not exited.
                if self._thread and self._thread.is_alive():
                    raise RuntimeError("Previous browser thread is still shutting down; retry after it exits")
                self._task_queue = queue.Queue()
                self._alive = True
                self._needs_restart = False
                self._ready = threading.Event()
                ready = self._ready
                self._thread = threading.Thread(target=self._run_loop, daemon=True, name="BrowserThread")
                self._thread.start()
        # Concurrent callers must also wait for this generation's launch.
        if not ready.wait(timeout=30):
            raise TimeoutError("Browser startup timed out (30s)")

    def _run_loop(self):
        """Event loop running on the dedicated thread. Processes tasks until stopped."""
        logger.info("[Browser] Background thread started")
        try:
            self._launch_browser()
        except Exception as e:
            logger.error(f"[Browser] Failed to launch browser: {e}")
            self._alive = False
            self._shutdown_browser()
            self._ready.set()
            self._drain_queue(RuntimeError(f"Browser launch failed: {e}"))
            return
        self._ready.set()

        while self._alive:
            try:
                task = self._task_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if task is None:
                break
            fn, args, kwargs, result_slot = task
            try:
                result_slot["value"] = fn(*args, **kwargs)
            except Exception as e:
                result_slot["error"] = e
                if _is_browser_dead_error(e):
                    self._needs_restart = True
                    logger.warning(
                        f"[Browser] Detected closed page/context ({e}); "
                        "will relaunch on next request."
                    )
            finally:
                self._complete_result_slot(result_slot)

        self._shutdown_browser()
        self._drain_queue(RuntimeError("Browser thread stopped"))
        logger.info("[Browser] Background thread exited")

    def _drain_queue(self, error: Exception):
        """Unblock all callers waiting on the queue with an error."""
        while True:
            try:
                task = self._task_queue.get_nowait()
            except queue.Empty:
                break
            if task is None:
                continue
            _, _, _, result_slot = task
            result_slot["error"] = error
            self._complete_result_slot(result_slot)

    def _complete_result_slot(self, result_slot):
        if result_slot.pop("activity_lease", False):
            self._release_activity()
        result_slot["event"].set()

    def _launch_browser(self):
        """Launch / connect Chromium on the background thread."""
        # Point Playwright at our pinned download dir before any launch so a
        # bundled-Chromium fallback finds the browser downloaded to ~/.cow.
        try:
            from agent.tools.browser.browser_env import apply_browsers_path_env
            apply_browsers_path_env()
        except Exception as e:
            logger.debug(f"[Browser] apply_browsers_path_env skipped: {e}")

        if self._headless is None:
            headless_cfg = self._config.get("headless")
            self._headless = headless_cfg if headless_cfg is not None else _should_use_headless()

        launch_args = [
            "--disable-dev-shm-usage",
            # Trim first-launch overhead: skip the first-run wizard, the default
            # browser prompt, and Chrome's background/component network chatter.
            # These have no effect on page interaction but noticeably speed up
            # cold starts and each navigation.
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-features=Translate,OptimizationHints",
        ]
        if self._headless:
            launch_args.append("--no-sandbox")

        if is_cloud_deployment():
            launch_args.extend([
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-features=site-per-process,TranslateUI,IsolateOrigins",
                "--no-zygote",
                "--js-flags=--max-old-space-size=384",
                "--memory-pressure-off",
            ])

        extra_args = self._config.get("launch_args", [])
        if extra_args:
            launch_args.extend(extra_args)

        viewport_w = self._config.get("viewport_width", 1280)
        viewport_h = self._config.get("viewport_height", 720)
        viewport = {"width": viewport_w, "height": viewport_h}
        user_agent = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )

        self._playwright = sync_playwright().start()

        if self._launch_mode == "cdp":
            self._connect_cdp(viewport)
        elif self._launch_mode == "system-cdp":
            self._launch_system_cdp(launch_args, viewport)
        elif self._launch_mode == "persistent":
            self._launch_persistent(launch_args, viewport, user_agent)
        else:
            self._launch_fresh(launch_args, viewport, user_agent)

        logger.info("[Browser] Browser ready")

    def _launch_fresh(self, launch_args: List[str], viewport: Dict[str, int], user_agent: str):
        """Classic launch: brand new Chromium with an empty context.

        When `self._channel` is set (e.g. "chrome"/"msedge"), Playwright drives
        the user's installed system browser instead of its own Chromium.
        """
        engine_label = f"system:{self._channel}" if self._channel else "chromium"
        logger.info(f"[Browser] Launching {engine_label} (fresh, headless={self._headless})")
        launch_kwargs: Dict[str, Any] = {
            "headless": self._headless,
            "args": launch_args,
        }
        if self._channel:
            launch_kwargs["channel"] = self._channel
        self._browser = self._playwright.chromium.launch(**launch_kwargs)
        self._context = self._browser.new_context(
            viewport=viewport,
            user_agent=user_agent,
        )
        self._page = self._context.new_page()
        self._wire_close_listeners()

    def _launch_persistent(self, launch_args: List[str], viewport: Dict[str, int], user_agent: str):
        """Launch Chromium with a persistent user_data_dir so login state survives."""
        os.makedirs(self._user_data_dir, exist_ok=True)
        engine_label = f"system:{self._channel}" if self._channel else "chromium"
        logger.info(
            f"[Browser] Launching {engine_label} (persistent, headless={self._headless}, "
            f"profile={self._user_data_dir})"
        )
        persistent_kwargs: Dict[str, Any] = {
            "user_data_dir": self._user_data_dir,
            "headless": self._headless,
            "args": launch_args,
            "viewport": viewport,
            "user_agent": user_agent,
        }
        # When driving a system browser, let it use its real UA instead of the
        # spoofed Chromium one (avoids UA/engine mismatch on real Chrome/Edge).
        if self._channel:
            persistent_kwargs["channel"] = self._channel
            persistent_kwargs.pop("user_agent", None)
        try:
            self._context = self._playwright.chromium.launch_persistent_context(**persistent_kwargs)
        except Exception as e:
            # Profile is locked when another Chromium instance already holds it.
            msg = str(e).lower()
            if "singletonlock" in msg or "profile" in msg or "lock" in msg:
                raise RuntimeError(
                    f"Browser profile '{self._user_data_dir}' is in use by another process. "
                    "Close the other Chromium / cow instance, or set a different "
                    "tools.browser.user_data_dir."
                ) from e
            raise

        # Persistent context has no parent Browser handle; reuse the auto-created page.
        self._browser = None
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()
        self._wire_close_listeners()

    def _launch_system_cdp(self, launch_args: List[str], viewport: Dict[str, int]):
        """Spawn the user's system Chrome/Edge with a debugging port, attach via CDP.

        This is the default for system browsers. Unlike launch(channel=...), it
        does not "take over" the browser app, so it avoids the macOS Automation
        prompt / long stall. Login state persists in the isolated user_data_dir.
        """
        from agent.tools.browser.chrome_launcher import ChromeLauncher

        os.makedirs(self._user_data_dir, exist_ok=True)
        logger.info(
            f"[Browser] Launching system:{self._channel} via spawn+CDP "
            f"(headless={self._headless}, profile={self._user_data_dir})"
        )
        self._chrome_launcher = ChromeLauncher(
            executable=self._system_exe,
            user_data_dir=self._user_data_dir,
            extra_args=launch_args,
            headless=self._headless,
        )
        endpoint = self._chrome_launcher.launch()

        self._browser = self._playwright.chromium.connect_over_cdp(endpoint)
        # The spawned Chrome opens its own default context (backed by
        # user_data_dir); reuse it so cookies / logins persist.
        contexts = self._browser.contexts
        self._context = contexts[0] if contexts else self._browser.new_context(viewport=viewport)
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()
        try:
            self._page.set_viewport_size(viewport)
        except Exception:
            pass
        self._wire_close_listeners()

    def _connect_cdp(self, viewport: Dict[str, int]):
        """Attach to an existing Chrome started with --remote-debugging-port."""
        endpoint = self._cdp_endpoint
        logger.info(f"[Browser] Connecting to existing Chrome via CDP: {endpoint}")
        try:
            self._browser = self._playwright.chromium.connect_over_cdp(endpoint)
        except Exception as e:
            msg = str(e).lower()
            if "econnrefused" in msg or "connect" in msg or "refused" in msg:
                raise RuntimeError(
                    f"Cannot reach Chrome at {endpoint}. The CDP browser is not "
                    "running. Ask the user to launch Chrome with "
                    "--remote-debugging-port and --user-data-dir, then retry. "
                    "Do not retry this tool until the user confirms."
                ) from e
            raise

        contexts = self._browser.contexts
        if contexts:
            self._context = contexts[0]
        else:
            self._context = self._browser.new_context(viewport=viewport)

        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()
        self._wire_close_listeners()

    def _wire_close_listeners(self):
        """Mark needs_restart whenever the browser / context / page dies externally."""
        def _on_dead(_obj=None):
            self._needs_restart = True

        try:
            if self._browser:
                self._browser.on("disconnected", _on_dead)
            if self._context:
                self._context.on("close", _on_dead)
            if self._page:
                self._page.on("close", _on_dead)
        except Exception as e:
            logger.debug(f"[Browser] Failed to wire close listeners: {e}")

    def _shutdown_browser(self):
        """Shut down Playwright resources on the background thread.

        Mode-specific behavior:
        - cdp: only disconnect the Playwright client; leave the user's Chrome
          and its tabs untouched (do NOT close the context).
        - persistent: close the persistent context (no separate browser handle).
        - fresh: close context, then browser.
        """
        self._cancel_idle_timer()

        if self._launch_mode == "cdp":
            # For external CDP, browser.close() only detaches the Playwright
            # client; the user's Chrome process and its tabs stay alive.
            try:
                if self._browser:
                    self._browser.close()
            except Exception as e:
                logger.debug(f"[Browser] cdp disconnect error: {e}")
        elif self._launch_mode == "system-cdp":
            # We own the spawned Chrome: detach the CDP client, then kill the
            # process we started so it doesn't linger.
            try:
                if self._browser:
                    self._browser.close()
            except Exception as e:
                logger.debug(f"[Browser] system-cdp disconnect error: {e}")
            try:
                if self._chrome_launcher:
                    self._chrome_launcher.close()
            except Exception as e:
                logger.debug(f"[Browser] chrome launcher close error: {e}")
            self._chrome_launcher = None
        else:
            for obj, label in [
                (self._context, "context"),
                (self._browser, "browser"),
            ]:
                try:
                    if obj:
                        obj.close()
                except Exception as e:
                    logger.debug(f"[Browser] {label} close error: {e}")

        try:
            if self._playwright:
                self._playwright.stop()
        except Exception as e:
            logger.debug(f"[Browser] playwright stop error: {e}")
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        logger.info("[Browser] Browser closed")

    def _submit(self, fn: Callable, *args, **kwargs):
        """Submit *fn* to the background thread and block until it completes."""
        self._acquire_activity()
        submitted = False
        try:
            if self._needs_restart:
                logger.info("[Browser] Restarting after detecting closed browser")
                self.close()
            self._start_thread()
            result_slot: Dict[str, Any] = {"event": threading.Event(), "activity_lease": True}
            with self._lock:
                if not self._alive:
                    raise RuntimeError("Browser is not available")
                self._task_queue.put((fn, args, kwargs, result_slot))
                submitted = True
            # A caller timeout does not end its actual queued/running operation.
            # The worker retains the lease until it completes or drains the task.
            if not result_slot["event"].wait(timeout=120):
                raise TimeoutError("Browser operation timed out (120s)")
            if "error" in result_slot:
                raise result_slot["error"]
            return result_slot.get("value")
        finally:
            if not submitted:
                self._release_activity()

    # ------------------------------------------------------------------
    # Idle auto-release
    # ------------------------------------------------------------------

    def _acquire_activity(self):
        with self._activity_lock:
            self._pending_operations += 1
            self._cancel_idle_timer()

    def _release_activity(self):
        with self._activity_lock:
            self._pending_operations -= 1
            if self._pending_operations == 0 and self._alive:
                self._reset_idle_timer()

    def _reset_idle_timer(self):
        with self._activity_lock:
            self._cancel_idle_timer()
            if self._pending_operations or not self._alive:
                return
            generation = self._idle_generation
            self._idle_timer = threading.Timer(
                self._idle_timeout, self._on_idle_timeout, args=(generation,)
            )
            self._idle_timer.daemon = True
            self._idle_timer.start()

    def _cancel_idle_timer(self):
        with self._activity_lock:
            self._idle_generation += 1
            if self._idle_timer:
                self._idle_timer.cancel()
                self._idle_timer = None

    def _on_idle_timeout(self, generation):
        self.close(_idle_generation=generation)

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def close(self, *, _idle_generation=None):
        """Shut down browser and background thread (safe from any thread)."""
        # Claim idle shutdown atomically with activity validation. An already
        # running old Timer callback cannot close a browser that was reused.
        with self._activity_lock:
            if _idle_generation is not None and (
                _idle_generation != self._idle_generation or self._pending_operations
            ):
                return
            self._cancel_idle_timer()
            with self._lock:
                if not self._alive:
                    if self._thread is None or not self._thread.is_alive():
                        self._needs_restart = False
                    return
                self._alive = False
                t = self._thread
                task_queue = self._task_queue
        if _idle_generation is not None:
            logger.info(f"[Browser] Idle for {self._idle_timeout}s, auto-releasing browser")
        if task_queue is not None:
            task_queue.put(None)
        if t is not None and t.is_alive():
            t.join(timeout=10)
        with self._lock:
            if self._thread is t and (t is None or not t.is_alive()):
                self._thread = None
                self._needs_restart = False

    # ------------------------------------------------------------------
    # Actions  (each method is dispatched to the background thread)
    # ------------------------------------------------------------------

    def navigate(self, url: str, timeout: int = 30000) -> Dict[str, Any]:
        return self._submit(self._do_navigate, url, timeout)

    def _do_navigate(self, url: str, timeout: int) -> Dict[str, Any]:
        page = self._page
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            status = resp.status if resp else None
        except Exception as e:
            return {"error": f"Navigation failed: {e}"}

        # SPAs keep long-lived connections (websockets, polling, analytics) and
        # rarely reach true "networkidle", so waiting the full timeout is wasted
        # time. domcontentloaded already gives a usable DOM; give the page a
        # short grace period for initial render/XHR, then proceed.
        try:
            page.wait_for_load_state("networkidle", timeout=1500)
        except Exception:
            pass
        page.wait_for_timeout(300)

        try:
            title = page.title()
        except Exception:
            title = ""
        try:
            current_url = page.url
        except Exception:
            current_url = url

        return {"url": current_url, "title": title, "status": status}

    def snapshot(self, selector: Optional[str] = None) -> str:
        return self._submit(self._do_snapshot, selector)

    def _do_snapshot(self, selector: Optional[str] = None) -> str:
        page = self._page
        try:
            # Return a single JSON string (not a nested object) to avoid
            # Playwright's per-node serialization round trips, which are slow
            # in the frozen build. See _SNAPSHOT_JS_STR.
            raw = page.evaluate(_SNAPSHOT_JS_STR)
            result = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as e:
            return f"[Snapshot error: {e}]"

        tree = result.get("tree")
        ref_count = result.get("refCount", 0)
        lines = _flatten_tree(tree)

        try:
            title = page.title()
        except Exception:
            title = ""
        try:
            url = page.url
        except Exception:
            url = ""

        header = f"Page: {title}  ({url})\nInteractive elements: {ref_count}\n---"
        body = "\n".join(lines)

        max_chars = self._config.get("snapshot_max_chars", 30000)
        if len(body) > max_chars:
            body = body[:max_chars] + "\n... [snapshot truncated]"

        return f"{header}\n{body}"

    def screenshot(self, full_page: bool = False, cwd: str = "") -> str:
        return self._submit(self._do_screenshot, full_page, cwd)

    def _do_screenshot(self, full_page: bool = False, cwd: str = "") -> str:
        page = self._page
        save_dir = self._get_screenshot_dir(cwd)
        filename = f"screenshot_{uuid.uuid4().hex[:8]}.png"
        filepath = os.path.join(save_dir, filename)
        page.screenshot(path=filepath, full_page=full_page)
        logger.info(f"[Browser] Screenshot saved: {filepath}")
        return filepath

    def click(self, ref: Optional[int] = None, selector: Optional[str] = None,
              timeout: int = 5000) -> Dict[str, Any]:
        return self._submit(self._do_click, ref, selector, timeout)

    def _do_click(self, ref, selector, timeout) -> Dict[str, Any]:
        page = self._page
        try:
            if ref is not None:
                result = page.evaluate(f"""
                    () => {{
                        const el = window.__cowRefMap && window.__cowRefMap[{ref}];
                        if (!el) return {{ error: "ref {ref} not found. Run snapshot first." }};
                        el.click();
                        return {{ clicked: true, tag: el.tagName.toLowerCase() }};
                    }}
                """)
                if result.get("error"):
                    return result
                page.wait_for_timeout(500)
                return result
            elif selector:
                page.click(selector, timeout=timeout)
                return {"clicked": True, "selector": selector}
            else:
                return {"error": "Provide either ref (from snapshot) or selector"}
        except Exception as e:
            return {"error": f"Click failed: {e}"}

    def fill(self, text: str, ref: Optional[int] = None,
             selector: Optional[str] = None, timeout: int = 5000) -> Dict[str, Any]:
        return self._submit(self._do_fill, text, ref, selector, timeout)

    def _do_fill(self, text, ref, selector, timeout) -> Dict[str, Any]:
        page = self._page
        try:
            if ref is not None:
                result = page.evaluate(f"""
                    () => {{
                        const el = window.__cowRefMap && window.__cowRefMap[{ref}];
                        if (!el) return {{ error: "ref {ref} not found. Run snapshot first." }};
                        el.focus();
                        el.value = "";
                        return {{ tag: el.tagName.toLowerCase(), name: el.name || "" }};
                    }}
                """)
                if result.get("error"):
                    return result
                page.keyboard.type(text)
                return {"filled": True, "ref": ref, "text": text}
            elif selector:
                page.fill(selector, text, timeout=timeout)
                return {"filled": True, "selector": selector, "text": text}
            else:
                return {"error": "Provide either ref (from snapshot) or selector"}
        except Exception as e:
            return {"error": f"Fill failed: {e}"}

    def select(self, value: str, ref: Optional[int] = None,
               selector: Optional[str] = None, timeout: int = 5000) -> Dict[str, Any]:
        return self._submit(self._do_select, value, ref, selector, timeout)

    def _do_select(self, value, ref, selector, timeout) -> Dict[str, Any]:
        page = self._page
        try:
            if ref is not None:
                result = page.evaluate(f"""
                    () => {{
                        const el = window.__cowRefMap && window.__cowRefMap[{ref}];
                        if (!el || el.tagName.toLowerCase() !== "select")
                            return {{ error: "ref {ref} is not a <select> element" }};
                        el.value = {repr(value)};
                        el.dispatchEvent(new Event("change", {{ bubbles: true }}));
                        return {{ selected: true, value: el.value }};
                    }}
                """)
                return result
            elif selector:
                page.select_option(selector, value, timeout=timeout)
                return {"selected": True, "selector": selector, "value": value}
            else:
                return {"error": "Provide either ref (from snapshot) or selector"}
        except Exception as e:
            return {"error": f"Select failed: {e}"}

    def scroll(self, direction: str = "down", amount: int = 500) -> Dict[str, Any]:
        return self._submit(self._do_scroll, direction, amount)

    def _do_scroll(self, direction, amount) -> Dict[str, Any]:
        page = self._page
        delta_map = {
            "down": (0, amount),
            "up": (0, -amount),
            "right": (amount, 0),
            "left": (-amount, 0),
        }
        dx, dy = delta_map.get(direction, (0, amount))
        try:
            page.mouse.wheel(dx, dy)
            page.wait_for_timeout(300)
            scroll_info = page.evaluate("""
                () => ({
                    scrollX: window.scrollX,
                    scrollY: window.scrollY,
                    scrollHeight: document.documentElement.scrollHeight,
                    clientHeight: document.documentElement.clientHeight
                })
            """)
            return {"scrolled": direction, "amount": amount, **scroll_info}
        except Exception as e:
            return {"error": f"Scroll failed: {e}"}

    def wait(self, selector: Optional[str] = None, timeout: int = 5000,
             state: str = "visible") -> Dict[str, Any]:
        return self._submit(self._do_wait, selector, timeout, state)

    def _do_wait(self, selector, timeout, state) -> Dict[str, Any]:
        page = self._page
        try:
            if selector:
                page.wait_for_selector(selector, timeout=timeout, state=state)
                return {"waited": True, "selector": selector, "state": state}
            else:
                page.wait_for_timeout(timeout)
                return {"waited": True, "timeout_ms": timeout}
        except Exception as e:
            return {"error": f"Wait failed: {e}"}

    def go_back(self) -> Dict[str, Any]:
        return self._submit(self._do_go_back)

    def _do_go_back(self) -> Dict[str, Any]:
        page = self._page
        try:
            page.go_back(wait_until="domcontentloaded", timeout=10000)
            try:
                title = page.title()
            except Exception:
                title = ""
            try:
                url = page.url
            except Exception:
                url = ""
            return {"url": url, "title": title}
        except Exception as e:
            return {"error": f"Go back failed: {e}"}

    def go_forward(self) -> Dict[str, Any]:
        return self._submit(self._do_go_forward)

    def _do_go_forward(self) -> Dict[str, Any]:
        page = self._page
        try:
            page.go_forward(wait_until="domcontentloaded", timeout=10000)
            try:
                title = page.title()
            except Exception:
                title = ""
            try:
                url = page.url
            except Exception:
                url = ""
            return {"url": url, "title": title}
        except Exception as e:
            return {"error": f"Go forward failed: {e}"}

    def get_text(self, selector: str) -> Dict[str, Any]:
        return self._submit(self._do_get_text, selector)

    def _do_get_text(self, selector) -> Dict[str, Any]:
        page = self._page
        try:
            text = page.text_content(selector, timeout=5000)
            return {"text": text or ""}
        except Exception as e:
            return {"error": f"Get text failed: {e}"}

    def evaluate(self, script: str) -> Dict[str, Any]:
        return self._submit(self._do_evaluate, script)

    def _do_evaluate(self, script) -> Dict[str, Any]:
        page = self._page
        try:
            result = page.evaluate(script)
            return {"result": result}
        except Exception as e:
            return {"error": f"Evaluate failed: {e}"}

    def video_frames(self, max_frames: int = 6, timeout: int = 20000,
                     cwd: str = "") -> Dict[str, Any]:
        """Sample the current page's unique video without navigating or reading audio."""
        if isinstance(max_frames, bool) or not isinstance(max_frames, int) or not 1 <= max_frames <= 8:
            return {"error": "video_frames 的 max_frames 必须是 1..8 的整数",
                    "reading_status": "unavailable", "frames": [], "audio_read": False}
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            return {"error": "video_frames 的 timeout 必须是正整数毫秒",
                    "reading_status": "unavailable", "frames": [], "audio_read": False}
        return self._submit(self._do_video_frames, max_frames, min(timeout, 20000), cwd)

    def _do_video_frames(self, max_frames: int, timeout: int, cwd: str) -> Dict[str, Any]:
        page = self._page
        started = time.monotonic()
        deadline = started + timeout / 1000
        # Leave time to restore the original position and playback state.
        sampling_deadline = deadline - min(1.0, timeout / 5000)
        frames: List[Dict[str, Any]] = []
        frame_paths: List[str] = []
        hashes = set()
        state = None
        video = None
        modified = False
        limitation = "仅抽取部分时间点的画面，音轨未读取"
        identity_valid = True
        result: Dict[str, Any] = {"reading_status": "unavailable", "frames": frames,
                                 "audio_read": False, "limitation": limitation}

        def remaining_ms(end=sampling_deadline):
            return max(0, int((end - time.monotonic()) * 1000))

        def check_identity():
            try:
                return self._page is page and page.url == state["url"] and bool(
                    video.evaluate(_VIDEO_CHECK_JS, state)
                )
            except Exception:
                return False

        try:
            url = page.url
            handle = page.evaluate_handle(_VISIBLE_VIDEO_JS)
            video = handle.as_element()
            if video is None:
                raise ValueError("unique_visible_video_required")
            state = video.evaluate(_VIDEO_INITIAL_STATE_JS, url)
            duration = float(state["duration"])
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("finite_video_duration_required")
            if not check_identity():
                identity_valid = False
                raise ValueError("video_source_changed")
            result["duration_seconds"] = duration
            # Seeking to the exact duration often displays a poster/end card.
            # The last sample is the final decodable instant before the end.
            last = max(0.0, duration - min(0.5, duration / 10))
            targets = [0.0] if max_frames == 1 else [last * i / (max_frames - 1) for i in range(max_frames)]
            if any(not any(float(a) <= target <= float(b) for a, b in state["ranges"])
                   for target in targets):
                raise ValueError("video_sample_range_not_seekable")
            save_dir = os.path.abspath(os.path.join(cwd or os.getcwd(), "tmp"))
            os.makedirs(save_dir, exist_ok=True)
            prefix = f"video_frame_{uuid.uuid4().hex}"
            for target in targets:
                if remaining_ms() <= 0:
                    limitation += "；达到总时间预算"
                    break
                if not check_identity():
                    identity_valid = False
                    raise ValueError("video_source_changed")
                modified = True
                seek_result = video.evaluate(_VIDEO_SEEK_JS, {
                    "state": state, "time": target, "timeout": max(1, min(2500, remaining_ms()))
                })
                if seek_result.get("error"):
                    if seek_result["error"] == "video_source_changed":
                        identity_valid = False
                        raise ValueError("video_source_changed")
                    limitation += f"；{seek_result['error']}"
                    break
                actual_time = float(seek_result["time_seconds"])
                if not math.isfinite(actual_time) or abs(actual_time - target) > 0.25:
                    raise ValueError("video_frame_timestamp_mismatch")
                if not check_identity():
                    identity_valid = False
                    raise ValueError("video_source_changed")
                if remaining_ms() <= 0:
                    limitation += "；达到总时间预算"
                    break
                try:
                    data_url = video.evaluate(_VIDEO_CANVAS_JS, {"time": actual_time, "maxPixels": 1500000})
                    if not isinstance(data_url, str) or not data_url.startswith("data:image/png;base64,"):
                        raise ValueError("video_canvas_format_invalid")
                    png_bytes = base64.b64decode(data_url.split(",", 1)[1], validate=True)
                    strategy = "video_canvas"
                except Exception as exc:
                    # A cross-origin video taints canvas. Screenshot the retained
                    # video element only; never substitute a page screenshot.
                    if not any(marker in str(exc).lower() for marker in ("taint", "securityerror", "cross-origin", "insecure")):
                        raise
                    if not check_identity():
                        identity_valid = False
                        raise ValueError("video_source_changed")
                    if remaining_ms() <= 0:
                        limitation += "；达到总时间预算"
                        break
                    png_bytes = video.screenshot(type="png", scale="css", timeout=max(1, remaining_ms()))
                    strategy = "video_element_screenshot"
                if not check_identity():
                    identity_valid = False
                    raise ValueError("video_source_changed")
                # Verify timestamps again after capture: a site's autoplay hook
                # must not advance the frame while its image is being obtained.
                frame_state = video.evaluate("v => ({time: v.currentTime, ready: v.readyState, seeking: v.seeking})")
                if frame_state["ready"] < 2 or frame_state["seeking"] or abs(float(frame_state["time"]) - actual_time) > 0.25:
                    raise ValueError("video_frame_changed_during_capture")
                png_bytes, digest = self._bounded_video_png(png_bytes)
                if digest in hashes:
                    continue
                hashes.add(digest)
                filepath = os.path.abspath(os.path.join(save_dir, f"{prefix}_{len(frames)}.png"))
                with open(filepath, "wb") as output:
                    output.write(png_bytes)
                frame_paths.append(filepath)
                frames.append({"path": filepath, "time_seconds": actual_time, "strategy": strategy})
            if not check_identity():
                identity_valid = False
                raise ValueError("video_source_changed")
            if frames:
                result["reading_status"] = "partial"
            else:
                result["error"] = "没有取得可校验的视频画面；请报告未读，勿循环重试"
        except Exception as exc:
            error = str(exc)
            # A changed source invalidates the entire result, including frames
            # captured before a navigation: callers must not mix two pages.
            if not identity_valid or error == "video_source_changed":
                for filepath in frame_paths:
                    try:
                        os.remove(filepath)
                    except OSError:
                        pass
                frames.clear()
                result["error"] = "视频页面或来源发生变化，未返回画面；勿循环重试"
            elif frames:
                result["reading_status"] = "partial"
                limitation += f"；抽帧中断：{error}"
            else:
                result["error"] = f"视频抽帧不可用：{error}；请报告未读，勿循环重试"
        finally:
            if state is not None and video is not None:
                try:
                    if check_identity() and modified:
                        result["restored"] = bool(video.evaluate(_VIDEO_RESTORE_JS, {
                            **state, "timeout": max(1, remaining_ms(deadline))
                        }))
                        if not check_identity():
                            identity_valid = False
                    elif not check_identity():
                        identity_valid = False
                        result["restored"] = False
                    else:
                        result["restored"] = True
                except Exception:
                    result["restored"] = False
                if not identity_valid:
                    for filepath in frame_paths:
                        try:
                            os.remove(filepath)
                        except OSError:
                            pass
                    frames.clear()
                    result["reading_status"] = "unavailable"
                    result["error"] = "视频页面或来源发生变化，未返回画面；勿循环重试"
                if result.get("restored") is False:
                    limitation += "；未能确认播放位置和播放状态恢复"
            if video is not None:
                try:
                    video.dispose()
                except Exception:
                    pass
            result["limitation"] = limitation
        return result

    @staticmethod
    def _bounded_video_png(data: bytes):
        from PIL import Image
        if len(data) > 12000000:
            raise ValueError("video_frame_bytes_limit")
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("video_frame_format_invalid")
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG" or image.width * image.height > 40000000:
                raise ValueError("video_frame_pixels_limit")
            image.load()
            image = image.convert("RGB")
            if image.width * image.height > 1500000:
                factor = math.sqrt(1500000 / (image.width * image.height))
                image = image.resize((max(1, int(image.width * factor)), max(1, int(image.height * factor))))
            digest = hashlib.sha256(image.tobytes()).hexdigest()
            output = io.BytesIO()
            image.save(output, format="PNG")
            return output.getvalue(), digest

    def press(self, key: str) -> Dict[str, Any]:
        return self._submit(self._do_press, key)

    def _do_press(self, key) -> Dict[str, Any]:
        page = self._page
        try:
            page.keyboard.press(key)
            page.wait_for_timeout(300)
            return {"pressed": key}
        except Exception as e:
            return {"error": f"Press failed: {e}"}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_screenshot_dir(self, cwd: str = "") -> str:
        if self._screenshot_dir and os.path.isdir(self._screenshot_dir):
            return self._screenshot_dir
        base = cwd or os.getcwd()
        d = os.path.join(base, "tmp")
        os.makedirs(d, exist_ok=True)
        self._screenshot_dir = d
        return d
