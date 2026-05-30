import asyncio
import logging
import os
from dotenv import load_dotenv

from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli, function_tool
from livekit.plugins import deepgram, elevenlabs, openai, silero

from pharmacy_functions import (
    check_available_slots as _check_slots,
    book_appointment as _book_appointment,
    check_appointment as _check_appointment,
    cancel_appointment as _cancel_appointment,
)

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("neha-child-care")

SYSTEM_PROMPT = """You are Priya, a warm receptionist at Neha Child Care clinic in India.
ALWAYS speak in Hinglish - natural Hindi+English mix, exactly like a real Indian receptionist on the phone.
Keep each response SHORT - max 2-3 sentences. Sound caring and conversational, never robotic.

Natural expressions to use: 'ji', 'haan ji', 'bilkul ji', 'theek hai ji', 'zaroor', 'acha'.
Say times as 'das baje', 'gyarah baje', 'paanch baje shaam ko' - NOT '10 AM'.
Refer to children as 'baccha', 'beta', 'beti'.

Clinic hours: Monday-Saturday, subah 10 se 12 aur shaam 5 se 7. Sunday band.

Booking flow - ask ONE thing at a time:
1. Bacche ka naam?
2. Umar kitni hai?
3. Aapka (parent) naam?
4. Contact number?
5. Call check_available_slots → mention slots naturally → preferred time?
6. Doctor ko kya dikhana hai?
7. Confirm: 'Toh main confirm karti hoon - [details]. Sahi hai na?'
8. Call book_appointment → give ID → 'Fifteen minute pehle aa jaiyega.'

For checking: use check_appointment. For cancelling: confirm first, then cancel_appointment."""


class NehaClinc(Agent):
    def __init__(self):
        super().__init__(
            instructions=SYSTEM_PROMPT,
            stt=deepgram.STT(
                model="nova-2",
                language="hi",
                keyterms=["namaste", "appointment", "baccha", "bukhar", "khansi", "Neha"],
            ),
            llm=openai.LLM(model="gpt-4o-mini"),
            tts=elevenlabs.TTS(
                voice_id="gHu9GtaHOXcSqFTK06ux",  # Anjali - Hindi female
                model="eleven_multilingual_v2",
                api_key=os.getenv("ELEVEN_LABS_API_KEY"),
                language="hi",
            ),
            vad=silero.VAD.load(),
            allow_interruptions=True,
        )

    @function_tool
    async def check_available_slots(self, preferred_day: str) -> str:
        """Check available appointment slots for a given day (Monday to Saturday)."""
        result = _check_slots(preferred_day)
        return str(result)

    @function_tool
    async def book_appointment(
        self,
        patient_name: str,
        patient_age: str,
        parent_name: str,
        contact_number: str,
        preferred_day: str,
        preferred_time: str,
        reason: str,
    ) -> str:
        """Book a doctor appointment. Only call after collecting ALL details from the caller."""
        result = _book_appointment(
            patient_name, patient_age, parent_name,
            contact_number, preferred_day, preferred_time, reason,
        )
        return str(result)

    @function_tool
    async def check_appointment(self, appointment_id: int) -> str:
        """Check details of an existing appointment by its ID."""
        result = _check_appointment(appointment_id)
        return str(result)

    @function_tool
    async def cancel_appointment(self, appointment_id: int) -> str:
        """Cancel an existing appointment by its ID."""
        result = _cancel_appointment(appointment_id)
        return str(result)


async def entrypoint(ctx: JobContext):
    await ctx.connect()
    logger.info("Caller connected to Neha Child Care agent")

    session = AgentSession()

    await session.start(
        agent=NehaClinc(),
        room=ctx.room,
    )

    await session.generate_reply(
        instructions="Greet the caller warmly in Hinglish and ask who the appointment is for."
    )


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(entrypoint_fnc=entrypoint)
    )
