#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Backwards-compatible ``RigolScope`` facade over the clean ``RigolDHO800`` driver.

The actual logic lives in ``rigol_dho800.py`` (written straight from the DHO800
Programming Guide). This module keeps the names and call signatures the rest of
the package historically imported -- ``RigolScope``, ``acquire(trace) ->
(voltage, header_bytes)``, ``time_array``,
``displayed_traces``, ``set_trigger_mode``, ``trigger_status``, ``max_samples``,
``screen_dump``, ``get_expanded_name``, and the ``_last_metadata`` cache that
``dimag_main`` reads for its per-shot HDF5 attributes.

Typical usage (unchanged):

    with RigolScope('192.168.7.59') as scope:
        scope.set_trigger_mode('SINGLE')
        # ... poll scope.trigger_status() until 'STOP' ...
        voltage, hdr = scope.acquire('C1')
        t = scope.time_array()
"""

import numpy as np

from .dho800 import RIGOL_WAVEDESC_SIZE, RigolDHO800, RigolScopeError

__all__ = ['RigolScope', 'RigolScopeError', 'RIGOL_WAVEDESC_SIZE']

# Short/long/expanded channel name maps used by the legacy API surface.
_EXPANDED_NAME = {
    'C1': 'Channel1', 'C2': 'Channel2', 'C3': 'Channel3', 'C4': 'Channel4',
    'MATH1': 'Math1', 'MATH2': 'Math2', 'MATH3': 'Math3', 'MATH4': 'Math4',
}


class RigolScope(RigolDHO800):
    """Legacy facade. New code should prefer ``RigolDHO800`` directly."""

    def __init__(self, ipv4_addr, verbose=True, timeout=5000):
        # Legacy ``timeout`` was in milliseconds; RigolDHO800 wants seconds.
        super().__init__(ipv4_addr, port=5555, timeout=timeout / 1000.0, verbose=verbose)
        self.ip_address = ipv4_addr
        self.idn_string = self.idn
        # Per-trace derived metadata from the most recent acquire() (keyed 'C1'..).
        self._last_metadata = {}

    def __repr__(self):
        return f"RigolScope({self.ip_address!r})"

    def __str__(self):
        return (f"Rigol scope at {self.ip_address}\n"
                f"Model: {self.model}\nSerial: {self.serial}\nFirmware: {self.firmware}\n"
                f"Memory depth: {self.max_samples()} pts\n")

    # -- channel-name helpers (legacy names) -------------------------------- #

    def validate_channel(self, Cn):
        return self.channel_name(Cn)

    def validate_trace(self, tr):
        # Accept 'C1'..'C4' / 'CHANnel1'.. / 1..4 ; MATH passes through unchanged.
        s = str(tr).upper()
        if s.startswith('MATH'):
            return s
        return self.short_channel_name(tr)

    def get_expanded_name(self, trace):
        return _EXPANDED_NAME.get(self.validate_trace(trace), str(trace))

    # -- displayed traces --------------------------------------------------- #

    def displayed_traces(self):
        """Analog channels currently on screen, as ('C1', 'C3', ...)."""
        return tuple(self.short_channel_name(ch) for ch in super().displayed_channels())

    # -- memory depth ------------------------------------------------------- #

    def max_samples(self, N=0):
        """Return ``:ACQuire:MDEPth?`` as int. (``N`` kept for signature compat.)"""
        if N and N > 0:
            return int(N)
        return self.memory_depth()

    # -- trigger control ---------------------------------------------------- #

    def set_trigger_mode(self, trigger_mode):
        """Legacy trigger control. Returns the previous ``:TRIGger:STATus?``.

        'SINGLE' arms one acquisition (``:SINGle``); 'STOP' stops (``:STOP``);
        'NORM'/'AUTO' set the trigger sweep mode.
        """
        try:
            prev = self.trigger_status()
        except Exception:
            prev = ''
        m = str(trigger_mode).strip().upper()
        if m == 'STOP':
            self.stop()
        elif m in ('SINGLE', 'SING'):
            self.single()
        elif m.startswith('NORM'):
            self.set_sweep('NORMal')
        elif m == 'AUTO':
            self.set_sweep('AUTO')
        else:
            raise ValueError(f"unsupported trigger mode: {trigger_mode!r}")
        return prev

    # -- acquisition -------------------------------------------------------- #

    def acquire(self, trace, seg=0, raw=False):
        """Read one trace. Returns ``(voltage_ndarray_float32, header_bytes)``.

        With ``raw=True`` the first element is the uint8 ADC-code array instead
        of converted volts. ``header_bytes`` is a 10-field compatibility string
        derived from scale/offset metadata, suitable for the HDF5 ``V256``
        header field.
        """
        short = self.short_channel_name(trace)
        w = self.read_channel(self.channel_name(trace), fmt='BYTE')

        metadata = dict(w.metadata)
        self._last_metadata[short] = metadata
        header_bytes = self._metadata_header_bytes(metadata)
        data = w.raw if raw else w.voltage.astype(np.float32)
        return data, header_bytes

    @staticmethod
    def _metadata_header_bytes(metadata):
        """Encode derived metadata in the historical 10-field header shape."""
        values = (
            int(metadata.get('format', 0)),
            int(metadata.get('type', 1)),
            int(metadata['points']),
            int(metadata.get('count', 1)),
            float(metadata['x_increment']),
            float(metadata['x_origin']),
            float(metadata.get('x_reference', 0.0)),
            float(metadata['y_increment']),
            float(metadata['y_origin']),
            float(metadata['y_reference']),
        )
        return (
            f"{values[0]},{values[1]},{values[2]},{values[3]},"
            f"{values[4]:.10e},{values[5]:.10e},{values[6]:.10e},"
            f"{values[7]:.10e},{values[8]:.10e},{values[9]:.10e}"
        ).encode('utf-8')

    # -- time axes ---------------------------------------------------------- #

    def time_array(self):
        """Time axis (s, rel. trigger) for the most recent ``acquire()``.

        Built from the cached metadata of the last-acquired trace so it matches
        that read exactly. If nothing has been acquired yet, falls back to the
        same MAX/Stop formula ``read_channel`` uses: ``x_increment = 1/SampleRate``,
        ``x_origin = -(N/2)*x_increment - timebase_offset`` over ``N = memory_depth``.
        """
        if self._last_metadata:
            md = next(reversed(self._last_metadata.values()))
            n = int(md['points'])
            return md['x_origin'] + np.arange(n, dtype=np.float64) * md['x_increment']
        n = self.memory_depth()
        x_increment = 1.0 / self.sample_rate()
        x_origin = -(n / 2.0) * x_increment - self.timebase_offset()
        return x_origin + np.arange(n, dtype=np.float64) * x_increment

    # -- screenshot --------------------------------------------------------- #

    def screen_dump(self, fig_name='Rigol Screen', white_background=False,
                    png_fn='rigol_screen_dump.png', show_plot=True):
        """Capture the display to ``png_fn`` and (optionally) show it. Returns the path."""
        try:
            path = self.screen_png(png_fn)
        except Exception as exc:
            print(f"Screen capture failed: {exc}")
            return None
        if show_plot:
            try:
                import matplotlib.image as mpimg
                import matplotlib.pyplot as plt
                img = mpimg.imread(path)
                plt.figure(fig_name, figsize=(12, 8))
                plt.imshow(img)
                plt.axis('off')
                plt.title(f"{self.model} screen capture")
                plt.tight_layout()
                plt.show()
            except Exception as exc:
                print(f"Error displaying screenshot: {exc}")
        return path
