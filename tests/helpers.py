import numpy as np
from scipy import signal


def spectrum_db(x: np.ndarray, fs: float):
    """Power spectrum (dB) of a real signal with a Blackman-Harris window."""
    x = np.asarray(x, np.float64)
    if x.ndim == 2:
        x = x[:, 0]
    w = signal.get_window("blackmanharris", len(x))
    sp = np.abs(np.fft.rfft(x * w)) ** 2
    f = np.fft.rfftfreq(len(x), 1 / fs)
    return f, 10 * np.log10(sp + 1e-30)


def dominant(x: np.ndarray, fs: float, exclude_hz: float = 30.0):
    """(peak frequency, peak dB, dB of the strongest content outside +-exclude_hz of the peak)."""
    f, db = spectrum_db(x, fs)
    k = int(np.argmax(db))
    # parabolic interpolation for sub-bin accuracy
    if 0 < k < len(db) - 1:
        a, b, c = db[k - 1], db[k], db[k + 1]
        d = 0.5 * (a - c) / (a - 2 * b + c)
    else:
        d = 0.0
    fpk = (k + d) * (f[1] - f[0])
    mask = np.abs(f - f[k]) > exclude_hz
    return fpk, db[k], db[mask].max()


def level_at(x: np.ndarray, fs: float, freq: float, width: float = 15.0) -> float:
    f, db = spectrum_db(x, fs)
    m = np.abs(f - freq) <= width
    return float(db[m].max())


def feed(chain, iq: np.ndarray, block: int = 131072):
    out = []
    for i in range(0, len(iq), block):
        out.append(chain.process(iq[i:i + block]))
    return np.concatenate(out)
