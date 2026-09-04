"""Desktop UI for the standalone pipeline, so the non-extension path doesn't
need the terminal.

Model loading takes ~15s (Demucs plus the PANNs classifier), so it happens on
a worker thread and the models are kept for the rest of the session -- only
the first Start pays that cost. The audio pipeline itself already runs on its
own threads; this just drives it and polls for status.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt, QSettings, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from . import audio_io
from .pipeline import Pipeline
from .separator import SpeechIsolator
from .vocal_classifier import VocalClassifier

SETUP_HELP = (
    "StreamerIsolate reads the stream's audio from an input device, removes the "
    "music, and plays the speech back to an output device.\n\n"
    "Because macOS has no built-in way to route one app's audio, you need a "
    "virtual audio device (BlackHole, or Pro Tools Audio Bridge if you have "
    "Pro Tools). Set that as your system output while the stream plays, pick it "
    "as 'Capture from' below, and set 'Play to' to your real speakers or "
    "interface.\n\n"
    "If you use Chrome, the browser extension avoids all of this routing — see "
    "the project README."
)


class ModelLoader(QThread):
    """Loads the models off the UI thread so the window doesn't freeze."""

    loaded = Signal(object, object)
    failed = Signal(str)

    def __init__(self, use_classifier: bool):
        super().__init__()
        self.use_classifier = use_classifier

    def run(self):
        try:
            isolator = SpeechIsolator()
            classifier = VocalClassifier() if self.use_classifier else None
            self.loaded.emit(isolator, classifier)
        except Exception as e:  # noqa: BLE001 - surfaced in the UI
            self.failed.emit(f"{type(e).__name__}: {e}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("StreamerIsolate")
        self.settings = QSettings("StreamerIsolate", "StreamerIsolate")

        self.isolator: SpeechIsolator | None = None
        self.classifier: VocalClassifier | None = None
        self.pipeline: Pipeline | None = None
        self.loader: ModelLoader | None = None

        self._build_ui()
        self._populate_devices()
        self._restore_settings()

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_pipeline)
        self.poll_timer.setInterval(300)

    # --- layout ---

    def _build_ui(self):
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        title = QLabel("StreamerIsolate")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(title)

        subtitle = QLabel("Removes background music from a livestream, keeping only speech.")
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: palette(mid);")
        layout.addWidget(subtitle)

        help_row = QHBoxLayout()
        self.help_button = QPushButton("How do I set this up?")
        self.help_button.setFlat(True)
        self.help_button.setStyleSheet("text-align: left; color: palette(link);")
        self.help_button.clicked.connect(self._show_help)
        help_row.addWidget(self.help_button)
        help_row.addStretch()
        layout.addLayout(help_row)

        layout.addWidget(self._separator())

        # Devices
        self.input_combo = QComboBox()
        self.output_combo = QComboBox()
        for combo in (self.input_combo, self.output_combo):
            combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._populate_devices)

        input_row = QHBoxLayout()
        input_row.addWidget(self.input_combo, 1)
        input_row.addWidget(refresh)

        layout.addWidget(QLabel("Capture the stream's audio from:"))
        layout.addLayout(input_row)
        layout.addWidget(QLabel("Play the cleaned audio to:"))
        layout.addWidget(self.output_combo)

        layout.addWidget(self._separator())

        # Strength
        strength_label_row = QHBoxLayout()
        strength_label_row.addWidget(QLabel("Vocal attenuation"))
        strength_label_row.addStretch()
        self.strength_value = QLabel("85%")
        self.strength_value.setStyleSheet("color: palette(mid);")
        strength_label_row.addWidget(self.strength_value)
        layout.addLayout(strength_label_row)

        self.strength_slider = QSlider(Qt.Horizontal)
        self.strength_slider.setRange(0, 100)
        self.strength_slider.setValue(85)
        self.strength_slider.valueChanged.connect(self._on_strength_changed)
        layout.addWidget(self.strength_slider)

        strength_hint = QLabel(
            "How hard detected singing is cut. Takes effect immediately, even while running."
        )
        strength_hint.setWordWrap(True)
        strength_hint.setStyleSheet("color: palette(mid); font-size: 11px;")
        layout.addWidget(strength_hint)

        layout.addSpacing(4)

        self.start_button = QPushButton("Start")
        self.start_button.setMinimumHeight(34)
        self.start_button.setDefault(True)
        self.start_button.clicked.connect(self._on_start_stop)
        layout.addWidget(self.start_button)

        self.status_label = QLabel("Idle")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.status_label)

        layout.addStretch()
        self.setCentralWidget(central)
        self.setMinimumWidth(430)

    def _separator(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    # --- devices/settings ---

    def _populate_devices(self):
        previous_in = self.input_combo.currentData()
        previous_out = self.output_combo.currentData()
        self.input_combo.clear()
        self.output_combo.clear()

        try:
            devices = audio_io.list_devices()
        except Exception as e:  # noqa: BLE001
            self._set_status(f"Could not list audio devices: {e}", error=True)
            return

        for d in devices:
            if d.max_input_channels > 0:
                self.input_combo.addItem(f"{d.name}  ({d.max_input_channels} in)", d.index)
            if d.max_output_channels > 0:
                self.output_combo.addItem(f"{d.name}  ({d.max_output_channels} out)", d.index)

        self._reselect(self.input_combo, previous_in)
        self._reselect(self.output_combo, previous_out)

    @staticmethod
    def _reselect(combo: QComboBox, value):
        if value is None:
            return
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _restore_settings(self):
        strength = self.settings.value("vocalStrength", 85, type=int)
        self.strength_slider.setValue(strength)
        self._reselect(self.input_combo, self.settings.value("inputDevice", type=int))
        self._reselect(self.output_combo, self.settings.value("outputDevice", type=int))

    # --- actions ---

    def _show_help(self):
        QMessageBox.information(self, "Setting up StreamerIsolate", SETUP_HELP)

    def _on_strength_changed(self, value: int):
        self.strength_value.setText(f"{value}%")
        self.settings.setValue("vocalStrength", value)
        if self.pipeline is not None:
            # Read fresh on each chunk by the worker thread, so this is enough.
            self.pipeline.vocal_strength = value / 100.0

    def _on_start_stop(self):
        if self.pipeline is not None:
            self._stop_pipeline()
        else:
            self._start_pipeline()

    def _start_pipeline(self):
        if self.input_combo.currentData() is None or self.output_combo.currentData() is None:
            self._set_status("Pick an input and an output device first.", error=True)
            return

        self.settings.setValue("inputDevice", self.input_combo.currentData())
        self.settings.setValue("outputDevice", self.output_combo.currentData())

        if self.isolator is None:
            self.start_button.setEnabled(False)
            self._set_status("Loading models… (first start only, ~15 seconds)")
            self.loader = ModelLoader(use_classifier=True)
            self.loader.loaded.connect(self._on_models_loaded)
            self.loader.failed.connect(self._on_models_failed)
            self.loader.start()
        else:
            self._launch_pipeline()

    def _on_models_loaded(self, isolator, classifier):
        self.isolator = isolator
        self.classifier = classifier
        self.start_button.setEnabled(True)
        self._launch_pipeline()

    def _on_models_failed(self, message: str):
        self.start_button.setEnabled(True)
        self._set_status(f"Could not load models — {message}", error=True)

    def _launch_pipeline(self):
        try:
            self.pipeline = Pipeline(
                output_device=self.output_combo.currentData(),
                isolator=self.isolator,
                input_device=self.input_combo.currentData(),
                vocal_classifier=self.classifier,
                vocal_strength=self.strength_slider.value() / 100.0,
            )
            self.pipeline.start()
        except Exception as e:  # noqa: BLE001
            self.pipeline = None
            self._set_status(f"Could not start — {type(e).__name__}: {e}", error=True)
            return

        self.start_button.setText("Stop")
        self._set_input_enabled(False)
        self._set_status("Buffering… audio starts in a few seconds.")
        self.poll_timer.start()

    def _stop_pipeline(self):
        self.poll_timer.stop()
        if self.pipeline is not None:
            self.pipeline.stop()
            self.pipeline = None
        self.start_button.setText("Start")
        self._set_input_enabled(True)
        self._set_status("Idle")

    def _poll_pipeline(self):
        if self.pipeline is None:
            return
        if self.pipeline.error:
            error = self.pipeline.error
            self._stop_pipeline()
            self._set_status(f"Stopped — {error}", error=True)
            return
        if self.pipeline.chunks_emitted > 0:
            self._set_status("Running — playing isolated speech.", ok=True)

    def _set_input_enabled(self, enabled: bool):
        self.input_combo.setEnabled(enabled)
        self.output_combo.setEnabled(enabled)

    def _set_status(self, text: str, error: bool = False, ok: bool = False):
        self.status_label.setText(text)
        if error:
            self.status_label.setStyleSheet("color: #c62828;")
        elif ok:
            self.status_label.setStyleSheet("color: #2e7d32; font-weight: 600;")
        else:
            self.status_label.setStyleSheet("color: palette(mid);")

    def closeEvent(self, event):
        if self.pipeline is not None:
            self.pipeline.stop()
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("StreamerIsolate")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
