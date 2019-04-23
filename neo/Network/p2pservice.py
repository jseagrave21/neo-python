import asyncio
import logging

from neo.Network.neonetwork.common.singleton import Singleton
from neo.Network.neonetwork.ledger import Ledger
from neo.Network.neonetwork.network.message import Message
from neo.Network.neonetwork.network.nodemanager import NodeManager
from neo.Network.neonetwork.network.syncmanager import SyncManager
from neo.Settings import settings

from contextlib import suppress


class NetworkService(Singleton):
    def init(self):
        self.loop = asyncio.get_event_loop()
        self.syncmgr = None
        self.nodemgr = None

    async def start(self):
        Message._magic = settings.MAGIC
        self.nodemgr = NodeManager()
        self.syncmgr = SyncManager(self.nodemgr)
        ledger = Ledger()
        self.syncmgr.ledger = ledger

        logging.getLogger("asyncio").setLevel(logging.DEBUG)
        self.loop.set_debug(False)
        task = asyncio.create_task(self.nodemgr.start())
        task.add_done_callback(lambda _: asyncio.create_task(self.syncmgr.start()))

    async def shutdown(self):
        if self.syncmgr:
            with suppress(asyncio.CancelledError):
                await self.syncmgr.shutdown()

        if self.nodemgr:
            with suppress(asyncio.CancelledError):
                await self.nodemgr.shutdown()

        for task in asyncio.all_tasks():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
