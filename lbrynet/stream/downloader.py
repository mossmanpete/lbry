import os
import asyncio
import typing
import logging
import binascii
from lbrynet.blob.blob_file import BlobFile, MAX_BLOB_SIZE
from lbrynet.blob.blob_manager import BlobFileManager
from lbrynet.stream.descriptor import StreamDescriptor
from lbrynet.stream.assembler import StreamAssembler
from lbrynet.stream.peer_finder import StreamPeerFinder
from lbrynet.dht.node import Node
if typing.TYPE_CHECKING:
    from lbrynet.peer import Peer

log = logging.getLogger(__name__)


class AsyncGeneratorJunction:
    """
    A helper to interleave the results from multiple async generators into one
    async generator.
    """

    def __init__(self, loop: asyncio.BaseEventLoop, queue: typing.Optional[asyncio.Queue] = None):
        self.loop = loop
        self.queue = queue or asyncio.Queue(loop=loop)
        self.handles: typing.List[asyncio.TimerHandle] = []
        self.running_iterators: typing.Dict[typing.AsyncGenerator, bool] = {}

    @property
    def running(self):
        return any(self.running_iterators.values())

    def add_generator(self, async_gen: typing.AsyncGenerator):
        """
        Add an async generator. This can be called during an iteration of the generator junction.
        """

        async def iterate(iterator: typing.AsyncGenerator):
            async for item in iterator:
                self.queue.put_nowait(item)
            self.running_iterators[iterator] = False

        self.running_iterators[async_gen] = True
        self.handles.append(self.loop.call_soon(lambda : self.loop.create_task(iterate(async_gen))))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.running:
            return await self.queue.get()
        else:
            raise StopAsyncIteration()


class BaseStreamDownloader(StreamAssembler):
    async def download(self, finished_callback) -> None:
        raise NotImplementedError()

    def stop(self):
        raise NotImplementedError()


class SinglePeerStreamDownloader(BaseStreamDownloader):
    def __init__(self, loop: asyncio.BaseEventLoop, blob_manager: BlobFileManager, peer: 'Peer', sd_hash: str,
                 peer_timeout: int, peer_connect_timeout: int, output_dir: typing.Optional[str] = None,
                 output_file_name: typing.Optional[str] = None):
        super().__init__(loop, blob_manager, sd_hash)
        self.peer = peer
        self.peer_timeout = peer_timeout
        self.peer_connect_timeout = peer_connect_timeout
        self.download_task: asyncio.Task = None
        self.output_dir = output_dir or os.getcwd()
        self.output_file_name = output_file_name

    async def get_blob(self, blob_hash: str, length: typing.Optional[int] = None) -> 'BlobFile':
        blob = self.blob_manager.get_blob(blob_hash, length)
        if not blob.get_is_verified():
            await self.peer.request_blobs([blob], self.peer_timeout, self.peer_connect_timeout)
        return blob

    async def download(self, finished_callback):
        try:
            await self.assemble_decrypted_stream(self.output_dir, self.output_file_name)
            log.info(
                "downloaded stream %s -> %s", self.sd_hash, self.output_path
            )
            finished_callback()
        finally:
            self.peer.disconnect_tcp()
            self.stop()


class StreamDownloader(BaseStreamDownloader):
    def __init__(self, loop: asyncio.BaseEventLoop, blob_manager: BlobFileManager, node: Node, sd_hash: str,
                 peer_timeout: int, peer_connect_timeout: int, output_dir: typing.Optional[str] = None,
                        output_file_name: typing.Optional[str] = None):
        super().__init__(loop, blob_manager, sd_hash)
        self.node = node
        self.peer_timeout = peer_timeout
        self.peer_connect_timeout = peer_connect_timeout
        self.download_task: asyncio.Task = None
        self.accumulator_task: asyncio.Task = None
        self.peer_finder = StreamPeerFinder(
            self.loop, self.blob_manager, self.node, self.sd_hash, self.peer_timeout, self.peer_connect_timeout
        )
        self.blobs: typing.Dict[str, 'BlobFile'] = {}
        self.lock = asyncio.Lock(loop=self.loop)
        self.current_blob: 'BlobFile' = None
        self.connections: typing.Set['Peer'] = set()
        self.pending_requests: typing.Dict['Peer', asyncio.Future] = {}
        self.has_peers = asyncio.Event(loop=self.loop)
        self.tasks: typing.List[typing.Coroutine] = []
        self.requests: typing.Dict[str, typing.List['Peer']] = {}

        self.output_dir = output_dir or os.getcwd()
        self.output_file_name = output_file_name

    async def _request_blob(self, peer: 'Peer'):
        log.info("request from %s:%i", peer.address, peer.tcp_port)
        try:
            await peer.request_blobs([self.current_blob], self.peer_timeout, self.peer_connect_timeout)
        except asyncio.TimeoutError:
            await self.lock.acquire()
            try:
                self.connections.remove(peer)
                if not self.connections:
                    self.has_peers.clear()
            finally:
                self.lock.release()

    def _update_requests(self):
        if self.current_blob.blob_hash not in self.requests:
            self.requests[self.current_blob.blob_hash] = []
        for peer in self.connections:
            if peer not in self.requests[self.current_blob.blob_hash]:
                self.requests[self.current_blob.blob_hash].append(peer)
                self.tasks.append(self._request_blob(peer))

    async def get_blob(self, blob_hash: str, length: typing.Optional[int] = None) -> 'BlobFile':
        await self.lock.acquire()
        try:
            self.current_blob = self.blob_manager.get_blob(blob_hash, length)
        finally:
            self.lock.release()

        if self.current_blob.get_is_verified():
            return self.current_blob

        self._update_requests()

        while not self.current_blob.get_is_verified():
            if not self.has_peers.is_set() and self.connections:
                self._update_requests()
            elif self.has_peers.is_set():
                self.has_peers.clear()
                self._update_requests()
                await self.lock.acquire()
                try:
                    self.has_peers.clear()
                finally:
                    self.lock.release()
                self._update_requests()
            elif self.current_blob.get_is_verified():
                return self.current_blob

            if self.tasks:
                tasks = []
                while self.tasks:
                    tasks.append(self.tasks.pop())
                await asyncio.wait(tasks, return_when='FIRST_COMPLETED', loop=self.loop)
            await asyncio.wait([self.has_peers.wait(), self.current_blob.finished_writing.wait()],
                               return_when='FIRST_COMPLETED', loop=self.loop)
            if self.current_blob.get_is_verified():
                return self.current_blob
        return self.current_blob

    async def _accumulate_connections(self):
        async for peer in self.peer_finder:
            connected = await peer.connect_tcp(self.peer_timeout, self.peer_connect_timeout)
            if connected:
                await self.lock.acquire()
                try:
                    self.connections.add(peer)
                    if not self.has_peers.is_set():
                        self.has_peers.set()
                finally:
                    self.lock.release()

    def stop(self):
        if self.accumulator_task and not (self.accumulator_task.done() or self.accumulator_task.cancelled()):
            self.accumulator_task.cancel()
        if self.download_task and not (self.download_task.done() or self.download_task.cancelled()):
            self.download_task.cancel()
        self.peer_finder.stop()
        while self.connections:
            self.connections.pop().disconnect_tcp()

    async def _download(self):
        self.accumulator_task = self.loop.create_task(self._accumulate_connections())
        try:
            await self.assemble_decrypted_stream(self.output_dir, self.output_file_name)
            log.info(
                "downloaded stream %s -> %s", self.sd_hash, self.output_path
            )
            log.info("stream finished")
            await self.blob_manager.storage.change_file_status(
                self.descriptor.stream_hash, 'finished'
            ).asFuture(self.loop)
            self.finished_callback()
        finally:
            self.stop()

    def download(self, finished_callback):
        self.finished_callback = finished_callback
        self.download_task = self.loop.create_task(self._download())
