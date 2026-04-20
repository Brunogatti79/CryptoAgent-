"""
integrations/claude_client_async.py
──────────────────────────────────
Wrapper async para brain.analyze() y brain.analyze_group_b().

El cliente de Anthropic (httpx síncrono) se ejecuta en un thread pool
via loop.run_in_executor para no bloquear el event loop de asyncio.
"""

import asyncio
import logging
from functools import partial

import brain

log = logging.getLogger(__name__)


async def analyze_async(market_context: str, regime_context: str = "") -> dict:
    """
    Versión async de brain.analyze().
    Retorna el mismo dict: {raw_response, signals, input_tokens, output_tokens}
    """
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            partial(brain.analyze, market_context, regime_context),
        )
        log.info(
            f"[Claude] Grupo A: {len(result['signals'])} señales | "
            f"tokens in={result['input_tokens']} out={result['output_tokens']}"
        )
        return result
    except Exception as e:
        log.error(f"[Claude] Error en analyze_async: {e}")
        return {"raw_response": "", "signals": [], "input_tokens": 0, "output_tokens": 0}


async def analyze_group_b_async(market_context: str) -> dict:
    """
    Versión async de brain.analyze_group_b().
    """
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            partial(brain.analyze_group_b, market_context),
        )
        log.info(
            f"[Claude] Grupo B: {len(result['signals'])} señales | "
            f"tokens in={result['input_tokens']} out={result['output_tokens']}"
        )
        return result
    except Exception as e:
        log.error(f"[Claude] Error en analyze_group_b_async: {e}")
        return {"raw_response": "", "signals": [], "input_tokens": 0, "output_tokens": 0}
