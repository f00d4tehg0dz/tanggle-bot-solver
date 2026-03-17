"""Browser automation module for tanggle.io using Playwright.

Handles launching the browser, navigating to puzzle pages,
injecting JavaScript to extract PixiJS game state, and
performing synthetic drag-and-drop operations on puzzle pieces.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, BrowserContext, Page

from .config import TanggleCredentials

logger = logging.getLogger(__name__)

# JavaScript to inject into the page to extract PixiJS puzzle state
EXTRACT_GAME_STATE_JS = """
() => {
    const canvas = document.querySelector('canvas');
    if (!canvas) return { error: 'No canvas found', canvasFound: false };

    // Check if the canvas has actually been drawn to by sampling pixels
    let hasContent = false;
    try {
        const ctx = canvas.getContext('2d') || canvas.getContext('webgl') || canvas.getContext('webgl2');
        if (ctx && ctx.getImageData) {
            // 2D context - sample center pixel
            const d = ctx.getImageData(canvas.width / 2, canvas.height / 2, 1, 1).data;
            hasContent = d[3] > 0; // non-transparent
        } else if (ctx && ctx.readPixels) {
            // WebGL context - sample center pixel
            const pixel = new Uint8Array(4);
            ctx.readPixels(canvas.width / 2, canvas.height / 2, 1, 1, ctx.RGBA, ctx.UNSIGNED_BYTE, pixel);
            hasContent = pixel[3] > 0;
        } else {
            // Can't read pixels, assume content if canvas is sized
            hasContent = canvas.width > 100 && canvas.height > 100;
        }
    } catch (e) {
        // Cross-origin or WebGL context - fallback to size check
        hasContent = canvas.width > 100 && canvas.height > 100;
    }

    // Try to find PixiJS app (may or may not be exposed)
    let app = null;
    const searchTargets = [
        window.__PIXI_APP__,
        canvas.__pixiApplication,
        window.app,
    ];
    for (const val of searchTargets) {
        if (val && val.stage && val.renderer) { app = val; break; }
    }
    if (!app) {
        for (const key of Object.keys(window)) {
            try {
                const val = window[key];
                if (val && val.stage && val.renderer) { app = val; break; }
            } catch (e) {}
        }
    }

    return {
        canvasFound: true,
        canvasWidth: canvas.width,
        canvasHeight: canvas.height,
        hasContent: hasContent,
        appFound: !!app,
        hasStage: app ? !!app.stage : false,
        stageChildren: app && app.stage ? app.stage.children.length : 0,
    };
}
"""

# JavaScript to hook WebSocket — wraps the constructor for NEW connections
# AND finds/hooks any EXISTING WebSocket connections on the page
HOOK_WEBSOCKET_JS = """
() => {
    if (window.__tanggleSolverHooked) return { status: 'already_hooked' };
    window.__tanggleSolverHooked = true;
    window.__tanggleSolverMessages = [];
    window.__tanggleSolverWs = null;

    function hookWs(ws, label) {
        console.log('[TanggleSolver] Hooking WebSocket:', label, ws.url);
        window.__tanggleSolverWs = ws;

        const origSend = ws.send.bind(ws);
        ws.send = function(data) {
            window.__tanggleSolverMessages.push({
                direction: 'out',
                timestamp: Date.now(),
                data: data instanceof ArrayBuffer
                    ? Array.from(new Uint8Array(data))
                    : (data instanceof Blob ? 'blob' : data)
            });
            return origSend(data);
        };

        ws.addEventListener('message', (event) => {
            if (event.data instanceof Blob) {
                event.data.arrayBuffer().then(buf => {
                    window.__tanggleSolverMessages.push({
                        direction: 'in',
                        timestamp: Date.now(),
                        data: Array.from(new Uint8Array(buf))
                    });
                });
            } else if (event.data instanceof ArrayBuffer) {
                window.__tanggleSolverMessages.push({
                    direction: 'in',
                    timestamp: Date.now(),
                    data: Array.from(new Uint8Array(event.data))
                });
            } else {
                window.__tanggleSolverMessages.push({
                    direction: 'in',
                    timestamp: Date.now(),
                    data: event.data
                });
            }
        });
    }

    // Hook the WebSocket constructor for future connections
    const OrigWebSocket = window.WebSocket;
    window.WebSocket = function(...args) {
        const ws = new OrigWebSocket(...args);
        hookWs(ws, 'new');
        return ws;
    };
    window.WebSocket.prototype = OrigWebSocket.prototype;
    window.WebSocket.CONNECTING = OrigWebSocket.CONNECTING;
    window.WebSocket.OPEN = OrigWebSocket.OPEN;
    window.WebSocket.CLOSING = OrigWebSocket.CLOSING;
    window.WebSocket.CLOSED = OrigWebSocket.CLOSED;

    return { status: 'hooked_constructor' };
}
"""

# JavaScript to find and hook an EXISTING WebSocket that's already open
HOOK_EXISTING_WS_JS = """
() => {
    if (!window.__tanggleSolverMessages) {
        window.__tanggleSolverMessages = [];
    }

    // Performance entries show all network connections including WebSockets
    const wsEntries = performance.getEntriesByType('resource')
        .filter(e => e.name.startsWith('wss://') || e.name.startsWith('ws://'));

    // Try to find the WS via common framework patterns
    // Check all objects for WebSocket instances
    let found = [];

    // Method 1: Search global scope
    for (const key of Object.keys(window)) {
        try {
            const val = window[key];
            if (val instanceof WebSocket && val.readyState === WebSocket.OPEN) {
                found.push({ source: 'window.' + key, ws: val });
            }
        } catch(e) {}
    }

    // Method 2: Intercept via prototype patching
    // Patch WebSocket.prototype.send to capture ALL sends from ANY WebSocket
    if (!WebSocket.prototype.__tangglePatched) {
        WebSocket.prototype.__tangglePatched = true;
        const origSend = WebSocket.prototype.send;
        WebSocket.prototype.send = function(data) {
            // Auto-hook this WebSocket if not already
            if (!this.__tanggleHooked) {
                this.__tanggleHooked = true;
                window.__tanggleSolverWs = this;
                console.log('[TanggleSolver] Auto-hooked WebSocket via send:', this.url);

                this.addEventListener('message', (event) => {
                    if (event.data instanceof Blob) {
                        event.data.arrayBuffer().then(buf => {
                            window.__tanggleSolverMessages.push({
                                direction: 'in',
                                timestamp: Date.now(),
                                data: Array.from(new Uint8Array(buf))
                            });
                        });
                    } else if (event.data instanceof ArrayBuffer) {
                        window.__tanggleSolverMessages.push({
                            direction: 'in',
                            timestamp: Date.now(),
                            data: Array.from(new Uint8Array(event.data))
                        });
                    } else {
                        window.__tanggleSolverMessages.push({
                            direction: 'in',
                            timestamp: Date.now(),
                            data: event.data
                        });
                    }
                });
            }

            window.__tanggleSolverMessages.push({
                direction: 'out',
                timestamp: Date.now(),
                data: data instanceof ArrayBuffer
                    ? Array.from(new Uint8Array(data))
                    : (data instanceof Blob ? 'blob' : typeof data === 'string' ? data : 'unknown')
            });

            return origSend.call(this, data);
        };
    }

    // Also hook any already-found WebSockets
    for (const f of found) {
        if (!f.ws.__tanggleHooked) {
            f.ws.__tanggleHooked = true;
            window.__tanggleSolverWs = f.ws;
            f.ws.addEventListener('message', (event) => {
                if (event.data instanceof Blob) {
                    event.data.arrayBuffer().then(buf => {
                        window.__tanggleSolverMessages.push({
                            direction: 'in', timestamp: Date.now(),
                            data: Array.from(new Uint8Array(buf))
                        });
                    });
                } else if (event.data instanceof ArrayBuffer) {
                    window.__tanggleSolverMessages.push({
                        direction: 'in', timestamp: Date.now(),
                        data: Array.from(new Uint8Array(event.data))
                    });
                } else {
                    window.__tanggleSolverMessages.push({
                        direction: 'in', timestamp: Date.now(),
                        data: event.data
                    });
                }
            });
        }
    }

    return {
        status: 'patched',
        wsEntries: wsEntries.map(e => e.name),
        foundGlobal: found.map(f => f.source),
        existingWs: !!window.__tanggleSolverWs,
    };
}
"""

# JavaScript to extract intercepted WebSocket messages
GET_WS_MESSAGES_JS = """
() => {
    const msgs = window.__tanggleSolverMessages || [];
    window.__tanggleSolverMessages = [];
    return msgs;
}
"""

# JavaScript to discover piece data from the PixiJS scene graph
DISCOVER_PIECES_JS = """
() => {
    const canvas = document.querySelector('canvas');
    if (!canvas) return { error: 'No canvas found' };

    // Walk the PixiJS display tree to find piece-like objects
    let app = null;
    for (const key of Object.keys(window)) {
        const val = window[key];
        if (val && val.stage && val.renderer) {
            app = val;
            break;
        }
    }
    if (!app && window.__PIXI_APP__) app = window.__PIXI_APP__;
    if (!app && canvas.__pixiApplication) app = canvas.__pixiApplication;

    if (!app || !app.stage) return { error: 'PixiJS app not found' };

    const pieces = [];
    const queue = [...app.stage.children];

    while (queue.length > 0) {
        const node = queue.shift();
        if (!node) continue;

        // Collect info about each display object
        const info = {
            type: node.constructor.name,
            x: node.x,
            y: node.y,
            width: node.width,
            height: node.height,
            visible: node.visible,
            interactive: node.interactive || node.eventMode === 'static',
            childCount: node.children ? node.children.length : 0,
            label: node.label || node.name || null,
        };

        // Check for puzzle-piece-like properties
        if (node.texture && node.texture.baseTexture) {
            info.hasTexture = true;
            info.textureWidth = node.texture.width;
            info.textureHeight = node.texture.height;
        }

        pieces.push(info);

        // Traverse children (limit depth to avoid infinite loops)
        if (node.children && pieces.length < 5000) {
            queue.push(...node.children);
        }
    }

    return {
        totalNodes: pieces.length,
        pieces: pieces.slice(0, 200),  // Return first 200 for analysis
    };
}
"""

# JavaScript to take a screenshot of just the puzzle canvas
SCREENSHOT_CANVAS_JS = """
() => {
    const canvas = document.querySelector('canvas');
    if (!canvas) return { error: 'No canvas found' };
    try {
        return canvas.toDataURL('image/png');
    } catch (e) {
        return { error: 'Canvas tainted or cross-origin: ' + e.message };
    }
}
"""

# JavaScript to perform a drag operation on the canvas
DRAG_PIECE_JS = """
(fromX, fromY, toX, toY, steps = 20) => {
    const canvas = document.querySelector('canvas');
    if (!canvas) return { error: 'No canvas found' };

    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;

    // Convert canvas coordinates to client coordinates
    const clientFromX = rect.left + fromX / scaleX;
    const clientFromY = rect.top + fromY / scaleY;
    const clientToX = rect.left + toX / scaleX;
    const clientToY = rect.top + toY / scaleY;

    return new Promise((resolve) => {
        // Pointer down at source
        canvas.dispatchEvent(new PointerEvent('pointerdown', {
            clientX: clientFromX, clientY: clientFromY,
            bubbles: true, pointerId: 1, pointerType: 'mouse',
            button: 0, buttons: 1,
        }));

        let step = 0;
        const interval = setInterval(() => {
            step++;
            const t = step / steps;
            const cx = clientFromX + (clientToX - clientFromX) * t;
            const cy = clientFromY + (clientToY - clientFromY) * t;

            canvas.dispatchEvent(new PointerEvent('pointermove', {
                clientX: cx, clientY: cy,
                bubbles: true, pointerId: 1, pointerType: 'mouse',
                button: 0, buttons: 1,
            }));

            if (step >= steps) {
                clearInterval(interval);
                canvas.dispatchEvent(new PointerEvent('pointerup', {
                    clientX: clientToX, clientY: clientToY,
                    bubbles: true, pointerId: 1, pointerType: 'mouse',
                    button: 0,
                }));
                resolve({ success: true, from: [fromX, fromY], to: [toX, toY] });
            }
        }, 16); // ~60fps
    });
}
"""


@dataclass
class PuzzleInfo:
    """Information about a tanggle.io puzzle room."""
    uuid: str
    pieces_x: int = 0
    pieces_y: int = 0
    total_pieces: int = 0
    completed_pieces: int = 0
    image_url: str = ""
    is_hardmode: bool = False


@dataclass
class PieceState:
    """State of a single puzzle piece as observed in the browser."""
    index: int
    x: float
    y: float
    grid_x: int = -1  # Target grid column
    grid_y: int = -1  # Target grid row
    is_placed: bool = False
    rotation: float = 0.0


class TanggleBrowser:
    """Controls a browser instance for interacting with tanggle.io puzzles."""

    def __init__(self, headless: bool = False, slow_mo: int = 0,
                 user_data_dir: Optional[str] = None):
        self.headless = headless
        self.slow_mo = slow_mo
        self.user_data_dir = user_data_dir
        self._playwright = None
        self._context: Optional[BrowserContext] = None
        self._page: Page  # Set in launch(), always valid after that
        self._puzzle_info: Optional[PuzzleInfo] = None

    async def launch(self):
        """Launch the browser and set up the context.

        Uses the system-installed Chrome (channel='chrome') instead of
        Playwright's bundled Chromium to avoid Cloudflare Turnstile
        detection. A persistent user data directory is used so login
        sessions survive between runs.
        """
        self._playwright = await async_playwright().start()

        # Use a persistent profile dir so cookies/sessions are kept
        data_dir = self.user_data_dir or str(
            Path.home() / ".tanggle-solver" / "chrome-profile"
        )

        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=data_dir,
            channel="chrome",  # Use real installed Chrome, not bundled Chromium
            headless=self.headless,
            slow_mo=self.slow_mo,
            viewport={"width": 1920, "height": 1080},
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
        )

        # Persistent context has pages already; use the first or create one
        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = await self._context.new_page()

        # Hook WebSocket before navigating
        await self._page.add_init_script(f"({HOOK_WEBSOCKET_JS})()")

        logger.info("Browser launched (using installed Chrome with persistent profile)")

    async def is_logged_in(self) -> bool:
        """Check if already logged into tanggle.io (e.g. from a previous session)."""
        try:
            await self._page.goto(
                "https://tanggle.io/account/settings",
                wait_until="networkidle", timeout=15000,
            )
            # If the page shows a "Log In" button, we're not logged in.
            # If it shows account settings content, we are.
            logged_in = await self._page.evaluate("""
                () => {
                    const text = document.body.innerText;
                    // Look for signs we're NOT logged in
                    const hasLogIn = !!document.querySelector('button, a')
                        && Array.from(document.querySelectorAll('button, a'))
                            .some(el => el.textContent.trim().match(/^Log\\s*In$/i));
                    const hasSignIn = text.includes('Sign in');
                    return !hasLogIn && !hasSignIn;
                }
            """)
            return logged_in
        except Exception:
            return False

    async def login(self, credentials: TanggleCredentials, wait_callback=None):
        """Log into tanggle.io if not already logged in.

        Navigates to /account/settings and clicks the "Log In" button
        to open the login modal. Pre-fills credentials so the user only
        needs to complete the Cloudflare challenge and submit.

        Args:
            credentials: Email/password to pre-fill (user still submits manually).
            wait_callback: Async callable invoked while waiting for the user.
        """
        # Skip login if session already exists from a previous run
        if await self.is_logged_in():
            logger.info("Already logged in (existing session)")
            return

        logger.info("Opening account settings page...")
        await self._page.goto(
            "https://tanggle.io/account/settings",
            wait_until="networkidle", timeout=30000,
        )

        # Click the "Log In" button to open the login modal
        logger.info("Clicking Log In button to open modal...")
        try:
            login_btn = self._page.locator('button, a').filter(has_text="Log In").first
            await login_btn.click(timeout=10000)
            await asyncio.sleep(1)  # Wait for modal to open
        except Exception as e:
            logger.warning(f"Could not click Log In button: {e}")
            logger.info("Waiting for user to open login modal manually...")

        # Pre-fill credentials in the modal
        if credentials.email:
            try:
                await self._page.fill(
                    'input[name="email"], input[type="email"]',
                    credentials.email, timeout=5000,
                )
            except Exception:
                logger.debug("Could not pre-fill email field")
        if credentials.password:
            try:
                await self._page.fill(
                    'input[name="password"], input[type="password"]',
                    credentials.password, timeout=5000,
                )
            except Exception:
                logger.debug("Could not pre-fill password field")

        logger.info("Credentials pre-filled. Waiting for manual login...")

        if wait_callback:
            await wait_callback()

        # Poll until the login modal closes / page shows logged-in state (max 5 min)
        timeout = 300
        start = time.time()
        while time.time() - start < timeout:
            # Check if we're now logged in (modal closed, account info visible)
            logged_in = await self._page.evaluate("""
                () => {
                    // Check if a login modal is still visible
                    const modal = document.querySelector('[role="dialog"], .modal');
                    if (modal && modal.offsetParent !== null) return false;
                    // Check if Log In button is gone
                    const btns = Array.from(document.querySelectorAll('button, a'));
                    const hasLogIn = btns.some(el => el.textContent.trim().match(/^Log\\s*In$/i));
                    return !hasLogIn;
                }
            """)
            if logged_in:
                logger.info("Login successful")
                await asyncio.sleep(1)
                return
            await asyncio.sleep(1)

        raise RuntimeError("Login timed out — user did not complete login within 5 minutes")

    async def navigate_to_puzzle(self, puzzle_url: str) -> int:
        """Navigate to a tanggle.io puzzle page.

        Returns the HTTP status code (200 on success, 403 if blocked, etc.).
        """
        logger.info(f"Navigating to {puzzle_url}")
        response = await self._page.goto(puzzle_url, wait_until="networkidle", timeout=30000)

        status = response.status if response else 0
        logger.info(f"Page response status: {status}")

        if status == 403:
            logger.warning("Got 403 Forbidden — IP is likely blocked")
            return 403

        # Wait for the canvas to appear (puzzle loaded)
        try:
            await self._page.wait_for_selector("canvas", timeout=15000)
            logger.info("Canvas element found - puzzle is loading")
        except Exception:
            logger.warning("Canvas not found within timeout - puzzle may not have loaded")

        # Give PixiJS time to initialize and WebSocket to connect
        await asyncio.sleep(3)

        # Hook into the existing WebSocket (already connected by now)
        hook_result = await self._page.evaluate(HOOK_EXISTING_WS_JS)
        logger.info(f"WebSocket hook result: {hook_result}")

        # Extract puzzle UUID from URL
        parts = puzzle_url.rstrip("/").split("/")
        uuid = parts[-1] if parts else "unknown"
        self._puzzle_info = PuzzleInfo(uuid=uuid)

        return status

    async def get_game_state(self) -> dict:
        """Extract the current PixiJS game state."""
        return await self._page.evaluate(EXTRACT_GAME_STATE_JS)

    async def discover_pieces(self) -> dict:
        """Walk the PixiJS scene graph to find puzzle pieces."""
        return await self._page.evaluate(DISCOVER_PIECES_JS)

    async def get_ws_messages(self) -> list:
        """Get intercepted WebSocket messages."""
        return await self._page.evaluate(GET_WS_MESSAGES_JS)

    async def screenshot_canvas(self) -> Optional[str]:
        """Take a screenshot of the puzzle canvas as base64 PNG."""
        result = await self._page.evaluate(SCREENSHOT_CANVAS_JS)
        if isinstance(result, dict) and "error" in result:
            logger.warning(f"Canvas screenshot failed: {result['error']}")
            return None
        return result

    async def screenshot_page(self, path: str = "puzzle_screenshot.png"):
        """Take a viewport screenshot (matches page coordinates for mouse events)."""
        await self._page.screenshot(path=path, full_page=False)
        logger.info(f"Page screenshot saved to {path}")

    async def drag_piece(self, from_x: float, from_y: float, to_x: float, to_y: float, steps: int = 20) -> dict:
        """Drag a puzzle piece using JS synthetic events (canvas coords)."""
        result = await self._page.evaluate(
            f"({DRAG_PIECE_JS})({from_x}, {from_y}, {to_x}, {to_y}, {steps})"
        )
        return result

    async def drag_piece_mouse(self, from_x: float, from_y: float,
                               to_x: float, to_y: float, steps: int = 20):
        """Drag a puzzle piece using Playwright's mouse API (page coords).

        This uses real mouse events that PixiJS will pick up properly.
        The coordinates are page pixel coordinates (matching the screenshot).
        """
        mouse = self._page.mouse

        # Move to source, press, drag to target, release
        await mouse.move(from_x, from_y)
        await asyncio.sleep(0.05)
        await mouse.down()
        await asyncio.sleep(0.05)

        # Move in steps for smooth drag
        for step in range(1, steps + 1):
            t = step / steps
            x = from_x + (to_x - from_x) * t
            y = from_y + (to_y - from_y) * t
            await mouse.move(x, y)
            await asyncio.sleep(0.01)

        await asyncio.sleep(0.05)
        await mouse.up()

    async def click_piece(self, x: float, y: float):
        """Click on a position (for pocket mode - pick up piece)."""
        await self._page.mouse.click(x, y)

    async def move_piece_pocket_mode(self, from_x: float, from_y: float,
                                     to_x: float, to_y: float):
        """Move a piece: hold to grab, drag to target, release.

        Uses a slow initial move to grab the piece, then a faster drag
        to the target position.
        """
        mouse = self._page.mouse

        # Move to source and press-and-hold to grab
        await mouse.move(from_x, from_y)
        await asyncio.sleep(0.05)
        await mouse.down()
        await asyncio.sleep(0.2)  # Hold to let game register the grab

        # Move to target in a few steps
        steps = 10
        for step in range(1, steps + 1):
            t = step / steps
            x = from_x + (to_x - from_x) * t
            y = from_y + (to_y - from_y) * t
            await mouse.move(x, y)
            await asyncio.sleep(0.02)

        await asyncio.sleep(0.1)
        await mouse.up()

    async def reset_viewport(self):
        """Reset the game viewport by pressing Escape to cancel any held piece."""
        await self._page.keyboard.press("Escape")
        await asyncio.sleep(0.2)

    async def execute_js(self, script: str):
        """Execute arbitrary JavaScript in the page context."""
        return await self._page.evaluate(script)

    async def fetch_puzzle_api(self, puzzle_uuid: str) -> Optional[dict]:
        """Try to fetch puzzle info from the public API."""
        try:
            response = await self._page.evaluate("""
                () => fetch('https://api.tanggle.io/puzzles/public-rooms')
                    .then(r => r.json())
                    .catch(e => ({ error: e.message }))
            """)
            if isinstance(response, list):
                for room in response:
                    if room.get("uuid") == puzzle_uuid:
                        return room
            return response
        except Exception as e:
            logger.warning(f"Failed to fetch puzzle API: {e}")
            return None

    async def get_puzzle_image_url(self) -> Optional[str]:
        """Try to extract the puzzle image URL from network requests or API."""
        # Check network requests for media.tanggle.io
        image_url = await self._page.evaluate("""
            () => {
                const entries = performance.getEntriesByType('resource');
                for (const entry of entries) {
                    if (entry.name.includes('media.tanggle.io') &&
                        (entry.name.includes('.webp') || entry.name.includes('.jpg') || entry.name.includes('.png'))) {
                        return entry.name;
                    }
                }
                return null;
            }
        """)
        return image_url

    async def wait_for_game_ready(self, timeout: int = 60):
        """Wait until the game canvas is loaded and rendering content."""
        start = time.time()
        while time.time() - start < timeout:
            state = await self.get_game_state()
            logger.debug(f"Game state poll: {state}")

            if not state.get("canvasFound"):
                await asyncio.sleep(1)
                continue

            # Best case: we found the PixiJS app with children
            if state.get("appFound") and state.get("stageChildren", 0) > 0:
                logger.info(f"Game ready (PixiJS app found): {state}")
                return True

            # Good enough: canvas exists, is large, and has rendered content
            w = state.get("canvasWidth", 0)
            h = state.get("canvasHeight", 0)
            if w > 100 and h > 100 and state.get("hasContent"):
                logger.info(f"Game ready (canvas active, {w}x{h}): {state}")
                return True

            await asyncio.sleep(1)
        logger.warning("Game did not become ready within timeout")
        return False

    @property
    def page(self) -> Page:
        return self._page

    async def close(self):
        """Close the browser."""
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser closed")