#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import io
import time
from typing import Optional

from loguru import logger
from pydantic import BaseModel

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InputTransportMessageFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
    StartFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from asterisk import AsteriskWsFrameSerializer
from pipecat.serializers.base_serializer import FrameSerializer
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.websocket.server import WebsocketServerCallbacks

try:
    import websockets
    from websockets.asyncio.server import serve as websocket_serve
    from websockets.protocol import State
except ModuleNotFoundError as e:
    logger.error(f"Missing module: {e}")
    raise Exception(f"Missing module: {e}")


class AsteriskWSServerParams(TransportParams):
    local_audio_buffer_frames: Optional[int] = 3000
    initial_jitter_buffer_ms: Optional[int] = 80
    max_remote_audio_buffer_frames: Optional[int] = 500
    remote_audio_buffer_resume_threshold: float = 0.5
    port: Optional[int] = 8765
    host: Optional[str] = "localhost"
    serializer: Optional[FrameSerializer] = AsteriskWsFrameSerializer()
    session_timeout: Optional[int] = None


class AsteriskWSServerInputTransport(BaseInputTransport):
    def __init__(
            self,
            transport: BaseTransport,
            params: AsteriskWSServerParams,
            callbacks: WebsocketServerCallbacks,
            **kwargs,
    ):
        super().__init__(params, **kwargs)
        self._params = params
        self._transport = transport
        self._host = self._params.host
        self._port = self._params.port
        self._callbacks = callbacks
        self._websocket: Optional[websockets.WebSocketServerProtocol] = None
        self._server_task = None
        self._session_timer_task = None
        self._stop_server_event = asyncio.Event()
        self._initialized = False

    async def start(self, frame: StartFrame):
        await super().start(frame)
        if self._initialized:
            return
        self._initialized = True
        if self._params.serializer:
            await self._params.serializer.setup(frame)
        if not self._server_task:
            self._server_task = self.create_task(self._server_task_handler())
        await self.set_transport_ready(frame)

    async def _terminate(self, gracefully: bool = False):
        if self._session_timer_task:
            await self.cancel_task(self._session_timer_task)
            self._session_timer_task = None
        if gracefully:
            self._stop_server_event.set()
            if self._server_task:
                await self._server_task
                self._server_task = None
        else:
            if self._server_task:
                await self.cancel_task(self._server_task)
                self._server_task = None

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._terminate(gracefully=True)

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._terminate()

    async def cleanup(self):
        await super().cleanup()
        await self._transport.cleanup()

    async def _server_task_handler(self):
        logger.info(f"Starting websocket server on {self._host}:{self._port}")
        async with websocket_serve(self._client_handler, self._host, self._port) as _:
            await self._callbacks.on_websocket_ready()
            await self._stop_server_event.wait()

    async def _client_handler(self, websocket: websockets.WebSocketServerProtocol):
        logger.info(f"New client connection from {websocket.remote_address}")
        if self._websocket is not None:
            logger.warning("We already have a client connected, ignoring new connection")
            await websocket.close()
            return
        self._websocket = websocket
        await self._callbacks.on_client_connected(websocket)
        if not self._session_timer_task and self._params.session_timeout:
            self._session_timer_task = self.create_task(
                self._session_timer(websocket, self._params.session_timeout)
            )
        try:
            async for message in websocket:
                if not self._params.serializer:
                    continue
                frame = await self._params.serializer.deserialize(message)
                if not frame:
                    continue
                if isinstance(frame, InputAudioRawFrame):
                    await self.push_audio_frame(frame)
                else:
                    await self.push_frame(frame)
        except Exception as e:
            logger.error(f"{self} exception receiving data: {e.__class__.__name__} ({e})")

        await self._callbacks.on_client_disconnected(websocket)
        if self._websocket:
            await self._websocket.close()
        self._websocket = None
        logger.info(f"Client {websocket.remote_address} disconnected")

    async def _session_timer(self, websocket, session_timeout):
        try:
            await asyncio.sleep(session_timeout)
            if websocket.state is not State.CLOSED:
                await self._callbacks.on_session_timeout(websocket)
        except asyncio.CancelledError:
            raise


class AsteriskWSServerOutputTransport(BaseOutputTransport):
    def __init__(self, transport: BaseTransport, params: AsteriskWSServerParams, **kwargs):
        super().__init__(params, **kwargs)
        self._transport = transport
        self._params = params or AsteriskWSServerParams()
        self._websocket: Optional[websockets.WebSocketServerProtocol] = None
        self._audio_buffer: asyncio.Queue[OutputAudioRawFrame] = asyncio.Queue(
            maxsize=self._params.local_audio_buffer_frames
        )
        self._buffer_consumer_task: Optional[asyncio.Task] = None
        self._buffer_state_monitor_task: Optional[asyncio.Task] = None
        self._audio_buffer_consumer_can_send = asyncio.Event()
        self._audio_buffer_bytes_buffered: int = 0
        self._initial_jitter_buffer_bytes: int = 0
        self._initial_jitter_buffer_is_filled: bool = False
        self._max_remote_audio_buffer_bytes: int = 0
        self._optimal_frame_size: Optional[int] = None
        self._ptime: float = 20
        self._remote_audio_buffer_bytes: int = 0
        self._remote_audio_buffer_is_full: bool = False
        self._remote_audio_buffer_resume_threshold_bytes: int = 0
        self._initialized = False

    async def set_client_connection(self, websocket: Optional[websockets.WebSocketServerProtocol]):
        if self._websocket is not None and websocket is not None:
            logger.warning(f"We already have a client connected, ignoring connection from {websocket.remote_address}")
            return
        self._websocket = websocket

    async def start(self, frame: StartFrame):
        await super().start(frame)
        if self._initialized:
            return
        self._initialized = True
        await self._params.serializer.setup(frame)
        await self.set_transport_ready(frame)

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._terminate(gracefully=True)

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._terminate()

    async def cleanup(self):
        await super().cleanup()
        await self._terminate()
        await self._transport.cleanup()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            await self._flush_buffers()
        elif (isinstance(frame, InputTransportMessageFrame) and
              frame.message.get("event") == "MEDIA_START"):
            await self._handle_media_start(frame.message)

    async def send_message(self, frame: OutputTransportMessageFrame | OutputTransportMessageUrgentFrame):
        if not self._websocket:
            return
        try:
            message = await self._params.serializer.serialize(frame)
            if message:
                await self._websocket.send(message)
        except Exception as e:
            logger.error(f"{self} exception sending message: {e}")

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        return await self._write_to_buffer(frame)

    async def _handle_media_start(self, message: dict):
        optimal_frame_size = message.get("optimal_frame_size")
        ptime = message.get("ptime")
        if optimal_frame_size is None or ptime is None:
            # Fallback defaults if Asterisk doesn't send them
            optimal_frame_size = 320 # 20ms of 8khz 16bit
            ptime = 20

        self._optimal_frame_size = int(optimal_frame_size)
        self._ptime = int(ptime) / 1000
        self._max_remote_audio_buffer_bytes = self._params.max_remote_audio_buffer_frames * self._optimal_frame_size
        self._remote_audio_buffer_resume_threshold_bytes = int(
            self._params.remote_audio_buffer_resume_threshold * self._max_remote_audio_buffer_bytes
        )

        if self._params.initial_jitter_buffer_ms > 0:
            self._initial_jitter_buffer_bytes = int(
                (self._params.initial_jitter_buffer_ms / int(ptime)) * self._optimal_frame_size
            )

        command = self._params.serializer.form_command("START_MEDIA_BUFFERING")
        if self._websocket:
            await self._websocket.send(command)

        self._buffer_state_monitor_task = self.create_task(self._buffer_state_monitor())
        self._buffer_consumer_task = self.create_task(self._buffer_consumer())

    async def _write_to_buffer(self, frame: OutputAudioRawFrame):
        try:
            payload = await self._params.serializer.serialize(frame)
        except Exception as e:
            logger.error(f"{self} exception serializing data: {e}")
            return False

        if payload:
            try:
                self._audio_buffer.put_nowait(payload)
                if self._params.initial_jitter_buffer_ms > 0 and not self._initial_jitter_buffer_is_filled:
                    self._audio_buffer_bytes_buffered += len(payload)
            except Exception as e:
                logger.error(f"Error adding frame to buffer: {e}")
                return False
        return True

    async def _buffer_consumer(self):
        if self._params.initial_jitter_buffer_ms > 0:
            while not self._initial_jitter_buffer_is_filled:
                if self._audio_buffer_bytes_buffered < self._initial_jitter_buffer_bytes:
                    await asyncio.sleep(0.02)
                else:
                    self._initial_jitter_buffer_is_filled = True

        self._audio_buffer_consumer_can_send.set()
        while True:
            await self._audio_buffer_consumer_can_send.wait()
            payload = await self._audio_buffer.get()
            try:
                if self._websocket:
                    await self._websocket.send(payload)
                    self._remote_audio_buffer_bytes += len(payload)
                    if self._remote_audio_buffer_bytes >= self._max_remote_audio_buffer_bytes:
                        self._remote_audio_buffer_is_full = True
                        self._audio_buffer_consumer_can_send.clear()
            except Exception as e:
                logger.error(f"{self} exception sending buffered data: {e}")
            self._audio_buffer.task_done()

    async def _buffer_state_monitor(self):
        while True:
            last_check_time = time.monotonic()
            await asyncio.sleep(self._ptime)
            current_time = time.monotonic()
            elapsed = current_time - last_check_time
            last_check_time = current_time
            bytes_consumed = round((elapsed / self._ptime) * self._optimal_frame_size)
            self._remote_audio_buffer_bytes = max(0, self._remote_audio_buffer_bytes - bytes_consumed)
            self._remote_audio_buffer_is_full = self._remote_audio_buffer_bytes >= self._max_remote_audio_buffer_bytes

            if self._audio_buffer_consumer_can_send.is_set():
                if self._remote_audio_buffer_is_full or self._audio_buffer.empty():
                    self._audio_buffer_consumer_can_send.clear()
            elif not self._remote_audio_buffer_is_full and not self._audio_buffer.empty():
                if self._remote_audio_buffer_bytes < self._remote_audio_buffer_resume_threshold_bytes:
                    self._audio_buffer_consumer_can_send.set()

    async def _flush_buffers(self):
        while not self._audio_buffer.empty():
            try:
                self._audio_buffer.get_nowait()
                self._audio_buffer.task_done()
            except asyncio.QueueEmpty:
                break
        self._remote_audio_buffer_bytes = 0
        self._remote_audio_buffer_is_full = False
        if self._params.serializer:
            payload = self._params.serializer.form_command("FLUSH_MEDIA")
            if self._websocket:
                await self._websocket.send(payload)

    async def _terminate(self, gracefully: bool = False):
        if gracefully:
            # Quick wait to drain
            start = time.time()
            while (not self._audio_buffer.empty() or self._remote_audio_buffer_bytes > 0) and (time.time() - start < 2.0):
                await asyncio.sleep(0.02)
        else:
            await self._flush_buffers()
        if self._buffer_consumer_task:
            await self.cancel_task(self._buffer_consumer_task)
            self._buffer_consumer_task = None
        if self._buffer_state_monitor_task:
            await self.cancel_task(self._buffer_state_monitor_task)
            self._buffer_state_monitor_task = None


class AsteriskWSServerTransport(BaseTransport):
    def __init__(
            self,
            params: AsteriskWSServerParams,
            input_name: Optional[str] = None,
            output_name: Optional[str] = None,
    ):
        super().__init__(input_name=input_name, output_name=output_name)
        self._params = params
        self._host = self._params.host
        self._port = self._params.port
        self._callbacks = WebsocketServerCallbacks(
            on_client_connected=self._on_client_connected,
            on_client_disconnected=self._on_client_disconnected,
            on_session_timeout=self._on_session_timeout,
            on_websocket_ready=self._on_websocket_ready,
        )
        self._input: Optional[AsteriskWSServerInputTransport] = None
        self._output: Optional[AsteriskWSServerOutputTransport] = None

        self._register_event_handler("on_client_connected")
        self._register_event_handler("on_client_disconnected")
        self._register_event_handler("on_session_timeout")
        self._register_event_handler("on_websocket_ready")

    def input(self) -> AsteriskWSServerInputTransport:
        if not self._input:
            self._input = AsteriskWSServerInputTransport(
                self, self._params, self._callbacks, name=self._input_name
            )
        return self._input

    def output(self) -> AsteriskWSServerOutputTransport:
        if not self._output:
            self._output = AsteriskWSServerOutputTransport(
                self, self._params, name=self._output_name
            )
        return self._output

    async def _on_client_connected(self, websocket):
        if self._output:
            await self._output.set_client_connection(websocket)
            await self._call_event_handler("on_client_connected", websocket)
        else:
            logger.error("A WebsocketServerTransport output is missing in the pipeline")

    async def _on_client_disconnected(self, websocket):
        if self._output:
            await self._output.set_client_connection(None)
            await self._output._terminate()
            await self._call_event_handler("on_client_disconnected", websocket)
        else:
            logger.error("A WebsocketServerTransport output is missing in the pipeline")

    async def _on_session_timeout(self, websocket):
        await self._call_event_handler("on_session_timeout", websocket)

    async def _on_websocket_ready(self):
        await self._call_event_handler("on_websocket_ready")