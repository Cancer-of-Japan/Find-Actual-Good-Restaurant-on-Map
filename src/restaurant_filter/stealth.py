"""Minimal fingerprint patches to reduce automation detection.

A stock Playwright Chromium launch is flagged by bot.sannysoft.com on exactly
two rows: ``navigator.webdriver === true`` and a ``SwiftShader`` WebGL renderer
(software rendering under Xvfb/headless). Everything else already passes. These
patches neutralise just those two tells so the fingerprint matches an ordinary
Linux desktop Chrome. Human-like mouse motion is handled separately in
``human_mouse.py`` -- this module only touches static JS fingerprints.
"""

from __future__ import annotations

# Passed to ``chromium.launch(args=...)``. ``AutomationControlled`` stops
# Chromium from advertising the ``navigator.webdriver`` flag it normally sets
# when driven over CDP.
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
]

# Injected before any page script runs via ``context.add_init_script``. The
# WebGL strings are a realistic Linux Mesa/Intel pair, consistent with the
# ``Linux x86_64`` user agent Chromium reports here.
STEALTH_JS = r"""
// navigator.webdriver -> undefined. Deleting the getter from the prototype is
// the reliable cross-version approach; defineProperty on the instance often
// throws because the property is non-configurable.
try { delete Object.getPrototypeOf(navigator).webdriver; } catch (e) {}
try {
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true });
} catch (e) {}

// Spoof the WebGL vendor/renderer so SwiftShader (software rendering under
// Xvfb) does not reveal a headless/VM environment. Values chosen to match a
// plausible Linux Chrome on Intel integrated graphics.
(function () {
  const spoof = {
    37445: 'Google Inc. (Intel)',  // UNMASKED_VENDOR_WEBGL
    37446: 'ANGLE (Intel, Mesa Intel(R) UHD Graphics 620 (KBL GT2), OpenGL 4.6 (Core Profile) Mesa 23.2.1)',  // UNMASKED_RENDERER_WEBGL
  };
  const protos = [
    typeof WebGLRenderingContext !== 'undefined' ? WebGLRenderingContext.prototype : null,
    typeof WebGL2RenderingContext !== 'undefined' ? WebGL2RenderingContext.prototype : null,
  ];
  for (const proto of protos) {
    if (!proto) continue;
    const original = proto.getParameter;
    proto.getParameter = function (p) {
      if (p in spoof) return spoof[p];
      return original.call(this, p);
    };
  }
})();
"""


def apply_stealth(context) -> None:
    """Register the stealth init script on a Playwright browser context."""
    context.add_init_script(STEALTH_JS)
