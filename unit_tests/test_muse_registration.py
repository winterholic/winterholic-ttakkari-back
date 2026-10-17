import unittest
from pathlib import Path
from uuid import uuid4

from ttakkari.config import Settings
from ttakkari.models import Engine
from ttakkari.runs.profiles import Profile, model_engine
from ttakkari.schemas import SessionIn


class MuseRegistrationTests(unittest.TestCase):
    def test_engine_is_a_first_class_session_choice(self):
        self.assertEqual(Engine('muse').value, 'muse')
        self.assertEqual(SessionIn(workspace_id=uuid4(), engine='muse').engine, 'muse')

    def test_non_coding_models_are_not_allowed_catalog_entries(self):
        models = Settings(_env_file=None).engine_models['muse']
        self.assertIn('muse-wonder-full-1.0', models)
        self.assertNotIn('sam-3.1', models)
        self.assertNotIn('muse-spark-1.1', models)

    def test_profile_uses_official_xdg_configuration(self):
        p = Profile(uuid4(), 'Muse', 'muse', config_dir=Path('/tmp/profile'))
        self.assertEqual(p.env()['XDG_CONFIG_HOME'], '/tmp/profile/config')
        self.assertEqual(p.env()['XDG_DATA_HOME'], '/tmp/profile/data')

    def test_muse_model_does_not_route_to_codex_or_antigravity(self):
        self.assertEqual(model_engine('muse-wonder-full-1.0'), 'muse')
