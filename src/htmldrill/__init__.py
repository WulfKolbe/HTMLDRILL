"""htmldrill — token-economical drill-down toolkit for HTML / live web documents.

A structural twin of pdfdrill/chatdrill: a shallow-first state machine over a
stratified standoff graph. HTML's two base media (the raw HTTP response and the
rendered DOM) are snapshotted; commands start cheap (L0: headers, meta, links,
json-ld) and escalate to a headless render only when the question demands it.
"""
__version__ = "0.1.0"
