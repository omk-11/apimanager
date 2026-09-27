from fastapi import FastAPI
from fastapi.responses import RedirectResponse

RENDER_URL = "https://YOUR-RENDER-APP.onrender.com"  # <-- replace with your real Render URL, no trailing slash

app = FastAPI()

@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def redirect_all(full_path: str = ""):
    return RedirectResponse(url=f"{RENDER_URL}/{full_path}", status_code=307)
