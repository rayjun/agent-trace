"""Terminal compatibility for Textual running inside Herdr."""
from textual.drivers.linux_driver import LinuxDriver


class HerdrDriver(LinuxDriver):
    """Use SGR cell coordinates and SIGWINCH for pane resizes.

    Herdr 0.9 acknowledges mode 2048 without sending the initial pixel size.
    Textual then enables mode 1016, but its parser cannot translate the pixel
    mouse reports into cells until a resize arrives. Clicks land off screen.
    """

    def _enable_mouse_support(self) -> None:
        # Also reset a pixel mode left behind by an earlier Textual process.
        self.write("\x1b[?1016l")
        super()._enable_mouse_support()

    def _query_in_band_window_resize(self) -> None:
        self.write("\x1b[?2048l")
        self.flush()
