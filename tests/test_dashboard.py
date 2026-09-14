from __future__ import annotations

from io import StringIO
from pathlib import Path

from rich.console import Console

from mktux_backup.dashboard import render_run_state, watch_state
from mktux_backup.state import StateStore


def test_dashboard_renders_site_progress() -> None:
    stream = StringIO()
    console = Console(file=stream, force_terminal=False, width=120)
    payload = {
        "run_id": "run-123",
        "destination": "/backups",
        "status": "running",
        "message": "backup em execução",
        "sites": {
            "example": {
                "status": "running",
                "stage": "files",
                "bytes_done": 512,
                "bytes_total": 1024,
                "files_done": 1,
                "files_total": 2,
                "current_item": "uploads/photo.jpg",
            }
        },
    }

    console.print(render_run_state(payload, footer="q sair"))

    output = stream.getvalue()
    assert "run-123" in output
    assert "example" in output
    assert "50%" in output
    assert "uploads/photo.jpg" in output


def test_watch_once_reads_atomic_state(tmp_path: Path) -> None:
    payload = {
        "run_id": "run-1",
        "destination": str(tmp_path),
        "status": "success",
        "message": "concluído",
        "sites": {},
    }
    StateStore(tmp_path).write_payload(payload)
    stream = StringIO()
    console = Console(file=stream, force_terminal=False, width=120)

    returned = watch_state(Path(tmp_path), console, once=True)

    assert returned == payload
    assert "SUCESSO" in stream.getvalue()
