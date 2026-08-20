# Version comes from installed package metadata (single source: pyproject) —
# a hardcoded string here shipped stale once ("0.1.0" inside the 0.1.1 wheel).
# Lazy module __getattr__ keeps `import rockycode` free of the metadata scan.
def __getattr__(name: str):
    if name == "__version__":
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("rockycode")
        except PackageNotFoundError:  # running from a bare checkout, uninstalled
            return "unknown"
    raise AttributeError(name)
