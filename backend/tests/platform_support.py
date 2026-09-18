"""The one platform fact this suite has to branch on.

Almost everything here is platform neutral. The exception is the set of assertions about POSIX
file mode bits, because those bits are the mechanism the application uses to keep the key file,
the databases and the TLS private key readable only by their owner.

Windows carries no such bits. `stat` there synthesises a mode from a single attribute: every
writable file reads back as 0o666 and every read-only one as 0o444, whatever the ACL permits. So
an assertion on those bits cannot hold on Windows, and rewriting it to hold would only be a way
of asserting nothing. Skipping says the true thing: this guarantee is checked where the platform
expresses it, and rests on the profile directory ACL where it does not.

`expirymanager.paths.MODE_BITS_ARE_MEANINGFUL` is the application side of the same fact.
"""

from __future__ import annotations

import pytest

from expirymanager.paths import MODE_BITS_ARE_MEANINGFUL

__all__ = ["MODE_BITS_ARE_MEANINGFUL", "requires_mode_bits"]

requires_mode_bits = pytest.mark.skipif(
    not MODE_BITS_ARE_MEANINGFUL,
    reason="asserts POSIX file mode bits, which this platform does not carry",
)
