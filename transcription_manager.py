import os
import json
from datetime import datetime
from typing import Any, Dict, List, Optional
 
 
class TranscriptionManager:
    """Manages in-memory transcription entries and persists them to disk.
 
    Tracks user speech, agent speech, and tool calls during a LiveKit session.
    Generates a human-readable summary and formatted transcript, and saves
    everything to a .txt file under a transcripts/ directory.
    """
 
    def __init__(self, session_id: str, caller_number: str = "unknown") -> None:
        self.session_id: str = session_id
        self.caller_number: str = caller_number or "unknown"
        self.transcript_entries: List[Dict[str, Any]] = []
        self.start_time: datetime = datetime.now()
        self.end_time: Optional[datetime] = None
        self.tools_used: List[str] = []
        self._closed: bool = False
        self._last_save_time: Optional[datetime] = None
        self._last_entry_count: int = 0
        self._auto_save_interval: int = 30  # Auto-save every 30 seconds
 
    # --- Capture methods -------------------------------------------------
    def add_user_message(self, text: str, timestamp: Optional[datetime] = None) -> None:
        try:
            if not text:
                return
            ts = timestamp or datetime.now()
            self.transcript_entries.append({
                "timestamp": ts.isoformat(),
                "speaker": "User",
                "message": text,
                "type": "speech",
            })
            self._try_auto_save()
        except Exception as e:
            print(f"[TRANSCRIPT] Error adding user message: {e}")
 
    def add_agent_message(self, text: str, timestamp: Optional[datetime] = None) -> None:
        try:
            if not text:
                return
            ts = timestamp or datetime.now()
            self.transcript_entries.append({
                "timestamp": ts.isoformat(),
                "speaker": "Agent",
                "message": text,
                "type": "speech",
            })
            self._try_auto_save()
        except Exception as e:
            print(f"[TRANSCRIPT] Error adding agent message: {e}")
 
    def add_tool_call(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        result: Any = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        try:
            ts = timestamp or datetime.now()
            if tool_name and tool_name not in self.tools_used:
                self.tools_used.append(tool_name)
            self.transcript_entries.append({
                "timestamp": ts.isoformat(),
                "speaker": "System",
                "message": f"Called tool: {tool_name}",
                "type": "tool_call",
                "tool_name": tool_name,
                "arguments": arguments or {},
                "result": (str(result)[:2000] if result is not None else None),
            })
            self._try_auto_save()
        except Exception as e:
            print(f"[TRANSCRIPT] Error adding tool call: {e}")
 
    def _try_auto_save(self) -> None:
        """Auto-save transcript periodically to prevent data loss."""
        try:
            now = datetime.now()
            entry_count = len(self.transcript_entries)
           
            # Check if we should auto-save (every 30 seconds or every 10 new entries)
            should_save = False
           
            if self._last_save_time is None:
                should_save = entry_count >= 5  # Save after first 5 entries
            else:
                time_since_save = (now - self._last_save_time).total_seconds()
                new_entries = entry_count - self._last_entry_count
                should_save = time_since_save >= self._auto_save_interval or new_entries >= 10
           
            if should_save:
                self._incremental_save()
                self._last_save_time = now
                self._last_entry_count = entry_count
        except Exception as e:
            # Don't let auto-save failures break the capture
            print(f"[TRANSCRIPT] Auto-save check failed: {e}")
   
    def _incremental_save(self) -> None:
        """Save transcript incrementally without finalizing."""
        try:
            os.makedirs("transcripts", exist_ok=True)
            filename = f"call_{self.start_time.strftime('%Y%m%d_%H%M%S')}_{self.caller_number}.txt"
            filepath = os.path.join("transcripts", filename)
           
            # Generate current state summary
            end = self.end_time or datetime.now()
            duration_secs = (end - self.start_time).total_seconds()
            minutes = int(duration_secs // 60)
            seconds = int(duration_secs % 60)
           
            user_messages = [e for e in self.transcript_entries if e.get("speaker") == "User"]
            agent_messages = [e for e in self.transcript_entries if e.get("speaker") == "Agent"]
            tool_calls = [e for e in self.transcript_entries if e.get("type") == "tool_call"]
           
            tools_used_block = ("\n".join(f"  - {tool}" for tool in self.tools_used) if self.tools_used else "  None")
           
            summary = (
                "=" * 80
                + "\nCALL TRANSCRIPTION SUMMARY (IN PROGRESS)\n"
                + "=" * 80
                + f"\nSession ID:        {self.session_id}"
                + f"\nCaller Number:     {self.caller_number}"
                + f"\nStart Time:        {self.start_time.strftime('%Y-%m-%d %I:%M:%S %p')}"
                + f"\nCurrent Time:      {end.strftime('%Y-%m-%d %I:%M:%S %p')}"
                + f"\nDuration So Far:   {minutes}m {seconds}s"
                + "\nStatistics:"
                + f"\n- User Messages:   {len(user_messages)}"
                + f"\n- Agent Messages:  {len(agent_messages)}"
                + f"\n- Tools Called:    {len(tool_calls)}"
                + "\nTools Used:\n"
                + tools_used_block
                + "\n" + "=" * 80
            )
           
            transcript_body = self.get_formatted_transcript()
           
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(summary)
                f.write("\n\nCALL TRANSCRIPT:\n")
                f.write("=" * 80 + "\n\n")
                f.write(transcript_body)
           
            print(f"[TRANSCRIPT] Auto-saved ({len(self.transcript_entries)} entries)")
        except Exception as e:
            print(f"[TRANSCRIPT] Incremental save failed: {e}")
 
    # --- Summary & formatting --------------------------------------------
    def generate_summary(self) -> str:
        self.end_time = datetime.now()
        duration_secs = (self.end_time - self.start_time).total_seconds()
        minutes = int(duration_secs // 60)
        seconds = int(duration_secs % 60)
 
        user_messages = [e for e in self.transcript_entries if e.get("speaker") == "User"]
        agent_messages = [e for e in self.transcript_entries if e.get("speaker") == "Agent"]
        tool_calls = [e for e in self.transcript_entries if e.get("type") == "tool_call"]
 
        tools_used_block = ("\n".join(f"  - {tool}" for tool in self.tools_used) if self.tools_used else "  None")
 
        summary = (
            "=" * 80
            + "\nCALL TRANSCRIPTION SUMMARY\n"
            + "=" * 80
            + f"\nSession ID:        {self.session_id}"
            + f"\nCaller Number:     {self.caller_number}"
            + f"\nStart Time:        {self.start_time.strftime('%Y-%m-%d %I:%M:%S %p')}"
            + f"\nEnd Time:          {self.end_time.strftime('%Y-%m-%d %I:%M:%S %p')}"
            + f"\nDuration:          {minutes}m {seconds}s"
            + "\nStatistics:"
            + f"\n- User Messages:   {len(user_messages)}"
            + f"\n- Agent Messages:  {len(agent_messages)}"
            + f"\n- Tools Called:    {len(tool_calls)}"
            + "\nTools Used:\n"
            + tools_used_block
            + "\n" + "=" * 80
        )
        return summary
 
    def get_formatted_transcript(self) -> str:
        lines: List[str] = []
        for entry in self.transcript_entries:
            try:
                timestamp = datetime.fromisoformat(entry["timestamp"])  # type: ignore[arg-type]
            except Exception:
                timestamp = datetime.now()
            time_str = timestamp.strftime('%I:%M:%S %p')
 
            if entry.get("type") == "tool_call":
                lines.append(f"[{time_str}] {entry.get('speaker')}: {entry.get('message')}")
                args = entry.get("arguments")
                if args:
                    try:
                        args_str = json.dumps(args, indent=2)
                    except Exception:
                        args_str = str(args)
                    lines.append(f"    Arguments: {args_str}")
                res = entry.get("result")
                if res is not None:
                    # Try to pretty format JSON-like results
                    pretty_res = None
                    if isinstance(res, (dict, list)):
                        try:
                            pretty_res = json.dumps(res, indent=2)
                        except Exception:
                            pretty_res = None
                    if pretty_res is None:
                        # Attempt JSON parse if string-like
                        try:
                            parsed = json.loads(res) if isinstance(res, str) else res
                            pretty_res = json.dumps(parsed, indent=2)
                        except Exception:
                            pretty_res = str(res)
                    lines.append(f"    Result: {pretty_res}")
            else:
                speaker = str(entry.get("speaker", "")).ljust(6)
                lines.append(f"[{time_str}] {speaker}: {entry.get('message')}")
        return "\n".join(lines)
 
    # --- Persistence ------------------------------------------------------
    def save_to_file(self, directory: str = "transcripts") -> Optional[str]:
        try:
            os.makedirs(directory, exist_ok=True)
            filename = f"call_{self.start_time.strftime('%Y%m%d_%H%M%S')}_{self.caller_number}.txt"
            filepath = os.path.join(directory, filename)
 
            summary = self.generate_summary()
            transcript_body = self.get_formatted_transcript()
 
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(summary)
                f.write("\n\nCALL TRANSCRIPT:\n")
                f.write("=" * 80 + "\n\n")
                f.write(transcript_body)
 
            print(f"[TRANSCRIPT] Saved to: {filepath}")
            return filepath
        except Exception as e:
            print(f"[TRANSCRIPT] Error saving: {e}")
            return None
 
    def save_json(self, directory: str = "transcripts") -> Optional[str]:
        """Optional: Save a machine-readable JSON alongside the .txt."""
        try:
            os.makedirs(directory, exist_ok=True)
            filename = f"call_{self.start_time.strftime('%Y%m%d_%H%M%S')}_{self.caller_number}.json"
            filepath = os.path.join(directory, filename)
 
            end = self.end_time or datetime.now()
            duration_seconds = (end - self.start_time).total_seconds()
 
            payload = {
                "session_id": self.session_id,
                "caller_number": self.caller_number,
                "start_time": self.start_time.isoformat(),
                "end_time": end.isoformat(),
                "duration_seconds": duration_seconds,
                "tools_used": self.tools_used,
                "transcript": self.transcript_entries,
            }
 
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
 
            print(f"[TRANSCRIPT] JSON saved to: {filepath}")
            return filepath
        except Exception as e:
            print(f"[TRANSCRIPT] Error saving JSON: {e}")
            return None
 
    # --- Idempotent finalize for guaranteed persistence -----------------
    def finalize(self, force: bool = False) -> None:
        """Persist the .txt transcript once at session end.
 
        If `force=True`, save even if the session hasn't been marked closed,
        useful for abrupt exits. Idempotent: subsequent calls are ignored.
        """
        try:
            marker_attr = "_finalized_marker"
            if getattr(self, marker_attr, False):
                return
            if not self._closed and not force:
                # Defer saving until session is closed, unless forced
                return
            txt = self.save_to_file()
            if txt:
                setattr(self, marker_attr, True)
                print(f"[TRANSCRIPT] Finalized successfully: {txt}")
        except Exception as e:
            print(f"[TRANSCRIPT] Error during finalize: {e}")
            # Try one last emergency save
            try:
                emergency_file = f"transcripts/emergency_{self.session_id}.txt"
                os.makedirs("transcripts", exist_ok=True)
                with open(emergency_file, "w", encoding="utf-8") as f:
                    f.write(f"Emergency save - Session: {self.session_id}\n")
                    f.write(f"Entries: {len(self.transcript_entries)}\n")
                    f.write("=" * 80 + "\n\n")
                    for entry in self.transcript_entries:
                        f.write(f"{entry.get('speaker', 'Unknown')}: {entry.get('message', 'N/A')}\n")
                print(f"[TRANSCRIPT] Emergency save successful: {emergency_file}")
            except Exception as emergency_err:
                print(f"[TRANSCRIPT] Emergency save also failed: {emergency_err}")
 
    def mark_session_closed(self) -> None:
        """Mark the session as closed to allow finalize to persist."""
        try:
            self._closed = True
            # Force one final auto-save before marking as closed
            self._incremental_save()
        except Exception as e:
            print(f"[TRANSCRIPT] Error in mark_session_closed: {e}")
 