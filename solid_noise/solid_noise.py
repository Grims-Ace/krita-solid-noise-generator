"""
Solid Noise Generator — Krita Plugin
Generates Perlin noise at canvas resolution with configurable octaves (1-16).

Compile a tiny C shared library at first use via gcc + ctypes.
This gives near-native performance (~0.1s at 1080p, 4 octaves) while requiring
zero third-party Python packages.  If gcc is unavailable we fall back to a
pure-Python path automatically.
"""

import array
import ctypes
import math
import os
import random
import subprocess
import tempfile

from krita import (Krita, Extension, DockWidget,
                   DockWidgetFactory, DockWidgetFactoryBase)
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QSlider, QSpinBox, QGroupBox, QProgressBar, QDoubleSpinBox,
    QCheckBox, QFrame,
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QObject


# ---------------------------------------------------------------------------
# C source for the noise kernel
# ---------------------------------------------------------------------------

_C_SOURCE = r"""
#include <math.h>
#include <string.h>

static int perm[512];

void sn_set_perm(int *p) {
    memcpy(perm, p, 512 * sizeof(int));
}

static float fade(float t) { return t*t*t*(t*(t*6.0f-15.0f)+10.0f); }

static float grad(int h, float x, float y) {
    h &= 3;
    if (h == 0) return  x + y;
    if (h == 1) return -x + y;
    if (h == 2) return  x - y;
    return -x - y;
}

/* Fill `out` (length=width) with one row of Perlin noise at height-coord ny. */
void sn_row(float *out, int width, float ny, float scale) {
    int   yi  = (int)floorf(ny) & 255;
    float yf  = ny - floorf(ny);
    float yf1 = yf - 1.0f;
    float v   = fade(yf);

    float inv_w = scale / (float)width;
    for (int x = 0; x < width; x++) {
        float nx  = x * inv_w;
        int   xi  = (int)floorf(nx) & 255;
        float xf  = nx - floorf(nx);
        float xf1 = xf - 1.0f;
        float u   = fade(xf);

        int aa = perm[perm[xi  ] + yi  ];
        int ab = perm[perm[xi  ] + yi+1];
        int ba = perm[perm[xi+1] + yi  ];
        int bb = perm[perm[xi+1] + yi+1];

        float x1 = grad(aa,xf,yf)  + u*(grad(ba,xf1,yf)  - grad(aa,xf,yf));
        float x2 = grad(ab,xf,yf1) + u*(grad(bb,xf1,yf1) - grad(ab,xf,yf1));
        out[x] = x1 + v*(x2 - x1);
    }
}

/*
 * Generate the full canvas (height rows x width cols) for one octave and
 * ADD the result * amplitude into acc[].
 */
void sn_octave(float *acc, int width, int height,
               float scale, float amplitude) {
    float inv_h = scale / (float)height;
    float *row  = (float *)__builtin_alloca(width * sizeof(float));
    for (int y = 0; y < height; y++) {
        float ny = y * inv_h;
        sn_row(row, width, ny, scale);
        int base = y * width;
        for (int x = 0; x < width; x++)
            acc[base + x] += row[x] * amplitude;
    }
}

/*
 * Normalise acc[], stretch so darkest=0 and brightest=255, pack as BGRA.
 * max_amp = sum of all octave amplitudes.
 * invert  = 0 or 1.
 */
void sn_pack_bgra(float *acc, unsigned char *out,
                  int npixels, float max_amp, int invert) {
    float inv = 1.0f / max_amp;

    /* First pass: normalise to [0,1] and find actual min/max. */
    float mn = 1.0f, mx = 0.0f;
    for (int i = 0; i < npixels; i++) {
        float v = (acc[i] * inv + 1.0f) * 0.5f;
        if (v < 0.0f) v = 0.0f;
        else if (v > 1.0f) v = 1.0f;
        acc[i] = v;
        if (v < mn) mn = v;
        if (v > mx) mx = v;
    }

    /* Second pass: remap [mn,mx] -> [0,255] and pack. */
    float range = mx - mn;
    float scale = (range > 1e-6f) ? (255.0f / range) : 0.0f;
    for (int i = 0; i < npixels; i++) {
        float v = (acc[i] - mn) * scale;
        if (invert) v = 255.0f - v;
        unsigned char bv = (unsigned char)(v + 0.5f);
        int base = i * 4;
        out[base  ] = bv;
        out[base+1] = bv;
        out[base+2] = bv;
        out[base+3] = 255;
    }
}
"""

# ---------------------------------------------------------------------------
# Build / load the shared library (once per session)
# ---------------------------------------------------------------------------

_clib      = None   # ctypes.CDLL or None
_clib_dir  = None   # tempfile.TemporaryDirectory kept alive

def _try_build_clib():
    global _clib, _clib_dir
    if _clib is not None:
        return True
    try:
        _clib_dir = tempfile.TemporaryDirectory(prefix="krita_solid_noise_")
        src_path = os.path.join(_clib_dir.name, "sn.c")
        lib_path = os.path.join(_clib_dir.name, "sn.so")
        with open(src_path, "w") as f:
            f.write(_C_SOURCE)
        result = subprocess.run(
            ["gcc", "-O2", "-shared", "-fPIC", "-o", lib_path, src_path, "-lm"],
            capture_output=True, timeout=30
        )
        if result.returncode != 0:
            return False
        lib = ctypes.CDLL(lib_path)

        lib.sn_set_perm.argtypes  = [ctypes.POINTER(ctypes.c_int)]
        lib.sn_set_perm.restype   = None

        lib.sn_octave.argtypes    = [ctypes.POINTER(ctypes.c_float),
                                      ctypes.c_int, ctypes.c_int,
                                      ctypes.c_float, ctypes.c_float]
        lib.sn_octave.restype     = None

        lib.sn_pack_bgra.argtypes = [ctypes.POINTER(ctypes.c_float),
                                      ctypes.POINTER(ctypes.c_ubyte),
                                      ctypes.c_int, ctypes.c_float, ctypes.c_int]
        lib.sn_pack_bgra.restype  = None

        _clib = lib
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Pure-Python fallback (used when gcc is unavailable)
# ---------------------------------------------------------------------------

def _py_fade(t):
    return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)

def _py_grad(h, x, y):
    h &= 3
    if h == 0: return  x + y
    if h == 1: return -x + y
    if h == 2: return  x - y
    return -x - y

def _py_generate(width, height, octaves, scale, seed, invert, progress_cb):
    perms = []
    for i in range(octaves):
        rng = random.Random(seed + i)
        p = list(range(256)); rng.shuffle(p)
        perms.append(p + p)

    amps  = [0.5 ** i for i in range(octaves)]
    freqs = [2.0 ** i for i in range(octaves)]
    max_a = sum(amps)
    acc   = [0.0] * (width * height)

    for oi in range(octaves):
        p = perms[oi]; amp = amps[oi]; fs = scale * freqs[oi]
        inv_h = fs / height; inv_w = fs / width
        for y in range(height):
            ny = y * inv_h
            nyi = int(math.floor(ny)) & 255
            yf = ny - math.floor(ny); yf1 = yf - 1.0; v = _py_fade(yf)
            base = y * width
            for x in range(width):
                nx = x * inv_w
                xi = int(math.floor(nx)) & 255
                xf = nx - math.floor(nx); xf1 = xf - 1.0; u = _py_fade(xf)
                aa=p[p[xi]+nyi]; ab=p[p[xi]+nyi+1]
                ba=p[p[xi+1]+nyi]; bb=p[p[xi+1]+nyi+1]
                x1 = _py_grad(aa,xf,yf)  + u*(_py_grad(ba,xf1,yf)  - _py_grad(aa,xf,yf))
                x2 = _py_grad(ab,xf,yf1) + u*(_py_grad(bb,xf1,yf1) - _py_grad(ab,xf,yf1))
                acc[base+x] += (x1 + v*(x2-x1)) * amp
        progress_cb(int((oi+1)/octaves*90))

    inv_norm = 1.0 / max_a
    # Normalise to [0,1]
    norm = []
    for v in acc:
        v = (v * inv_norm + 1.0) * 0.5
        if v < 0.0: v = 0.0
        elif v > 1.0: v = 1.0
        norm.append(v)
    # Stretch so min→0 and max→1
    mn = min(norm); mx = max(norm)
    rng = mx - mn
    scale = (1.0 / rng) if rng > 1e-6 else 0.0
    pixels = bytearray(width * height * 4)
    for i in range(width * height):
        v = (norm[i] - mn) * scale
        if invert: v = 1.0 - v
        bv = int(v * 255.0 + 0.5)
        b = i * 4
        pixels[b] = pixels[b+1] = pixels[b+2] = bv; pixels[b+3] = 255
    progress_cb(100)
    return bytes(pixels)


# ---------------------------------------------------------------------------
# C-accelerated path
# ---------------------------------------------------------------------------

def _c_generate(width, height, octaves, scale, seed, invert, progress_cb):
    lib = _clib
    npix = width * height

    acc_arr  = (ctypes.c_float * npix)(*([0.0] * npix))
    bgra_arr = (ctypes.c_ubyte * (npix * 4))()

    amps  = [0.5 ** i for i in range(octaves)]
    freqs = [2.0 ** i for i in range(octaves)]
    max_a = sum(amps)

    for oi in range(octaves):
        rng = random.Random(seed + oi)
        p = list(range(256)); rng.shuffle(p); p = p * 2
        perm_arr = (ctypes.c_int * 512)(*p)
        lib.sn_set_perm(perm_arr)
        lib.sn_octave(acc_arr, width, height,
                      ctypes.c_float(scale * freqs[oi]),
                      ctypes.c_float(amps[oi]))
        progress_cb(int((oi + 1) / octaves * 90))

    lib.sn_pack_bgra(acc_arr, bgra_arr, npix,
                     ctypes.c_float(max_a), ctypes.c_int(1 if invert else 0))
    progress_cb(100)
    return bytes(bgra_arr)


def generate(width, height, octaves, scale, seed, invert, progress_cb):
    if _try_build_clib():
        return _c_generate(width, height, octaves, scale, seed, invert, progress_cb)
    return _py_generate(width, height, octaves, scale, seed, invert, progress_cb)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

class NoiseWorker(QObject):
    progress = pyqtSignal(int)
    finished = pyqtSignal(bytes, int, int)
    error    = pyqtSignal(str)

    def __init__(self, width, height, octaves, scale, seed, invert):
        super().__init__()
        self.width   = width
        self.height  = height
        self.octaves = octaves
        self.scale   = scale
        self.seed    = seed
        self.invert  = invert

    def run(self):
        try:
            data = generate(self.width, self.height, self.octaves,
                            self.scale, self.seed, self.invert,
                            self.progress.emit)
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

        # Scale
        sg = QGroupBox("Scale")
        sl = QHBoxLayout(sg)
        sl.addWidget(QLabel("Zoom:"))
        self._scale_spin = QDoubleSpinBox()
        self._scale_spin.setRange(0.1, 32.0)
        self._scale_spin.setSingleStep(0.5)
        self._scale_spin.setValue(4.0)
        sl.addWidget(self._scale_spin)
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
        self._worker = NoiseWorker(w, h,
                                   self._oct_slider.value(),
                                   self._scale_spin.value(),
                                   self._seed_spin.value(),
                                   self._invert_chk.isChecked())
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
            accel = "C" if _clib else "Python"
            self._status.setText(
                f"Done  {w}x{h}, {self._oct_slider.value()} oct  [{accel}]"
            )
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
