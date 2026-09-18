import json
from dotenv import load_dotenv
from prompt import AGENT_INSTRUCTION, SESSION_INSTRUCTION
from livekit import agents
from livekit.agents import AgentSession, Agent, RoomInputOptions, BackgroundAudioPlayer, AudioConfig, BuiltinAudioClip
from livekit.plugins import openai, noise_cancellation, elevenlabs
from openai.types.realtime import AudioTranscription
from openai.types.realtime.realtime_audio_input_turn_detection import SemanticVad
from mcp_client import MCPServerSse
from mcp_client.agent_tools import MCPToolsIntegration
import os
from datetime import datetime
from tools import open_url, create_transfer_to_human_tool, create_end_call_tool
from db import fetch_tokens_from_both_dbs
from transcription_manager import TranscriptionManager

load_dotenv()


def load_tokens_from_db() -> None:
    """Fetch tokens from MySQL (primary) with SQL Server fallback, then set env vars."""
    try:
        tokens = fetch_tokens_from_both_dbs()
        ex_token = tokens.get("ex_auth_token", "")
        ws_token = tokens.get("ws_auth_token", "")

        if ex_token:
            os.environ["EHR_BEARER_TOKEN"] = ex_token
            print(f"[DB] EHR_BEARER_TOKEN set (len={len(ex_token)})")
        else:
            print("[DB] ex_auth_token missing from both databases")

        if ws_token:
            os.environ["COMPANION_TOKEN"] = ws_token
            print(f"[DB] COMPANION_TOKEN set (len={len(ws_token)})")
        else:
            print("[DB] ws_auth_token missing from both databases")
    except Exception as exc:
        print(f"[DB] Token fetch failed (agent continues): {exc}")


# Prefer DB tokens; fall back to any existing environment values
load_tokens_from_db()


class Assistant(Agent):
    def __init__(self, transfer_tool=None, end_call_tool=None) -> None:
        tools = [
            open_url,
        ]
        if transfer_tool:
            tools.append(transfer_tool)
        if end_call_tool:
            tools.append(end_call_tool)
        # Inject the actual day of the week into the prompt at runtime
        current_day = datetime.now().strftime("%A")  # e.g. "Thursday", "Friday"
        instructions = AGENT_INSTRUCTION.replace("{current_day_of_week}", current_day)
        super().__init__(instructions=instructions, tools=tools)


async def entrypoint(ctx: agents.JobContext):
    # Refresh tokens from DB at the start of each session
    load_tokens_from_db()

    # --- Resolve caller number for transcript filename ---
    caller_number = "unknown"
    for participant in ctx.room.remote_participants.values():
        if participant.identity.startswith("sip_"):
            caller_number = participant.identity.replace("sip_", "").split("@")[0]
            break

    transcript = TranscriptionManager(
        session_id=ctx.room.name,
        caller_number=caller_number,
    )

    session = AgentSession(
        llm=openai.realtime.RealtimeModel(
            turn_detection=SemanticVad(
                type="semantic_vad",
                eagerness="low",
                create_response=True,
                interrupt_response=True,
            ),
            input_audio_noise_reduction="far_field",
            input_audio_transcription=AudioTranscription(
                model="gpt-4o-transcribe",
            ),
        ),
        tts=elevenlabs.TTS(
            voice_id="2EiwWnXFnvU5JabPnXlBw",  # Hope voice
            model="eleven_multilingual_v2"
        )
    )

    transfer_to_human = create_transfer_to_human_tool(ctx.room)
    end_call = create_end_call_tool(ctx.room)

    mcp_server = MCPServerSse(
        params={"url": os.environ.get("N8N_MCP_SERVER_URL")},
        cache_tools_list=True,
        name="SSE MCP Server",
    )

    agent = await MCPToolsIntegration.create_agent_with_tools(
        agent_class=Assistant,
        agent_kwargs={"transfer_tool": transfer_to_human, "end_call_tool": end_call},
        mcp_servers=[mcp_server]
    )

    # --- Wire up transcription event listeners (before session.start) ---
    @session.on("user_input_transcribed")
    def on_user_speech(event):
        try:
            if event.is_final and event.transcript:
                transcript.add_user_message(event.transcript)
        except Exception as e:
            print(f"[TRANSCRIPT] user_input_transcribed error: {e}")

    @session.on("conversation_item_added")
    def on_conversation_item(event):
        try:
            item = event.item
            if getattr(item, "role", None) == "assistant":
                text = getattr(item, "text_content", None)
                if text:
                    transcript.add_agent_message(text)
        except Exception as e:
            print(f"[TRANSCRIPT] conversation_item_added error: {e}")

    @session.on("function_tools_executed")
    def on_tools_executed(event):
        try:
            for call, output in event.zipped():
                args = {}
                try:
                    args = json.loads(call.arguments) if call.arguments else {}
                except Exception:
                    args = {"raw": call.arguments}
                result = output.output if output else None
                transcript.add_tool_call(
                    tool_name=call.name,
                    arguments=args,
                    result=result,
                )
        except Exception as e:
            print(f"[TRANSCRIPT] function_tools_executed error: {e}")

    @session.on("close")
    def on_session_close(event):
        try:
            transcript.mark_session_closed()
            transcript.finalize(force=True)
        except Exception as e:
            print(f"[TRANSCRIPT] session close error: {e}")

    await session.start(
        room=ctx.room,
        agent=agent,
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVCTelephony(),
        ),
    )

    # Background audio: office ambience (continuous) + typing sounds (during tool calls)
    background_audio = BackgroundAudioPlayer(
        ambient_sound=AudioConfig("Noise_Background/crowd-talking-3.mp3", volume=0.05),
        thinking_sound=[
            AudioConfig(BuiltinAudioClip.KEYBOARD_TYPING, volume=0.8),
            AudioConfig(BuiltinAudioClip.KEYBOARD_TYPING2, volume=0.7),
        ],
    )
    await background_audio.start(room=ctx.room, agent_session=session)

    await ctx.connect()

    # Inject the actual day of the week into the session instruction
    current_day = datetime.now().strftime("%A")
    session_instruction = SESSION_INSTRUCTION.replace("{current_day_of_week}", current_day)

    await session.generate_reply(
        instructions=session_instruction,
    )


if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="appointment_prescription_agent",
        )
    )