import os
import asyncio
import datetime
from dotenv import load_dotenv
from loguru import logger
import io
import aiofiles
import wave
# Pipeline & Core
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineTask, PipelineParams
from pipecat.frames.frames import LLMRunFrame, TTSSpeakFrame
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
# Audio & VAD
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams

# Services
from pipecat.services.groq.llm import GroqLLMService
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.services.piper.tts import PiperTTSService


# Aggregators & Context (Universal for 0.102)
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams
)
from pipecat.processors.aggregators.llm_context import LLMContext

# Turns & Strategies
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy

# Transport & Runner
from pipecat_tail.runner import TailRunner
from pipecat.runner.types import RunnerArguments
from asterisk import AsteriskWsFrameSerializer
from chan_ws_server import (
    AsteriskWSServerParams,
    AsteriskWSServerTransport,
)

# Custom internal module
from mom_generator import run_batch_transcript
from mom_generator import summarize_json_transcripts
load_dotenv(override=True)

PROMPT_FOR_LLM = """
You are an intelligent AI Voice Sales Agent named Botler for a premium real estate developer Yahu Builders. Your goal is to handle inbound calls from potential customers, qualify them by gathering specific details, and answer their queries naturally.

### YOUR OBJECTIVES (PROFILE THE USER)
You must casually guide the conversation to collect the following five pieces of information:
1. Name: The caller's name.
2. Location: Where do they want to buy?
3. Requirement: What are they looking for? (e.g., 3BHK, 4BHK, Villa, Plot)
4. Budget: What is their investment range?
5. Timeline: When do they plan to move or invest?

### CONTEXTUAL LOGIC & BEHAVIOR
- Tone & Style: Professional, warm, and concise but smooth enough for conversation.
- Keep responses SHORT (10-20 words).
- Your output will be used to generate speech (so do not use special characters).

### GUARDRAILS
- Do not hallucinate features or prices.
- Do not speak for more than 10 seconds at a time.
"""


async def save_audio_file(audio: bytes, filename: str, sample_rate: int, num_channels: int):
    """Save audio data to a WAV file."""

    if len(audio) > 0:
        with io.BytesIO() as buffer:
            with wave.open(buffer, "wb") as wf:
                wf.setsampwidth(2)
                wf.setnchannels(num_channels)
                wf.setframerate(sample_rate)
                wf.writeframes(audio)
            async with aiofiles.open(filename, "wb") as file:
                await file.write(buffer.getvalue())
        logger.info(f"Audio saved to {filename}")


async def run_bot(transport: AsteriskWSServerTransport, vad_analyzer: SileroVADAnalyzer, handle_sigint: bool):
    logger.info("Starting Botler...")
    file=""
    # Initialize Services
    audio_saved_event = asyncio.Event()
    saved_audio_path = ""
    tts = PiperTTSService(
        voice_id="en_US-amy-medium",
        use_cuda=True,
    )
    stt = WhisperSTTService(model="base", device="cuda", compute_type="float16")
    llm = GroqLLMService(
        api_key=os.getenv("GROQ_API_KEY"),
        model="openai/gpt-oss-120b",
    )

    context = LLMContext(
        messages=[{"role": "system", "content": PROMPT_FOR_LLM}],
    )

    audiobuffer = AudioBufferProcessor(
        num_channels=1
    )

    # 0.102 VAD Configuration
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=vad_analyzer,
            user_turn_strategies=UserTurnStrategies(
                stop=[
                    SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.5)
                ]
            )
        )
    )

    pipeline = Pipeline([
        transport.input(),
        stt,
        user_aggregator,
        llm,
        tts,
        transport.output(),
        audiobuffer,
        assistant_aggregator,
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(enable_metrics=True)
    )


    @llm.event_handler("on_completion_timeout")
    async def on_completion_timeout(service, *args):
        logger.warning("LLM completion timed out")
        await tts.queue_frame(TTSSpeakFrame("I am sorry. I did not get that."))

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected - starting conversation")
        await audiobuffer.start_recording()
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await audiobuffer.stop_recording()

        # 2. NEW: Wait for the exact file to finish saving to disk
        logger.info("Waiting for audio file to save to disk...")
        await audio_saved_event.wait()

        if saved_audio_path:
            logger.info(f"Triggering transcript for: {saved_audio_path}")
            # Pass the specific file as a list to mom_generator
            await run_batch_transcript(os.getenv("SARVAM_API_KEY"), [saved_audio_path])
            await summarize_json_transcripts(os.getenv("MOM_GROQ_API_KEY"))

        await task.cancel()

    @audiobuffer.event_handler("on_audio_data")
    async def on_audio_data(buffer, audio, sample_rate, num_channels):
        nonlocal saved_audio_path
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        recordings_dir = os.path.join(os.path.dirname(__file__), "recordings")
        os.makedirs(recordings_dir, exist_ok=True)
        filename = os.path.join(recordings_dir, f"{timestamp}.wav")
        await save_audio_file(audio, filename, sample_rate, num_channels)

        # Store the filename and signal that the file is ready
        saved_audio_path = filename
        audio_saved_event.set()

    runner = TailRunner()
    await runner.run(task)


async def bot(runner_args: RunnerArguments):
    vad_params = VADParams(
        start_secs=0.2,
        stop_secs=0.2
    )
    vad_analyzer = SileroVADAnalyzer(params=vad_params)

    transport = AsteriskWSServerTransport(
        params=AsteriskWSServerParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            serializer=AsteriskWsFrameSerializer(),
        )
    )
    await run_bot(transport, vad_analyzer, runner_args.handle_sigint)


if __name__ == "__main__":
    print("inbound/outbound:")
    # Default to inbound if empty to avoid blocking in some environments
    try:
        operation = input().strip().lower()
    except EOFError:
        operation = "inbound"

    if operation == "outbound":
        PROMPT_FOR_LLM = PROMPT_FOR_LLM.replace("handle inbound calls", "make outbound calls")

    runner_args = RunnerArguments()
    runner_args.handle_sigint = True

    try:
        asyncio.run(bot(runner_args))
    except KeyboardInterrupt:
        pass