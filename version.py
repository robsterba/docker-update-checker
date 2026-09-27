"""Application version information."""
import pathlib

# Candidate locations for the VERSION file:
# - repo root (local dev and Docker image: this module sits at the root)
# - parent directory (fallback for alternative layouts)
VERSION_CANDIDATES = [
    pathlib.Path(__file__).parent / "VERSION",
    pathlib.Path(__file__).parent.parent / "VERSION",
]


def get_version():
    """Read version from the VERSION file. Returns '1.0.0' if not found."""
    for path in VERSION_CANDIDATES:
        try:
            with open(path, 'r') as f:
                version = f.read().strip()
                if version:
                    return version
        except (FileNotFoundError, IOError):
            continue
    # Fallback version
    return "1.0.0"


# Read version at module load time for compatibility
VERSION = get_version()
