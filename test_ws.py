import asyncio
import websockets

async def test():
    try:
        ws = await websockets.connect("wss://base-mainnet.g.alchemy.com/v2/M9viWxQrKCvbgvOTim2Qd")
        print("CONNECTED OK")
        await ws.close()
    except Exception as e:
        print(f"FAILED: {e}")

asyncio.run(test())