from types import SimpleNamespace

import pytest

from gateway.platforms.base import BasePlatformAdapter
from gateway.run import GatewayRunner


class RecordingAdapter:
    extract_local_files = staticmethod(BasePlatformAdapter.extract_local_files)

    def __init__(self):
        self.image_batches = []
        self.videos = []
        self.documents = []

    async def send_multiple_images(self, *, chat_id, images, metadata=None, human_delay=0):
        self.image_batches.append({"chat_id": chat_id, "images": images, "metadata": metadata})
        return SimpleNamespace(success=True)

    async def send_video(self, *, chat_id, video_path, metadata=None):
        self.videos.append({"chat_id": chat_id, "video_path": video_path, "metadata": metadata})
        return SimpleNamespace(success=True)

    async def send_document(self, *, chat_id, file_path, metadata=None):
        self.documents.append({"chat_id": chat_id, "file_path": file_path, "metadata": metadata})
        return SimpleNamespace(success=True)


@pytest.mark.asyncio
async def test_kanban_artifact_dispatch_matches_response_image_set(tmp_path):
    """Only response-dispatch image types use send_multiple_images."""
    png = tmp_path / "chart.png"
    svg = tmp_path / "diagram.svg"
    bmp = tmp_path / "screenshot.bmp"
    tiff = tmp_path / "scan.tiff"
    mp4 = tmp_path / "demo.mp4"
    pdf = tmp_path / "report.pdf"
    for path in (png, svg, bmp, tiff, mp4, pdf):
        path.write_bytes(b"artifact")

    adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)

    await runner._deliver_kanban_artifacts(
        adapter=adapter,
        chat_id="chat-1",
        metadata={"thread_id": "thread-1"},
        event_payload={"artifacts": [str(png), str(svg), str(bmp), str(tiff), str(mp4), str(pdf)]},
        task=None,
    )

    assert len(adapter.image_batches) == 1
    assert len(adapter.image_batches[0]["images"]) == 1
    assert adapter.image_batches[0]["images"][0][0].endswith("chart.png")

    assert [item["video_path"] for item in adapter.videos] == [str(mp4)]
    assert [item["file_path"] for item in adapter.documents] == [
        str(svg),
        str(bmp),
        str(tiff),
        str(pdf),
    ]


@pytest.mark.asyncio
async def test_kanban_artifact_dispatch_dedupes_and_ignores_missing_paths(tmp_path):
    artifact = tmp_path / "result.csv"
    artifact.write_text("a,b\n1,2\n", encoding="utf-8")
    missing = tmp_path / "missing.csv"

    adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)

    await runner._deliver_kanban_artifacts(
        adapter=adapter,
        chat_id="chat-1",
        metadata={},
        event_payload={"artifacts": [str(artifact), str(artifact), str(missing)]},
        task=SimpleNamespace(result=f"legacy reference {artifact}"),
    )

    assert adapter.image_batches == []
    assert adapter.videos == []
    assert [item["file_path"] for item in adapter.documents] == [str(artifact)]
