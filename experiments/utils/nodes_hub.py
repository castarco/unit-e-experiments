#!/usr/bin/env python3

# Copyright (c) 2018 The Unit-e developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.


from asyncio import (
    AbstractEventLoop,
    Protocol,
    AbstractServer,
    Task,
    Transport,
    gather,
    sleep as asyncio_sleep
)
from logging import getLogger
from struct import pack, unpack
from typing import (
    Dict,
    List,
    Optional,
    Tuple
)

from test_framework.messages import hash256
from test_framework.test_node import TestNode
from test_framework.util import p2p_port


MSG_HEADER_LENGTH = 4 + 12 + 4 + 4
VERSION_PORT_OFFSET = 4 + 8 + 8 + 26 + 8 + 16


logger = getLogger('TestFramework.nodes_hub')


class NodesHub:
    """
    A central hub to connect all the nodes at test/simulation time. It has many
    purposes:
      - Give us the ability to capture & analyze traffic
      - Give us the ability to add arbitrary delays/latencies between any node

    The hub will open many ports at the same time to handle inbound connections,
    one for each node. When a node A wants to send a message to node B, the
    message will travel through the hub (H hereinafter). So, if A wants to be
    connected to B and C, it will establish two connections to H (every open
    port of H will represent a specific node), and H will establish one new
    connection to B, and another one to C.

    In this class, we refer to the nodes through their index in the self.nodes
    property.
    """

    def __init__(
            self,
            loop: AbstractEventLoop,
            nodes: List[TestNode],
            host: str = '127.0.0.1'
    ):
        self.loop = loop
        self.nodes = nodes

        self.host = host

        # This allows us to specify asymmetric delays
        self.node2node_delays: Dict[Tuple[int, int], float] = {}

        self.proxy_servers: List[AbstractServer] = []
        self.relay_tasks: Dict[Tuple[int, int], Task] = {}
        self.ports2node_map: Dict[int, int] = {}

        self.sender2proxy_transports: Dict[Tuple[int, int], Transport] = {}
        self.proxy2receiver_transports: Dict[Tuple[int, int], Transport] = {}

        # Lock-like object used by NodesHub.connect_nodes
        self.pending_connection: Optional[Tuple[int, int]] = None

    def sync_start_proxies(self):
        """
        This method creates (& starts) a listener proxy for each node, the
        connections from each proxy to the real node that they represent will be
        done whenever a node connects to the proxy.

        It starts the nodes's proxies.
        """
        for node_id in range(len(self.nodes)):
            self.ports2node_map[self.get_node_port(node_id)] = node_id
            self.ports2node_map[self.get_proxy_port(node_id)] = node_id

        self.proxy_servers = self.loop.run_until_complete(gather(*[
            self.loop.create_server(
                protocol_factory=lambda: NodeProxy(hub_ref=self),
                host=self.host,
                port=self.get_proxy_port(node_id)
            )
            for node_id, node in enumerate(self.nodes)
        ]))

    def sync_biconnect_nodes_as_linked_list(self, nodes_list=None):
        """
        Helper to make easier using NodesHub in non-asyncio aware code.
        Connects nodes as a linked list.
        """
        if nodes_list is None:
            nodes_list = range(len(self.nodes))

        if 0 == len(nodes_list):
            return

        connection_futures = []

        for i, j in zip(nodes_list, nodes_list[1:]):
            connection_futures.append(self.connect_nodes(i, j))
            connection_futures.append(self.connect_nodes(j, i))

        self.loop.run_until_complete(gather(*connection_futures))

    def sync_connect_nodes(self, graph_edges: set):
        """
        Helper to make easier using NodesHub in non-asyncio aware code. Allows
        to setup a network given an arbitrary graph (in the form of edges set).
        """
        self.loop.run_until_complete(
            gather(*[self.connect_nodes(i, j) for (i, j) in graph_edges])
        )

    @staticmethod
    def get_node_port(node_idx):
        return p2p_port(node_idx)

    def get_proxy_port(self, node_idx):
        return p2p_port(len(self.nodes) + 1 + node_idx)

    def get_proxy_address(self, node_idx):
        return '%s:%s' % (self.host, self.get_proxy_port(node_idx))

    def set_nodes_delay(self, outbound_idx, inbound_idx, delay):
        # delay is measured in seconds
        if delay == 0:
            self.node2node_delays.pop((outbound_idx, inbound_idx), None)
        else:
            self.node2node_delays[(outbound_idx, inbound_idx)] = delay

    def disconnect_nodes(self, outbound_idx, inbound_idx):
        sender2proxy_transport: Transport = self.sender2proxy_transports.get(
            (outbound_idx, inbound_idx), None
        )
        proxy2receiver_transport: Transport = self.proxy2receiver_transports.get(
            (outbound_idx, inbound_idx), None
        )
        relay_task: Task = self.relay_tasks.get(
            (outbound_idx, inbound_idx), None
        )

        if sender2proxy_transport is not None and not sender2proxy_transport.is_closing():
            sender2proxy_transport.close()

        if proxy2receiver_transport is not None and not proxy2receiver_transport.is_closing():
            proxy2receiver_transport.close()

        if relay_task is not None and not relay_task.cancelled():
            relay_task.cancel()

        # Removing references
        self.sender2proxy_transports.pop((outbound_idx, inbound_idx), None)
        self.proxy2receiver_transports.pop((outbound_idx, inbound_idx), None)
        self.relay_tasks.pop((outbound_idx, inbound_idx), None)

    async def connect_nodes(self, outbound_idx: int, inbound_idx: int):
        """
        :param outbound_idx: Refers the "sender" (asking for a new connection)
        :param inbound_idx: Refers the "receiver" (listening for new connections)
        """

        # We have to wait until all the proxies are configured and listening
        while len(self.proxy_servers) < len(self.nodes):
            await asyncio_sleep(0)

        # We have to be sure that all the previous calls to connect_nodes have
        # finished. Because we are using cooperative scheduling we don't have to
        # worry about race conditions, this while loop is enough.
        while self.pending_connection is not None:
            await asyncio_sleep(0)

        # We acquire the lock. This tuple is also useful for the NodeProxy
        # instance.
        self.pending_connection = (outbound_idx, inbound_idx)

        if (
                self.pending_connection in self.sender2proxy_transports or
                self.pending_connection in self.proxy2receiver_transports
        ):
            raise RuntimeError(
                'Connection (node%s --> node%s) already established' %
                self.pending_connection[:]
            )

        self.connect_sender_to_proxy(*self.pending_connection)
        self.connect_proxy_to_receiver(*self.pending_connection)

        # We wait until we know that all the connections have been created
        while (
                self.pending_connection not in self.sender2proxy_transports or
                self.pending_connection not in self.proxy2receiver_transports
        ):
            await asyncio_sleep(0)

        self.pending_connection = None  # We release the lock

    def connect_sender_to_proxy(self, outbound_idx, inbound_idx):
        """
        Establishes a connection between a real node and the proxy representing
        another node
        """
        sender_node = self.nodes[outbound_idx]
        proxy_address = self.get_proxy_address(inbound_idx)

        # Add the proxy to the outgoing connections list
        sender_node.addnode(proxy_address, 'add')
        # Connect to the proxy. Will trigger NodeProxy.connection_made
        sender_node.addnode(proxy_address, 'onetry')

    def connect_proxy_to_receiver(self, outbound_idx, inbound_idx):
        """
        Creates a sender that connects to a node and relays messages between
        that node and its associated proxy
        """

        relay_coroutine = self.loop.create_connection(
            protocol_factory=lambda c2sp=self.pending_connection: ProxyRelay(
                hub_ref=self, sender2receiver_pair=c2sp
            ),
            host=self.host,
            port=self.get_node_port(inbound_idx)
        )
        self.relay_tasks[(outbound_idx, inbound_idx)] = self.loop.create_task(
            relay_coroutine
        )

    def process_buffer(self, buffer, transport: Transport):
        """
        This function helps the hub to impersonate nodes by modifying 'version'
        messages changing the "from" addresses.
        """

        # We do nothing until we have (magic + command + length + checksum)
        while len(buffer) > MSG_HEADER_LENGTH:

            # We only care about command & msglen
            msglen = unpack("<i", buffer[4 + 12:4 + 12 + 4])[0]

            # We wait until we have the full message
            if len(buffer) < MSG_HEADER_LENGTH + msglen:
                return

            command = buffer[4:4 + 12].split(b'\x00', 1)[0]
            logger.debug('Processing command %s' % str(command))

            if b'version' == command:
                msg = buffer[MSG_HEADER_LENGTH:MSG_HEADER_LENGTH + msglen]

                node_port: int = unpack(
                    '!H', msg[VERSION_PORT_OFFSET:VERSION_PORT_OFFSET + 2]
                )[0]
                if node_port != 0:
                    proxy_port = self.get_proxy_port(self.ports2node_map[node_port])
                else:
                    proxy_port = 0  # The node is not listening for connections

                msg = (
                    msg[:VERSION_PORT_OFFSET] +
                    pack('!H', proxy_port) +
                    msg[VERSION_PORT_OFFSET + 2:]
                )

                msg_checksum = hash256(msg)[:4]  # Truncated double sha256
                new_header = buffer[:MSG_HEADER_LENGTH - 4] + msg_checksum

                transport.write(new_header + msg)
            else:
                # We pass an unaltered message
                transport.write(buffer[:MSG_HEADER_LENGTH + msglen])

            buffer = buffer[MSG_HEADER_LENGTH + msglen:]

        return buffer


class NodeProxy(Protocol):
    def __init__(self, hub_ref):
        self.hub_ref = hub_ref
        self.sender2receiver_pair = None
        self.recvbuf = b''

    def connection_made(self, transport):
        self.sender2receiver_pair = self.hub_ref.pending_connection

        logger.debug(
            'Client %s connected to proxy %s' % self.sender2receiver_pair[:]
        )
        self.hub_ref.sender2proxy_transports[self.sender2receiver_pair] = transport

    def connection_lost(self, exc):
        logger.debug(
            'Lost connection between sender %s and proxy %s' %
            self.sender2receiver_pair[:]
        )
        self.hub_ref.disconnect_nodes(*self.sender2receiver_pair)

    def data_received(self, data):
        self.hub_ref.loop.create_task(self.__handle_received_data(data))

    async def __handle_received_data(self, data):
        while self.sender2receiver_pair not in self.hub_ref.proxy2receiver_transports:
            # We can't relay the data yet, we need a connection on the other side
            await asyncio_sleep(0)

        if self.sender2receiver_pair in self.hub_ref.node2node_delays:
            await asyncio_sleep(
                self.hub_ref.node2node_delays[self.sender2receiver_pair]
            )

        if len(data) > 0:
            logger.debug(
                'Proxy connection %s received %s bytes' %
                (repr(self.sender2receiver_pair), len(data))
            )
            self.recvbuf += data
            self.recvbuf = self.hub_ref.process_buffer(
                buffer=self.recvbuf,
                transport=self.hub_ref.proxy2receiver_transports[self.sender2receiver_pair]
            )


class ProxyRelay(Protocol):
    def __init__(self, hub_ref, sender2receiver_pair):
        self.hub_ref = hub_ref
        self.sender2receiver_pair = sender2receiver_pair
        self.receiver2sender_pair = sender2receiver_pair[::-1]
        self.recvbuf = b''

    def connection_made(self, transport):
        logger.debug(
            'Created connection between proxy and its associated node %s to receive messages from node %s' %
            self.receiver2sender_pair
        )
        self.hub_ref.proxy2receiver_transports[self.sender2receiver_pair] = transport

    def connection_lost(self, exc):
        logger.debug(
            'Lost connection between proxy and its associated node %s to receive messages from node %s' %
            self.receiver2sender_pair
        )
        self.hub_ref.disconnect_nodes(*self.sender2receiver_pair)

    def data_received(self, data):
        self.hub_ref.loop.create_task(self.__handle_received_data(data))

    async def __handle_received_data(self, data):
        while self.sender2receiver_pair not in self.hub_ref.sender2proxy_transports:
            # We can't relay the data yet, we need a connection on the other side
            await asyncio_sleep(0)

        if self.receiver2sender_pair in self.hub_ref.node2node_delays:
            await asyncio_sleep(
                self.hub_ref.node2node_delays[self.receiver2sender_pair]
            )

        if len(data) > 0:
            logger.debug(
                'Proxy relay connection %s received %s bytes' %
                (repr(self.sender2receiver_pair), len(data))
            )
            self.recvbuf += data
            self.recvbuf = self.hub_ref.process_buffer(
                buffer=self.recvbuf,
                transport=self.hub_ref.sender2proxy_transports[self.sender2receiver_pair]
            )
