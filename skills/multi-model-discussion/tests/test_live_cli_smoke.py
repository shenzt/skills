import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("live_cli_smoke.py")
SPEC = importlib.util.spec_from_file_location("live_cli_smoke_validation", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not import live CLI smoke validator")
SMOKE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SMOKE
SPEC.loader.exec_module(SMOKE)


class LiveCliSmokeValidationTests(unittest.TestCase):
    def manifest_for(self, participant, command):
        expected_model = SMOKE.EXPECTED_MODELS[participant]
        return {
            "profile_health": {"status": "healthy"},
            "results": [
                {
                    "participant_id": participant,
                    "status": "ok",
                    "parse_ok": True,
                    "requested_model": expected_model,
                    "observed_model": expected_model,
                    "command": command,
                }
            ],
        }

    def validate(self, participant, command):
        manifest = {
            **self.manifest_for(participant, command),
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            SMOKE.validate_manifest(path, (participant,))

    def test_default_fable_5_1_accepts_exact_inline_schema(self):
        participant = "claude"
        command = SMOKE.COUNCIL.command_for(participant, Path("/tmp/unused"))
        self.validate(participant, command)

    def test_default_claude_accepts_schema_path_marker(self):
        participant = "claude"
        command = SMOKE.COUNCIL.command_for(participant, Path("/tmp/unused"))
        command = SMOKE.COUNCIL.display_command(participant, command)
        self.validate(participant, command)

    def test_default_fable_5_1_rejects_modified_inline_schema(self):
        participant = "claude"
        command = SMOKE.COUNCIL.command_for(participant, Path("/tmp/unused"))
        schema_index = command.index("--json-schema") + 1
        command[schema_index] = '{"type":"object"}'
        manifest = self.manifest_for(participant, command)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(
                RuntimeError, "structured-output schema was not pinned"
            ):
                SMOKE.validate_manifest(path, (participant,))

    def test_default_fable_5_1_rejects_missing_schema_flag(self):
        participant = "claude"
        command = SMOKE.COUNCIL.command_for(participant, Path("/tmp/unused"))
        schema_index = command.index("--json-schema")
        del command[schema_index : schema_index + 2]
        manifest = self.manifest_for(participant, command)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(
                RuntimeError, "structured-output schema was not pinned"
            ):
                SMOKE.validate_manifest(path, (participant,))


if __name__ == "__main__":
    unittest.main()
