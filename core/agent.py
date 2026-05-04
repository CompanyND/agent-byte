"""
core/agent.py — Zpětná kompatibilita.

Importy z core.agent stále fungují — přesměrují na agents.byte.agent.
Nový kód by měl importovat přímo z agents.byte.agent nebo agents.atlas.agent atd.
"""

from agents.byte.agent import ByteAgent, ByteTask, ByteResponse, get_byte

__all__ = ["ByteAgent", "ByteTask", "ByteResponse", "get_byte"]
