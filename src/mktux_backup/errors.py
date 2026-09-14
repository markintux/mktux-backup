"""Domain errors exposed by the command line interface."""


class BackupError(Exception):
    """Base error for expected application failures."""


class ConfigError(BackupError):
    """The project configuration could not be loaded or validated."""


class LockError(BackupError):
    """Another backup run owns the execution lock."""


class StorageError(BackupError):
    """The destination or staging area could not be used safely."""


class TransferError(BackupError):
    """A remote file operation failed."""


class DatabaseError(BackupError):
    """MySQL inspection or dump failed."""


class VerificationError(BackupError):
    """A completed backup failed an integrity check."""


class BackupCancelled(BackupError):
    """The current run was cancelled cooperatively by the user."""
