"""The 18 workflow nodes as thin functions `(state, runtime) -> state update`.

A node reads artifact ids from the state, loads what it needs, calls an agent, a worker or plain
code, stores the result as a new artifact and returns its id. Nodes never call each other.
"""
