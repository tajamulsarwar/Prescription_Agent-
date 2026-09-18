from livekit.agents import function_tool, RunContext
import webbrowser


@function_tool
async def open_url(url: str, context: RunContext) -> str:
    """Opens a URL in the user's default web browser."""
    try:
        webbrowser.open(url)
        return f"Opened {url} in your web browser."
    except Exception as e:
        return f"Failed to open {url}. Error: {str(e)}"


def create_transfer_to_human_tool(room):
    """Factory function to create a transfer_to_human tool with room context bound."""
    from transfer_config import get_agent_config

    human_cfg = get_agent_config("human")
    sip_address = human_cfg["sip_transfer"]

    @function_tool
    async def transfer_to_human(reason: str, context: RunContext) -> str:
        """Transfer the call to a live human agent. IMPORTANT: Only call this AFTER your explanation message has finished speaking completely."""
        import asyncio
        from livekit import api
        from livekit.api import LiveKitAPI

        # Wait 10 seconds to allow the pre-transfer TTS audio to finish
        # playing before initiating the SIP transfer. This prevents the
        # call from being cut off mid-sentence (e.g., controlled substance
        # explanation which is ~18 seconds of speech).
        await asyncio.sleep(10)

        for participant in room.remote_participants.values():
            if participant.identity.startswith("sip_"):
                lk_api = LiveKitAPI()
                try:
                    await lk_api.sip.transfer_sip_participant(
                        api.TransferSIPParticipantRequest(
                            room_name=room.name,
                            participant_identity=participant.identity,
                            transfer_to=sip_address,
                        )
                    )
                finally:
                    await lk_api.aclose()
                return "Transferring you now. Please hold."

        return "I'm unable to complete the transfer at this time."

    return transfer_to_human


def create_end_call_tool(room):
    """Factory function to create an end_call tool that disconnects the SIP caller."""

    @function_tool
    async def end_call(context: RunContext) -> str:
        """End the current call and disconnect the caller. IMPORTANT: Only call this AFTER your farewell message has finished speaking completely."""
        import asyncio
        from livekit import api
        from livekit.api import LiveKitAPI

        # Wait 5 seconds to allow the farewell TTS audio to finish playing
        # before disconnecting the SIP participant. This prevents the call
        # from being cut off before the patient hears the full goodbye.
        await asyncio.sleep(5)

        for participant in room.remote_participants.values():
            if participant.identity.startswith("sip_"):
                lk_api = LiveKitAPI()
                try:
                    await lk_api.room.remove_participant(
                        api.RoomParticipantIdentity(
                            room=room.name,
                            identity=participant.identity,
                        )
                    )
                finally:
                    await lk_api.aclose()
                return "Call ended successfully. Do not generate any further text. Do not say anything. Do not acknowledge this message. Stop completely."

        return "Call ended successfully. The caller has already disconnected. Do not generate any further text. Do not say anything. Do not acknowledge this message. Stop completely."

    return end_call