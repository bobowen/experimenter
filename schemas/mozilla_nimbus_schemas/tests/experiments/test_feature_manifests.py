from pathlib import Path

import pytest
import yaml

from mozilla_nimbus_schemas.experiments import DesktopFeatureManifest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "feature_manifests"


@pytest.mark.parametrize("manifest_file", FIXTURE_DIR.joinpath("desktop").iterdir())
def test_desktop_manifest_fixtures_are_valid(manifest_file):
    with manifest_file.open() as f:
        contents = yaml.safe_load(f)

    DesktopFeatureManifest.model_validate(contents)
