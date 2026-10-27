"""Include tests stored beside each tweak in the standard repository test run."""

from pathlib import Path
import unittest


def load_tests(loader, tests, pattern):
    """Discover each independent tweak test directory, including hyphenated names."""
    suite = unittest.TestSuite()
    root = Path(__file__).parents[1]
    for directory in sorted((root / "tweaks").glob("*/tests")):
        suite.addTests(loader.discover(str(directory), pattern="test_*.py",
                                      top_level_dir=str(directory)))
    return suite
