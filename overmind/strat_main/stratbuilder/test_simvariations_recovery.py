import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from overmind.strat_main.stratbuilder import SimVariations


class RemoteChunkLedgerTest(unittest.TestCase):
    @staticmethod
    def _read_states(events):
        with tempfile.TemporaryDirectory() as workdir:
            ledger = Path(workdir) / "remote_chunks.jsonl"
            ledger.write_text(
                "".join(json.dumps(event) + "\n" for event in events)
            )
            return SimVariations._read_remote_chunk_states(
                workdir, mock.Mock()
            )

    def test_heartbeat_does_not_replace_submitted_lifecycle_state(self):
        events = [
            {
                "run_id": "chunk_0045_GoingMerry",
                "experiment_id": "exp-1",
                "chunk_idx": 45,
                "variants": [101, 102],
                "status": "submitted",
            },
            {
                "run_id": "chunk_0045_GoingMerry",
                "status": "heartbeat",
                "completed_jobs": 59,
            },
        ]

        states = self._read_states(events)
        state = states["chunk_0045_GoingMerry"]
        self.assertEqual(state["last_status"], "heartbeat")
        self.assertEqual(state["last_lifecycle_status"], "submitted")
        self.assertEqual(state["completed_jobs"], 59)
        self.assertEqual(state["variants"], [101, 102])

    def test_heartbeat_does_not_reopen_scored_chunk(self):
        run_id = "chunk_0045_GoingMerry"
        states = self._read_states([
            {"run_id": run_id, "status": "submitted"},
            {"run_id": run_id, "status": "scored"},
            {"run_id": run_id, "status": "heartbeat"},
        ])

        self.assertEqual(states[run_id]["last_status"], "heartbeat")
        self.assertEqual(states[run_id]["last_lifecycle_status"], "scored")


if __name__ == "__main__":
    unittest.main()
