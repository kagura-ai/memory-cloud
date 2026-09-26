"""Whether a ``str`` can be UTF-8 encoded (Issue #1718).

``"\\ud800"`` is a legal JSON escape. ``json.loads`` turns it into a Python
``str`` holding a lone surrogate, and plain ``str`` request fields accept it.
Raw surrogate bytes in a JSON body do the same, because ``json.loads(bytes)``
decodes with ``surrogatepass``: CESU-8 bytes for U+1F600 become the pair
``"\\ud83d\\ude00"`` as two separate code points. ``str.encode()`` then raises
``UnicodeEncodeError`` — from bcrypt, pyotp, a Redis key or the database driver
— which ends a request in a 500. ``os.environ`` and ``getpass`` on a pipe
produce the same kind of ``str`` from a non-UTF-8 byte (``surrogateescape``).

Check untrusted text with :func:`is_utf8_encodable` before it reaches one of
those. Credentials that fail the check are simply wrong credentials: no stored
password, login id or token can contain a surrogate code point.

If you turn a caught ``UnicodeEncodeError`` into another exception instead,
raise it ``from None``: the error's ``.object`` holds the whole input
(see ``normalize_beta_invite_label``, #1595).
"""


def is_utf8_encodable(value: str) -> bool:
    """Return whether ``value`` can be encoded as UTF-8.

    Args:
        value: Any text, typically straight from a request body or the
            environment.

    Returns:
        False when ``value`` holds a surrogate code point (U+D800..U+DFFF),
        alone or as half of a pair stored as two code points, which is the only
        way a ``str`` can fail to encode as UTF-8; True otherwise.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True
