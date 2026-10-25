"""Waveshare e-Paper driver (2.7" family), vendored verbatim.

Only the drivers the e-ink face plugin needs are kept: ``epd2in7_V2`` (current
V2 panel), ``epd2in7`` (original V1), and the shared ``epdconfig`` GPIO/SPI
backend. See ``VENDOR.md`` for provenance and licence.

Importing ``epdconfig`` instantiates a GPIO backend at import time, which pulls
in ``spidev``/``gpiozero`` (or ``Jetson.GPIO`` off a Pi). Import these modules
lazily, inside a ``try/except``, only on a real device — never at module top —
so the build/test host and a HAT-less device stay unaffected.
"""
