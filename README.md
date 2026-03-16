# Solid Noise Generator — Krita Plugin

A Krita plugin that generates solid Perlin noise at your canvas resolution. Supports configurable octave detail, scale, seed, and invert. Output is always contrast-stretched so the darkest pixel is pure black and the brightest is pure white.

---

## Installation

1. In Krita, go to **Tools → Scripts → Import Python Plugin from File…**
2. Select `solid_noise_plugin.zip`
3. Restart Krita
4. Go to **Settings → Configure Krita → Python Plugin Manager**, check **Solid Noise Generator**, click OK
5. Restart Krita again
6. Open the docker via **Settings → Dockers → Solid Noise Generator**

> **Manual install:** If you prefer, extract the zip and copy `solid_noise.desktop` and the `solid_noise/` folder into your Krita `pykrita` directory:
> - **Linux/macOS:** `~/.local/share/krita/pykrita/`
> - **Windows:** `%APPDATA%\krita\pykrita\`

---

## Controls

| Control | Description |
|---|---|
| **Octaves (1–16)** | Number of noise layers stacked together. 1 is smooth and cloud-like; 16 is highly detailed. Each octave doubles the frequency and halves the amplitude. |
| **Scale** | Zoom level of the noise pattern. Higher values show more features across the canvas. |
| **Seed** | Determines the random pattern. Same seed always produces the same result. Hit **Random** for a new one. |
| **Invert** | Flips light and dark values. |
| **Generate on new layer** | When checked, noise is painted onto a new layer so your existing content is untouched. |

---

## How it works

Perlin noise is computed using **fractal Brownian motion (fBm)**: multiple octaves of noise are summed, each at double the frequency and half the amplitude of the last:

```
value = Σ  0.5^i × perlin(x × 2^i, y × 2^i)
         i=0..octaves-1
```

After all octaves are accumulated, the result is **contrast-stretched** so the minimum value maps to pure black (0) and the maximum to pure white (255), guaranteeing full tonal range regardless of settings.
