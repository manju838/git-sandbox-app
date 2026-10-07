from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .exercises import EXERCISE_BY_ID, EXERCISES, Lab

app = FastAPI(title="Interactive Git Sandbox")

INDEX_HTML = Path(__file__).with_name("index.html")

# One in-memory lab for this local, single-user app.
lab = Lab()


class CommandRequest(BaseModel):
    command: str


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


@app.get("/api/exercises")
def list_exercises() -> list[dict]:
    return [{"id": e.id, "title": e.title, "area": e.area} for e in EXERCISES]


@app.get("/api/state")
def get_state() -> dict:
    return lab.payload()


@app.post("/api/command")
def run_command(request: CommandRequest) -> dict:
    output = lab.run(request.command)
    return {"output": output, **lab.payload()}


@app.post("/api/exercise/{exercise_id}")
def load_exercise(exercise_id: str) -> dict:
    if exercise_id not in EXERCISE_BY_ID:
        raise HTTPException(status_code=404, detail=f"Unknown exercise '{exercise_id}'")
    lab.load(exercise_id)
    return lab.payload()
