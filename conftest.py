"""Puts the repository root on sys.path for pytest.

The package is not pip-installed in CI, and pytest's prepend import mode only
adds the test file's own directory. Without this file, every test fails at
`import datarail_agents`.
"""
