"""Vendored third-party code bundled with kestrel.

Subpackages here are copies of upstream libraries that have no suitable
pinnable package on PyPI, carried verbatim with their own licences. They are
imported lazily by the plugins that need them so the daemon and the test host
never import the hardware backends.
"""
