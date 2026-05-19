"""
Solid Noise Generator — Krita Plugin
Generates Perlin noise at canvas resolution with configurable octaves (1-16).
"""

import math
import random

from krita import DockWidget, DockWidgetFactory, DockWidgetFactoryBase, Extension, Krita
from PyQt5.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

# ---------------------------------------------------------------------------
# Noise generation (Pure Python)
# ---------------------------------------------------------------------------


def _fade(t):
    return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)


def _grad(h, x, y):
    h &= 3
    if h == 0:
        return x + y
    if h == 1:
        return -x + y
    if h == 2:
        return x - y
    return -x - y


def generate(width, height, octaves, base_scale, zoom, seed, invert, progress_cb):
    perms = []
    for i in range(octaves):
        rng = random.Random(seed + i)
        p = list(range(256))
        rng.shuffle(p)
        perms.append(p + p)

    amps = [0.5**i for i in range(octaves)]
    freqs = [2.0**i for i in range(octaves)]
    max_a = sum(amps)
    acc = [0.0] * (width * height)

    # Effective scale: less zoom = smaller fs = zoomed in (larger features)
    # More zoom = larger fs = zoomed out (smaller features)
    effective_scale = base_scale * zoom

    for oi in range(octaves):
        p = perms[oi]
        amp = amps[oi]
        fs = effective_scale * freqs[oi]
        inv_h = fs / height
        inv_w = fs / width
        for y in range(height):
            ny = y * inv_h
            nyi = int(math.floor(ny)) & 255
            yf = ny - math.floor(ny)
            yf1 = yf - 1.0
            v = _fade(yf)
            base = y * width
            for x in range(width):
                nx = x * inv_w
                xi = int(math.floor(nx)) & 255
                xf = nx - math.floor(nx)
                xf1 = xf - 1.0
                u = _fade(xf)
                aa = p[p[xi] + nyi]
                ab = p[p[xi] + nyi + 1]
                ba = p[p[xi + 1] + nyi]
                bb = p[p[xi + 1] + nyi + 1]
                x1 = _grad(aa, xf, yf) + u * (_grad(ba, xf1, yf) - _grad(aa, xf, yf))
                x2 = _grad(ab, xf, yf1) + u * (_grad(bb, xf1, yf1) - _grad(ab, xf, yf1))
                acc[base + x] += (x1 + v * (x2 - x1)) * amp
        progress_cb(int((oi + 1) / octaves * 90))

    inv_norm = 1.0 / max_a
    # Normalise to [0,1]
    norm = []
    for v in acc:
        v = (v * inv_norm + 1.0) * 0.5
        if v < 0.0:
            v = 0.0
        elif v > 1.0:
            v = 1.0
        norm.append(v)
    # Stretch so min→0 and max→1
    mn = min(norm)
    mx = max(norm)
    rng = mx - mn
    scale = (1.0 / rng) if rng > 1e-6 else 0.0
    pixels = bytearray(width * height * 4)
    for i in range(width * height):
        v = (norm[i] - mn) * scale
        if invert:
            v = 1.0 - v
        bv = int(v * 255.0 + 0.5)
        b = i * 4
        pixels[b] = pixels[b + 1] = pixels[b + 2] = bv
        pixels[b + 3] = 255
    progress_cb(100)
    return bytes(pixels)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class NoiseWorker(QObject):
    progress = pyqtSignal(int)
    finished = pyqtSignal(bytes, int, int)
    error = pyqtSignal(str)

    def __init__(self, width, height, octaves, base_scale, zoom, seed, invert):
        super().__init__()
        self.width = width
        self.height = height
        self.octaves = octaves
        self.base_scale = base_scale
        self.zoom = zoom
        self.seed = seed
        self.invert = invert

    def run(self):
        try:
            data = generate(
                self.width,
                self.height,
                self.octaves,
                self.base_scale,
                self.zoom,
                self.seed,
                self.invert,
                self.progress.emit,
            )
            self.finished.emit(data, self.width, self.height)
        except Exception as e:
            self.error.emit(str(e))


# ---------------------------------------------------------------------------
# Docker widget
# ---------------------------------------------------------------------------

DOCKER_ID = "solid_noise_docker"


class SolidNoiseDock(DockWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Solid Noise Generator")
        self._thread = None
        self._worker = None
        self._build_ui()

    def _build_ui(self):
        root = QWidget(self)
        self.setWidget(root)
        layout = QVBoxLayout(root)
        layout.setSpacing(8)
        layout.setContentsMargins(10, 10, 10, 10)

        # Octaves
        og = QGroupBox("Detail (Octaves)")
        ol = QVBoxLayout(og)
        row = QHBoxLayout()
        self._oct_slider = QSlider(Qt.Horizontal)
        self._oct_slider.setRange(1, 16)
        self._oct_slider.setValue(4)
        self._oct_slider.setTickInterval(1)
        self._oct_slider.setTickPosition(QSlider.TicksBelow)
        self._oct_lbl = QLabel("4")
        self._oct_lbl.setMinimumWidth(24)
        self._oct_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._oct_slider.valueChanged.connect(lambda v: self._oct_lbl.setText(str(v)))
        row.addWidget(self._oct_slider)
        row.addWidget(self._oct_lbl)
        ol.addLayout(row)
        hint = QLabel("1 = smooth   16 = highly detailed")
        hint.setStyleSheet("color: palette(mid); font-size: 10px;")
        ol.addWidget(hint)
        layout.addWidget(og)

        # Zoom
        sg = QGroupBox("Zoom")
        sl = QVBoxLayout(sg)

        # Zoom Slider
        row = QHBoxLayout()
        row.addWidget(QLabel("Factor:"))
        self._zoom_slider = QSlider(Qt.Horizontal)
        self._zoom_slider.setRange(10, 1000)  # 0.1 to 10.0
        self._zoom_slider.setValue(100)  # 1.0
        self._zoom_lbl = QLabel("1.00x")
        self._zoom_lbl.setMinimumWidth(40)
        self._zoom_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._zoom_slider.valueChanged.connect(
            lambda v: self._zoom_lbl.setText(f"{v / 100:.2f}x")
        )
        row.addWidget(self._zoom_slider)
        row.addWidget(self._zoom_lbl)
        sl.addLayout(row)

        hint = QLabel("Zoom < 1.0 zooms in, > 1.0 zooms out")
        hint.setStyleSheet("color: palette(mid); font-size: 10px;")
        sl.addWidget(hint)
        layout.addWidget(sg)

        # Seed
        sdg = QGroupBox("Seed")
        sdl = QHBoxLayout(sdg)
        self._seed_spin = QSpinBox()
        self._seed_spin.setRange(0, 99999)
        self._seed_spin.setValue(42)
        sdl.addWidget(self._seed_spin)
        rb = QPushButton("Random")
        rb.clicked.connect(lambda: self._seed_spin.setValue(random.randint(0, 99999)))
        sdl.addWidget(rb)
        layout.addWidget(sdg)

        # Options
        self._invert_chk = QCheckBox("Invert noise")
        layout.addWidget(self._invert_chk)
        self._new_layer_chk = QCheckBox("Generate on new layer")
        self._new_layer_chk.setChecked(True)
        layout.addWidget(self._new_layer_chk)

        # Divider
        div = QFrame()
        div.setFrameShape(QFrame.HLine)
        div.setFrameShadow(QFrame.Sunken)
        layout.addWidget(div)

        # Progress
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setVisible(False)
        layout.addWidget(self._bar)

        # Button
        self._btn = QPushButton("Generate Noise")
        self._btn.setMinimumHeight(36)
        self._btn.setStyleSheet("font-weight: bold;")
        self._btn.clicked.connect(self._on_generate)
        layout.addWidget(self._btn)

        self._status = QLabel("")
        self._status.setAlignment(Qt.AlignCenter)
        self._status.setStyleSheet("font-size: 11px; color: palette(mid);")
        layout.addWidget(self._status)

        layout.addStretch()

    def _set_busy(self, busy):
        self._btn.setEnabled(not busy)
        self._bar.setVisible(busy)
        if not busy:
            self._bar.setValue(0)

    def _on_generate(self):
        app = Krita.instance()
        doc = app.activeDocument()
        if doc is None:
            self._status.setText("No active document.")
            return
        w, h = doc.width(), doc.height()
        self._status.setText(f"Generating {w}x{h}...")
        self._set_busy(True)

        self._thread = QThread()
        self._worker = NoiseWorker(
            w,
            h,
            self._oct_slider.value(),
            4.0,  # Default base scale
            self._zoom_slider.value() / 100.0,
            self._seed_spin.value(),
            self._invert_chk.isChecked(),
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._bar.setValue)
        self._worker.finished.connect(self._on_done)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_done(self, data, w, h):
        try:
            app = Krita.instance()
            doc = app.activeDocument()
            if doc is None:
                self._on_error("Document closed.")
                return
            if self._new_layer_chk.isChecked():
                layer = doc.createNode("Solid Noise", "paintlayer")
                doc.rootNode().addChildNode(layer, None)
            else:
                layer = doc.activeNode()
            layer.setPixelData(data, 0, 0, w, h)
            doc.refreshProjection()
            self._status.setText(f"Done  {w}x{h}, {self._oct_slider.value()} oct")
        except Exception as e:
            self._on_error(str(e))
        finally:
            self._set_busy(False)

    def _on_error(self, msg):
        self._status.setText(f"Error: {msg}")
        self._set_busy(False)

    def canvasChanged(self, canvas):
        pass


# ---------------------------------------------------------------------------
# Extension + docker registration
# ---------------------------------------------------------------------------


class SolidNoisePlugin(Extension):
    def __init__(self, parent=None):
        super().__init__(parent)

    def setup(self):
        pass

    def createActions(self, window):
        pass


_app = Krita.instance()
_app.addExtension(SolidNoisePlugin(_app))
_app.addDockWidgetFactory(
    DockWidgetFactory(DOCKER_ID, DockWidgetFactoryBase.DockRight, SolidNoiseDock)
)
