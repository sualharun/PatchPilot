"""Compatibility imports for Python project detection."""

from .infrastructure.repository.python_project import (
    PYTHON_PROJECT_MARKERS,
    detect_install_commands,
    detect_test_commands,
    summarize_project,
)

__all__ = ["PYTHON_PROJECT_MARKERS", "detect_install_commands", "detect_test_commands", "summarize_project"]
