"""LiveBridge: a Remote Script that exposes Live's Object Model over a local socket.

Live calls create_instance() when LiveBridge is selected as a Control Surface.
The import is deferred so the Live-free modules (server, dispatch, handlers)
can be imported and tested outside Live.
"""


def create_instance(c_instance):
    from .bridge import LiveBridge

    return LiveBridge(c_instance)
