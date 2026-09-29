"""Public, discovery-only WebSocket adapter; never derives tradable USD prices."""

from __future__ import annotations

import asyncio
import json
import random
import re
import time

from .store import Store

PUBLIC_WS = "wss://pumpportal.fun/api/data"
ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
SIGNATURE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{64,88}$")


def parse_creation(message: str, received_at: int) -> tuple[str, str, int, str] | None:
    if len(message) > 16_384:
        return None
    try:
        payload = json.loads(message)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get('txType') != 'create':
        return None
    signature, mint = payload.get('signature'), payload.get('mint')
    if not isinstance(signature, str) or not SIGNATURE.fullmatch(signature):
        return None
    if not isinstance(mint, str) or not ADDRESS.fullmatch(mint):
        return None
    # The endpoint has no trusted on-chain timestamp. The first local receipt is the only clock.
    return signature, mint, received_at, message


async def run_discovery(store: Store, url: str = PUBLIC_WS) -> None:
    import websockets

    if url != PUBLIC_WS:
        raise ValueError('Only the public, read-only discovery endpoint is supported')
    backoff = 1.0
    while True:
        try:
            store.feed_status('connecting', int(time.time() * 1000))
            async with websockets.connect(url, open_timeout=10, ping_interval=20,
                                          ping_timeout=20, max_size=16_384) as socket:
                await socket.send(json.dumps({'method': 'subscribeNewToken'}))
                acknowledgement = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
                if (not isinstance(acknowledgement, dict) or
                    not isinstance(acknowledgement.get('message'), str) or
                    'Successfully subscribed to token creation' not in acknowledgement['message']):
                    raise ValueError('Discovery subscription was not acknowledged')
                store.feed_status('connected', int(time.time() * 1000))
                backoff = 1.0
                async for message in socket:
                    received_at = int(time.time() * 1000)
                    if not isinstance(message, str):
                        continue
                    creation = parse_creation(message, received_at)
                    if creation and store.record_discovery(*creation):
                        print(json.dumps({'source': 'PumpPortal', 'mint': creation[1],
                                          'received_at': received_at}), flush=True)
            raise ConnectionError('WebSocket ended')
        except asyncio.CancelledError:
            store.feed_status('stopped', int(time.time() * 1000))
            raise
        except (OSError, ConnectionError, TimeoutError, ValueError, websockets.WebSocketException) as error:
            store.feed_status('reconnecting', int(time.time() * 1000), str(error))
            print(f'Discovery reconnecting: {error}', flush=True)
            await asyncio.sleep(backoff + random.uniform(0, min(backoff, 1)))
            backoff = min(60, backoff * 2)
