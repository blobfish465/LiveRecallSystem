"""LiveRecall server internals.

state.py   - revision state + bounded history of finalized captions (no I/O)
channel.py - per-client outbound queue + sender task (backpressure lives here)

solution_server.py wires these to real WebSocket connections.
"""
