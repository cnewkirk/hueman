"""hueman: declarative, Terraform-style management of Philips Hue lighting.

The package is intentionally split so the pure-logic pieces (config parsing,
circadian math, sun times, plan diffing, motion translation) carry no network
dependency and are unit-testable, while the I/O surface (the CLIP API v2 client
and TLS pinning) is isolated in :mod:`hueman.client` and :mod:`hueman.pin`.
"""

__version__ = "0.1.0"
