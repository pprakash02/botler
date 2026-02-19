from google import genai
from loguru import logger


async def generate_meeting_minutes(transcript_log, api_key_passed):
    """
    Takes the raw transcript log and generates meeting minutes
    :param transcript_log:
    :return:
    """
    if not transcript_log:
        return "No transcript log"

    # New SDK Initialization
    try:
        client = genai.Client(api_key=api_key_passed)

        prompt = f"""
            You are an AI assistant generating Meeting Minutes (MoM) for a voice call.
            Based on the transcript below, provide a structured summary including:
            - Customer Name
            - Budget
            - Location
            - Investment Timeline
            - Key Discussion Points
            - Action Items for Sales Team (if any)
            Do not use asterisks in your output. it will be formatted as a plaintext file.
            Transcript:
            {transcript_log}
            """

        # Correct async call for the google-genai SDK
        response = await client.aio.models.generate_content(
            model='gemini-2.5-flash',  # Updated to a valid model ID for the new SDK
            contents=prompt
        )
        return response.text
    except Exception as e:
        logger.error(f"Error generating Meeting Minutes: {e}")
        return f"Error generating Meeting Minutes: {e}"