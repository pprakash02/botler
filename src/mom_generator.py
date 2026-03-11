import os
import asyncio
import json
import glob
from pathlib import Path

from groq import Groq
from sarvamai import SarvamAI
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

# Default directories (relative to src/)
DEFAULT_TRANSCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "transcripts")
DEFAULT_SUMMARIES_DIR = os.path.join(os.path.dirname(__file__), "generated_mom")


async def run_batch_transcript(api_key, audio_paths, transcripts_dir=None):
    """Transcribe audio files using Sarvam AI's batch API.

    Args:
        api_key: Sarvam AI API subscription key.
        audio_paths: List of absolute paths to .wav files to transcribe.
        transcripts_dir: Directory to download transcript JSONs into.
    """
    if not audio_paths:
        logger.warning("No audio files provided. Skipping transcription.")
        return

    if transcripts_dir is None:
        transcripts_dir = DEFAULT_TRANSCRIPTS_DIR

    os.makedirs(transcripts_dir, exist_ok=True)

    # All Sarvam SDK calls are synchronous / blocking, so we run them
    # in a thread to avoid stalling the async event loop.
    def _blocking_transcribe():
        client = SarvamAI(api_subscription_key=api_key)
        job = client.speech_to_text_job.create_job(
            model="saaras:v3",
            mode="translate",
            language_code="unknown",
            with_diarization=True,
            num_speakers=2,
        )

        job.upload_files(file_paths=audio_paths)
        job.start()
        job.wait_until_complete()

        file_results = job.get_file_results()

        logger.info(f"Transcription successful: {len(file_results['successful'])}")
        for f in file_results["successful"]:
            logger.info(f"  ✓ {f['file_name']}")

        if file_results["failed"]:
            logger.warning(f"Transcription failed: {len(file_results['failed'])}")
            for f in file_results["failed"]:
                logger.warning(f"  ✗ {f['file_name']}: {f['error_message']}")

        if file_results["successful"]:
            job.download_outputs(output_dir=transcripts_dir)
            logger.info(
                f"Downloaded {len(file_results['successful'])} transcript(s) to: {transcripts_dir}"
            )

    await asyncio.to_thread(_blocking_transcribe)


async def summarize_json_transcripts(mom_api_key, transcripts_dir=None, summaries_dir=None):
    """Summarize transcript JSONs into MoM text files using Groq.

    Args:
        mom_api_key: Groq API key.
        transcripts_dir: Directory containing transcript .json files.
        summaries_dir: Directory to write summary .txt files into.
    """
    if transcripts_dir is None:
        transcripts_dir = DEFAULT_TRANSCRIPTS_DIR
    if summaries_dir is None:
        summaries_dir = DEFAULT_SUMMARIES_DIR

    if not os.path.exists(transcripts_dir):
        logger.warning(f"Transcripts directory {transcripts_dir} does not exist yet.")
        return

    json_files = glob.glob(os.path.join(transcripts_dir, "*.json"))

    if not json_files:
        logger.warning(f"No JSON files found in {transcripts_dir}.")
        return

    os.makedirs(summaries_dir, exist_ok=True)

    # Use the Groq SDK
    client = Groq(api_key=mom_api_key)

    logger.info(f"Found {len(json_files)} JSON transcript(s). Starting summarization...")

    for file_path in json_files:
        base_name = os.path.basename(file_path)
        file_stem = os.path.splitext(base_name)[0]
        summary_file_path = os.path.join(summaries_dir, f"{file_stem}_summary.txt")

        # Skip files that already have a summary
        if os.path.exists(summary_file_path):
            continue

        logger.info(f"Processing: {base_name}...")
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Extract diarized transcript if available, else fall back to plain transcript
            transcript_text = ""
            if data.get("diarized_transcript") and data["diarized_transcript"].get("entries"):
                lines = []
                for entry in data["diarized_transcript"]["entries"]:
                    speaker = entry.get("speaker_id", "?")
                    text = entry.get("transcript", "").strip()
                    lines.append(f"Speaker {speaker}: {text}")
                transcript_text = "\n".join(lines)
            else:
                logger.info("  -> No diarization found. Falling back to standard transcript.")
                transcript_text = data.get("transcript", "")

            if not transcript_text.strip():
                logger.info("  -> Skipped: Transcript text is empty.")
                continue

            prompt = f"""\
You are an expert analyst. Read the following conversational transcript and provide a structured summary.
Include:
1. Main Topic/Objective
2. Key Points Discussed
3. Decisions Made or Action Items (if applicable)
(Your output will be formatted in plaintext so do not use * or special symbols)
(USE THE NAMES OF THE PERSONS IN THE CONVERSATION INSTEAD OF SPEAKER 0 AND SPEAKER 1 IF YOU CAN FIND THEIR NAMES FROM THE TRANSCRIPT)
Transcript:
{transcript_text}
"""

            # Groq call is synchronous — run in thread
            response = await asyncio.to_thread(
                client.chat.completions.create,
                model="openai/gpt-oss-120b",
                messages=[{"role": "user", "content": prompt}],
            )
            summary_text = response.choices[0].message.content

            with open(summary_file_path, "w", encoding="utf-8") as out_file:
                out_file.write(summary_text)

            logger.info(f"  -> Saved MoM to {summary_file_path}")

            # Respect API rate limits (15 RPM on free tier)
            await asyncio.sleep(4)

        except Exception as e:
            logger.error(f"  -> Error processing {base_name}: {e}")

    logger.info("Batch summarization complete!")


if __name__ == "__main__":
    test_paths = (
        glob.glob("./recordings/*.wav")
        + glob.glob("./recordings/*.mp3")
        + glob.glob("./recordings/*.mpeg")
    )
    asyncio.run(run_batch_transcript(os.getenv("SARVAM_API_KEY"), test_paths))
    asyncio.run(summarize_json_transcripts(os.getenv("MOM_GROQ_API_KEY")))