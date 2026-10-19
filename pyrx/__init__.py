"""PyRX - a receive-only software-defined radio with an SDR#-style interface."""

import os as _os

# The DSP does many small matrix-vector products; multi-threaded BLAS gains nothing there
# and its spinning worker threads burned >10 cores (measured). Must be set before numpy loads.
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    _os.environ.setdefault(_v, "1")

__version__ = "1.0.0"
