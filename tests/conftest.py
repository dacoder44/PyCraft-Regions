import pytest

from mca_editor import Region
from mca_editor.fixtures import make_chunk, write_region


@pytest.fixture
def fixture_region(tmp_path):
    def create(*, documents=None, filename="r.0.0.mca", codec=2, **chunk_options):
        documents = [make_chunk(**chunk_options)] if documents is None else documents
        return Region(write_region(tmp_path / filename, documents, codec=codec)).load()

    return create


@pytest.fixture
def reopen(tmp_path):
    counter = 0

    def save(region):
        nonlocal counter
        counter += 1
        path = tmp_path / f"edited-{counter}.mca"
        region.save(path)
        return Region(
            path,
            region_coords=region.region_coords,
            dimension=region.dimension,
            metadata_policy=region.metadata_policy,
            block_entity_policy=region.block_entity_policy,
        ).load()

    return save
