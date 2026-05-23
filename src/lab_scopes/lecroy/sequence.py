"""Sequence-mode (segmented) waveform math for the LeCroy driver.

These helpers are intentionally pure (no I/O, no scope state) so they can be
unit-tested without a socket and kept physically separate from the real-time
acquisition path in ``scope.py``.

Background (MAUI Remote Control & Automation Manual, pp. 6-19..6-29, 7-152/7-153):
A sequence ``:WAVEFORM?`` transfer with ``SN=0`` returns *all* segments in one
reply. The reply is one WAVEDESC followed by USERTEXT, the TRIGTIME array, the
RISTIME array, and finally ``DATA_ARRAY_1`` holding every segment's samples
concatenated end to end. ``WAVE_ARRAY_1`` is the *total* byte length of that
combined data array (not per-segment), and ``SUBARRAY_COUNT`` is the number of
acquired segments. So::

    total_samples = WAVE_ARRAY_1 / bytes_per_sample
    nsamples_per_segment = total_samples / SUBARRAY_COUNT

The byte offset to the first sample (``ndx0``) is computed by
``LeCroyScope.parse_wavedesc`` and already includes the TRIGTIME array length,
so the data slice lands correctly without any special handling here.

Future enhancement: per-segment trigger offsets. Per the manual (p. 6-29) each
segment has its own horizontal offset ``x[i, n] = HORIZ_INTERVAL * i +
TRIGGER_OFFSET[n]``, where ``TRIGGER_OFFSET[n]`` lives in the TRIGTIME array as
one ``{double TRIGGER_TIME; double TRIGGER_OFFSET}`` pair per segment. The
current driver returns a single shared per-sample time axis (using
``HORIZ_OFFSET``), which is correct for sample *spacing* but does not encode the
per-segment trigger offsets. Decoding the TRIGTIME array would belong here (see
also the ``get_sequence_trigger_times`` stub in ``scope.py``).
"""

from __future__ import annotations


def bytes_per_sample(comm_type: int) -> int:
    """Return the wave-sample width in bytes for a WAVEDESC ``comm_type``.

    ``comm_type`` 1 = word (int16, 2 bytes); 0 = byte (int8, 1 byte).
    """
    if comm_type == 1:
        return 2
    if comm_type == 0:
        return 1
    raise RuntimeError(
        f"**** wd.comm_type = {comm_type}; expected value is either 0 or 1"
    )


def segment_sample_count(wd) -> tuple[int, int]:
    """Return ``(total_samples, nsamples_per_segment)`` for a sequence WAVEDESC.

    ``total_samples`` is the flattened sample count across all segments
    (``WAVE_ARRAY_1 / bytes_per_sample``); ``nsamples_per_segment`` is that
    divided by ``subarray_count``. Raises if the descriptor is not a valid
    multi-segment sequence.
    """
    if wd.subarray_count < 2:
        raise RuntimeError(
            f"**** sequence mode requires subarray_count >= 2, got {wd.subarray_count}"
        )
    total_samples = wd.wave_array_1 // bytes_per_sample(wd.comm_type)
    if total_samples % wd.subarray_count != 0:
        raise RuntimeError(
            f"**** total_samples ({total_samples}) is not divisible by "
            f"subarray_count ({wd.subarray_count}); ragged segment data"
        )
    nsamples = total_samples // wd.subarray_count
    if nsamples == 0:
        raise RuntimeError(
            "**** nsamples per segment = 0 (trace has no data? scope not triggered?)"
        )
    return total_samples, nsamples


def split_segments(flat_data, subarray_count: int, nsamples: int) -> list:
    """Reshape a flat per-sample array into a list of ``subarray_count`` segments."""
    return list(flat_data.reshape(subarray_count, nsamples))
