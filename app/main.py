from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .m3u import M3UResolver, filter_entries
from .storage import JsonStore
from .stream_manager import StreamManager


app = FastAPI(
    title="Live Sync Control Plane",
    description=(
        "Backend APIs for M3U channel lookup, profile config, stream control, "
        "logs, snapshots, ROI, and offset state."
    ),
)
store = JsonStore()
resolver = M3UResolver()
manager = StreamManager(store, resolver)


class M3USearchRequest(BaseModel):
    playlist_url: str
    query: str = ""
    limit: int = Field(default=100, ge=1, le=500)


class StartRequest(BaseModel):
    profile_id: str = "default"


class OffsetRequest(BaseModel):
    offset_seconds: float


class ROIRequest(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)
    note: str | None = None


# Endpoints:
# GET  /health
# POST /m3u/search              body: {playlist_url, query, limit}
# POST /m3u/resolve             body: {playlist_url, query=<exact channel>, limit=1}
# GET  /profiles
# PUT  /profiles/{profile_id}   body: profile JSON
# GET  /profiles/{profile_id}
# DELETE /profiles/{profile_id}
# POST /stream/start            body: {profile_id}
# POST /stream/stop
# GET  /stream/status
# GET  /logs?limit=200
# POST /snapshot
# GET  /snapshot/latest
# GET/PUT /config/{profile_id}/offset
# GET/PUT /config/{profile_id}/roi


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/m3u/search")
def search_m3u(request: M3USearchRequest) -> dict[str, Any]:
    try:
        entries = resolver.fetch(request.playlist_url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "count": len(entries),
        "results": [entry.as_dict() for entry in filter_entries(entries, request.query, request.limit)],
    }


@app.post("/m3u/resolve")
def resolve_m3u(request: M3USearchRequest) -> dict[str, Any]:
    try:
        entry = resolver.resolve(request.playlist_url, request.query)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return entry.as_dict()


@app.get("/profiles")
def list_profiles() -> dict[str, Any]:
    return {"profiles": store.list_profiles()}


@app.get("/profiles/{profile_id}")
def get_profile(profile_id: str) -> dict[str, Any]:
    try:
        return store.load_profile(profile_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="profile not found") from exc


@app.put("/profiles/{profile_id}")
def put_profile(profile_id: str, profile: dict[str, Any]) -> dict[str, Any]:
    profile = dict(profile)
    profile["id"] = profile_id
    try:
        return store.save_profile(profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/profiles/{profile_id}")
def delete_profile(profile_id: str) -> dict[str, str]:
    try:
        store.delete_profile(profile_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="profile not found") from exc
    return {"status": "deleted"}


@app.post("/stream/start")
def start_stream(request: StartRequest) -> dict[str, Any]:
    try:
        profile = store.load_profile(request.profile_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="profile not found") from exc
    offset = store.load_offset(request.profile_id)
    if offset is not None and "offset_seconds" not in profile:
        profile["offset_seconds"] = offset
    try:
        return manager.start(profile)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/stream/stop")
def stop_stream() -> dict[str, Any]:
    return manager.stop(wait=False)


@app.get("/stream/status")
def stream_status() -> dict[str, Any]:
    return manager.status()


@app.get("/logs")
def logs(limit: int = Query(default=200, ge=1, le=1000)) -> dict[str, Any]:
    return {"logs": manager.logs(limit)}


@app.post("/snapshot")
def snapshot() -> dict[str, Any]:
    try:
        return manager.capture_snapshot()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/snapshot/latest")
def latest_snapshot() -> FileResponse:
    path = manager.status().get("snapshot_path")
    if not path:
        raise HTTPException(status_code=404, detail="no snapshot captured")
    return FileResponse(str(path), media_type="image/jpeg")


@app.get("/config/{profile_id}/offset")
def get_offset(profile_id: str) -> dict[str, Any]:
    return {"profile_id": profile_id, "offset_seconds": store.load_offset(profile_id)}


@app.put("/config/{profile_id}/offset")
def put_offset(profile_id: str, request: OffsetRequest) -> dict[str, Any]:
    store.save_offset(profile_id, request.offset_seconds)
    return {"profile_id": profile_id, "offset_seconds": request.offset_seconds}


@app.get("/config/{profile_id}/roi")
def get_roi(profile_id: str) -> dict[str, Any]:
    return {"profile_id": profile_id, "roi": store.load_roi(profile_id)}


@app.put("/config/{profile_id}/roi")
def put_roi(profile_id: str, request: ROIRequest) -> dict[str, Any]:
    roi = request.dict()
    store.save_roi(profile_id, roi)
    return {"profile_id": profile_id, "roi": roi}
