"""The file-read and file-write boundary.

Two decisions used to be made ad hoc at every call site, and both were made
inconsistently:

- **Reading.** Seven sites opened text files; exactly one passed ``errors=``.
  The other six used a strict codec, so a single byte that is not valid UTF-8 -
  in a voice sample, a hand-edited ``voice.md``, a shell profile - raised
  ``UnicodeDecodeError``. That is a ``ValueError``, not an ``OSError``, so it
  also slipped past the ``except OSError`` guards those call sites did have.
  :func:`read_text` is for *content*, where a mangled character beats a dead
  run. Structured files that must round-trip exactly (the YAML config) stay
  strict and report the encoding themselves - see ``config._read_yaml``.

- **Writing.** ``config``, ``cache`` and ``state`` each hand-rolled temp +
  ``os.replace``, all three using the same ``<target>.tmp`` name, so two
  voicelog runs in one repo collided on the scratch file and either could
  unlink the other's in-flight temp - defeating the last-write-wins guarantee
  all three claimed. Meanwhile ``voicefile``, which owns the only accumulating
  artifact a user cannot regenerate, truncated its target before writing.

:func:`atomic_write_text` **raises**. Failure policy belongs to the caller and
differs by file: the config write propagates so ``--setup`` can print the YAML
to save by hand, the cache warns and continues because it runs after a paid
model call, and the state write is silent because a watermark is a convenience.
"""
from __future__ import annotations

import os
import stat
import tempfile


def read_text(path: str) -> str:
    """Read a *content* file as UTF-8, replacing anything undecodable.

    Raises ``OSError`` if the file cannot be opened at all - absent and
    unreadable are different from mis-encoded, and callers already handle them.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _discard(path: str) -> None:
    """Delete a scratch file, even one that has been made read-only.

    Windows refuses to unlink a read-only file, so a bare unlink here could
    leave behind the very thing this module promises never to leave behind.
    """
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass
    try:
        os.unlink(path)
    except OSError:
        pass


def _default_mode() -> int:
    """What a plain ``open(path, "w")`` would have created, per the umask."""
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def _inherited_mode(path: str) -> int:
    """The mode a rewrite of ``path`` should end up with.

    mkstemp deliberately creates at 0600 and ``os.replace`` carries that
    inode's mode to the destination - so without this, every rewrite silently
    tightened the file. ``voice.md`` is committed and read by other people;
    dropping it to owner-only would lock out a shared checkout or a CI step
    running as another user, for a file that holds nothing secret.
    """
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return _default_mode()


def atomic_write_text(path: str, text: str, *, mode: int | None = None) -> None:
    """Write ``text`` to ``path`` as UTF-8 with LF endings, atomically.

    The content lands in full or not at all: an interrupt, a full disk or an
    encoding failure leaves whatever was already there untouched, and no
    scratch file behind. ``mode`` is applied to the temp file before the
    rename, so the content is never briefly world-readable at the real path.
    Left unset, the file keeps the permissions it already had - or takes the
    process default if it is new - rather than inheriting mkstemp's 0600, which
    would silently tighten every file this touches.

    The temp file is created by ``tempfile.mkstemp`` in the destination
    directory - unique per call, so concurrent writers cannot share it, and on
    the same filesystem, so ``os.replace`` is a rename rather than a copy.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".voicelog-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        wanted = mode if mode is not None else _inherited_mode(path)
        os.replace(tmp, path)
    except BaseException:
        # BaseException, not Exception: a KeyboardInterrupt here would otherwise
        # leave the scratch file behind, and this helper exists to promise it
        # does not. The original is untouched either way - it is only ever
        # replaced by the single os.replace above.
        _discard(tmp)
        raise

    # After the rename, not before. mkstemp creates at 0600 - the tightest mode
    # we ever apply - so the file is at its most restrictive between the two
    # calls and is never briefly wider than intended. Doing it first meant a
    # read-only destination produced a read-only *temp*, whose rename failed and
    # whose cleanup then failed too (Windows will not delete a read-only file),
    # leaking a scratch file holding the whole changelog into the work tree on
    # every run - and the failure holds the watermark back, so it retries
    # forever. Swallowed because the content is already committed at this point;
    # reporting a failed write would be a lie the caller might act on.
    try:
        os.chmod(path, wanted)
    except OSError:
        pass
