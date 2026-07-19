import os

from app.conversation.models import Turn


class TranscriptStore:
    def __init__(self, base_directory: str) -> None:
        """base_directory is the folder holding every session's transcript file."""
        self._base_directory = base_directory

    def transcript_path_for(self, session_id: str) -> str:
        """Deterministic path, e.g. base_directory/{session_id}.jsonl"""
        return os.path.join(self._base_directory, f"{session_id}.jsonl")

    async def append_turn(self, transcript_path: str, turn: Turn) -> None:
        """Append one turn as a single JSON line. Never rewrites the whole file."""
        os.makedirs(os.path.dirname(transcript_path), exist_ok=True)
        with open(transcript_path, "a") as f:
            f.write(turn.model_dump_json() + "\n")

    async def read_turns(self, transcript_path: str) -> list[Turn]:
        """Read and parse every line into Turn objects, oldest first."""
        if not os.path.exists(transcript_path):
            return []
        with open(transcript_path) as f:
            lines = [line for line in f.readlines() if line.strip()]
        return [Turn.model_validate_json(line) for line in lines]
