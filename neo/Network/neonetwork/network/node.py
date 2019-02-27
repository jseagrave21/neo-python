from neo.Network.neonetwork.network.message import Message
from neo.Network.neonetwork.network.payloads.version import VersionPayload
from neo.Network.neonetwork.network.payloads.getblocks import GetBlocksPayload
from neo.Network.neonetwork.network.payloads.addr import AddrPayload
from neo.Network.neonetwork.network.payloads.networkaddress import NetworkAddressWithTime
from neo.Network.neonetwork.network.payloads.inventory import InventoryPayload, InventoryType
from neo.Network.neonetwork.network.payloads.block import Block
from neo.Network.neonetwork.network.payloads.headers import HeadersPayload
from neo.Network.neonetwork.network.payloads.ping import PingPayload
from neo.Network.neonetwork.core.uint256 import UInt256
from neo.Network.neonetwork.core.header import Header
from neo.Network.neonetwork.network.ipfilter import ipfilter
from neo.Blockchain import GetBlockchain
from datetime import datetime
from typing import Optional, List, TYPE_CHECKING
import asyncio
from contextlib import suppress
from neo.Network.neonetwork.common import msgrouter, encode_base62
from neo.Network.neonetwork.network.nodeweight import NodeWeight
from neo.logging import log_manager
import binascii

logger = log_manager.getLogger('network')

if TYPE_CHECKING:
    from neo.Network.neonetwork.network.nodemanager import NodeManager
    from neo.Network.neonetwork.network.protocol import NeoProtocol


class NeoNode:
    def __init__(self, protocol: 'NeoProtocol', nodemanager: 'NodeManager', quality_check=False):
        self.protocol = protocol
        self.nodemanager = nodemanager
        self.quality_check = quality_check

        self.address = None
        self.nodeid = id(self)
        self.nodeid_human = encode_base62(self.nodeid)
        self.version = None
        self.tasks = []
        self.nodeweight = NodeWeight(self.nodeid)
        self.best_height = 0  # track the block height of node

        self._inv_hash_for_height = None  # temp variable to track which hash we used for determining the nodes best height

    # connection setup and control functions
    async def connection_made(self, transport) -> None:
        addr_tuple = self.protocol._stream_writer.get_extra_info('peername')
        self.address = f"{addr_tuple[0]}:{addr_tuple[1]}"

        if not ipfilter.is_allowed(addr_tuple[0]):
            await self.disconnect()

        # storing the task in case the connection is lost before it finishes the task, this allows us to cancel the task
        task = asyncio.create_task(self.do_handshake())
        self.tasks.append(task)
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=2)
            self.tasks.remove(task)

    async def do_handshake(self) -> None:
        send_version = Message(command='version', payload=VersionPayload(port=10333, userAgent="NEOPYTHON-PLUS-0.0.1"))
        await self.send_message(send_version)

        m = await self.read_message(timeout=3)
        if not m or m.command != 'version':
            await self.disconnect()
            return

        if not self.validate_version(m.payload):
            await self.disconnect()
            return

        m_verack = Message(command='verack')
        await self.send_message(m_verack)

        m = await self.read_message(timeout=3)
        if not m or m.command != 'verack':
            await self.disconnect()
            return

        if self.quality_check:
            self.nodemanager.quality_check_result(self.address, healthy=True)
        else:
            logger.debug(f"Connected to {self.version.user_agent} @ {self.address}: {self.version.start_height}")
            self.nodemanager.add_connected_node(self)
            self.tasks.append(asyncio.create_task(self.run()))

    async def disconnect(self) -> None:
        for t in self.tasks:
            with suppress(asyncio.CancelledError):
                t.cancel()
                await t
        self.nodemanager.remove_connected_node(self)
        self.protocol.disconnect()

    def connection_lost(self, exc) -> None:
        logger.debug(f"{datetime.now()} Connection lost {self.address} excL {exc}")
        for t in self.tasks:
            t.cancel()
        self.nodemanager.remove_connected_node(self)
        if self.quality_check:
            self.nodemanager.quality_check_result(self.address, healthy=False)

    def validate_version(self, data) -> bool:
        try:
            self.version = VersionPayload.deserialize_from_bytes(data)
        except ValueError:
            logger.debug("failed to deserialize Version")
            return False

        if self.version.nonce == self.nodeid:
            logger.debug("Client is self")
            return False

        # update nodes height indicator
        self.best_height = self.version.start_height

        # print("verification OK")
        return True

    async def run(self) -> None:
        logger.debug("Waiting for a message")
        while True:
            # we want to always listen for an incoming message
            message = await self.read_message(timeout=90)
            if not message:
                continue

            if message.command == 'addr':
                addr_payload = AddrPayload.deserialize_from_bytes(message.payload)
                for a in addr_payload.addresses:
                    msgrouter.on_addr(f"{a.address}:{a.port}")
            elif message.command == 'getaddr':
                await self.send_address_list()
            elif message.command == 'inv':
                inv = InventoryPayload.deserialize_from_bytes(message.payload)
                if not inv:
                    return

                if inv.type == InventoryType.block:
                    # neo-cli broadcasts INV messages on a regular interval. We can use those as trigger to request their latest block height
                    # supported from 2.10.0.1 onwards
                    if len(inv.hashes) > 0:
                        m = Message(command='ping', payload=PingPayload(GetBlockchain().Height))
                        await self.send_message(m)
                        # self._inv_hash_for_height = inv.hashes[-1]
                        # await self.get_data(inv.type, inv.hashes)
                    elif inv.type == InventoryType.consensus:
                        pass
                elif inv.type == InventoryType.tx:
                    pass
            elif message.command == 'block':
                block = Block.deserialize_from_bytes(message.payload)
                if block:
                    if self._inv_hash_for_height == block.hash and block.index > self.best_height:
                        logger.debug(f"Updating node {self.nodeid_human} height from {self.best_height} to {block.index}")
                        self.best_height = block.index
                        self._inv_hash_for_height = None

                    await msgrouter.on_block(self.nodeid, block, message.payload)
            elif message.command == 'headers':
                header_payload = HeadersPayload.deserialize_from_bytes(message.payload)

                if header_payload and len(header_payload.headers) > 0:
                    await msgrouter.on_headers(self.nodeid, header_payload.headers)
            elif message.command == 'pong':
                payload = PingPayload.deserialize_from_bytes(message.payload)
                if payload:
                    logger.debug(f"Updating node {self.nodeid_human} height from {self.best_height} to {payload.current_height}")
                    self.best_height = payload.current_height
                    self._inv_hash_for_height = None
            elif message.command == 'getdata':
                inv = InventoryPayload.deserialize_from_bytes(message.payload)
                if not inv:
                    return

                for h in inv.hashes:
                    item = self.nodemanager.relay_cache.try_get(h)
                    if item is None:
                        # for the time being we only support data retrieval for our own relays
                        continue
                    if inv.type == InventoryType.tx:
                        raw_payload = binascii.unhexlify(item.ToArray())
                        m = Message(command='tx', payload=raw_payload)  # this is still an old code base InventoryMixin type
                        await self.send_message(m)
            else:
                if message.command not in ['consensus', 'getheaders']:
                    logger.debug(f"Message with command: {message.command}")

    # raw network commands
    async def get_address_list(self) -> None:
        """ Send a request for receiving known addresses"""
        m = Message(command='getaddr')
        await self.send_message(m)

    async def send_address_list(self) -> None:
        """ Send our known addresses """
        known_addresses = []
        for node in self.nodemanager.nodes:
            host, port = node.address.split(':')
            if host and port:
                known_addresses.append(NetworkAddressWithTime(address=host, port=int(port)))
        if len(known_addresses) > 0:
            m = Message(command='address', payload=AddrPayload(addresses=known_addresses))
            await self.send_message(m)

    async def get_headers(self, hash_start: UInt256, hash_stop: Optional[UInt256] = None) -> None:
        """ Send a request for headers from `hash_start` + 1 to `hash_stop`

            Not specifying a `hash_stop` results in requesting at most 2000 headers.
        """
        m = Message(command='getheaders', payload=GetBlocksPayload(hash_start, hash_stop))
        await self.send_message(m)

    async def send_headers(self, headers: List[Header]) -> None:
        """ Send a list of Header objects.

            This is usually done as a response to a 'getheaders' request.
        """
        if len(headers) > 2000:
            headers = headers[:2000]

        m = Message(command='headers', payload=HeadersPayload(headers))
        await self.send_message(m)

    async def get_blocks(self, hash_start: UInt256, hash_stop: Optional[UInt256] = None) -> None:
        """ Send a request for blocks from `hash_start` + 1 to `hash_stop`

            Not specifying a `hash_stop` results in requesting at most 500 blocks.
        """
        m = Message(command='getblocks', payload=GetBlocksPayload(hash_start, hash_stop))
        await self.send_message(m)

    async def get_data(self, type: InventoryType, hashes: List[UInt256]) -> None:
        """ Send a request for receiving the specified inventory data."""
        if len(hashes) < 1:
            return

        m = Message(command='getdata', payload=InventoryPayload(type, hashes))
        await self.send_message(m)

    async def relay(self, inventory) -> bool:
        """
        Try to relay the inventory to the network

        Args:
            inventory: should be of type Block, Transaction or ConsensusPayload (see: InventoryType) 

        Returns: False if inventory is already in the mempool, or if relaying to nodes failed (e.g. because we have no nodes connected)

        """
        # TODO: this is based on the current/old neo-python Block, Transaction and ConsensusPlayload classes
        #  meaning attribute naming will change (no longer camelCase) once we move to python naming convention
        #  for now we need to convert them to our new types or calls will fail
        new_inventorytype = InventoryType(inventory.InventoryType)
        new_hash = UInt256(data=inventory.Hash.ToArray())
        inv = InventoryPayload(type=new_inventorytype, hashes=[new_hash])
        m = Message(command='inv', payload=inv)
        await self.send_message(m)

        return True

    # utility functions
    async def send_message(self, message: Message) -> None:
        await self.protocol.send_message(message)

    async def read_message(self, timeout: int = 30) -> Message:
        return await self.protocol.read_message(timeout)

    def __eq__(self, other):
        if type(other) is type(self):
            return self.address == other.address and self.nodeid == other.nodeid
        else:
            return False
