"""Independently testable checks used by the publication release gate."""

from .runtime_hygiene import check_runtime_hygiene

__all__ = ["check_runtime_hygiene"]
