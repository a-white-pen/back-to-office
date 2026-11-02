"""Optional hook interface; copy to private/research_job_listing/adapters.py.

Implement only employer-specific reads that are explicitly configured privately.
Missing private adapters leave the generic hiring-platform checks available.
"""


def script_shell(url):
    """Return whether a private reader supplies this script-built vacancy page."""
    return False


def read_vacancy(url):
    """Return (text, title, location, mechanism), or None without a matching reader."""
    return


def discover_boards(markup, origin):
    """Return privately configured Board objects recognised in page or bundle text."""
    return []


def search_board(board, listing, calls):
    """Return vacancy candidates for a private board, or None when unsupported."""
    return
