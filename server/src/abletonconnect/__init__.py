"""AbletonConnect MCP server."""

__version__ = "0.1.0"


def main() -> None:
    from .server import main as run

    run()
