"""Hot loops of the FPV video decoder, compiled with numba (see DECISIONS.md for the measurements
that justify it). No Qt. Nothing here keeps state of its own: state lives in arrays owned by the
caller, so `decoder.py` decides when things happen and block boundaries never matter.

Indices are relative to the start of the sync buffer y; lines are returned as float sample times."""

from __future__ import annotations

import numpy as np
from numba import njit

# indices into the H-sync tracker state array
ST_MODE = 0            # 0 searching, 1 tracking
ST_NEXT = 1            # predicted next sync edge, absolute sample position
ST_PERIOD = 2          # measured line period, samples
ST_TIP = 3             # sync tip level (signal units)
ST_BLANK = 4           # blanking (back porch) level
ST_MISS = 5            # consecutive lines without a sync edge
ST_LINE = 6            # running line counter
ST_RUN = 7             # current run of broad half-lines
ST_SINCE_V = 8         # half-lines since the last vertical sync
ST_NOMINAL = 9         # nominal line period in samples (search window centre, free run)
ST_MIN_P = 10          # search limits on the line period
ST_MAX_P = 11
ST_SCAN = 12           # absolute position the pulse scanner has reached
ST_THR = 13            # slicing threshold fraction between tip and blanking
ST_LEN = 14

MAX_PULSES = 128
EV_MAX = 8


@njit(cache=True, fastmath=True, nogil=True)
def fir_filter(x, hist, taps, out):
    """Symmetric odd-length FIR: out[i] gets the filter output for x[i], history in hist."""
    n = len(x)
    L = len(taps)
    m = L // 2
    H = L - 1
    # work buffer = hist + x, as one contiguous array
    z = np.empty(n + H, np.float32)
    z[:H] = hist
    z[H:] = x
    for i in range(n):
        s = z[i + m] * taps[m]
        for k in range(m):
            s += (z[i + k] + z[i + H - k]) * taps[k]
        out[i] = s
    hist[:] = z[n:]


@njit(cache=True, fastmath=True, nogil=True)
def scan_pulses(y, n0, start, stop, th, min_w, max_w, out):
    """Find sync pulses in y[start:stop): falling edges whose low stretch lasts min_w..max_w
    samples. Writes absolute edge times into out; returns (count, next scan index)."""
    count = 0
    i = start
    cap = len(out)
    while i < stop and count < cap:
        if y[i - 1] >= th and y[i] < th:
            w = 1
            j = i + 1
            nmax = len(y)
            while j < nmax and y[j] < th and w <= max_w:
                j += 1
                w += 1
            if j >= nmax:
                return count, i       # needs more data: rescan this edge next time
            if min_w <= w <= max_w:
                d = float(y[i - 1] - y[i])
                frac = (y[i - 1] - th) / d if d > 0 else 0.0
                out[count] = n0 + (i - 1) + frac
                count += 1
            i = j
        else:
            i += 1
    return count, i


@njit(cache=True, fastmath=True, nogil=True)
def find_edge(y, center, W, th, check_from, check_to):
    """Falling edge at threshold th nearest to `center` within +-W, followed by a sync-like low.
    Returns the interpolated edge position or -1."""
    a = int(center) - W
    b = int(center) + W
    if a < 1:
        a = 1
    if b >= len(y) - check_to - 1:
        b = len(y) - check_to - 2
    best = -1.0
    best_d = 1e18
    for i in range(a, b + 1):
        if y[i - 1] >= th and y[i] < th:
            # the pulse must really stay low (not a noise spike)
            ok = True
            for k in range(check_from, check_to + 1, 4):
                if y[i + k] >= th:
                    ok = False
                    break
            if ok:
                d = float(y[i - 1] - y[i])
                t = (i - 1) + ((y[i - 1] - th) / d if d > 0 else 0.0)
                dist = abs(t - center)
                if dist < best_d:
                    best_d = dist
                    best = t
    return best


@njit(cache=True, fastmath=True, nogil=True)
def track_lines(y, n0, st, W, sync_samples, tip_a, tip_b, cl_a, cl_b, kp, ki, level_a,
                lines_t, hits, broad_half, tips, blanks):
    """Line tracker with flywheel. Follows the sync edges in y; for each completed line writes
    its start time (absolute), whether a real edge was seen, broad-pulse flags per half line
    (bit0 = first half, bit1 = second half) and the measured levels.

    Returns the number of lines produced."""
    n_lines = 0
    nmax = len(lines_t)
    period = st[ST_PERIOD]
    next_abs = st[ST_NEXT]
    tip = st[ST_TIP]
    blank = st[ST_BLANK]
    frac = st[ST_THR]
    miss = int(st[ST_MISS])
    while n_lines < nmax:
        p = next_abs - n0
        if p < 0.0:                # the grid is behind the buffer: y[i] below would be out of bounds
            st[ST_MODE] = 0
            break
        # need a complete line after the predicted edge in the buffer
        if p + period + W + cl_b + 2 >= len(y):
            break
        th = tip + frac * (blank - tip)
        edge = -1.0
        if p - W - 2 > 0:
            edge = find_edge(y, p, W, th, sync_samples // 5, sync_samples // 3)
        hit = edge >= 0
        if hit:
            e = edge - p
            miss = 0
            t_line = next_abs + e * 0.25
            next_abs += period + kp * e
            period += ki * e
        else:
            miss += 1
            t_line = next_abs
            next_abs += period
        i0 = int(round(p))
        # half-line low counts for the vertical sync detector
        h = int(period / 2)
        lo1 = 0
        lo2 = 0
        for i in range(i0, i0 + h):
            if y[i] < th:
                lo1 += 1
        for i in range(i0 + h, i0 + 2 * h):
            if y[i] < th:
                lo2 += 1
        flag = 0
        if lo1 > 0.6 * h:
            flag |= 1
        if lo2 > 0.6 * h:
            flag |= 2
        # levels (only on normal lines: a sync pulse and a back porch)
        tip_m = float(np.mean(y[i0 + tip_a:i0 + tip_b]))
        cl = float(np.mean(y[i0 + cl_a:i0 + cl_b]))
        if hit and flag == 0 and lo1 < 0.25 * h:
            tip += level_a * (tip_m - tip)
            blank += level_a * (cl - blank)
        lines_t[n_lines] = t_line
        hits[n_lines] = hit
        broad_half[n_lines] = flag
        tips[n_lines] = tip_m
        blanks[n_lines] = cl
        n_lines += 1
    st[ST_PERIOD] = period
    st[ST_NEXT] = next_abs
    st[ST_TIP] = tip
    st[ST_BLANK] = blank
    st[ST_MISS] = miss
    return n_lines


@njit(cache=True, fastmath=True, nogil=True)
def find_vsync(lines_flags, nlines, st, min_gap, ev_half):
    """Scan line flags (bit0/bit1 = broad half line) for the start of a vertical sync. The start
    is reported as a half-line index relative to the first line of this batch (may be negative:
    the broad pulses can begin in the previous batch)."""
    run = int(st[ST_RUN])
    since = int(st[ST_SINCE_V])
    ev = 0
    for i in range(nlines):
        for h in range(2):
            since += 1
            if (lines_flags[i] >> h) & 1:
                run += 1
                if run == 3 and since >= min_gap and ev < EV_MAX:
                    ev_half[ev] = 2 * i + h - 2
                    ev += 1
                    since = 0
            else:
                run = 0
    st[ST_RUN] = run
    st[ST_SINCE_V] = since
    return ev


@njit(cache=True, fastmath=True, nogil=True)
def _interp_row(z, start, step, width, out):
    """Catmull-Rom interpolation of z at start + k*step for k in range(width)."""
    n = len(z)
    for k in range(width):
        p = start + k * step
        if p < 1:
            p = 1.0
        i = int(p)
        if i >= n - 2:
            i = n - 3
        t = p - i
        t2 = t * t
        t3 = t2 * t
        a = -0.5 * z[i - 1] + 1.5 * z[i] - 1.5 * z[i + 1] + 0.5 * z[i + 2]
        b = z[i - 1] - 2.5 * z[i] + 2.0 * z[i + 1] - 0.5 * z[i + 2]
        c = -0.5 * z[i - 1] + 0.5 * z[i + 1]
        out[k] = a * t3 + b * t2 + c * t + z[i]


# chroma decoder state
CS_THETA = 0        # reference phase, cycles
CS_RATE = 1         # reference phase advance per line beyond nominal, cycles (frequency error)
CS_BRE = 2          # burst phasor average
CS_BIM = 3
CS_ACC = 4          # mean burst magnitude
CS_VOTE = 5         # PAL switch vote
CS_LOCK = 6         # burst coherence 0..1
CS_LEN = 7


@njit(cache=True, fastmath=True, nogil=True)
def measure_burst(v, n0, t, cs, fsc_fs, line_len, burst_a, burst_b, line_no, is_pal, loop):
    """Measure the colour burst of the line starting at time t (samples, relative to v[0]) and
    update the burst loop. Returns the PAL switch sign for this line."""
    i0 = int(t)
    if i0 < 0 or i0 + burst_b + 2 >= len(v):
        return 1.0
    w = 2.0 * np.pi * (fsc_fs + cs[CS_RATE] / line_len)
    phase = (i0 + burst_a + n0) * fsc_fs + cs[CS_THETA]
    phase -= np.floor(phase)
    cr = np.cos(2.0 * np.pi * phase)
    ci = -np.sin(2.0 * np.pi * phase)
    wr = np.cos(w)
    wi = -np.sin(w)
    re = 0.0
    im = 0.0
    for k in range(burst_a, burst_b):
        s = v[i0 + k]
        re += s * cr
        im += s * ci
        nr = cr * wr - ci * wi
        ci = cr * wi + ci * wr
        cr = nr
    g = 2.0 / (burst_b - burst_a)
    re *= g
    im *= g
    bav_re = cs[CS_BRE] + 0.08 * (re - cs[CS_BRE])
    bav_im = cs[CS_BIM] + 0.08 * (im - cs[CS_BIM])
    cs[CS_BRE] = bav_re
    cs[CS_BIM] = bav_im
    mag = np.sqrt(re * re + im * im)
    cs[CS_ACC] += 0.05 * (mag - cs[CS_ACC])
    coh = np.sqrt(bav_re * bav_re + bav_im * bav_im)
    lock_est = coh / cs[CS_ACC] if cs[CS_ACC] > 1e-9 else 0.0
    cs[CS_LOCK] += 0.02 * (lock_est - cs[CS_LOCK])
    if loop:
        # second-order loop steering the mean burst phase to +90 degrees
        err = (np.arctan2(bav_im, bav_re) - np.pi / 2) / (2 * np.pi) if coh > 1e-9 else 0.0
        err -= np.floor(err + 0.5)
        cs[CS_RATE] += 0.002 * err
        cs[CS_THETA] = (cs[CS_THETA] + cs[CS_RATE] + 0.05 * err) % 1.0
    sw = 1.0
    if is_pal:
        alt = 1.0 if line_no % 2 == 0 else -1.0
        # relative to the average the burst sits at -45 / +45 degrees
        side = im * bav_re - re * bav_im
        meas = -1.0 if side > 0 else 1.0
        cs[CS_VOTE] = cs[CS_VOTE] * 0.98 + 0.02 * meas * alt
        sw = alt if cs[CS_VOTE] >= 0 else -alt
    return sw


@njit(cache=True, fastmath=True, nogil=True)
def box_filter(z, out, n, L):
    """Moving average of length L: out[i] = mean(z[i..i+L-1]), i = 0..n-1."""
    acc = 0.0
    for i in range(L):
        acc += z[i]
    out[0] = acc / L
    for i in range(1, n):
        acc += z[i + L - 1] - z[i - 1]
        out[i] = acc / L


@njit(cache=True, fastmath=True, nogil=True)
def decode_lines(v, n0, lines_t, line_nos, rows, nlines, cs, cfg, cos_tab, sin_tab, field_y, field_u, field_v,
                 row_flags, blanks, scale_v, prev_uv):
    """Turn a batch of composite lines into pixels (see the cfg layout in decoder.py).

    The chroma oscillator phase is a function of the absolute sample position only, so hue does
    not depend on line timing jitter. The burst loop is updated once per line, in order.
    Chroma is low-passed by two cascaded moving averages (cheap, and with cfg-chosen lengths
    their nulls land on the 2*fsc mixing product).

    Returns the number of lines processed."""
    fs = cfg[0]
    fsc = cfg[1]
    px0 = cfg[2]
    px_step = cfg[3]
    width = int(cfg[4])
    burst_a = int(cfg[5])
    burst_b = int(cfg[6])
    is_pal = cfg[7] > 0.5
    colour_on = cfg[8] > 0.5
    setup = cfg[9]
    y_gain = cfg[10]
    target = cfg[11]
    span = int(cfg[12])              # working window: burst start .. end of the picture
    line_len = cfg[13]
    use_two = is_pal and cfg[14] > 0.5
    L1 = int(cfg[15])
    L2 = int(cfg[16])
    fsc_fs = fsc / fs
    half_win = (L1 + L2 - 2) // 2
    margin = L1 + L2
    nv = len(v)
    lr = np.empty(span + margin, np.float32)
    z_re = np.empty(span + margin, np.float32)
    z_im = np.empty(span + margin, np.float32)
    box1 = np.empty(span + L2, np.float32)
    box2 = np.empty(span + L2, np.float32)
    c_re = np.empty(span, np.float32)
    c_im = np.empty(span, np.float32)
    tmp = np.empty(width, np.float32)
    ok = 0
    for ln in range(nlines):
        t = lines_t[ln] - n0
        i0 = int(t) + 1
        r = int(rows[ln])
        if i0 + span + margin + 2 >= nv or i0 < 0:
            continue
        row_flags[r] = 1
        blank = blanks[ln]
        scale = scale_v[ln]
        sw = measure_burst(v, n0, t, cs, fsc_fs, line_len, burst_a, burst_b, line_nos[ln], is_pal, True)
        coh = np.sqrt(cs[CS_BRE] * cs[CS_BRE] + cs[CS_BIM] * cs[CS_BIM])
        g_c = target / coh if coh > 1e-6 else 0.0
        if g_c > 16.0:
            g_c = 16.0
        if colour_on:
            # phase of the chroma oscillator at the first sample of this line; table rotation
            # (tab[k] holds exp(j*2*pi*fsc/fs*k) for one line) keeps it vectorised
            rate = cs[CS_RATE] / line_len
            ph = (i0 + n0) * fsc_fs + cs[CS_THETA]
            ph -= np.floor(ph)
            c0 = float(np.cos(2.0 * np.pi * ph))
            s0 = float(np.sin(2.0 * np.pi * ph))
            n_w = span + margin
            if rate == 0.0:
                for k in range(n_w):
                    ct = cos_tab[k]
                    st = sin_tab[k]
                    cr = ct * c0 - st * s0
                    ci = st * c0 + ct * s0
                    z = v[i0 + k]
                    z_re[k] = 2.0 * z * cr
                    z_im[k] = -2.0 * z * ci
            else:
                # frequency correction through a linear phase term within the line
                dr = float(np.cos(2.0 * np.pi * rate))
                di = float(np.sin(2.0 * np.pi * rate))
                rr = 1.0
                ri = 0.0
                for k in range(n_w):
                    ct = cos_tab[k]
                    st = sin_tab[k]
                    cr = ct * c0 - st * s0
                    ci = st * c0 + ct * s0
                    cr2 = cr * rr - ci * ri
                    ci2 = cr * ri + ci * rr
                    z = v[i0 + k]
                    z_re[k] = 2.0 * z * cr2
                    z_im[k] = -2.0 * z * ci2
                    nr = rr * dr - ri * di
                    ri = rr * di + ri * dr
                    rr = nr
            box_filter(z_re, box1, span + L2, L1)
            box_filter(box1, c_re, span, L2)
            box_filter(z_im, box2, span + L2, L1)
            box_filter(box2, c_im, span, L2)
            # re-modulate to subtract the chroma: oscillator delayed by the filter delay
            ph2 = (i0 + half_win + n0) * fsc_fs + cs[CS_THETA]
            ph2 -= np.floor(ph2)
            c0 = float(np.cos(2.0 * np.pi * ph2))
            s0 = float(np.sin(2.0 * np.pi * ph2))
            for k in range(span):
                ct = cos_tab[k]
                st = sin_tab[k]
                cr = ct * c0 - st * s0
                ci = st * c0 + ct * s0
                chroma = c_re[k] * cr - c_im[k] * ci
                lr[k] = v[i0 + k + half_win] - chroma
        else:
            for k in range(span):
                lr[k] = v[i0 + k + half_win]
        # sample position of pixel 0 inside the working window
        p0 = (t + px0) - (i0 + half_win)
        _interp_row(lr, p0, px_step, width, tmp)
        for x in range(width):
            field_y[r, x] = ((tmp[x] - blank) * scale - setup) * y_gain
        if colour_on:
            _interp_row(c_re, p0, px_step, width, tmp)
            for x in range(width):
                vv = tmp[x] * g_c * sw
                if use_two:
                    field_v[r, x] = 0.5 * (vv + prev_uv[1, x])
                    prev_uv[1, x] = vv
                else:
                    field_v[r, x] = vv
            _interp_row(c_im, p0, px_step, width, tmp)
            for x in range(width):
                uu = -tmp[x] * g_c
                if use_two:
                    field_u[r, x] = 0.5 * (uu + prev_uv[0, x])
                    prev_uv[0, x] = uu
                else:
                    field_u[r, x] = uu
        else:
            for x in range(width):
                field_u[r, x] = 0.0
                field_v[r, x] = 0.0
        ok += 1
    return ok


@njit(cache=True, fastmath=True, nogil=True)
def render_rgb(y, u, v, contrast, brightness, sat, colour, rgb):
    """YUV (0..1) float planes to packed RGB bytes (faster than the numpy equivalent)."""
    h, w = y.shape
    for r in range(h):
        for x in range(w):
            yy = y[r, x] * contrast + brightness
            if yy < 0.0:
                yy = 0.0
            elif yy > 1.0:
                yy = 1.0
            if colour:
                rr = yy + v[r, x] * 1.1402509 * sat
                bb = yy + u[r, x] * 2.0325203 * sat
                gg = (yy - 0.299 * rr - 0.114 * bb) * 1.7035775
            else:
                rr = gg = bb = yy
            rgb[r, x, 0] = 255.5 * (1.0 if rr > 1.0 else (0.0 if rr < 0.0 else rr))
            rgb[r, x, 1] = 255.5 * (1.0 if gg > 1.0 else (0.0 if gg < 0.0 else gg))
            rgb[r, x, 2] = 255.5 * (1.0 if bb > 1.0 else (0.0 if bb < 0.0 else bb))
