import os
import asyncio
import datetime
from dotenv import load_dotenv
from loguru import logger

# Pipeline & Core
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineTask, PipelineParams
from pipecat.frames.frames import LLMRunFrame, TTSSpeakFrame

# Audio & VAD
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams

# Services
from pipecat.services.google.llm import GoogleLLMService
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
from summarizer import generate_meeting_minutes

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
- Tone & Style: Professional, warm, and concise.
- Keep responses SHORT (10-20 words).
- Your output will be used to generate speech.

### GUARDRAILS
- Do not hallucinate features or prices.
- Do not speak for more than 10 seconds at a time.
"""


async def run_bot(transport: AsteriskWSServerTransport, vad_analyzer: SileroVADAnalyzer, handle_sigint: bool):
    logger.info("Starting Botler...")

    # Initialize Services
    tts = PiperTTSService(
        voice_id="en_US-amy-medium",
        use_cuda=True,
    )
    stt = WhisperSTTService(model="small.en", device="cuda", compute_type="float16")
    llm = GoogleLLMService(api_key=os.getenv("GEMINI_API_KEY"))

    context = LLMContext(
        messages=[{"role": "system", "content": PROMPT_FOR_LLM}],
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
        assistant_aggregator,
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(enable_metrics=True)
    )

    transcript_log = []

    # Updated signature to accept *args to prevent TypeError when extra context is passed
    @user_aggregator.event_handler("on_user_turn_stopped")
    async def on_user_turn_stopped(aggregator, text: str, *args):
        if text:
            transcript_log.append(f"user: {text}")

    # Updated signature to accept *args
    @assistant_aggregator.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(aggregator, text: str, *args):
        if text:
            transcript_log.append(f"assistant: {text}")

    async def save_transcript():
        if not transcript_log:
            logger.warning("No transcript to save.")
            return

        full_log = "\n".join(transcript_log)
        summary = await generate_meeting_minutes(full_log, os.getenv("MOM_GEMINI_API_KEY"))

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"MoM_{timestamp}.txt"

        with open(filename, "w") as f:
            f.write(summary)
        logger.info(f"Minutes of Meeting saved to {filename}")

    @llm.event_handler("on_completion_timeout")
    async def on_completion_timeout(service, *args):
        logger.warning("LLM completion timed out")
        await tts.queue_frame(TTSSpeakFrame("I am sorry. I did not get that."))

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected - starting conversation")
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await save_transcript()
        await task.cancel()

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