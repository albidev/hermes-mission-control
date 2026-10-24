"""Product-local invite-rooms operation boundary (P1).

No live routes, no HTTP handlers.  The service, peer registry and canonical RPC
live here so there is exactly one implementation of each accepted protocol.
"""