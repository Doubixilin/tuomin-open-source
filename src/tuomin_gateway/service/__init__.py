"""Local HTTP service layer for the tuomin gateway (optional ``[serve]`` extra).

Exposes redact / refill / session endpoints on 127.0.0.1 so any local app can
share one neutral desensitization engine, each declaring its own profile and
dictionary. Plaintext and mappings never leave the machine.
"""
